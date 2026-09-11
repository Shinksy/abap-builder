from services.abap_source import normalize_abap_blank_lines
from services.callable_signature_provider import normalize_provider_signatures
from services.validator import parse_callable_invocations


def function_module_call_statements(contract, callable_metadata=None):
    return "\n\n".join(call_function_statement(call, callable_metadata) for call in (contract or {}).get("function_module_calls") or [])


def class_method_call_statements(contract, callable_metadata=None):
    return "\n\n".join(call_method_statement(call, callable_metadata) for call in (contract or {}).get("class_method_calls") or [])


def callable_call_statements(contract, callable_metadata=None):
    calls = []
    calls.extend((call.get("sequence", 0), "function", call) for call in (contract or {}).get("function_module_calls") or [])
    calls.extend((call.get("sequence", 0), "method", call) for call in (contract or {}).get("class_method_calls") or [])
    statements = []
    for _sequence, kind, call in sorted(calls, key=lambda item: item[0]):
        if kind == "function":
            statements.append(call_function_statement(call, callable_metadata))
        else:
            statements.append(call_method_statement(call, callable_metadata))
    return statements


def call_function_statement(call, callable_metadata=None):
    signature = normalize_provider_signatures(callable_metadata).get(call.get("name"), {})
    return callable_statement(f"CALL FUNCTION '{call.get('name')}'", call, signature)


def call_method_statement(call, callable_metadata=None):
    return callable_statement(f"CALL METHOD {method_call_target(call)}", call, normalize_provider_signatures(callable_metadata).get(call.get("name"), {}))


def callable_statement(first_line, call, signature):
    sections = {"EXPORTING": [], "IMPORTING": [], "CHANGING": [], "TABLES": [], "RECEIVING": []}
    parameters = callable_parameters(signature)
    for mapping in call.get("parameters") or []:
        parameter_name = mapping.get("parameter") or returning_parameter_name(signature)
        variable = mapping.get("variable")
        if not parameter_name or not variable:
            continue
        parameter = parameters.get(str(parameter_name).upper()) or {}
        direction = str(parameter.get("direction") or "").upper()
        if mapping.get("returning") or direction == "RETURNING":
            sections["RECEIVING"].append((parameter_name, variable))
        else:
            section = abap_call_section_for_signature_direction(direction)
            if section in sections:
                sections[section].append((parameter_name, variable))
    lines = [first_line]
    for section_name in ("EXPORTING", "IMPORTING", "CHANGING", "TABLES", "RECEIVING"):
        values = sections[section_name]
        if not values:
            continue
        lines.append(f"  {section_name}")
        for index, (parameter, variable) in enumerate(values):
            suffix = "." if section_name == "RECEIVING" and index == len(values) - 1 else ""
            lines.append(f"    {parameter.lower()} = {variable}{suffix}")
    if lines[-1][-1:] != ".":
        lines[-1] += "."
    return "\n".join(lines)


def abap_call_section_for_signature_direction(direction):
    return {
        "IMPORTING": "EXPORTING",
        "EXPORTING": "IMPORTING",
        "CHANGING": "CHANGING",
        "TABLES": "TABLES",
    }.get(str(direction or "").upper(), "")


def method_call_target(call):
    if call.get("call_type") == "instance" and call.get("receiver"):
        _, _, method = str(call.get("name") or "").partition("=>")
        return f"{call['receiver']}->{method.lower()}"
    return str(call.get("name") or "").replace("=>", "=>").lower()


def callable_parameters(signature):
    params = signature.get("parameters") if isinstance(signature, dict) else {}
    result = {str(name).upper(): value for name, value in params.items()} if isinstance(params, dict) else {}
    returning = signature.get("returning") if isinstance(signature, dict) else None
    if isinstance(returning, dict) and returning.get("name"):
        result[str(returning.get("name")).upper()] = returning
    return result


def returning_parameter_name(signature):
    returning = signature.get("returning") if isinstance(signature, dict) else None
    return str(returning.get("name") or "").upper() if isinstance(returning, dict) else ""


def apply_deterministic_callable_interfaces(source, generation_contract=None, callable_metadata=None):
    deterministic = callable_call_statements(generation_contract, callable_metadata)
    if not deterministic:
        return source
    lines = str(source or "").splitlines()
    calls = parse_callable_invocations(lines)
    if not calls:
        return source
    output = []
    cursor = 0
    for index, call in enumerate(calls):
        start = call.get("start_index", 0)
        end = call.get("end_index", start)
        if start < cursor:
            continue
        output.extend(lines[cursor:start])
        if index < len(deterministic):
            indent = leading_whitespace(lines[start])
            output.extend(indent + line if line else line for line in deterministic[index].splitlines())
        cursor = end + 1
    output.extend(lines[cursor:])
    return normalize_abap_blank_lines("\n".join(output))


def leading_whitespace(value):
    import re

    return re.match(r"^\s*", str(value or "")).group(0)

