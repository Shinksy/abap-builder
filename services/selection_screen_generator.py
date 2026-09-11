import re

from services.abap_source import (
    abap_statement_units,
    first_statement_code_line,
    insert_declaration_statements,
    is_selection_screen_line,
    normalize_abap_blank_lines,
)


def selection_screen_declarations(contract):
    lines = []
    for item in (contract or {}).get("selection_screen") or []:
        kind = item.get("kind")
        name = item.get("name")
        if kind == "PARAMETERS":
            if item.get("as_checkbox"):
                line = f"PARAMETERS {name} AS CHECKBOX"
            elif item.get("radiobutton_group"):
                line = f"PARAMETERS {name}"
            else:
                line = f"PARAMETERS {name} {normalize_declaration(item.get('type_or_like')) or 'TYPE string'}"
            if item.get("radiobutton_group"):
                line += f" RADIOBUTTON GROUP {item['radiobutton_group']}"
            if item.get("default") not in (None, ""):
                line += f" DEFAULT {selection_screen_default_literal(item['default'])}"
            lines.append(line + ".")
        elif kind == "SELECT-OPTIONS" and item.get("for_field"):
            lines.append(f"SELECT-OPTIONS {name} FOR {item['for_field'].lower()}.")
    return "\n".join(lines)


def selection_screen_default_literal(value):
    text = str(value or "").strip()
    if not text:
        return text
    if re.match(r"^'.*'$", text):
        return text
    if text.upper() == "BFG WK_1":
        return "'BFG WK_1'"
    if text.upper() == "X":
        return "'X'"
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return text
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
        return text
    text = text.replace("'", "''")
    return f"'{text}'"


def normalize_declaration(value):
    text = re.sub(r"\s+", " ", str(value or "").strip().rstrip("."))
    if not text:
        return ""
    if re.match(r"^(TYPE|LIKE)\b", text, re.IGNORECASE):
        return text
    return "TYPE " + text


def apply_deterministic_selection_screen_declarations(source, generation_contract=None):
    if generation_contract is None:
        return source
    cleaned = remove_selection_screen_declaration_units(source)
    deterministic = selection_screen_declarations(generation_contract)
    if not deterministic:
        return cleaned
    return insert_declaration_statements(cleaned, deterministic.splitlines())


def remove_selection_screen_declaration_units(source):
    kept = []
    for unit in abap_statement_units(source):
        first_code = first_statement_code_line(unit)
        if is_selection_screen_line(first_code):
            continue
        kept.extend(unit)
    return normalize_abap_blank_lines("\n".join(kept))
