import re

from services.abap_source import (
    call_block,
    collect_declared_field_symbols,
    collect_declared_names,
    collect_declared_types,
    collect_logical_declarations,
    chained_declaration_start,
    collect_local_type_declarations,
    collect_table_row_types,
    declaration_section_end,
    enclosing_callable_name,
    referenced_local_type,
    referenced_type_name,
    scope_bounds,
    split_code_and_comment,
    split_string_segments,
    statement_ends,
)
from services.validator import (
    normalize_callable_mappings as normalize_mapping_metadata,
    normalize_callable_signatures as normalize_signature_metadata,
    parse_callable_invocations,
    validate_abap,
)


PREFIX_MAP = {
    "it_": "t_",
    "lt_": "t_",
    "gt_": "t_",
    "ls_": "st_",
    "gs_": "st_",
    "wa_": "st_",
}


def auto_fix_abap(source, callable_signatures=None, callable_mappings=None, progress_callback=None):
    if progress_callback:
        progress_callback("Running deterministic validation", "Checking generated ABAP...")
    original_issues = validate_abap(source, callable_signatures=callable_signatures)
    if progress_callback:
        progress_callback("Applying safe deterministic fixes", "Applying safe deterministic fixes...")
    fixed_source = source
    fixes = []
    changed_rules = []
    fixed_source, changed_rules = apply_fixer_rule(
        fixed_source,
        fixes,
        changed_rules,
        "INLINE_DECLARATION_FIXES",
        fix_safe_inline_declarations,
        callable_signatures=callable_signatures,
    )
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "SELECT_CLAUSE_ORDER", fix_select_clause_order)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "INVALID_ENDSELECT_AFTER_INTO_TABLE", fix_endselect_after_select_into_table)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "TABLE_DECLARATION_NORMALIZATION", fix_table_declarations)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "INVALID_SELECT_OPTIONS_FOR_FIELD", fix_select_options_for_field)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "STRING_CONCATENATION", fix_simple_concatenation)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "FORBIDDEN_NAMING_PREFIX", rename_forbidden_prefixes)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "STRING_TEMPLATE", fix_message_templates)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "INVALID_LEAVE_LIST_PAGE", fix_leave_list_page)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "ABAP_LIST_PROCESSING_EXIT_MISMATCH", fix_list_processing_leave_report)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "INDENTED_ASTERISK_COMMENT", fix_indented_asterisk_comments)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "ALV_LOCAL_STRUCTURE_NAME", fix_invalid_local_alv_structure_name)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "TYPE_USED_BEFORE_DECLARATION", fix_local_type_declaration_order)
    fixed_source, changed_rules = apply_fixer_rule(fixed_source, fixes, changed_rules, "DATA_DECLARATION_PREFIX_ORDER", fix_data_declaration_prefix_order)
    fixed_source, changed_rules = apply_fixer_rule(
        fixed_source,
        fixes,
        changed_rules,
        "CALLABLE_SIGNATURE_CORRECTION",
        fix_callable_invocations,
        callable_signatures,
        callable_mappings,
    )
    fixed_source, changed_rules = apply_fixer_rule(
        fixed_source,
        fixes,
        changed_rules,
        "BAPI_MESSAGE_GETDETAIL_TABLES_SECTION_REMOVED",
        fix_bapi_message_getdetail_tables_section,
    )
    if progress_callback:
        progress_callback("Re-running deterministic validation", "Checking fixed ABAP...")
    final_issues = validate_abap(fixed_source, callable_signatures=callable_signatures)
    return {
        "original_source": source,
        "fixed_source": fixed_source,
        "original_issue_count": len(original_issues),
        "final_issue_count": len(final_issues),
        "final_issues": final_issues,
        "fixes": fixes,
        "diagnostics": {
            "source_before_fixer": source,
            "source_after_fixer": fixed_source,
            "changed_rules": changed_rules,
        },
    }


def apply_fixer_rule(source, fixes, changed_rules, fallback_rule_id, fixer, *args, **kwargs):
    fixed_source, new_fixes = fixer(source, *args, **kwargs)
    fixes.extend(new_fixes)
    if fixed_source != source:
        rule_ids = [fix.get("rule_id") for fix in new_fixes if fix.get("rule_id")]
        if not rule_ids:
            rule_ids = [fallback_rule_id]
        for rule_id in rule_ids:
            if rule_id not in changed_rules:
                changed_rules.append(rule_id)
    return fixed_source, changed_rules


def fix_safe_inline_declarations(source, callable_signatures=None):
    lines = source.splitlines()
    table_types = collect_table_row_types(lines)
    declared_types = collect_declared_types(lines)
    declarations = collect_declared_names(source)
    field_symbol_declarations = collect_declared_field_symbols(lines)
    pending = []
    fixes = []
    fixed = list(lines)
    normalized_signatures = normalize_callable_signatures(callable_signatures or {})

    for number, line in enumerate(lines, start=1):
        new_line = line
        loop_match = re.search(r"\bLOOP\s+AT\s+([A-Za-z_]\w*)\b.*\bINTO\s+@?DATA\s*\(\s*([A-Za-z_]\w*)\s*\)", new_line, re.IGNORECASE)
        field_symbol_loop_match = re.search(r"\bLOOP\s+AT\s+([A-Za-z_]\w*)\b.*\bASSIGNING\s+FIELD-SYMBOL\s*\(\s*(<[A-Za-z_]\w*>)\s*\)", new_line, re.IGNORECASE)
        read_match = re.search(r"\bREAD\s+TABLE\s+([A-Za-z_]\w*)\b.*\bINTO\s+@?DATA\s*\(\s*([A-Za-z_]\w*)\s*\)", new_line, re.IGNORECASE)
        field_symbol_read_match = re.search(r"\bREAD\s+TABLE\s+([A-Za-z_]\w*)\b.*\bASSIGNING\s+FIELD-SYMBOL\s*\(\s*(<[A-Za-z_]\w*>)\s*\)", new_line, re.IGNORECASE)
        invalid_decl_match = re.match(r"^(\s*)DATA\s*\(\s*([A-Za-z_]\w*)\s*\)\s+TYPE\s+(.+)$", new_line, re.IGNORECASE)
        assign_match = re.match(r"^(\s*)DATA\s*\(\s*([A-Za-z_]\w*)\s*\)\s*=\s*(.+?)\s*\.\s*$", new_line, re.IGNORECASE)
        catch_match = re.search(r"\bCATCH\s+([A-Za-z_]\w*)\s+INTO\s+DATA\s*\(\s*([A-Za-z_]\w*)\s*\)", new_line, re.IGNORECASE)
        call_param_match = re.search(r"\b([A-Za-z_]\w*)\s*=\s*@?DATA\s*\(\s*([A-Za-z_]\w*)\s*\)", new_line, re.IGNORECASE)

        if loop_match and loop_match.group(1).lower() in table_types:
            table, name = loop_match.groups()
            pending.append((number - 1, name, table_types[table.lower()], "INLINE_DATA_LOOP"))
            new_line = re.sub(r"@?DATA\s*\(\s*" + re.escape(name) + r"\s*\)", name, new_line, flags=re.IGNORECASE)
            fixes.append({"rule_id": "INLINE_DATA_LOOP", "description": f"Replaced inline LOOP declaration {name} on line {number}."})
        elif field_symbol_loop_match and field_symbol_loop_match.group(1).lower() in table_types:
            table, name = field_symbol_loop_match.groups()
            pending.append((number - 1, name, table_types[table.lower()], "INLINE_FIELD_SYMBOL"))
            new_line = re.sub(r"FIELD-SYMBOL\s*\(\s*" + re.escape(name) + r"\s*\)", name, new_line, flags=re.IGNORECASE)
            fixes.append({"rule_id": "INLINE_FIELD_SYMBOL", "description": f"Replaced inline FIELD-SYMBOL declaration {name} on line {number}."})
        elif read_match and read_match.group(1).lower() in table_types:
            table, name = read_match.groups()
            pending.append((number - 1, name, table_types[table.lower()], "INLINE_DATA_READ_TABLE"))
            new_line = re.sub(r"@?DATA\s*\(\s*" + re.escape(name) + r"\s*\)", name, new_line, flags=re.IGNORECASE)
            fixes.append({"rule_id": "INLINE_DATA_READ_TABLE", "description": f"Replaced inline READ TABLE declaration {name} on line {number}."})
        elif field_symbol_read_match and field_symbol_read_match.group(1).lower() in table_types:
            table, name = field_symbol_read_match.groups()
            pending.append((number - 1, name, table_types[table.lower()], "INLINE_FIELD_SYMBOL"))
            new_line = re.sub(r"FIELD-SYMBOL\s*\(\s*" + re.escape(name) + r"\s*\)", name, new_line, flags=re.IGNORECASE)
            fixes.append({"rule_id": "INLINE_FIELD_SYMBOL", "description": f"Replaced inline READ TABLE field-symbol declaration {name} on line {number}."})
        elif invalid_decl_match:
            indent, name, type_part = invalid_decl_match.groups()
            new_line = f"{indent}DATA {name} TYPE {type_part}"
            declarations.add(name.lower())
            fixes.append({"rule_id": "INVALID_DATA_DECLARATION", "description": f"Replaced parenthesised DATA declaration {name} on line {number}."})
        elif assign_match:
            indent, name, expr = assign_match.groups()
            inferred_type = infer_assignment_type(expr, declared_types)
            if inferred_type:
                if name.lower() not in declarations:
                    pending.append((number - 1, name, inferred_type, "INLINE_DATA"))
                new_line = f"{indent}{name} = {expr}."
                fixes.append({"rule_id": "INLINE_DATA", "description": f"Converted simple inline DATA assignment {name} on line {number}."})
        elif catch_match:
            exception_type, name = catch_match.groups()
            if name.lower() not in declarations:
                pending.append((number - 1, name, f"REF TO {exception_type}", "INLINE_DATA_CATCH"))
            new_line = re.sub(r"DATA\s*\(\s*" + re.escape(name) + r"\s*\)", name, new_line, flags=re.IGNORECASE)
            fixes.append({"rule_id": "INLINE_DATA_CATCH", "description": f"Replaced inline CATCH declaration {name} on line {number}."})
        elif call_param_match:
            parameter, name = call_param_match.groups()
            callable_name = enclosing_callable_name(lines, number - 1)
            inferred_type = callable_parameter_type(normalized_signatures, callable_name, parameter)
            if inferred_type:
                pending.append((number - 1, name, inferred_type, "INLINE_DATA_CALL_PARAMETER"))
                new_line = re.sub(r"@?DATA\s*\(\s*" + re.escape(name) + r"\s*\)", name, new_line, flags=re.IGNORECASE)
                fixes.append({"rule_id": "INLINE_DATA_CALL_PARAMETER", "description": f"Replaced inline call parameter declaration {name} on line {number}."})

        fixed[number - 1] = new_line

    if pending:
        insertions = {}
        for line_index, name, inferred_type, rule_id in pending:
            lower_name = declaration_key(name)
            if rule_id == "INLINE_FIELD_SYMBOL":
                if lower_name in field_symbol_declarations:
                    continue
            elif lower_name in declarations:
                continue
            insert_at = scoped_declaration_insert_index(fixed, line_index, inferred_type)
            if insert_at is None or insert_at > line_index:
                continue
            if rule_id == "INLINE_FIELD_SYMBOL":
                insertions.setdefault(insert_at, []).append(f"FIELD-SYMBOLS {name} TYPE {inferred_type}.")
                field_symbol_declarations.add(lower_name)
            else:
                insertions.setdefault(insert_at, []).append(f"DATA {name} TYPE {inferred_type}.")
                declarations.add(lower_name)
        fixed = apply_insertions(fixed, insertions)

    return "\n".join(fixed), fixes


def declaration_key(name):
    return name.strip("<>").lower()


def infer_assignment_type(expression, declared_types=None):
    expr = expression.strip()
    if re.fullmatch(r"abap_(?:true|false)", expr, re.IGNORECASE):
        return "abap_bool"
    if re.fullmatch(r"'[^']*'", expr):
        return "string"
    if declared_types:
        variable = re.fullmatch(r"([A-Za-z_]\w*)", expr)
        if variable:
            metadata = declared_types.get(variable.group(1).lower())
            if metadata and metadata.get("type"):
                return metadata["type"]
    return None


def scoped_declaration_insert_index(lines, line_index, inferred_type):
    scope_start, _scope_end = scope_bounds(lines, line_index)
    insert_at = declaration_section_end(lines, scope_start)
    required_local_type = referenced_type_name(inferred_type)
    if required_local_type and required_local_type.startswith("ty_"):
        local_types = collect_local_type_declarations(lines)
        declared_at = local_types.get(required_local_type.lower())
        if declared_at:
            insert_at = max(insert_at, declared_at)
    if insert_at > line_index:
        return None
    return insert_at


def apply_insertions(lines, insertions):
    fixed = []
    for index, line in enumerate(lines):
        fixed.extend(insertions.get(index, []))
        fixed.append(line)
    fixed.extend(insertions.get(len(lines), []))
    return fixed


def normalize_callable_signatures(callable_signatures):
    normalized = {}
    for callable_name, signature in callable_signatures.items():
        params = signature.get("parameters", signature) if isinstance(signature, dict) else {}
        normalized_params = {}
        for parameter_name, metadata in params.items():
            if isinstance(metadata, dict):
                type_text = metadata.get("type") or metadata.get("abap_type")
            else:
                type_text = metadata
            if type_text:
                normalized_params[parameter_name.lower()] = str(type_text)
        normalized[callable_name.lower()] = normalized_params
    return normalized


def callable_parameter_type(signatures, callable_name, parameter):
    if not callable_name:
        return None
    return signatures.get(callable_name.lower(), {}).get(parameter.lower())


def fix_callable_invocations(source, callable_signatures=None, callable_mappings=None):
    signatures = normalize_signature_metadata(callable_signatures)
    mappings = normalize_mapping_metadata(callable_mappings)
    if not signatures or not mappings:
        return source, []
    lines = source.splitlines()
    replacements = {}
    fixes = []
    for call in parse_callable_invocations(lines):
        callable_key = call["name"].lower()
        signature = signatures.get(callable_key)
        mapping = mappings.get(callable_key)
        if not signature or not mapping:
            continue
        replacement = corrected_callable_block(call, signature, mapping)
        if not replacement:
            continue
        replacements[(call["start_index"], call["end_index"])] = replacement
        fixes.append({"rule_id": "CALLABLE_SIGNATURE_CORRECTION", "description": f"Rebuilt CALL FUNCTION {call['name']} from supplied callable metadata."})
    if not replacements:
        return source, []
    fixed = []
    index = 0
    while index < len(lines):
        replacement_key = next((key for key in replacements if key[0] == index), None)
        if replacement_key:
            fixed.extend(replacements[replacement_key])
            index = replacement_key[1] + 1
            continue
        fixed.append(lines[index])
        index += 1
    return "\n".join(fixed), fixes


def corrected_callable_block(call, signature, mapping):
    parameters = signature["parameters"]
    for parameter in parameters.values():
        if not parameter["required"] or parameter["direction"] == "RETURNING":
            continue
        if parameter["direction"] not in mapping or parameter["name"].lower() not in mapping[parameter["direction"]]:
            return None
    used = set()
    sections = {}
    for direction, mapped_parameters in mapping.items():
        if direction == "RETURNING":
            continue
        for lower_name, mapped in mapped_parameters.items():
            parameter = parameters.get(lower_name)
            if not parameter or parameter["direction"] != direction or mapped.get("value") in (None, ""):
                return None
            sections.setdefault(direction, []).append((parameter["name"], str(mapped["value"])))
            used.add(lower_name)
    for actual in call["parameters"]:
        lower_name = actual["name"].lower()
        parameter = parameters.get(lower_name)
        if not parameter or parameter["direction"] == "RETURNING" or parameter["direction"] != actual["section"] or lower_name in used:
            continue
        value_match = re.match(r"^\s*[A-Za-z_]\w*\s*=\s*(.+?)\s*\.?\s*$", split_code_and_comment(actual["source_line"])[0])
        if value_match:
            sections.setdefault(actual["section"], []).append((parameter["name"], value_match.group(1).rstrip(",")))
            used.add(lower_name)
    if not sections:
        return None
    first_line = re.sub(r"\.\s*$", "", call["source_line"])
    base_indent = re.match(r"^(\s*)", first_line).group(1)
    parameter_indent = base_indent + "  "
    value_indent = base_indent + "    "
    fixed = [first_line]
    fixed.extend(call_comment_lines(call))
    for direction in ("EXPORTING", "IMPORTING", "CHANGING", "TABLES"):
        if direction not in sections:
            continue
        fixed.append(f"{parameter_indent}{direction}")
        for index, (name, value) in enumerate(sections[direction]):
            suffix = "." if direction == last_section(sections) and index == len(sections[direction]) - 1 else ""
            fixed.append(f"{value_indent}{name} = {value}{suffix}")
    if not fixed[-1].rstrip().endswith("."):
        fixed[-1] = fixed[-1].rstrip() + "."
    return fixed


def call_comment_lines(call):
    comments = []
    for _number, line in call["lines"][1:]:
        code, comment = split_code_and_comment(line)
        if line.lstrip().startswith("*") or (not code.strip() and comment):
            comments.append(line)
    return comments


def last_section(sections):
    for direction in reversed(("EXPORTING", "IMPORTING", "CHANGING", "TABLES")):
        if direction in sections:
            return direction
    return None


def fix_bapi_message_getdetail_tables_section(source):
    lines = source.splitlines()
    replacements = {}
    fixes = []
    for call in parse_callable_invocations(lines):
        if call["name"].upper() != "BAPI_MESSAGE_GETDETAIL":
            continue
        replacement = remove_tables_section_from_call(call)
        if replacement == [line for _number, line in call["lines"]]:
            continue
        replacements[(call["start_index"], call["end_index"])] = replacement
        fixes.append(
            {
                "rule_id": "BAPI_MESSAGE_GETDETAIL_TABLES_SECTION_REMOVED",
                "description": (
                    "Removed TABLES section from BAPI_MESSAGE_GETDETAIL "
                    f"call on line {call['line_number']}."
                ),
            }
        )
    if not replacements:
        return source, []

    fixed = []
    index = 0
    while index < len(lines):
        replacement_key = next((key for key in replacements if key[0] == index), None)
        if replacement_key:
            fixed.extend(replacements[replacement_key])
            index = replacement_key[1] + 1
            continue
        fixed.append(lines[index])
        index += 1
    return "\n".join(fixed), fixes


def remove_tables_section_from_call(call):
    call_lines = [line for _number, line in call["lines"]]
    fixed = []
    skipping_tables = False
    removed_final_section = False
    for index, line in enumerate(call_lines):
        stripped = split_code_and_comment(line)[0].strip()
        section_match = re.match(r"^(EXPORTING|IMPORTING|CHANGING|TABLES|EXCEPTIONS)\b", stripped, re.IGNORECASE)
        if section_match:
            section = section_match.group(1).upper()
            if section == "TABLES":
                skipping_tables = True
                removed_final_section = tables_section_runs_to_call_end(call_lines, index)
                continue
            skipping_tables = False
        if skipping_tables:
            continue
        fixed.append(line)

    if removed_final_section and fixed and not fixed[-1].rstrip().endswith("."):
        fixed[-1] = fixed[-1].rstrip() + "."
    return fixed


def tables_section_runs_to_call_end(call_lines, section_index):
    for line in call_lines[section_index + 1:]:
        stripped = split_code_and_comment(line)[0].strip()
        if re.match(r"^(EXPORTING|IMPORTING|CHANGING|EXCEPTIONS)\b", stripped, re.IGNORECASE):
            return False
    return True




def fix_table_declarations(source):
    fixed_lines = []
    fixes = []

    generic_table = re.compile(
        r"\bTYPE\s+TABLE\s+OF\b",
        re.IGNORECASE,
    )
    empty_key = re.compile(
        r"\s+WITH\s+EMPTY\s+KEY\b",
        re.IGNORECASE,
    )

    for line_number, line in enumerate(source.splitlines(), start=1):
        code, comment = split_code_and_comment(line)
        rewritten = code
        changes = []

        normalized = generic_table.sub("TYPE STANDARD TABLE OF", rewritten)
        if normalized != rewritten:
            rewritten = normalized
            changes.append("replaced TYPE TABLE OF with TYPE STANDARD TABLE OF")

        without_empty_key = empty_key.sub("", rewritten)
        if without_empty_key != rewritten:
            rewritten = without_empty_key
            changes.append("removed WITH EMPTY KEY")

        fixed_lines.append(rewritten + comment)

        if changes:
            fixes.append(
                {
                    "rule_id": "TABLE_DECLARATION_NORMALIZATION",
                    "description": (
                        f"Normalized internal table declaration on line {line_number}: "
                        + "; ".join(changes)
                        + "."
                    ),
                }
            )

    return "\n".join(fixed_lines), fixes

def fix_simple_concatenation(source):
    fixed = []
    fixes = []
    assignment = re.compile(r"^(\s*)([A-Za-z_]\w*(?:-[A-Za-z_]\w*)?)\s*=\s*(.+&&.+)\.\s*$")
    token = re.compile(r"^('[^']*'|[A-Za-z_]\w*(?:-[A-Za-z_]\w*)?)$")
    for number, line in enumerate(source.splitlines(), start=1):
        match = assignment.match(line)
        if not match:
            fixed.append(line)
            continue
        indent, target, expression = match.groups()
        parts = [part.strip() for part in expression.split("&&")]
        if parts and all(token.match(part) for part in parts):
            fixed.append(f"{indent}CONCATENATE {' '.join(parts)}")
            fixed.append(f"{indent}  INTO {target}.")
            fixes.append({"rule_id": "STRING_CONCATENATION", "description": f"Converted simple && concatenation on line {number}."})
        else:
            fixed.append(line)
    return "\n".join(fixed), fixes


def rename_forbidden_prefixes(source):
    declarations = collect_declared_renames(source)
    if not declarations:
        return source, []

    protected_lines = selection_screen_statement_line_indexes(source.splitlines())
    fixed_lines = []
    for index, line in enumerate(source.splitlines()):
        if index in protected_lines:
            fixed_lines.append(line)
            continue
        fixed_lines.append(rewrite_code_segments(line, declarations))

    fixes = [
        {
            "rule_id": "FORBIDDEN_NAMING_PREFIX",
            "description": f"Renamed {old_name} to {new_name}.",
        }
        for old_name, new_name in declarations.items()
    ]
    return "\n".join(fixed_lines), fixes


def collect_declared_renames(source):
    renames = {}
    declaration = re.compile(
        r"\b(?:DATA|FIELD-SYMBOLS|CONSTANTS|TYPES)\s*:?\s*<?([A-Za-z_]\w*)>?",
        re.IGNORECASE,
    )
    for line in source.splitlines():
        code = split_code_and_comment(line)[0]
        for segment, is_string in split_string_segments(code):
            if is_string:
                continue
            match = declaration.search(segment)
            if not match:
                continue
            old_name = match.group(1)
            lower_name = old_name.lower()
            if lower_name in renames:
                continue
            for old_prefix, new_prefix in PREFIX_MAP.items():
                if lower_name.startswith(old_prefix):
                    renames[lower_name] = new_prefix + old_name[len(old_prefix):]
                    break
    return renames


def fix_select_options_for_field(source):
    lines = source.splitlines()
    protected = select_options_statement_line_indexes(lines)
    if not protected:
        return source, []

    fixed_lines = []
    changed = False
    pattern = re.compile(
        r"(\bFOR\s+)FIELD\s+([A-Z0-9_/]+-[A-Z0-9_]+\b)",
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        if index not in protected:
            fixed_lines.append(line)
            continue
        code, comment = split_code_and_comment(line)
        fixed_code = pattern.sub(r"\1\2", code)
        if fixed_code != code:
            changed = True
        fixed_lines.append(fixed_code + comment)

    fixes = []
    if changed:
        fixes.append(
            {
                "rule_id": "INVALID_SELECT_OPTIONS_FOR_FIELD",
                "description": "Removed invalid FIELD keyword from SELECT-OPTIONS FOR DDIC field references.",
            }
        )
    return "\n".join(fixed_lines), fixes


def select_options_statement_line_indexes(lines):
    protected = set()
    in_select_options_statement = False
    for index, line in enumerate(lines):
        code = split_code_and_comment(line)[0].strip()
        if re.match(r"^SELECT-OPTIONS\b", code, re.IGNORECASE):
            in_select_options_statement = True
        if in_select_options_statement:
            protected.add(index)
            if statement_ends(code):
                in_select_options_statement = False
    return protected


def selection_screen_statement_line_indexes(lines):
    protected = set()
    in_selection_statement = False
    for index, line in enumerate(lines):
        code = split_code_and_comment(line)[0].strip()
        if re.match(r"^(?:PARAMETERS|SELECT-OPTIONS)\b", code, re.IGNORECASE):
            in_selection_statement = True
        if in_selection_statement:
            protected.add(index)
            if statement_ends(code):
                in_selection_statement = False
    return protected


def rewrite_code_segments(line, renames):
    code, comment = split_code_and_comment(line)
    rewritten = []
    for segment, is_string in split_string_segments(code):
        if is_string:
            rewritten.append(segment)
            continue
        for old_name, new_name in renames.items():
            segment = re.sub(rf"(?<!-)\b{re.escape(old_name)}\b", new_name, segment, flags=re.IGNORECASE)
        rewritten.append(segment)
    return "".join(rewritten) + comment


def fix_message_templates(source):
    fixed_lines = []
    fixes = []
    temp_name = "lv_message_text"
    simple_message = re.compile(
        r"^\s*MESSAGE\s+\|([^|{}]*)\{\s*([A-Za-z_]\w*)\s*\}([^|{}]*)\|\s+TYPE\s+('?[A-Za-z]'?)\s*\.\s*$",
        re.IGNORECASE,
    )
    any_message_template = re.compile(r"^\s*MESSAGE\s+\|.*\|\s+TYPE\s+", re.IGNORECASE)
    for line_number, line in enumerate(source.splitlines(), start=1):
        match = simple_message.match(line)
        if match:
            before, variable, after, message_type = match.groups()
            fixed_lines.extend(
                [
                    f"DATA {temp_name} TYPE string.",
                    f"CONCATENATE '{before.strip()}' {variable} '{after.strip()}' INTO {temp_name} SEPARATED BY space.",
                    f"MESSAGE {temp_name} TYPE {message_type}.",
                ]
            )
            fixes.append(
                {
                    "rule_id": "STRING_TEMPLATE",
                    "description": f"Converted simple MESSAGE string template on line {line_number}.",
                }
            )
        else:
            fixed_lines.append(line)
            if any_message_template.match(line):
                fixes.append(
                    {
                        "rule_id": "STRING_TEMPLATE",
                        "description": f"Left complex MESSAGE string template unchanged on line {line_number}.",
                        "applied": False,
                    }
                )
    return "\n".join(fixed_lines), fixes


def fix_leave_list_page(source):
    fixed_lines = []
    fixes = []
    statement = re.compile(r"^(\s*)LEAVE\s+LIST-PAGE\s*\.(\s*)$", re.IGNORECASE)
    for line_number, line in enumerate(source.splitlines(), start=1):
        code, comment = split_code_and_comment(line)
        match = statement.match(code)
        if not match:
            fixed_lines.append(line)
            continue
        fixed_lines.append(f"{match.group(1)}LEAVE LIST-PROCESSING.{match.group(2)}" + comment)
        fixes.append(
            {
                "rule_id": "INVALID_LEAVE_LIST_PAGE",
                "description": f"Replaced LEAVE LIST-PAGE with LEAVE LIST-PROCESSING on line {line_number}.",
            }
        )
    return "\n".join(fixed_lines), fixes


def fix_list_processing_leave_report(source):
    fixed_lines = []
    fixes = []
    issues_by_line = {
        item["line_number"]
        for item in validate_abap(source)
        if item["rule_id"] == "ABAP_LIST_PROCESSING_EXIT_MISMATCH"
    }
    statement = re.compile(r"^(\s*)LEAVE\s+REPORT\s*\.(\s*)$", re.IGNORECASE)
    for line_number, line in enumerate(source.splitlines(), start=1):
        if line_number not in issues_by_line:
            fixed_lines.append(line)
            continue
        code, comment = split_code_and_comment(line)
        match = statement.match(code)
        if not match:
            fixed_lines.append(line)
            continue
        fixed_lines.append(f"{match.group(1)}LEAVE LIST-PROCESSING.{match.group(2)}" + comment)
        fixes.append(
            {
                "rule_id": "ABAP_LIST_PROCESSING_EXIT_MISMATCH",
                "description": f"Replaced LEAVE REPORT with LEAVE LIST-PROCESSING on line {line_number}.",
            }
        )
    return "\n".join(fixed_lines), fixes


def fix_indented_asterisk_comments(source):
    fixed_lines = []
    fixes = []
    comment = re.compile(r"^\s+(\*.*)$")
    for line_number, line in enumerate(source.splitlines(), start=1):
        match = comment.match(line)
        if not match:
            fixed_lines.append(line)
            continue
        fixed_lines.append(match.group(1))
        fixes.append(
            {
                "rule_id": "INDENTED_ASTERISK_COMMENT",
                "description": f"Moved full-line * comment marker to column 1 on line {line_number}.",
            }
        )
    return "\n".join(fixed_lines), fixes


def fix_checkbox_parameters(source):
    lines = source.splitlines()
    fixed_lines = list(lines)
    fixes = []
    for declaration in collect_logical_declarations(lines, "PARAMETERS"):
        if declaration["end"] == declaration["start"]:
            continue
        first_code = split_code_and_comment(declaration["lines"][0])[0].strip()
        if chained_declaration_start(first_code) != "PARAMETERS":
            continue
        for offset, line in enumerate(declaration["lines"]):
            line_number = declaration["start"] + offset + 1
            rewritten = rewrite_checkbox_parameter_item(line, first_parameter=(offset == 0), preserve_chain_prefix=True)
            if rewritten == line:
                continue
            fixed_lines[line_number - 1] = rewritten
            fixes.append(
                {
                    "rule_id": "INVALID_CHECKBOX_SYNTAX",
                    "description": f"Converted checkbox PARAMETERS declaration on line {line_number}.",
                }
            )

    parameter = re.compile(
        r"^(\s*)PARAMETERS\s*:?\s+([A-Za-z_]\w*)\b(.+?)\.\s*$",
        re.IGNORECASE,
    )
    for line_number, line in enumerate(fixed_lines, start=1):
        code, comment = split_code_and_comment(line)
        match = parameter.match(code)
        if not match:
            continue
        indent, name, body = match.groups()
        upper_body = body.upper()
        if "AS CHECKBOX" not in upper_body or not re.search(r"\b(TYPE|LENGTH)\b", upper_body):
            continue
        if "," in body:
            continue
        default_x = re.search(r"\bDEFAULT\s+'X'", body, re.IGNORECASE)
        default_clause = " DEFAULT 'X'" if default_x else ""
        fixed_lines[line_number - 1] = f"{indent}PARAMETERS {name} AS CHECKBOX{default_clause}." + comment
        fixes.append(
            {
                "rule_id": "INVALID_CHECKBOX_SYNTAX",
                "description": f"Converted checkbox PARAMETERS declaration {name} on line {line_number}.",
            }
        )
    return "\n".join(fixed_lines), fixes


def rewrite_checkbox_parameter_item(line, first_parameter, preserve_chain_prefix):
    code, comment = split_code_and_comment(line)
    delimiter_match = re.search(r"(\s*)([,\.])\s*$", code)
    if not delimiter_match:
        return line
    spacing, delimiter = delimiter_match.groups()
    content = code[: delimiter_match.start()]

    if first_parameter:
        match = re.match(r"^(\s*PARAMETERS\s*:?\s*)([A-Za-z_]\w*\b.*)$", content, re.IGNORECASE)
        if not match:
            return line
        prefix, item = match.groups()
        if not preserve_chain_prefix:
            prefix = re.sub(r":", "", prefix)
    else:
        match = re.match(r"^(\s*)([A-Za-z_]\w*\b.*)$", content)
        if not match:
            return line
        prefix, item = match.groups()

    name_match = re.match(r"^([A-Za-z_]\w*)\b(.*)$", item.strip(), re.IGNORECASE)
    if not name_match:
        return line
    name, body = name_match.groups()
    upper_item = item.upper()
    if "AS CHECKBOX" not in upper_item or not re.search(r"\b(TYPE|LENGTH)\b", upper_item):
        return line

    default_x = re.search(r"\bDEFAULT\s+'X'", body, re.IGNORECASE)
    default_clause = " DEFAULT 'X'" if default_x else ""
    return f"{prefix}{name} AS CHECKBOX{default_clause}{spacing}{delimiter}{comment}"


def fix_invalid_local_alv_structure_name(source):
    lines = source.splitlines()
    remove_indexes = set()
    fixes = []
    for start_index, line in enumerate(lines):
        if not re.search(r"CALL\s+FUNCTION\s+'REUSE_ALV_GRID_DISPLAY'", line, re.IGNORECASE):
            continue
        block = list(call_block(lines, start_index))
        has_explicit_field_catalogue = any(
            re.search(r"\b(?:IT_FIELDCAT|T_FIELDCAT)\s*=", block_line, re.IGNORECASE)
            for _number, block_line in block
        )
        if not has_explicit_field_catalogue:
            continue
        for number, block_line in block:
            if re.search(r"\bI_STRUCTURE_NAME\s*=\s*'TY_[A-Za-z0-9_]+'\s*\.?", block_line, re.IGNORECASE):
                remove_indexes.add(number - 1)
                fixes.append(
                    {
                        "rule_id": "ALV_LOCAL_STRUCTURE_NAME",
                        "description": f"Removed invalid local ALV i_structure_name on line {number}.",
                    }
                )
    if not remove_indexes:
        return source, []
    fixed_lines = [line for index, line in enumerate(lines) if index not in remove_indexes]
    return "\n".join(fixed_lines), fixes


def fix_local_type_declaration_order(source):
    lines = source.splitlines()
    local_types = collect_local_type_declarations(lines)
    if not local_types:
        return source, []

    movable = []
    in_form = False
    for statement in source_statement_ranges(lines):
        start_index = statement["start"]
        statement_lines = statement["lines"]
        first_code = first_code_line(statement_lines)
        if re.match(r"^FORM\b", first_code, re.IGNORECASE):
            in_form = True
        if re.match(r"^ENDFORM\b", first_code, re.IGNORECASE):
            in_form = False
            continue
        if in_form:
            continue
        if len(statement_lines) != 1:
            continue
        line = statement_lines[0]
        code = split_code_and_comment(line)[0].strip()
        reference = referenced_local_type(code)
        if not reference or reference.lower() not in local_types:
            continue
        if local_types[reference.lower()] <= start_index + 1:
            continue
        if not is_simple_top_level_data_statement(code):
            continue
        movable.append((start_index, reference.lower(), statement_lines))

    if not movable:
        return source, []

    first_target_line = min(local_types[type_name] for _index, type_name, _statement_lines in movable)
    if has_top_level_executable_before(lines, first_target_line):
        return source, []

    insert_after_index = max(local_types[type_name] for _index, type_name, _statement_lines in movable) - 1
    move_indexes = {index for index, _type_name, _statement_lines in movable}
    moved_lines = [
        line
        for _index, _type_name, statement_lines in movable
        for line in statement_lines
    ]
    fixed_lines = []
    inserted = False
    for index, line in enumerate(lines):
        if index in move_indexes:
            continue
        fixed_lines.append(line)
        if index == insert_after_index and not inserted:
            fixed_lines.extend(moved_lines)
            inserted = True

    fixes = [
        {
            "rule_id": "TYPE_USED_BEFORE_DECLARATION",
            "description": f"Moved DATA declaration after local TYPES declaration for {type_name}.",
        }
        for _index, type_name, _statement_lines in movable
    ]
    return "\n".join(fixed_lines), fixes


def fix_data_declaration_prefix_order(source):
    lines = source.splitlines()
    statements = source_statement_ranges(lines)
    if not statements:
        return source, []

    fixed_units = []
    data_block = []
    changed = False
    past_global_declarations = False

    def flush_data_block():
        nonlocal changed
        if not data_block:
            return
        ordered = sorted(
            data_block,
            key=lambda item: (data_declaration_prefix_rank(item["name"]), item["ordinal"]),
        )
        if [item["ordinal"] for item in ordered] != [item["ordinal"] for item in data_block]:
            changed = True
        fixed_units.extend(item["statement"]["lines"] for item in ordered)
        data_block.clear()

    for statement in statements:
        first_code = first_code_line(statement["lines"])
        if re.match(r"^(FORM|START-OF-SELECTION|END-OF-SELECTION|INITIALIZATION|AT\s+SELECTION-SCREEN)\b", first_code, re.IGNORECASE):
            past_global_declarations = True
        name = None if past_global_declarations else single_data_declaration_name(statement["lines"])
        if name:
            data_block.append({"statement": statement, "name": name, "ordinal": len(data_block)})
            continue
        flush_data_block()
        fixed_units.append(statement["lines"])

    flush_data_block()
    if not changed:
        return source, []
    return "\n".join(line for unit in fixed_units for line in unit), [
        {
            "rule_id": "DATA_DECLARATION_PREFIX_ORDER",
            "description": "Grouped top-level DATA declarations by t_, st_, and w_ variable prefixes.",
        }
    ]


def single_data_declaration_name(statement_lines):
    statement_text = " ".join(split_code_and_comment(line)[0].strip() for line in statement_lines).strip()
    if not re.match(r"^DATA\b", statement_text, re.IGNORECASE):
        return None
    if "," in statement_text:
        return None
    match = re.match(r"^DATA\s*:?\s+([A-Za-z_]\w*)\b", statement_text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower()


def data_declaration_prefix_rank(name):
    lower_name = str(name or "").lower()
    if lower_name.startswith("t_"):
        return 0
    if lower_name.startswith("st_"):
        return 1
    if lower_name.startswith("w_"):
        return 2
    return 3


def source_statement_ranges(lines):
    ranges = []
    current = []
    start = 0
    for index, line in enumerate(lines):
        if not current:
            start = index
        current.append(line)
        code = split_code_and_comment(line)[0].strip()
        if statement_ends(code):
            ranges.append({"start": start, "end": index, "lines": current})
            current = []
    if current:
        ranges.append({"start": start, "end": len(lines) - 1, "lines": current})
    return ranges


def first_code_line(lines):
    for line in lines or []:
        code = split_code_and_comment(line)[0].strip()
        if code:
            return code
    return ""


def fix_select_clause_order(source):
    lines = source.splitlines()
    fixed_units = []
    fixes = []

    for statement in source_statement_ranges(lines):
        statement_lines = statement["lines"]
        first_code = first_code_line(statement_lines)
        if not re.match(r"^SELECT\b", first_code, re.IGNORECASE):
            fixed_units.append(statement_lines)
            continue
        if any(split_code_and_comment(line)[1].strip() for line in statement_lines):
            fixed_units.append(statement_lines)
            continue
        statement_text = " ".join(
            split_code_and_comment(line)[0].strip()
            for line in statement_lines
            if split_code_and_comment(line)[0].strip()
        )
        fixed_text = normalized_select_clause_order(statement_text)
        if not fixed_text or normalized_abap_whitespace(fixed_text) == normalized_abap_whitespace(statement_text):
            fixed_units.append(statement_lines)
            continue
        indent_match = re.match(r"^(\s*)", statement_lines[0])
        indent = indent_match.group(1) if indent_match else ""
        fixed_units.append([indent + fixed_text])
        fixes.append(
            {
                "rule_id": "SELECT_CLAUSE_ORDER",
                "description": f"Normalised SELECT clause order for statement starting on line {statement['start'] + 1}.",
            }
        )

    if not fixes:
        return source, []
    return "\n".join(line for unit in fixed_units for line in unit), fixes


def normalized_select_clause_order(statement_text):
    text = str(statement_text or "").strip()
    if not re.match(r"^SELECT\b", text, re.IGNORECASE) or not text.endswith("."):
        return None
    body = text[:-1].strip()
    if not re.search(r"\bFROM\b", body, re.IGNORECASE):
        return None

    target_match = select_target_clause_match(body)
    if not target_match:
        return None

    if select_target_clause_is_before_trailing_clauses(body, target_match):
        return None

    target_clause = target_match.group(0).strip()
    without_target = (body[: target_match.start()] + body[target_match.end() :]).strip()
    without_target = re.sub(r"\s+", " ", without_target)
    insertion_index = select_target_insertion_index(without_target)
    if insertion_index is None:
        return None

    fixed_body = (
        without_target[:insertion_index].rstrip()
        + " "
        + target_clause
        + " "
        + without_target[insertion_index:].lstrip()
    ).strip()
    return re.sub(r"\s+", " ", fixed_body) + "."


def select_target_clause_is_before_trailing_clauses(statement_body, target_match):
    trailing_match = re.search(
        r"\b(FOR\s+ALL\s+ENTRIES\s+IN|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|UP\s+TO|PACKAGE\s+SIZE)\b",
        statement_body,
        re.IGNORECASE,
    )
    return not trailing_match or target_match.start() < trailing_match.start()


def select_target_clause_match(statement_body):
    target_pattern = re.compile(
        r"\b(?:"
        r"APPENDING\s+(?:CORRESPONDING\s+FIELDS\s+OF\s+)?TABLE\s+[A-Za-z_][A-Za-z0-9_]*(?:\[\])?"
        r"|INTO\s+(?:CORRESPONDING\s+FIELDS\s+OF\s+)?(?:TABLE\s+)?[A-Za-z_][A-Za-z0-9_]*(?:\[\])?"
        r")\b",
        re.IGNORECASE,
    )
    matches = list(target_pattern.finditer(statement_body))
    return matches[0] if len(matches) == 1 else None


def select_target_insertion_index(statement_without_target):
    from_match = re.search(r"\bFROM\b", statement_without_target, re.IGNORECASE)
    if not from_match:
        return None
    suffix = statement_without_target[from_match.end() :]
    clause_match = re.search(
        r"\b(FOR\s+ALL\s+ENTRIES\s+IN|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|UP\s+TO|PACKAGE\s+SIZE)\b",
        suffix,
        re.IGNORECASE,
    )
    if clause_match:
        return from_match.end() + clause_match.start()
    return len(statement_without_target)


def normalized_abap_whitespace(text):
    return re.sub(r"\s+", " ", str(text or "").strip())


def fix_endselect_after_select_into_table(source):
    fixed = []
    fixes = []
    current_statement = []
    pending_select_into_table = None

    for line_number, line in enumerate(source.splitlines(), start=1):
        code = split_code_and_comment(line)[0]
        stripped = code.strip()

        if pending_select_into_table and re.fullmatch(r"ENDSELECT\s*\.", stripped, re.IGNORECASE):
            fixes.append(
                {
                    "rule_id": "INVALID_ENDSELECT_AFTER_INTO_TABLE",
                    "description": (
                        "Removed ENDSELECT after SELECT ... INTO TABLE statement "
                        f"ending on line {pending_select_into_table}."
                    ),
                }
            )
            pending_select_into_table = None
            continue

        if stripped and pending_select_into_table and not is_comment_only_code(stripped):
            pending_select_into_table = None

        fixed.append(line)

        if not stripped or is_comment_only_code(stripped):
            continue
        current_statement.append(stripped)
        if not statement_ends(stripped):
            continue
        statement_text = " ".join(current_statement)
        pending_select_into_table = (
            line_number
            if is_select_into_table_statement(statement_text)
            else None
        )
        current_statement = []

    return "\n".join(fixed), fixes


def is_select_into_table_statement(statement_text):
    return bool(
        re.match(r"^\s*SELECT\b", statement_text, re.IGNORECASE)
        and re.search(r"\bINTO\s+TABLE\b", statement_text, re.IGNORECASE)
    )


def is_comment_only_code(stripped_code):
    return stripped_code.startswith(("*", '"'))


def is_simple_top_level_data_statement(code):
    if ":" in code or not code.endswith("."):
        return False
    return bool(
        re.match(
            r"^DATA\s+[A-Za-z_]\w*\s+TYPE\s+(?:(?:STANDARD|SORTED|HASHED)\s+TABLE\s+OF\s+)?ty_[A-Za-z0-9_]+\s*\.\s*$",
            code,
            re.IGNORECASE,
        )
    )


def has_top_level_executable_before(lines, line_number):
    in_form = False
    in_type_block = False
    in_chained_declaration = False
    for line in lines[: line_number - 1]:
        code = split_code_and_comment(line)[0].strip()
        if not code:
            continue
        if in_chained_declaration:
            if statement_ends(code):
                in_chained_declaration = False
            continue
        if re.match(r"^FORM\b", code, re.IGNORECASE):
            in_form = True
        if in_form:
            if re.match(r"^ENDFORM\b", code, re.IGNORECASE):
                in_form = False
            continue
        if re.match(r"^TYPES\s*:?\s+BEGIN\s+OF\b", code, re.IGNORECASE):
            in_type_block = True
            continue
        if in_type_block:
            if re.match(r"^END\s+OF\b", code, re.IGNORECASE):
                in_type_block = False
            continue
        if chained_declaration_start(code):
            if not statement_ends(code):
                in_chained_declaration = True
            continue
        if is_global_declaration_layout_line(code):
            continue
        return True
    return False


def is_global_declaration_layout_line(code):
    return bool(
        re.match(
            r"^(REPORT|TABLES|TYPES|END\s+OF|DATA|CONSTANTS|FIELD-SYMBOLS|SELECTION-SCREEN|PARAMETERS|SELECT-OPTIONS)\b",
            code,
            re.IGNORECASE,
        )
    )
