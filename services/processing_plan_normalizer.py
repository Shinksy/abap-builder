import json
import re


GLOBAL_STYLE_PREFIXES = ("t_", "st_", "w_", "gt_", "gs_", "gv_", "it_", "lt_", "ls_", "lv_", "wa_", "ct_")

SUPPORTED_PROCESSING_PLAN_OPERATIONS = {
    "APPEND",
    "CALL_FUNCTION",
    "CALL_METHOD",
    "CALL_STATIC_METHOD",
    "AGGREGATE",
    "AVERAGE",
    "CALCULATE",
    "CLEAR",
    "CONCATENATE",
    "DELETE",
    "DERIVE",
    "IF",
    "LOOP",
    "MOVE",
    "PERCENTAGE",
    "READ",
    "SORT",
    "TRANSFORM",
}


def normalize_processing_plan(value, context=None):
    return normalize_processing_plan_with_diagnostics(value, context=context)["plan"]


def normalize_processing_plan_with_diagnostics(value, context=None):
    diagnostics = {"rejected_steps": [], "modified_steps": [], "transformation_trace": []}
    append_processing_plan_trace(diagnostics, "normalize_processing_plan.input", value)
    if not isinstance(value, dict):
        record_processing_step_rejection(diagnostics, [], value, "processing plan root is not an object", "normalize_processing_plan")
        append_processing_plan_trace(diagnostics, "normalize_processing_plan.output", {"processing_steps": []})
        return {"plan": {"processing_steps": []}, "diagnostics": diagnostics}
    steps = normalize_processing_step_collection(value.get("processing_steps"))
    append_processing_plan_trace(diagnostics, "normalize_processing_step_collection.root", {"processing_steps": steps})
    if steps is None:
        record_processing_step_rejection(diagnostics, ["processing_steps"], steps, "processing_steps is not an array", "normalize_processing_plan")
        append_processing_plan_trace(diagnostics, "normalize_processing_plan.output", {"processing_steps": []})
        return {"plan": {"processing_steps": []}, "diagnostics": diagnostics}
    normalized_context = dict(context or {})
    normalized_context["diagnostics"] = diagnostics
    normalized = normalize_processing_steps(steps, normalized_context, path=["processing_steps"])
    append_processing_plan_trace(diagnostics, "normalize_processing_steps.root", {"processing_steps": normalized})
    normalized = prune_unused_read_steps(normalized, diagnostics, path=["processing_steps"])
    append_processing_plan_trace(diagnostics, "prune_unused_read_steps.root", {"processing_steps": normalized})
    plan = {"processing_steps": renumber_processing_steps(normalized)}
    append_processing_plan_trace(diagnostics, "renumber_processing_steps.root", plan)
    append_processing_plan_trace(diagnostics, "normalize_processing_plan.output", plan)
    return {"plan": plan, "diagnostics": diagnostics}


def normalize_processing_steps(steps, context, path=None):
    steps = normalize_processing_step_collection(steps)
    if steps is None:
        record_processing_step_rejection(context.get("diagnostics"), path, steps, "steps branch is not an array", "normalize_processing_steps")
        return []
    normalized = []
    current_loop = None
    for index, item in enumerate(steps or [], start=1):
        item_path = list(path or []) + [index - 1]
        step = normalize_processing_step(item, index, context, item_path)
        if not step:
            continue
        if step.get("operation") == "LOOP":
            normalized.append(step)
            current_loop = step if not processing_step_has_child_steps(item) else None
            continue
        if current_loop is not None:
            current_loop.setdefault("steps", []).append(step)
        else:
            normalized.append(step)
    return normalized


def normalize_processing_step_collection(steps):
    if isinstance(steps, list):
        return steps
    if isinstance(steps, dict):
        def sort_key(item):
            key, _value = item
            text = str(key or "").strip()
            return (0, int(text)) if text.isdigit() else (1, text)

        return [value for _key, value in sorted(steps.items(), key=sort_key)]
    return None


def processing_step_has_child_steps(item):
    if not isinstance(item, dict):
        return False
    item = canonical_processing_step_input(item)
    for key in ("steps", "child_steps", "children", "body", "then", "then_steps", "else", "else_steps"):
        if normalize_processing_step_collection(item.get(key)):
            return True
    return False


def normalize_processing_step(item, index, context, path=None):
    if not isinstance(item, dict):
        record_processing_step_rejection(context.get("diagnostics"), path, item, "step is not an object", "normalize_processing_step")
        return None
    item = canonical_processing_step_input(item)
    operation = str(item.get("operation") or "").strip().upper()
    if operation == "CALL_METHOD" and not (item.get("object") or item.get("object_name")) and "=>" in str(item.get("name") or item.get("callable") or ""):
        operation = "CALL_STATIC_METHOD"
    if operation not in SUPPORTED_PROCESSING_PLAN_OPERATIONS:
        record_processing_step_rejection(context.get("diagnostics"), path, item, "unsupported or missing operation", "normalize_processing_step")
        return None
    step = {"step": item.get("step") if isinstance(item.get("step"), int) else index, "operation": operation}
    if operation == "LOOP":
        source = normalize_plan_identifier(item.get("source"))
        if source:
            step["source"] = source
        into = normalize_plan_identifier(item.get("into")) or work_area_for_table(source, context)
        if into:
            step["into"] = into
        raw_children = item.get("steps") or item.get("child_steps") or item.get("children") or item.get("body") or []
        children = normalize_processing_steps(raw_children, context, path=list(path or []) + ["steps"])
        record_empty_normalized_branch(context.get("diagnostics"), list(path or []) + ["steps"], raw_children, children, "LOOP steps branch")
        step["steps"] = children
        record_processing_step_modification(context.get("diagnostics"), path, item, step, "normalized LOOP fields and child branch", "normalize_processing_step")
        return step
    if operation == "READ":
        source = normalize_plan_identifier(item.get("source"))
        into = normalize_plan_identifier(item.get("into")) or work_area_for_table(source, context)
        conditions = normalize_read_lookup_conditions(source, into, normalize_read_conditions(item, context), context)
        if not source or not into or not conditions:
            record_processing_step_rejection(context.get("diagnostics"), path, item, "READ step is missing source, into, or valid conditions", "normalize_processing_step")
            return None
        step.update({"source": source, "into": into, "conditions": conditions})
        record_processing_step_modification(context.get("diagnostics"), path, item, step, "normalized READ source, work area, and conditions", "normalize_processing_step")
        return step
    if operation == "MOVE":
        source = normalize_plan_reference(item.get("source"), context, role="source")
        target = normalize_plan_reference(item.get("target"), context, role="target")
        if not source or not target:
            record_processing_step_rejection(context.get("diagnostics"), path, item, "MOVE step is missing source or target", "normalize_processing_step")
            return None
        step.update({"source": source, "target": target})
        record_processing_step_modification(context.get("diagnostics"), path, item, step, "normalized MOVE source and target references", "normalize_processing_step")
        return step
    if operation in {"CALCULATE", "DERIVE"}:
        target = normalize_plan_reference(item.get("target"), context, role="target")
        expression = str(item.get("expression") or item.get("formula") or item.get("calculation") or "").strip()
        sources = normalize_plan_reference_list(item.get("sources") or item.get("source_fields"), context)
        if not target or not expression:
            record_processing_step_rejection(context.get("diagnostics"), path, item, f"{operation} step is missing target or expression", "normalize_processing_step")
            return None
        step.update({"target": target, "expression": expression, "sources": sources})
        record_processing_step_modification(context.get("diagnostics"), path, item, step, f"normalized {operation} target, expression, and source references", "normalize_processing_step")
        return step
    if operation == "TRANSFORM":
        source = normalize_plan_reference(item.get("source"), context, role="source")
        target = normalize_plan_reference(item.get("target"), context, role="target")
        transformation = str(item.get("transformation") or item.get("expression") or "").strip()
        if not source or not target or not transformation:
            record_processing_step_rejection(context.get("diagnostics"), path, item, "TRANSFORM step is missing source, target, or transformation", "normalize_processing_step")
            return None
        step.update({"source": source, "target": target, "transformation": transformation})
        record_processing_step_modification(context.get("diagnostics"), path, item, step, "normalized TRANSFORM source, target, and transformation", "normalize_processing_step")
        return step
    if operation == "AGGREGATE":
        source = normalize_plan_identifier(item.get("source"))
        target = normalize_plan_reference(item.get("target"), context, role="target")
        function = str(item.get("function") or item.get("aggregate") or "SUM").strip().upper()
        group_by = normalize_plan_reference_list(item.get("group_by") or item.get("grouping_keys"), context)
        sources = normalize_plan_reference_list(item.get("sources") or item.get("source_fields"), context)
        if not source or not target or not function:
            record_processing_step_rejection(context.get("diagnostics"), path, item, "AGGREGATE step is missing source, target, or function", "normalize_processing_step")
            return None
        step.update({"source": source, "target": target, "function": function, "group_by": group_by, "sources": sources})
        record_processing_step_modification(context.get("diagnostics"), path, item, step, "normalized AGGREGATE source, target, function, grouping, and source references", "normalize_processing_step")
        return step
    if operation == "COUNT":
        source = normalize_plan_identifier(item.get("source"))
        target = normalize_plan_reference(item.get("target"), context, role="target")
        group_by = normalize_plan_reference_list(item.get("group_by") or item.get("grouping_keys"), context)
        distinct = normalize_plan_reference(item.get("distinct") or item.get("distinct_by"), context, role="source") if item.get("distinct") or item.get("distinct_by") else None
        if not source or not target:
            record_processing_step_rejection(context.get("diagnostics"), path, item, "COUNT step is missing source or target", "normalize_processing_step")
            return None
        step.update({"source": source, "target": target, "group_by": group_by, "distinct": distinct})
        record_processing_step_modification(context.get("diagnostics"), path, item, step, "normalized COUNT source, target, grouping, and distinct reference", "normalize_processing_step")
        return step
    if operation in {"AVERAGE", "PERCENTAGE"}:
        numerator = normalize_plan_reference(item.get("numerator"), context, role="source")
        denominator = normalize_plan_reference(item.get("denominator"), context, role="source")
        target = normalize_plan_reference(item.get("target"), context, role="target")
        group_by = normalize_plan_reference_list(item.get("group_by") or item.get("grouping_keys"), context)
        if not numerator or not denominator or not target:
            record_processing_step_rejection(context.get("diagnostics"), path, item, f"{operation} step is missing numerator, denominator, or target", "normalize_processing_step")
            return None
        step.update({"numerator": numerator, "denominator": denominator, "target": target, "group_by": group_by})
        record_processing_step_modification(context.get("diagnostics"), path, item, step, f"normalized {operation} numerator, denominator, target, and grouping", "normalize_processing_step")
        return step
    if operation in {"CALL_FUNCTION", "CALL_METHOD", "CALL_STATIC_METHOD"}:
        name = normalize_callable_step_name(item, operation=operation, context=context)
        if not name:
            record_processing_step_rejection(context.get("diagnostics"), path, item, f"{operation} step is missing callable identity", "normalize_processing_step")
            return None
        apply_callable_step_identity(step, item, operation, name, context)
        mappings = normalize_callable_mappings(item, name, context)
        step["input_parameters"] = mappings["input_parameters"]
        step["output_parameters"] = mappings["output_parameters"]
        returned_value_key, returned_value = normalize_callable_returned_value(item, context)
        if returned_value:
            step[returned_value_key] = returned_value
        record_processing_step_modification(context.get("diagnostics"), path, item, step, f"normalized {operation} name and parameter mappings", "normalize_processing_step")
        return step
    if operation == "IF":
        condition = normalize_if_condition(item, context)
        if not condition:
            record_processing_step_rejection(context.get("diagnostics"), path, item, "IF step is missing a valid condition", "normalize_processing_step")
            return None
        step.update(condition)
        raw_then = item.get("then") or item.get("then_steps") or item.get("steps") or item.get("children") or []
        raw_else = item.get("else") or item.get("else_steps") or []
        step["then"] = normalize_processing_steps(raw_then, context, path=list(path or []) + ["then"])
        step["else"] = normalize_processing_steps(raw_else, context, path=list(path or []) + ["else"])
        record_empty_normalized_branch(context.get("diagnostics"), list(path or []) + ["then"], raw_then, step["then"], "IF then branch")
        record_empty_normalized_branch(context.get("diagnostics"), list(path or []) + ["else"], raw_else, step["else"], "IF else branch")
        record_processing_step_modification(context.get("diagnostics"), path, item, step, "normalized IF condition and child branches", "normalize_processing_step")
        return step
    for key in ("source", "target", "condition", "into"):
        value = item.get(key)
        if value:
            step[key] = normalize_plan_reference(value, context) if key in {"source", "target", "into"} else str(value).strip()
    record_processing_step_modification(context.get("diagnostics"), path, item, step, f"normalized {operation} scalar fields", "normalize_processing_step")
    return step


def normalize_callable_step_name(item, operation=None, context=None):
    operation = str(operation or (item or {}).get("operation") or "").strip().upper()
    raw = str((item or {}).get("name") or (item or {}).get("callable") or "").strip()
    class_name = str((item or {}).get("class") or (item or {}).get("class_name") or "").strip()
    method_name = str((item or {}).get("method") or (item or {}).get("method_name") or "").strip()
    object_name = str((item or {}).get("object") or (item or {}).get("object_name") or "").strip()
    if operation == "CALL_STATIC_METHOD" and class_name and method_name:
        raw = f"{class_name}=>{method_name}"
    elif operation == "CALL_METHOD" and object_name and method_name:
        raw = resolve_instance_method_callable_identity(object_name, method_name, context) or raw
    elif class_name and method_name:
        raw = f"{class_name}=>{method_name}"
    elif method_name and not raw:
        raw = method_name
    name = raw.upper().replace("~", "=>").replace("->", "=>")
    return name if re.fullmatch(r"[A-Z][A-Z0-9_]{1,29}(?:(?:=>)[A-Z][A-Z0-9_]{1,29})?", name) else ""


def normalize_callable_returned_value(item, context=None):
    for key in ("receiving_parameter", "returning_parameter", "receiving", "returned_value", "return_value", "result"):
        value = normalize_plan_reference((item or {}).get(key), context, role="target")
        if value:
            return ("returning_parameter" if key == "returning_parameter" else "receiving_parameter"), value
    return "receiving_parameter", ""


def apply_callable_step_identity(step, item, operation, callable_name, context):
    if operation == "CALL_FUNCTION":
        step["name"] = callable_name
        return
    class_name = str((item or {}).get("class") or (item or {}).get("class_name") or "").strip()
    method_name = str((item or {}).get("method") or (item or {}).get("method_name") or "").strip()
    object_name = str((item or {}).get("object") or (item or {}).get("object_name") or "").strip()
    if operation == "CALL_STATIC_METHOD":
        if not class_name and "=>" in callable_name:
            class_name, method_name = callable_name.split("=>", 1)
        step["class"] = normalize_abap_class_identifier(class_name)
        step["method"] = normalize_abap_method_identifier(method_name)
        step["name"] = callable_name
        return
    if not method_name and "=>" in callable_name:
        _class_name, method_name = callable_name.split("=>", 1)
    step["object"] = normalize_plan_identifier(object_name)
    step["method"] = normalize_abap_method_identifier(method_name)
    step["name"] = callable_name


def resolve_instance_method_callable_identity(object_name, method_name, context=None):
    method = normalize_abap_method_identifier(method_name)
    if not method:
        return ""
    suffix = f"=>{method}"
    candidates = [
        identity
        for identity in sorted((context or {}).get("callable_identities") or [])
        if str(identity or "").upper().endswith(suffix)
    ]
    return candidates[0] if len(candidates) == 1 else ""


def normalize_abap_class_identifier(value):
    text = str(value or "").strip().upper().replace("~", "=>").replace("->", "=>")
    return text if re.fullmatch(r"[A-Z][A-Z0-9_]{1,29}", text) else ""


def normalize_abap_method_identifier(value):
    text = str(value or "").strip().upper()
    return text if re.fullmatch(r"[A-Z][A-Z0-9_]{1,29}", text) else ""


def canonical_processing_step_input(item):
    if not isinstance(item, dict) or item.get("operation"):
        return item
    operation_keys = [
        str(key or "").strip().upper()
        for key in item
        if str(key or "").strip().upper() in SUPPORTED_PROCESSING_PLAN_OPERATIONS
    ]
    if len(operation_keys) != 1 or len(item) != 1:
        return item
    operation = operation_keys[0]
    nested = item.get(next(key for key in item if str(key or "").strip().upper() == operation))
    canonical = dict(nested) if isinstance(nested, dict) else {}
    canonical["operation"] = operation
    return canonical


def renumber_processing_steps(steps, start=1):
    renumber_processing_steps_from(steps, start)
    return steps


def prune_unused_read_steps(steps, diagnostics=None, path=None):
    pruned = []
    for index, step in enumerate(steps or []):
        item = dict(step)
        for key in ("steps", "then", "else"):
            if isinstance(item.get(key), list):
                raw_children = item.get(key)
                item[key] = prune_unused_read_steps(item.get(key), diagnostics, path=list(path or []) + [index, key])
                record_empty_normalized_branch(diagnostics, list(path or []) + [index, key], raw_children, item[key], f"{item.get('operation')} {key} branch", function_name="prune_unused_read_steps")
        if item.get("operation") == "READ":
            into = normalize_plan_identifier(item.get("into"))
            later_text = processing_plan_text_blob({"processing_steps": steps[index + 1 :]})
            if into and not re.search(rf"\b{re.escape(into)}\b", later_text, re.IGNORECASE):
                record_processing_step_rejection(diagnostics, list(path or []) + [index], step, f"READ result work area {into} is not referenced by any later step in the same branch", "prune_unused_read_steps")
                continue
        pruned.append(item)
    return pruned


def renumber_processing_steps_from(steps, start=1):
    number = start
    for step in steps or []:
        step["step"] = number
        number += 1
        for key in ("steps", "then", "else"):
            children = step.get(key)
            if isinstance(children, list):
                number = renumber_processing_steps_from(children, number)
    return number


def work_area_for_table(table_name, context):
    name = normalize_plan_identifier(table_name)
    if not name:
        return ""
    mapped = (context or {}).get("table_to_work_area", {}).get(name)
    if mapped:
        return mapped
    if name.startswith("t_") and len(name) > 2:
        return "st_" + name[2:]
    return ""


def normalize_read_conditions(item, context):
    candidates = []
    if isinstance(item.get("conditions"), list):
        candidates.extend(item.get("conditions"))
    elif isinstance(item.get("conditions"), dict):
        candidates.append(item.get("conditions"))
    elif isinstance(item.get("condition"), dict):
        candidates.append(item.get("condition"))
    elif isinstance(item.get("match"), dict):
        candidates.append(item.get("match"))
    for key in ("match", "condition"):
        value = item.get(key)
        if isinstance(value, str):
            candidates.extend(parse_read_condition_string(value))
    normalized = []
    for candidate in candidates:
        condition = normalize_read_condition(candidate, context)
        if condition:
            normalized.append(condition)
    return normalized


def normalize_read_lookup_conditions(source, into, conditions, context):
    qualified = [
        normalize_read_lookup_condition(source, into, condition, context)
        for condition in conditions or []
    ]
    return [
        condition
        for condition in qualified
        if is_read_lookup_condition(source, into, condition, context)
    ]


def normalize_read_lookup_condition(source, into, condition, context):
    if not isinstance(condition, dict):
        return condition
    normalized = dict(condition)
    for side, other_side in (("left", "right"), ("right", "left")):
        value = normalized.get(side)
        other = normalized.get(other_side)
        if read_condition_side_is_bare_lookup_field(value, context) and not read_condition_side_is_sql_filter_value(other, context):
            normalized[side] = f"{normalize_plan_identifier(into)}-{normalize_plan_identifier(value)}"
    return normalized


def is_read_lookup_condition(source, into, condition, context):
    if not isinstance(condition, dict):
        return False
    operator = str(condition.get("operator") or "").strip().upper()
    if operator != "=":
        return False
    left = condition.get("left")
    right = condition.get("right")
    if not left or not right:
        return False
    if read_condition_side_is_sql_filter_value(left, context) or read_condition_side_is_sql_filter_value(right, context):
        return False
    return (
        read_condition_side_targets_read_row(left, source, into)
        or read_condition_side_targets_read_row(right, source, into)
    )


def read_condition_side_targets_read_row(value, source, into):
    prefix = plan_reference_prefix(value)
    return bool(prefix and prefix in {normalize_plan_identifier(source), normalize_plan_identifier(into)})


def read_condition_side_is_bare_lookup_field(value, context):
    identifier = normalize_plan_identifier(value)
    if not identifier:
        return False
    if identifier in (context or {}).get("selection_parameters", set()):
        return False
    return not identifier.startswith(GLOBAL_STYLE_PREFIXES)


def read_condition_side_is_sql_filter_value(value, context):
    text = str(value or "").strip()
    if not text:
        return True
    if is_plan_literal(text):
        return True
    identifier = normalize_plan_identifier(text)
    if identifier:
        if identifier in (context or {}).get("selection_parameters", set()):
            return True
        if not identifier.startswith(GLOBAL_STYLE_PREFIXES):
            return True
    return False


def plan_reference_prefix(value):
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{0,29})[-.]([A-Za-z][A-Za-z0-9_]{0,29})", str(value or "").strip())
    return match.group(1).lower() if match else ""


def parse_read_condition_string(value):
    text = str(value or "").strip()
    if not text or is_placeholder_condition(text):
        return []
    parts = re.split(r"\s+\bAND\b\s+|&&", text, flags=re.IGNORECASE)
    conditions = []
    for part in parts:
        match = re.match(r"(.+?)\s*(=|<>|NE|EQ)\s*(.+)", part.strip(), re.IGNORECASE)
        if match:
            conditions.append({"left": match.group(1).strip(), "operator": match.group(2).strip(), "right": match.group(3).strip()})
    return conditions


def normalize_read_condition(condition, context):
    if not isinstance(condition, dict):
        return None
    left = normalize_plan_reference(condition.get("left") or condition.get("source"), context)
    operator = str(condition.get("operator") or "=").strip().upper()
    if operator == "EQ":
        operator = "="
    if operator == "NE":
        operator = "<>"
    if operator in {"IS INITIAL", "IS NOT INITIAL"}:
        if not left or is_placeholder_condition(f"{left} {operator}"):
            return None
        return {"left": left, "operator": operator}
    if operator == "CONTAINS ERROR":
        if not left or is_placeholder_condition(f"{left} {operator}"):
            return None
        return {"left": left, "operator": operator}
    right = normalize_plan_reference(condition.get("right") or condition.get("target"), context)
    if not right and operator in {"<>", "="}:
        unary_operator = "IS NOT INITIAL" if operator == "<>" else "IS INITIAL"
        if not left or is_placeholder_condition(f"{left} {unary_operator}"):
            return None
        return {"left": left, "operator": unary_operator}
    if not left or not right or is_placeholder_condition(f"{left} {operator} {right}"):
        return None
    return {"left": left, "operator": operator, "right": right}


def normalize_if_condition(item, context):
    if isinstance(item.get("conditions"), list):
        conditions = []
        for condition in item.get("conditions") or []:
            normalized = normalize_read_condition(condition, context)
            if normalized:
                conditions.append(normalized)
        if conditions:
            return {"conditions": conditions}
    if isinstance(item.get("conditions"), dict):
        normalized = normalize_read_condition(item.get("conditions"), context)
        return {"conditions": [normalized]} if normalized else {}
    if isinstance(item.get("condition"), dict):
        normalized = normalize_read_condition(item.get("condition"), context)
        return {"conditions": [normalized]} if normalized else {}
    condition = str(item.get("condition") or "").strip()
    if condition and not is_placeholder_condition(condition):
        return {"condition": condition}
    return {}


def normalize_callable_mappings(item, callable_name, context):
    input_parameters = {}
    output_parameters = {}
    for key, value in callable_mapping_items(item):
        parameter = str(key or "").strip().upper()
        target = normalize_plan_reference(value, context)
        if not parameter or not target:
            continue
        direction = callable_parameter_direction(callable_name, parameter, context)
        if direction == "exporting":
            if is_ddic_input_work_area_reference(target, context):
                record_processing_step_rejection(context.get("diagnostics"), ["CALL_FUNCTION", callable_name, parameter], {parameter: target}, "CALL_FUNCTION exporting parameter maps into a DDIC input work area", "normalize_callable_mappings")
                continue
            output_parameters[parameter] = target
        else:
            input_parameters[parameter] = target
    return {"input_parameters": input_parameters, "output_parameters": output_parameters}


def callable_mapping_items(item):
    pairs = []
    for section in ("input_parameters", "output_parameters", "importing", "exporting", "changing", "tables"):
        value = item.get(section)
        if isinstance(value, dict):
            pairs.extend(value.items())
        elif isinstance(value, list):
            pairs.extend(callable_mapping_list_items(value))
    for key in ("parameters", "parameter_mappings"):
        value = item.get(key)
        if isinstance(value, dict):
            pairs.extend(value.items())
        elif isinstance(value, list):
            pairs.extend(callable_mapping_list_items(value))
    return pairs


def callable_mapping_list_items(value):
    pairs = []
    for entry in value or []:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("parameter")
            target = entry.get("target") or entry.get("value") or entry.get("source")
            pairs.append((name, target))
    return pairs


def callable_parameter_direction(callable_name, parameter_name, context):
    directions = (context or {}).get("callable_directions") or {}
    direction = directions.get((str(callable_name or "").upper(), str(parameter_name or "").upper()))
    if direction in {"importing", "exporting"}:
        return direction
    parameter = str(parameter_name or "").upper()
    return "exporting" if parameter.startswith(("E_", "EV_", "RETURN", "RESULT", "MESSAGE")) else "importing"


def normalize_plan_identifier(value):
    name = str(value or "").strip()
    return name.lower() if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,29}", name) else ""


def normalize_plan_reference(value, context=None, role=None):
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{0,29})[-.]([A-Za-z][A-Za-z0-9_]{0,29})", text)
    if match:
        return f"{match.group(1).lower()}-{match.group(2).lower()}"
    identifier = normalize_plan_identifier(text)
    if identifier and role == "target":
        output_fields = (context or {}).get("output_fields") or set()
        if identifier.upper() in output_fields:
            return f"w_output-{identifier}"
    return identifier or text


def normalize_plan_reference_list(value, context):
    if value is None:
        return []
    raw_values = value if isinstance(value, list) else [value]
    normalized = []
    for item in raw_values:
        reference = normalize_plan_reference(item, context, role="source")
        if reference and reference not in normalized:
            normalized.append(reference)
    return normalized


def is_plan_literal(value):
    text = str(value or "").strip()
    return bool(re.fullmatch(r"'.*'|`.*`|\d+(?:\.\d+)?", text))


def is_placeholder_condition(value):
    condition = re.sub(r"\s+", "", str(value or "").strip().lower())
    return condition in {"1=0", "0=1", "true=false", "false=true"}


def is_ddic_input_work_area_reference(value, context):
    object_prefix = str(value or "").partition("-")[0].lower()
    work_areas = set(((context or {}).get("table_to_work_area") or {}).values())
    return object_prefix in work_areas


def processing_plan_payload(processing_plan=None):
    if isinstance(processing_plan, dict):
        return processing_plan
    text = str(processing_plan or "").strip()
    if not text or text.lower() == "none":
        return {"processing_steps": []}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"processing_steps": []}
    return parsed if isinstance(parsed, dict) else {"processing_steps": []}


def processing_plan_text_blob(processing_plan=None):
    return "\n".join(processing_plan_strings(processing_plan_payload(processing_plan)))


def processing_plan_strings(value):
    strings = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                strings.append(key)
            strings.extend(processing_plan_strings(item))
    elif isinstance(value, list):
        for item in value:
            strings.extend(processing_plan_strings(item))
    elif value is not None:
        strings.append(str(value))
    return strings


def format_processing_plan_path(path):
    if not path:
        return "processing_plan"
    return "processing_plan." + ".".join(str(item) for item in path)


def record_processing_step_rejection(diagnostics, path, step, reason, function_name):
    if diagnostics is None:
        return
    code_location = f"services/orchestrator.py:{function_name}"
    diagnostics.setdefault("rejected_steps", []).append(
        {
            "path": list(path or []),
            "step": step,
            "original_step": step,
            "resulting_step": None,
            "reason": reason,
            "function": function_name,
            "code_location": code_location,
        }
    )
    diagnostics.setdefault("modified_steps", []).append(
        {
            "path": list(path or []),
            "original_step": step,
            "resulting_step": None,
            "reason": reason,
            "function": function_name,
            "code_location": code_location,
        }
    )


def record_processing_step_modification(diagnostics, path, original_step, resulting_step, reason, function_name):
    if diagnostics is None:
        return
    original = processing_plan_diagnostic_snapshot(original_step)
    resulting = processing_plan_diagnostic_snapshot(resulting_step)
    if original == resulting:
        return
    diagnostics.setdefault("modified_steps", []).append(
        {
            "path": list(path or []),
            "original_step": original,
            "resulting_step": resulting,
            "reason": reason,
            "function": function_name,
            "code_location": f"services/orchestrator.py:{function_name}",
        }
    )


def append_processing_plan_trace(diagnostics, stage, plan):
    if diagnostics is None:
        return
    diagnostics.setdefault("transformation_trace", []).append(
        {
            "stage": stage,
            "plan": processing_plan_diagnostic_snapshot(plan),
        }
    )


def processing_plan_extraction_trace(parsed_plan, normalized, validation_input=None):
    trace = [
        {
            "stage": "parse_json_response.deserialized",
            "plan": processing_plan_diagnostic_snapshot(parsed_plan),
        }
    ]
    trace.extend(((normalized or {}).get("diagnostics") or {}).get("transformation_trace") or [])
    trace.append(
        {
            "stage": "validate_processing_plan.input",
            "plan": processing_plan_diagnostic_snapshot(validation_input),
        }
    )
    return trace


def processing_plan_diagnostic_snapshot(value):
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return str(value)


def record_empty_normalized_branch(diagnostics, path, raw_children, normalized_children, branch_name, function_name="normalize_processing_step"):
    if raw_children and not normalized_children:
        record_processing_step_rejection(
            diagnostics,
            path,
            raw_children,
            f"{branch_name} contained child steps but none remained after normalization",
            function_name,
        )
