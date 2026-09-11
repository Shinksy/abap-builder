import re

from services.abap_source import (
    abap_statement_units,
    first_statement_code_line,
    insert_declaration_statements,
    normalize_abap_blank_lines,
)
from services.selection_screen_generator import selection_screen_declarations


def type_declarations(contract, ddic_metadata=None):
    lines = []
    emitted_names = set()
    for output in (contract or {}).get("output_structures") or []:
        fields = output.get("fields") or []
        if not fields:
            continue
        lines.append(f"TYPES: BEGIN OF {output['name']},")
        for field in fields:
            type_text = normalize_declaration(field.get("type_or_like"))
            if type_text:
                lines.append(f"         {field['name'].lower()} {type_text},")
        lines.append(f"       END OF {output['name']}.")
        emitted_names.add(normalize_type_name(output.get("name")))
    for read in (contract or {}).get("database_reads") or []:
        if normalize_type_name(read.get("row_type")) in emitted_names:
            continue
        lines.append(f"TYPES: BEGIN OF {read['row_type']},")
        for field in read.get("fields") or []:
            lines.append(f"         {field.lower()} TYPE {read['table']}-{field},")
        lines.append(f"       END OF {read['row_type']}.")
        emitted_names.add(normalize_type_name(read.get("row_type")))
    for type_name, fields in grouped_ddic_backed_type_fields(contract).items():
        if normalize_type_name(type_name) in emitted_names or not fields:
            continue
        lines.append(f"TYPES: BEGIN OF {type_name},")
        for field in fields:
            lines.append(f"         {field['field'].lower()} TYPE {field['object']}-{field['field']},")
        lines.append(f"       END OF {type_name}.")
        emitted_names.add(normalize_type_name(type_name))
    return "\n".join(lines)


def grouped_ddic_backed_type_fields(contract):
    grouped = {}
    seen = set()
    for item in (contract or {}).get("ddic_backed_types") or []:
        name = normalize_identifier(item.get("name"))
        object_name = str(item.get("object") or "").strip().upper()
        field_name = str(item.get("field") or "").strip().upper()
        if not name or not object_name or not field_name:
            continue
        key = (name.lower(), object_name, field_name)
        if key in seen:
            continue
        seen.add(key)
        grouped.setdefault(name, []).append({"object": object_name, "field": field_name})
    return grouped


def data_declarations(contract):
    lines = []
    for item in (contract or {}).get("internal_tables") or []:
        if item.get("name") and item.get("row_type"):
            lines.append(f"DATA {item['name']} TYPE STANDARD TABLE OF {item['row_type']}.")
    for item in (contract or {}).get("work_areas") or []:
        row_type = item.get("type") or item.get("row_type")
        if item.get("name") and row_type:
            lines.append(f"DATA {item['name']} TYPE {row_type}.")
    for item in (contract or {}).get("scalar_variables") or []:
        if item.get("name") and item.get("type_or_like"):
            lines.append(f"DATA {item['name']} {item['type_or_like']}.")
    return "\n".join(dedupe(lines))


def apply_deterministic_declarations(source, generation_contract=None, ddic_metadata=None):
    if generation_contract is None:
        return source
    cleaned = remove_contract_owned_declaration_units(source)
    deterministic = deterministic_declaration_lines(generation_contract, ddic_metadata)
    if not deterministic:
        return cleaned
    return insert_declaration_statements(cleaned, deterministic)


def deterministic_declaration_lines(generation_contract, ddic_metadata=None):
    parts = [
        type_declarations(generation_contract, ddic_metadata),
        data_declarations(generation_contract),
        selection_screen_declarations(generation_contract),
    ]
    lines = []
    for part in parts:
        if part:
            lines.extend(part.splitlines())
    return lines


def remove_contract_owned_declaration_units(source):
    kept = []
    for unit in abap_statement_units(source):
        first_code = first_statement_code_line(unit)
        if is_contract_owned_declaration_line(first_code):
            continue
        kept.extend(unit)
    return normalize_abap_blank_lines("\n".join(kept))


def is_contract_owned_declaration_line(stripped):
    return bool(re.match(r"^(TYPES|DATA|PARAMETERS|SELECT-OPTIONS|SELECTION-SCREEN)\b", stripped, re.IGNORECASE))


def normalize_declaration(value):
    text = re.sub(r"\s+", " ", str(value or "").strip().rstrip("."))
    if not text:
        return ""
    if re.match(r"^(TYPE|LIKE)\b", text, re.IGNORECASE):
        return text
    return "TYPE " + text


def normalize_identifier(value):
    text = str(value or "").strip()
    text = re.sub(r"[^0-9A-Za-z_]", "_", text).strip("_").lower()
    if text and text[0].isdigit():
        text = "v_" + text
    return text


def normalize_type_name(value):
    return str(value or "").strip().lower()


def dedupe(values):
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
