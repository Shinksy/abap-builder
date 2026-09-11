import re


CHAIN_DECLARATION_KEYWORDS = {
    "TABLES",
    "DATA",
    "TYPES",
    "CONSTANTS",
    "FIELD-SYMBOLS",
    "PARAMETERS",
    "SELECT-OPTIONS",
}


def split_code_and_comment(line):
    if line.startswith("*"):
        return "", line
    in_string = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == "'":
            in_string = not in_string
        if char == '"' and not in_string:
            return line[:index], line[index:]
        index += 1
    return line, ""


def split_string_segments(text):
    segments = []
    start = 0
    in_string = False
    for index, char in enumerate(text):
        if char == "'":
            if in_string:
                segments.append((text[start:index + 1], True))
                start = index + 1
                in_string = False
            else:
                if start < index:
                    segments.append((text[start:index], False))
                start = index
                in_string = True
    if start < len(text):
        segments.append((text[start:], in_string))
    return segments


def statement_ends(code):
    return code.rstrip().endswith(".")


def abap_statement_units(source):
    units = []
    current = []
    in_form = False
    for line in str(source or "").splitlines():
        stripped = line.strip()
        if not current and not stripped:
            units.append([line])
            continue
        if re.match(r"^FORM\b", stripped, re.IGNORECASE):
            in_form = True
        current.append(line)
        if in_form:
            if re.match(r"^ENDFORM\b", stripped, re.IGNORECASE):
                units.append(current)
                current = []
                in_form = False
            continue
        code = split_code_and_comment(line)[0].strip()
        if statement_ends(code):
            units.append(current)
            current = []
    if current:
        units.append(current)
    return units


def first_statement_code_line(statement):
    for line in statement or []:
        code = split_code_and_comment(line)[0].strip()
        if code:
            return code
    return ""


def normalize_abap_blank_lines(source, max_blank_lines=1):
    lines = str(source or "").splitlines()
    normalized = []
    blank_count = 0
    for line in lines:
        if not line.strip():
            blank_count += 1
            if blank_count <= max_blank_lines:
                normalized.append("")
            continue
        blank_count = 0
        normalized.append(line)
    while normalized and not normalized[-1].strip():
        normalized.pop()
    return "\n".join(normalized)


def insert_declaration_statements(source, statements):
    units = abap_statement_units(source)
    if not units:
        return "\n".join(statements)
    insert_at = 0
    for index, unit in enumerate(units):
        first_code = first_statement_code_line(unit)
        if re.match(r"^(REPORT|TABLES|TYPES|CONSTANTS|DATA|FIELD-SYMBOLS|RANGES)\b", first_code, re.IGNORECASE):
            insert_at = index + 1
            continue
        break
    assembled_units = []
    for index, unit in enumerate(units):
        if index == insert_at:
            assembled_units.append(list(statements))
        assembled_units.append(unit)
    if insert_at >= len(units):
        assembled_units.append(list(statements))
    return "\n".join(line for unit in assembled_units for line in unit)


def is_selection_screen_line(stripped):
    return bool(re.match(r"^(PARAMETERS|SELECT-OPTIONS|SELECTION-SCREEN)\b", stripped, re.IGNORECASE))


def join_lines(lines):
    cleaned = list(lines or [])
    while cleaned and not str(cleaned[0]).strip():
        cleaned.pop(0)
    while cleaned and not str(cleaned[-1]).strip():
        cleaned.pop()
    return "\n".join(str(line) for line in cleaned).strip()


def chained_declaration_start(code):
    match = re.match(r"^([A-Za-z-]+)\s*:", code.strip(), re.IGNORECASE)
    if match and match.group(1).upper() in CHAIN_DECLARATION_KEYWORDS:
        return match.group(1).upper()
    return None


def collect_logical_declarations(lines, keyword):
    declarations = []
    keyword_upper = keyword.upper()
    index = 0
    while index < len(lines):
        code = split_code_and_comment(lines[index])[0].strip()
        if not re.match(rf"^{re.escape(keyword)}\b", code, re.IGNORECASE):
            index += 1
            continue
        start = index
        end = index
        if chained_declaration_start(code) == keyword_upper and not statement_ends(code):
            end += 1
            while end < len(lines):
                next_code = split_code_and_comment(lines[end])[0].strip()
                if statement_ends(next_code):
                    break
                end += 1
        declarations.append({"start": start, "end": end, "lines": lines[start : end + 1]})
        index = end + 1
    return declarations


def collect_declared_types(lines):
    declarations = {}
    data_decl = re.compile(
        r"\b(?:DATA|CONSTANTS)\s+([A-Za-z_]\w*)\s+TYPE\s+(.+?)\s*\.\s*$",
        re.IGNORECASE,
    )
    field_symbol_decl = re.compile(
        r"\bFIELD-SYMBOLS\s+<([A-Za-z_]\w*)>\s+TYPE\s+(.+?)\s*\.\s*$",
        re.IGNORECASE,
    )
    for line in lines:
        code = split_code_and_comment(line)[0]
        match = data_decl.search(code) or field_symbol_decl.search(code)
        if not match:
            continue
        name, type_text = match.groups()
        declarations[name.lower()] = type_metadata(type_text)
    return declarations


def collect_declared_names(source):
    names = set()
    declaration = re.compile(r"\b(?:DATA|FIELD-SYMBOLS|CONSTANTS|TYPES|PARAMETERS|SELECT-OPTIONS)\s*:?\s*<?([A-Za-z_]\w*)>?", re.IGNORECASE)
    for line in source.splitlines():
        code = split_code_and_comment(line)[0]
        for segment, is_string in split_string_segments(code):
            if not is_string:
                names.update(match.group(1).lower() for match in declaration.finditer(segment))
    return names


def collect_declared_field_symbols(lines):
    names = set()
    declaration = re.compile(r"\bFIELD-SYMBOLS\s+<([A-Za-z_]\w*)>", re.IGNORECASE)
    for line in lines:
        code = split_code_and_comment(line)[0]
        names.update(match.group(1).lower() for match in declaration.finditer(code))
    return names


def collect_table_row_types(lines):
    declarations = collect_declared_types(lines)
    row_types = {
        name: metadata["row_type"]
        for name, metadata in declarations.items()
        if metadata.get("row_type")
    }
    row_types.update(collect_form_parameter_row_types(lines, row_types))
    return row_types


def type_metadata(type_text):
    cleaned = " ".join(type_text.strip().rstrip(".").split())
    table_match = re.match(r"(?:(?:STANDARD|SORTED|HASHED)\s+)?TABLE\s+OF\s+([A-Za-z_]\w*)\b", cleaned, re.IGNORECASE)
    if table_match:
        return {"type": cleaned, "row_type": table_match.group(1)}
    return {"type": cleaned, "row_type": None}


def collect_form_parameter_row_types(lines, row_types):
    forms = collect_form_interfaces(lines)
    calls = collect_perform_calls(lines)
    inferred = {}
    for form_name, form_interface in forms.items():
        relevant_calls = calls.get(form_name, [])
        if not relevant_calls:
            continue
        if not all(form_call_interface_matches(form_interface, call) for call in relevant_calls):
            continue
        for section, params in form_interface.items():
            for position, parameter in enumerate(params):
                if not parameter.get("table_like"):
                    continue
                candidate_types = set()
                matched_all_calls = True
                for call in relevant_calls:
                    actuals = call.get(section, [])
                    if position >= len(actuals):
                        matched_all_calls = False
                        break
                    row_type = row_types.get(actuals[position].lower())
                    if not row_type:
                        matched_all_calls = False
                        break
                    candidate_types.add(row_type.lower())
                if matched_all_calls and len(candidate_types) == 1:
                    inferred[parameter["name"].lower()] = row_types[relevant_calls[0][section][position].lower()]
    return inferred


def form_call_interface_matches(form_interface, call):
    for section, params in form_interface.items():
        if len(call.get(section, [])) != len(params):
            return False
    return True


def collect_form_interfaces(lines):
    forms = {}
    for line in lines:
        code = split_code_and_comment(line)[0].strip()
        match = re.match(r"FORM\s+([A-Za-z_]\w*)\s*(.*?)\.\s*$", code, re.IGNORECASE)
        if not match:
            continue
        form_name, rest = match.groups()
        forms[form_name.lower()] = parse_form_sections(rest)
    return forms


def parse_form_sections(text):
    sections = {"using": [], "changing": [], "tables": []}
    current = None
    tokens = text.replace(",", " ").split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lower = token.lower()
        if lower in sections:
            current = lower
            index += 1
            continue
        if current and re.match(r"^[A-Za-z_]\w*$", token):
            parameter = {"name": token, "table_like": False}
            lookahead = [item.lower() for item in tokens[index + 1:index + 6]]
            if current == "tables" or lookahead[:3] == ["type", "standard", "table"] or lookahead[:2] == ["type", "table"]:
                parameter["table_like"] = True
            sections[current].append(parameter)
            index = consume_form_type_clause(tokens, index + 1)
            continue
        index += 1
    return sections


def consume_form_type_clause(tokens, index):
    if index >= len(tokens) or tokens[index].lower() not in {"type", "like"}:
        return index
    index += 1
    while index < len(tokens):
        lower = tokens[index].lower()
        if lower in {"using", "changing", "tables"}:
            break
        index += 1
        if lower not in {"standard", "sorted", "hashed", "table", "of"}:
            break
    return index


def collect_perform_calls(lines):
    calls = {}
    for line in lines:
        code = split_code_and_comment(line)[0].strip()
        match = re.match(r"PERFORM\s+([A-Za-z_]\w*)\s*(.*?)\.\s*$", code, re.IGNORECASE)
        if not match:
            continue
        form_name, rest = match.groups()
        calls.setdefault(form_name.lower(), []).append(parse_perform_sections(rest))
    return calls


def parse_perform_sections(text):
    sections = {"using": [], "changing": [], "tables": []}
    current = None
    for token in text.replace(",", " ").split():
        lower = token.lower()
        if lower in sections:
            current = lower
            continue
        if current and re.match(r"^[A-Za-z_]\w*$", token):
            sections[current].append(token)
    return sections


def scope_bounds(lines, line_index):
    form_start = None
    event_start = None
    for index in range(0, line_index + 1):
        code = split_code_and_comment(lines[index])[0].strip()
        if re.match(r"^FORM\b", code, re.IGNORECASE):
            form_start = index
            event_start = None
        elif re.match(r"^ENDFORM\b", code, re.IGNORECASE):
            form_start = None
        elif form_start is None and re.match(r"^(START-OF-SELECTION|END-OF-SELECTION|INITIALIZATION|AT\s+SELECTION-SCREEN)\b", code, re.IGNORECASE):
            event_start = index
    if form_start is not None:
        for index in range(line_index + 1, len(lines)):
            if re.match(r"^ENDFORM\b", split_code_and_comment(lines[index])[0].strip(), re.IGNORECASE):
                return form_start + 1, index
        return form_start + 1, len(lines)
    if event_start is not None:
        return event_start + 1, len(lines)
    return 0, len(lines)


def declaration_section_end(lines, scope_start):
    index = scope_start
    in_type_block = False
    while index < len(lines):
        code = split_code_and_comment(lines[index])[0].strip()
        if not code or code.startswith("*") or code.startswith('"'):
            index += 1
            continue
        if re.match(r"^TYPES\s*:?\s+BEGIN\s+OF\b", code, re.IGNORECASE):
            in_type_block = True
            index += 1
            continue
        if in_type_block:
            index += 1
            if re.match(r"^END\s+OF\b", code, re.IGNORECASE):
                in_type_block = False
            continue
        if is_declaration_section_line(code):
            index += 1
            continue
        break
    return index


def is_declaration_section_line(code):
    return bool(
        re.match(
            r"^(REPORT|TABLES|TYPES|CONSTANTS|FIELD-SYMBOLS|SELECTION-SCREEN|PARAMETERS|SELECT-OPTIONS)\b|^DATA\s+",
            code,
            re.IGNORECASE,
        )
    )


def referenced_type_name(type_text):
    match = re.search(r"\b(?:TABLE\s+OF\s+)?(ty_[A-Za-z0-9_]+)\b", type_text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def collect_local_type_declarations(lines):
    local_types = {}
    pending_type = None
    simple_type = re.compile(r"^\s*TYPES\s*:?\s+([A-Za-z_]\w*)\b", re.IGNORECASE)
    begin_type = re.compile(r"^\s*TYPES\s*:?\s+BEGIN\s+OF\s+([A-Za-z_]\w*)\b", re.IGNORECASE)
    end_type = re.compile(r"^\s*END\s+OF\s+([A-Za-z_]\w*)\b", re.IGNORECASE)
    for number, line in enumerate(lines, start=1):
        code = split_code_and_comment(line)[0]
        begin_match = begin_type.search(code)
        if begin_match:
            pending_type = begin_match.group(1).lower()
            continue
        if pending_type:
            end_match = end_type.search(code)
            if end_match and end_match.group(1).lower() == pending_type:
                local_types[pending_type] = number
                pending_type = None
            continue
        type_match = simple_type.search(code)
        if type_match:
            name = type_match.group(1).lower()
            if name.startswith("ty_"):
                local_types[name] = number
    return local_types


def referenced_local_type(statement):
    if not re.match(r"^(?:DATA|CONSTANTS|FIELD-SYMBOLS)\b", statement, re.IGNORECASE):
        return None
    match = re.search(r"\b(?:TYPE|LIKE)\s+(?:(?:STANDARD|SORTED|HASHED)\s+TABLE\s+OF\s+)?(ty_[A-Za-z0-9_]+)\b", statement, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def enclosing_callable_name(lines, line_index):
    for index in range(line_index, max(-1, line_index - 20), -1):
        code = split_code_and_comment(lines[index])[0].strip()
        function_match = re.search(r"CALL\s+FUNCTION\s+'([^']+)'", code, re.IGNORECASE)
        if function_match:
            return function_match.group(1)
        method_match = re.search(r"\bCALL\s+METHOD\s+([A-Za-z_]\w*(?:=>|->)?[A-Za-z_]\w*)", code, re.IGNORECASE)
        if method_match:
            return method_match.group(1)
        if index != line_index and "." in code:
            break
    return None


def call_block(lines, start_index):
    terminated = False
    for index in range(start_index, min(len(lines), start_index + 40)):
        code = split_code_and_comment(lines[index])[0]
        stripped = code.strip()
        if terminated and starts_new_statement(stripped):
            return
        yield index + 1, lines[index]
        if stripped.endswith("."):
            terminated = True


def starts_new_statement(stripped):
    if not stripped:
        return False
    if re.match(r"^(EXPORTING|IMPORTING|TABLES|CHANGING|EXCEPTIONS)\b", stripped, re.IGNORECASE):
        return False
    if re.match(r"^[A-Za-z_]\w*\s*=", stripped):
        return False
    if re.match(r"^(DATA|FIELD-SYMBOLS|TYPES|CONSTANTS|PARAMETERS|SELECT-OPTIONS|FORM|ENDFORM|START-OF-SELECTION|END-OF-SELECTION|CALL|PERFORM|LOOP|READ|SELECT|IF|ENDIF|ENDLOOP|WRITE)\b", stripped, re.IGNORECASE):
        return True
    return False
