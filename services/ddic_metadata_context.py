import re
import inspect

from services.ddic_metadata_provider import NoOpDdicMetadataProvider, dedupe_table_names
from services.callable_signature_provider import normalize_provider_signatures


LOCAL_PREFIXES = ("TY_", "T_", "LT_", "GT_", "LS_", "GS_", "ST_", "WA_", "W_", "GV_", "LV_", "P_", "S_")
SPECIFICATION_MODE = "specification"
ABAP_SOURCE_MODE = "abap_source"

ABAP_KEYWORDS = {
    "ADD",
    "APPEND",
    "AT",
    "AUTHORITY-CHECK",
    "BACK",
    "BREAK-POINT",
    "CALL",
    "CASE",
    "CATCH",
    "CHECK",
    "CLASS",
    "CLEAR",
    "CLOSE",
    "COLLECT",
    "COMMIT",
    "CONCATENATE",
    "CONSTANTS",
    "CONTINUE",
    "CREATE",
    "DATA",
    "DELETE",
    "DESCRIBE",
    "DO",
    "ELSE",
    "ELSEIF",
    "END",
    "ENDAT",
    "ENDCASE",
    "ENDCLASS",
    "ENDDO",
    "ENDFORM",
    "ENDIF",
    "ENDLOOP",
    "ENDMETHOD",
    "ENDMODULE",
    "ENDSELECT",
    "ENDTRY",
    "EXIT",
    "EXPORTING",
    "FETCH",
    "FIELD",
    "FIELD-SYMBOLS",
    "FOR",
    "FORM",
    "FREE",
    "FROM",
    "FUNCTION",
    "IF",
    "IMPORTING",
    "IN",
    "INCLUDE",
    "INFOTYPES",
    "INSERT",
    "INTO",
    "IS",
    "JOIN",
    "LEAVE",
    "LIKE",
    "LOOP",
    "MESSAGE",
    "METHOD",
    "MODIFY",
    "MODULE",
    "MOVE",
    "NEW",
    "OPEN",
    "OR",
    "OF",
    "PARAMETER",
    "PARAMETERS",
    "PERFORM",
    "RAISE",
    "RAISING",
    "RANGES",
    "READ",
    "RECEIVING",
    "REFRESH",
    "REPORT",
    "RETURN",
    "RETURNING",
    "ROLLBACK",
    "SELECT",
    "SELECTION",
    "SELECTION-SCREEN",
    "SET",
    "SKIP",
    "SORT",
    "SPLIT",
    "START",
    "START-OF-SELECTION",
    "STATICS",
    "STOP",
    "SUBMIT",
    "SUM",
    "TABLE",
    "TABLES",
    "TRANSFER",
    "TRY",
    "TYPE",
    "TYPES",
    "UPDATE",
    "USING",
    "VALUE",
    "WHEN",
    "WHERE",
    "WHILE",
    "WRITE",
}

OBJECT_PATTERN = r"(?:/[A-Za-z0-9_]+/[A-Za-z0-9_]+|[A-Za-z][A-Za-z0-9_]*)"
FIELD_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"


def extract_relevant_ddic_names(*texts, mode=SPECIFICATION_MODE):
    dependencies = extract_typed_ddic_dependencies(*texts, mode=mode)
    ordered_names = []
    for dependency in dependencies:
        if dependency.get("kind") in {"ddic_table", "ddic_structure"}:
            ordered_names.append(dependency.get("name"))
        elif dependency.get("kind") == "ddic_field":
            ordered_names.append(dependency.get("object"))
    return [name for name in dedupe_table_names(ordered_names) if is_valid_ddic_object_name(name)]


def extract_typed_ddic_dependencies(*texts, mode=SPECIFICATION_MODE):
    positioned = []
    offset = 0
    for text in texts:
        if not text:
            continue
        cleaned = strip_comments_and_strings(str(text), mode=mode)
        for dependency in typed_ddic_dependencies_from_text(cleaned):
            positioned.append((offset + dependency.pop("_position", 0), dependency))
        offset += len(cleaned) + 1
    ordered = [dependency for _position, dependency in sorted(positioned, key=lambda item: item[0])]
    return dedupe_typed_dependencies(ordered)


def extract_relevant_ddic_names_from_source(*texts):
    return extract_relevant_ddic_names(*texts, mode=ABAP_SOURCE_MODE)


def extract_post_generation_ddic_names_from_source(*texts):
    positioned_names = []
    offset = 0
    for text in texts:
        if not text:
            continue
        cleaned = strip_comments_and_strings(str(text), mode=ABAP_SOURCE_MODE)
        names = []
        names.extend(extract_qualified_reference_tables(cleaned))
        names.extend(extract_type_like_references(cleaned))
        names.extend(extract_sql_tables(cleaned))
        names.extend(extract_tables_statements(cleaned))
        upper_cleaned = cleaned.upper()
        for name in names:
            position = upper_cleaned.find(name)
            positioned_names.append((offset + position if position >= 0 else offset + len(cleaned), name))
        offset += len(cleaned) + 1
    ordered_names = [name for _position, name in sorted(positioned_names, key=lambda item: item[0])]
    return [name for name in dedupe_table_names(ordered_names) if is_valid_ddic_object_name(name)]


def extract_ambiguous_standalone_type_like_names_from_source(*texts):
    names = []
    for text in texts:
        if not text:
            continue
        cleaned = strip_comments_and_strings(str(text), mode=ABAP_SOURCE_MODE)
        for match in re.finditer(
            rf"\b(?:TYPE|LIKE)\s+(?:STANDARD\s+TABLE\s+OF\s+|SORTED\s+TABLE\s+OF\s+|HASHED\s+TABLE\s+OF\s+)?({OBJECT_PATTERN})\b(?!\s*-)",
            cleaned,
            re.IGNORECASE,
        ):
            candidate = match.group(1).upper()
            if is_valid_ddic_object_name(candidate) and not is_builtin_abap_type(candidate):
                names.append(candidate)
    return dedupe_table_names(names)


def retrieve_ddic_metadata(table_names, provider=None, progress_callback=None):
    provider = provider or NoOpDdicMetadataProvider()
    names = [name for name in dedupe_table_names(table_names) if is_valid_ddic_object_name(name)]
    if not names:
        return {"tables": {}}
    metadata = call_get_tables(provider, names, progress_callback=progress_callback)
    if not isinstance(metadata, dict):
        return {"tables": {}}
    tables = metadata.get("tables")
    if isinstance(tables, dict):
        return {"tables": tables}
    return {"tables": {}}


def retrieve_missing_ddic_metadata(existing_metadata, candidate_names, provider=None, progress_callback=None):
    existing_tables = normalized_tables(existing_metadata)
    missing = [
        name
        for name in dedupe_table_names(candidate_names)
        if is_valid_ddic_object_name(name) and name not in existing_tables
    ]
    if not missing:
        return existing_metadata or {"tables": {}}
    return merge_ddic_metadata(existing_metadata, retrieve_ddic_metadata(missing, provider, progress_callback=progress_callback))


def call_get_tables(provider, names, progress_callback=None):
    if progress_callback and "progress_callback" in inspect.signature(provider.get_tables).parameters:
        return provider.get_tables(names, progress_callback=progress_callback)
    return provider.get_tables(names)


def merge_ddic_metadata(*metadata_items):
    merged = {"tables": {}}
    diagnostics = []
    for metadata in metadata_items:
        if not isinstance(metadata, dict):
            continue
        for table_name, table_metadata in normalized_tables(metadata).items():
            merged["tables"][table_name] = table_metadata
        if isinstance(metadata.get("_diagnostics"), list):
            diagnostics.extend(metadata["_diagnostics"])
    if diagnostics:
        merged["_diagnostics"] = diagnostics
    return merged


def render_compact_ddic_catalogue(metadata, max_fields_per_table=40):
    tables = normalized_tables(metadata)
    if not tables:
        return ""
    lines = [
        "SAP DDIC metadata catalogue:",
        "- This catalogue is internal verified metadata. Use these table-field names exactly.",
        "- Do not invent SAP table fields or substitute similar-looking field names.",
    ]
    for table_name in sorted(tables):
        table_metadata = tables[table_name]
        fields = normalized_fields(table_metadata)
        field_names = ordered_field_names(table_metadata, fields)
        field_parts = []
        for field_name in field_names[:max_fields_per_table]:
            field = fields[field_name]
            details = field_detail(field)
            field_parts.append(f"{field_name}{details}")
        if field_parts:
            lines.append(f"- {table_name}: " + ", ".join(field_parts))
        else:
            lines.append(f"- {table_name}: no fields returned")
    return "\n".join(lines)


def ordered_field_names(table_metadata, fields):
    ordered = []
    for field_name in table_metadata.get("field_order", []) if isinstance(table_metadata, dict) else []:
        normalized_name = str(field_name or "").upper()
        if normalized_name in fields and normalized_name not in ordered:
            ordered.append(normalized_name)
    ordered.extend(field_name for field_name in sorted(fields) if field_name not in ordered)
    return ordered


def append_ddic_catalogue(prompt_text, metadata):
    catalogue = render_compact_ddic_catalogue(metadata)
    if not catalogue:
        return prompt_text
    return f"{prompt_text.rstrip()}\n\n{catalogue}\n"


def append_callable_catalogue(prompt_text, callable_metadata):
    signatures = normalize_provider_signatures(callable_metadata)
    if not signatures:
        return prompt_text
    lines = [
        "SAP callable signature catalogue:",
        "- This catalogue is internal verified metadata. Use callable names and parameters exactly.",
    ]
    for callable_name in sorted(signatures):
        signature = signatures[callable_name]
        params = signature.get("parameters", signature) if isinstance(signature, dict) else {}
        if isinstance(params, dict) and params:
            parts = []
            for name in sorted(params):
                parameter = params.get(name, {})
                if isinstance(parameter, dict):
                    direction = str(parameter.get("direction") or "").upper()
                    abap_type = str(parameter.get("abap_type") or parameter.get("type") or "").upper()
                    detail = " ".join(part for part in (direction, abap_type) if part)
                    parts.append(f"{str(name).upper()}{f' [{detail}]' if detail else ''}")
                else:
                    parts.append(str(name).upper())
            returning = signature.get("returning") if isinstance(signature, dict) else None
            if isinstance(returning, dict) and returning.get("name"):
                return_type = str(returning.get("abap_type") or returning.get("type") or "").upper()
                parts.append(f"{str(returning.get('name')).upper()} [RETURNING{f' {return_type}' if return_type else ''}]")
            lines.append(f"- {callable_name}: " + ", ".join(parts))
        else:
            lines.append(f"- {callable_name}: signature metadata returned")
    return f"{prompt_text.rstrip()}\n\n" + "\n".join(lines) + "\n"


def ddic_identifier_provenance(metadata, source="sap-metadata"):
    provenance = {}
    for table_name, table_metadata in normalized_tables(metadata).items():
        for field_name in normalized_fields(table_metadata):
            identifier = f"{table_name}-{field_name}".upper()
            provenance[identifier] = {"identifier": identifier, "source": source}
    return provenance


def has_ddic_metadata(metadata):
    return bool(normalized_tables(metadata))


def normalized_tables(metadata):
    if not isinstance(metadata, dict):
        return {}
    tables = metadata.get("tables") if isinstance(metadata.get("tables"), dict) else metadata
    if not isinstance(tables, dict):
        return {}
    return {str(name).upper(): value for name, value in tables.items() if is_valid_ddic_object_name(str(name).upper())}


def normalized_fields(table_metadata):
    if isinstance(table_metadata, dict):
        fields = table_metadata.get("fields") or table_metadata.get("components") or {}
    else:
        fields = table_metadata
    if isinstance(fields, dict):
        return {str(name).upper(): value for name, value in fields.items() if is_probable_field_name(str(name).upper())}
    if isinstance(fields, (list, tuple, set)):
        return {str(name).upper(): {"name": str(name).upper()} for name in fields if is_probable_field_name(str(name).upper())}
    return {}


def field_detail(field):
    if not isinstance(field, dict):
        return ""
    details = []
    datatype = str(field.get("datatype") or "").upper()
    length = field.get("length")
    decimals = field.get("decimals")
    if datatype:
        type_text = datatype
        if length:
            type_text += f"({length}"
            if decimals:
                type_text += f",{decimals}"
            type_text += ")"
        details.append(type_text)
    if field.get("key"):
        details.append("key")
    description = str(field.get("description") or "").strip()
    if description:
        details.append(description)
    return f" [{'; '.join(details)}]" if details else ""


def strip_comments_and_strings(text, mode=SPECIFICATION_MODE):
    lines = []
    for line in text.splitlines():
        if mode == ABAP_SOURCE_MODE and line.startswith("*"):
            continue
        code = line.split('"', 1)[0]
        if mode == SPECIFICATION_MODE:
            code = code.replace("`", "")
        lines.append(re.sub(r"'[^']*'", "''", code))
    return "\n".join(lines)


def typed_ddic_dependencies_from_text(text):
    dependencies = []
    dependencies.extend(extract_typed_named_metadata_references(text))
    dependencies.extend(extract_typed_table_read_section_headings(text))
    dependencies.extend(extract_typed_prose_table_references(text))
    dependencies.extend(extract_typed_qualified_field_references(text))
    dependencies.extend(extract_typed_sql_tables(text))
    dependencies.extend(extract_typed_tables_statements(text))
    dependencies.extend(extract_typed_read_tables(text))
    dependencies.extend(extract_typed_declared_type_references(text))
    return dependencies


def extract_typed_named_metadata_references(text):
    dependencies = []
    patterns = [
        ("ddic_table", rf"\bSAP\s+table\s*:?\s*({OBJECT_PATTERN})\b", False),
        ("ddic_table", rf"\btable\s*:\s*({OBJECT_PATTERN})\b", False),
        ("ddic_table", rf"\btable\s+({OBJECT_PATTERN})\b", True),
        ("ddic_structure", rf"\bSAP\s+structure\s*:?\s*({OBJECT_PATTERN})\b", False),
        ("ddic_structure", rf"\bstructure\s*:\s*({OBJECT_PATTERN})\b", False),
        ("ddic_structure", rf"\bstructure\s+({OBJECT_PATTERN})\b", True),
        ("ddic_structure", rf"\bSAP\s+view\s*:?\s*({OBJECT_PATTERN})\b", False),
        ("ddic_structure", rf"\bview\s*:\s*({OBJECT_PATTERN})\b", False),
        ("ddic_structure", rf"\bview\s+({OBJECT_PATTERN})\b", True),
    ]
    for kind, pattern, require_strong in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if require_strong and not is_strong_literal_object(match.group(1)):
                continue
            add_typed_object_dependency(dependencies, kind, match.group(1), match.start(1), "metadata_label")
    for match in re.finditer(
        rf"\b(?:reference\s+field|field)\s*:\s*({OBJECT_PATTERN})-({FIELD_PATTERN})\b",
        text,
        re.IGNORECASE,
    ):
        add_typed_field_dependency(dependencies, match.group(1), match.group(2), match.start(1), "reference_field_label")
    return dependencies


def extract_typed_table_read_section_headings(text):
    dependencies = []
    in_table_read_section = False
    section_level = None
    offset = 0
    for line in str(text or "").splitlines(True):
        heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            level = len(heading.group(1))
            heading_text = heading.group(2).strip()
            heading_position = offset + heading.start(2)
            if re.search(r"\b(?:table|database)\s+reads?\b", heading_text, re.IGNORECASE):
                in_table_read_section = True
                section_level = level
            elif in_table_read_section and section_level is not None and level <= section_level:
                in_table_read_section = False
                section_level = None
            elif in_table_read_section:
                object_match = re.match(rf"({OBJECT_PATTERN})\b", heading_text, re.IGNORECASE)
                if object_match and is_strong_literal_object(object_match.group(1)):
                    add_typed_object_dependency(
                        dependencies,
                        "ddic_table",
                        object_match.group(1),
                        heading_position + object_match.start(1),
                        "table_read_heading",
                    )
        elif in_table_read_section:
            read_match = re.match(
                rf"^\s*(?:\d+[.)]\s*)?(?:[-*]\s*)?Read\s+({OBJECT_PATTERN})\b",
                line,
                re.IGNORECASE,
            )
            if read_match and is_strong_literal_object(read_match.group(1)):
                add_typed_object_dependency(
                    dependencies,
                    "ddic_table",
                    read_match.group(1),
                    offset + read_match.start(1),
                    "table_read_entry",
                )
        offset += len(line)
    return dependencies


def extract_typed_prose_table_references(text):
    dependencies = []
    patterns = [
        rf"\bread\s+(?:the\s+)?(?:current\s+)?({OBJECT_PATTERN})\s+records?\b",
        rf"\bread\s+(?:the\s+)?(?:current\s+)?({OBJECT_PATTERN})(?=\s*(?:[.;:]|$))",
        rf"\bbased\s+on\s+({OBJECT_PATTERN})\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            candidate = match.group(1)
            if is_strong_literal_object(candidate):
                add_typed_object_dependency(
                    dependencies,
                    "ddic_table",
                    candidate,
                    match.start(1),
                    "prose_table_reference",
                )
    return dependencies


def extract_typed_qualified_field_references(text):
    dependencies = []
    for match in re.finditer(rf"(?<![A-Za-z0-9_/])({OBJECT_PATTERN})-({FIELD_PATTERN})\b", text):
        object_name = match.group(1)
        field_name = match.group(2)
        if is_strong_literal_object(object_name):
            add_typed_field_dependency(dependencies, object_name, field_name, match.start(1), "qualified_field")
    return dependencies


def extract_typed_sql_tables(text):
    dependencies = []
    patterns = [
        rf"\bSELECT\b[\s\S]{{0,500}}?\bFROM\s+({OBJECT_PATTERN})\b",
        rf"\bJOIN\s+({OBJECT_PATTERN})\b",
        rf"\bUPDATE\s+({OBJECT_PATTERN})\b",
        rf"\bMODIFY\s+({OBJECT_PATTERN})\b",
        rf"\bDELETE\s+FROM\s+({OBJECT_PATTERN})\b",
        rf"\bINSERT\s+(?:INTO\s+)?(?:TABLE\s+)?({OBJECT_PATTERN})\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            add_typed_object_dependency(dependencies, "ddic_table", match.group(1), match.start(1), "sql_table")
    return dependencies


def extract_typed_tables_statements(text):
    dependencies = []
    for match in re.finditer(rf"^\s*TABLES\s*:?\s*({OBJECT_PATTERN})\b", text, re.IGNORECASE | re.MULTILINE):
        add_typed_object_dependency(dependencies, "ddic_table", match.group(1), match.start(1), "tables_statement")
    return dependencies


def extract_typed_read_tables(text):
    dependencies = []
    for match in re.finditer(rf"\bREAD\s+TABLE\s+({OBJECT_PATTERN})\b", text, re.IGNORECASE):
        add_typed_object_dependency(dependencies, "ddic_table", match.group(1), match.start(1), "read_table")
    return dependencies


def extract_typed_declared_type_references(text):
    dependencies = []
    for match in re.finditer(
        rf"\b(?:TYPE|LIKE)\s+(?:STANDARD\s+TABLE\s+OF\s+|SORTED\s+TABLE\s+OF\s+|HASHED\s+TABLE\s+OF\s+)?({OBJECT_PATTERN})-({FIELD_PATTERN})\b",
        text,
        re.IGNORECASE,
    ):
        add_typed_field_dependency(dependencies, match.group(1), match.group(2), match.start(1), "declared_field_type")
    for match in re.finditer(
        rf"\b(?:reference\s+type|data\s+type|ddic\s+type|data\s+element)\s*:\s*({OBJECT_PATTERN})\b",
        text,
        re.IGNORECASE,
    ):
        add_typed_type_dependency(dependencies, match.group(1), match.start(1), "type_label")
    for match in re.finditer(rf"^\s*[PS]_[A-Za-z0-9_]+\s+({OBJECT_PATTERN})\b(?!\s*-)", text, re.IGNORECASE | re.MULTILINE):
        candidate = match.group(1)
        if is_strong_literal_object(candidate):
            add_typed_type_dependency(dependencies, candidate, match.start(1), "selection_screen_type")
    return dependencies


def add_typed_object_dependency(dependencies, kind, name, position, source):
    candidate = str(name or "").upper()
    if is_valid_ddic_object_name(candidate):
        dependencies.append({"kind": kind, "name": candidate, "source": source, "_position": position})


def add_typed_field_dependency(dependencies, object_name, field_name, position, source):
    object_key = str(object_name or "").upper()
    field_key = str(field_name or "").upper()
    if is_valid_ddic_object_name(object_key) and is_probable_field_name(field_key):
        dependencies.append(
            {
                "kind": "ddic_field",
                "name": f"{object_key}-{field_key}",
                "object": object_key,
                "field": field_key,
                "source": source,
                "_position": position,
            }
        )


def add_typed_type_dependency(dependencies, name, position, source):
    candidate = str(name or "").upper()
    if is_valid_ddic_object_name(candidate) and not is_builtin_abap_type(candidate):
        dependencies.append({"kind": "ddic_type", "name": candidate, "source": source, "_position": position})


def dedupe_typed_dependencies(dependencies):
    seen = set()
    result = []
    for dependency in dependencies or []:
        key = (
            dependency.get("kind"),
            dependency.get("name"),
            dependency.get("object"),
            dependency.get("field"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dependency)
    return result


def extract_read_tables(text):
    names = []
    for match in re.finditer(rf"\bREAD(?:\s+TABLE)?\s+({OBJECT_PATTERN})\b", text, re.IGNORECASE):
        if is_strong_literal_object(match.group(1)):
            candidate = match.group(1).upper()
            if is_valid_ddic_object_name(candidate):
                names.append(candidate)
    return names


def extract_qualified_reference_tables(text):
    names = []
    patterns = [
        rf"\b(?:field|fields|for|where|and|or|by|using|with|on|equals?|=)\s+({OBJECT_PATTERN})-({FIELD_PATTERN})\b",
        rf"\b({OBJECT_PATTERN})-({FIELD_PATTERN})\s+(?:in|=|eq|ne|lt|le|gt|ge|between|is|like)\b",
    ]
    for pattern in patterns:
        names.extend(extract_first_group(text, pattern))
    for match in re.finditer(rf"^\s*(?:[-*]\s*)?({OBJECT_PATTERN})-({FIELD_PATTERN})\b", text, re.MULTILINE):
        if is_strong_literal_object(match.group(1)):
            names.append(match.group(1).upper())
    return names


def extract_sql_tables(text):
    names = []
    patterns = [
        rf"\bSELECT\b[\s\S]{{0,500}}?\bFROM\s+({OBJECT_PATTERN})\b",
        rf"\bJOIN\s+({OBJECT_PATTERN})\b",
        rf"\bUPDATE\s+({OBJECT_PATTERN})\b",
        rf"\bMODIFY\s+({OBJECT_PATTERN})\b",
        rf"\bDELETE\s+FROM\s+({OBJECT_PATTERN})\b",
        rf"\bINSERT\s+(?:INTO\s+)?(?:TABLE\s+)?({OBJECT_PATTERN})\b",
    ]
    for pattern in patterns:
        names.extend(extract_first_group(text, pattern))
    return names


def extract_tables_statements(text):
    names = []
    for match in re.finditer(rf"^\s*TABLES\s*:?\s*({OBJECT_PATTERN})\b", text, re.IGNORECASE | re.MULTILINE):
        names.append(match.group(1).upper())
    return names


def extract_type_like_references(text):
    return extract_first_group(
        text,
        rf"\b(?:TYPE|LIKE)\s+(?:STANDARD\s+TABLE\s+OF\s+|SORTED\s+TABLE\s+OF\s+|HASHED\s+TABLE\s+OF\s+)?({OBJECT_PATTERN})-({FIELD_PATTERN})\b",
    )


def extract_named_metadata_references(text):
    names = []
    for pattern in [
        rf"\bSAP\s+(?:table|structure|view)\s*:?\s*({OBJECT_PATTERN})\b",
        rf"\b(?:table|structure|view)\s*:\s*({OBJECT_PATTERN})\b",
        rf"\bfield\s+({OBJECT_PATTERN})-({FIELD_PATTERN})\b",
    ]:
        names.extend(extract_first_group(text, pattern))
    for match in re.finditer(rf"\b(?:table|structure|view)\s+({OBJECT_PATTERN})\b", text, re.IGNORECASE):
        if is_strong_literal_object(match.group(1)):
            candidate = match.group(1).upper()
            if is_valid_ddic_object_name(candidate):
                names.append(candidate)
    return names


def is_probable_ddic_name(name):
    return is_valid_ddic_object_name(name)


def is_valid_ddic_object_name(name):
    upper = str(name or "").upper()
    if not re.fullmatch(r"(?:/[A-Z0-9_]+/[A-Z0-9_]+|[A-Z][A-Z0-9_]{1,29})", upper):
        return False
    if upper in ABAP_KEYWORDS:
        return False
    return not upper.startswith(LOCAL_PREFIXES)


def is_builtin_abap_type(name):
    return str(name or "").upper() in {
        "ANY",
        "C",
        "CHAR",
        "D",
        "DECFLOAT16",
        "DECFLOAT34",
        "F",
        "I",
        "INT1",
        "INT2",
        "INT4",
        "INT8",
        "N",
        "NUMC",
        "P",
        "STRING",
        "T",
        "X",
        "XSTRING",
    }


def is_local_identifier(name):
    return str(name or "").upper().startswith(LOCAL_PREFIXES)


def is_probable_field_name(name):
    return bool(re.match(r"^[A-Z_][A-Z0-9_]{0,29}$", str(name or "").upper()))


def is_strong_literal_object(value):
    text = str(value or "")
    upper = text.upper()
    return (
        text == upper
        or upper.startswith(("Z", "Y", "/"))
        or bool(re.search(r"[0-9_]", text))
    )


def extract_first_group(text, pattern):
    names = []
    for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
        candidate = match.group(1).upper()
        if is_valid_ddic_object_name(candidate):
            names.append(candidate)
    return names
