import json
import re
from copy import deepcopy

from services.callable_generator import (
    callable_call_statements,
    callable_parameters,
    class_method_call_statements,
    function_module_call_statements,
    returning_parameter_name,
)
from services.callable_signature_provider import normalize_callable_identity, normalize_provider_signatures
from services.declaration_generator import data_declarations, type_declarations
from services.ddic_metadata_context import normalized_fields, normalized_tables
from services.selection_screen_generator import selection_screen_declarations


class GenerationContractError(Exception):
    def __init__(self, diagnostics):
        errors = (diagnostics or {}).get("validation_errors") or []
        super().__init__("Generation contract validation failed: " + "; ".join(errors))
        self.diagnostics = diagnostics


CALLABLE_INPUT_DIRECTIONS = {"IMPORTING", "CHANGING", "TABLES"}
CALLABLE_OUTPUT_DIRECTIONS = {"EXPORTING", "CHANGING", "TABLES", "RETURNING"}


def build_structured_generation_contract(
    declaration_requirements=None,
    processing_plan=None,
    ddic_metadata=None,
    callable_metadata=None,
    program_name=None,
):
    requirements = generation_safe_requirements(
        normalize_requirements_payload(declaration_requirements),
        ddic_metadata=ddic_metadata,
    )
    plan = processing_plan_payload(processing_plan)
    contract = {
        "program_name": normalize_program_name(program_name or requirements.get("report_name")),
        "selection_screen": selection_screen_contract(requirements),
        "ddic_backed_types": ddic_backed_types_contract(requirements, plan),
        "internal_tables": internal_table_contracts(requirements, plan),
        "work_areas": work_area_contracts(requirements, plan),
        "scalar_variables": scalar_variable_contracts(requirements, plan),
        "database_reads": database_read_contracts(plan),
        "processing_steps": deepcopy(plan.get("processing_steps") or []),
        "form_routines": form_routine_contracts(plan),
        "perform_calls": perform_call_contracts(plan),
        "function_module_calls": function_module_call_contracts(plan),
        "class_method_calls": class_method_call_contracts(plan),
        "output_structures": output_structure_contracts(requirements),
        "output_processing": output_processing_contract(plan),
    }
    enrich_callable_variable_declarations(contract, callable_metadata)
    canonicalize_contract_ddic_references(contract, ddic_metadata)
    contract["validation"] = validate_generation_contract(contract, ddic_metadata, callable_metadata)
    contract["deterministic_abap"] = deterministic_abap_components(contract, ddic_metadata, callable_metadata)
    return contract


def assert_generation_contract_valid(contract):
    validation = (contract or {}).get("validation") or validate_generation_contract(contract)
    if not validation.get("valid"):
        raise GenerationContractError({"validation_errors": validation.get("errors") or []})


def validate_selection_screen_generation_contract(contract, ddic_metadata=None):
    errors = []
    metadata = normalized_tables(ddic_metadata)
    seen_names = set()
    for item in (contract or {}).get("selection_screen") or []:
        name = item.get("name")
        if name:
            key = name.lower()
            if key in seen_names:
                errors.append(f"duplicate declaration rejected: {name}")
            seen_names.add(key)
        for reference in selection_screen_ddic_references(item):
            object_name = reference.get("object")
            field_name = reference.get("field")
            if not object_name or not metadata:
                continue
            if object_name not in metadata:
                errors.append(f"DDIC reference {object_name} is unresolved")
                continue
            if field_name and field_name not in normalized_fields(metadata[object_name]):
                errors.append(f"DDIC reference {object_name}-{field_name} is unresolved")
    return {"valid": not errors, "errors": dedupe(errors)}


def canonicalize_contract_ddic_references(contract, ddic_metadata=None):
    metadata = normalized_tables(ddic_metadata)
    if not metadata:
        return contract
    for item in (contract or {}).get("selection_screen") or []:
        if item.get("for_field"):
            item["for_field"] = canonical_ddic_reference_text(item["for_field"], metadata)
        if item.get("type_or_like"):
            item["type_or_like"] = canonical_type_reference_text(item["type_or_like"], metadata)
    for item in (contract or {}).get("ddic_backed_types") or []:
        object_name = str(item.get("object") or "").upper()
        field_name = str(item.get("field") or "").upper()
        resolved = resolve_ddic_field_name(object_name, field_name, metadata)
        if resolved:
            item["field"] = resolved
    for read in (contract or {}).get("database_reads") or []:
        table = str(read.get("table") or "").upper()
        read["fields"] = [resolve_ddic_field_name(table, field, metadata) or field for field in read.get("fields") or []]
        for condition in read.get("where") or []:
            if condition.get("field"):
                condition["field"] = resolve_ddic_field_name(table, condition.get("field"), metadata) or condition.get("field")
    for output in (contract or {}).get("output_structures") or []:
        for field in output.get("fields") or []:
            if field.get("type_or_like"):
                field["type_or_like"] = canonical_type_reference_text(field["type_or_like"], metadata)
    for item in (contract or {}).get("scalar_variables") or []:
        if item.get("type_or_like"):
            item["type_or_like"] = canonical_type_reference_text(item["type_or_like"], metadata)
    return contract


def canonical_ddic_reference_text(value, metadata):
    ref = ddic_reference(value)
    resolved = resolve_ddic_field_name(ref.get("object"), ref.get("field"), metadata)
    return f"{ref['object']}-{resolved}" if ref.get("object") and resolved else value


def canonical_type_reference_text(value, metadata):
    ref = first_ddic_reference(value)
    if not ref:
        return value
    resolved = resolve_ddic_field_name(ref.get("object"), ref.get("field"), metadata)
    if not resolved:
        return value
    return re.sub(
        rf"\b{re.escape(ref['object'])}-{re.escape(ref['field'])}\b",
        f"{ref['object']}-{resolved}",
        str(value),
        count=1,
        flags=re.IGNORECASE,
    )


def resolve_ddic_field_name(object_name, field_name, metadata):
    object_name = str(object_name or "").upper()
    field_name = str(field_name or "").upper()
    table = metadata.get(object_name)
    if not field_name or not table:
        return field_name
    fields = normalized_fields(table)
    if field_name in fields:
        return field_name
    candidates = [
        name
        for name, details in fields.items()
        if field_name in ddic_field_aliases(name, details)
    ]
    return candidates[0] if len(candidates) == 1 else ""


def ddic_field_aliases(name, details):
    aliases = {str(name or "").upper()}
    if isinstance(details, dict):
        for key in ("description", "label", "medium_label", "long_label", "short_label", "scrtext_s", "scrtext_m", "scrtext_l", "rollname"):
            aliases.add(normalize_ddic_alias_text(details.get(key)))
    return {alias for alias in aliases if alias}


def normalize_ddic_alias_text(value):
    return re.sub(r"[^A-Z0-9]+", "_", str(value or "").upper()).strip("_")


def deterministic_abap_components(contract, ddic_metadata=None, callable_metadata=None):
    return {
        "selection_screen": selection_screen_declarations(contract),
        "types": type_declarations(contract, ddic_metadata),
        "data": data_declarations(contract),
        "form_headers": form_headers(contract),
        "perform_calls": perform_call_statements(contract),
        "function_module_calls": function_module_call_statements(contract, callable_metadata),
        "class_method_calls": class_method_call_statements(contract, callable_metadata),
        "database_reads": database_read_statements(contract),
        "main_processing_flow": main_processing_flow(contract),
        "final_program": assemble_deterministic_program(contract, ddic_metadata, callable_metadata),
    }


def assemble_deterministic_program(contract, ddic_metadata=None, callable_metadata=None):
    parts = []
    program_name = (contract or {}).get("program_name")
    if program_name:
        parts.append(f"REPORT {program_name}.")
    for key in ("selection_screen", "types", "data", "main_processing_flow"):
        text = deterministic_abap_components_without_final(contract, ddic_metadata, callable_metadata).get(key, "")
        if text:
            parts.append(text)
    forms = form_headers(contract)
    if forms:
        parts.append(forms)
    return "\n\n".join(parts).strip()


def deterministic_abap_components_without_final(contract, ddic_metadata=None, callable_metadata=None):
    return {
        "selection_screen": selection_screen_declarations(contract),
        "types": type_declarations(contract, ddic_metadata),
        "data": data_declarations(contract),
        "main_processing_flow": main_processing_flow(contract),
    }


def validate_generation_contract(contract, ddic_metadata=None, callable_metadata=None):
    errors = []
    metadata = normalized_tables(ddic_metadata)
    signatures = {normalize_callable_identity(name): value for name, value in normalize_provider_signatures(callable_metadata).items()}
    callable_ddic_objects = callable_metadata_ddic_objects(signatures)
    declarations = declared_type_index(contract)
    seen_declarations = set()

    for declaration in declaration_names(contract):
        key = declaration.lower()
        if key in seen_declarations:
            errors.append(f"duplicate declaration rejected: {declaration}")
        seen_declarations.add(key)

    for reference in ddic_references(contract):
        object_name = reference.get("object")
        field_name = reference.get("field")
        if not metadata:
            if object_name in callable_ddic_objects:
                continue
            errors.append(f"DDIC reference {object_name}{('-' + field_name) if field_name else ''} is unresolved")
            continue
        if object_name not in metadata:
            if object_name in callable_ddic_objects:
                continue
            errors.append(f"DDIC reference {object_name} is unresolved")
            continue
        if field_name and field_name not in normalized_fields(metadata[object_name]):
            errors.append(f"DDIC reference {object_name}-{field_name} is unresolved")

    form_definitions = {normalize_identifier(item.get("name")).lower() for item in (contract or {}).get("form_routines") or []}
    form_calls = [normalize_identifier(item.get("name")).lower() for item in (contract or {}).get("perform_calls") or []]
    for form_name in form_calls:
        if form_name and form_name not in form_definitions:
            errors.append(f"FORM call {form_name} has no corresponding FORM definition")
    for form_name in form_definitions:
        if list(form_calls).count(form_name) > 1:
            continue
    duplicate_forms = duplicates(form_definitions_from_contract(contract))
    for form_name in duplicate_forms:
        errors.append(f"duplicate FORM definition rejected: {form_name}")

    for call in callable_calls(contract):
        identity = normalize_callable_identity(call.get("name"))
        signature = signatures.get(identity)
        if not signature:
            errors.append(f"callable signature {identity} is unresolved")
            continue
        parameters = callable_parameters(signature)
        for mapping in call.get("parameters") or []:
            parameter_name = str(mapping.get("parameter") or "").upper()
            if mapping.get("returning") and not parameter_name:
                parameter_name = returning_parameter_name(signature)
            parameter = parameters.get(parameter_name)
            if not parameter:
                errors.append(f"callable parameter {identity}.{parameter_name} does not exist")
                continue
            expected_direction = str(parameter.get("direction") or "").upper()
            requested_direction = str(mapping.get("direction") or "").lower()
            allowed_directions = callable_contract_mapping_directions(requested_direction)
            if allowed_directions and expected_direction not in allowed_directions:
                errors.append(
                    f"callable parameter {identity}.{parameter_name} is mapped as {requested_direction} "
                    f"but metadata direction is {expected_direction}"
                )
            actual = variable_type_lookup_key(mapping.get("variable"))
            if actual and not callable_variable_type_compatible(declarations.get(actual.lower()), parameter, metadata):
                expected = callable_parameter_type_reference(parameter)
                found = declarations.get(actual.lower(), "")
                errors.append(f"callable variable {actual} is not type-compatible with {identity}.{parameter_name}: expected {expected}, found {found or 'unresolved'}")

    return {"valid": not errors, "errors": dedupe(errors)}


def enrich_callable_variable_declarations(contract, callable_metadata=None):
    signatures = {
        normalize_callable_identity(name): value
        for name, value in normalize_provider_signatures(callable_metadata).items()
    }
    if not signatures:
        return contract
    declared = declared_type_index(contract)
    scalar_variables = (contract or {}).setdefault("scalar_variables", [])
    internal_tables = (contract or {}).setdefault("internal_tables", [])
    work_areas = (contract or {}).setdefault("work_areas", [])
    for call in callable_calls(contract):
        signature = signatures.get(normalize_callable_identity(call.get("name")))
        if not signature:
            continue
        if call.get("call_type") == "instance" and call.get("receiver"):
            receiver = normalize_identifier(call.get("receiver"))
            class_name = str(call.get("name") or "").split("=>", 1)[0].upper()
            if receiver and receiver not in declared and class_name:
                upsert_scalar_variable_declaration(scalar_variables, receiver, f"TYPE REF TO {class_name}")
                declared[receiver] = normalized_type_reference(f"TYPE REF TO {class_name}")
        parameters = callable_parameters(signature)
        for mapping in call.get("parameters") or []:
            variable = normalize_identifier(mapping.get("variable"))
            parameter_name = str(mapping.get("parameter") or "").upper()
            if mapping.get("returning") and not parameter_name:
                parameter_name = returning_parameter_name(signature)
            parameter = parameters.get(parameter_name)
            if not variable:
                continue
            lookup_key = variable_type_lookup_key(mapping.get("variable"))
            if should_replace_callable_mapping_variable(lookup_key, mapping.get("variable"), parameter, declared):
                variable = callable_mapping_variable_name(parameter_name, parameter)
                mapping["variable"] = variable
                lookup_key = variable
            if parameter and variable in declared and str(lookup_key or "").lower() in declared:
                mapping["signature_direction"] = str((parameter or {}).get("direction") or "").upper()
                mapping["required"] = bool((parameter or {}).get("required")) if "required" in (parameter or {}) else False
                mapping["type_or_like"] = normalize_declaration(callable_parameter_type_reference(parameter))
                mapping["parameter_kind"] = callable_parameter_contract_kind(parameter, call)
                continue
            type_reference = callable_parameter_type_reference(parameter)
            if not type_reference:
                continue
            mapping["signature_direction"] = str((parameter or {}).get("direction") or "").upper()
            mapping["required"] = bool((parameter or {}).get("required")) if "required" in (parameter or {}) else False
            mapping["type_or_like"] = normalize_declaration(type_reference)
            mapping["parameter_kind"] = callable_parameter_contract_kind(parameter, call)
            if callable_parameter_expects_table(parameter):
                add_unique_dict(internal_tables, {"name": variable, "row_type": type_reference})
                remove_scalar_variable_declaration(scalar_variables, variable)
                declared[variable] = normalized_type_reference(f"TYPE STANDARD TABLE OF {type_reference}")
            elif callable_parameter_expects_object_reference(parameter, call):
                type_name = object_reference_type_name(parameter)
                upsert_scalar_variable_declaration(scalar_variables, variable, f"TYPE REF TO {type_name}")
                declared[variable] = normalized_type_reference(f"TYPE REF TO {type_name}")
            elif callable_parameter_expects_structure(parameter):
                add_unique_dict(work_areas, {"name": variable, "type": type_reference})
                remove_scalar_variable_declaration(scalar_variables, variable)
                declared[variable] = normalized_type_reference(f"TYPE {type_reference}")
            else:
                declaration = normalize_declaration(type_reference)
                upsert_scalar_variable_declaration(scalar_variables, variable, declaration)
                declared[variable] = normalized_type_reference(declaration)
    return contract


def generation_safe_requirements(requirements, ddic_metadata=None):
    sanitized = deepcopy(requirements or {})
    metadata = normalized_tables(ddic_metadata)
    output_owned_names = contract_owned_output_declaration_names(sanitized)
    sanitized["global_variables"] = [
        generation_safe_global_variable(item, metadata)
        for item in sanitized.get("global_variables") or []
        if normalize_identifier((item or {}).get("name")) not in output_owned_names
    ]
    sanitized["output_structure_fields"] = [
        generation_safe_output_field(item, metadata)
        for item in sanitized.get("output_structure_fields") or []
    ]
    return sanitized


def contract_owned_output_declaration_names(requirements):
    names = set()
    if not (requirements or {}).get("output_structure_fields"):
        return names
    for output in output_structure_contracts(requirements):
        for key in ("name", "table", "work_area"):
            name = normalize_identifier(output.get(key))
            if name:
                names.add(name)
    return names


def generation_safe_global_variable(item, metadata):
    if not isinstance(item, dict):
        return item
    updated = dict(item)
    declaration = normalize_declaration_statement_text(updated.get("declaration") or updated.get("type_or_like"))
    reference = first_ddic_reference(declaration)
    name = normalize_identifier(updated.get("name"))
    if reference and not ddic_reference_available(reference, metadata) and name:
        updated["declaration"] = f"DATA {name} TYPE string."
    return updated


def generation_safe_output_field(item, metadata):
    if not isinstance(item, dict):
        return item
    updated = dict(item)
    name = str(updated.get("name") or "").strip().upper()
    type_or_like = normalize_declaration(updated.get("type_or_like"))
    type_or_like = canonical_type_reference_text(type_or_like, metadata)
    reference = first_ddic_reference(type_or_like)
    if reference and not ddic_reference_available(reference, metadata):
        type_or_like = ""
    updated["type_or_like"] = type_or_like or fallback_output_field_type(name)
    return updated


def ddic_reference_available(reference, metadata):
    if not reference:
        return True
    if not metadata:
        return False
    table = metadata.get(reference.get("object"))
    if not table:
        return False
    field = reference.get("field")
    return not field or field in normalized_fields(table)


def fallback_output_field_type(name):
    normalized = str(name or "").strip().upper().replace(" ", "_")
    if normalized.endswith("ROW_NUMBER") or normalized in {"ROW", "COUNT", "TOTAL"}:
        return "TYPE i"
    if normalized in {"HOURS", "AMOUNT", "QUANTITY"}:
        return "TYPE p DECIMALS 2"
    return "TYPE string"


def normalize_declaration_statement_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip().rstrip("."))


def should_replace_callable_mapping_variable(lookup_key, raw_variable, parameter, declared):
    if not parameter:
        return False
    text = str(raw_variable or "").strip()
    if "-" in text or "." in text:
        return True
    key = normalize_identifier(lookup_key)
    if key in {"w_output", "t_output"}:
        return True
    declared_type = (declared or {}).get(str(key or "").lower())
    if declared_type and callable_parameter_expects_table(parameter):
        return not callable_variable_type_compatible(declared_type, parameter)
    return False


def callable_mapping_variable_name(parameter_name, parameter):
    name = normalize_identifier(parameter_name)
    if not name:
        name = "value"
    if callable_parameter_expects_table(parameter):
        return f"t_{name}"
    if callable_parameter_expects_structure(parameter):
        return f"st_{name}"
    return f"w_{name}"


def callable_metadata_ddic_objects(signatures):
    objects = set()
    for signature in (signatures or {}).values():
        for parameter in callable_parameters(signature).values():
            type_reference = callable_parameter_type_reference(parameter)
            reference = first_ddic_reference(type_reference)
            if reference and reference.get("object"):
                objects.add(reference["object"])
                continue
            type_name = normalized_type_reference(type_reference)
            if type_name and not is_builtin_type_name(type_name) and not type_name.startswith("REF TO "):
                objects.add(type_name)
    return objects


def remove_scalar_variable_declaration(scalar_variables, name):
    normalized = normalize_identifier(name)
    scalar_variables[:] = [
        item
        for item in scalar_variables or []
        if normalize_identifier((item or {}).get("name")) != normalized
    ]


def upsert_scalar_variable_declaration(scalar_variables, name, declaration):
    for item in scalar_variables or []:
        if normalize_identifier((item or {}).get("name")) == name:
            if not item.get("type_or_like"):
                item["type_or_like"] = declaration
            return
    add_unique_dict(scalar_variables, {"name": name, "type_or_like": declaration})


def selection_screen_contract(requirements):
    items = []
    for parameter in requirements.get("parameters") or []:
        if not isinstance(parameter, dict):
            continue
        item = {"kind": "PARAMETERS", "name": normalize_identifier(parameter.get("name"))}
        for key in ("type_or_like", "default", "as_checkbox", "radiobutton_group"):
            if parameter.get(key) not in (None, ""):
                item[key] = parameter.get(key)
        items.append(item)
    for option in requirements.get("select_options") or []:
        if not isinstance(option, dict):
            continue
        field = option.get("for_field") or option.get("field")
        item = {"kind": "SELECT-OPTIONS", "name": normalize_identifier(option.get("name"))}
        if field:
            item["for_field"] = normalize_ddic_field_reference(field)
        items.append(item)
    return [item for item in items if item.get("name")]


def selection_screen_ddic_references(item):
    references = []
    if item.get("for_field"):
        references.append(ddic_reference(item["for_field"]))
    if item.get("type_or_like"):
        ref = first_ddic_reference(item["type_or_like"])
        if ref:
            references.append(ref)
    return references


def ddic_backed_types_contract(requirements, plan):
    types = []
    for field in requirements.get("output_structure_fields") or []:
        if isinstance(field, dict) and field.get("type_or_like"):
            ref = first_ddic_reference(field.get("type_or_like"))
            if ref:
                add_unique_dict(types, {"name": "ty_output", "object": ref["object"], "field": ref.get("field")})
    for read in database_read_contracts(plan):
        for field in read.get("fields") or []:
            add_unique_dict(types, {"name": read.get("row_type"), "object": read.get("table"), "field": field})
    for read in table_read_contracts(plan):
        for field in read.get("fields") or []:
            add_unique_dict(types, {"name": read.get("row_type"), "object": read.get("object"), "field": field})
    return types


def internal_table_contracts(requirements, plan):
    tables = []
    for item in requirements.get("internal_tables") or []:
        normalized = normalize_table_item(item)
        if normalized:
            add_unique_dict(tables, normalized)
    for item in requirements.get("global_variables") or []:
        global_table = global_internal_table_contract(item)
        if global_table:
            add_unique_dict(tables, global_table)
    for read in database_read_contracts(plan):
        name = normalize_identifier(read.get("target_table") or read.get("into_table"))
        if name:
            add_unique_dict(tables, {"name": name, "row_type": read.get("row_type") or read.get("table")})
    for read in table_read_contracts(plan):
        if read.get("source"):
            add_unique_dict(tables, {"name": read["source"], "row_type": read.get("row_type")})
    if requirements.get("output_structure_fields"):
        add_unique_dict(tables, {"name": "t_output", "row_type": "ty_output"})
    return tables


def work_area_contracts(requirements, plan):
    work_areas = []
    for item in requirements.get("work_areas") or []:
        normalized = normalize_table_item(item)
        if normalized:
            add_unique_dict(work_areas, normalized)
    for item in requirements.get("global_variables") or []:
        global_work_area = global_work_area_contract(item)
        if global_work_area:
            add_unique_dict(work_areas, global_work_area)
    for read in database_read_contracts(plan):
        name = normalize_identifier(read.get("work_area") or read.get("into"))
        if name:
            add_unique_dict(work_areas, {"name": name, "type": read.get("row_type") or read.get("table")})
    for read in table_read_contracts(plan):
        if read.get("work_area"):
            add_unique_dict(work_areas, {"name": read["work_area"], "type": read.get("row_type")})
    if requirements.get("output_structure_fields"):
        add_unique_dict(work_areas, {"name": "w_output", "type": "ty_output"})
    return work_areas


def scalar_variable_contracts(requirements, plan):
    scalars = []
    existing_names = {normalize_identifier(item.get("name")) for item in requirements.get("parameters") or [] if isinstance(item, dict)}
    existing_names.update(normalize_identifier(item.get("name")) for item in requirements.get("select_options") or [] if isinstance(item, dict))
    for item in requirements.get("global_variables") or []:
        if not isinstance(item, dict):
            continue
        name = normalize_identifier(item.get("name"))
        declaration = normalize_global_declaration_clause(item)
        if name and declaration and not global_declaration_owned_elsewhere(item):
            add_unique_dict(scalars, {"name": name, "type_or_like": declaration})
        if name:
            existing_names.add(name)
    for item in internal_table_contracts(requirements, plan):
        if item.get("name"):
            existing_names.add(normalize_identifier(item.get("name")))
    for item in work_area_contracts(requirements, plan):
        if item.get("name"):
            existing_names.add(normalize_identifier(item.get("name")))
    for call in function_module_call_contracts(plan) + class_method_call_contracts(plan):
        for mapping in call.get("parameters") or []:
            variable = normalize_identifier(mapping.get("variable"))
            if variable and "-" not in str(mapping.get("variable") or "") and variable not in existing_names:
                add_unique_dict(scalars, {"name": variable, "type_or_like": ""})
                existing_names.add(variable)
    return scalars


def database_read_contracts(plan):
    reads = []
    for step in all_processing_steps(plan):
        operation = str(step.get("operation") or "").upper()
        if operation not in {"SELECT", "DATABASE_READ", "READ_DATABASE"}:
            continue
        table = normalize_object_name(step.get("table") or step.get("from") or step.get("source"))
        fields = [str(field or "").upper() for field in step.get("fields") or step.get("select_fields") or [] if str(field or "").strip()]
        where = []
        for condition in step.get("where") or step.get("conditions") or []:
            if isinstance(condition, dict):
                where.append(
                    {
                        "field": str(condition.get("field") or "").upper(),
                        "operator": str(condition.get("operator") or "=").strip() or "=",
                        "value": str(condition.get("value") or condition.get("source") or "").strip(),
                    }
                )
        target_table = normalize_identifier(step.get("target_table") or step.get("into_table") or step.get("target"))
        if table and fields and target_table:
            reads.append(
                {
                    "table": table,
                    "fields": fields,
                    "where": where,
                    "target_table": target_table,
                    "work_area": normalize_identifier(step.get("work_area") or step.get("into")),
                    "row_type": normalize_identifier(step.get("row_type") or f"ty_{table.lower()}"),
                }
            )
    return reads


def table_read_contracts(plan):
    reads = []
    for step in all_processing_steps(plan):
        if str(step.get("operation") or "").upper() != "READ":
            continue
        source = normalize_identifier(step.get("source"))
        work_area = normalize_identifier(step.get("into"))
        object_name = ddic_object_from_table_or_work_area(source) or ddic_object_from_table_or_work_area(work_area)
        fields = []
        for condition in step.get("conditions") or []:
            if isinstance(condition, dict):
                collect_table_read_field_for_object(fields, condition.get("left"), object_name)
                collect_table_read_field_for_object(fields, condition.get("right"), object_name)
        if source and work_area and object_name:
            reads.append(
                {
                    "source": source,
                    "work_area": work_area,
                    "object": object_name,
                    "row_type": f"ty_{object_name.lower()}",
                    "fields": fields,
                }
            )
    for call in function_module_call_contracts(plan) + class_method_call_contracts(plan):
        for mapping in call.get("parameters") or []:
            variable = str(mapping.get("variable") or "")
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)[-.]([A-Za-z_][A-Za-z0-9_]*)", variable)
            if not match:
                continue
            object_name = ddic_object_from_table_or_work_area(match.group(1))
            if not object_name:
                continue
            reads.append(
                {
                    "source": "",
                    "work_area": normalize_identifier(match.group(1)),
                    "object": object_name,
                    "row_type": f"ty_{object_name.lower()}",
                    "fields": [match.group(2).upper()],
                }
            )
    return merge_table_read_contracts(reads)


def form_routine_contracts(plan):
    names = []
    for step in all_processing_steps(plan):
        for key in ("form", "routine"):
            if step.get(key):
                names.append(step.get(key))
        if str(step.get("operation") or "").upper() == "FORM" and step.get("name"):
            names.append(step.get("name"))
    names.extend(call.get("name") for call in perform_call_contracts(plan))
    return [{"name": normalize_identifier(name)} for name in dedupe(name for name in names if name)]


def perform_call_contracts(plan):
    calls = []
    for step in all_processing_steps(plan):
        if str(step.get("operation") or "").upper() == "PERFORM" and step.get("name"):
            calls.append({"name": normalize_identifier(step.get("name"))})
    return calls


def function_module_call_contracts(plan):
    calls = []
    for sequence, step in enumerate(all_processing_steps(plan)):
        if str(step.get("operation") or "").upper() != "CALL_FUNCTION":
            continue
        name = normalize_callable_identity(step.get("name") or step.get("callable"))
        if name:
            calls.append({"name": name, "sequence": sequence, "parameters": callable_mappings_from_step(step)})
    return calls


def class_method_call_contracts(plan):
    calls = []
    for sequence, step in enumerate(all_processing_steps(plan)):
        operation = str(step.get("operation") or "").upper()
        if operation not in {"CALL_METHOD", "CALL_STATIC_METHOD"}:
            continue
        if operation == "CALL_STATIC_METHOD":
            name = normalize_callable_identity(step.get("name") or f"{step.get('class', '')}=>{step.get('method', '')}")
            call_type = "static"
        else:
            name = normalize_callable_identity(step.get("name") or f"{step.get('class', '')}=>{step.get('method', '')}")
            call_type = "instance"
        if name:
            calls.append({"name": name, "sequence": sequence, "call_type": call_type, "receiver": normalize_identifier(step.get("object")), "parameters": callable_mappings_from_step(step)})
    return calls


def callable_mappings_from_step(step):
    mappings = []
    for key, direction in (
        ("input_parameters", "input"),
        ("output_parameters", "output"),
        ("changing_parameters", "changing"),
        ("table_parameters", "tables"),
        ("tables_parameters", "tables"),
    ):
        values = step.get(key) or {}
        for parameter, variable in parameter_value_pairs(values):
            mappings.append({"parameter": str(parameter or "").upper(), "variable": str(variable or "").strip(), "direction": direction})
    returning = step.get("returning_parameter") or step.get("receiving_parameter")
    if returning:
        mappings.append({"parameter": str(step.get("returning_name") or "").upper(), "variable": str(returning).strip(), "direction": "output", "returning": True})
    return mappings


def output_structure_contracts(requirements):
    fields = []
    for field in requirements.get("output_structure_fields") or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip().upper()
        type_or_like = normalize_declaration(field.get("type_or_like"))
        if name and type_or_like:
            item = {"name": name, "type_or_like": type_or_like}
            include_when = str(field.get("include_when") or "").strip()
            if include_when:
                item["include_when"] = include_when
            for key in ("heading", "description", "label", "column_heading", "seltext_l"):
                text = str(field.get(key) or "").strip()
                if text:
                    item["heading"] = text
                    break
            fields.append(item)
    return [{"name": "ty_output", "table": "t_output", "work_area": "w_output", "fields": fields}] if fields else []


def output_processing_contract(plan):
    steps = [step for step in all_processing_steps(plan) if str(step.get("operation") or "").upper() in {"APPEND", "WRITE", "OUTPUT", "DISPLAY_ALV", "WRITE_CSV"}]
    return {"steps": deepcopy(steps)}


def form_headers(contract):
    lines = []
    for item in (contract or {}).get("form_routines") or []:
        name = normalize_identifier(item.get("name"))
        if name:
            lines.append(f"FORM {name}.\nENDFORM.")
    return "\n\n".join(lines)


def perform_call_statements(contract):
    return "\n".join(f"PERFORM {item['name']}." for item in (contract or {}).get("perform_calls") or [] if item.get("name"))


def database_read_statements(contract):
    lines = []
    for read in (contract or {}).get("database_reads") or []:
        fields = ", ".join(field.lower() for field in read.get("fields") or [])
        lines.append(f"SELECT {fields}")
        lines.append(f"  FROM {read['table'].lower()}")
        lines.append(f"  INTO CORRESPONDING FIELDS OF TABLE {read['target_table']}")
        where = read.get("where") or []
        if where:
            conditions = [f"{item['field'].lower()} {item['operator']} {item['value']}" for item in where]
            lines.append("  WHERE " + " AND ".join(conditions))
        lines[-1] += "."
    return "\n".join(lines)


def main_processing_flow(contract):
    performs = perform_call_statements(contract)
    if not performs:
        return ""
    return "START-OF-SELECTION.\n" + "\n".join(f"  {line}" for line in performs.splitlines())


def normalize_requirements_payload(value):
    if isinstance(value, dict) and isinstance(value.get("requirements"), dict):
        value = value["requirements"]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return deepcopy(value) if isinstance(value, dict) else {}


def processing_plan_payload(value):
    if isinstance(value, dict) and isinstance(value.get("plan"), dict):
        value = value["plan"]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return deepcopy(value) if isinstance(value, dict) else {}


def all_processing_steps(plan):
    result = []
    for step in (plan or {}).get("processing_steps") or []:
        collect_step(step, result)
    return result


def collect_step(step, result):
    if not isinstance(step, dict):
        return
    result.append(step)
    for key in ("steps", "then", "else"):
        for child in step.get(key) or []:
            collect_step(child, result)


def declared_type_index(contract):
    result = {}
    for item in (contract or {}).get("selection_screen") or []:
        if item.get("kind") == "PARAMETERS":
            result[item["name"].lower()] = normalized_type_reference(item.get("type_or_like") or "TYPE string")
        elif item.get("kind") == "SELECT-OPTIONS" and item.get("for_field"):
            result[item["name"].lower()] = normalized_type_reference(f"TYPE RANGE OF {item['for_field']}")
    for item in (contract or {}).get("internal_tables") or []:
        if item.get("name") and item.get("row_type"):
            result[item["name"].lower()] = normalized_type_reference(f"TYPE STANDARD TABLE OF {item['row_type']}")
    for item in (contract or {}).get("work_areas") or []:
        row_type = item.get("type") or item.get("row_type")
        if item.get("name") and row_type:
            result[item["name"].lower()] = normalized_type_reference(f"TYPE {row_type}")
            merge_component_types(result, item["name"], row_type, contract)
    for item in (contract or {}).get("scalar_variables") or []:
        if item.get("name") and item.get("type_or_like"):
            result[item["name"].lower()] = normalized_type_reference(item["type_or_like"])
    return result


def merge_component_types(result, variable_name, row_type, contract):
    output_type = output_structure_for_type(row_type, contract)
    if output_type:
        for field in output_type.get("fields") or []:
            field_name = str(field.get("name") or "").strip().lower()
            type_reference = normalized_type_reference(field.get("type_or_like"))
            if field_name and type_reference:
                result[f"{variable_name.lower()}-{field_name}"] = type_reference
        return
    object_name = ddic_object_from_row_type(row_type)
    if not object_name:
        return
    for type_item in (contract or {}).get("ddic_backed_types") or []:
        if type_item.get("object") != object_name or not type_item.get("field"):
            continue
        result[f"{variable_name.lower()}-{str(type_item['field']).lower()}"] = normalized_type_reference(
            f"TYPE {object_name}-{type_item['field']}"
        )


def output_structure_for_type(row_type, contract):
    normalized_row_type = normalize_type_name(row_type)
    for output in (contract or {}).get("output_structures") or []:
        if normalize_type_name(output.get("name")) == normalized_row_type:
            return output
    return None


def declaration_names(contract):
    names = []
    names.extend(item.get("name") for item in (contract or {}).get("selection_screen") or [])
    names.extend(item.get("name") for item in (contract or {}).get("internal_tables") or [])
    names.extend(item.get("name") for item in (contract or {}).get("work_areas") or [])
    names.extend(item.get("name") for item in (contract or {}).get("scalar_variables") or [])
    names.extend(output.get("name") for output in (contract or {}).get("output_structures") or [])
    names.extend(read.get("row_type") for read in (contract or {}).get("database_reads") or [])
    return [name for name in names if name]


def ddic_references(contract):
    references = []
    for item in (contract or {}).get("selection_screen") or []:
        if item.get("for_field"):
            references.append(ddic_reference(item["for_field"]))
        if item.get("type_or_like"):
            ref = first_ddic_reference(item["type_or_like"])
            if ref:
                references.append(ref)
    for item in (contract or {}).get("output_structures") or []:
        for field in item.get("fields") or []:
            ref = first_ddic_reference(field.get("type_or_like"))
            if ref:
                references.append(ref)
    for item in (contract or {}).get("ddic_backed_types") or []:
        if item.get("object"):
            references.append({"object": item.get("object"), "field": item.get("field") or ""})
    for item in (contract or {}).get("scalar_variables") or []:
        ref = first_ddic_reference(item.get("type_or_like"))
        if ref:
            references.append(ref)
    for read in (contract or {}).get("database_reads") or []:
        references.append({"object": read.get("table"), "field": ""})
        for field in read.get("fields") or []:
            references.append({"object": read.get("table"), "field": field})
        for condition in read.get("where") or []:
            if condition.get("field"):
                references.append({"object": read.get("table"), "field": condition.get("field")})
    return [ref for ref in references if ref.get("object")]


def callable_calls(contract):
    return list((contract or {}).get("function_module_calls") or []) + list((contract or {}).get("class_method_calls") or [])


def callable_variable_type_compatible(actual_type, parameter, ddic_metadata=None):
    if not actual_type:
        return False
    expected = callable_parameter_type_reference(parameter)
    if not expected:
        return True
    actual = normalized_type_reference(actual_type)
    if callable_parameter_expects_table(parameter):
        actual_row_type = table_row_type_from_declaration("TYPE " + actual)
        return actual_row_type == expected or actual == expected
    if actual == f"REF TO {expected}":
        return True
    return actual == expected or ddic_component_types_compatible(actual, expected, ddic_metadata)


def ddic_component_types_compatible(actual, expected, ddic_metadata=None):
    actual_ref = ddic_component_reference(actual)
    expected_ref = ddic_component_reference(expected)
    if not actual_ref or not expected_ref:
        return False
    if actual_ref["field"] == expected_ref["field"]:
        return True
    return ddic_field_technical_signature(actual_ref, ddic_metadata) == ddic_field_technical_signature(expected_ref, ddic_metadata)


def callable_parameter_type_reference(parameter):
    base = normalized_type_reference((parameter or {}).get("abap_type") or (parameter or {}).get("type"))
    field = str((parameter or {}).get("field") or "").strip().upper()
    if base and field and "-" not in base and not is_builtin_type_name(base):
        return f"{base}-{field}"
    return base


def callable_contract_mapping_directions(requested_direction):
    direction = str(requested_direction or "").lower()
    if direction == "input":
        return CALLABLE_INPUT_DIRECTIONS
    if direction == "output":
        return CALLABLE_OUTPUT_DIRECTIONS
    if direction == "changing":
        return {"CHANGING"}
    if direction == "tables":
        return {"TABLES"}
    return set()


def callable_parameter_expects_table(parameter):
    direction = str((parameter or {}).get("direction") or "").upper()
    if direction == "TABLES":
        return True
    for key in ("is_table", "table", "table_parameter"):
        if (parameter or {}).get(key) is True:
            return True
    kind = str((parameter or {}).get("kind") or (parameter or {}).get("parameter_kind") or "").upper()
    return kind in {"TABLE", "TABLES"}


def callable_parameter_contract_kind(parameter, call=None):
    if callable_parameter_expects_table(parameter):
        return "table"
    if callable_parameter_expects_object_reference(parameter, call):
        return "reference"
    if callable_parameter_expects_structure(parameter):
        return "structure"
    return "scalar"


def callable_parameter_expects_object_reference(parameter, call=None):
    type_reference = callable_parameter_type_reference(parameter)
    if not type_reference:
        return False
    if normalized_type_reference(type_reference).startswith("REF TO "):
        return True
    class_name = str((call or {}).get("name") or "").split("=>", 1)[0].upper()
    direction = str((parameter or {}).get("direction") or "").upper()
    return bool(class_name and direction == "RETURNING" and type_reference == class_name)


def object_reference_type_name(parameter):
    type_reference = callable_parameter_type_reference(parameter)
    match = re.match(r"REF\s+TO\s+(.+)$", normalized_type_reference(type_reference), re.IGNORECASE)
    return match.group(1) if match else type_reference


def callable_parameter_expects_structure(parameter):
    type_reference = callable_parameter_type_reference(parameter)
    if not type_reference or "-" in type_reference:
        return False
    if normalized_type_reference(type_reference).startswith("REF TO "):
        return False
    return not is_builtin_type_name(type_reference)


def ddic_field_technical_signature(reference, ddic_metadata=None):
    tables = normalized_tables(ddic_metadata)
    table = tables.get((reference or {}).get("object"))
    field = normalized_fields(table).get((reference or {}).get("field"))
    if not isinstance(field, dict):
        return None
    return (
        str(field.get("datatype") or field.get("type") or "").upper(),
        str(field.get("length") or ""),
        str(field.get("decimals") or ""),
    )


def ddic_component_reference(type_text):
    match = re.fullmatch(r"([A-Z0-9_/]+)-([A-Z0-9_]+)", normalized_type_reference(type_text))
    if not match:
        return None
    return {"object": match.group(1), "field": match.group(2)}


def collect_ddic_reference_field(fields, value):
    ref = first_ddic_reference(value)
    if ref and ref.get("field"):
        append_unique(fields, ref["field"])


def collect_table_read_field_for_object(fields, value, object_name):
    reference = plan_component_reference(value)
    if not reference:
        return
    if reference["object"] == normalize_object_name(object_name):
        append_unique(fields, reference["field"])


def plan_component_reference(value):
    text = str(value or "").strip()
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)[-.]([A-Za-z_][A-Za-z0-9_]*)", text)
    if match:
        object_name = ddic_object_from_table_or_work_area(match.group(1))
        if object_name:
            return {"object": object_name, "field": match.group(2).upper()}
    ref = first_ddic_reference(text)
    if ref and ref.get("object") and ref.get("field"):
        return ref
    return None


def ddic_object_from_table_or_work_area(value):
    text = normalize_identifier(value)
    if text in {"t_output", "w_output"}:
        return ""
    for prefix in ("t_", "st_", "w_"):
        if text.startswith(prefix) and len(text) > len(prefix):
            return text[len(prefix):].upper()
    return ""


def merge_table_read_contracts(reads):
    merged = []
    by_key = {}
    for read in reads or []:
        key = (read.get("source"), read.get("work_area"), read.get("object"))
        if key not in by_key:
            item = dict(read)
            item["fields"] = []
            by_key[key] = item
            merged.append(item)
        for field in read.get("fields") or []:
            append_unique(by_key[key]["fields"], field)
    return merged


def append_unique(values, value):
    if value and value not in values:
        values.append(value)


def is_builtin_type_name(value):
    return str(value or "").upper() in {
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


def normalized_type_reference(value):
    text = normalize_declaration(value)
    text = re.sub(r"^(TYPE|LIKE)\s+", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip().upper()


def normalize_declaration(value):
    text = re.sub(r"\s+", " ", str(value or "").strip().rstrip("."))
    if not text:
        return ""
    if re.match(r"^(TYPE|LIKE)\b", text, re.IGNORECASE):
        return text
    return "TYPE " + text


def normalize_global_declaration_clause(item):
    if not isinstance(item, dict):
        return ""
    declaration = str(item.get("declaration") or item.get("type_or_like") or "").strip()
    name = normalize_identifier(item.get("name"))
    match = None
    if name:
        match = re.match(
            rf"DATA\s+{re.escape(name)}\s+((?:TYPE|LIKE)\b.+?)\s*\.\s*$",
            declaration,
            re.IGNORECASE,
        )
    if match:
        return normalize_declaration(match.group(1))
    if name and re.match(rf"DATA\s+{re.escape(name)}\b", declaration, re.IGNORECASE):
        return ""
    return normalize_declaration(declaration)


def normalize_identifier(value):
    text = str(value or "").strip()
    text = re.sub(r"[^0-9A-Za-z_]", "_", text).strip("_").lower()
    if text and text[0].isdigit():
        text = "v_" + text
    return text


def normalize_program_name(value):
    name = normalize_identifier(value)
    return name.upper() if name else ""


def normalize_object_name(value):
    return str(value or "").strip().upper()


def normalize_ddic_field_reference(value):
    ref = ddic_reference(value)
    return f"{ref['object']}-{ref['field']}" if ref.get("object") and ref.get("field") else str(value or "").strip().upper()


def ddic_reference(value):
    match = re.search(r"\b([A-Za-z/][A-Za-z0-9_/]{1,29})-([A-Za-z_][A-Za-z0-9_]*)\b", str(value or ""))
    if match:
        return {"object": match.group(1).upper(), "field": match.group(2).upper()}
    return {"object": str(value or "").strip().upper(), "field": ""}


def first_ddic_reference(value):
    match = re.search(r"\b(?:TYPE|LIKE)?\s*([A-Za-z/][A-Za-z0-9_/]{1,29})-([A-Za-z_][A-Za-z0-9_]*)\b", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    return {"object": match.group(1).upper(), "field": match.group(2).upper()}


def normalize_table_item(item):
    if isinstance(item, dict):
        name = normalize_identifier(item.get("name"))
        row_type = normalize_identifier(item.get("row_type") or item.get("type"))
    else:
        name = normalize_identifier(item)
        row_type = ""
    return {"name": name, "row_type": row_type} if name else {}


def global_internal_table_contract(item):
    if not isinstance(item, dict):
        return {}
    name = normalize_identifier(item.get("name"))
    declaration = normalize_global_declaration_clause(item)
    row_type = table_row_type_from_declaration(declaration)
    return {"name": name, "row_type": row_type} if name and row_type else {}


def global_work_area_contract(item):
    if not isinstance(item, dict):
        return {}
    name = normalize_identifier(item.get("name"))
    declaration = normalize_global_declaration_clause(item)
    row_type = work_area_type_from_declaration(declaration)
    return {"name": name, "type": row_type} if name and row_type and not is_builtin_type_name(row_type) else {}


def global_declaration_owned_elsewhere(item):
    return bool(global_internal_table_contract(item) or global_work_area_contract(item))


def table_row_type_from_declaration(declaration):
    match = re.search(r"\bTYPE\s+(?:STANDARD|SORTED|HASHED)?\s*TABLE\s+OF\s+([A-Za-z0-9_/]+)\b", declaration, re.IGNORECASE)
    return normalize_type_name(match.group(1)) if match else ""


def work_area_type_from_declaration(declaration):
    if re.search(r"\bTABLE\s+OF\b", declaration, re.IGNORECASE):
        return ""
    if re.search(r"\bTYPE\s+REF\s+TO\b", declaration, re.IGNORECASE):
        return ""
    if re.search(r"\bTYPE\s+[A-Za-z/][A-Za-z0-9_/]{1,29}-[A-Za-z_][A-Za-z0-9_]*\b", declaration, re.IGNORECASE):
        return ""
    match = re.search(r"\bTYPE\s+([A-Za-z][A-Za-z0-9_]*|/[A-Za-z0-9_]+/[A-Za-z0-9_]+)\b(?!\s*-)", declaration, re.IGNORECASE)
    return normalize_type_name(match.group(1)) if match else ""


def parameter_value_pairs(values):
    if isinstance(values, dict):
        return list(values.items())
    pairs = []
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, dict):
                continue
            parameter = item.get("parameter") or item.get("name")
            variable = item.get("value") or item.get("variable") or item.get("target")
            if parameter and variable:
                pairs.append((parameter, variable))
    return pairs


def variable_type_lookup_key(value):
    text = str(value or "").strip()
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)[-.]([A-Za-z_][A-Za-z0-9_]*)", text)
    if match:
        return f"{normalize_identifier(match.group(1))}-{match.group(2).lower()}"
    return normalize_identifier(text)


def ddic_object_from_row_type(row_type):
    text = normalize_type_name(row_type)
    if not text:
        return ""
    if text.startswith("TY_"):
        return text[3:]
    return text


def normalize_type_name(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def add_unique_dict(values, item):
    item_name = str((item or {}).get("name") or "").lower()
    if item_name and "field" not in (item or {}) and any(str((existing or {}).get("name") or "").lower() == item_name for existing in values):
        return
    key = json.dumps(item, sort_keys=True)
    if key not in {json.dumps(existing, sort_keys=True) for existing in values}:
        values.append(item)


def dedupe(values):
    result = []
    seen = set()
    for value in values or []:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def duplicates(values):
    seen = set()
    repeated = []
    for value in values or []:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated


def form_definitions_from_contract(contract):
    return [normalize_identifier(item.get("name")).lower() for item in (contract or {}).get("form_routines") or [] if item.get("name")]
