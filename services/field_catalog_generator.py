import re

from services.abap_source import (
    abap_statement_units,
    first_statement_code_line,
    normalize_abap_blank_lines,
    split_code_and_comment,
)
from services.ddic_metadata_context import normalized_fields, normalized_tables


ALV_FIELDCAT_TABLE_NAME = "t_fieldcat"
ALV_FIELDCAT_WORK_AREA_NAME = "w_fieldcat"


def apply_deterministic_alv_field_catalogue(
    source,
    generation_contract=None,
    declaration_requirements=None,
    ddic_metadata=None,
    source_text=None,
):
    entries = deterministic_alv_field_catalogue_entries(
        generation_contract=generation_contract,
        declaration_requirements=declaration_requirements,
        ddic_metadata=ddic_metadata,
        source_text=source_text,
    )
    if not entries:
        return source
    return replace_alv_field_catalogue_population(source, alv_field_catalogue_population_lines(entries))


def deterministic_alv_field_catalogue_entries(
    generation_contract=None,
    declaration_requirements=None,
    ddic_metadata=None,
    source_text=None,
):
    fields = output_contract_fields(generation_contract, declaration_requirements)
    output_names = {str(field.get("name") or "").upper() for field in fields}
    entries = []
    seen = set()
    for field in fields:
        name = str(field.get("name") or "").strip().upper()
        if not name or name in seen or name not in output_names:
            continue
        seen.add(name)
        heading = (
            output_field_contract_heading(field)
            or output_field_heading_from_spec(source_text, name)
            or output_field_ddic_description(field, ddic_metadata)
            or name
        )
        entry = {"name": name, "heading": heading}
        include_when = str(field.get("include_when") or "").strip()
        if include_when:
            entry["include_when"] = include_when
        entries.append(entry)
    return entries


def output_contract_fields(generation_contract=None, declaration_requirements=None):
    for output in (generation_contract or {}).get("output_structures") or []:
        fields = output.get("fields") if isinstance(output, dict) else None
        if fields:
            return [field for field in fields if isinstance(field, dict)]
    requirements = parse_declaration_requirements_text(declaration_requirements)
    fields = requirements.get("output_structure_fields") if isinstance(requirements, dict) else []
    return [field for field in fields or [] if isinstance(field, dict)]


def parse_declaration_requirements_text(declaration_requirements):
    import json

    text = str(declaration_requirements or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def output_field_contract_heading(field):
    return first_non_empty(
        field.get("heading"),
        field.get("description"),
        field.get("label"),
        field.get("column_heading"),
        field.get("seltext_l"),
    )


def first_non_empty(*values):
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def output_field_heading_from_spec(source_text, field_name):
    name = str(field_name or "").strip().upper()
    if not name:
        return ""
    pattern = re.compile(rf"\b{re.escape(name)}\b\s*(?:[-:=]|\t)\s*(.+)$", re.IGNORECASE)
    for line in str(source_text or "").splitlines():
        text = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        match = pattern.search(text)
        if match:
            heading = clean_output_heading_text(match.group(1))
            if heading:
                return heading
    return ""


def clean_output_heading_text(value):
    text = str(value or "").strip().strip("`*_")
    text = re.sub(r"\s+", " ", text)
    return text[:80].strip()


def output_field_ddic_description(field, ddic_metadata=None):
    reference = output_field_ddic_reference(field)
    if not reference:
        return ""
    tables = normalized_tables(ddic_metadata)
    object_name, field_name = reference
    field_metadata = normalized_fields(tables.get(object_name, {})).get(field_name)
    if isinstance(field_metadata, dict):
        return str(field_metadata.get("description") or "").strip()
    return ""


def output_field_ddic_reference(field):
    text = str((field or {}).get("type_or_like") or "")
    match = re.search(r"\b(?:TYPE|LIKE)\s+([A-Z0-9_/]+)-([A-Z0-9_]+)\b", text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).upper(), match.group(2).upper()


def alv_field_catalogue_population_lines(entries):
    lines = [
        f"REFRESH {ALV_FIELDCAT_TABLE_NAME}.",
        "",
    ]
    for entry in entries:
        block = single_alv_field_catalogue_entry_lines(entry)
        condition = str(entry.get("include_when") or "").strip()
        if condition:
            lines.append(f"IF {condition}.")
            lines.extend("  " + line if line else line for line in block)
            lines.append("ENDIF.")
        else:
            lines.extend(block)
        lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    return lines


def single_alv_field_catalogue_entry_lines(entry):
    heading = str(entry.get("heading") or entry.get("name") or "").strip()
    return [
        f"CLEAR {ALV_FIELDCAT_WORK_AREA_NAME}.",
        f"{ALV_FIELDCAT_WORK_AREA_NAME}-fieldname = {abap_string_literal(entry.get('name'))}.",
        f"{ALV_FIELDCAT_WORK_AREA_NAME}-seltext_l = {abap_string_literal(heading[:40])}.",
        f"{ALV_FIELDCAT_WORK_AREA_NAME}-seltext_m = {abap_string_literal(heading[:20])}.",
        f"{ALV_FIELDCAT_WORK_AREA_NAME}-seltext_s = {abap_string_literal(heading[:10])}.",
        f"APPEND {ALV_FIELDCAT_WORK_AREA_NAME} TO {ALV_FIELDCAT_TABLE_NAME}.",
    ]


def abap_string_literal(value):
    return "'" + str(value or "").replace("'", "''") + "'"


def replace_alv_field_catalogue_population(source, population_lines):
    lines = str(source or "").splitlines()
    output = []
    changed = False
    for unit in abap_statement_units(source):
        first_code = first_statement_code_line(unit)
        if re.match(r"^FORM\s+display_alv\b", first_code, re.IGNORECASE):
            output.extend(replace_field_catalogue_in_form(unit, population_lines))
            changed = True
        else:
            output.extend(unit)
    return normalize_abap_blank_lines("\n".join(output if changed else lines))


def replace_field_catalogue_in_form(form_lines, population_lines):
    call_index = next(
        (
            index
            for index, line in enumerate(form_lines)
            if re.search(r"CALL\s+FUNCTION\s+'REUSE_ALV_GRID_DISPLAY'", split_code_and_comment(str(line))[0], re.IGNORECASE)
        ),
        None,
    )
    if call_index is None:
        return form_lines
    before = remove_empty_if_blocks(
        [line for line in form_lines[:call_index] if not alv_field_catalogue_population_line(line)]
    )
    indent = re.match(r"^\s*", str(form_lines[call_index])).group(0)
    inserted = [indent + line if line else line for line in population_lines]
    return before + [""] + inserted + [""] + list(form_lines[call_index:])


def alv_field_catalogue_population_line(line):
    code = split_code_and_comment(str(line))[0]
    return bool(
        re.search(rf"\b(?:REFRESH|CLEAR|FREE)\s+{ALV_FIELDCAT_TABLE_NAME}\b", code, re.IGNORECASE)
        or re.search(rf"\bCLEAR\s+{ALV_FIELDCAT_WORK_AREA_NAME}\b", code, re.IGNORECASE)
        or re.search(rf"\b{ALV_FIELDCAT_WORK_AREA_NAME}-[A-Z0-9_]+\s*=", code, re.IGNORECASE)
        or re.search(
            rf"\bAPPEND\s+{ALV_FIELDCAT_WORK_AREA_NAME}\s+TO\s+{ALV_FIELDCAT_TABLE_NAME}\b",
            code,
            re.IGNORECASE,
        )
    )


def remove_empty_if_blocks(lines):
    current = list(lines)
    changed = True
    while changed:
        changed = False
        stack = []
        remove_ranges = []
        for index, line in enumerate(current):
            code = split_code_and_comment(str(line))[0].strip()
            if re.match(r"^IF\b", code, re.IGNORECASE):
                stack.append(index)
            elif re.match(r"^ENDIF\b", code, re.IGNORECASE) and stack:
                start = stack.pop()
                body = current[start + 1 : index]
                if not any(split_code_and_comment(str(item))[0].strip() for item in body):
                    remove_ranges.append((start, index))
        if remove_ranges:
            current = [
                line
                for index, line in enumerate(current)
                if not any(start <= index <= end for start, end in remove_ranges)
            ]
            changed = True
    return current

