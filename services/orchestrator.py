import json
import re
from pathlib import Path
from time import perf_counter

from services.abap_source import split_code_and_comment, statement_ends
from services.callable_signature_provider import normalize_provider_signatures
from services.ddic_metadata_context import field_detail, normalized_fields, normalized_tables
from services.final_assembler import (
    APP_FINAL_ASSEMBLY_MODE,
    LLM_FINAL_ASSEMBLY_MODE,
    assemble_final_abap_from_chunks,
    normalize_final_assembly_mode,
)
from services.llm import generate_abap
CHUNK_DIAGNOSTIC = "abap_generation_chunks.json"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALV_FIELDCAT_TABLE_NAME = "t_fieldcat"
ALV_FIELDCAT_WORK_AREA_NAME = "w_fieldcat"
ALV_FIELDCAT_TABLE_TYPE = "slis_t_fieldcat_alv"
ALV_FIELDCAT_WORK_AREA_TYPE = "slis_fieldcat_alv"
CHUNK_PROMPT_PATHS = {
    "declarations": PROJECT_ROOT / "prompts" / "declarations_chunk.txt",
    "database_read_forms": PROJECT_ROOT / "prompts" / "database_read_forms_chunk.txt",
    "processing_form": PROJECT_ROOT / "prompts" / "processing_form_chunk.txt",
    "output_forms": PROJECT_ROOT / "prompts" / "output_forms_chunk.txt",
    "main_program_flow": PROJECT_ROOT / "prompts" / "main_program_flow_chunk.txt",
}
DECLARATIONS_CHUNK_PROMPT_PATH = CHUNK_PROMPT_PATHS["declarations"]
DECLARATION_REQUIREMENTS_PROMPT_PATH = PROJECT_ROOT / "prompts" / "declaration_requirements_extraction.txt"
PROCESSING_PLAN_PROMPT_PATH = PROJECT_ROOT / "prompts" / "processing_plan_extraction.txt"
MAX_PROCESSING_PLAN_EXTRACTION_ATTEMPTS = 2


ABAP_CHUNKS = [
    {
        "name": "declarations",
        "instruction": (
            "Generate only REPORT, TABLES, TYPES, DATA, constants, PARAMETERS, and SELECT-OPTIONS."
        ),
    },
    {
        "name": "database_read_forms",
        "instruction": (
            "Generate only database read FORM routines. "
            "Do not generate REPORT statements, declarations, selection-screen declarations, or event blocks."
        ),
    },
    {
        "name": "processing_form",
        "instruction": (
            "Generate only the processing FORM routine or routines. "
            "Do not generate REPORT statements, declarations, selection-screen declarations, or event blocks."
        ),
    },
    {
        "name": "output_forms",
        "instruction": (
            "Generate only output FORM routines. "
            "Do not generate REPORT statements, declarations, selection-screen declarations, or event blocks."
        ),
    },
    {
        "name": "main_program_flow",
        "instruction": (
            "Generate only the main program flow event block and PERFORM calls. "
            "Do not generate declarations or FORM routines."
        ),
    },
]
FORM_GENERATING_CHUNKS = {"database_read_forms", "processing_form", "output_forms"}
GLOBAL_STYLE_PREFIXES = ("t_", "st_", "w_", "gt_", "gs_", "gv_", "it_", "lt_", "ls_", "lv_", "wa_", "ct_")


class ChunkedGenerationError(Exception):
    def __init__(self, message, chunks=None, processing_plan=None):
        super().__init__(message)
        self.chunks = list(chunks or [])
        self.processing_plan = processing_plan


class ProcessingPlanValidationError(Exception):
    def __init__(self, diagnostics):
        errors = (diagnostics or {}).get("validation_errors") or []
        super().__init__("Processing plan validation failed: " + "; ".join(errors))
        self.diagnostics = diagnostics


class ProcessingContractValidationError(Exception):
    def __init__(self, diagnostics):
        errors = (diagnostics or {}).get("validation_errors") or []
        super().__init__("Processing contract validation failed: " + "; ".join(errors))
        self.diagnostics = diagnostics


def generate_chunked_abap_program(
    prompt_text,
    source_text,
    abap_generator=None,
    progress_callback=None,
    pre_chunk_progress_callback=None,
    declaration_requirements_generator=None,
    processing_plan_generator=None,
    callable_metadata=None,
    ddic_metadata=None,
    declaration_requirements=None,
    approved_processing_plan=None,
    final_assembly_mode=APP_FINAL_ASSEMBLY_MODE,
):
    generator = abap_generator or generate_abap
    if declaration_requirements is None:
        if pre_chunk_progress_callback:
            pre_chunk_progress_callback("Extracting declaration requirements")
        declaration_requirements = extract_declaration_requirements(
            source_text,
            declaration_requirements_generator or generator,
            metadata_context=prompt_text,
            callable_metadata=callable_metadata,
        )
        declaration_requirements = enrich_declaration_requirements_for_form_globals(
            declaration_requirements,
            prompt_text,
            source_text,
        )
    declaration_requirements_text = declaration_requirements_for_prompt(declaration_requirements)
    if approved_processing_plan is None:
        if pre_chunk_progress_callback:
            pre_chunk_progress_callback("Extracting processing plan")
        processing_plan = extract_processing_plan(
            source_text,
            processing_plan_generator or generator,
            metadata_context=prompt_text,
            callable_metadata=callable_metadata,
            ddic_metadata=ddic_metadata,
            declaration_requirements=declaration_requirements_text,
        )
    else:
        processing_plan = approved_processing_plan
    declaration_requirements = declaration_requirements_with_processing_plan_variables(
        declaration_requirements,
        processing_plan,
    )
    declaration_requirements_text = declaration_requirements_for_prompt(declaration_requirements)
    processing_plan_text = processing_plan_for_prompt(processing_plan)
    chunks = []
    chunks_to_generate = abap_chunks_for_processing_plan(
        ABAP_CHUNKS,
        prompt_text,
        source_text=source_text,
        declaration_requirements=declaration_requirements_text,
        processing_plan=processing_plan_text,
    )
    total_chunks = len(chunks_to_generate)
    for index, chunk in enumerate(chunks_to_generate, start=1):
        chunk_name = chunk["name"]
        chunk_processing_plan = processing_plan_for_chunk(chunk, processing_plan_text)
        chunk_source_text = chunk_requirement_block(
            chunk_name,
            source_text,
            prompt_text,
            declaration_requirements=declaration_requirements_text,
            processing_plan=chunk_processing_plan,
            ddic_metadata=ddic_metadata,
        )
        ddic_diagnostics = chunk_ddic_diagnostics(
            chunk_name,
            prompt_text,
            source_text=source_text,
            chunk_requirements=chunk_source_text,
            declaration_requirements=declaration_requirements_text,
            processing_plan=chunk_processing_plan,
            ddic_metadata=ddic_metadata,
        )
        filtered_ddic_metadata = ddic_diagnostics["final_filtered_metadata"]
        chunk_contract = chunk_generation_contract_block(
            prompt_text,
            chunk_name,
            source_text=source_text,
            declaration_requirements=declaration_requirements_text,
            processing_plan=chunk_processing_plan,
            ddic_metadata=ddic_metadata,
        )
        chunk_prompt = chunk_prompt_text(
            prompt_text,
            chunk,
            source_text=source_text,
            declaration_requirements=declaration_requirements_text,
            processing_plan=chunk_processing_plan,
            ddic_metadata=ddic_metadata,
        )
        started_at = None
        result = {}
        raw_text = ""
        text = ""
        duration_seconds = None
        try:
            if progress_callback:
                progress_callback(index, total_chunks, chunk)
            started_at = perf_counter()
            result = generator(chunk_prompt, chunk_source_text)
            duration_seconds = perf_counter() - started_at
            raw_text = response_text(result)
            text = clean_abap_response(raw_text)
            if chunk_name == "declarations":
                text = ensure_required_tables_declarations(text, declaration_requirements_text)
                text = ensure_required_global_declarations(text, declaration_requirements_text)
                text = ensure_database_read_declarations(
                    text,
                    prompt_text,
                    source_text=source_text,
                    declaration_requirements=declaration_requirements_text,
                    ddic_metadata=ddic_metadata,
                )
                text = group_declaration_statements_by_prefix(text)
            elif chunk_name in FORM_GENERATING_CHUNKS:
                ensure_form_chunk_uses_declared_globals(
                    text,
                    chunk_name,
                    prompt_text,
                    source_text=source_text,
                    declaration_requirements=declaration_requirements_text,
                )
            post_processing_diagnostics = (
                declaration_post_processing_diagnostics(raw_text, text)
                if chunk_name == "declarations"
                else None
            )
            chunks.append(
                chunk_diagnostic_record(
                    chunk,
                    chunk_prompt,
                    raw_text,
                    text,
                    result,
                    duration_seconds,
                    filtered_ddic_metadata,
                    ddic_diagnostics,
                    post_processing_diagnostics,
                    declaration_requirements if chunk_name == "declarations" else None,
                    chunk_contract if chunk_name == "declarations" else None,
                    processing_plan_payload_for_diagnostic(chunk, processing_plan) if chunk_name == "processing_form" else None,
                )
            )
        except Exception as exc:
            if started_at is not None and duration_seconds is None:
                duration_seconds = perf_counter() - started_at
            failed = chunk_diagnostic_record(
                chunk,
                chunk_prompt,
                raw_text,
                text,
                result,
                duration_seconds,
                filtered_ddic_metadata,
                ddic_diagnostics,
                None,
                declaration_requirements if chunk_name == "declarations" else None,
                chunk_contract if chunk_name == "declarations" else None,
                processing_plan_payload_for_diagnostic(chunk, processing_plan) if chunk_name == "processing_form" else None,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise ChunkedGenerationError(
                f"{type(exc).__name__}: {exc}",
                chunks + [failed],
                processing_plan=processing_plan,
            ) from exc
    assembly_mode = normalize_final_assembly_mode(final_assembly_mode)
    final_assembly_result = None
    if assembly_mode == LLM_FINAL_ASSEMBLY_MODE:
        if pre_chunk_progress_callback:
            pre_chunk_progress_callback("Assembling final ABAP with LLM")
        started_at = perf_counter()
        final_assembly_result = generator(
            final_llm_assembly_prompt(),
            final_llm_assembly_source(chunks),
        )
        final_text = clean_abap_response(response_text(final_assembly_result))
        final_assembly_result = {
            "mode": assembly_mode,
            "prompt": final_llm_assembly_prompt(),
            "source": final_llm_assembly_source(chunks),
            "text": final_text,
            "model": final_assembly_result.get("model") if isinstance(final_assembly_result, dict) else None,
            "usage": final_assembly_result.get("usage") if isinstance(final_assembly_result, dict) else None,
            "duration_seconds": perf_counter() - started_at,
        }
    else:
        final_text = assemble_abap_chunks(chunks, final_assembly_mode=assembly_mode)
        final_assembly_result = {
            "mode": assembly_mode,
            "text": final_text,
            "model": None,
            "usage": None,
            "duration_seconds": None,
        }
    return {
        "text": final_text,
        "chunks": chunks,
        "final_assembly_mode": assembly_mode,
        "final_assembly": final_assembly_result,
        "model": final_assembly_result.get("model") or first_value(chunk.get("model") for chunk in chunks),
        "usage": aggregate_usage(
            [declaration_requirements.get("usage"), processing_plan.get("usage")]
            + [chunk.get("usage") for chunk in chunks]
            + [final_assembly_result.get("usage")]
        ),
        "declaration_requirements": declaration_requirements,
        "processing_plan": processing_plan,
        "post_generation_source_stages": post_generation_source_stages(chunks, final_text),
    }


def chunk_diagnostic_record(
    chunk,
    chunk_prompt,
    raw_text,
    text,
    result,
    duration_seconds,
    filtered_ddic_metadata,
    ddic_diagnostics,
    post_processing_diagnostics,
    declaration_requirements,
    declaration_naming_contract,
    processing_plan=None,
    error=None,
):
    record = {
        "name": chunk["name"],
        "instruction": chunk["instruction"],
        "subtitle": chunk.get("subtitle"),
        "prompt": chunk_prompt,
        "raw_response": raw_text,
        "text": text,
        "model": result.get("model") if isinstance(result, dict) else None,
        "usage": result.get("usage") if isinstance(result, dict) else None,
        "duration_seconds": duration_seconds,
        "filtered_ddic_metadata": filtered_ddic_metadata,
        "ddic_metadata_filter": ddic_diagnostics,
        "post_processing_diagnostics": post_processing_diagnostics,
        "declaration_requirements": declaration_requirements,
        "declaration_naming_contract": declaration_naming_contract,
        "processing_plan": processing_plan,
    }
    if error:
        record["error"] = error
    return record


def abap_chunks_for_processing_plan(chunks, base_prompt, source_text=None, declaration_requirements=None, processing_plan=None):
    result = []
    for chunk in chunks or []:
        if (chunk or {}).get("name") == "processing_form":
            result.extend(processing_form_chunks_for_top_level_steps(chunk, processing_plan))
            continue
        if (chunk or {}).get("name") == "output_forms" and not output_forms_chunk_required(
            base_prompt,
            source_text=source_text,
            declaration_requirements=declaration_requirements,
            processing_plan=processing_plan,
        ):
            continue
        result.append(chunk)
    return result


def processing_form_chunks_for_top_level_steps(chunk, processing_plan=None):
    steps = processing_plan_top_level_steps(processing_plan)
    if not steps:
        return [chunk]
    result = []
    for index, step in enumerate(steps, start=1):
        split_chunk = dict(chunk)
        split_chunk["processing_plan"] = processing_subtree_plan_text(step)
        split_chunk["processing_plan_step_index"] = index
        split_chunk["subtitle"] = processing_step_subtitle(step, index)
        result.append(split_chunk)
    return result


def processing_plan_top_level_steps(processing_plan=None):
    payload = processing_plan_payload(processing_plan)
    return [step for step in payload.get("processing_steps") or [] if isinstance(step, dict)]


def processing_subtree_plan_text(step):
    return json.dumps({"processing_steps": [step]}, indent=2, sort_keys=True)


def processing_step_subtitle(step, index=None):
    if not isinstance(step, dict):
        return f"Processing step {index}" if index else "Processing step"
    operation = str(step.get("operation") or "step").strip().upper()
    prefix = f"Step {index}: " if index else ""
    if operation == "LOOP":
        source = normalize_plan_identifier(step.get("source")) or str(step.get("source") or "").strip()
        child_count = len(step.get("steps") or []) if isinstance(step.get("steps"), list) else 0
        detail = f"Loop {source}" if source else "Loop records"
        if child_count:
            detail += f" with {child_count} nested step{'s' if child_count != 1 else ''}"
        return prefix + detail
    if operation == "READ":
        source = normalize_plan_identifier(step.get("source")) or str(step.get("source") or "").strip()
        return prefix + (f"Read {source} by lookup key" if source else "Read by lookup key")
    if operation in {"CALL_FUNCTION", "CALL_METHOD", "CALL_STATIC_METHOD"}:
        name = step.get("name") or step.get("callable")
        if not name and step.get("class") and step.get("method"):
            name = f"{step.get('class')}=>{step.get('method')}"
        return prefix + (f"Call {str(name).strip().upper()}" if name else f"{operation.replace('_', ' ').title()}")
    if operation == "IF":
        branch_count = len(step.get("then") or []) + len(step.get("else") or [])
        detail = "Evaluate condition"
        if branch_count:
            detail += f" with {branch_count} branch step{'s' if branch_count != 1 else ''}"
        return prefix + detail
    target = normalize_plan_reference(step.get("target"), {}) if step.get("target") else ""
    source = normalize_plan_reference(step.get("source"), {}) if step.get("source") else ""
    parts = [operation.replace("_", " ").title()]
    if source:
        parts.append(source)
    if target:
        parts.append(f"to {target}")
    return prefix + " ".join(parts)


def processing_plan_for_chunk(chunk, default_processing_plan=None):
    if (chunk or {}).get("name") == "processing_form" and (chunk or {}).get("processing_plan"):
        return chunk["processing_plan"]
    return default_processing_plan


def processing_plan_payload_for_diagnostic(chunk, full_processing_plan=None):
    if (chunk or {}).get("name") == "processing_form" and (chunk or {}).get("processing_plan"):
        return {
            "plan": processing_plan_payload(chunk.get("processing_plan")),
            "top_level_step_index": chunk.get("processing_plan_step_index"),
        }
    return full_processing_plan


def output_forms_chunk_required(base_prompt, source_text=None, declaration_requirements=None, processing_plan=None):
    if not processing_plan_creates_output_records(processing_plan, declaration_requirements):
        return True
    contract = prompt_block(base_prompt, "Shared generation contract:", ())
    for line in str(contract or "").splitlines():
        if line.strip().lower().startswith("exact form names:"):
            return bool(filtered_form_names(line, "output_forms", processing_plan=processing_plan, declaration_requirements=declaration_requirements))
    return True


def extract_declaration_requirements(source_text, generator, metadata_context=None, callable_metadata=None):
    prompt = declaration_requirements_extraction_prompt(metadata_context, callable_metadata=callable_metadata)
    started_at = perf_counter()
    result = generator(prompt, source_text)
    duration_seconds = perf_counter() - started_at
    raw_text = response_text(result).strip()
    parsed = parse_json_response(raw_text)
    callable_signatures = normalize_provider_signatures(callable_metadata)
    normalized = normalize_declaration_requirements_with_diagnostics(
        parsed.get("value"),
        ddic_catalogue=extract_ddic_catalogue(metadata_context),
        callable_metadata=callable_signatures,
    )
    requirements = normalized["requirements"]
    output_fields = (requirements or {}).get("output_structure_fields") if isinstance(requirements, dict) else []
    return {
        "prompt": prompt,
        "raw_response": raw_text,
        "requirements": requirements,
        "diagnostics": {
            "callable_metadata_passed_to_requirement_extraction": callable_signatures,
            "output_field_source_mappings": normalized["output_field_source_mappings"],
            "final_output_structure_fields": output_fields or [],
        },
        "parse_error": parsed.get("error"),
        "model": result.get("model") if isinstance(result, dict) else None,
        "usage": result.get("usage") if isinstance(result, dict) else None,
        "duration_seconds": duration_seconds,
    }


def extract_processing_plan(source_text, generator, metadata_context=None, callable_metadata=None, declaration_requirements=None, ddic_metadata=None):
    processing_source_text = extract_processing_rules_section(source_text)
    processing_contract = build_processing_contract(
        metadata_context=metadata_context,
        callable_metadata=callable_metadata,
        declaration_requirements=declaration_requirements,
        processing_rules_text=processing_source_text,
        ddic_metadata=ddic_metadata,
    )
    if processing_contract["validation_errors"]:
        raise ProcessingContractValidationError(processing_contract_diagnostics(processing_contract))
    validation_declaration_requirements = declaration_requirements_with_processing_contract_variables_text(
        declaration_requirements,
        processing_contract["final_processing_contract"],
    )
    base_prompt = processing_plan_extraction_prompt(processing_contract=processing_contract)
    attempts = []
    validation_errors = []
    total_duration = 0
    for attempt in range(1, MAX_PROCESSING_PLAN_EXTRACTION_ATTEMPTS + 1):
        prompt = processing_plan_retry_prompt(base_prompt, validation_errors)
        started_at = perf_counter()
        result = generate_processing_plan_response(generator, prompt, processing_source_text)
        duration_seconds = perf_counter() - started_at
        total_duration += duration_seconds
        raw_text = response_text(result).strip()
        parsed = parse_json_response(raw_text)
        normalized = normalize_processing_plan_with_diagnostics(
            parsed.get("value"),
            base_prompt=metadata_context,
            declaration_requirements=validation_declaration_requirements,
            callable_metadata=callable_metadata,
        )
        validation = validate_processing_plan(
            normalized["plan"],
            base_prompt=metadata_context,
            declaration_requirements=validation_declaration_requirements,
            callable_metadata=callable_metadata,
            normalization_diagnostics=normalized["diagnostics"],
        )
        retryable_errors = processing_plan_retryable_validation_errors(parsed, validation, normalized)
        diagnostics = {
            "prompt": prompt,
            "source_text": processing_source_text,
            "original_source_text": source_text,
            "processing_contract": processing_contract["final_processing_contract"],
            "processing_contract_diagnostics": processing_contract_diagnostics(processing_contract),
            "validation_error_type": "business_processing_plan" if validation["errors"] else None,
            "raw_response_json": raw_response_json(result),
            "raw_response": raw_text,
            "parsed_plan_before_normalization": parsed.get("value"),
            "processing_plan_trace": processing_plan_extraction_trace(
                parsed.get("value"),
                normalized,
            validation_input=normalized["plan"],
            ),
            "final_plan_passed_to_deterministic_validation": normalized["plan"],
            "plan": normalized["plan"] if validation["valid"] or not retryable_errors else None,
            "invalid_plan": None if validation["valid"] or not retryable_errors else normalized["plan"],
            "normalization_diagnostics": normalized["diagnostics"],
            "validation_errors": validation["errors"],
            "retryable_validation_errors": retryable_errors,
            "parse_error": parsed.get("error"),
            "model": result.get("model") if isinstance(result, dict) else None,
            "usage": result.get("usage") if isinstance(result, dict) else None,
            "duration_seconds": duration_seconds,
            "attempt": attempt,
        }
        attempts.append(dict(diagnostics))
        if retryable_errors:
            validation_errors = retryable_errors
            continue
        if validation["valid"]:
            diagnostics["attempts"] = list(attempts)
            diagnostics["duration_seconds"] = total_duration
            return diagnostics
        diagnostics["attempts"] = list(attempts)
        diagnostics["duration_seconds"] = total_duration
        raise ProcessingPlanValidationError(diagnostics)
    failed = dict(attempts[-1] if attempts else {})
    failed["attempts"] = attempts
    failed["duration_seconds"] = total_duration
    raise ProcessingPlanValidationError(failed)


def generate_processing_plan_response(generator, prompt, source_text):
    response_format = processing_plan_response_format()
    try:
        return generator(prompt, source_text, response_format=response_format)
    except TypeError as exc:
        if "response_format" not in str(exc):
            raise
        return generator(prompt, source_text)


def processing_plan_response_format():
    return {
        "type": "json_schema",
        "name": "processing_plan",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["processing_steps"],
            "properties": {
                "processing_steps": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/step"},
                },
            },
            "$defs": {
                "reference": {
                    "type": "string",
                },
                "parameter_mapping": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["parameter", "value"],
                    "properties": {
                        "parameter": {"type": "string"},
                        "value": {"$ref": "#/$defs/reference"},
                    },
                },
                "parameter_mappings": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/parameter_mapping"},
                },
                "condition": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["left", "operator", "right"],
                    "properties": {
                        "left": {"$ref": "#/$defs/reference"},
                        "operator": {
                            "type": "string",
                            "enum": ["=", "<>", "IS INITIAL", "IS NOT INITIAL", "CONTAINS ERROR"],
                        },
                        "right": {"type": ["string", "null"]},
                    },
                },
                "step": {
                    "anyOf": [
                        {"$ref": "#/$defs/loop_step"},
                        {"$ref": "#/$defs/read_step"},
                        {"$ref": "#/$defs/if_step"},
                        {"$ref": "#/$defs/move_step"},
                        {"$ref": "#/$defs/clear_step"},
                        {"$ref": "#/$defs/append_step"},
                        {"$ref": "#/$defs/call_function_step"},
                        {"$ref": "#/$defs/call_static_method_step"},
                        {"$ref": "#/$defs/call_method_step"},
                        {"$ref": "#/$defs/concatenate_step"},
                        {"$ref": "#/$defs/sort_step"},
                        {"$ref": "#/$defs/delete_step"},
                    ],
                },
                "loop_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "source", "into", "steps"],
                    "properties": {
                        "operation": {"type": "string", "enum": ["LOOP"]},
                        "source": {"$ref": "#/$defs/reference"},
                        "into": {"$ref": "#/$defs/reference"},
                        "steps": {"type": "array", "items": {"$ref": "#/$defs/step"}},
                    },
                },
                "read_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "source", "into", "conditions"],
                    "properties": {
                        "operation": {"type": "string", "enum": ["READ"]},
                        "source": {"$ref": "#/$defs/reference"},
                        "into": {"$ref": "#/$defs/reference"},
                        "conditions": {"type": "array", "items": {"$ref": "#/$defs/condition"}},
                    },
                },
                "if_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "conditions", "then", "else"],
                    "properties": {
                        "operation": {"type": "string", "enum": ["IF"]},
                        "conditions": {"type": "array", "items": {"$ref": "#/$defs/condition"}},
                        "then": {"type": "array", "items": {"$ref": "#/$defs/step"}},
                        "else": {"type": "array", "items": {"$ref": "#/$defs/step"}},
                    },
                },
                "move_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "source", "target"],
                    "properties": {
                        "operation": {"type": "string", "enum": ["MOVE"]},
                        "source": {"$ref": "#/$defs/reference"},
                        "target": {"$ref": "#/$defs/reference"},
                    },
                },
                "clear_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "target"],
                    "properties": {
                        "operation": {"type": "string", "enum": ["CLEAR"]},
                        "target": {"$ref": "#/$defs/reference"},
                    },
                },
                "append_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "source", "target"],
                    "properties": {
                        "operation": {"type": "string", "enum": ["APPEND"]},
                        "source": {"$ref": "#/$defs/reference"},
                        "target": {"$ref": "#/$defs/reference"},
                    },
                },
                "call_function_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "name", "input_parameters", "output_parameters"],
                    "properties": {
                        "operation": {"type": "string", "enum": ["CALL_FUNCTION"]},
                        "name": {"type": "string"},
                        "input_parameters": {"$ref": "#/$defs/parameter_mappings"},
                        "output_parameters": {"$ref": "#/$defs/parameter_mappings"},
                    },
                },
                "call_static_method_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "operation",
                        "class",
                        "method",
                        "input_parameters",
                        "output_parameters",
                        "receiving_parameter",
                        "returning_parameter",
                    ],
                    "properties": {
                        "operation": {"type": "string", "enum": ["CALL_STATIC_METHOD"]},
                        "class": {"type": "string"},
                        "method": {"type": "string"},
                        "input_parameters": {"$ref": "#/$defs/parameter_mappings"},
                        "output_parameters": {"$ref": "#/$defs/parameter_mappings"},
                        "receiving_parameter": {"type": ["string", "null"]},
                        "returning_parameter": {"type": ["string", "null"]},
                    },
                },
                "call_method_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "operation",
                        "object",
                        "method",
                        "input_parameters",
                        "output_parameters",
                        "receiving_parameter",
                        "returning_parameter",
                    ],
                    "properties": {
                        "operation": {"type": "string", "enum": ["CALL_METHOD"]},
                        "object": {"type": "string"},
                        "method": {"type": "string"},
                        "input_parameters": {"$ref": "#/$defs/parameter_mappings"},
                        "output_parameters": {"$ref": "#/$defs/parameter_mappings"},
                        "receiving_parameter": {"type": ["string", "null"]},
                        "returning_parameter": {"type": ["string", "null"]},
                    },
                },
                "concatenate_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "sources", "target", "separator"],
                    "properties": {
                        "operation": {"type": "string", "enum": ["CONCATENATE"]},
                        "sources": {
                            "type": "array",
                            "items": {"$ref": "#/$defs/reference"},
                        },
                        "target": {"$ref": "#/$defs/reference"},
                        "separator": {"type": ["string", "null"]},
                    },
                },
                "sort_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "source", "by"],
                    "properties": {
                        "operation": {"type": "string", "enum": ["SORT"]},
                        "source": {"$ref": "#/$defs/reference"},
                        "by": {"type": "array", "items": {"$ref": "#/$defs/reference"}},
                    },
                },
                "delete_step": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "source", "conditions"],
                    "properties": {
                        "operation": {"type": "string", "enum": ["DELETE"]},
                        "source": {"$ref": "#/$defs/reference"},
                        "conditions": {"type": "array", "items": {"$ref": "#/$defs/condition"}},
                    },
                },
            },
        },
    }


def extract_processing_rules_section(source_text):
    text = str(source_text or "")
    lines = text.splitlines()
    start_index = None
    start_level = None
    for index, line in enumerate(lines):
        heading = markdown_heading(line)
        if not heading:
            continue
        if normalize_markdown_heading_text(heading["text"]) == "processing rules":
            start_index = index + 1
            start_level = heading["level"]
            break
    if start_index is None:
        return text
    end_index = len(lines)
    for index in range(start_index, len(lines)):
        heading = markdown_heading(lines[index])
        if heading and heading["level"] <= start_level:
            end_index = index
            break
    return "\n".join(lines[start_index:end_index]).strip()


def markdown_heading(line):
    match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", str(line or ""))
    if not match:
        return None
    return {"level": len(match.group(1)), "text": match.group(2).strip()}


def normalize_markdown_heading_text(value):
    text = re.sub(r"\s+", " ", str(value or "").strip()).strip(" :")
    return text.lower()


def processing_plan_retry_prompt(base_prompt, validation_errors=None):
    errors = [str(error).strip() for error in validation_errors or [] if str(error).strip()]
    if not errors:
        return base_prompt
    lines = [
        base_prompt.rstrip(),
        "",
        "The previous processing plan was rejected by deterministic validation.",
        "Return a complete corrected processing plan. Do not omit valid required steps.",
        "Validation errors to fix:",
    ]
    lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines).rstrip() + "\n"


def processing_plan_retryable_validation_errors(parsed, validation, normalized):
    errors = []
    parse_error = (parsed or {}).get("error")
    if parse_error:
        errors.append(f"invalid JSON: {parse_error}")
    plan_value = (parsed or {}).get("value")
    if isinstance(plan_value, dict) and "processing_steps" not in plan_value:
        errors.append("missing processing_steps")
    elif not isinstance(plan_value, dict):
        errors.append("processing plan root is not an object")
    diagnostics = (normalized or {}).get("diagnostics") or {}
    for item in diagnostics.get("rejected_steps") or []:
        reason = str((item or {}).get("reason") or "").strip()
        if processing_plan_rejection_reason_is_retryable(reason):
            path = format_processing_plan_path((item or {}).get("path"))
            errors.append(f"{path}: {reason}")
    return dedupe_preserve_order(errors)


def processing_plan_rejection_reason_is_retryable(reason):
    return str(reason or "").strip() in {
        "processing plan root is not an object",
        "processing_steps is not an array",
        "steps branch is not an array",
        "step is not an object",
        "unsupported or missing operation",
    }


def processing_plan_for_prompt(diagnostics):
    plan = (diagnostics or {}).get("plan")
    if plan is not None:
        return json.dumps(plan, indent=2, sort_keys=True)
    raw_response = str((diagnostics or {}).get("raw_response") or "").strip()
    return raw_response or "None"


def normalize_processing_plan(value, base_prompt=None, declaration_requirements=None, callable_metadata=None):
    return normalize_processing_plan_with_diagnostics(
        value,
        base_prompt=base_prompt,
        declaration_requirements=declaration_requirements,
        callable_metadata=callable_metadata,
    )["plan"]


def normalize_processing_plan_with_diagnostics(value, base_prompt=None, declaration_requirements=None, callable_metadata=None):
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
    context = processing_plan_normalization_context(
        base_prompt=base_prompt,
        declaration_requirements=declaration_requirements,
        callable_metadata=callable_metadata,
        diagnostics=diagnostics,
    )
    normalized = normalize_processing_steps(steps, context, path=["processing_steps"])
    append_processing_plan_trace(diagnostics, "normalize_processing_steps.root", {"processing_steps": normalized})
    normalized = prune_unused_read_steps(normalized, diagnostics, path=["processing_steps"])
    append_processing_plan_trace(diagnostics, "prune_unused_read_steps.root", {"processing_steps": normalized})
    plan = {"processing_steps": renumber_processing_steps(normalized)}
    append_processing_plan_trace(diagnostics, "renumber_processing_steps.root", plan)
    append_processing_plan_trace(diagnostics, "normalize_processing_plan.output", plan)
    return {"plan": plan, "diagnostics": diagnostics}


SUPPORTED_PROCESSING_PLAN_OPERATIONS = {
    "APPEND",
    "CALL_FUNCTION",
    "CALL_METHOD",
    "CALL_STATIC_METHOD",
    "CLEAR",
    "CONCATENATE",
    "DELETE",
    "IF",
    "LOOP",
    "MOVE",
    "READ",
    "SORT",
}


def processing_plan_normalization_context(base_prompt=None, declaration_requirements=None, callable_metadata=None, diagnostics=None):
    return {
        "base_prompt": base_prompt,
        "declaration_requirements": declaration_requirements,
        "table_to_work_area": table_to_work_area_contracts(base_prompt),
        "selection_parameters": set(selection_parameter_names_from_requirements(declaration_requirements)),
        "output_fields": output_field_names_from_requirements(declaration_requirements),
        "callable_directions": callable_parameter_directions(metadata_context=base_prompt, callable_metadata=callable_metadata),
        "callable_identities": processing_contract_callable_identities(base_prompt, callable_metadata),
        "diagnostics": diagnostics if diagnostics is not None else {"rejected_steps": []},
    }


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
        record_processing_step_modification(
            context.get("diagnostics"),
            path,
            item,
            step,
            "normalized LOOP fields and child branch",
            "normalize_processing_step",
        )
        return step
    if operation == "READ":
        source = normalize_plan_identifier(item.get("source"))
        into = normalize_plan_identifier(item.get("into")) or work_area_for_table(source, context)
        conditions = normalize_read_lookup_conditions(source, into, normalize_read_conditions(item, context), context)
        if not source or not into or not conditions:
            reason = "READ step is missing source, into, or valid conditions"
            record_processing_step_rejection(context.get("diagnostics"), path, item, reason, "normalize_processing_step")
            return None
        step.update({"source": source, "into": into, "conditions": conditions})
        record_processing_step_modification(
            context.get("diagnostics"),
            path,
            item,
            step,
            "normalized READ source, work area, and conditions",
            "normalize_processing_step",
        )
        return step
    if operation == "MOVE":
        source = normalize_plan_reference(item.get("source"), context, role="source")
        target = normalize_plan_reference(item.get("target"), context, role="target")
        if not source or not target:
            record_processing_step_rejection(context.get("diagnostics"), path, item, "MOVE step is missing source or target", "normalize_processing_step")
            return None
        step.update({"source": source, "target": target})
        record_processing_step_modification(
            context.get("diagnostics"),
            path,
            item,
            step,
            "normalized MOVE source and target references",
            "normalize_processing_step",
        )
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
        record_processing_step_modification(
            context.get("diagnostics"),
            path,
            item,
            step,
            f"normalized {operation} name and parameter mappings",
            "normalize_processing_step",
        )
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
        record_processing_step_modification(
            context.get("diagnostics"),
            path,
            item,
            step,
            "normalized IF condition and child branches",
            "normalize_processing_step",
        )
        return step
    for key in ("source", "target", "condition", "into"):
        value = item.get(key)
        if value:
            step[key] = normalize_plan_reference(value, context) if key in {"source", "target", "into"} else str(value).strip()
    record_processing_step_modification(
        context.get("diagnostics"),
        path,
        item,
        step,
        f"normalized {operation} scalar fields",
        "normalize_processing_step",
    )
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


def processing_contract_callable_identities(base_prompt=None, callable_metadata=None):
    names = set(callable_identities_from_prompt(base_prompt))
    names.update(str(name or "").strip().upper() for name in normalize_provider_signatures(callable_metadata))
    names.update(callable_catalogue_parameter_index(base_prompt))
    return {name for name in names if name}


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
    if isinstance(nested, dict):
        canonical = dict(nested)
    else:
        canonical = {}
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
                record_empty_normalized_branch(
                    diagnostics,
                    list(path or []) + [index, key],
                    raw_children,
                    item[key],
                    f"{item.get('operation')} {key} branch",
                    function_name="prune_unused_read_steps",
                )
        if item.get("operation") == "READ":
            into = normalize_plan_identifier(item.get("into"))
            later_text = processing_plan_text_blob({"processing_steps": steps[index + 1 :]})
            if into and not re.search(rf"\b{re.escape(into)}\b", later_text, re.IGNORECASE):
                record_processing_step_rejection(
                    diagnostics,
                    list(path or []) + [index],
                    step,
                    f"READ result work area {into} is not referenced by any later step in the same branch",
                    "prune_unused_read_steps",
                )
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


def table_to_work_area_contracts(base_prompt=None):
    result = {}
    for item in ddic_object_contracts_from_prompt(base_prompt).values():
        table = normalize_plan_identifier(item.get("table"))
        work_area = normalize_plan_identifier(item.get("work_area"))
        if table and work_area:
            result[table] = work_area
    return result


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
    return [
        condition
        for condition in conditions or []
        if is_read_lookup_condition(source, into, condition, context)
    ]


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
                record_processing_step_rejection(
                    context.get("diagnostics"),
                    ["CALL_FUNCTION", callable_name, parameter],
                    {parameter: target},
                    "CALL_FUNCTION exporting parameter maps into a DDIC input work area",
                    "normalize_callable_mappings",
                )
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


def callable_parameter_directions(metadata_context=None, callable_metadata=None):
    directions = {}
    for callable_name, signature in normalize_provider_signatures(callable_metadata).items():
        params = signature.get("parameters", {}) if isinstance(signature, dict) else {}
        if isinstance(params, dict):
            for parameter_name, details in params.items():
                direction = str((details or {}).get("direction") or "").strip().lower()
                if direction:
                    directions[(callable_name.upper(), str(parameter_name).upper())] = direction
    catalogue = extract_callable_catalogue(metadata_context)
    for line in str(catalogue or "").splitlines():
        match = re.match(r"^-\s+([A-Z0-9_/=><-]+)\s*:\s*(.*)$", line.strip(), re.IGNORECASE)
        if not match:
            continue
        callable_name = match.group(1).upper()
        for part in split_ddic_field_parts(match.group(2)):
            param_match = re.match(r"([A-Z][A-Z0-9_]{0,29})\s+\[([^\]]+)\]", part.strip(), re.IGNORECASE)
            if not param_match:
                continue
            detail = param_match.group(2).lower()
            if "exporting" in detail or "returning" in detail:
                direction = "exporting"
            elif "importing" in detail or "changing" in detail or "tables" in detail:
                direction = "importing"
            else:
                continue
            directions[(callable_name, param_match.group(1).upper())] = direction
    return directions


def normalize_plan_identifier(value):
    name = str(value or "").strip()
    return name.lower() if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,29}", name) else ""


def normalize_plan_reference(value, context, role=None):
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


def output_field_names_from_requirements(declaration_requirements=None):
    requirements = parse_declaration_requirements_text(declaration_requirements)
    fields = requirements.get("output_structure_fields") if isinstance(requirements, dict) else []
    names = set()
    for field in fields or []:
        name = field.get("name") if isinstance(field, dict) else field
        if name:
            names.add(str(name).strip().upper())
    return names


def output_field_type_map_from_requirements(declaration_requirements=None):
    requirements = parse_declaration_requirements_text(declaration_requirements)
    fields = requirements.get("output_structure_fields") if isinstance(requirements, dict) else []
    result = {}
    for field in fields or []:
        if isinstance(field, dict):
            name = str(field.get("name") or "").strip().upper()
            type_or_like = str(field.get("type_or_like") or "").strip()
        else:
            name = str(field or "").strip().upper()
            type_or_like = ""
        if name:
            result[name] = normalize_type_keyword(type_or_like) if type_or_like else ""
    return result


def validate_processing_plan(
    processing_plan,
    base_prompt=None,
    declaration_requirements=None,
    callable_metadata=None,
    normalization_diagnostics=None,
):
    context = processing_plan_validation_context(
        base_prompt=base_prompt,
        declaration_requirements=declaration_requirements,
        callable_metadata=callable_metadata,
    )
    errors = []
    for item in (normalization_diagnostics or {}).get("rejected_steps") or []:
        reason = str((item or {}).get("reason") or "")
        if "contained child steps but none remained" in reason:
            errors.append(f"nested control-flow branch was not preserved at {format_processing_plan_path((item or {}).get('path'))}: {reason}")
    steps = (processing_plan or {}).get("processing_steps") if isinstance(processing_plan, dict) else None
    if not isinstance(steps, list):
        errors.append("processing_steps must be an array")
        return {"valid": False, "errors": errors}
    validate_processing_steps(steps, context, errors, path=["processing_steps"])
    validate_required_output_steps(steps, context, errors)
    return {"valid": not errors, "errors": dedupe_preserve_order(errors)}


def processing_plan_validation_context(base_prompt=None, declaration_requirements=None, callable_metadata=None):
    ddic = parse_ddic_catalogue(extract_ddic_catalogue(base_prompt))
    contracts = ddic_object_contracts_from_prompt(base_prompt)
    output_names = output_names_for_contract(declaration_requirements)
    output_fields = output_field_names_from_requirements(declaration_requirements)
    return {
        "contracts": contracts,
        "ddic_tables": ddic.get("tables") or {},
        "aliases": processing_plan_contract_aliases(contracts),
        "alias_roles": processing_plan_contract_alias_roles(contracts),
        "globals": processing_plan_allowed_globals(base_prompt, declaration_requirements),
        "processing_variables": processing_variable_names_from_requirements(declaration_requirements),
        "output_names": output_names,
        "output_fields": output_fields,
        "output_field_types": output_field_type_map_from_requirements(declaration_requirements),
        "callable_signatures": {str(name).upper(): value for name, value in normalize_provider_signatures(callable_metadata).items()},
        "callable_catalogue": callable_catalogue_parameter_index(base_prompt),
        "callable_types": callable_parameter_type_index(callable_metadata),
        "callable_identities": processing_contract_callable_identities(base_prompt, callable_metadata),
    }


def processing_plan_contract_aliases(contracts):
    aliases = {}
    for object_name, item in (contracts or {}).items():
        for key in ("structure", "table", "work_area"):
            alias = normalize_plan_identifier(item.get(key))
            if alias:
                aliases[alias] = object_name
    return aliases


def processing_plan_contract_alias_roles(contracts):
    aliases = {}
    for object_name, item in (contracts or {}).items():
        for key in ("structure", "table", "work_area"):
            alias = normalize_plan_identifier(item.get(key))
            if alias:
                aliases[alias] = {"object": object_name, "role": key}
    return aliases


def processing_plan_allowed_globals(base_prompt=None, declaration_requirements=None):
    names = set()
    for item in required_form_global_variables(base_prompt, declaration_requirements=declaration_requirements):
        name = normalize_plan_identifier((item or {}).get("name"))
        if name:
            names.add(name)
    names.update(processing_variable_names_from_requirements(declaration_requirements))
    names.update(selection_parameter_names_from_requirements(declaration_requirements))
    return names


def callable_catalogue_parameter_index(base_prompt=None):
    result = {}
    for line in str(extract_callable_catalogue(base_prompt) or "").splitlines():
        match = re.match(r"^-\s+([A-Z0-9_/=><-]+)\s*:\s*(.*)$", line.strip(), re.IGNORECASE)
        if not match:
            continue
        callable_name = match.group(1).upper()
        result.setdefault(callable_name, {})
        for part in split_ddic_field_parts(match.group(2)):
            param_match = re.match(r"([A-Z][A-Z0-9_]{0,29})(?:\s+\[([^\]]+)\])?", part.strip(), re.IGNORECASE)
            if not param_match:
                continue
            direction = callable_catalogue_direction(param_match.group(2))
            result[callable_name][param_match.group(1).upper()] = {"direction": direction}
    return result


def callable_catalogue_direction(detail):
    text = str(detail or "").lower()
    if "returning" in text or "exporting" in text:
        return "output"
    if "importing" in text or "changing" in text or "tables" in text:
        return "input"
    return ""


def validate_processing_steps(steps, context, errors, path=None):
    for index, step in enumerate(steps or []):
        step_path = list(path or []) + [index]
        if not isinstance(step, dict):
            errors.append(f"{format_processing_plan_path(step_path)} is not an object")
            continue
        operation = str(step.get("operation") or "").upper()
        if operation == "LOOP":
            validate_plan_reference(step.get("source"), context, errors, step_path + ["source"], role="table")
            validate_plan_reference(step.get("into"), context, errors, step_path + ["into"], role="work_area")
            validate_processing_steps(step.get("steps") or [], context, errors, step_path + ["steps"])
        elif operation == "READ":
            validate_plan_reference(step.get("source"), context, errors, step_path + ["source"], role="table")
            validate_plan_reference(step.get("into"), context, errors, step_path + ["into"], role="work_area")
            for cond_index, condition in enumerate(step.get("conditions") or []):
                validate_condition_references(condition, context, errors, step_path + ["conditions", cond_index])
        elif operation == "MOVE":
            validate_plan_reference(step.get("source"), context, errors, step_path + ["source"], role="source")
            validate_plan_reference(step.get("target"), context, errors, step_path + ["target"], role="target")
            validate_move_does_not_use_ddic_work_area_as_temporary_storage(step, context, errors, step_path)
        elif operation in {"CALL_FUNCTION", "CALL_METHOD", "CALL_STATIC_METHOD"}:
            validate_callable_step(step, context, errors, step_path)
        elif operation == "IF":
            for cond_index, condition in enumerate(step.get("conditions") or []):
                validate_condition_references(condition, context, errors, step_path + ["conditions", cond_index])
            validate_processing_steps(step.get("then") or [], context, errors, step_path + ["then"])
            validate_processing_steps(step.get("else") or [], context, errors, step_path + ["else"])
        else:
            for key in ("source", "target", "into"):
                if step.get(key):
                    validate_plan_reference(step.get(key), context, errors, step_path + [key], role=key)
    validate_read_result_safety(steps, context, errors, path or [])


def validate_condition_references(condition, context, errors, path):
    if not isinstance(condition, dict):
        return
    validate_plan_reference(condition.get("left"), context, errors, list(path) + ["left"], role="source")
    if "right" in condition:
        validate_plan_reference(condition.get("right"), context, errors, list(path) + ["right"], role="source")


def validate_callable_step(step, context, errors, path):
    operation = str(step.get("operation") or "CALL_FUNCTION").strip().upper()
    callable_name = callable_identity_for_processing_step(step, context)
    if not callable_name:
        errors.append(f"{format_processing_plan_path(path)} {operation} has no callable identity")
        return
    if operation == "CALL_STATIC_METHOD":
        if not normalize_abap_class_identifier(step.get("class")):
            errors.append(f"{format_processing_plan_path(path + ['class'])} CALL_STATIC_METHOD has no class")
        if not normalize_abap_method_identifier(step.get("method")):
            errors.append(f"{format_processing_plan_path(path + ['method'])} CALL_STATIC_METHOD has no method")
    elif operation == "CALL_METHOD":
        if not normalize_plan_identifier(step.get("object")):
            errors.append(f"{format_processing_plan_path(path + ['object'])} CALL_METHOD has no object")
        if not normalize_abap_method_identifier(step.get("method")):
            errors.append(f"{format_processing_plan_path(path + ['method'])} CALL_METHOD has no method")
    signatures = context.get("callable_signatures") or {}
    catalogue = context.get("callable_catalogue") or {}
    if signatures and callable_name not in signatures:
        errors.append(f"{format_processing_plan_path(path)} references callable {callable_name} not found in verified callable metadata")
    elif catalogue and callable_name not in catalogue:
        errors.append(f"{format_processing_plan_path(path)} references callable {callable_name} not found in callable contract")
    params = callable_parameters_for_validation(callable_name, context)
    for section, direction in (("input_parameters", "input"), ("output_parameters", "output")):
        mappings = step.get(section) or {}
        if not isinstance(mappings, dict):
            errors.append(f"{format_processing_plan_path(path + [section])} must be an object")
            continue
        for parameter, target in mappings.items():
            parameter_name = str(parameter or "").strip().upper()
            validate_callable_parameter_mapping(callable_name, parameter_name, target, direction, params, context, errors, path + [section, parameter_name])
    for key in ("receiving_parameter", "returning_parameter"):
        if step.get(key):
            validate_plan_reference(step.get(key), context, errors, path + [key], role="target")
            validate_callable_returned_value_mapping(callable_name, step.get(key), context, errors, path + [key])


def callable_identity_for_processing_step(step, context=None):
    operation = str((step or {}).get("operation") or "").strip().upper()
    if operation == "CALL_FUNCTION":
        return str((step or {}).get("name") or "").strip().upper()
    if operation == "CALL_STATIC_METHOD":
        name = str((step or {}).get("name") or "").strip().upper().replace("->", "=>")
        if name:
            return name
        class_name = normalize_abap_class_identifier((step or {}).get("class"))
        method_name = normalize_abap_method_identifier((step or {}).get("method"))
        return f"{class_name}=>{method_name}" if class_name and method_name else ""
    if operation == "CALL_METHOD":
        name = str((step or {}).get("name") or "").strip().upper().replace("->", "=>")
        if name:
            return name
        return resolve_instance_method_callable_identity((step or {}).get("object"), (step or {}).get("method"), context)
    return ""


def callable_parameters_for_validation(callable_name, context):
    signatures = context.get("callable_signatures") or {}
    signature = signatures.get(callable_name)
    if isinstance(signature, dict):
        params = {
            str(name).upper(): dict(value or {})
            for name, value in (signature.get("parameters") or {}).items()
            if isinstance(value, dict)
        }
        returning = signature.get("returning")
        if isinstance(returning, dict) and returning.get("name"):
            params[str(returning.get("name")).upper()] = dict(returning)
        return params
    return (context.get("callable_catalogue") or {}).get(callable_name) or {}


def validate_callable_parameter_mapping(callable_name, parameter_name, target, direction, params, context, errors, path):
    if params and parameter_name not in params:
        errors.append(f"{format_processing_plan_path(path)} parameter {parameter_name} is not in verified metadata for {callable_name}")
        return
    parameter = params.get(parameter_name) or {}
    parameter_direction = callable_parameter_validation_direction(parameter)
    if parameter_direction and direction not in parameter_direction:
        errors.append(f"{format_processing_plan_path(path)} parameter {parameter_name} is mapped as {direction} but verified metadata marks it as {parameter_direction}")
    validate_plan_reference(target, context, errors, path, role="target" if direction == "output" else "source")
    if direction == "input":
        validate_callable_input_mapping_source(target, context, errors, path)
    parameter_type = callable_parameter_validation_type(callable_name, parameter_name, parameter, context)
    target_type = processing_plan_reference_type(target, context)
    if parameter_type and target_type and not compatible_processing_plan_types(parameter_type, target_type):
        errors.append(f"{format_processing_plan_path(path)} target {target} type {target_type} is not compatible with {callable_name} parameter {parameter_name} type {parameter_type}")


def validate_callable_returned_value_mapping(callable_name, target, context, errors, path):
    returning = callable_returning_parameter_for_validation(callable_name, context)
    if not returning:
        return
    parameter_type = callable_parameter_validation_type(callable_name, str(returning.get("name") or ""), returning, context)
    target_type = processing_plan_reference_type(target, context)
    if parameter_type and target_type and not compatible_processing_plan_types(parameter_type, target_type):
        errors.append(f"{format_processing_plan_path(path)} target {target} type {target_type} is not compatible with {callable_name} returning parameter type {parameter_type}")


def callable_returning_parameter_for_validation(callable_name, context):
    signature = (context.get("callable_signatures") or {}).get(str(callable_name or "").upper())
    returning = signature.get("returning") if isinstance(signature, dict) else None
    return returning if isinstance(returning, dict) and returning.get("name") else None


def validate_callable_input_mapping_source(value, context, errors, path):
    info = processing_plan_reference_info(value, context)
    if info.get("kind") == "output_field":
        errors.append(
            f"{format_processing_plan_path(path)} CALL_FUNCTION input parameter maps from output record component {value}; "
            "use a valid contract field or declared processing variable instead"
        )
        return
    if info.get("kind") in {"ddic_field", "global", "literal", "system_field"}:
        return
    errors.append(
        f"{format_processing_plan_path(path)} CALL_FUNCTION input parameter must map directly from a valid contract field "
        f"or declared processing variable, got {value}"
    )


def validate_move_does_not_use_ddic_work_area_as_temporary_storage(step, context, errors, path):
    source = step.get("source")
    target = step.get("target")
    source_info = processing_plan_reference_info(source, context)
    target_info = processing_plan_reference_info(target, context)
    if (
        source_info.get("kind") == "ddic_field"
        and target_info.get("kind") == "ddic_field"
        and target_info.get("alias_role") == "work_area"
        and source_info.get("object") != target_info.get("object")
    ):
        errors.append(
            f"{format_processing_plan_path(path + ['target'])} MOVE target {target} uses DDIC work area "
            f"{target_info.get('alias')} as temporary storage for value from {source_info.get('object')}"
        )


def validate_read_result_safety(steps, context, errors, path):
    for index, step in enumerate(steps or []):
        if not isinstance(step, dict) or str(step.get("operation") or "").upper() != "READ":
            continue
        into = normalize_plan_identifier(step.get("into"))
        if not into:
            continue
        if read_work_area_cleared_before(steps, index, into) or successful_read_result_checked_after(steps, index):
            continue
        step_path = list(path or []) + [index]
        errors.append(
            f"{format_processing_plan_path(step_path)} READ result work area {into} is not safely tested; "
            "clear the work area before READ or add an explicit successful-read result check"
        )


def read_work_area_cleared_before(steps, index, into):
    for prior in reversed((steps or [])[:index]):
        if not isinstance(prior, dict):
            continue
        if str(prior.get("operation") or "").upper() == "CLEAR" and normalize_plan_reference(prior.get("target"), {}) == into:
            return True
    return False


def successful_read_result_checked_after(steps, index):
    for later in (steps or [])[index + 1 :]:
        if not isinstance(later, dict):
            continue
        if str(later.get("operation") or "").upper() == "IF":
            for condition in later.get("conditions") or []:
                if is_successful_read_condition(condition):
                    return True
    return False


def is_successful_read_condition(condition):
    if not isinstance(condition, dict):
        return False
    left = normalize_plan_reference(condition.get("left") or condition.get("source"), {})
    right = normalize_plan_reference(condition.get("right") or condition.get("target"), {})
    operator = str(condition.get("operator") or "").strip().upper()
    if operator == "EQ":
        operator = "="
    return operator == "=" and left == "sy-subrc" and right in {"0", "'0'", "`0`"}


def callable_parameter_validation_direction(parameter):
    direction = str((parameter or {}).get("direction") or "").strip().upper()
    if direction in {"IMPORTING"}:
        return {"input"}
    if direction in {"EXPORTING", "RETURNING"}:
        return {"output"}
    if direction in {"CHANGING", "TABLES"}:
        return {"input", "output"}
    direction = str((parameter or {}).get("direction") or "").strip().lower()
    if direction in {"input", "output"}:
        return {direction}
    return set()


def callable_parameter_validation_type(callable_name, parameter_name, parameter, context):
    type_or_like = verified_callable_parameter_type(parameter) if isinstance(parameter, dict) else ""
    if type_or_like:
        return normalize_type_keyword(type_or_like)
    entry = callable_entry_for_pair(callable_name, parameter_name, context.get("callable_types") or {})
    return normalize_type_keyword(entry.get("type_or_like")) if entry else ""


def validate_plan_reference(value, context, errors, path, role=None):
    text = str(value or "").strip()
    if not text or is_plan_literal(text):
        return
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{0,29})[-.]([A-Za-z][A-Za-z0-9_]{0,29})", text)
    if match:
        object_name = match.group(1).lower()
        field_name = match.group(2).upper()
        if object_name == "sy":
            return
        if object_name == (context.get("output_names") or {}).get("work_area"):
            if field_name not in (context.get("output_fields") or set()):
                errors.append(f"{format_processing_plan_path(path)} references output field {field_name} not found in output structure contract")
            return
        ddic_object = (context.get("aliases") or {}).get(object_name)
        if not ddic_object:
            errors.append(f"{format_processing_plan_path(path)} references undeclared object {object_name}")
            return
        fields = (context.get("ddic_tables") or {}).get(ddic_object, {}).get("fields") or []
        if fields and field_name not in {field.get("name") for field in fields}:
            errors.append(f"{format_processing_plan_path(path)} references field {ddic_object}-{field_name} not found in verified DDIC metadata")
        return
    identifier = normalize_plan_identifier(text)
    if not identifier:
        return
    contract_values = context.get("contracts") or {}
    if role == "table" and contract_values and identifier not in {str(item.get("table") or "").lower() for item in contract_values.values()}:
        errors.append(f"{format_processing_plan_path(path)} references table {identifier} not found in generation contract")
    elif role == "work_area" and contract_values and identifier not in {str(item.get("work_area") or "").lower() for item in contract_values.values()}:
        errors.append(f"{format_processing_plan_path(path)} references work area {identifier} not found in generation contract")
    elif (context.get("globals") or set()) and identifier.startswith(GLOBAL_STYLE_PREFIXES) and identifier not in (context.get("globals") or set()):
        errors.append(f"{format_processing_plan_path(path)} references undeclared global object {identifier}")


def processing_plan_reference_info(value, context):
    text = str(value or "").strip()
    if not text:
        return {"kind": "empty"}
    if is_plan_literal(text):
        return {"kind": "literal"}
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{0,29})[-.]([A-Za-z][A-Za-z0-9_]{0,29})", text)
    if match:
        alias = match.group(1).lower()
        field = match.group(2).upper()
        output_names = context.get("output_names") or {}
        if alias == "sy":
            return {"kind": "system_field", "alias": alias, "field": field}
        if alias == output_names.get("work_area"):
            return {"kind": "output_field", "alias": alias, "field": field}
        role = (context.get("alias_roles") or {}).get(alias)
        if role:
            return {
                "kind": "ddic_field",
                "alias": alias,
                "alias_role": role.get("role"),
                "object": role.get("object"),
                "field": field,
            }
        return {"kind": "unknown_field", "alias": alias, "field": field}
    identifier = normalize_plan_identifier(text)
    if identifier and identifier in (context.get("globals") or set()):
        return {"kind": "global", "name": identifier}
    if identifier:
        return {"kind": "identifier", "name": identifier}
    return {"kind": "expression", "value": text}


def validate_required_output_steps(steps, context, errors):
    output_names = context.get("output_names") or {}
    if not output_names:
        return
    all_steps = []
    collect_processing_plan_steps(steps, all_steps)
    uses_output = any(processing_step_references_output(step, output_names) for step in all_steps)
    if not uses_output:
        return
    has_clear = any(str(step.get("operation") or "").upper() == "CLEAR" and normalize_plan_reference(step.get("target"), {}) == output_names["work_area"] for step in all_steps)
    has_append = any(
        str(step.get("operation") or "").upper() == "APPEND"
        and normalize_plan_reference(step.get("source"), {}) == output_names["work_area"]
        and normalize_plan_reference(step.get("target"), {}) == output_names["table"]
        for step in all_steps
    )
    if not has_clear:
        errors.append(f"required output creation step CLEAR {output_names['work_area']} is missing")
    if not has_append:
        errors.append(f"required output append step APPEND {output_names['work_area']} TO {output_names['table']} is missing")


def processing_step_references_output(step, output_names):
    if not isinstance(step, dict):
        return False
    needle = output_names.get("work_area")
    table = output_names.get("table")
    text = processing_plan_text_blob(step).lower()
    return bool(needle and re.search(rf"\b{re.escape(needle.lower())}\b", text)) or bool(table and re.search(rf"\b{re.escape(table.lower())}\b", text))


def processing_plan_reference_type(value, context):
    text = str(value or "").strip()
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{0,29})[-.]([A-Za-z][A-Za-z0-9_]{0,29})", text)
    if not match:
        return ""
    object_name = match.group(1).lower()
    field_name = match.group(2).upper()
    output_names = context.get("output_names") or {}
    if object_name == output_names.get("work_area"):
        return (context.get("output_field_types") or {}).get(field_name, "")
    ddic_object = (context.get("aliases") or {}).get(object_name)
    if ddic_object:
        return f"TYPE {ddic_object}-{field_name}"
    return ""


def compatible_processing_plan_types(left, right):
    left_type = normalize_type_for_comparison(left)
    right_type = normalize_type_for_comparison(right)
    if not left_type or not right_type:
        return True
    if left_type == right_type:
        return True
    left_field = left_type.rsplit("-", 1)[-1] if "-" in left_type else ""
    right_field = right_type.rsplit("-", 1)[-1] if "-" in right_type else ""
    return bool(left_field and right_field and left_field == right_field)


def normalize_type_for_comparison(value):
    text = normalize_type_keyword(value).upper()
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^(TYPE|LIKE)\s+", "", text)


def is_plan_literal(value):
    text = str(value or "").strip()
    return bool(re.fullmatch(r"'.*'|`.*`|\d+(?:\.\d+)?", text))


def format_processing_plan_path(path):
    if not path:
        return "processing_plan"
    return "processing_plan." + ".".join(str(item) for item in path)


def is_placeholder_condition(value):
    condition = re.sub(r"\s+", "", str(value or "").strip().lower())
    return condition in {"1=0", "0=1", "true=false", "false=true"}


def is_ddic_input_work_area_reference(value, context):
    object_prefix = str(value or "").partition("-")[0].lower()
    work_areas = set(((context or {}).get("table_to_work_area") or {}).values())
    return object_prefix in work_areas


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


def raw_response_json(result):
    if isinstance(result, dict):
        if "raw_response_json" in result:
            return result.get("raw_response_json")
        return processing_plan_diagnostic_snapshot(result)
    return processing_plan_diagnostic_snapshot(result)


def record_empty_normalized_branch(diagnostics, path, raw_children, normalized_children, branch_name, function_name="normalize_processing_step"):
    if raw_children and not normalized_children:
        record_processing_step_rejection(
            diagnostics,
            path,
            raw_children,
            f"{branch_name} contained child steps but none remained after normalization",
            function_name,
        )


def load_declaration_requirements_prompt(path=DECLARATION_REQUIREMENTS_PROMPT_PATH):
    return Path(path).read_text(encoding="utf-8")


def load_processing_plan_prompt(path=PROCESSING_PLAN_PROMPT_PATH):
    return Path(path).read_text(encoding="utf-8")


def declaration_requirements_extraction_prompt(metadata_context=None, callable_metadata=None):
    prompt = load_declaration_requirements_prompt()
    ddic_catalogue = extract_ddic_catalogue(metadata_context)
    callable_catalogue = extract_callable_catalogue(metadata_context)
    if not callable_catalogue and callable_metadata:
        callable_catalogue = render_callable_metadata_for_extraction(callable_metadata)
    blocks = []
    if ddic_catalogue:
        blocks.append("Available SAP DDIC metadata:\n" + ddic_catalogue)
    if callable_catalogue:
        blocks.append("Available SAP callable metadata:\n" + callable_catalogue)
    if not blocks:
        return prompt
    return f"{prompt.rstrip()}\n\n" + "\n\n".join(blocks).rstrip() + "\n"


def processing_plan_extraction_prompt(metadata_context=None, callable_metadata=None, processing_contract=None):
    prompt = load_processing_plan_prompt()
    contract = processing_contract or build_processing_contract(
        metadata_context=metadata_context,
        callable_metadata=callable_metadata,
    )
    rendered_contract = render_processing_contract_for_prompt(contract)
    blocks = [rendered_contract] if rendered_contract else []
    if not blocks:
        return prompt
    return f"{prompt.rstrip()}\n\n" + "\n\n".join(blocks).rstrip() + "\n"


def build_processing_contract(metadata_context=None, callable_metadata=None, declaration_requirements=None, processing_rules_text=None, ddic_metadata=None):
    ddic_catalogue = processing_contract_ddic_catalogue(metadata_context, ddic_metadata)
    object_contracts = ddic_object_contracts_from_prompt(metadata_context)
    callable_identities = callable_identities_from_prompt(metadata_context)
    output_contract = filtered_output_contract_from_requirements(declaration_requirements)
    discovered_dependencies = discover_processing_rule_dependencies(
        processing_rules_text,
        ddic_catalogue=ddic_catalogue,
        object_contracts=object_contracts,
        callable_identities=callable_identities,
        callable_metadata=callable_metadata,
        callable_catalogue=extract_callable_catalogue(metadata_context),
        output_contract=output_contract,
        declaration_requirements=declaration_requirements,
    )
    required_callables = dedupe_preserve_order(discovered_dependencies["callables"])
    required_ddic_fields = required_ddic_fields_for_processing_contract(output_contract, required_callables, callable_metadata)
    merge_required_ddic_fields(required_ddic_fields, discovered_dependencies["ddic_fields"])
    required_ddic_objects = dedupe_preserve_order(
        list(required_ddic_fields)
        + discovered_dependencies["ddic_objects"]
        + [
            alias["object"]
            for alias in discovered_dependencies["internal_tables"] + discovered_dependencies["work_areas"]
            if alias.get("object")
        ]
    )
    filtered_metadata = filtered_ddic_metadata_for_processing_contract(ddic_catalogue, required_ddic_objects, required_ddic_fields)
    filtered_callables = filtered_callable_metadata_for_processing_contract(
        required_callables,
        callable_metadata,
        extract_callable_catalogue(metadata_context),
    )
    contract = {
        "ddic_objects": {name: object_contracts[name] for name in object_contracts if name in required_ddic_objects},
        "metadata": filtered_metadata,
        "callables": filtered_callables,
        "output": output_contract,
        "selection_parameters": discovered_dependencies["selection_parameters"],
        "processing_variables": discovered_dependencies["processing_variables"],
        "system_fields": discovered_dependencies["system_fields"],
        "dependencies": discovered_dependencies.get("_references") or [],
    }
    missing_dependencies = missing_processing_rule_dependencies(
        discovered_dependencies,
        contract,
        ddic_catalogue=ddic_catalogue,
        callable_metadata=callable_metadata,
        callable_catalogue=extract_callable_catalogue(metadata_context),
    )
    validation_errors = validate_processing_contract(
        contract,
        ddic_catalogue=ddic_catalogue,
        required_ddic_objects=required_ddic_objects,
        required_ddic_fields=required_ddic_fields,
        callable_identities=required_callables,
    )
    validation_errors.extend(missing_dependency_messages(missing_dependencies))
    validation_errors = dedupe_preserve_order(validation_errors)
    return {
        "final_processing_contract": contract,
        "filtered_metadata": filtered_metadata,
        "filtered_callable_metadata": filtered_callables,
        "filtered_output_contract": output_contract,
        "processing_rules_text": str(processing_rules_text or ""),
        "discovered_dependencies": discovered_dependencies,
        "missing_dependencies": missing_dependencies,
        "llm_call_allowed": not validation_errors,
        "validation_errors": validation_errors,
    }


def processing_contract_diagnostics(contract):
    return {
        "processing_rules_text": (contract or {}).get("processing_rules_text") or "",
        "discovered_dependencies": (contract or {}).get("discovered_dependencies") or {},
        "final_processing_contract": (contract or {}).get("final_processing_contract") or {},
        "missing_dependencies": (contract or {}).get("missing_dependencies") or [],
        "llm_call_allowed": bool((contract or {}).get("llm_call_allowed")),
        "filtered_metadata_supplied": (contract or {}).get("filtered_metadata") or {},
        "filtered_callable_metadata_supplied": (contract or {}).get("filtered_callable_metadata") or {},
        "filtered_output_contract_supplied": (contract or {}).get("filtered_output_contract") or [],
        "validation_errors": (contract or {}).get("validation_errors") or [],
        "validation_error_type": "infrastructure_metadata_contract" if (contract or {}).get("validation_errors") else None,
    }


def render_processing_contract_for_prompt(contract):
    diagnostics = processing_contract_diagnostics(contract)
    return (
        "Processing contract supplied by validated application artefacts:\n"
        + json.dumps(diagnostics["final_processing_contract"], indent=2, sort_keys=True)
        + "\n\n"
        + "Use only the objects, fields, callables, processing variables, and output fields in this contract.\n"
        + "When a processing variable is supplied, use it for that resolved value instead of using DDIC work areas or output-record components as temporary storage."
    )


def filtered_output_contract_from_requirements(declaration_requirements=None):
    requirements = parse_declaration_requirements_text(declaration_requirements)
    fields = requirements.get("output_structure_fields") if isinstance(requirements, dict) else []
    result = []
    for field in fields or []:
        if isinstance(field, dict):
            name = str(field.get("name") or "").strip()
            type_or_like = str(field.get("type_or_like") or "").strip()
            source = str(field.get("source") or field.get("source_field") or "").strip()
        else:
            name = str(field or "").strip()
            type_or_like = ""
            source = ""
        if name:
            item = {"name": name.upper()}
            if type_or_like:
                item["type_or_like"] = normalize_type_keyword(type_or_like)
            if source:
                item["source"] = source
            result.append(item)
    return result


def discover_processing_rule_dependencies(
    processing_rules_text=None,
    ddic_catalogue=None,
    object_contracts=None,
    callable_identities=None,
    callable_metadata=None,
    callable_catalogue=None,
    output_contract=None,
    declaration_requirements=None,
):
    text = str(processing_rules_text or "")
    lines = processing_rule_lines(text)
    alias_index = processing_contract_alias_index(object_contracts)
    output_fields = {str(item.get("name") or "").upper(): item for item in output_contract or [] if isinstance(item, dict)}
    selection_fields = selection_parameter_names_from_requirements(declaration_requirements)
    callable_index = processing_contract_callable_index(callable_identities, callable_metadata, callable_catalogue)
    object_method_callables = processing_rule_object_method_callables(text, callable_index)
    field_index = processing_contract_ddic_field_index(ddic_catalogue)
    dependencies = {
        "ddic_objects": [],
        "internal_tables": [],
        "work_areas": [],
        "ddic_fields": {},
        "system_fields": [],
        "callables": [],
        "callable_parameters": {},
        "output_fields": [],
        "selection_parameters": [],
        "processing_variables": [],
        "_references": [],
    }
    for line in lines:
        scan_processing_rule_aliases(line, alias_index, dependencies)
        scan_processing_rule_field_references(line, alias_index, field_index, dependencies)
        scan_processing_rule_unqualified_fields(line, field_index, dependencies)
        scan_processing_rule_callables(line, callable_index, dependencies, object_method_callables)
        scan_processing_rule_output_fields(line, output_fields, dependencies)
        scan_processing_rule_selection_parameters(line, selection_fields, dependencies)
    dependencies["processing_variables"] = discover_processing_rule_variables(lines)
    dependencies["ddic_fields"] = {
        object_name: sorted(fields)
        for object_name, fields in dependencies["ddic_fields"].items()
    }
    dependencies["callable_parameters"] = {
        callable_name: sorted(parameters)
        for callable_name, parameters in dependencies["callable_parameters"].items()
    }
    return dependencies


def processing_rule_lines(text):
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def processing_contract_alias_index(object_contracts=None):
    aliases = {}
    for object_name, contract in (object_contracts or {}).items():
        object_key = str(object_name or "").upper()
        aliases[object_key.lower()] = {"kind": "ddic_object", "name": object_key, "object": object_key}
        for kind, field in (("structure", "structure"), ("internal_table", "table"), ("work_area", "work_area")):
            value = str((contract or {}).get(field) or "").strip()
            if value:
                aliases[value.lower()] = {"kind": kind, "name": value, "object": object_key}
    return aliases


def processing_contract_callable_index(callable_identities=None, callable_metadata=None, callable_catalogue=None):
    index = {}
    for name in callable_identities or []:
        key = str(name or "").strip().upper()
        if key:
            index.setdefault(key, set())
    for callable_name, signature in normalize_provider_signatures(callable_metadata).items():
        key = str(callable_name or "").strip().upper()
        if not key:
            continue
        index.setdefault(key, set())
        params = signature.get("parameters", {}) if isinstance(signature, dict) else {}
        if isinstance(params, dict):
            for parameter_name in params:
                index[key].add(str(parameter_name or "").strip().upper())
        returning = signature.get("returning") if isinstance(signature, dict) else None
        if isinstance(returning, dict) and returning.get("name"):
            index[key].add(str(returning.get("name")).strip().upper())
    catalogue = callable_catalogue_parameter_index("SAP callable signature catalogue:\n" + str(callable_catalogue or ""))
    for callable_name, params in catalogue.items():
        index.setdefault(callable_name, set()).update(params.keys())
    return index


def processing_contract_ddic_field_index(ddic_catalogue=None):
    result = {}
    for object_name, table in ((ddic_catalogue or {}).get("tables") or {}).items():
        fields = set()
        for field in table.get("fields") or []:
            field_name = str(field.get("name") or "").strip().upper()
            if field_name:
                fields.add(field_name)
        result[str(object_name or "").upper()] = fields
    return result


def selection_parameter_names_from_requirements(declaration_requirements=None):
    requirements = parse_declaration_requirements_text(declaration_requirements)
    result = []
    if not isinstance(requirements, dict):
        return result
    for value in requirements.get("selection_parameters") or []:
        name = normalize_abap_identifier(value)
        if name:
            append_unique(result, name)
    for section in ("parameters", "select_options"):
        for item in requirements.get(section) or []:
            if isinstance(item, dict):
                name = normalize_abap_identifier(item.get("name"))
                if name:
                    append_unique(result, name)
    return result


def scan_processing_rule_aliases(line, alias_index, dependencies):
    for token in processing_rule_identifier_tokens(line):
        alias = alias_index.get(token.lower())
        if alias:
            add_processing_rule_alias_dependency(dependencies, alias, line)
        elif re.fullmatch(r"t_[A-Za-z0-9_]+", token, re.IGNORECASE):
            add_processing_rule_reference(dependencies, "internal_table", token, "", line)
        elif re.fullmatch(r"(?:st|w|wa|ls|gs)_[A-Za-z0-9_]+", token, re.IGNORECASE):
            add_processing_rule_reference(dependencies, "work_area", token, "", line)


def scan_processing_rule_field_references(line, alias_index, field_index, dependencies):
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_/]{1,29})[-.]([A-Za-z][A-Za-z0-9_]{1,29})\b", line):
        left = match.group(1)
        field_name = match.group(2).upper()
        alias = alias_index.get(left.lower())
        if is_abap_system_field_reference(left, field_name):
            add_processing_rule_system_field(dependencies, left, field_name, line)
        elif alias and alias.get("object"):
            add_processing_rule_ddic_field(dependencies, alias["object"], field_name, line)
            add_processing_rule_alias_dependency(dependencies, alias, line)
        elif left.upper() in field_index:
            add_processing_rule_ddic_field(dependencies, left.upper(), field_name, line)
        elif not is_processing_rule_local_reference_prefix(left) and is_strong_processing_rule_ddic_object(left):
            add_processing_rule_ddic_field(dependencies, left.upper(), field_name, line)


def scan_processing_rule_unqualified_fields(line, field_index, dependencies):
    object_names = processing_rule_objects_in_line(line, field_index)
    if not object_names:
        return
    tokens = {token.upper() for token in processing_rule_identifier_tokens(line)}
    for object_name in object_names:
        for field_name in field_index.get(object_name) or set():
            if field_name in tokens:
                add_processing_rule_ddic_field(dependencies, object_name, field_name, line)


def processing_rule_objects_in_line(line, field_index):
    tokens = {token.upper() for token in processing_rule_identifier_tokens(line)}
    return [object_name for object_name in field_index if object_name in tokens]


def scan_processing_rule_callables(line, callable_index, dependencies, object_method_callables=None):
    upper_line = line.upper()
    resolved_object_callables = object_method_callables_for_line(line, object_method_callables)
    object_method_names = object_method_names_for_line(line) if resolved_object_callables else set()
    ambiguous_aliases = ambiguous_callable_aliases(callable_index)
    for callable_name, parameters in callable_index.items():
        exact_mention = processing_rule_mentions_exact_callable(line, callable_name)
        if (
            resolved_object_callables
            and callable_method_name(callable_name) in object_method_names
            and callable_name not in resolved_object_callables
            and not exact_mention
        ):
            continue
        if (
            not processing_rule_mentions_callable(line, callable_name, ambiguous_aliases=ambiguous_aliases)
            and callable_name not in resolved_object_callables
        ):
            continue
        append_unique(dependencies["callables"], callable_name)
        add_processing_rule_reference(dependencies, "callable", callable_name, "", line)
        for parameter_name in parameters:
            if re.search(rf"\b{re.escape(parameter_name)}\b", upper_line):
                dependencies["callable_parameters"].setdefault(callable_name, set()).add(parameter_name)
                add_processing_rule_reference(dependencies, "callable_parameter", parameter_name, callable_name, line)


def processing_rule_mentions_callable(line, callable_name, ambiguous_aliases=None):
    upper_line = str(line or "").upper()
    name = str(callable_name or "").strip().upper()
    if not name:
        return False
    if processing_rule_mentions_exact_callable(line, name):
        return True
    ambiguous_aliases = set(ambiguous_aliases or [])
    aliases = callable_name_aliases(name)
    return any(
        alias not in ambiguous_aliases and re.search(rf"\b{re.escape(alias)}\b", upper_line)
        for alias in aliases
    )


def processing_rule_mentions_exact_callable(line, callable_name):
    upper_line = str(line or "").upper()
    name = str(callable_name or "").strip().upper()
    return bool(name and re.search(rf"\b{re.escape(name)}\b", upper_line))


def ambiguous_callable_aliases(callable_index):
    owners = {}
    for callable_name in callable_index or {}:
        for alias in callable_name_aliases(callable_name):
            owners.setdefault(alias, set()).add(str(callable_name or "").strip().upper())
    return {alias for alias, names in owners.items() if len(names) > 1}


def callable_name_aliases(callable_name):
    name = str(callable_name or "").upper()
    if re.search(r"=>|->|~", name):
        part = re.split(r"=>|->|~", name)[-1].strip("_")
        shortened = re.sub(r"^(?:Z|Y|CL|IF)_", "", part)
        return dedupe_preserve_order([alias for alias in (part, shortened) if len(alias) >= 3 and alias != name])
    aliases = []
    for part in re.split(r"=>|->|~|/", name):
        part = part.strip("_")
        if len(part) >= 3:
            aliases.append(part)
        shortened = re.sub(r"^(?:Z|Y|CL|IF)_", "", part)
        if len(shortened) >= 3:
            aliases.append(shortened)
    return dedupe_preserve_order([alias for alias in aliases if alias != callable_name])


def processing_rule_object_method_callables(text, callable_index):
    object_classes = processing_rule_reference_object_classes(text)
    callables = set(callable_index or {})
    result = {}
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]{1,29})\s*->\s*([A-Za-z][A-Za-z0-9_]{1,29})\b", str(text or "")):
        object_name = match.group(1).lower()
        method_name = match.group(2).upper()
        object_identity = f"{object_name.upper()}=>{method_name}"
        if object_identity in callables:
            result.setdefault(match.group(0).upper(), set()).add(object_identity)
        class_name = object_classes.get(object_name)
        if not class_name:
            continue
        identity = f"{class_name}=>{method_name}"
        if identity in callables:
            result.setdefault(match.group(0).upper(), set()).add(identity)
    return result


def object_method_callables_for_line(line, object_method_callables=None):
    matches = set()
    upper_line = str(line or "").upper()
    for expression, identities in (object_method_callables or {}).items():
        if expression in upper_line:
            matches.update(identities)
    return matches


def object_method_names_for_line(line):
    return {
        match.group(2).upper()
        for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]{1,29})\s*->\s*([A-Za-z][A-Za-z0-9_]{1,29})\b", str(line or ""))
    }


def callable_method_name(callable_name):
    parts = re.split(r"=>|->|~", str(callable_name or "").strip().upper())
    return parts[-1] if len(parts) > 1 else ""


def processing_rule_reference_object_classes(text):
    result = {}
    declaration_text = re.sub(r'"[^\n]*', "", str(text or ""))
    for statement in re.finditer(r"\bDATA\s*:?\s*([\s\S]*?)[.]", declaration_text, re.IGNORECASE):
        for part in split_ddic_field_parts(statement.group(1)):
            match = re.search(
                r"\b([A-Za-z][A-Za-z0-9_]{1,29})\b\s+TYPE\s+REF\s+TO\s+([A-Za-z][A-Za-z0-9_]{1,29})\b",
                part,
                re.IGNORECASE,
            )
            if match:
                result[match.group(1).lower()] = match.group(2).upper()
    return result


def scan_processing_rule_output_fields(line, output_fields, dependencies):
    upper_line = line.upper()
    for field_name in output_fields:
        if re.search(rf"\b{re.escape(field_name)}\b", upper_line):
            append_unique(dependencies["output_fields"], field_name)
            add_processing_rule_reference(dependencies, "output_field", field_name, "", line)


def scan_processing_rule_selection_parameters(line, selection_fields, dependencies):
    for name in selection_fields or []:
        if re.search(rf"\b{re.escape(name)}\b", line, re.IGNORECASE):
            append_unique(dependencies["selection_parameters"], name)
            add_processing_rule_reference(dependencies, "selection_parameter", name, "", line)
    for name in inferred_selection_parameter_names(line):
        append_unique(dependencies["selection_parameters"], name)
        add_processing_rule_reference(dependencies, "selection_parameter", name, "", line)


def inferred_selection_parameter_names(line):
    result = []
    for token in processing_rule_identifier_tokens(line):
        name = normalize_abap_identifier(token)
        if name and re.fullmatch(r"[ps]_[a-z][a-z0-9_]{0,27}", name):
            append_unique(result, name)
    return result


def discover_processing_rule_variables(lines):
    candidates = {}
    pending_terms = []
    for line in lines or []:
        terms = processing_rule_value_terms(line)
        for term in terms:
            candidates.setdefault(term, {"term": term, "source_fields": [], "used_by": []})
        refs = processing_rule_ddic_field_refs(line)
        if refs:
            source_terms = processing_rule_source_terms(line, refs, terms, candidates, pending_terms)
            for term in source_terms:
                candidate = candidates.setdefault(term, {"term": term, "source_fields": [], "used_by": []})
                for ref in refs:
                    append_unique(candidate["source_fields"], ref)
            pending_terms = []
        elif terms and re.search(r"\b(use|source|found|fallback|otherwise|resolved|determin(?:e|ed)|derive(?:d)?)\b", line, re.IGNORECASE):
            pending_terms = terms
        usage_terms = processing_rule_resolved_usage_terms(line, candidates)
        for term in usage_terms:
            candidate = candidates.setdefault(term, {"term": term, "source_fields": [], "used_by": []})
            append_unique(candidate["used_by"], line)
    result = []
    for term, candidate in candidates.items():
        sources = candidate.get("source_fields") or []
        usages = candidate.get("used_by") or []
        if len(sources) < 2 or not usages:
            continue
        name = processing_variable_name_for_term(term)
        first_source = ddic_reference_parts(sources[0])
        type_or_like = f"TYPE {first_source[0]}-{first_source[1]}" if first_source else ""
        item = {
            "name": name,
            "description": f"Resolved value for {term}",
            "source_fields": sources,
            "used_by": usages,
        }
        if type_or_like:
            item["type_or_like"] = type_or_like
            item["declaration"] = f"DATA {name} {type_or_like}."
        result.append(item)
    return result


def processing_rule_value_terms(line):
    terms = []
    text = re.sub(r"[*_`]", "", str(line or ""))
    patterns = [
        r"\b(?:determine|resolve|derive)\s+(?:the\s+|a\s+|an\s+)?([A-Za-z][A-Za-z0-9_ /\-]{1,80}?)(?:[.:;,]|$)",
        r"\b(?:the\s+|a\s+|an\s+)?([A-Za-z][A-Za-z0-9_ /\-]{1,80}?)\s+is\s+(?:found|resolved|determined|derived)\b",
        r"\b(?:resolved|determined|derived)\s+([A-Za-z][A-Za-z0-9_ /\-]{1,80}?)(?:[.:;,]|$)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            term = normalize_processing_variable_term(match.group(1))
            if term:
                append_unique(terms, term)
    return terms


def processing_rule_source_terms(line, refs, terms, candidates, pending_terms):
    result = []
    for term in list(terms or []) + list(pending_terms or []):
        append_unique(result, term)
    text = str(line or "")
    if not re.search(r"\b(use|source|found|fallback|otherwise|obtained|resolved|determin(?:e|ed)|derive(?:d)?)\b", text, re.IGNORECASE):
        return result
    known_terms = list((candidates or {}).keys())
    normalized_text = " " + normalize_processing_variable_term(text) + " "
    for ref in refs or []:
        field_term = normalize_processing_variable_term(ref.rsplit("-", 1)[-1])
        if field_term:
            append_unique(result, field_term)
        for term in known_terms:
            if f" {term} " in normalized_text or field_term == term or term.endswith(f" {field_term}") or field_term.endswith(f" {term}"):
                append_unique(result, term)
    return result


def processing_rule_resolved_usage_terms(line, candidates):
    result = []
    text = re.sub(r"[*_`]", "", str(line or ""))
    for match in re.finditer(r"\b(?:resolved|determined|derived)\s+([A-Za-z][A-Za-z0-9_ /\-]{1,80}?)(?:[.:;,]|$)", text, re.IGNORECASE):
        term = normalize_processing_variable_term(match.group(1))
        if term:
            append_unique(result, term)
    normalized_text = " " + normalize_processing_variable_term(text) + " "
    for term in (candidates or {}):
        if re.search(r"\b(populate|map|assign|move|pass|use)\b", text, re.IGNORECASE) and f" {term} " in normalized_text:
            append_unique(result, term)
    return result


def processing_rule_ddic_field_refs(line):
    result = []
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_/]{1,29})[-.]([A-Za-z][A-Za-z0-9_]{1,29})\b", str(line or "")):
        left = match.group(1)
        if is_processing_rule_local_reference_prefix(left) or left.lower() == "sy":
            continue
        if is_strong_processing_rule_ddic_object(left):
            append_unique(result, f"{left.upper()}-{match.group(2).upper()}")
    return result


def is_strong_processing_rule_ddic_object(value):
    text = str(value or "")
    upper = text.upper()
    return bool(text) and (text == upper or upper.startswith(("/", "Z", "Y")) or bool(re.search(r"[0-9_]", text)))


def normalize_processing_variable_term(value):
    text = re.sub(r"[^0-9A-Za-z_]+", " ", str(value or "").strip()).lower()
    text = re.sub(r"\b(the|a|an|value|field|from|to|using|with|as|and|or|then|else|if|when|where|record|output)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def processing_variable_name_for_term(term):
    slug = re.sub(r"[^0-9a-z]+", "_", normalize_processing_variable_term(term)).strip("_")
    if not slug:
        slug = "value"
    parts = [part for part in slug.split("_") if part]
    if len(parts) > 4:
        parts = parts[-4:]
    return normalize_abap_identifier("lv_resolved_" + "_".join(parts))


def processing_rule_identifier_tokens(line):
    return re.findall(r"\b[A-Za-z][A-Za-z0-9_/]{1,29}\b", str(line or ""))


def is_processing_rule_local_reference_prefix(value):
    return str(value or "").lower().startswith(("t_", "st_", "w_", "wa_", "lt_", "ls_", "gt_", "gs_", "lv_", "gv_"))


def is_abap_system_field_reference(object_name, field_name=None):
    alias = str(object_name or "").strip().lower()
    field = str(field_name or "").strip()
    return alias == "sy" and bool(field) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,29}", field))


def add_processing_rule_system_field(dependencies, object_name, field_name, line):
    alias = str(object_name or "").strip().lower()
    field = str(field_name or "").strip().upper()
    if not is_abap_system_field_reference(alias, field):
        return
    reference = f"{alias}-{field.lower()}"
    append_unique(dependencies["system_fields"], reference)
    add_processing_rule_reference(dependencies, "system_field", reference, "", line)


def add_processing_rule_alias_dependency(dependencies, alias, line):
    kind = alias.get("kind")
    object_name = alias.get("object")
    if object_name:
        append_unique(dependencies["ddic_objects"], object_name)
    if kind == "internal_table":
        add_alias_entry(dependencies["internal_tables"], alias)
    elif kind == "work_area":
        add_alias_entry(dependencies["work_areas"], alias)
    add_processing_rule_reference(dependencies, kind, alias.get("name"), object_name, line)


def add_alias_entry(entries, alias):
    item = {"name": alias.get("name"), "object": alias.get("object")}
    if item not in entries:
        entries.append(item)


def add_processing_rule_ddic_field(dependencies, object_name, field_name, line):
    object_key = str(object_name or "").upper()
    field_key = str(field_name or "").upper()
    if not object_key or not field_key:
        return
    append_unique(dependencies["ddic_objects"], object_key)
    dependencies["ddic_fields"].setdefault(object_key, set()).add(field_key)
    add_processing_rule_reference(dependencies, "ddic_field", field_key, object_key, line)


def add_processing_rule_reference(dependencies, kind, name, parent, line):
    item = {
        "kind": str(kind or ""),
        "name": str(name or ""),
        "parent": str(parent or ""),
        "processing_rule_text": str(line or "").strip(),
    }
    if item not in dependencies["_references"]:
        dependencies["_references"].append(item)


def merge_required_ddic_fields(required, discovered):
    for object_name, fields in (discovered or {}).items():
        required.setdefault(object_name, set()).update(fields or [])


def missing_processing_rule_dependencies(discovered, contract, ddic_catalogue=None, callable_metadata=None, callable_catalogue=None):
    missing = []
    metadata = (contract or {}).get("metadata") or {}
    contract_objects = (contract or {}).get("ddic_objects") or {}
    contract_fields = {
        object_name: {field.get("name") for field in (payload or {}).get("fields") or []}
        for object_name, payload in metadata.items()
    }
    for ref in (discovered or {}).get("_references") or []:
        kind = ref.get("kind")
        name = ref.get("name")
        parent = ref.get("parent")
        if kind == "ddic_object" and name not in contract_objects:
            missing.append(missing_processing_rule_dependency("ddic_object", name, "", ref))
        elif kind == "ddic_field" and parent not in contract_objects:
            missing.append(missing_processing_rule_dependency("ddic_object", parent, "", ref))
        elif kind == "ddic_field" and name not in contract_fields.get(parent, set()):
            missing.append(missing_processing_rule_dependency("ddic_field", f"{parent}-{name}", parent, ref))
        elif kind == "internal_table" and ref.get("parent") and ref.get("parent") not in contract_objects:
            missing.append(missing_processing_rule_dependency("internal_table", name, parent, ref))
        elif kind == "work_area" and ref.get("parent") and ref.get("parent") not in contract_objects:
            missing.append(missing_processing_rule_dependency("work_area", name, parent, ref))
        elif kind == "callable" and name not in ((contract or {}).get("callables") or {}):
            missing.append(missing_processing_rule_dependency("callable", name, "", ref))
        elif kind == "callable_parameter" and not processing_contract_has_callable_parameter(contract, parent, name):
            missing.append(missing_processing_rule_dependency("callable_parameter", name, parent, ref))
    return dedupe_missing_dependencies(missing)


def processing_contract_has_callable_parameter(contract, callable_name, parameter_name):
    callable_entry = ((contract or {}).get("callables") or {}).get(str(callable_name or "").upper()) or {}
    params = callable_entry.get("parameters") if isinstance(callable_entry, dict) else {}
    return not isinstance(params, dict) or str(parameter_name or "").upper() in {str(name).upper() for name in params}


def missing_processing_rule_dependency(kind, name, parent, reference):
    return {
        "kind": kind,
        "name": name,
        "parent": parent or "",
        "processing_rule_text": (reference or {}).get("processing_rule_text") or "",
    }


def dedupe_missing_dependencies(items):
    result = []
    seen = set()
    for item in items or []:
        key = (item.get("kind"), item.get("name"), item.get("parent"), item.get("processing_rule_text"))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def missing_dependency_messages(missing):
    messages = []
    for item in missing or []:
        messages.append(
            "processing contract dependency missing: "
            f"{item.get('kind')} {item.get('name')} required by processing rule: {item.get('processing_rule_text')}"
        )
    return messages


def required_ddic_fields_for_processing_contract(output_contract, callable_identities=None, callable_metadata=None):
    result = {}
    for field in output_contract or []:
        for value in (field.get("type_or_like"), field.get("source")):
            ref = ddic_reference_parts(value)
            if ref:
                result.setdefault(ref[0], set()).add(ref[1])
    signatures = normalize_provider_signatures(callable_metadata)
    for callable_name in callable_identities or []:
        signature = signatures.get(str(callable_name).upper())
        if not isinstance(signature, dict):
            continue
        for parameter in list((signature.get("parameters") or {}).values()) + [signature.get("returning")]:
            if not isinstance(parameter, dict):
                continue
            for value in (
                parameter.get("type_or_like"),
                parameter.get("like"),
                parameter.get("abap_type") if ddic_reference_in_type_text(parameter.get("abap_type")) else "",
            ):
                ref = ddic_reference_parts(value)
                if ref:
                    result.setdefault(ref[0], set()).add(ref[1])
    return result


def ddic_reference_parts(value):
    match = re.search(r"\b(?:TYPE|LIKE)?\s*([A-Z0-9_/]+)-([A-Z0-9_]+)\b", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    return match.group(1).upper(), match.group(2).upper()


def filtered_ddic_metadata_for_processing_contract(ddic_catalogue, object_names, required_fields):
    tables = (ddic_catalogue or {}).get("tables") or {}
    filtered = {}
    for object_name in object_names or []:
        table = tables.get(object_name)
        if not table:
            continue
        requested = set(required_fields.get(object_name) or [])
        fields = []
        for field in table.get("fields") or []:
            name = str(field.get("name") or "").upper()
            if requested and name not in requested:
                continue
            fields.append({"name": name, "metadata": field.get("text") or name})
        filtered[object_name] = {"fields": fields}
    return filtered


def filtered_callable_metadata_for_processing_contract(callable_identities, callable_metadata=None, callable_catalogue=None):
    signatures = normalize_provider_signatures(callable_metadata)
    catalogue = callable_catalogue_parameter_index("SAP callable signature catalogue:\n" + str(callable_catalogue or ""))
    filtered = {}
    for name in callable_identities or []:
        key = str(name).upper()
        if key in signatures:
            filtered[key] = signatures[key]
        elif key in catalogue:
            filtered[key] = {"parameters": catalogue[key]}
    return filtered


def validate_processing_contract(contract, ddic_catalogue=None, required_ddic_objects=None, required_ddic_fields=None, callable_identities=None):
    errors = []
    tables = (ddic_catalogue or {}).get("tables") or {}
    metadata = (contract or {}).get("metadata") or {}
    for object_name in required_ddic_objects or []:
        if object_name not in tables:
            errors.append(f"infrastructure metadata error: required DDIC object {object_name} is missing from validated metadata")
    for object_name, fields in (required_ddic_fields or {}).items():
        table = tables.get(object_name) or {}
        available = {field.get("name") for field in table.get("fields") or []}
        if object_name not in tables:
            continue
        for field_name in sorted(fields):
            if field_name not in available:
                errors.append(f"infrastructure metadata error: required DDIC field {object_name}-{field_name} is missing from validated metadata")
    for object_name in (contract or {}).get("ddic_objects") or {}:
        if object_name not in metadata:
            errors.append(f"contract validation error: DDIC object {object_name} has naming contract but no filtered metadata")
    for callable_name in callable_identities or []:
        if callable_name not in ((contract or {}).get("callables") or {}):
            errors.append(f"infrastructure callable metadata error: required callable {callable_name} has no validated signature metadata")
    return dedupe_preserve_order(errors)


def callable_identities_from_prompt(base_prompt):
    contract = prompt_block(base_prompt, "Shared generation contract:", ())
    for line in str(contract or "").splitlines():
        if line.strip().lower().startswith("exact callable identities:"):
            return [name.upper() for name in comma_values(line)]
    return []


def parse_json_response(text):
    cleaned = clean_json_response(text)
    try:
        return {"value": json.loads(cleaned), "error": None}
    except json.JSONDecodeError as exc:
        return {"value": None, "error": f"{type(exc).__name__}: {exc}"}


def clean_json_response(text):
    value = str(text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def declaration_requirements_with_processing_contract_variables_text(declaration_requirements, processing_contract):
    variables = (processing_contract or {}).get("processing_variables") or []
    selection_parameters = (processing_contract or {}).get("selection_parameters") or []
    if not variables and not selection_parameters:
        return declaration_requirements
    requirements = parse_declaration_requirements_text(declaration_requirements)
    enriched = declaration_requirements_payload_with_processing_contract(requirements, processing_contract)
    return json.dumps(enriched, indent=2, sort_keys=True)


def declaration_requirements_with_processing_plan_variables(declaration_requirements, processing_plan):
    variables = processing_variables_from_processing_plan(processing_plan)
    if not variables:
        return declaration_requirements
    if isinstance(declaration_requirements, dict):
        updated = dict(declaration_requirements)
        requirements = updated.get("requirements") if isinstance(updated.get("requirements"), dict) else {}
        updated["requirements"] = declaration_requirements_payload_with_processing_variables(requirements, variables)
        return updated
    return declaration_requirements_with_processing_contract_variables_text(
        declaration_requirements,
        {"processing_variables": variables},
    )


def processing_variables_from_processing_plan(processing_plan):
    diagnostics = (processing_plan or {}).get("processing_contract_diagnostics") if isinstance(processing_plan, dict) else {}
    contract = (diagnostics or {}).get("final_processing_contract") if isinstance(diagnostics, dict) else {}
    return (contract or {}).get("processing_variables") or []


def declaration_requirements_payload_with_processing_variables(requirements, variables):
    enriched = dict(requirements or {})
    processing_variables = normalize_processing_variables(enriched.get("processing_variables"))
    globals_required = normalize_global_variables(enriched.get("global_variables"))
    for variable in normalize_processing_variables(variables):
        add_processing_variable_requirement(processing_variables, variable)
        add_global_variable_requirement(
            globals_required,
            {
                "name": variable.get("name"),
                "declaration": variable.get("declaration") or processing_variable_declaration(variable),
            },
        )
    enriched["processing_variables"] = processing_variables
    enriched["global_variables"] = globals_required
    return enriched


def declaration_requirements_payload_with_processing_contract(requirements, processing_contract):
    enriched = declaration_requirements_payload_with_processing_variables(
        requirements,
        (processing_contract or {}).get("processing_variables") or [],
    )
    selection_parameters = list(enriched.get("selection_parameters") or [])
    for name in (processing_contract or {}).get("selection_parameters") or []:
        normalized = normalize_abap_identifier(name)
        if normalized:
            append_unique(selection_parameters, normalized)
    if selection_parameters:
        enriched["selection_parameters"] = selection_parameters
    return enriched


def processing_variable_names_from_requirements(declaration_requirements=None):
    requirements = parse_declaration_requirements_text(declaration_requirements)
    result = set()
    if isinstance(requirements, dict):
        for item in normalize_processing_variables(requirements.get("processing_variables")):
            name = normalize_plan_identifier(item.get("name"))
            if name:
                result.add(name)
    return result


def normalize_processing_variables(values):
    result = []
    for item in values or []:
        if isinstance(item, dict):
            variable = dict(item)
            name = normalize_abap_identifier(variable.get("name"))
        else:
            variable = {}
            name = normalize_abap_identifier(item)
        if not name:
            continue
        variable["name"] = name
        if variable.get("type_or_like"):
            variable["type_or_like"] = normalize_type_keyword(variable.get("type_or_like"))
        if variable.get("declaration"):
            variable["declaration"] = normalize_declaration_statement(variable.get("declaration"))
        elif variable.get("type_or_like"):
            variable["declaration"] = processing_variable_declaration(variable)
        variable["source_fields"] = dedupe_preserve_order(
            [normalize_type_keyword(value).replace("TYPE ", "").replace("LIKE ", "") for value in variable.get("source_fields") or [] if str(value or "").strip()]
        )
        variable["used_by"] = dedupe_preserve_order([str(value or "").strip() for value in variable.get("used_by") or [] if str(value or "").strip()])
        add_processing_variable_requirement(result, variable)
    return result


def add_processing_variable_requirement(values, item):
    name = normalize_abap_identifier((item or {}).get("name"))
    if not name:
        return
    payload = dict(item or {})
    payload["name"] = name
    for index, existing in enumerate(values):
        if existing.get("name") == name:
            merged = dict(existing)
            for key, value in payload.items():
                if key in {"source_fields", "used_by"}:
                    merged[key] = dedupe_preserve_order((merged.get(key) or []) + (value or []))
                elif value and not merged.get(key):
                    merged[key] = value
            values[index] = merged
            return
    values.append(payload)


def processing_variable_declaration(variable):
    name = normalize_abap_identifier((variable or {}).get("name"))
    type_or_like = normalize_type_keyword((variable or {}).get("type_or_like"))
    if name and type_or_like:
        return f"DATA {name} {type_or_like}."
    return ""


def declaration_requirements_for_prompt(diagnostics):
    requirements = (diagnostics or {}).get("requirements")
    if requirements is not None:
        return json.dumps(requirements, indent=2, sort_keys=True)
    raw_response = str((diagnostics or {}).get("raw_response") or "").strip()
    return raw_response or "None"


def normalize_declaration_requirements(requirements, ddic_catalogue=None, callable_metadata=None):
    return normalize_declaration_requirements_with_diagnostics(
        requirements,
        ddic_catalogue=ddic_catalogue,
        callable_metadata=callable_metadata,
    )["requirements"]


def normalize_declaration_requirements_with_diagnostics(requirements, ddic_catalogue=None, callable_metadata=None):
    if not isinstance(requirements, dict):
        return {"requirements": requirements, "output_field_source_mappings": []}
    normalized = dict(requirements)
    normalized["parameters"] = normalize_parameter_requirements(requirements.get("parameters"))
    fields_result = normalize_output_structure_fields_with_diagnostics(
        requirements.get("output_structure_fields"),
        ddic_catalogue=ddic_catalogue,
        callable_metadata=callable_metadata,
    )
    normalized["output_structure_fields"] = fields_result["fields"]
    normalized["tables_declarations"] = normalize_tables_declarations(
        requirements,
        ddic_catalogue=ddic_catalogue,
    )
    normalized["global_variables"] = normalize_global_variables(requirements.get("global_variables"))
    return {
        "requirements": normalized,
        "output_field_source_mappings": fields_result["source_mappings"],
    }


def normalize_parameter_requirements(parameters):
    result = []
    group_map = {}
    used_groups = set()
    for item in parameters or []:
        if not isinstance(item, dict):
            continue
        parameter = dict(item)
        raw_group = str(parameter.get("radiobutton_group") or "").strip()
        if raw_group:
            parameter["radiobutton_group"] = normalized_radiobutton_group(raw_group, group_map, used_groups)
        else:
            parameter["radiobutton_group"] = ""
        result.append(parameter)
    return result


def normalized_radiobutton_group(group, group_map, used_groups):
    normalized = re.sub(r"[^0-9A-Za-z_]", "", str(group or "").strip()).lower()
    if re.fullmatch(r"[a-z][a-z0-9_]{0,3}", normalized):
        used_groups.add(normalized)
        return normalized
    key = normalized.upper()
    if key in group_map:
        return group_map[key]
    index = 1
    while True:
        candidate = f"r{index:03d}"
        if candidate not in used_groups:
            group_map[key] = candidate
            used_groups.add(candidate)
            return candidate
        index += 1


def enrich_declaration_requirements_for_form_globals(diagnostics, base_prompt=None, source_text=None):
    requirements = (diagnostics or {}).get("requirements")
    if not isinstance(requirements, dict):
        return diagnostics
    enriched = dict(requirements)
    globals_required = normalize_global_variables(enriched.get("global_variables"))
    for item in required_form_global_variables(
        base_prompt,
        source_text=source_text,
        declaration_requirements=json.dumps(enriched),
    ):
        add_global_variable_requirement(globals_required, item)
    enriched["global_variables"] = globals_required
    updated = dict(diagnostics or {})
    updated["requirements"] = enriched
    return updated


def normalize_global_variables(values):
    result = []
    for item in values or []:
        if isinstance(item, dict):
            name = normalize_abap_identifier(item.get("name"))
            declaration = normalize_declaration_statement(item.get("declaration") or item.get("type_or_like"))
        else:
            name = normalize_abap_identifier(item)
            declaration = ""
        if not name:
            continue
        add_global_variable_requirement(result, {"name": name, "declaration": declaration})
    return result


def add_global_variable_requirement(values, item):
    name = normalize_abap_identifier((item or {}).get("name"))
    if not name:
        return
    declaration = normalize_declaration_statement((item or {}).get("declaration"))
    for existing in values:
        if normalize_abap_identifier(existing.get("name")) == name:
            if declaration and not existing.get("declaration"):
                existing["declaration"] = declaration
            return
    values.append({"name": name, "declaration": declaration})


def normalize_abap_identifier(value):
    name = str(value or "").strip().lower()
    return name if re.fullmatch(r"[a-z][a-z0-9_]{0,29}", name) else ""


def normalize_declaration_statement(value):
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text if text.endswith(".") else text + "."


def normalize_tables_declarations(requirements, ddic_catalogue=None):
    parsed = parse_ddic_catalogue(ddic_catalogue)
    tables = parsed.get("tables", {})
    declared = []
    for value in (requirements or {}).get("tables_declarations") or []:
        table_name = normalize_ddic_object_name(value)
        if table_name and (not tables or table_name in tables):
            append_unique(declared, table_name)
    for item in (requirements or {}).get("select_options") or []:
        for value in declaration_reference_values(item, ("for_field", "field", "type_or_like")):
            table_name = verified_ddic_table_for_reference(value, tables)
            if table_name:
                append_unique(declared, table_name)
    return declared


def declaration_reference_values(item, keys):
    if isinstance(item, dict):
        return [item.get(key) for key in keys]
    return [item]


def verified_ddic_table_for_reference(value, tables):
    match = re.search(r"\b([A-Z0-9_/]+)-([A-Z0-9_]+)\b", str(value or ""), re.IGNORECASE)
    if not match:
        return ""
    table_name = match.group(1).upper()
    field_name = match.group(2).upper()
    table = tables.get(table_name)
    if not table:
        return "" if tables else table_name
    if any(field.get("name") == field_name for field in table.get("fields", [])):
        return table_name
    return ""


def normalize_ddic_object_name(value):
    name = str(value or "").strip().upper()
    return name if re.fullmatch(r"[A-Z0-9_/]+", name) else ""


def normalize_output_structure_fields(fields, ddic_catalogue=None, callable_metadata=None):
    return normalize_output_structure_fields_with_diagnostics(
        fields,
        ddic_catalogue=ddic_catalogue,
        callable_metadata=callable_metadata,
    )["fields"]


def normalize_output_structure_fields_with_diagnostics(fields, ddic_catalogue=None, callable_metadata=None):
    ddic_fields = ddic_catalogue_field_index(ddic_catalogue)
    callable_index = callable_parameter_type_index(callable_metadata)
    normalized = []
    source_mappings = []
    seen = set()
    for item in fields or []:
        field = normalize_output_structure_field(item, ddic_fields, callable_index)
        name = field.get("name")
        if not name or name.upper() in seen:
            continue
        seen.add(name.upper())
        normalized.append(public_output_structure_field(field))
        source_mappings.append(output_field_source_mapping(field))
    return {"fields": normalized, "source_mappings": source_mappings}


def normalize_output_structure_field(item, ddic_fields, callable_index):
    if isinstance(item, dict):
        name = str(item.get("name") or "").strip()
        source_field = str(item.get("source_field") or "").strip()
        source = str(item.get("source") or "").strip()
        callable_name = str(item.get("callable") or item.get("function_module") or item.get("method") or "").strip()
        parameter_name = str(item.get("parameter") or item.get("parameter_name") or "").strip()
        type_or_like = str(item.get("type_or_like") or "").strip()
        include_when = str(item.get("include_when") or "").strip()
    else:
        name = str(item or "").strip()
        source_field = ""
        source = ""
        callable_name = ""
        parameter_name = ""
        type_or_like = ""
        include_when = ""

    normalized_type, source_mapping = normalize_output_field_type(
        name,
        type_or_like,
        source_field,
        source,
        callable_name,
        parameter_name,
        ddic_fields,
        callable_index,
    )
    field = {
        "name": name.upper(),
        "type_or_like": normalized_type,
        "_source_mapping": source_mapping,
    }
    if include_when:
        field["include_when"] = include_when
    return field


def public_output_structure_field(field):
    result = {
        "name": field.get("name", ""),
        "type_or_like": field.get("type_or_like", ""),
    }
    if field.get("include_when"):
        result["include_when"] = field["include_when"]
    return result


def normalize_output_field_type(
    name,
    type_or_like,
    source_field,
    source,
    callable_name,
    parameter_name,
    ddic_fields,
    callable_index,
):
    ddic_type = ddic_type_for_reference(source_field, ddic_fields)
    if ddic_type:
        return ddic_type, source_mapping("ddic", source_field, ddic_type)
    ddic_type = ddic_type_for_type_text(type_or_like, ddic_fields)
    if ddic_type:
        return ddic_type, source_mapping("ddic", type_or_like, ddic_type)
    callable_type = callable_type_for_reference(
        type_or_like,
        source_field,
        source,
        callable_name,
        parameter_name,
        callable_index,
    )
    if callable_type:
        return callable_type["type_or_like"], callable_type
    if type_or_like and not ddic_reference_in_type_text(type_or_like):
        return type_or_like, source_mapping("explicit_abap_type", type_or_like, type_or_like)
    ddic_type = unique_ddic_type_for_field_name(name, ddic_fields)
    if ddic_type:
        return ddic_type, source_mapping("ddic", name, ddic_type)
    return "", source_mapping("unverified", source or source_field or type_or_like or name, "")


def extract_ddic_catalogue(metadata_context):
    return prompt_block(
        metadata_context,
        "SAP DDIC metadata catalogue:",
        ("SAP callable signature catalogue:", "Shared generation contract:"),
    )


def extract_callable_catalogue(metadata_context):
    return prompt_block(
        metadata_context,
        "SAP callable signature catalogue:",
        ("Shared generation contract:",),
    )


def render_callable_metadata_for_extraction(callable_metadata):
    signatures = normalize_provider_signatures(callable_metadata)
    if not signatures:
        return ""
    lines = [
        "SAP callable signature catalogue:",
        "- This catalogue is internal verified metadata. Use callable names and parameters exactly.",
    ]
    for callable_name in sorted(signatures):
        signature = signatures[callable_name]
        parts = []
        params = signature.get("parameters", {}) if isinstance(signature, dict) else {}
        if isinstance(params, dict):
            for parameter_name in sorted(params):
                detail = callable_parameter_type_text(params.get(parameter_name, {}))
                parts.append(f"{parameter_name.upper()}{f' [{detail}' if detail else ''}{']' if detail else ''}")
        returning = signature.get("returning") if isinstance(signature, dict) else None
        if isinstance(returning, dict) and returning.get("name"):
            detail = callable_parameter_type_text(returning)
            parts.append(f"{str(returning.get('name')).upper()} [RETURNING{f' {detail}' if detail else ''}]")
        lines.append(f"- {callable_name}: " + (", ".join(parts) if parts else "signature metadata returned"))
    return "\n".join(lines)


def ddic_catalogue_field_index(ddic_catalogue):
    index = {}
    for line in str(ddic_catalogue or "").splitlines():
        match = re.match(r"^\s*-\s+([A-Z0-9_/]+)\s*:\s*(.*)$", line.strip(), re.IGNORECASE)
        if not match:
            continue
        table_name = match.group(1).upper()
        field_names = re.findall(r"\b([A-Z][A-Z0-9_]{1,29})\b(?:\s*\[[^\]]*\])?", match.group(2).upper())
        if field_names[:1] == ["NO"]:
            continue
        for field_name in field_names:
            if field_name in {"NO", "FIELDS", "SELECTED"}:
                continue
            index.setdefault(field_name, set()).add(table_name)
    return index


def ddic_type_for_reference(value, ddic_fields):
    match = re.search(r"\b([A-Z0-9_/]+)-([A-Z0-9_]+)\b", str(value or ""), re.IGNORECASE)
    if not match:
        return ""
    table_name = match.group(1).upper()
    field_name = match.group(2).upper()
    if table_name in ddic_fields.get(field_name, set()):
        return f"TYPE {table_name}-{field_name}"
    return ""


def ddic_type_for_type_text(type_or_like, ddic_fields):
    text = str(type_or_like or "").strip()
    match = re.fullmatch(r"(TYPE|LIKE)\s+([A-Z0-9_/]+)-([A-Z0-9_]+)", text, re.IGNORECASE)
    if not match:
        return ""
    table_name = match.group(2).upper()
    field_name = match.group(3).upper()
    if table_name in ddic_fields.get(field_name, set()):
        return f"TYPE {table_name}-{field_name}"
    return ""


def ddic_reference_in_type_text(type_or_like):
    return bool(re.search(r"\b(TYPE|LIKE)\s+[A-Z0-9_/]+-[A-Z0-9_]+\b", str(type_or_like or ""), re.IGNORECASE))


def unique_ddic_type_for_field_name(name, ddic_fields):
    tables = ddic_fields.get(str(name or "").upper(), set())
    if len(tables) != 1:
        return ""
    table_name = next(iter(tables))
    return f"TYPE {table_name}-{str(name or '').upper()}"


def callable_parameter_type_index(callable_metadata):
    signatures = normalize_provider_signatures(callable_metadata)
    entries = []
    by_pair = {}
    by_parameter = {}
    by_type = {}
    for callable_name, signature in signatures.items():
        params = signature.get("parameters", {}) if isinstance(signature, dict) else {}
        if isinstance(params, dict):
            for parameter_name, parameter in params.items():
                add_callable_parameter_type_entry(
                    entries,
                    by_pair,
                    by_parameter,
                    by_type,
                    callable_name,
                    parameter_name,
                    parameter,
                )
        returning = signature.get("returning") if isinstance(signature, dict) else None
        if isinstance(returning, dict) and returning.get("name"):
            add_callable_parameter_type_entry(
                entries,
                by_pair,
                by_parameter,
                by_type,
                callable_name,
                returning.get("name"),
                returning,
            )
    return {
        "entries": entries,
        "by_pair": by_pair,
        "by_parameter": by_parameter,
        "by_type": by_type,
    }


def add_callable_parameter_type_entry(entries, by_pair, by_parameter, by_type, callable_name, parameter_name, parameter):
    if not isinstance(parameter, dict):
        return
    type_or_like = verified_callable_parameter_type(parameter)
    if not type_or_like:
        return
    entry = {
        "source_kind": "callable_parameter",
        "callable": str(callable_name or "").upper(),
        "parameter": str(parameter_name or "").upper(),
        "direction": str(parameter.get("direction") or "").upper(),
        "type_or_like": type_or_like,
    }
    entries.append(entry)
    by_pair[(entry["callable"], entry["parameter"])] = entry
    by_parameter.setdefault(entry["parameter"], []).append(entry)
    by_type.setdefault(type_or_like.upper(), []).append(entry)


def verified_callable_parameter_type(parameter):
    abap_type = str((parameter or {}).get("abap_type") or (parameter or {}).get("type") or "").strip()
    field_name = str((parameter or {}).get("field") or "").strip()
    if not abap_type:
        return ""
    reference = ddic_type_from_raw_metadata(abap_type, field_name)
    if reference:
        return reference
    if ddic_reference_in_type_text(abap_type):
        return normalize_type_keyword(abap_type)
    return f"TYPE {abap_type}"


def ddic_type_from_raw_metadata(abap_type, field_name):
    object_name = str(abap_type or "").strip().upper()
    field = str(field_name or "").strip().upper()
    if not object_name or not field:
        return ""
    if not re.fullmatch(r"[A-Z0-9_/]+", object_name) or not re.fullmatch(r"[A-Z0-9_]+", field):
        return ""
    return f"TYPE {object_name}-{field}"


def normalize_type_keyword(type_or_like):
    text = str(type_or_like or "").strip()
    match = re.fullmatch(r"(TYPE|LIKE)\s+(.+)", text, re.IGNORECASE)
    if not match:
        return text
    return f"TYPE {match.group(2).upper()}" if "-" in match.group(2) else f"{match.group(1).upper()} {match.group(2)}"


def callable_parameter_type_text(parameter):
    direction = str((parameter or {}).get("direction") or "").upper()
    type_or_like = verified_callable_parameter_type(parameter)
    return " ".join(part for part in (direction, type_or_like) if part)


def callable_type_for_reference(
    type_or_like,
    source_field,
    source,
    callable_name,
    parameter_name,
    callable_index,
):
    pair_entry = callable_entry_for_pair(callable_name, parameter_name, callable_index)
    if pair_entry:
        return dict(pair_entry)

    references = [type_or_like, source_field, source]
    for reference in references:
        pair_entry = callable_entry_from_reference(reference, callable_index)
        if pair_entry:
            return dict(pair_entry)

    parameter_entry = unique_callable_parameter_entry(parameter_name, callable_index)
    if parameter_entry:
        return dict(parameter_entry)

    type_entry = unique_callable_type_entry(type_or_like, callable_index)
    if type_entry:
        return dict(type_entry)

    return None


def callable_entry_for_pair(callable_name, parameter_name, callable_index):
    callable_key = str(callable_name or "").upper()
    parameter_key = str(parameter_name or "").upper()
    if callable_key and parameter_key:
        return callable_index.get("by_pair", {}).get((callable_key, parameter_key))
    return None


def callable_entry_from_reference(reference, callable_index):
    text = str(reference or "").upper()
    if not text:
        return None
    for entry in callable_index.get("entries", []) or []:
        callable_name = entry.get("callable", "")
        parameter_name = entry.get("parameter", "")
        if callable_name and parameter_name and callable_name in text and re.search(rf"\b{re.escape(parameter_name)}\b", text):
            return entry
    matches = []
    for entry in callable_index.get("entries", []) or []:
        parameter_name = entry.get("parameter", "")
        if parameter_name and re.fullmatch(rf"(?:TYPE|LIKE)?\s*{re.escape(parameter_name)}", text):
            matches.append(entry)
    return matches[0] if len(matches) == 1 else None


def unique_callable_parameter_entry(parameter_name, callable_index):
    matches = callable_index.get("by_parameter", {}).get(str(parameter_name or "").upper(), [])
    return matches[0] if len(matches) == 1 else None


def unique_callable_type_entry(type_or_like, callable_index):
    text = normalize_type_keyword(type_or_like).upper()
    matches = callable_index.get("by_type", {}).get(text, [])
    return matches[0] if len(matches) == 1 else None


def source_mapping(source_kind, source, type_or_like):
    return {
        "source_kind": source_kind,
        "source": str(source or ""),
        "type_or_like": str(type_or_like or ""),
        "verified": bool(type_or_like),
    }


def output_field_source_mapping(field):
    mapping = dict(field.get("_source_mapping") or {})
    mapping["name"] = field.get("name", "")
    mapping.setdefault("type_or_like", field.get("type_or_like", ""))
    mapping.setdefault("verified", bool(field.get("type_or_like")))
    return mapping


def ensure_required_tables_declarations(source, declaration_requirements=None):
    required = required_tables_declarations(declaration_requirements)
    if not required:
        return source
    units = abap_statement_units(source)
    if not units:
        return "\n".join(f"TABLES {table_name.lower()}." for table_name in required)

    required_set = set(required)
    tables_units = tables_declaration_units(units, required_set)
    required_in_source = {name for unit in tables_units for name in unit["required_names"]}
    declared_required = []
    for unit in tables_units:
        for name in unit["required_names"]:
            append_unique(declared_required, name)
    if (
        required_in_source == required_set
        and declared_required == required
        and not any(unit["remove"] for unit in tables_units)
        and required_tables_are_in_declaration_position(units, tables_units)
    ):
        return source

    tables_block = required_tables_declaration_block(required, tables_units)
    insert_at = 0
    for index, unit in enumerate(units):
        first_code = first_statement_code_line(unit)
        if re.match(r"^REPORT\b", first_code, re.IGNORECASE):
            insert_at = index + 1
            continue
        break
    assembled_units = []
    for index, unit in enumerate(units):
        if index == insert_at:
            assembled_units.append(tables_block)
        if any(info["index"] == index and info["required_names"] for info in tables_units):
            continue
        assembled_units.append(unit)
    if insert_at >= len(units):
        assembled_units.append(tables_block)
    return "\n".join(line for unit in assembled_units for line in unit)


def required_tables_declarations(declaration_requirements=None):
    requirements = parse_declaration_requirements_text(declaration_requirements)
    values = requirements.get("tables_declarations") if isinstance(requirements, dict) else []
    result = []
    for value in values or []:
        table_name = normalize_ddic_object_name(value)
        if table_name:
            append_unique(result, table_name)
    return result


def declared_tables(source):
    result = []
    for statement in abap_statement_units(source):
        first_code = first_statement_code_line(statement)
        if not re.match(r"^TABLES\b", first_code, re.IGNORECASE):
            continue
        for table_name in tables_declared_by_statement(statement):
            append_unique(result, table_name)
    return result


def tables_declaration_units(units, required_set):
    result = []
    seen_required = set()
    for index, unit in enumerate(units):
        first_code = first_statement_code_line(unit)
        if not re.match(r"^TABLES\b", first_code, re.IGNORECASE):
            continue
        names = tables_declared_by_statement(unit)
        required_names = [name for name in names if name in required_set]
        duplicate_required = any(name in seen_required for name in required_names)
        has_repeated_name = len(names) != len(set(names))
        has_extra_name = any(name not in required_set for name in names)
        remove = bool(required_names) and (duplicate_required or has_repeated_name or has_extra_name)
        if not remove:
            for name in required_names:
                seen_required.add(name)
        result.append(
            {
                "index": index,
                "lines": unit,
                "names": names,
                "required_names": required_names,
                "remove": remove,
            }
        )
    return result


def required_tables_declaration_block(required, tables_units):
    block = []
    emitted = set()
    reusable_units = [unit for unit in tables_units if not unit["remove"]]
    for table_name in required:
        if table_name in emitted:
            continue
        reusable = next((unit for unit in reusable_units if table_name in unit["required_names"]), None)
        if reusable:
            block.extend(reusable["lines"])
            emitted.update(name for name in reusable["required_names"] if name in required)
            reusable_units.remove(reusable)
            continue
        block.append(f"TABLES {table_name.lower()}.")
        emitted.add(table_name)
    return block


def required_tables_are_in_declaration_position(units, tables_units):
    required_indices = {unit["index"] for unit in tables_units if unit["required_names"]}
    if not required_indices:
        return False
    report_seen = False
    for index, unit in enumerate(units):
        first_code = first_statement_code_line(unit)
        if not first_code:
            continue
        if re.match(r"^REPORT\b", first_code, re.IGNORECASE):
            report_seen = True
            continue
        if index in required_indices:
            if not report_seen:
                return False
            continue
        if re.match(
            r"^(TYPES|CONSTANTS|DATA|FIELD-SYMBOLS|RANGES|PARAMETERS|SELECT-OPTIONS|SELECTION-SCREEN)\b",
            first_code,
            re.IGNORECASE,
        ):
            return not any(required_index > index for required_index in required_indices)
    return True


def tables_declared_by_statement(statement):
    statement_text = " ".join(split_code_and_comment(line)[0] for line in statement)
    match = re.search(r"\bTABLES\b\s*:?\s*(.*?)[.]\s*$", statement_text, re.IGNORECASE)
    if not match:
        return []
    return [
        normalize_ddic_object_name(value)
        for value in re.split(r"[,:\s]+", match.group(1))
        if normalize_ddic_object_name(value)
    ]


def ensure_required_global_declarations(source, declaration_requirements=None):
    required = required_global_variable_declarations(declaration_requirements)
    if not required:
        return source
    declared = declared_global_identifiers(source)
    missing = [
        item["declaration"]
        for item in required
        if item.get("declaration") and item["name"] not in declared
    ]
    if not missing:
        return source
    return insert_declaration_statements(source, missing)


def required_global_variable_declarations(declaration_requirements=None):
    requirements = parse_declaration_requirements_text(declaration_requirements)
    values = requirements.get("global_variables") if isinstance(requirements, dict) else []
    result = []
    for item in normalize_global_variables(values):
        if item.get("declaration"):
            result.append(item)
    return result


def ensure_database_read_declarations(source, base_prompt=None, source_text=None, declaration_requirements=None, ddic_metadata=None):
    declarations = deterministic_database_read_declarations(
        base_prompt,
        source_text=source_text,
        declaration_requirements=declaration_requirements,
        ddic_metadata=ddic_metadata,
    )
    if not declarations:
        return source
    cleaned = remove_database_read_declaration_units(source, declarations)
    return insert_declaration_statements(cleaned, database_read_declaration_lines(declarations))


def deterministic_database_read_declarations(base_prompt=None, source_text=None, declaration_requirements=None, ddic_metadata=None):
    context = database_read_selected_field_context(base_prompt, source_text, declaration_requirements, ddic_metadata=ddic_metadata)
    selected_fields = context.get("selected_fields_in_spec_order") or []
    if not selected_fields:
        return []
    metadata = context.get("full_sap_metadata_returned") or {}
    object_contracts = context.get("object_contracts") or {}
    fields_by_object = requested_fields_by_ddic_object(selected_fields)
    declarations = []
    ordered_object_names = [name for name in object_contracts if name in fields_by_object]
    ordered_object_names.extend(name for name in fields_by_object if name not in ordered_object_names)
    for object_name in ordered_object_names:
        object_contract = object_contracts.get(object_name, {})
        table_name = normalize_abap_identifier(object_contract.get("table")) or "t_" + contract_identifier_suffix(object_name)
        work_area = normalize_abap_identifier(object_contract.get("work_area")) or "st_" + contract_identifier_suffix(object_name)
        type_name = local_database_read_type_name(object_name, object_contract)
        selected = fields_by_object.get(object_name) or []
        all_fields = metadata_field_names(metadata.get(object_name))
        if not table_name or not work_area or not selected or not all_fields:
            continue
        complete = set(selected) == set(all_fields)
        declarations.append(
            {
                "object_name": object_name,
                "table": table_name,
                "work_area": work_area,
                "type": type_name,
                "fields": selected,
                "components": database_read_component_contracts(object_name, selected, metadata),
                "complete": complete,
            }
        )
    return declarations


def metadata_field_names(field_texts):
    names = []
    for item in field_texts or []:
        field_name = ddic_field_name_from_part(item)
        if field_name:
            append_unique(names, field_name)
    return names


def database_read_declaration_lines(declarations):
    lines = []
    for declaration in declarations or []:
        object_name = declaration["object_name"]
        row_type = object_name if declaration.get("complete") else declaration["type"]
        if declaration.get("complete"):
            lines.append(f"DATA {declaration['table']} TYPE STANDARD TABLE OF {row_type}.")
            lines.append(f"DATA {declaration['work_area']} TYPE {row_type}.")
            continue
        lines.append(f"TYPES: BEGIN OF {row_type},")
        for component in declaration.get("components") or []:
            name, _, type_ref = component.partition(" TYPE ")
            if name and type_ref:
                lines.append(f"         {name} TYPE {type_ref},")
        lines.append(f"       END OF {row_type}.")
        lines.append(f"DATA {declaration['table']} TYPE STANDARD TABLE OF {row_type}.")
        lines.append(f"DATA {declaration['work_area']} TYPE {row_type}.")
    return lines


STANDARD_REPORT_HEADER_TEMPLATE = """************************************************************************
*  Report      : Z_REPORT                Author :                      *
*                                                                      *
*  Log Number  : XXXX                    Date   : sy-datum             *
*                                                                      *
*  Description :                                                       *
*  Program description goes here  .  .  .  .  .  .  .  .  .  .  .  .   *
*                                                                      *
*                                                                      *
************************************************************************
*  Revision History                                                    *
************************************************************************
*  Date        :                          Mod ID      :                *
*                                                                      *
*  Name        :                          Log Number  :                *
*                                                                      *
*  Description :                                                       *
*                                                                      *
************************************************************************"""


def ensure_standard_report_header(source):
    units = abap_statement_units(source)
    if not units:
        return source
    if "Revision History" in str(source or "") and re.search(r"^\*\s+Report\s+:", str(source or ""), re.IGNORECASE | re.MULTILINE):
        return source
    for index, unit in enumerate(units):
        first_code = first_statement_code_line(unit)
        match = re.match(r"^REPORT\s+([A-Z0-9_/]+)\b", first_code, re.IGNORECASE)
        if not match:
            continue
        report_name = match.group(1)
        header = STANDARD_REPORT_HEADER_TEMPLATE.replace("Z_REPORT", report_name)
        assembled = units[: index + 1] + [header.splitlines()] + units[index + 1 :]
        return "\n".join(line for assembled_unit in assembled for line in assembled_unit)
    return source


def group_declaration_statements_by_prefix(source):
    units = abap_statement_units(source)
    grouped = {"ty_": [], "t_": [], "st_": [], "w_": []}
    kept = []
    insert_at = None
    for unit in units:
        prefix = declaration_statement_group_prefix(unit)
        if prefix:
            if insert_at is None:
                insert_at = len(kept)
            grouped[prefix].append(unit)
            continue
        kept.append(unit)
    if insert_at is None:
        return source
    ordered_groups = []
    for prefix in ("ty_", "t_", "st_", "w_"):
        ordered_groups.extend(grouped[prefix])
    assembled_units = kept[:insert_at] + ordered_groups + kept[insert_at:]
    return "\n".join(line for unit in assembled_units for line in unit)


def declaration_statement_group_prefix(unit):
    first_code = first_statement_code_line(unit)
    type_match = re.match(r"^TYPES\s*:?\s+BEGIN\s+OF\s+([A-Z][A-Z0-9_]{0,29})\b", first_code, re.IGNORECASE)
    if not type_match:
        type_match = re.match(r"^TYPES\s*:?\s+([A-Z][A-Z0-9_]{0,29})\b", first_code, re.IGNORECASE)
    if type_match and type_match.group(1).lower().startswith("ty_"):
        return "ty_"
    if not re.match(r"^DATA\b", first_code, re.IGNORECASE):
        return None
    names = identifiers_declared_by_statement(unit)
    if not names:
        return None
    name = names[0]
    for prefix in ("t_", "st_", "w_"):
        if name.startswith(prefix):
            return prefix
    return None


def remove_database_read_declaration_units(source, declarations):
    targets = database_read_declaration_targets(declarations)
    if not targets["types"] and not targets["data"]:
        return source
    kept = []
    for unit in abap_statement_units(source):
        replacement = database_read_declaration_unit_without_targets(unit, targets)
        if replacement is None:
            continue
        kept.extend(replacement)
    return "\n".join(kept)


def database_read_declaration_unit_without_targets(unit, targets):
    type_segments = type_structure_segments(unit)
    if type_segments:
        preserved = []
        changed = False
        for segment in type_segments:
            if segment["name"] in targets["types"]:
                changed = True
                continue
            preserved.extend(standalone_type_structure_lines(segment))
        if changed:
            return preserved
    if database_read_declaration_unit_matches(unit, targets):
        return None
    return unit


def type_structure_segments(unit):
    segments = []
    lines = list(unit or [])
    index = 0
    while index < len(lines):
        code = split_code_and_comment(lines[index])[0].strip()
        begin_match = re.match(r"^(?:TYPES\s*:?\s*)?BEGIN\s+OF\s+([A-Z][A-Z0-9_]{0,29})\b", code, re.IGNORECASE)
        if not begin_match:
            index += 1
            continue
        name = begin_match.group(1).lower()
        start = index
        end = index
        while end < len(lines):
            end_code = split_code_and_comment(lines[end])[0].strip()
            if re.match(rf"^END\s+OF\s+{re.escape(name)}\b", end_code, re.IGNORECASE):
                break
            end += 1
        if end >= len(lines):
            index += 1
            continue
        segments.append({"name": name, "lines": lines[start : end + 1]})
        index = end + 1
    return segments


def standalone_type_structure_lines(segment):
    lines = list((segment or {}).get("lines") or [])
    if not lines:
        return []
    first = split_code_and_comment(lines[0])[0]
    if not re.match(r"^\s*TYPES\b", first, re.IGNORECASE):
        lines[0] = re.sub(r"^\s*BEGIN\b", "TYPES: BEGIN", lines[0], count=1, flags=re.IGNORECASE)
    lines[-1] = re.sub(r",\s*(\".*)?$", r".\1", lines[-1])
    return lines


def database_read_declaration_targets(declarations):
    return {
        "types": {item["type"].lower() for item in declarations or [] if item.get("type")},
        "data": {
            name
            for item in declarations or []
            for name in (item.get("table"), item.get("work_area"))
            if name
        },
    }


def database_read_declaration_unit_matches(unit, targets):
    first_code = first_statement_code_line(unit)
    type_match = re.match(r"^TYPES\s*:?\s+BEGIN\s+OF\s+([A-Z][A-Z0-9_]{0,29})\b", first_code, re.IGNORECASE)
    if type_match and type_match.group(1).lower() in targets["types"]:
        return True
    if not re.match(r"^DATA\b", first_code, re.IGNORECASE):
        return False
    declared = set(identifiers_declared_by_statement(unit))
    return bool(declared & targets["data"])


def declared_global_identifiers(source):
    result = []
    for statement in abap_statement_units(source):
        first_code = first_statement_code_line(statement)
        if not re.match(r"^(DATA|CONSTANTS|FIELD-SYMBOLS|RANGES)\b", first_code, re.IGNORECASE):
            continue
        for name in identifiers_declared_by_statement(statement):
            append_unique(result, name)
    return result


def identifiers_declared_by_statement(statement):
    statement_text = " ".join(split_code_and_comment(line)[0] for line in statement)
    match = re.match(r"\s*(DATA|CONSTANTS|RANGES)\s*:?\s*(.*?)[.]\s*$", statement_text, re.IGNORECASE)
    if not match:
        return []
    body = match.group(2)
    names = []
    for part in split_ddic_field_parts(body):
        name_match = re.match(r"\s*([A-Z][A-Z0-9_]{0,29})\b", part.strip(), re.IGNORECASE)
        if name_match:
            append_unique(names, name_match.group(1).lower())
    return names


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


def ensure_form_chunk_uses_declared_globals(
    source,
    chunk_name,
    base_prompt=None,
    source_text=None,
    declaration_requirements=None,
):
    local_declarations = form_chunk_local_declaration_lines(source)
    if local_declarations:
        raise ValueError(f"{chunk_name} generated local declaration(s) inside FORM: {', '.join(local_declarations)}")
    allowed = allowed_form_global_identifiers(
        base_prompt,
        source_text=source_text,
        declaration_requirements=declaration_requirements,
    )
    invented = [
        name
        for name in form_chunk_global_style_identifiers(source)
        if name not in allowed
    ]
    if invented:
        names = ", ".join(invented)
        raise ValueError(f"{chunk_name} referenced undeclared global variable(s): {names}")


def form_chunk_local_declaration_lines(source):
    declarations = []
    in_form = False
    for line in str(source or "").splitlines():
        code = split_code_and_comment(line)[0].strip()
        if re.match(r"^FORM\b", code, re.IGNORECASE):
            in_form = True
            continue
        if in_form and re.match(r"^ENDFORM\b", code, re.IGNORECASE):
            in_form = False
            continue
        if in_form and re.match(r"^(DATA|TYPES)\b", code, re.IGNORECASE):
            append_unique(declarations, code)
    return declarations


def allowed_form_global_identifiers(base_prompt=None, source_text=None, declaration_requirements=None):
    allowed = []
    for item in required_form_global_variables(
        base_prompt,
        source_text=source_text,
        declaration_requirements=declaration_requirements,
    ):
        append_unique(allowed, item["name"])
    requirements = parse_declaration_requirements_text(declaration_requirements)
    if isinstance(requirements, dict):
        for item in requirements.get("parameters") or []:
            if isinstance(item, dict):
                append_unique(allowed, normalize_abap_identifier(item.get("name")))
        for item in requirements.get("select_options") or []:
            if isinstance(item, dict):
                append_unique(allowed, normalize_abap_identifier(item.get("name")))
    return set(name for name in allowed if name)


def form_chunk_global_style_identifiers(source):
    identifiers = []
    in_call_function = False
    for line in str(source or "").splitlines():
        code = split_code_and_comment(line)[0]
        stripped = code.strip()
        if re.match(r"^CALL\s+FUNCTION\b", stripped, re.IGNORECASE):
            in_call_function = True
        for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]{1,29})\b", code):
            name = match.group(1).lower()
            if in_call_function and is_call_function_parameter_name(code, match):
                continue
            if name.startswith(GLOBAL_STYLE_PREFIXES):
                append_unique(identifiers, name)
        if in_call_function and statement_ends(stripped):
            in_call_function = False
    return identifiers


def is_call_function_parameter_name(code, match):
    before = code[: match.start()].strip()
    after = code[match.end() :]
    if before and not re.fullmatch(r"(EXPORTING|IMPORTING|TABLES|CHANGING|EXCEPTIONS)", before, re.IGNORECASE):
        return False
    return bool(re.match(r"\s*=", after))


def chunk_prompt_text(base_prompt, chunk, source_text=None, declaration_requirements=None, processing_plan=None, ddic_metadata=None):
    chunk_name = chunk.get("name")
    context = chunk_template_context_prompt(
        chunk_name,
        base_prompt,
        source_text,
        declaration_requirements=declaration_requirements,
        processing_plan=processing_plan,
        ddic_metadata=ddic_metadata,
    )
    return (
        "Chunked generation mode:\n"
        f"- Chunk: {chunk_name}\n"
        "- Use only the prompt template and context for this chunk.\n"
        "- Do not invent alternative names such as t_mpe0001 or wa_edidc.\n"
        "- Return only ABAP source for this chunk. Do not use Markdown fences.\n"
        "\n"
        f"{context}\n"
    )


def declaration_chunk_context_prompt(base_prompt, source_text=None, declaration_requirements=None):
    return chunk_template_context_prompt(
        "declarations",
        base_prompt,
        source_text,
        declaration_requirements=declaration_requirements,
    )


def render_declarations_chunk_prompt(source_text=None, ddic_catalogue=None, declaration_contract=None):
    return render_chunk_prompt_template(
        "declarations",
        source_text=source_text,
        ddic_catalogue=ddic_catalogue,
        chunk_contract=declaration_contract,
    )


def chunk_template_context_prompt(chunk_name, base_prompt, source_text=None, declaration_requirements=None, processing_plan=None, ddic_metadata=None):
    chunk_requirements = chunk_requirement_block(
        chunk_name,
        source_text,
        base_prompt,
        declaration_requirements=declaration_requirements,
        processing_plan=processing_plan,
        ddic_metadata=ddic_metadata,
    )
    chunk_contract = chunk_generation_contract_block(
        base_prompt,
        chunk_name,
        source_text=source_text,
        declaration_requirements=declaration_requirements,
        processing_plan=processing_plan,
        ddic_metadata=ddic_metadata,
    )
    return render_chunk_prompt_template(
        chunk_name,
        source_text=chunk_requirements,
        ddic_catalogue=chunk_ddic_catalogue(
            chunk_name,
            base_prompt,
            source_text=source_text,
            chunk_requirements=chunk_requirements,
            declaration_requirements=declaration_requirements,
            processing_plan=processing_plan,
            ddic_metadata=ddic_metadata,
        ),
        callable_catalogue=chunk_callable_catalogue(chunk_name, base_prompt, processing_plan=processing_plan),
        chunk_contract=chunk_contract,
        chunk_requirements=chunk_requirements,
    )


def render_chunk_prompt_template(
    chunk_name,
    source_text=None,
    ddic_catalogue=None,
    callable_catalogue=None,
    chunk_contract=None,
    chunk_requirements=None,
):
    template = load_chunk_prompt_template(chunk_name)
    requirements = str(chunk_requirements or source_text or "").strip() or "None"
    replacements = {
        "{{DDIC_METADATA}}": str(ddic_catalogue or "").strip() or "None",
        "{{CALLABLE_METADATA}}": str(callable_catalogue or "").strip() or "None",
        "{{CHUNK_CONTRACT}}": str(chunk_contract or "").strip() or "None",
        "{{DECLARATION_REQUIREMENTS}}": requirements,
        "{{DATABASE_READ_REQUIREMENTS}}": requirements,
        "{{PROCESSING_REQUIREMENTS}}": requirements,
        "{{OUTPUT_REQUIREMENTS}}": requirements,
        "{{MAIN_FLOW_REQUIREMENTS}}": requirements,
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template.strip()


def load_chunk_prompt_template(chunk_name):
    try:
        path = CHUNK_PROMPT_PATHS[chunk_name]
    except KeyError as exc:
        raise ValueError(f"Unknown ABAP chunk prompt template: {chunk_name}") from exc
    return path.read_text(encoding="utf-8")


def load_declarations_chunk_prompt(path=DECLARATIONS_CHUNK_PROMPT_PATH):
    return Path(path).read_text(encoding="utf-8")


def chunk_requirement_block(chunk_name, source_text, base_prompt, declaration_requirements=None, processing_plan=None, ddic_metadata=None):
    if chunk_name == "declarations":
        return str(declaration_requirements or "").strip() or "None"
    if chunk_name == "processing_form":
        return str(processing_plan or "").strip() or "None"
    excerpts = relevant_specification_excerpts(
        source_text,
        chunk_name,
        object_names=ddic_object_names_from_prompt(base_prompt) if chunk_name == "database_read_forms" else None,
    )
    sections = []
    if excerpts:
        sections.append("Relevant specification excerpts:\n" + excerpts)
    metadata = requirement_metadata_block(chunk_name, base_prompt)
    if metadata:
        sections.append(metadata)
    contract = chunk_generation_contract_block(
        base_prompt,
        chunk_name,
        source_text=source_text,
        declaration_requirements=declaration_requirements,
        processing_plan=processing_plan,
        ddic_metadata=ddic_metadata,
    )
    if contract:
        sections.append("Relevant naming contract:\n" + contract)
    return "\n\n".join(sections).strip() or "None"


def relevant_specification_excerpts(source_text, chunk_name, object_names=None):
    keywords = {
        "database_read_forms": (
            "select",
            "selection",
            "select-option",
            "parameter",
            "where",
            "database",
            "table",
            "read",
            "extract",
            "fetch",
            "retrieve",
            "join",
            "for all entries",
        ),
        "processing_form": (
            "process",
            "processing",
            "calculate",
            "derive",
            "validate",
            "mapping",
            "map",
            "transform",
            "loop",
            "read table",
            "move",
            "append",
            "call function",
            "function module",
            "bapi",
            "method",
        ),
        "output_forms": (
            "output",
            "display",
            "alv",
            "csv",
            "file",
            "download",
            "export",
            "write",
            "list",
            "presentation",
        ),
        "main_program_flow": (
            "flow",
            "sequence",
            "first",
            "then",
            "after",
            "before",
            "start",
            "run",
            "execute",
            "call",
        ),
        "declarations": (
            "parameter",
            "parameters",
            "select-option",
            "select-options",
            "selection",
            "screen",
            "type",
            "types",
            "data",
            "constant",
            "constants",
            "scalar",
            "variable",
            "field",
            "fields",
            "output",
        ),
    }.get(chunk_name, ())
    if chunk_name == "database_read_forms":
        lines = database_read_specification_units(source_text, keywords, object_names=object_names)
        return "\n".join(f"- {line}" for line in lines)
    if chunk_name == "processing_form":
        lines = processing_specification_units(source_text, keywords)
        return "\n".join(f"- {line}" for line in lines)
    lines = specification_units(source_text)
    matched = [line for line in lines if contains_any_keyword(line, keywords)]
    return "\n".join(f"- {line}" for line in matched)


def processing_specification_units(source_text, keywords):
    lines = []
    active = False
    for raw_line in str(source_text or "").splitlines():
        stripped = raw_line.strip(" \t-")
        if not stripped:
            continue
        if is_processing_section_start(stripped):
            active = True
            append_unique(lines, stripped)
            continue
        if active and is_processing_section_boundary(stripped):
            active = False
            continue
        if active:
            append_unique(lines, stripped)
            continue
        for sentence in split_specification_sentences(stripped):
            if contains_any_keyword(sentence, keywords):
                append_unique(lines, sentence)
    return lines


def is_processing_section_start(line):
    title = normalized_section_title(line)
    return bool(
        re.search(
            r"\b(processing|record processing|business processing|identifier resolution|output record)\b",
            title,
            re.IGNORECASE,
        )
    )


def is_processing_section_boundary(line):
    title = normalized_section_title(line)
    if is_processing_section_start(title):
        return False
    return bool(
        re.fullmatch(
            r"(alv output|csv output|file output|output|selection screen|dependent table reads|database reads|database access|acceptance criteria|overview|functional specification)",
            title,
            re.IGNORECASE,
        )
    )


def normalized_section_title(line):
    title = re.sub(r"^#{1,6}\s*", "", str(line or "")).strip()
    title = title.rstrip(":")
    return re.sub(r"\s+", " ", title)


def database_read_specification_units(source_text, keywords, object_names=None):
    object_names = {str(name).upper() for name in object_names or []}
    units = document_structure_units(source_text)
    matched = []
    active_section = None
    for unit in units:
        section_object = ""
        if unit["heading_level"] is not None:
            if active_section and unit["heading_level"] <= active_section["level"]:
                active_section = None
            section_object = database_object_section_name(unit["text"], object_names)
        elif not active_section:
            section_object = database_object_intro_name(unit["text"], object_names)
        elif database_object_intro_name(unit["text"], object_names):
            section_object = database_object_intro_name(unit["text"], object_names)
        if section_object:
            active_section = {"level": unit["heading_level"] or 99, "object": section_object}
            matched.append(unit["text"])
            continue
        if active_section:
            matched.append(unit["text"])
            continue
        if contains_any_keyword(unit["text"], keywords) or has_ddic_field_reference(unit["text"]):
            matched.append(unit["text"])
            continue
    return matched


def document_structure_units(source_text):
    units = []
    for line in str(source_text or "").splitlines():
        stripped = line.strip(" \t-")
        if not stripped:
            continue
        heading_level = heading_level_for_line(stripped)
        for sentence in split_specification_sentences(stripped):
            units.append({"text": sentence, "heading_level": heading_level})
    return units


def heading_level_for_line(line):
    match = re.match(r"^(#{1,6})\s+\S", str(line or ""))
    return len(match.group(1)) if match else None


def database_object_section_name(text, object_names):
    if not object_names:
        return ""
    heading_text = re.sub(r"^#{1,6}\s*", "", str(text or "")).strip()
    normalized = heading_text.upper()
    if normalized in object_names:
        return normalized
    intro_name = database_object_intro_name(heading_text, object_names)
    return intro_name


def database_object_intro_name(text, object_names):
    if not object_names:
        return ""
    value = re.sub(r"^\s*(?:[*-]|\d+[.)])\s+", "", str(text or "")).strip()
    match = re.match(r"(?:(?:read|retrieve|select|extract)\s+)?([A-Z][A-Z0-9_/]{1,29})\b", value, re.IGNORECASE)
    if not match:
        return ""
    candidate = match.group(1).upper()
    return candidate if candidate in object_names else ""


def has_ddic_field_reference(text):
    return bool(re.search(r"\b[A-Z][A-Z0-9_/]{1,29}[-.][A-Z][A-Z0-9_]{0,29}\b", str(text or ""), re.IGNORECASE))


def is_simple_database_field_list_unit(text):
    value = re.sub(r"^\s*(?:[*-]|\d+[.)])\s+", "", str(text or "")).strip().strip(":")
    if not value:
        return False
    tokens = re.split(r"[\s,;/]+", value)
    tokens = [token for token in tokens if token]
    if not tokens:
        return False
    return all(re.fullmatch(r"[A-Z][A-Z0-9_]{1,29}", token) for token in tokens)


def specification_units(source_text):
    text = str(source_text or "").strip()
    if not text:
        return []
    raw_units = []
    for line in text.splitlines():
        stripped = line.strip(" \t-")
        if not stripped:
            continue
        raw_units.extend(split_specification_sentences(stripped))
    return dedupe_preserve_order(raw_units)


def split_specification_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [part.strip() for part in parts if part.strip()]


def contains_any_keyword(text, keywords):
    lower = str(text or "").lower()
    return any(keyword in lower for keyword in keywords)


def dedupe_preserve_order(values):
    seen = set()
    result = []
    for value in values or []:
        key = str(value or "")
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def requirement_metadata_block(chunk_name, base_prompt):
    if chunk_name == "processing_form":
        metadata = chunk_callable_catalogue(chunk_name, base_prompt)
        return ("Relevant callable metadata:\n" + metadata) if metadata else ""
    return ""


def declaration_generation_contract_block(base_prompt):
    return chunk_generation_contract_block(base_prompt, "declarations")


def chunk_ddic_catalogue(
    chunk_name,
    base_prompt,
    source_text=None,
    chunk_requirements=None,
    declaration_requirements=None,
    processing_plan=None,
    ddic_metadata=None,
):
    return chunk_ddic_diagnostics(
        chunk_name,
        base_prompt,
        source_text=source_text,
        chunk_requirements=chunk_requirements,
        declaration_requirements=declaration_requirements,
        processing_plan=processing_plan,
        ddic_metadata=ddic_metadata,
    )["final_filtered_metadata"]


def chunk_ddic_diagnostics(
    chunk_name,
    base_prompt,
    source_text=None,
    chunk_requirements=None,
    declaration_requirements=None,
    processing_plan=None,
    ddic_metadata=None,
):
    empty = {
        "raw_extracted_database_read_requirements": "",
        "normalized_database_read_requirements": [],
        "requested_fields_by_ddic_object": {},
        "full_sap_metadata_returned": {},
        "accepted_fields": [],
        "selected_fields_in_spec_order": [],
        "rejected_fields": [],
        "fields_extracted_from_specification": [],
        "matched_sap_metadata_fields": [],
        "complete_row_type_objects": [],
        "final_filtered_metadata": "",
    }
    if chunk_name == "main_program_flow":
        return empty
    catalogue = prompt_block(
        base_prompt,
        "SAP DDIC metadata catalogue:",
        ("SAP callable signature catalogue:", "Shared generation contract:"),
    )
    if not catalogue and not normalized_tables(ddic_metadata):
        return empty
    contract = chunk_generation_contract_block(
        base_prompt,
        chunk_name,
        source_text=source_text,
        declaration_requirements=declaration_requirements,
        processing_plan=processing_plan,
        ddic_metadata=ddic_metadata,
    )
    field_source = (
        str(declaration_requirements or "").strip()
        if chunk_name == "declarations"
        else str(processing_plan or chunk_requirements or "").strip()
        if chunk_name == "processing_form"
        else field_extraction_source(
            source_text,
            chunk_name,
            chunk_requirements,
            declaration_requirements=declaration_requirements,
            base_prompt=base_prompt,
        )
    )
    return filter_ddic_catalogue_for_chunk(
        catalogue,
        chunk_name,
        contract,
        field_source,
        ddic_metadata=ddic_metadata,
    )


def field_extraction_source(source_text, chunk_name, chunk_requirements, declaration_requirements=None, base_prompt=None):
    excerpts = relevant_specification_excerpts(
        source_text,
        chunk_name,
        object_names=ddic_object_names_from_prompt(base_prompt) if chunk_name == "database_read_forms" else None,
    )
    sections = []
    if excerpts:
        sections.append(excerpts)
    excerpt_section = specification_excerpt_section(chunk_requirements)
    if excerpt_section and excerpt_section != excerpts:
        sections.append(excerpt_section)
    if chunk_name == "database_read_forms":
        declaration_text = str(declaration_requirements or "").strip()
        if declaration_text:
            sections.append(declaration_text)
    return "\n".join(sections).strip()


def specification_excerpt_section(requirements):
    text = str(requirements or "")
    marker = "Relevant specification excerpts:"
    start = text.find(marker)
    if start == -1:
        return ""
    start += len(marker)
    end = text.find("\n\n", start)
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def filter_ddic_catalogue_for_chunk(catalogue, chunk_name, contract, field_source, ddic_metadata=None):
    empty = {
        "raw_extracted_database_read_requirements": "",
        "normalized_database_read_requirements": [],
        "requested_fields_by_ddic_object": {},
        "full_sap_metadata_returned": {},
        "accepted_fields": [],
        "rejected_fields": [],
        "fields_extracted_from_specification": [],
        "matched_sap_metadata_fields": [],
        "complete_row_type_objects": [],
        "final_filtered_metadata": "",
    }
    parsed = full_or_compact_ddic_catalogue(catalogue, ddic_metadata)
    if not parsed["tables"]:
        return empty
    object_names = ddic_objects_for_chunk(contract, parsed["tables"])
    if not object_names:
        return empty
    requested_object_names = list(object_names)
    extracted_fields = explicit_ddic_fields_from_requirements(
        field_source,
        parsed["tables"],
        object_names,
        aliases=ddic_identifier_aliases_from_contract(contract),
        contextual_unqualified=chunk_name == "database_read_forms",
    )
    selected_fields = matched_ddic_fields(extracted_fields, parsed["tables"])
    matched_fields = selected_fields
    row_type_objects = complete_row_type_objects_from_contract(chunk_name, contract, parsed["tables"])
    if chunk_name == "database_read_forms":
        matched_fields = database_read_required_fields(
            matched_fields,
            parsed["tables"],
            object_names,
            row_type_objects,
        )
    rejected_fields = rejected_ddic_fields(extracted_fields, parsed["tables"], object_names)
    object_names = ddic_objects_for_filtered_metadata(object_names, matched_fields, row_type_objects)
    if not object_names:
        return {
            "raw_extracted_database_read_requirements": field_source if chunk_name == "database_read_forms" else "",
            "normalized_database_read_requirements": specification_units(field_source) if chunk_name == "database_read_forms" else [],
            "requested_fields_by_ddic_object": requested_fields_by_ddic_object(extracted_fields),
            "full_sap_metadata_returned": full_sap_metadata_returned(parsed["tables"], requested_object_names),
            "accepted_fields": sorted(matched_fields),
            "selected_fields_in_spec_order": selected_fields if chunk_name == "database_read_forms" else [],
            "rejected_fields": rejected_fields,
            "fields_extracted_from_specification": sorted(extracted_fields),
            "matched_sap_metadata_fields": sorted(matched_fields),
            "complete_row_type_objects": sorted(row_type_objects),
            "final_filtered_metadata": "",
        }
    lines = parsed["header"][:]
    for table_name in object_names:
        table = parsed["tables"].get(table_name)
        if not table:
            continue
        fields = filtered_ddic_fields(table_name, table["fields"], matched_fields)
        if fields:
            lines.append(f"- {table_name}: " + ", ".join(fields))
        elif table_name in row_type_objects and chunk_name == "database_read_forms":
            raise RuntimeError(f"Database-read DDIC metadata for {table_name} has no verified fields selected.")
        elif table_name in row_type_objects:
            lines.append(f"- {table_name}: no fields selected")
    final_metadata = "\n".join(lines).strip() if len(lines) > len(parsed["header"]) else ""
    return {
        "raw_extracted_database_read_requirements": field_source if chunk_name == "database_read_forms" else "",
        "normalized_database_read_requirements": specification_units(field_source) if chunk_name == "database_read_forms" else [],
        "requested_fields_by_ddic_object": requested_fields_by_ddic_object(extracted_fields),
        "full_sap_metadata_returned": full_sap_metadata_returned(parsed["tables"], requested_object_names),
        "accepted_fields": sorted(matched_fields),
        "selected_fields_in_spec_order": selected_fields if chunk_name == "database_read_forms" else [],
        "rejected_fields": rejected_fields,
        "fields_extracted_from_specification": sorted(extracted_fields),
        "matched_sap_metadata_fields": sorted(matched_fields),
        "complete_row_type_objects": sorted(row_type_objects),
        "final_filtered_metadata": final_metadata,
    }


def parse_ddic_catalogue(catalogue):
    result = {"header": [], "tables": {}}
    for line in str(catalogue or "").splitlines():
        stripped = line.strip()
        match = re.match(r"^-\s+([A-Z0-9_/]+):\s*(.*)$", stripped, re.IGNORECASE)
        if not match:
            if stripped:
                result["header"].append(line)
            continue
        table_name = match.group(1).upper()
        field_parts = split_ddic_field_parts(match.group(2))
        fields = []
        for part in field_parts:
            field_name = ddic_field_name_from_part(part)
            if field_name:
                fields.append({"name": field_name, "text": part})
        result["tables"][table_name] = {"line": line, "fields": fields}
    return result


def full_or_compact_ddic_catalogue(catalogue=None, ddic_metadata=None):
    parsed = parse_ddic_metadata_for_processing_contract(ddic_metadata)
    if parsed["tables"]:
        return parsed
    return parse_ddic_catalogue(catalogue)


def processing_contract_ddic_catalogue(metadata_context=None, ddic_metadata=None):
    structured = parse_ddic_metadata_for_processing_contract(ddic_metadata)
    if structured["tables"]:
        return structured
    return parse_ddic_catalogue(extract_ddic_catalogue(metadata_context))


def parse_ddic_metadata_for_processing_contract(ddic_metadata=None):
    result = {"header": [], "tables": {}}
    for table_name, table_metadata in normalized_tables(ddic_metadata).items():
        fields = []
        for field_name, field in normalized_fields(table_metadata).items():
            fields.append({"name": field_name, "text": f"{field_name}{field_detail(field)}"})
        result["tables"][table_name] = {"line": "", "fields": fields}
    return result


def ddic_field_name_from_part(part):
    match = re.match(r"([A-Z][A-Z0-9_]{1,29})(?:\s+\[|$)", str(part or "").strip(), re.IGNORECASE)
    return match.group(1).upper() if match else ""


def split_ddic_field_parts(text):
    parts = []
    current = []
    bracket_depth = 0
    for char in str(text or ""):
        if char == "[":
            bracket_depth += 1
        elif char == "]" and bracket_depth:
            bracket_depth -= 1
        if char == "," and bracket_depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)
    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts


def ddic_objects_for_chunk(contract, available_tables):
    names = []
    for line in str(contract or "").splitlines():
        match = re.match(r"^-\s+([A-Z0-9_/]+):\s+structure\s+", line.strip(), re.IGNORECASE)
        if match:
            names.append(match.group(1).upper())
    if not names:
        names = list(available_tables.keys())
    available = set(available_tables)
    return [name for name in dedupe_preserve_order(names) if name in available]


def ddic_object_names_from_prompt(base_prompt):
    catalogue = extract_ddic_catalogue(base_prompt)
    parsed = parse_ddic_catalogue(catalogue)
    return list(parsed["tables"])


def explicit_ddic_fields_from_requirements(requirements, tables, object_names, aliases=None, contextual_unqualified=False):
    available = {name: tables[name] for name in object_names if name in tables}
    alias_map = {str(key).upper(): str(value).upper() for key, value in (aliases or {}).items()}
    extracted = []
    for unit in specification_units(requirements):
        unit_references = []
        for table_name, table in available.items():
            if not re.search(rf"\b{re.escape(table_name)}\b", unit, re.IGNORECASE):
                continue
            for field in table["fields"]:
                match = re.search(rf"\b{re.escape(field['name'])}\b", unit, re.IGNORECASE)
                if match:
                    unit_references.append((match.start(), f"{table_name}-{field['name']}"))
        for match in re.finditer(r"\b([A-Z][A-Z0-9_/]{1,29})[-.]([A-Z][A-Z0-9_]{0,29})\b", unit, re.IGNORECASE):
            object_name = match.group(1).upper()
            table_name = alias_map.get(object_name, object_name)
            unit_references.append((match.start(), f"{table_name}-{match.group(2).upper()}"))
        for _, reference in sorted(unit_references):
            append_unique(extracted, reference)
    if contextual_unqualified:
        contextual_fields = contextual_database_read_fields(requirements, available)
        for reference in extracted:
            append_unique(contextual_fields, reference)
        return contextual_fields
    unique_field_names = unique_metadata_field_names(available)
    for unit in specification_units(requirements):
        for field_name, table_name in unique_field_names.items():
            if re.search(rf"\b{re.escape(field_name)}\b", unit, re.IGNORECASE):
                append_unique(extracted, f"{table_name}-{field_name}")
    return extracted


def contextual_database_read_fields(requirements, available):
    extracted = []
    current_object = ""
    current_level = None
    in_read_fields = False
    object_names = set(available)
    field_names_by_object = {
        table_name: {field["name"] for field in table.get("fields", [])}
        for table_name, table in available.items()
    }
    for unit in document_structure_units(requirements):
        if (
            current_level is not None
            and unit["heading_level"] is not None
            and unit["heading_level"] <= current_level
            and not database_object_section_name(unit["text"], object_names)
        ):
            current_object = ""
            current_level = None
            in_read_fields = False
        section_object = (
            database_object_section_name(unit["text"], object_names)
            if unit["heading_level"] is not None
            else database_object_intro_name(unit["text"], object_names)
        )
        if section_object:
            current_object = section_object
            current_level = unit["heading_level"]
            in_read_fields = False
            continue
        if not current_object:
            continue
        if re.match(r"^#{1,6}\s+read\s+fields\b|^read\s+fields\b", unit["text"], re.IGNORECASE):
            in_read_fields = True
            continue
        if re.match(r"^#{1,6}\s+where\s+conditions\b|^where\s+conditions\b", unit["text"], re.IGNORECASE):
            in_read_fields = False
            continue
        if not in_read_fields:
            continue
        tokens = simple_database_field_tokens(unit["text"])
        if not tokens:
            continue
        valid_fields = field_names_by_object.get(current_object, set())
        for token in tokens:
            if token in valid_fields:
                append_unique(extracted, f"{current_object}-{token}")
    return extracted


def append_unique(values, value):
    if value and value not in values:
        values.append(value)


def ddic_objects_mentioned_in_unit(unit, available):
    mentioned = []
    for table_name in available:
        match = re.search(rf"\b{re.escape(table_name)}\b", str(unit or ""), re.IGNORECASE)
        if match:
            mentioned.append((match.start(), table_name))
    return [table_name for _, table_name in sorted(mentioned)]


def simple_database_field_tokens(text):
    if not is_simple_database_field_list_unit(text):
        return []
    value = re.sub(r"^\s*(?:[*-]|\d+[.)])\s+", "", str(text or "")).strip().strip(":")
    return [token.upper() for token in re.split(r"[\s,;/]+", value) if token]


def ddic_identifier_aliases_from_contract(contract):
    aliases = {}
    for line in str(contract or "").splitlines():
        match = re.match(
            r"^-\s+([A-Z0-9_/]+):\s+structure\s+([^,\s]+),\s+table\s+([^,\s]+),\s+work area\s+([^,\s]+)",
            line.strip(),
            re.IGNORECASE,
        )
        if not match:
            continue
        ddic_name = match.group(1).upper()
        for identifier in match.groups()[1:]:
            aliases[str(identifier).upper()] = ddic_name
    return aliases


def unique_metadata_field_names(tables):
    seen = {}
    duplicates = set()
    for table_name, table in tables.items():
        for field in table["fields"]:
            name = field["name"]
            if name in seen:
                duplicates.add(name)
            else:
                seen[name] = table_name
    return {field: table for field, table in seen.items() if field not in duplicates}


def matched_ddic_fields(extracted_fields, tables):
    matched = []
    for reference in extracted_fields:
        table_name, _, field_name = reference.partition("-")
        table = tables.get(table_name)
        if not table:
            continue
        if any(field["name"] == field_name for field in table["fields"]):
            append_unique(matched, reference)
    return matched


def database_read_required_fields(matched_fields, tables, object_names, row_type_objects):
    required = list(matched_fields or [])
    available = {name: tables[name] for name in object_names if name in tables}
    requested_field_names = dedupe_preserve_order(reference.partition("-")[2] for reference in required)
    for field_name in list(requested_field_names):
        if not field_name:
            continue
        for table_name, table in available.items():
            if any(field["name"] == field_name for field in table["fields"]):
                append_unique(required, f"{table_name}-{field_name}")
    for table_name in row_type_objects:
        table = tables.get(table_name)
        if not table:
            continue
        for reference in key_field_references(table_name, table["fields"]):
            append_unique(required, reference)
    return matched_ddic_fields(required, tables)


def key_field_references(table_name, fields):
    references = []
    for field in fields or []:
        field_name = field.get("name", "")
        if field_name == "MANDT":
            continue
        if "; key" in str(field.get("text") or "").lower():
            references.append(f"{table_name}-{field_name}")
    return references


def requested_fields_by_ddic_object(fields):
    grouped = {}
    for reference in fields or []:
        table_name, separator, field_name = reference.partition("-")
        if not separator:
            continue
        if field_name not in grouped.setdefault(table_name, []):
            grouped[table_name].append(field_name)
    return grouped


def full_sap_metadata_returned(tables, object_names):
    metadata = {}
    for table_name in object_names or []:
        table = tables.get(table_name)
        if not table:
            continue
        metadata[table_name] = [field["text"] for field in table["fields"]]
    return metadata


def rejected_ddic_fields(extracted_fields, tables, object_names):
    rejected = []
    allowed_objects = set(object_names or [])
    for reference in sorted(extracted_fields or []):
        table_name, separator, field_name = reference.partition("-")
        if not separator or not table_name or not field_name:
            rejected.append({"reference": reference, "reason": "not a complete DDIC object-field reference"})
            continue
        if table_name not in allowed_objects:
            rejected.append({"reference": reference, "reason": "DDIC object is not required for this chunk"})
            continue
        table = tables.get(table_name)
        if not table:
            rejected.append({"reference": reference, "reason": "DDIC object was not returned by SAP metadata"})
            continue
        if not any(field["name"] == field_name for field in table["fields"]):
            rejected.append({"reference": reference, "reason": "field was not returned by SAP metadata for this object"})
    return rejected


def ddic_objects_for_filtered_metadata(object_names, matched_fields, row_type_objects):
    matched_tables = {reference.partition("-")[0] for reference in matched_fields}
    allowed = matched_tables | set(row_type_objects)
    return [name for name in object_names if name in allowed]


def complete_row_type_objects_from_contract(chunk_name, contract, tables):
    if chunk_name not in {"declarations", "database_read_forms", "processing_form"}:
        return set()
    names = set()
    available = set(tables)
    for line in str(contract or "").splitlines():
        match = re.match(r"^-\s+([A-Z0-9_/]+):\s+structure\s+", line.strip(), re.IGNORECASE)
        if match and match.group(1).upper() in available:
            names.add(match.group(1).upper())
    return names


def filtered_ddic_fields(table_name, fields, matched_fields):
    return [
        field["text"]
        for field in fields
        if f"{table_name}-{field['name']}" in matched_fields
    ]


def chunk_callable_catalogue(chunk_name, base_prompt, processing_plan=None):
    if chunk_name != "processing_form":
        return ""
    catalogue = prompt_block(
        base_prompt,
        "SAP callable signature catalogue:",
        ("Shared generation contract:",),
    )
    names = processing_plan_callable_names(processing_plan, base_prompt=base_prompt)
    if not names:
        return ""
    lines = []
    for line in str(catalogue or "").splitlines():
        match = re.match(r"^-\s+([A-Z0-9_/=><-]+)\s*:", line.strip(), re.IGNORECASE)
        if match and match.group(1).upper() not in names:
            continue
        if match:
            line = filter_callable_catalogue_line(line, processing_plan=processing_plan, base_prompt=base_prompt)
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def chunk_generation_contract_block(base_prompt, chunk_name, source_text=None, declaration_requirements=None, processing_plan=None, ddic_metadata=None):
    if chunk_name == "processing_form":
        return processing_plan_contract_block(
            base_prompt,
            source_text=source_text,
            declaration_requirements=declaration_requirements,
            processing_plan=processing_plan,
        )
    kept = chunk_generation_contract_core_lines(
        base_prompt,
        chunk_name,
        source_text=source_text,
        declaration_requirements=declaration_requirements,
        processing_plan=processing_plan,
    )
    if chunk_name == "declarations":
        kept.extend(tables_declaration_contract_lines(declaration_requirements))
        kept.extend(global_variable_declaration_contract_lines(declaration_requirements))
        kept.extend(database_read_local_type_contract_lines(base_prompt, source_text, declaration_requirements, ddic_metadata=ddic_metadata))
        kept.extend(output_naming_contract_lines(declaration_requirements))
        kept.extend(alv_field_catalog_declaration_contract_lines(source_text, base_prompt))
    if chunk_name == "database_read_forms":
        kept.extend(database_read_select_field_order_contract_lines(base_prompt, source_text, declaration_requirements, ddic_metadata=ddic_metadata))
        kept.extend(form_allowed_global_contract_lines(base_prompt, source_text, declaration_requirements, chunk_name))
    if chunk_name == "processing_form":
        kept.extend(form_allowed_global_contract_lines(base_prompt, source_text, declaration_requirements, chunk_name))
    if chunk_name == "output_forms":
        kept.extend(output_forms_output_table_contract_lines(declaration_requirements, processing_plan=processing_plan))
        kept.extend(alv_field_catalog_output_contract_lines(source_text, base_prompt))
        kept.extend(file_output_global_contract_lines(source_text, base_prompt, declaration_requirements))
        kept.extend(form_allowed_global_contract_lines(base_prompt, source_text, declaration_requirements, chunk_name))
    return "\n".join(kept).strip()


def processing_plan_contract_block(base_prompt, source_text=None, declaration_requirements=None, processing_plan=None):
    contract = prompt_block(base_prompt, "Shared generation contract:", ())
    if not contract:
        return ""
    identifiers = processing_plan_referenced_identifiers(processing_plan)
    callables = processing_plan_callable_names(processing_plan, base_prompt=base_prompt)
    ddic_objects = processing_plan_ddic_objects(base_prompt, processing_plan)
    kept = []
    for line in contract.splitlines():
        stripped = line.strip()
        normalized = stripped.lower()
        if not stripped:
            continue
        if stripped == "Shared generation contract:":
            kept.append(stripped)
        elif is_shared_no_local_form_rule(normalized):
            kept.append(line)
        elif normalized.startswith("- ") and ": structure " in normalized and ", table " in normalized:
            object_name = ddic_object_contract_line_name(line)
            if object_name in ddic_objects:
                kept.append(line)
        elif normalized.startswith("exact internal-table names:"):
            filtered = [name for name in comma_values(line) if name.lower() in identifiers]
            if filtered:
                kept.append("Exact internal-table names: " + ", ".join(filtered))
        elif normalized.startswith("exact work-area names:"):
            filtered = [name for name in comma_values(line) if name.lower() in identifiers]
            if filtered:
                kept.append("Exact work-area names: " + ", ".join(filtered))
        elif normalized.startswith("exact callable identities:"):
            filtered = [name for name in comma_values(line) if name.upper() in callables]
            if filtered:
                kept.append("Exact callable identities: " + ", ".join(filtered))
        elif normalized.startswith("exact form names:"):
            kept.extend(filtered_form_contract_line(
                line,
                "processing_form",
                processing_plan=processing_plan,
                declaration_requirements=declaration_requirements,
            ))
    kept.extend(processing_plan_output_contract_lines(declaration_requirements, identifiers))
    kept.extend(form_allowed_global_contract_lines(
        base_prompt,
        source_text,
        declaration_requirements,
        "processing_form",
        allowed_names=identifiers,
    ))
    return "\n".join(dedupe_preserve_order(kept)).strip()


def processing_plan_output_contract_lines(declaration_requirements=None, identifiers=None):
    output_names = output_names_for_contract(declaration_requirements)
    if not output_names:
        return []
    identifiers = identifiers or set()
    if output_names["table"].lower() not in identifiers and output_names["work_area"].lower() not in identifiers:
        return []
    return [
        f"Exact output names: type {output_names['type']} (TYPES definition), internal table {output_names['table']} (STANDARD TABLE OF {output_names['type']}), work area {output_names['work_area']} (TYPE {output_names['type']})"
    ]


def ddic_object_contract_line_name(line):
    match = re.match(r"^-\s+([A-Z0-9_/]+):\s+structure\s+", str(line or "").strip(), re.IGNORECASE)
    return match.group(1).upper() if match else ""


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


def processing_plan_text_blob(processing_plan=None):
    return "\n".join(processing_plan_strings(processing_plan_payload(processing_plan)))


def processing_plan_referenced_identifiers(processing_plan=None):
    identifiers = []
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]{1,29})\b", processing_plan_text_blob(processing_plan)):
        append_unique(identifiers, match.group(1).lower())
    return set(identifiers)


def processing_plan_callable_names(processing_plan=None, base_prompt=None, callable_metadata=None):
    context = processing_plan_normalization_context(base_prompt=base_prompt, callable_metadata=callable_metadata)
    names = []
    for step in processing_plan_all_steps(processing_plan):
        if str(step.get("operation") or "").strip().upper() in {"CALL_FUNCTION", "CALL_METHOD", "CALL_STATIC_METHOD"}:
            name = callable_identity_for_processing_step(step, context)
            if name:
                append_unique(names, name)
    return set(names)


def processing_plan_callable_parameter_names(processing_plan=None, callable_name=None, base_prompt=None, callable_metadata=None):
    context = processing_plan_normalization_context(base_prompt=base_prompt, callable_metadata=callable_metadata)
    names = []
    target_callable = str(callable_name or "").strip().upper()
    for step in processing_plan_all_steps(processing_plan):
        if str(step.get("operation") or "").strip().upper() not in {"CALL_FUNCTION", "CALL_METHOD", "CALL_STATIC_METHOD"}:
            continue
        if target_callable and callable_identity_for_processing_step(step, context) != target_callable:
            continue
        for key in ("parameters", "parameter_mappings", "input_parameters", "output_parameters", "importing", "exporting", "tables", "changing"):
            value = step.get(key)
            if isinstance(value, dict):
                for name in value:
                    append_unique(names, str(name).upper())
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        for name in (item.get("name"), item.get("parameter"), item.get("target")):
                            if name:
                                append_unique(names, str(name).upper())
                    elif item:
                        append_unique(names, str(item).upper())
    return set(names)


def processing_plan_all_steps(processing_plan=None):
    payload = processing_plan_payload(processing_plan)
    result = []
    collect_processing_plan_steps(payload.get("processing_steps") or [], result)
    return result


def collect_processing_plan_steps(steps, result):
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        result.append(step)
        for key in ("steps", "then", "else"):
            collect_processing_plan_steps(step.get(key) or [], result)


def filter_callable_catalogue_line(line, processing_plan=None, base_prompt=None, callable_metadata=None):
    match = re.match(r"^(\s*-\s+)([A-Z0-9_/=><-]+)(\s*:\s*)(.*)$", str(line or ""), re.IGNORECASE)
    if not match:
        return line
    callable_name = match.group(2).upper()
    parameter_names = processing_plan_callable_parameter_names(
        processing_plan,
        callable_name,
        base_prompt=base_prompt,
        callable_metadata=callable_metadata,
    )
    uses_returned_value = processing_plan_callable_uses_returned_value(
        processing_plan,
        callable_name,
        base_prompt=base_prompt,
        callable_metadata=callable_metadata,
    )
    if not parameter_names:
        return line
    parts = split_ddic_field_parts(match.group(4))
    filtered = []
    for part in parts:
        param_match = re.match(r"([A-Z][A-Z0-9_]{0,29})(?:\s+\[|$)", part.strip(), re.IGNORECASE)
        if param_match and param_match.group(1).upper() in parameter_names:
            filtered.append(part)
        elif uses_returned_value and re.search(r"\[\s*RETURNING\b", part, re.IGNORECASE):
            filtered.append(part)
    if not filtered:
        return ""
    return f"{match.group(1)}{match.group(2)}{match.group(3)}" + ", ".join(filtered)


def processing_plan_callable_uses_returned_value(processing_plan=None, callable_name=None, base_prompt=None, callable_metadata=None):
    context = processing_plan_normalization_context(base_prompt=base_prompt, callable_metadata=callable_metadata)
    target_callable = str(callable_name or "").strip().upper()
    for step in processing_plan_all_steps(processing_plan):
        if str(step.get("operation") or "").strip().upper() not in {"CALL_METHOD", "CALL_STATIC_METHOD"}:
            continue
        if target_callable and callable_identity_for_processing_step(step, context) != target_callable:
            continue
        if step.get("receiving_parameter") or step.get("returning_parameter"):
            return True
    return False


def processing_plan_ddic_objects(base_prompt, processing_plan=None):
    catalogue = extract_ddic_catalogue(base_prompt)
    parsed = parse_ddic_catalogue(catalogue)
    contracts = ddic_object_contracts_from_prompt(base_prompt)
    identifiers = processing_plan_referenced_identifiers(processing_plan)
    object_names = []
    for object_name, contract in contracts.items():
        related = {
            object_name.lower(),
            str(contract.get("structure") or "").lower(),
            str(contract.get("table") or "").lower(),
            str(contract.get("work_area") or "").lower(),
        }
        if identifiers & related:
            append_unique(object_names, object_name)
    if parsed["tables"]:
        aliases = {}
        for object_name, contract in contracts.items():
            aliases[object_name] = object_name
            aliases.update(
                {
                    str(contract.get("structure") or "").upper(): object_name,
                    str(contract.get("table") or "").upper(): object_name,
                    str(contract.get("work_area") or "").upper(): object_name,
                }
            )
        extracted = explicit_ddic_fields_from_requirements(
            processing_plan_text_blob(processing_plan),
            parsed["tables"],
            list(parsed["tables"]),
            aliases=aliases,
        )
        for reference in extracted:
            object_name = reference.partition("-")[0]
            if object_name in parsed["tables"]:
                append_unique(object_names, object_name)
    return set(object_names)


def chunk_generation_contract_core_lines(base_prompt, chunk_name, source_text=None, declaration_requirements=None, processing_plan=None):
    contract = prompt_block(base_prompt, "Shared generation contract:", ())
    if not contract:
        return []
    kept = []
    for line in contract.splitlines():
        kept.extend(
            filtered_contract_lines(
                line,
                chunk_name,
                source_text=source_text,
                declaration_requirements=declaration_requirements,
                processing_plan=processing_plan,
            )
        )
    return kept


def filtered_contract_lines(line, chunk_name, source_text=None, declaration_requirements=None, processing_plan=None):
    stripped = line.strip()
    normalized = stripped.lower()
    if not stripped:
        return []
    if stripped == "Shared generation contract:":
        return [stripped]
    if is_shared_no_local_form_rule(normalized):
        if chunk_name in {"declarations", "database_read_forms", "processing_form", "output_forms"}:
            return [line]
        return []
    if normalized.startswith("- ") and ": structure " in normalized and ", table " in normalized:
        if chunk_name in {"declarations", "database_read_forms", "processing_form"}:
            return [line]
        return []
    if normalized.startswith("exact internal-table names:"):
        if chunk_name in {"declarations", "database_read_forms", "processing_form"}:
            return [line]
        return []
    if normalized.startswith("exact work-area names:"):
        if chunk_name in {"declarations", "database_read_forms", "processing_form"}:
            return [line]
        return []
    if normalized.startswith("exact output structure fields:"):
        if chunk_name == "output_forms":
            return filtered_output_field_contract_line(
                line,
                chunk_name,
                source_text,
                declaration_requirements=declaration_requirements,
            )
        return []
    if normalized.startswith("exact callable identities:"):
        if chunk_name == "processing_form":
            return [line]
        return []
    if normalized.startswith("exact form names:"):
        return filtered_form_contract_line(
            line,
            chunk_name,
            processing_plan=processing_plan,
            declaration_requirements=declaration_requirements,
        )
    return []


def is_shared_no_local_form_rule(normalized_line):
    return normalized_line in {
        "- do not create local declarations inside form routines.",
        "- do not generate data, types, constants, field-symbols, ranges, or statics declarations inside any form.",
        "- every variable required by generated forms must be declared globally by the declarations chunk.",
        "- form routines must reuse the exact global names from this shared naming contract.",
        "- do not invent local names such as lt_*, ls_*, lv_*, wa_*, gt_*, gs_*, or gv_*.",
    }


def output_naming_contract_lines(declaration_requirements=None):
    output_names = output_names_for_contract(declaration_requirements)
    if not output_names:
        return []
    return [
        f"Exact output names: type {output_names['type']} (TYPES definition), internal table {output_names['table']} (STANDARD TABLE OF {output_names['type']}), work area {output_names['work_area']} (TYPE {output_names['type']})"
    ]


def tables_declaration_contract_lines(declaration_requirements=None):
    table_names = required_tables_declarations(declaration_requirements)
    if not table_names:
        return []
    return [
        "Required TABLES declarations: " + ", ".join(table_names),
        "- Generate one TABLES declaration for every required TABLES declaration name.",
    ]


def global_variable_declaration_contract_lines(declaration_requirements=None):
    globals_required = required_global_variable_declarations(declaration_requirements)
    if not globals_required:
        return []
    lines = ["Required global variable declarations:"]
    for item in globals_required:
        lines.append(f"- {item['name']}: {item['declaration']}")
    return lines


def form_allowed_global_contract_lines(base_prompt=None, source_text=None, declaration_requirements=None, chunk_name=None, allowed_names=None):
    globals_required = required_form_global_variables(
        base_prompt,
        source_text=source_text,
        declaration_requirements=declaration_requirements,
    )
    requirements = parse_declaration_requirements_text(declaration_requirements)
    if isinstance(requirements, dict):
        for item in requirements.get("parameters") or []:
            if isinstance(item, dict):
                add_global_variable_requirement(globals_required, {"name": item.get("name"), "declaration": ""})
        for item in requirements.get("select_options") or []:
            if isinstance(item, dict):
                add_global_variable_requirement(globals_required, {"name": item.get("name"), "declaration": ""})
    if allowed_names is not None:
        allowed = {str(name).lower() for name in allowed_names or []}
        globals_required = [item for item in globals_required if item["name"].lower() in allowed]
    if not globals_required:
        return []
    names = [item["name"] for item in globals_required]
    return [
        "Allowed global variables for FORM chunks: " + ", ".join(names),
        "- Do not reference any global variable that is not listed in the allowed global variables for FORM chunks.",
        "- Do not invent undeclared global variables; use only declared globals from the declarations chunk.",
    ]


def required_form_global_variables(base_prompt=None, source_text=None, declaration_requirements=None):
    result = []
    for table_name in comma_values_from_contract(base_prompt, "Exact internal-table names:"):
        add_global_variable_requirement(result, {"name": table_name, "declaration": ""})
    for work_area_name in comma_values_from_contract(base_prompt, "Exact work-area names:"):
        add_global_variable_requirement(result, {"name": work_area_name, "declaration": ""})
    for object_name, item in ddic_object_contracts_from_prompt(base_prompt).items():
        table_name = normalize_abap_identifier(item.get("table"))
        work_area = normalize_abap_identifier(item.get("work_area"))
        row_type = local_database_read_type_name(object_name, item)
        if table_name:
            declaration = f"DATA {table_name} TYPE STANDARD TABLE OF {row_type}." if row_type else ""
            add_global_variable_requirement(result, {"name": table_name, "declaration": declaration})
        if work_area:
            declaration = f"DATA {work_area} TYPE {row_type}." if row_type else ""
            add_global_variable_requirement(result, {"name": work_area, "declaration": declaration})
    output_names = output_names_for_contract(declaration_requirements)
    if output_names:
        add_global_variable_requirement(
            result,
            {
                "name": output_names["table"],
                "declaration": f"DATA {output_names['table']} TYPE STANDARD TABLE OF {output_names['type']}.",
            },
        )
        add_global_variable_requirement(
            result,
            {
                "name": output_names["work_area"],
                "declaration": f"DATA {output_names['work_area']} TYPE {output_names['type']}.",
            },
        )
    if alv_output_required(source_text, base_prompt):
        add_global_variable_requirement(
            result,
            {"name": ALV_FIELDCAT_TABLE_NAME, "declaration": f"DATA {ALV_FIELDCAT_TABLE_NAME} TYPE {ALV_FIELDCAT_TABLE_TYPE}."},
        )
        add_global_variable_requirement(
            result,
            {"name": ALV_FIELDCAT_WORK_AREA_NAME, "declaration": f"DATA {ALV_FIELDCAT_WORK_AREA_NAME} TYPE {ALV_FIELDCAT_WORK_AREA_TYPE}."},
        )
    if file_output_required(source_text, base_prompt):
        add_global_variable_requirement(
            result,
            {"name": "w_filename", "declaration": "DATA w_filename TYPE string."},
        )
        add_global_variable_requirement(
            result,
            {"name": "w_csv_line", "declaration": "DATA w_csv_line TYPE string."},
        )
    for item in required_global_variable_declarations(declaration_requirements):
        add_global_variable_requirement(result, item)
    return result


def comma_values_from_contract(base_prompt, label):
    contract = prompt_block(base_prompt, "Shared generation contract:", ())
    for line in str(contract or "").splitlines():
        if line.strip().lower().startswith(label.lower()):
            return [normalize_abap_identifier(value) for value in comma_values(line)]
    return []


def file_output_global_contract_lines(source_text=None, base_prompt=None, declaration_requirements=None):
    if not file_output_required(source_text, base_prompt):
        return []
    return [
        "Exact file-output global variable: w_filename.",
        "- Use existing global variable w_filename directly; it is already declared TYPE string.",
        "- Treat w_filename as the dataset path only; do not use it as a CSV content buffer.",
        "Exact CSV line global variable: w_csv_line.",
        "- Use existing global variable w_csv_line directly; it is already declared TYPE string.",
        "- Build CSV header or row content in w_csv_line before TRANSFER when the content is not a literal.",
        "- TRANSFER literals or w_csv_line TO w_filename; never TRANSFER w_filename TO w_filename.",
        "- Do not create local filename variables such as lv_filename, l_filename, or filename.",
    ]


def database_read_local_type_contract_lines(base_prompt, source_text=None, declaration_requirements=None, ddic_metadata=None):
    context = database_read_selected_field_context(base_prompt, source_text, declaration_requirements, ddic_metadata=ddic_metadata)
    selected_fields = context.get("selected_fields_in_spec_order") or []
    if not selected_fields:
        return []
    metadata = context.get("full_sap_metadata_returned") or {}
    fields_by_object = requested_fields_by_ddic_object(selected_fields)
    object_contracts = context.get("object_contracts") or {}
    lines = [
        "Exact database-read local row types:",
        "- Generate one TYPES structure per database-read object using only the listed components.",
        "- Do not declare database-read internal tables with complete DDIC table types.",
    ]
    ordered_object_names = [name for name in object_contracts if name in fields_by_object]
    ordered_object_names.extend(name for name in fields_by_object if name not in ordered_object_names)
    for object_name in ordered_object_names:
        fields = fields_by_object[object_name]
        object_contract = object_contracts.get(object_name, {})
        table_name = object_contract.get("table") or "t_" + contract_identifier_suffix(object_name)
        work_area = object_contract.get("work_area") or "st_" + contract_identifier_suffix(object_name)
        type_name = local_database_read_type_name(object_name, object_contract)
        components = database_read_component_contracts(object_name, fields, metadata)
        if not components:
            continue
        lines.append(
            f"- {object_name}: local type {type_name}; internal table {table_name} TYPE STANDARD TABLE OF {type_name}; work area {work_area} TYPE {type_name}; components: "
            + ", ".join(components)
        )
    return lines if len(lines) > 3 else []


def database_read_select_field_order_contract_lines(base_prompt, source_text=None, declaration_requirements=None, ddic_metadata=None):
    context = database_read_selected_field_context(base_prompt, source_text, declaration_requirements, ddic_metadata=ddic_metadata)
    selected_fields = context.get("selected_fields_in_spec_order") or []
    fields_by_object = requested_fields_by_ddic_object(selected_fields)
    if not fields_by_object:
        return []
    object_contracts = context.get("object_contracts") or {}
    lines = [
        "Exact database-read SELECT field order:",
        "- Use these SELECT field lists in exactly this order; the order comes only from the functional specification, not SAP metadata order.",
    ]
    ordered_object_names = [name for name in object_contracts if name in fields_by_object]
    ordered_object_names.extend(name for name in fields_by_object if name not in ordered_object_names)
    for object_name in ordered_object_names:
        lines.append(f"- {object_name}: " + ", ".join(fields_by_object[object_name]))
    return lines


def database_read_selected_field_context(base_prompt, source_text=None, declaration_requirements=None, ddic_metadata=None):
    catalogue = prompt_block(
        base_prompt,
        "SAP DDIC metadata catalogue:",
        ("SAP callable signature catalogue:", "Shared generation contract:"),
    )
    parsed = full_or_compact_ddic_catalogue(catalogue, ddic_metadata)
    if not parsed["tables"]:
        return {
            "selected_fields_in_spec_order": [],
            "full_sap_metadata_returned": {},
            "object_contracts": {},
        }
    contract = "\n".join(chunk_generation_contract_core_lines(base_prompt, "database_read_forms"))
    object_names = ddic_objects_for_chunk(contract, parsed["tables"])
    if not object_names:
        return {
            "selected_fields_in_spec_order": [],
            "full_sap_metadata_returned": {},
            "object_contracts": {},
        }
    field_source = field_extraction_source(
        source_text,
        "database_read_forms",
        None,
        declaration_requirements=declaration_requirements,
        base_prompt=base_prompt,
    )
    available = {name: parsed["tables"][name] for name in object_names if name in parsed["tables"]}
    extracted_fields = contextual_database_read_fields(field_source, available)
    return {
        "selected_fields_in_spec_order": matched_ddic_fields(extracted_fields, parsed["tables"]),
        "full_sap_metadata_returned": full_sap_metadata_returned(parsed["tables"], object_names),
        "object_contracts": ddic_object_contracts_from_prompt(base_prompt),
    }


def ddic_object_contracts_from_prompt(base_prompt):
    contracts = {}
    contract = prompt_block(base_prompt, "Shared generation contract:", ())
    for line in str(contract or "").splitlines():
        match = re.match(
            r"^-\s+([A-Z0-9_/]+):\s+structure\s+([^,\s]+),\s+table\s+([^,\s]+),\s+work area\s+([^,\s]+)",
            line.strip(),
            re.IGNORECASE,
        )
        if not match:
            continue
        object_name = match.group(1).upper()
        contracts[object_name] = {
            "structure": match.group(2),
            "table": match.group(3),
            "work_area": match.group(4),
        }
    return contracts


def local_database_read_type_name(object_name, object_contract=None):
    structure = str((object_contract or {}).get("structure") or "")
    if structure.lower().startswith("st_") and len(structure) > 3:
        return "ty_" + contract_identifier_suffix(structure[3:])
    return "ty_" + contract_identifier_suffix(object_name)


def contract_identifier_suffix(value):
    suffix = re.sub(r"[^0-9A-Za-z]+", "_", str(value or "").lower())
    suffix = re.sub(r"_+", "_", suffix).strip("_")
    return suffix or "data"


def database_read_component_contracts(object_name, fields, metadata):
    field_metadata = metadata.get(object_name)
    available = metadata_field_names(field_metadata)
    components = []
    for field_name in fields or []:
        field = str(field_name or "").upper()
        if available and field not in available:
            continue
        components.append(f"{field.lower()} TYPE {object_name}-{field}")
    return components


def metadata_ordered_fields(field_metadata, fields):
    requested = {str(field or "").upper() for field in fields or [] if str(field or "")}
    ordered = []
    for item in field_metadata or []:
        name = ddic_field_name_from_part(item)
        if name and name in requested:
            ordered.append(name)
    for field in fields or []:
        name = str(field or "").upper()
        if name and name in requested and name not in ordered:
            ordered.append(name)
    return ordered


def metadata_field_names(field_metadata):
    names = set()
    for item in field_metadata or []:
        name = ddic_field_name_from_part(item)
        if name:
            names.add(name)
    return names


def output_forms_output_table_contract_lines(declaration_requirements=None, processing_plan=None):
    output_names = output_names_for_contract(declaration_requirements)
    if not output_names:
        return []
    table_name = output_names["table"]
    lines = [
        f"Exact global output internal table: {table_name}. This global object already exists; use {table_name} directly. Do not invent alternative output table names such as gt_output, it_output, lt_output, or ct_output."
    ]
    if processing_plan_creates_output_records(processing_plan, declaration_requirements):
        lines.append(
            f"The processing plan already creates and appends output records into {table_name}; output routines must consume the existing rows and must not CLEAR, REFRESH, FREE, DELETE, or append extra rows to {table_name} unless the processing plan explicitly requires a separate output-processing stage."
        )
    return lines


def alv_field_catalog_declaration_contract_lines(source_text=None, base_prompt=None):
    if not alv_output_required(source_text, base_prompt):
        return []
    return [
        "Exact ALV field-catalogue globals:",
        f"- Declare global internal table {ALV_FIELDCAT_TABLE_NAME} TYPE {ALV_FIELDCAT_TABLE_TYPE}.",
        f"- Declare global work area {ALV_FIELDCAT_WORK_AREA_NAME} TYPE {ALV_FIELDCAT_WORK_AREA_TYPE}.",
    ]


def alv_field_catalog_output_contract_lines(source_text=None, base_prompt=None):
    if not alv_output_required(source_text, base_prompt):
        return []
    return [
        "Exact ALV field-catalogue globals:",
        f"- Use existing global internal table {ALV_FIELDCAT_TABLE_NAME} directly; it is already declared TYPE {ALV_FIELDCAT_TABLE_TYPE}.",
        f"- Use existing global work area {ALV_FIELDCAT_WORK_AREA_NAME} directly; it is already declared TYPE {ALV_FIELDCAT_WORK_AREA_TYPE}.",
        "- Do not create local field-catalogue DATA, TYPES, CONSTANTS, FIELD-SYMBOLS, RANGES, or STATICS declarations inside output FORM routines.",
        "- Do not invent alternative field-catalogue names such as lt_fieldcat, it_fieldcat, gt_fieldcat, ls_fieldcat, wa_fieldcat, or gs_fieldcat.",
    ]


def alv_output_required(source_text=None, base_prompt=None):
    return specification_requests_alv_output(source_text) or contract_requests_alv_output(base_prompt)


def specification_requests_alv_output(source_text=None):
    return bool(re.search(r"\bALV\b|\bREUSE_ALV\b|\bCL_SALV_TABLE\b|\bCL_GUI_ALV_GRID\b", str(source_text or ""), re.IGNORECASE))


def contract_requests_alv_output(base_prompt=None):
    contract = prompt_block(base_prompt, "Shared generation contract:", ())
    return bool(re.search(r"\bdisplay_alv\b|\bREUSE_ALV\b|\bALV\b", str(contract or ""), re.IGNORECASE))


def file_output_required(source_text=None, base_prompt=None):
    return specification_requests_file_output(source_text) or contract_requests_file_output(base_prompt)


def specification_requests_file_output(source_text=None):
    return bool(re.search(r"\bCSV\b|\bcomma[-\s]?separated\b|\bdownload\b|\bexport\b|\bfile\b", str(source_text or ""), re.IGNORECASE))


def contract_requests_file_output(base_prompt=None):
    contract = prompt_block(base_prompt, "Shared generation contract:", ())
    return bool(re.search(r"\bwrite_csv\b|\bCSV\b|\bfile\b", str(contract or ""), re.IGNORECASE))


def output_names_for_contract(declaration_requirements=None):
    requirements = parse_declaration_requirements_text(declaration_requirements)
    output_fields = requirements.get("output_structure_fields") if isinstance(requirements, dict) else []
    if not output_fields:
        return {}
    return {
        "type": "ty_output",
        "table": "t_output",
        "work_area": "w_output",
    }


def parse_declaration_requirements_text(declaration_requirements):
    text = str(declaration_requirements or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def filtered_output_field_contract_line(line, chunk_name, source_text=None, declaration_requirements=None):
    requirement_field_contract = output_structure_field_contract_lines_from_requirements(declaration_requirements)
    if requirement_field_contract:
        return requirement_field_contract
    fields = comma_values(line)
    explicit_fields = explicit_field_names_for_chunk(source_text, fields, chunk_name)
    if not explicit_fields:
        return []
    return ["Exact output structure fields: " + ", ".join(explicit_fields)]


def output_structure_field_contract_lines_from_requirements(declaration_requirements=None):
    requirements = parse_declaration_requirements_text(declaration_requirements)
    fields = requirements.get("output_structure_fields") if isinstance(requirements, dict) else []
    names = []
    conditional = []
    for field in fields or []:
        if isinstance(field, dict):
            name = str(field.get("name") or "").strip()
            include_when = str(field.get("include_when") or "").strip()
        else:
            name = str(field or "").strip()
            include_when = ""
        if name:
            names.append(name)
        if name and include_when:
            conditional.append(f"- {name}: include only when {include_when}")
    if not names:
        return []
    lines = ["Exact output structure fields: " + ", ".join(names)]
    if conditional:
        lines.append("Conditional output structure fields:")
        lines.extend(conditional)
    return lines


def explicit_field_names_for_chunk(source_text, candidate_fields, chunk_name):
    excerpts = relevant_specification_excerpts(source_text, chunk_name)
    if not excerpts:
        return []
    found = []
    for field in candidate_fields:
        if re.search(rf"\b{re.escape(str(field))}\b", excerpts, re.IGNORECASE):
            found.append(str(field))
    return found


def filtered_form_contract_line(line, chunk_name, processing_plan=None, declaration_requirements=None):
    filtered = filtered_form_names(
        line,
        chunk_name,
        processing_plan=processing_plan,
        declaration_requirements=declaration_requirements,
    )
    if not filtered:
        return []
    if chunk_name == "database_read_forms":
        label = "Exact database FORM names"
    elif chunk_name == "processing_form":
        label = "Exact processing FORM names"
    elif chunk_name == "output_forms":
        label = "Exact output FORM names"
    elif chunk_name == "main_program_flow":
        label = "Exact FORM names"
    else:
        return []
    return [f"{label}: {', '.join(filtered)}"]


def filtered_form_names(line, chunk_name, processing_plan=None, declaration_requirements=None):
    names = comma_values(line)
    if chunk_name == "database_read_forms":
        filtered = [name for name in names if name.lower().startswith("read_")]
    elif chunk_name == "processing_form":
        filtered = [name for name in names if name.lower().startswith("process")]
    elif chunk_name == "output_forms":
        filtered = [name for name in names if name.lower().startswith(("output", "display", "write"))]
    elif chunk_name == "main_program_flow":
        filtered = names
    else:
        return []
    if processing_plan_creates_output_records(processing_plan, declaration_requirements) and not processing_plan_requires_separate_output_stage(processing_plan):
        filtered = [name for name in filtered if name.lower() != "output_data"]
    return filtered


def processing_plan_creates_output_records(processing_plan=None, declaration_requirements=None):
    output_names = output_names_for_contract(declaration_requirements)
    if not output_names:
        return False
    output_work_area = normalize_plan_identifier(output_names.get("work_area"))
    output_table = normalize_plan_identifier(output_names.get("table"))
    if not output_work_area or not output_table:
        return False
    has_output_assignment = False
    has_output_append = False
    for step in processing_plan_all_steps(processing_plan):
        operation = str((step or {}).get("operation") or "").upper()
        source = normalize_plan_reference(step.get("source"), {})
        target = normalize_plan_reference(step.get("target"), {})
        if operation == "CLEAR" and target == output_work_area:
            has_output_assignment = True
        elif operation == "MOVE" and output_record_field_reference(step.get("target"), output_work_area):
            has_output_assignment = True
        elif operation == "APPEND" and source == output_work_area and target == output_table:
            has_output_append = True
    return has_output_assignment and has_output_append


def output_record_field_reference(value, output_work_area):
    text = str(value or "").strip()
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{0,29})[-.]([A-Za-z][A-Za-z0-9_]{0,29})", text)
    return bool(match and normalize_plan_identifier(match.group(1)) == output_work_area)


def processing_plan_requires_separate_output_stage(processing_plan=None):
    payload = processing_plan_payload(processing_plan)
    for key in ("separate_output_stage", "requires_separate_output_stage", "output_processing_stage"):
        if truthy_plan_flag(payload.get(key)):
            return True
    for step in processing_plan_all_steps(payload):
        if truthy_plan_flag(step.get("separate_output_stage")) or truthy_plan_flag(step.get("requires_separate_output_stage")):
            return True
        text = " ".join(
            str(step.get(key) or "")
            for key in ("stage", "form", "routine", "name", "description", "reason")
        )
        if re.search(r"\boutput_data\b|\bseparate\s+output(?:[-\s]+processing)?\s+stage\b", text, re.IGNORECASE):
            return True
    return False


def truthy_plan_flag(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "required", "separate", "explicit"}


def comma_values(line):
    _, _, value = str(line or "").partition(":")
    values = [item.strip() for item in value.split(",")]
    return [item for item in values if item and item.lower() != "none"]


def prompt_block(prompt_text, start_marker, stop_markers):
    text = str(prompt_text or "")
    start = text.find(start_marker)
    if start == -1:
        return ""
    end = len(text)
    for marker in stop_markers or ():
        marker_index = text.find(marker, start + len(start_marker))
        if marker_index != -1:
            end = min(end, marker_index)
    return text[start:end].strip()


def assemble_abap_chunks(chunks, final_assembly_mode=APP_FINAL_ASSEMBLY_MODE):
    if normalize_final_assembly_mode(final_assembly_mode) == APP_FINAL_ASSEMBLY_MODE:
        return assemble_final_abap_from_chunks(chunks)
    return assemble_abap_chunks_from_llm_sections(chunks)


def final_llm_assembly_prompt():
    return "\n".join(
        [
            "Assemble ABAP generation chunks into one complete classical SAP ECC ABAP report.",
            "Use only the supplied chunk responses.",
            "Preserve the implemented business logic, table reads, SELECT field lists, WHERE clauses, FORM names, and output behavior.",
            "Resolve mechanical assembly problems such as duplicate global declarations, duplicate FORM blocks, misplaced declarations, or repeated REPORT statements.",
            "Do not add fields to SELECT field lists unless they are explicitly required to be read or returned.",
            "Fields used only in WHERE conditions must remain in the WHERE clause and must not be added to SELECT lists or output structures.",
            "Return ABAP code only.",
        ]
    )


def final_llm_assembly_source(chunks):
    blocks = []
    for chunk in chunks or []:
        name = (chunk or {}).get("name", "chunk")
        text = (chunk or {}).get("text", "")
        blocks.append(f"===== {name} =====\n{text}".strip())
    return "\n\n".join(blocks)


def assemble_abap_chunks_from_llm_sections(chunks):
    sections = {
        "report_declarations": [],
        "selection_screen": [],
        "main_event": [],
        "forms": [],
    }
    for chunk in chunks or []:
        name = chunk.get("name")
        if name == "declarations":
            sections["report_declarations"].extend(declaration_chunk_statement_lines(chunk.get("text", "")))
            continue
        split = split_abap_chunk(chunk.get("text", ""))
        if name == "main_program_flow":
            sections["main_event"].extend(split["main_event"])
        else:
            sections["forms"].extend(split["forms"])
    return "\n".join(
        section
        for section in (
            join_lines(sections["report_declarations"]),
            join_lines(sections["selection_screen"]),
            join_lines(sections["main_event"]),
            join_lines(dedupe_form_lines(sections["forms"])),
        )
        if section
    )


def post_generation_source_stages(chunks, assembled_source=None):
    declaration_chunk = next((chunk for chunk in chunks or [] if chunk.get("name") == "declarations"), None)
    stages = []
    if declaration_chunk:
        diagnostics = declaration_chunk.get("post_processing_diagnostics") or {}
        for name in (
            "raw_declarations_llm_response",
            "declarations_after_extraction_or_parsing",
            "declarations_after_categorisation",
            "declarations_immediately_before_assembly",
        ):
            stages.append({"stage": name, "source": diagnostics.get(name, "")})
    stages.append({
        "stage": "complete_source_immediately_after_assembly",
        "source": str(assembled_source) if assembled_source is not None else assemble_abap_chunks(chunks),
    })
    return stages


def declaration_post_processing_diagnostics(raw_text, cleaned_text):
    categorised = split_abap_chunk(cleaned_text)
    return {
        "raw_declarations_llm_response": str(raw_text or ""),
        "declarations_after_extraction_or_parsing": str(cleaned_text or ""),
        "declarations_after_categorisation": categorised_declaration_source(categorised),
        "declaration_categories": categorised,
        "declarations_immediately_before_assembly": join_lines(declaration_chunk_statement_lines(cleaned_text)),
    }


def categorised_declaration_source(categorised):
    return "\n".join(
        section
        for section in (
            join_lines((categorised or {}).get("report_declarations")),
            join_lines((categorised or {}).get("selection_screen")),
            join_lines((categorised or {}).get("main_event")),
            join_lines((categorised or {}).get("forms")),
        )
        if section
    )


def declaration_chunk_statement_lines(source):
    lines = []
    for statement in abap_statement_units(source):
        first_code = first_statement_code_line(statement)
        if re.match(r"^FORM\b", first_code, re.IGNORECASE) or is_main_event_line(first_code):
            continue
        lines.extend(statement)
    return lines


def split_abap_chunk(source):
    sections = {
        "report_declarations": [],
        "selection_screen": [],
        "main_event": [],
        "forms": [],
    }
    for statement in abap_statement_units(source):
        first_code = first_statement_code_line(statement)
        if re.match(r"^FORM\b", first_code, re.IGNORECASE):
            sections["forms"].extend(statement)
        elif is_selection_screen_line(first_code):
            sections["selection_screen"].extend(statement)
        elif is_main_event_line(first_code):
            sections["main_event"].extend(statement)
        else:
            sections["report_declarations"].extend(statement)
    return sections


def abap_statement_units(source):
    units = []
    current = []
    in_form = False
    for line in (source or "").splitlines():
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


def is_selection_screen_line(stripped):
    return bool(re.match(r"^(PARAMETERS|SELECT-OPTIONS|SELECTION-SCREEN)\b", stripped, re.IGNORECASE))


def is_main_event_line(stripped):
    return bool(re.match(r"^(START-OF-SELECTION|END-OF-SELECTION|INITIALIZATION|AT\s+SELECTION-SCREEN|PERFORM)\b", stripped, re.IGNORECASE))


def join_lines(lines):
    cleaned = list(lines or [])
    while cleaned and not str(cleaned[0]).strip():
        cleaned.pop(0)
    while cleaned and not str(cleaned[-1]).strip():
        cleaned.pop()
    return "\n".join(str(line) for line in cleaned).strip()


def dedupe_form_lines(lines):
    seen = set()
    result = []
    current = []
    in_form = False
    for line in lines or []:
        stripped = str(line or "").strip()
        if re.match(r"^FORM\b", stripped, re.IGNORECASE):
            in_form = True
            current = [line]
            continue
        if in_form:
            current.append(line)
            if re.match(r"^ENDFORM\b", stripped, re.IGNORECASE):
                block = "\n".join(current).strip()
                key = re.sub(r"\s+", " ", block).upper()
                if key not in seen:
                    seen.add(key)
                    result.extend(current)
                current = []
                in_form = False
            continue
        result.append(line)
    if current:
        block = "\n".join(current).strip()
        key = re.sub(r"\s+", " ", block).upper()
        if key not in seen:
            result.extend(current)
    return result


def clean_abap_response(response_text):
    text = str(response_text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def aggregate_usage(usages):
    totals = {}
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
    return totals or None


def first_value(values):
    for value in values:
        if value:
            return value
    return None


def save_chunk_diagnostic(job_folder, generation_result):
    payload = {
        "chunks": (generation_result or {}).get("chunks", []),
        "assembled_abap": (generation_result or {}).get("text", ""),
        "final_assembly_mode": (generation_result or {}).get("final_assembly_mode"),
        "final_assembly": (generation_result or {}).get("final_assembly"),
        "post_generation_source_stages": (generation_result or {}).get("post_generation_source_stages", []),
        "used_fallback": bool((generation_result or {}).get("used_fallback")),
        "fallback_reason": (generation_result or {}).get("fallback_reason"),
        "declaration_requirements": (generation_result or {}).get("declaration_requirements"),
        "processing_plan": (generation_result or {}).get("processing_plan"),
    }
    (Path(job_folder) / CHUNK_DIAGNOSTIC).write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


def response_text(result):
    if isinstance(result, dict):
        return str(result.get("text", ""))
    return str(result or "")

