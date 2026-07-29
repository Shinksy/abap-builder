import json
import re
from time import perf_counter

from config import Config
from services.ddic_metadata_context import (
    ABAP_KEYWORDS,
    extract_relevant_ddic_names,
    extract_typed_ddic_dependencies,
)
from services.llm import generate_dependency_analysis
from services.prompts.dependency_analysis_prompt import dependency_analysis_prompt


REQUIRED_ANALYSIS_PROPERTIES = (
    "ddic_objects",
    "callables",
    "unresolved",
)


def analyze_sap_dependencies(specification_text, enabled=None, llm_analyzer=None):
    started_at = perf_counter()
    enabled = bool(getattr(Config, "SAP_DEPENDENCY_ANALYSIS_ENABLED", False) if enabled is None else enabled)
    diagnostics = {
        "enabled": enabled,
        "used_fallback": False,
        "fallback_reason": None,
        "duration_seconds": 0.0,
        "error": None,
        "prompt": dependency_analysis_prompt(),
        "input": dependency_analysis_input(dependency_analysis_prompt(), specification_text),
        "raw_response": None,
    }
    if not enabled:
        result = deterministic_dependency_fallback(specification_text)
        diagnostics.update({"used_fallback": True, "fallback_reason": "disabled"})
    else:
        try:
            raw = (llm_analyzer or generate_dependency_analysis)(diagnostics["prompt"], specification_text)
            diagnostics["raw_response"] = dependency_raw_response_text(raw)
            parsed = parse_dependency_analysis_response(raw)
            validate_dependency_analysis_json(parsed)
            result = normalize_dependency_analysis(parsed, specification_text)
        except Exception as exc:
            result = deterministic_dependency_fallback(specification_text)
            diagnostics.update(
                {
                    "used_fallback": True,
                    "fallback_reason": dependency_fallback_reason(exc),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    diagnostics["duration_seconds"] = perf_counter() - started_at
    result["_diagnostics"] = diagnostics
    return result


def dependency_raw_response_text(response):
    if isinstance(response, dict):
        value = response.get("text", response)
        if isinstance(value, str):
            return value
        return json.dumps(value, indent=2, sort_keys=True)
    return str(response or "")


def dependency_analysis_input(prompt_text, specification_text):
    return [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": specification_text or ""},
    ]


def dependency_fallback_reason(exc):
    if isinstance(exc, json.JSONDecodeError):
        return "invalid JSON"
    if isinstance(exc, ValueError) and "empty" in str(exc).lower():
        return "empty response"
    if isinstance(exc, ValueError) and "dependency analysis JSON" in str(exc):
        return "invalid dependency analysis JSON"
    return "LLM call failed"


def parse_dependency_analysis_response(response):
    if isinstance(response, dict):
        response = response.get("text", response)
    if isinstance(response, dict):
        return response
    text = str(response or "").strip()
    if not text:
        raise ValueError("dependency analysis response is empty")
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    return json.loads((fenced.group(1) if fenced else text).strip())


def validate_dependency_analysis_json(raw):
    if not isinstance(raw, dict):
        raise ValueError("dependency analysis JSON must be an object")
    missing = [key for key in REQUIRED_ANALYSIS_PROPERTIES if key not in raw]
    if missing:
        raise ValueError(f"dependency analysis JSON missing required properties: {', '.join(missing)}")
    for key in REQUIRED_ANALYSIS_PROPERTIES:
        if not isinstance(raw.get(key), list):
            raise ValueError(f"dependency analysis JSON property {key} must be an array")
    dependency_ddic_catalog(raw.get("ddic_objects", []))
    validate_unresolved_items(raw.get("unresolved", []))


def dependency_ddic_catalog(entries):
    objects = set()
    fields_by_object = {}
    for entry in entries:
        validate_ddic_object_entry(entry)
        name = raw_identifier(entry, key="name")
        objects.add(name)
        fields_by_object[name] = set()
    return objects, fields_by_object


def validate_ddic_object_entry(entry):
    if not isinstance(entry, dict):
        raise ValueError("dependency analysis JSON ddic_objects entries must be objects")
    name = raw_identifier(entry, key="name")
    if not name:
        raise ValueError("dependency analysis JSON has an unknown DDIC object reference")
    expected = ddic_dependency_object(name)
    for key in ("structure", "table"):
        if entry.get(key) != expected[key]:
            raise ValueError(f"dependency analysis JSON ddic_objects {key} must be {expected[key]}")


def validate_unresolved_items(entries):
    for entry in entries:
        if isinstance(entry, str):
            if not entry.strip():
                raise ValueError("dependency analysis JSON unresolved item is empty")
        elif isinstance(entry, dict):
            if not entry.get("name") or not entry.get("reason"):
                raise ValueError("dependency analysis JSON unresolved item is missing required properties")
        else:
            raise ValueError("dependency analysis JSON unresolved items must be strings or objects")


def normalize_dependency_analysis(raw, specification_text=""):
    raw = raw if isinstance(raw, dict) else {}
    typed_ddic = extract_typed_ddic_dependencies(specification_text)
    ddic_evidence = ddic_object_evidence_names(typed_ddic)
    ddic, rejected_ddic = normalize_ddic_objects(raw.get("ddic_objects", []), allowed_names=ddic_evidence if specification_text else None)
    callables = normalize_identifiers(raw.get("callables", []), key="name")
    callables.extend(normalize_identifiers(raw.get("function_modules", []), key="name"))
    for group in list(raw.get("classes", []) or []) + list(raw.get("interfaces", []) or []):
        if isinstance(group, dict):
            class_name = normalize_identifier(group.get("name"))
            methods = normalize_identifiers(group.get("methods", []))
            callables.extend(f"{class_name}=>{method}" for method in methods if class_name)
    ddic = dedupe(ddic)
    callables = dedupe(callables)
    ddic_types = dedupe(normalize_identifiers(raw.get("ddic_types", [])) + ddic_type_dependency_names(typed_ddic))
    return {
        "ddic_objects": ddic_dependency_objects(ddic),
        "ddic_types": ddic_types,
        "callables": callables,
        "unresolved": normalize_unresolved_items(raw.get("unresolved", raw.get("unresolved_dependencies", [])), ddic, callables),
        "rejected_analysis_entries": dedupe_rejections(rejected_ddic),
        "dependencies": typed_dependency_records(typed_ddic, callables),
    }


def normalize_ddic_objects(entries, allowed_names=None):
    accepted = []
    rejected = []
    allowed_names = set(allowed_names) if allowed_names is not None else None
    for entry in list(entries or []):
        name = raw_identifier(entry, key="name")
        if not name:
            continue
        if not is_ddic_candidate_syntax(name):
            rejected.append(rejected_entry(name, "ddic_objects", "invalid DDIC table/structure/view identifier"))
        elif name == "SY" or name.startswith("SY_"):
            rejected.append(rejected_entry(name, "ddic_objects", "system field"))
        elif name in ABAP_KEYWORDS:
            rejected.append(rejected_entry(name, "ddic_objects", "ABAP keyword"))
        elif allowed_names is not None and name not in allowed_names:
            rejected.append(rejected_entry(name, "ddic_objects", "no explicit DDIC table, structure, or field evidence in specification"))
        else:
            accepted.append(name)
    return dedupe(accepted), rejected


def is_ddic_candidate_syntax(name):
    return bool(re.fullmatch(r"(?:/[A-Z0-9_]+/[A-Z0-9_]+|[A-Z][A-Z0-9_]{1,29})", str(name or "").upper()))


def rejected_entry(name, category, reason):
    return {"name": name, "category": category, "reason": reason}


def dedupe_rejections(entries):
    seen = set()
    result = []
    for entry in entries or []:
        key = (entry.get("name"), entry.get("category"), entry.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def deterministic_dependency_fallback(specification_text):
    typed_ddic = extract_typed_ddic_dependencies(specification_text)
    return {
        "ddic_objects": ddic_dependency_objects(extract_relevant_ddic_names(specification_text)),
        "ddic_types": ddic_type_dependency_names(typed_ddic),
        "callables": [],
        "unresolved": [],
        "dependencies": typed_dependency_records(typed_ddic, []),
    }


def ddic_dependency_objects(names):
    return [ddic_dependency_object(name) for name in dedupe(names)]


def ddic_dependency_object(name):
    normalized = raw_identifier(name)
    lower_name = normalized.lower()
    return {
        "name": normalized,
        "structure": f"st_{lower_name}",
        "table": f"t_{lower_name}",
    }


def normalize_unresolved_items(values, ddic_objects=None, callables=None):
    classified = set(ddic_objects or []) | set(callables or [])
    return [item for item in normalize_identifiers(values) if item not in classified]


def ddic_object_evidence_names(typed_dependencies):
    names = []
    for dependency in typed_dependencies or []:
        kind = dependency.get("kind")
        if kind in {"ddic_table", "ddic_structure"}:
            names.append(dependency.get("name"))
        elif kind == "ddic_field":
            names.append(dependency.get("object"))
    return dedupe([name for name in names if name])


def ddic_type_dependency_names(typed_dependencies):
    return dedupe(
        [
            dependency.get("name")
            for dependency in typed_dependencies or []
            if dependency.get("kind") == "ddic_type" and dependency.get("name")
        ]
    )


def typed_dependency_records(typed_ddic_dependencies, callables):
    records = [dict(dependency) for dependency in typed_ddic_dependencies or []]
    for callable_name in callables or []:
        kind = "static_method" if "=>" in callable_name else "function_module"
        records.append({"kind": kind, "name": callable_name})
    return records


def normalize_identifiers(values, key=None):
    if isinstance(values, (str, dict)):
        values = [values]
    normalized = []
    for value in values or []:
        if isinstance(value, dict):
            value = value.get(key or "name")
        item = normalize_identifier(value)
        if item:
            normalized.append(item)
    return dedupe(normalized)


def normalize_identifier(value):
    text = raw_identifier(value)
    text = text.replace("->", "=>")
    return "" if text in ABAP_KEYWORDS else text


def raw_identifier(value, key=None):
    if isinstance(value, dict):
        value = value.get(key or "name")
    text = str(value or "").strip().upper().strip("`'\".,:;()[]{}")
    if not re.fullmatch(r"(?:/[A-Z0-9_]+/[A-Z0-9_]+|[A-Z][A-Z0-9_]{1,29})(?:(?:=>|->)[A-Z][A-Z0-9_]{1,29})?", text):
        return ""
    return text


def dedupe(values):
    seen = set()
    result = []
    for value in values or []:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
