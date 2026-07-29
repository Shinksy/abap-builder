import re

from services.abap_source import (
    call_block,
    collect_local_type_declarations,
    collect_logical_declarations,
    referenced_local_type,
    split_code_and_comment,
    split_string_segments,
)


LINE_RULES = (
    ("INLINE_DATA_LOOP", r"\bLOOP\s+AT\b.*\bINTO\s+@?DATA\s*\(", "Inline DATA declaration in LOOP is not allowed.", "Declare the work area before LOOP."),
    ("INLINE_DATA_READ_TABLE", r"\bREAD\s+TABLE\b.*\bINTO\s+@?DATA\s*\(", "Inline DATA declaration in READ TABLE is not allowed.", "Declare the target before READ TABLE."),
    ("INLINE_DATA_SPLIT", r"\bSPLIT\b.*\bINTO\s+@?DATA\s*\(", "Inline DATA declaration in SPLIT is not allowed.", "Declare the targets before SPLIT."),
    ("INLINE_DATA_CATCH", r"\bCATCH\b.*\bINTO\s+@?DATA\s*\(", "Inline DATA declaration in CATCH is not allowed.", "Declare the exception reference before CATCH."),
    ("INLINE_DATA_SELECT", r"\bSELECT\b.*\bINTO\s+@?DATA\s*\(", "Inline DATA declaration in SELECT is not allowed.", "Declare the target before SELECT."),
    ("INVALID_DATA_DECLARATION", r"@?DATA\s*\(\s*[A-Za-z_]\w*\s*\)\s+TYPE\b", "Parenthesised DATA declaration is not valid classical ABAP.", "Use DATA name TYPE type."),
    ("INLINE_DATA_CALL_PARAMETER", r"\b[A-Za-z_]\w*\s*=\s*@?DATA\s*\(", "Inline DATA declaration in a call parameter is not allowed.", "Declare the call parameter target before the call."),
    ("INLINE_DATA", r"@?DATA\s*\(", "Inline DATA declaration is not allowed.", "Declare the variable separately before it is used."),
    ("INLINE_FINAL", r"\bFINAL\s*\(", "Inline FINAL declaration is not allowed.", "Use a standard DATA declaration."),
    ("INLINE_FIELD_SYMBOL", r"\bFIELD-SYMBOL\s*\(", "Inline FIELD-SYMBOL declaration is not allowed.", "Declare the field symbol separately."),
    ("STRING_TEMPLATE", r"\|[^|\r\n]+\|", "String templates are not allowed.", "Use CONCATENATE or classical character handling."),
    ("HOST_VARIABLE_ESCAPE", r"@(?!DATA\s*\()[A-Za-z_]\w*", "Host-variable escape syntax is not allowed.", "Remove @ host-variable escapes."),
    ("STRING_CONCATENATION", r"&&", "Modern && string concatenation is not allowed.", "Use CONCATENATE."),
    ("CONSTRUCTOR_EXPRESSION", r"\b(?:VALUE\s+(?:#|[A-Za-z_]\w*)|NEW\s+(?:#|[A-Za-z_]\w*)|CONV\s+(?:#|[A-Za-z_]\w*)|CORRESPONDING\s+(?:#|[A-Za-z_]\w*)|REDUCE\s+(?:#|[A-Za-z_]\w*)|FILTER\s+(?:#|[A-Za-z_]\w*))\s*\(", "Constructor expressions are not allowed.", "Use explicit declarations and assignments."),
    ("TABLE_EXPRESSION", r"\b[A-Za-z_]\w*\s*\[[^\]\r\n]+\]", "Table expressions are not allowed.", "Use READ TABLE with an explicit target."),
)
SPECIFIC_INLINE_RULES = {"INLINE_DATA_LOOP", "INLINE_DATA_READ_TABLE", "INLINE_DATA_SPLIT", "INLINE_DATA_CATCH", "INLINE_DATA_SELECT", "INVALID_DATA_DECLARATION", "INLINE_DATA_CALL_PARAMETER"}
FORBIDDEN_PREFIXES = ("it_", "gt_", "lt_", "ls_", "gs_", "wa_")
CALLABLE_DIRECTIONS = ("EXPORTING", "IMPORTING", "CHANGING", "TABLES", "RETURNING")
ABAP_HYPHEN_KEYWORDS = {
    "LIST-PROCESSING",
    "USER-COMMAND",
    "LINE-SELECTION",
    "START-OF-SELECTION",
    "END-OF-SELECTION",
    "TOP-OF-PAGE",
    "END-OF-PAGE",
    "SELECT-OPTIONS",
    "FIELD-SYMBOLS",
}
LOCAL_QUALIFIED_PREFIXES = (
    "FS_",
    "GS_",
    "GT_",
    "G_",
    "LS_",
    "LT_",
    "L_",
    "P_",
    "S_",
    "ST_",
    "SY",
    "T_",
    "WA_",
    "W_",
)


def validate_abap(source, callable_signatures=None, identifier_provenance=None, alv_requested=False):
    lines = source.splitlines()
    issues = validate_line_rules(lines)
    issues += validate_callable_signatures(lines, callable_signatures)
    issues += validate_indented_asterisk_comments(lines)
    issues += validate_leave_list_page(lines)
    issues += validate_list_processing_leave_report(lines)
    issues += validate_executable_placeholders(lines)
    issues += validate_selection_screen(lines)
    issues += validate_parameter_declarations(lines)
    issues += validate_chained_declarations(lines)
    issues += validate_ddic_identifier_provenance(lines, identifier_provenance)
    issues += validate_alv(lines)
    issues += validate_requested_alv_output(lines, alv_requested)
    issues += validate_select_option_guards(lines)
    issues += validate_binary_search(lines)
    issues += validate_naming(lines)
    issues += validate_local_type_declaration_order(lines)
    return dedupe_issues(issues)


def validate_ddic_identifier_provenance(lines, identifier_provenance=None):
    if identifier_provenance is None:
        return []
    approved = {identifier.upper(): metadata for identifier, metadata in identifier_provenance.items()}
    approved_identifiers = {metadata.get("identifier", identifier).upper() for identifier, metadata in approved.items()}
    issues = []
    for line_number, line in enumerate(lines, start=1):
        code = split_code_and_comment(line)[0]
        for segment, is_string in split_string_segments(code):
            if is_string:
                continue
            for match in re.finditer(r"\b[A-Za-z_]\w*-[A-Za-z_]\w*\b", segment):
                proposed = match.group(0).upper()
                if proposed in ABAP_HYPHEN_KEYWORDS:
                    continue
                if is_local_qualified_identifier(proposed):
                    continue
                if proposed in approved:
                    continue
                closest = closest_ddic_identifier(proposed, approved_identifiers)
                closest_metadata = approved.get(closest, {}) if closest else {}
                issues.append(
                    issue(
                        "ABAP_UNVERIFIED_DDIC_IDENTIFIER",
                        line_number,
                        f"Unverified SAP identifier {proposed} is not approved.",
                        line,
                        "Use an approved SAP identifier from source, specification, SAP metadata, or the approved change plan.",
                        proposed_identifier=proposed,
                        closest_identifier=closest,
                        closest_source=closest_metadata.get("source"),
                    )
                )
    return issues


def is_local_qualified_identifier(identifier):
    prefix = identifier.split("-", 1)[0].upper()
    return any(prefix == item.rstrip("_") or prefix.startswith(item) for item in LOCAL_QUALIFIED_PREFIXES)


def closest_ddic_identifier(identifier, candidates):
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: levenshtein_distance(identifier.upper(), candidate.upper()))


def levenshtein_distance(left, right):
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[right_index] + 1,
                    current[right_index - 1] + 1,
                    previous[right_index - 1] + (0 if left_char == right_char else 1),
                )
            )
        previous = current
    return previous[-1]


def normalize_callable_signatures(callable_signatures):
    normalized = {}
    if not callable_signatures:
        return normalized
    if isinstance(callable_signatures, dict) and "callables" in callable_signatures:
        callable_signatures = callable_signatures["callables"]
    if isinstance(callable_signatures, list):
        iterable = [(entry.get("callable") or entry.get("name"), entry) for entry in callable_signatures if isinstance(entry, dict)]
    elif isinstance(callable_signatures, dict):
        iterable = callable_signatures.items()
    else:
        return normalized

    for callable_name, signature in iterable:
        if not callable_name or not isinstance(signature, dict):
            continue
        params = signature.get("parameters", signature.get("params", signature))
        normalized_params = {}
        if isinstance(params, list):
            param_items = [(item.get("parameter") or item.get("name"), item) for item in params if isinstance(item, dict)]
        elif isinstance(params, dict):
            param_items = params.items()
        else:
            param_items = []
        for parameter_name, metadata in param_items:
            if not parameter_name:
                continue
            if isinstance(metadata, dict):
                if not (metadata.get("direction") or metadata.get("section")):
                    continue
                direction = (metadata.get("direction") or metadata.get("section")).upper()
                abap_type = metadata.get("type") or metadata.get("abap_type")
                required = bool(metadata.get("required", False))
            else:
                continue
            if direction not in CALLABLE_DIRECTIONS:
                continue
            normalized_params[parameter_name.lower()] = {
                "name": parameter_name,
                "direction": direction,
                "abap_type": str(abap_type) if abap_type else None,
                "required": required,
            }
        if normalized_params:
            normalized[callable_name.lower()] = {"name": callable_name, "parameters": normalized_params}
    return normalized


def normalize_callable_mappings(callable_mappings):
    normalized = {}
    if not callable_mappings:
        return normalized
    if isinstance(callable_mappings, dict) and "technical_mapping" in callable_mappings:
        callable_mappings = callable_mappings["technical_mapping"]
    if isinstance(callable_mappings, dict) and "callable_mappings" in callable_mappings:
        callable_mappings = callable_mappings["callable_mappings"]
    if isinstance(callable_mappings, dict) and "technical_mappings" in callable_mappings:
        callable_mappings = callable_mappings["technical_mappings"]
    if isinstance(callable_mappings, dict) and "callable" in callable_mappings:
        callable_mappings = [callable_mappings]
    if isinstance(callable_mappings, list):
        iterable = [(entry.get("callable"), entry.get("parameter_mappings", {})) for entry in callable_mappings if isinstance(entry, dict)]
    elif isinstance(callable_mappings, dict):
        iterable = callable_mappings.items()
    else:
        return normalized
    for callable_name, mappings in iterable:
        if not callable_name or not isinstance(mappings, dict):
            continue
        sections = {}
        for direction, params in mappings.items():
            upper_direction = direction.upper()
            if upper_direction not in CALLABLE_DIRECTIONS or not isinstance(params, dict):
                continue
            sections[upper_direction] = {name.lower(): {"name": name, "value": value} for name, value in params.items()}
        normalized[callable_name.lower()] = sections
    return normalized


def validate_callable_signatures(lines, callable_signatures):
    signatures = normalize_callable_signatures(callable_signatures)
    if not signatures:
        return []
    issues = []
    for call in parse_callable_invocations(lines):
        signature = signatures.get(call["name"].lower())
        if not signature:
            continue
        parameters = signature["parameters"]
        present = set()
        for actual in call["parameters"]:
            parameter = parameters.get(actual["name"].lower())
            if not parameter:
                issues.append(callable_issue("CALLABLE_UNKNOWN_PARAMETER", actual, call, None, "Remove the unsupported parameter or supply signature metadata for it."))
                continue
            if parameter["direction"] == "RETURNING":
                issues.append(callable_issue("CALLABLE_RETURNING_MISUSED", actual, call, parameter, "Use the callable's RETURNING result form instead of passing it as a normal parameter."))
                continue
            if actual["section"] in {"TABLES", "CHANGING"} and parameter["direction"] != actual["section"]:
                issues.append(callable_issue("CALLABLE_UNSUPPORTED_SECTION", actual, call, parameter, f"Move {actual['name']} to {parameter['direction']} or remove the unsupported {actual['section']} parameter."))
                continue
            if parameter["direction"] != actual["section"]:
                issues.append(callable_issue("CALLABLE_PARAMETER_WRONG_SECTION", actual, call, parameter, f"Move {actual['name']} to {parameter['direction']}."))
                continue
            present.add(actual["name"].lower())
        for parameter in parameters.values():
            if parameter["required"] and parameter["direction"] != "RETURNING" and parameter["name"].lower() not in present:
                issues.append(
                    issue(
                        "CALLABLE_REQUIRED_PARAMETER_MISSING",
                        call["line_number"],
                        f"Required parameter {parameter['name']} is missing for {call['name']}.",
                        call["source_line"],
                        f"Add {parameter['name']} under {parameter['direction']}.",
                        callable_name=call["name"],
                        parameter_name=parameter["name"],
                        expected_section=parameter["direction"],
                        actual_section=None,
                    )
                )
    return issues


def validate_line_rules(lines):
    issues = []
    compiled = [(rule_id, re.compile(pattern, re.IGNORECASE), msg, fix) for rule_id, pattern, msg, fix in LINE_RULES]
    for line_number, line in enumerate(lines, start=1):
        specific_inline_found = False
        for rule_id, pattern, message, suggested_fix in compiled:
            if rule_id == "INLINE_DATA" and specific_inline_found:
                continue
            matches = list(pattern.finditer(line))
            if matches and rule_id in SPECIFIC_INLINE_RULES:
                specific_inline_found = True
            if rule_id == "STRING_CONCATENATION" and matches:
                issues.append(issue(rule_id, line_number, message, line, suggested_fix))
                continue
            for _match in matches:
                issues.append(issue(rule_id, line_number, message, line, suggested_fix))
    return issues


def parse_callable_invocations(lines):
    calls = []
    for index, line in enumerate(lines):
        code = split_code_and_comment(line)[0]
        function_match = re.search(r"\bCALL\s+FUNCTION\s+'([^']+)'", code, re.IGNORECASE)
        method_match = re.search(r"\bCALL\s+METHOD\s+([A-Za-z_]\w*(?:(?:=>|->)[A-Za-z_]\w*)?)", code, re.IGNORECASE)
        if not function_match and not method_match:
            continue
        name = function_match.group(1) if function_match else method_match.group(1)
        call = {
            "name": name,
            "line_number": index + 1,
            "source_line": line,
            "start_index": index,
            "end_index": index,
            "parameters": [],
            "lines": [],
        }
        current_section = None
        for number, call_line in call_block(lines, index):
            call["end_index"] = number - 1
            call["lines"].append((number, call_line))
            stripped = split_code_and_comment(call_line)[0].strip()
            section_match = re.match(r"^(EXPORTING|IMPORTING|CHANGING|TABLES|RETURNING)\b", stripped, re.IGNORECASE)
            if section_match:
                current_section = section_match.group(1).upper()
                continue
            parameter_match = re.match(r"^([A-Za-z_]\w*)\s*=", stripped)
            if parameter_match and current_section:
                call["parameters"].append(
                    {
                        "name": parameter_match.group(1),
                        "section": current_section,
                        "line_number": number,
                        "source_line": call_line,
                    }
                )
        calls.append(call)
    return calls


def callable_issue(rule_id, actual, call, parameter, suggested_fix):
    expected_section = parameter["direction"] if parameter else None
    messages = {
        "CALLABLE_UNKNOWN_PARAMETER": f"Parameter {actual['name']} is not defined for {call['name']}.",
        "CALLABLE_PARAMETER_WRONG_SECTION": f"Parameter {actual['name']} is under {actual['section']} but {call['name']} expects {expected_section}.",
        "CALLABLE_UNSUPPORTED_SECTION": f"Parameter {actual['name']} is passed under unsupported {actual['section']} for {call['name']}.",
        "CALLABLE_RETURNING_MISUSED": f"RETURNING parameter {actual['name']} for {call['name']} must not be passed as a normal call parameter.",
    }
    return issue(
        rule_id,
        actual["line_number"],
        messages[rule_id],
        actual["source_line"],
        suggested_fix,
        callable_name=call["name"],
        parameter_name=actual["name"],
        expected_section=expected_section,
        actual_section=actual["section"],
    )


def validate_indented_asterisk_comments(lines):
    issues = []
    for number, line in enumerate(lines, start=1):
        if not line or line.startswith("*"):
            continue
        if re.match(r"^\s+\*", line):
            issues.append(
                issue(
                    "INDENTED_ASTERISK_COMMENT",
                    number,
                    "Classical ABAP full-line * comments must start in column 1.",
                    line,
                    "Move the * comment marker to column 1.",
                )
            )
    return issues


def validate_leave_list_page(lines):
    issues = []
    pattern = re.compile(r"^\s*LEAVE\s+LIST-PAGE\s*\.\s*$", re.IGNORECASE)
    for number, line in enumerate(lines, start=1):
        code = split_code_and_comment(line)[0]
        for segment, is_string in split_string_segments(code):
            if is_string:
                continue
            if pattern.match(segment):
                issues.append(
                    issue(
                        "INVALID_LEAVE_LIST_PAGE",
                        number,
                        "LEAVE LIST-PAGE is unsupported on the configured classical SAP ECC target.",
                        line,
                        "LEAVE LIST-PROCESSING.",
                    )
                )
                break
    return issues


def validate_list_processing_leave_report(lines):
    issues = []
    for number, line in enumerate(lines, start=1):
        code = split_code_and_comment(line)[0]
        for segment, is_string in split_string_segments(code):
            if is_string:
                continue
            if re.match(r"^\s*LEAVE\s+REPORT\s*\.\s*$", segment, re.IGNORECASE):
                issues.append(
                    issue(
                        "ABAP_LIST_PROCESSING_EXIT_MISMATCH",
                        number,
                        "LEAVE REPORT is not valid for the configured classical SAP ECC target; use LEAVE LIST-PROCESSING.",
                        line,
                        "Use LEAVE LIST-PROCESSING.",
                    )
                )
                break
    return issues


def starts_abap_event_block(stripped):
    return bool(
        re.match(
            r"^(INITIALIZATION|START-OF-SELECTION|END-OF-SELECTION|AT\s+SELECTION-SCREEN|AT\s+LINE-SELECTION|AT\s+USER-COMMAND|AT\s+PF\d+|TOP-OF-PAGE(?:\s+DURING\s+LINE-SELECTION)?|END-OF-PAGE)\b",
            stripped,
            re.IGNORECASE,
        )
    )


def is_list_processing_event(stripped):
    return bool(
        re.match(
            r"^(AT\s+LINE-SELECTION|AT\s+USER-COMMAND|AT\s+PF\d+|TOP-OF-PAGE\s+DURING\s+LINE-SELECTION)\b",
            stripped,
            re.IGNORECASE,
        )
    )


def validate_executable_placeholders(lines):
    issues = []
    placeholder_terms = re.compile(
        r"\b(?:placeholder|dummy\s+to\s+compile|satisfy\s+standard\s+syntax|todo|fake)\b",
        re.IGNORECASE,
    )
    for number, line in enumerate(lines, start=1):
        code, comment = split_code_and_comment(line)
        if not code.strip() or not comment or not placeholder_terms.search(comment):
            continue
        if is_artificial_executable_code(code, comment):
            issues.append(
                issue(
                    "EXECUTABLE_PLACEHOLDER",
                    number,
                    "Executable placeholder code is not allowed.",
                    line,
                    "Replace artificial placeholder logic with the real implementation.",
                )
            )
    return issues


def is_artificial_executable_code(code, comment):
    upper_code = code.upper()
    upper_comment = comment.upper()
    if "TODO" in upper_comment:
        return bool(re.search(r"\b(?:SELECT|WHERE|IF|LOOP|READ|CALL|MOVE|APPEND|MODIFY|DELETE|UPDATE|INSERT|PERFORM|PARAMETERS|SELECT-OPTIONS)\b", upper_code))
    if "FAKE" in upper_comment:
        return bool(re.search(r"\b(?:PARAMETERS|SELECT-OPTIONS|WHERE|SELECT|IF)\b", upper_code))
    return bool(re.search(r"\b(?:WHERE|IF|LOOP|READ|SELECT|CALL|MOVE|APPEND|MODIFY|DELETE|UPDATE|INSERT|PERFORM|PARAMETERS|SELECT-OPTIONS)\b|=", upper_code))


def validate_selection_screen(lines):
    issues = []
    for line_number, item_text, source_line in parameter_items(lines):
        upper = item_text.upper()
        if "AS CHECKBOX" in upper and re.search(r"\b(TYPE|LENGTH)\b", upper):
            issues.append(issue("INVALID_CHECKBOX_SYNTAX", line_number, "Checkbox PARAMETERS must not use TYPE or LENGTH.", source_line, "Use PARAMETERS name AS CHECKBOX."))

    for number, line in enumerate(lines, start=1):
        upper = line.upper()
        if "PARAMETERS" not in upper:
            continue
        if "AS RADIOBUTTON" in upper:
            issues.append(issue("INVALID_RADIOBUTTON_SYNTAX", number, "Use RADIOBUTTON GROUP, not AS RADIOBUTTON.", line, "Use PARAMETERS name RADIOBUTTON GROUP group."))
        if "RADIOBUTTON" in upper:
            if re.search(r"\b(TYPE|LENGTH)\b", upper):
                issues.append(issue("INVALID_RADIOBUTTON_SYNTAX", number, "Radio-button PARAMETERS must not use TYPE or LENGTH.", line, "Use PARAMETERS name RADIOBUTTON GROUP group."))
            if "GROUP" not in upper:
                issues.append(issue("RADIOBUTTON_WITHOUT_GROUP", number, "RADIOBUTTON must specify GROUP.", line, "Add RADIOBUTTON GROUP group."))
    return issues


def validate_parameter_declarations(lines):
    issues = []
    for line_number, item_text, source_line in parameter_items(lines):
        tokens = declaration_tokens(item_text)
        upper_tokens = [token.upper() for token in tokens]
        if "RADIOBUTTON" in upper_tokens:
            for token in tokens:
                if token.lower() == "out":
                    issues.append(
                        issue(
                            "ABAP_INVALID_PARAMETER_DECLARATION",
                            line_number,
                            "Radio-button PARAMETERS declaration contains malformed token out.",
                            source_line,
                            "Remove malformed token out from the radio-button declaration.",
                            token=token,
                        )
                    )
                    break
    return issues


def declaration_tokens(text):
    code = split_code_and_comment(text)[0]
    tokens = []
    for segment, is_string in split_string_segments(code):
        if is_string:
            continue
        tokens.extend(re.findall(r"[A-Za-z_]\w*|'[^']*'|\S", segment))
    return tokens


def validate_chained_declarations(lines):
    issues = []
    chain_keyword = None
    declaration_keywords = r"DATA|TYPES|CONSTANTS|TABLES|PARAMETERS|SELECT-OPTIONS|FIELD-SYMBOLS"
    for line_number, line in enumerate(lines, start=1):
        code = split_code_and_comment(line)[0]
        stripped = code.strip()
        if not stripped:
            continue
        starts_chain = re.match(rf"^({declaration_keywords})\s*:", stripped, re.IGNORECASE)
        starts_declaration = re.match(rf"^({declaration_keywords})\b", stripped, re.IGNORECASE)
        merged = re.search(rf",\s*(?:{declaration_keywords})\s*:?\b", stripped, re.IGNORECASE)
        if merged:
            issues.append(
                issue(
                    "ABAP_BROKEN_CHAINED_DECLARATION",
                    line_number,
                    "Declaration appears to be merged with another declaration statement.",
                    line,
                    "Keep declaration chains separate and terminate the previous statement with a period.",
                )
            )
        if starts_chain:
            chain_keyword = starts_chain.group(1).upper()
        elif starts_declaration:
            chain_keyword = None

        if stripped.endswith(",") and chain_keyword is None:
            issues.append(
                issue(
                    "ABAP_BROKEN_CHAINED_DECLARATION",
                    line_number,
                    "Declaration has a trailing comma but is not inside a chained declaration.",
                    line,
                    "Use a period to terminate the declaration, or open a valid chained declaration with a colon.",
                )
            )

        if stripped.endswith("."):
            chain_keyword = None
    return issues


def parameter_items(lines):
    items = []
    for declaration in collect_logical_declarations(lines, "PARAMETERS"):
        for offset, line in enumerate(declaration["lines"]):
            code = split_code_and_comment(line)[0]
            if offset == 0:
                code = re.sub(r"^\s*PARAMETERS\s*:?\s*", "", code, flags=re.IGNORECASE)
            text = re.sub(r"\s*[,\.]\s*$", "", code).strip()
            if text:
                items.append((declaration["start"] + offset + 1, text, line))
    return items


def validate_alv(lines):
    issues = []
    lvc_names = {m.group(1).lower() for line in lines for m in [re.search(r"\bDATA\s+([A-Za-z_]\w*)\s+TYPE\s+LVC_[TS]_FCAT\b", line, re.IGNORECASE)] if m}
    for index, line in enumerate(lines):
        if not re.search(r"CALL\s+FUNCTION\s+'REUSE_ALV_GRID_DISPLAY'", line, re.IGNORECASE):
            continue
        for number, call_line in call_block(lines, index):
            if re.search(r"\bLVC_[TS]_FCAT\b", call_line, re.IGNORECASE) or any(re.search(rf"\b{name}\b", call_line, re.IGNORECASE) for name in lvc_names):
                issues.append(issue("ALV_LVC_FIELDCAT_WITH_REUSE_ALV", number, "REUSE_ALV_GRID_DISPLAY expects SLIS field catalogues, not LVC field catalogues.", call_line, "Use SLIS_T_FIELDCAT_ALV / SLIS_FIELDCAT_ALV."))
            if re.search(r"\bI_STRUCTURE_NAME\s*=\s*'?TY_[A-Za-z0-9_]+'?", call_line, re.IGNORECASE):
                issues.append(issue("ALV_LOCAL_STRUCTURE_NAME", number, "i_structure_name must refer to a Dictionary structure, not a local TYPES name.", call_line, "Pass a Dictionary structure name or provide a proper SLIS field catalogue."))
    return issues


def validate_requested_alv_output(lines, alv_requested=False):
    if not alv_requested or has_real_alv_call(lines):
        return []
    for number, line in enumerate(lines, start=1):
        if re.search(r"\bWRITE\b", split_code_and_comment(line)[0], re.IGNORECASE):
            return [
                issue(
                    "ALV_REQUESTED_WITH_CLASSICAL_LIST_OUTPUT",
                    number,
                    "ALV output was requested, but the generated output uses WRITE without a real ALV call.",
                    line,
                    "Use REUSE_ALV_GRID_DISPLAY or the ALV mechanism explicitly required by the specification.",
                )
            ]
    return []


def has_real_alv_call(lines):
    source = "\n".join(split_code_and_comment(line)[0] for line in lines)
    patterns = (
        r"\bCALL\s+FUNCTION\s+'REUSE_ALV_[A-Z0-9_]+'",
        r"\bCL_GUI_ALV_GRID\b",
        r"\bCL_SALV_TABLE\b",
        r"\bSET_TABLE_FOR_FIRST_DISPLAY\b",
    )
    return any(re.search(pattern, source, re.IGNORECASE) for pattern in patterns)


def validate_select_option_guards(lines):
    issues = []
    for index, line in enumerate(lines):
        match = re.search(r"\bIF\b.*\b([A-Za-z_]\w*)\[\]\s+IS\s+NOT\s+INITIAL\b", line, re.IGNORECASE)
        if not match:
            continue
        option = match.group(1)
        block = "\n".join(lines[index + 1:index + 8])
        if re.search(r"\bSELECT\b", block, re.IGNORECASE) and re.search(rf"\bIN\s+{re.escape(option)}\b", block, re.IGNORECASE):
            issues.append(issue("BLANK_SELECT_OPTION_GUARD", index + 1, "A blank select-option means no restriction; the SELECT should still run.", line, "Remove the guard and keep the select-option in the SQL filter."))
    return issues


def validate_binary_search(lines):
    issues = []
    sorted_tables = set()
    for number, line in enumerate(lines, start=1):
        if re.search(r"\b(FORM|START-OF-SELECTION|END-OF-SELECTION)\b", line, re.IGNORECASE):
            sorted_tables.clear()
        sort_match = re.search(r"\bSORT\s+([A-Za-z_]\w*)\s+BY\b", line, re.IGNORECASE)
        if sort_match:
            sorted_tables.add(sort_match.group(1).lower())
        read_match = re.search(r"\bREAD\s+TABLE\s+([A-Za-z_]\w*)\b.*\bBINARY\s+SEARCH\b", line, re.IGNORECASE)
        if read_match and read_match.group(1).lower() not in sorted_tables:
            issues.append(issue("BINARY_SEARCH_WITHOUT_SORT", number, "BINARY SEARCH requires an earlier SORT of the same table in the same block.", line, "SORT the table by the searched key fields before READ TABLE ... BINARY SEARCH."))
    return issues


def validate_naming(lines):
    issues = []
    reported = set()
    pattern = re.compile(r"\b(?:DATA|FIELD-SYMBOLS|CONSTANTS|TYPES|PARAMETERS|SELECT-OPTIONS)\s*:?\s*<?([A-Za-z_]\w*)>?", re.IGNORECASE)
    for number, line in enumerate(lines, start=1):
        match = pattern.search(line)
        if not match:
            continue
        name = match.group(1).lower()
        if name in reported or not name.startswith(FORBIDDEN_PREFIXES):
            continue
        reported.add(name)
        issues.append(issue("FORBIDDEN_NAMING_PREFIX", number, "Declared identifier uses a forbidden prefix.", line, "Use a business-meaningful name without it_, gt_, lt_, ls_, gs_, or wa_."))
    return issues


def validate_local_type_declaration_order(lines):
    issues = []
    local_types = collect_local_type_declarations(lines)
    declared = set()
    for number, line in enumerate(lines, start=1):
        code = split_code_and_comment(line)[0]
        statement = code.strip()
        if not statement:
            continue
        for type_name, declared_line in local_types.items():
            if declared_line <= number:
                declared.add(type_name)
        reference = referenced_local_type(statement)
        if not reference:
            continue
        lower_reference = reference.lower()
        if lower_reference not in local_types:
            issues.append(
                issue(
                    "UNKNOWN_LOCAL_TYPE",
                    number,
                    f"Declaration references unknown local type {reference}.",
                    line,
                    "Declare the local type before using it, or use a Dictionary type.",
                )
            )
        elif lower_reference not in declared:
            issues.append(
                issue(
                    "TYPE_USED_BEFORE_DECLARATION",
                    number,
                    f"Declaration references local type {reference} before it is declared.",
                    line,
                    "Move this declaration after the referenced local TYPES definition.",
                )
            )
    return issues


def issue(rule_id, line_number, message, source_line, suggested_fix, **extra):
    item = {"rule_id": rule_id, "severity": "error", "line_number": line_number, "message": message, "source_line": source_line, "suggested_fix": suggested_fix}
    item.update(extra)
    return item


def dedupe_issues(issues):
    seen = set()
    deduped = []
    for item in issues:
        key = (
            item["rule_id"],
            item["line_number"],
            item.get("callable_name"),
            item.get("parameter_name"),
            item.get("expected_section"),
            item.get("actual_section"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
