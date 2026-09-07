import json
import re
import time
from pathlib import Path
from threading import Thread

from config import Config
from services.abap_source import collect_declared_names, collect_local_type_declarations
from services.callable_signature_provider import (
    callable_identities_from_source,
    merge_callable_metadata,
    normalize_provider_signatures,
    resolve_callable_metadata_for_identities,
)
from services.ddic_metadata_context import (
    append_callable_catalogue,
    append_ddic_catalogue,
    ddic_identifier_provenance,
    extract_relevant_ddic_names,
    extract_typed_ddic_dependencies,
    extract_ambiguous_standalone_type_like_names_from_source,
    extract_post_generation_ddic_names_from_source,
    has_ddic_metadata,
    is_valid_ddic_object_name,
    normalized_fields,
    normalized_tables,
    retrieve_missing_ddic_metadata,
)
from services.ddic_metadata_provider import NoOpDdicMetadataProvider
from services.fixer import auto_fix_abap
from services.job_options import load_job_options
from services.llm import (
    generate_abap,
    generate_code_review_repair,
    generate_dependency_analysis,
    reset_current_model_settings,
    set_current_model_settings,
)
from services.orchestrator import (
    ChunkedGenerationError,
    ProcessingContractValidationError,
    ProcessingPlanValidationError,
    aggregate_usage,
    declaration_requirements_with_processing_plan_variables,
    declaration_requirements_for_prompt,
    ensure_database_read_declarations,
    ensure_standard_report_header,
    ensure_required_tables_declarations,
    group_declaration_statements_by_prefix,
    generate_chunked_abap_program,
    save_chunk_diagnostic,
    extract_declaration_requirements,
    extract_processing_rules_section,
    enrich_declaration_requirements_for_form_globals,
    extract_processing_plan,
    validate_processing_plan,
    normalize_processing_plan_with_diagnostics,
    parse_declaration_requirements_text,
    processing_plan_for_prompt,
    processing_plan_payload,
    required_form_global_variables,
    processing_step_subtitle,
    validate_generated_processing_completeness,
)
from services.progress import update_progress
from services.sap_dependency_analysis import (
    analyze_sap_dependencies,
    ddic_dependency_object,
    normalize_identifiers,
)
from services.validator import parse_callable_invocations, validate_abap


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_SKELETON_PATH = PROJECT_ROOT / "templates" / "report_skeleton.abap"
DATABASE_READ_PATTERNS_PATH = PROJECT_ROOT / "templates" / "database_read_patterns.abap"
POST_GENERATION_INVALID_DDIC_FRAGMENTS = {"END", "LINE", "NON", "START", "SY"}
POST_GENERATION_PROCESSING_DIAGNOSTIC = "post_generation_processing.json"
PROCESSING_PLAN_PROPOSAL_ARTIFACT = "processing_plan_proposal.json"
APPROVED_PROCESSING_PLAN_ARTIFACT = "approved_processing_plan.json"
PROCESSING_PLAN_CONTEXT_ARTIFACT = "processing_plan_context.json"
PROCESSING_PLAN_FAILURE_ARTIFACT = "processing_plan_extraction_failure.json"
PROCESSING_PLAN_LLM_SOURCE_ARTIFACT = "processing_plan_llm_source.txt"
PROCESSING_PLAN_DIAGNOSTICS_ARTIFACT = "processing_plan_diagnostics.json"


def start_create_abap_job(
    job_id,
    input_path,
    jobs_folder,
    prompt_path,
    callable_metadata=None,
    signature_provider=None,
    ddic_metadata_provider=None,
    sap_syntax_checker=None,
    code_review_repairer=None,
    dependency_analyzer=None,
    processing_plan_review_required=True,
    approved_processing_plan=None,
    prepared_declaration_requirements=None,
    prior_section_durations=None,
    prior_usage=None,
):
    thread = Thread(
        target=run_create_abap,
        kwargs={
            "job_id": job_id,
            "input_path": input_path,
            "jobs_folder": jobs_folder,
            "prompt_path": prompt_path,
            "callable_metadata": callable_metadata or {},
            "signature_provider": signature_provider,
            "ddic_metadata_provider": ddic_metadata_provider,
            "sap_syntax_checker": sap_syntax_checker,
            "code_review_repairer": code_review_repairer,
            "dependency_analyzer": dependency_analyzer,
            "processing_plan_review_required": processing_plan_review_required,
            "approved_processing_plan": approved_processing_plan,
            "prepared_declaration_requirements": prepared_declaration_requirements,
            "prior_section_durations": prior_section_durations,
            "prior_usage": prior_usage,
        },
        daemon=True,
    )
    thread.start()
    return thread


def run_create_abap(
    job_id,
    input_path,
    jobs_folder,
    prompt_path,
    callable_metadata=None,
    signature_provider=None,
    ddic_metadata_provider=None,
    sap_syntax_checker=None,
    code_review_repairer=None,
    dependency_analyzer=None,
    processing_plan_review_required=False,
    approved_processing_plan=None,
    prepared_declaration_requirements=None,
    prior_section_durations=None,
    prior_usage=None,
):
    job_folder = Path(jobs_folder) / job_id
    job_folder.mkdir(parents=True, exist_ok=True)
    section_durations = dict(prior_section_durations or {})
    options = load_job_options(jobs_folder, job_id)
    model_settings = options.get("model_settings") or {}
    post_generation_diagnostics = {"stages": [], "model_settings": model_settings}
    model_settings_token = set_current_model_settings(model_settings)

    try:
        save_model_settings(job_folder, model_settings)
        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Reading specification...",
            stage="Reading specification",
        )
        source_text = Path(input_path).read_text(encoding="utf-8")
        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Loading prompt and templates...",
            stage="Loading prompt and templates",
        )
        prompt_template = Path(prompt_path).read_text(encoding="utf-8")
        report_skeleton = load_report_skeleton()
        database_read_patterns = load_database_read_patterns()
        prompt_text = render_create_prompt(prompt_template, source_text, report_skeleton, database_read_patterns)
        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Analyzing SAP dependencies...",
            stage="Analyzing dependencies",
        )
        dependency_analysis = analyze_sap_dependencies(
            source_text,
            enabled=True if dependency_analyzer else None,
            llm_analyzer=dependency_analyzer,
        )
        dependency_analysis.setdefault("_diagnostics", {})["model_settings"] = model_settings
        merge_processing_rule_ddic_dependencies(dependency_analysis, source_text)
        merge_specification_callable_dependencies(dependency_analysis, source_text)
        add_section_duration(
            section_durations,
            "dependency_analysis",
            (dependency_analysis.get("_diagnostics") or {}).get("duration_seconds")
            if isinstance(dependency_analysis, dict)
            else None
        )
        dependency_analysis["pre_generation_ddic_objects"] = list(dependency_analysis.get("ddic_objects", []))
        dependency_analysis["pre_generation_callable_identities"] = list(dependency_analysis.get("callables", []))
        pre_generation_ddic_names = normalize_identifiers(dependency_analysis.get("ddic_objects", []), key="name")
        ddic_progress_callback = lambda message: update_progress(
            jobs_folder,
            job_id,
            "Running",
            message,
            stage="Loading SAP metadata",
        )
        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Loading SAP metadata...",
            stage="Loading SAP metadata",
        )
        metadata_started_at = time.monotonic()
        ddic_metadata, callable_metadata = enrich_metadata(
            pre_generation_ddic_names,
            dependency_analysis.get("callables", []),
            None,
            callable_metadata,
            ddic_metadata_provider,
            signature_provider,
            progress_callback=ddic_progress_callback,
        )
        add_section_duration(section_durations, "sap_metadata_requests", time.monotonic() - metadata_started_at)
        prompt_text = append_ddic_catalogue(prompt_text, ddic_metadata)
        prompt_text = append_callable_catalogue(prompt_text, callable_metadata)
        generation_contract = build_generation_contract(source_text, dependency_analysis, ddic_metadata, callable_metadata)
        prompt_text = append_generation_contract(prompt_text, generation_contract)
        if processing_plan_review_required:
            declaration_requirements, processing_plan = extract_processing_plan_for_review(
                job_folder,
                jobs_folder,
                job_id,
                prompt_text,
                source_text,
                callable_metadata=callable_metadata,
                ddic_metadata=ddic_metadata,
                signature_provider=signature_provider,
                model_settings=model_settings,
            )
            add_section_duration(
                section_durations,
                "declaration_requirements",
                declaration_requirements.get("duration_seconds") if isinstance(declaration_requirements, dict) else None,
            )
            add_section_duration(
                section_durations,
                "processing_plan_extraction",
                processing_plan.get("duration_seconds") if isinstance(processing_plan, dict) else None,
            )
            save_processing_plan_context(
                job_folder,
                {
                    "input_path": str(input_path),
                    "prompt_path": str(prompt_path),
                    "prompt_text": prompt_text,
                    "declaration_requirements": declaration_requirements,
                    "callable_metadata": callable_metadata,
                    "section_durations": section_durations,
                    "usage": aggregate_usage(
                        [
                            declaration_requirements.get("usage") if isinstance(declaration_requirements, dict) else None,
                            processing_plan.get("usage") if isinstance(processing_plan, dict) else None,
                        ]
                    ),
                    "model_settings": model_settings,
                },
            )
            save_dependency_analysis(job_folder, dependency_analysis)
            save_ddic_metadata(job_folder, ddic_metadata)
            update_progress(
                jobs_folder,
                job_id,
                "Awaiting Review",
                "Review the proposed processing plan before ABAP generation.",
                stage="awaiting_processing_plan_review",
            )
            return
        def update_chunk_progress(chunk_index, chunk_total, chunk):
            stage = f"Processing Chunk {chunk_index} of {chunk_total}"
            message = stage
            subtitle = str((chunk or {}).get("subtitle") or "").strip()
            if subtitle:
                message = f"{message} - {subtitle}"
            update_progress(jobs_folder, job_id, "Running", message, stage=stage)

        def update_pre_chunk_progress(stage):
            update_progress(jobs_folder, job_id, "Running", f"{stage}...", stage=stage)

        if approved_processing_plan is not None:
            update_progress(
                jobs_folder,
                job_id,
                "Running",
                "Generating ABAP from the approved processing plan...",
                stage="generating_abap",
            )
        started_at = time.perf_counter()
        llm_result = generate_abap_with_orchestrator(
            prompt_text,
            source_text,
            job_folder,
            callable_metadata=callable_metadata,
            ddic_metadata=ddic_metadata,
            progress_callback=update_chunk_progress,
            pre_chunk_progress_callback=update_pre_chunk_progress,
            declaration_requirements=prepared_declaration_requirements,
            approved_processing_plan=approved_processing_plan,
            model_settings=model_settings,
            final_assembly_mode=options.get("final_assembly_mode"),
        )
        duration_seconds = time.perf_counter() - started_at
        add_section_duration(section_durations, "generated_abap", duration_seconds)
        record_orchestrator_source_stages(post_generation_diagnostics, llm_result)
        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Cleaning generated ABAP...",
            stage="Cleaning generated ABAP",
        )
        response_text, model_name, usage = normalize_llm_result(llm_result)
        usage = usage_for_final_metrics(llm_result, usage, prior_usage=prior_usage)
        generated_abap = clean_response(response_text)
        generated_ddic = classify_post_generation_ddic_candidates(generated_abap)
        record_post_generation_ddic_diagnostics(dependency_analysis, generated_ddic)
        generated_callables = callable_identities_from_source(generated_abap, parse_callable_invocations)
        record_callable_diagnostics(dependency_analysis, generated_callables)
        metadata_started_at = time.monotonic()
        ddic_metadata, callable_metadata = enrich_metadata(
            generated_ddic["accepted"],
            generated_callables,
            ddic_metadata,
            callable_metadata,
            ddic_metadata_provider,
            None,
            progress_callback=ddic_progress_callback,
        )
        add_section_duration(section_durations, "sap_metadata_requests", time.monotonic() - metadata_started_at)

        def update_fix_progress(stage, message):
            update_progress(jobs_folder, job_id, "Running", message, stage=stage)

        auto_fix_started_at = time.monotonic()
        fix_result = auto_fix_abap(
            generated_abap,
            callable_signatures=callable_metadata.get("callable_signatures") or callable_metadata.get("callables"),
            callable_mappings=callable_metadata,
            progress_callback=update_fix_progress,
        )
        add_section_duration(section_durations, "auto_fix", time.monotonic() - auto_fix_started_at)
        final_abap = fix_result["fixed_source"]
        final_abap = ensure_required_tables_declarations(
            final_abap,
            declaration_requirements_for_prompt(llm_result.get("declaration_requirements")),
        )
        final_abap = ensure_database_read_declarations(
            final_abap,
            prompt_text,
            source_text=source_text,
            declaration_requirements=declaration_requirements_for_prompt(llm_result.get("declaration_requirements")),
            ddic_metadata=ddic_metadata,
        )
        final_abap = group_declaration_statements_by_prefix(final_abap)
        final_abap = ensure_standard_report_header(final_abap)
        for stage in fixer_diagnostic_stages(fix_result):
            record_post_generation_stage(post_generation_diagnostics, stage["stage"], stage["source"])
        record_post_generation_stage(
            post_generation_diagnostics,
            "complete_source_after_required_tables_enforcement",
            final_abap,
        )
        final_ddic = classify_post_generation_ddic_candidates(final_abap)
        record_post_generation_ddic_diagnostics(dependency_analysis, final_ddic)
        final_callables = callable_identities_from_source(final_abap, parse_callable_invocations)
        record_callable_diagnostics(dependency_analysis, final_callables)
        metadata_started_at = time.monotonic()
        ddic_metadata, callable_metadata = enrich_metadata(
            final_ddic["accepted"],
            final_callables,
            ddic_metadata,
            callable_metadata,
            ddic_metadata_provider,
            None,
            progress_callback=ddic_progress_callback,
        )
        add_section_duration(section_durations, "sap_metadata_requests", time.monotonic() - metadata_started_at)
        validation_started_at = time.monotonic()
        validation_issues = fix_result["final_issues"]
        validation_issues = merge_validation_issues(
            validation_issues,
            validate_generated_processing_completeness(
                final_abap,
                source_text=source_text,
                processing_plan=llm_result.get("processing_plan") if isinstance(llm_result, dict) else approved_processing_plan,
                declaration_requirements=(
                    llm_result.get("declaration_requirements")
                    if isinstance(llm_result, dict)
                    else prepared_declaration_requirements
                ),
            ),
        )
        if specification_requests_alv(source_text):
            validation_issues = merge_validation_issues(
                validation_issues,
                validate_abap(final_abap, alv_requested=True),
            )
        if has_ddic_metadata(ddic_metadata):
            ddic_issues = validate_abap(
                final_abap,
                identifier_provenance=ddic_identifier_provenance(ddic_metadata),
            )
            validation_issues = merge_validation_issues(validation_issues, ddic_issues)
        add_section_duration(section_durations, "validation", time.monotonic() - validation_started_at)
        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Saving generated ABAP and metrics...",
            stage="Saving results",
        )
        assembled_abap = final_abap
        record_post_generation_stage(post_generation_diagnostics, "complete_source_before_optional_sap_syntax_check_save", assembled_abap)
        (job_folder / "original_generated.abap").write_text(generated_abap, encoding="utf-8")
        (job_folder / "generated.abap").write_text(assembled_abap, encoding="utf-8")
        record_callable_signature_diagnostics(dependency_analysis, callable_metadata)
        save_dependency_analysis(job_folder, dependency_analysis)
        save_ddic_metadata(job_folder, ddic_metadata)
        save_fix_summary(job_folder, fix_result)
        save_validation_issues(job_folder, validation_issues)
        sap_syntax_started_at = time.monotonic()
        final_abap = maybe_run_sap_syntax_check(
            job_folder,
            jobs_folder,
            job_id,
            assembled_abap,
            sap_syntax_checker,
            code_review_repairer=code_review_repairer,
            callable_metadata=callable_metadata,
            post_generation_diagnostics=post_generation_diagnostics,
        )
        final_abap = ensure_database_read_declarations(
            final_abap,
            prompt_text,
            source_text=source_text,
            declaration_requirements=declaration_requirements_for_prompt(llm_result.get("declaration_requirements")),
            ddic_metadata=ddic_metadata,
        )
        final_abap = group_declaration_statements_by_prefix(final_abap)
        final_abap = ensure_standard_report_header(final_abap)
        add_section_duration(section_durations, "sap_syntax_check", time.monotonic() - sap_syntax_started_at)
        record_post_generation_stage(post_generation_diagnostics, "complete_source_immediately_before_final_save", final_abap)
        (job_folder / "generated.abap").write_text(final_abap, encoding="utf-8")
        save_post_generation_diagnostics(job_folder, post_generation_diagnostics)
        cost_breakdown = cost_breakdown_from_job_artifacts(job_folder)
        if not cost_breakdown_has_entries(cost_breakdown):
            cost_breakdown = llm_cost_breakdown_from_result(llm_result)
        metrics = build_metrics(
            model_name=model_name,
            duration_seconds=active_processing_duration(section_durations, fallback=duration_seconds),
            usage=usage,
            prompt_text=prompt_text,
            source_text=source_text,
            generated_abap=final_abap,
            section_durations=section_durations,
            model_settings=model_settings,
            cost_breakdown=cost_breakdown,
        )
        save_metrics(job_folder, metrics)
        update_progress(jobs_folder, job_id, "Complete", "ABAP generation complete.", stage="Complete")
    except Exception as exc:
        update_progress(jobs_folder, job_id, "Error", str(exc), stage="Error")
    finally:
        reset_current_model_settings(model_settings_token)


def extract_processing_plan_for_review(
    job_folder,
    jobs_folder,
    job_id,
    prompt_text,
    source_text,
    callable_metadata=None,
    ddic_metadata=None,
    signature_provider=None,
    model_settings=None,
):
    update_progress(
        jobs_folder,
        job_id,
        "Running",
        "Extracting declaration requirements...",
        stage="Extracting declaration requirements",
    )
    declaration_requirements = extract_declaration_requirements(
        source_text,
        generate_dependency_analysis,
        metadata_context=prompt_text,
        callable_metadata=callable_metadata,
    )
    declaration_requirements = enrich_declaration_requirements_for_form_globals(
        declaration_requirements,
        prompt_text,
        source_text,
    )
    declaration_requirements_text = declaration_requirements_for_prompt(declaration_requirements)
    update_progress(
        jobs_folder,
        job_id,
        "Running",
        "Extracting and validating processing plan...",
        stage="extracting_processing_plan",
    )
    processing_plan_source_text = extract_processing_rules_section(source_text)
    save_processing_plan_llm_source(job_folder, processing_plan_source_text)
    callable_metadata = enrich_processing_rule_callable_metadata(
        processing_plan_source_text,
        callable_metadata,
        signature_provider,
    )
    try:
        processing_plan = extract_processing_plan(
            processing_plan_source_text,
            generate_dependency_analysis,
            metadata_context=prompt_text,
            callable_metadata=callable_metadata,
            declaration_requirements=declaration_requirements_text,
            ddic_metadata=ddic_metadata,
        )
    except ProcessingContractValidationError as exc:
        processing_plan = dict(exc.diagnostics or {})
        processing_plan["model_settings"] = model_settings or {}
        save_processing_plan_extraction_failure(job_folder, processing_plan)
        save_processing_plan_diagnostics(job_folder, processing_plan)
        raise
    except ProcessingPlanValidationError as exc:
        processing_plan = dict(exc.diagnostics or {})
        processing_plan["model_settings"] = model_settings or {}
        save_processing_plan_extraction_failure(job_folder, processing_plan)
    if isinstance(processing_plan, dict):
        processing_plan["model_settings"] = model_settings or {}
    declaration_requirements = declaration_requirements_with_processing_plan_variables(
        declaration_requirements,
        processing_plan,
    )
    save_processing_plan_diagnostics(job_folder, processing_plan)
    save_processing_plan_proposal(
        job_folder,
        processing_plan,
        prompt_text=prompt_text,
        source_text=source_text,
        declaration_requirements=declaration_requirements,
    )
    return declaration_requirements, processing_plan


def enrich_processing_rule_callable_metadata(processing_rules_text, callable_metadata=None, signature_provider=None):
    identities = explicit_object_method_identities(processing_rules_text)
    if not identities:
        return callable_metadata
    return merge_callable_metadata(
        callable_metadata,
        resolve_callable_metadata_for_identities(identities, signature_provider=signature_provider),
    )


def explicit_object_method_identities(text):
    result = []
    seen = set()
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]{1,29})\s*->\s*([A-Za-z][A-Za-z0-9_]{1,29})\b", str(text or "")):
        identity = f"{match.group(1).upper()}=>{match.group(2).upper()}"
        if identity not in seen:
            seen.add(identity)
            result.append(identity)
    return result


def save_processing_plan_llm_source(job_folder, source_text):
    (Path(job_folder) / PROCESSING_PLAN_LLM_SOURCE_ARTIFACT).write_text(
        str(source_text or ""),
        encoding="utf-8",
    )


def save_processing_plan_extraction_failure(job_folder, diagnostics):
    (Path(job_folder) / PROCESSING_PLAN_FAILURE_ARTIFACT).write_text(
        json.dumps(diagnostics or {}, indent=2),
        encoding="utf-8",
    )


def save_processing_plan_diagnostics(job_folder, diagnostics):
    (Path(job_folder) / PROCESSING_PLAN_DIAGNOSTICS_ARTIFACT).write_text(
        json.dumps(diagnostics or {}, indent=2),
        encoding="utf-8",
    )


def save_processing_plan_proposal(job_folder, processing_plan, prompt_text=None, source_text=None, declaration_requirements=None):
    payload = processing_plan_review_payload(
        processing_plan,
        prompt_text=prompt_text,
        source_text=source_text,
        declaration_requirements=declaration_requirements,
    )
    (Path(job_folder) / PROCESSING_PLAN_PROPOSAL_ARTIFACT).write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    return payload


def processing_plan_review_payload(processing_plan, prompt_text=None, source_text=None, declaration_requirements=None):
    review_candidate = processing_plan_review_candidate(processing_plan)
    plan = review_candidate.get("plan") or review_candidate.get("invalid_plan") or {"processing_steps": []}
    diagnostics = review_candidate.get("normalization_diagnostics") or {}
    validation_errors = processing_plan_review_errors(review_candidate)
    warnings = processing_plan_validation_warnings(diagnostics)
    summary = summarize_processing_plan(plan)
    if not processing_plan_has_steps(plan) and validation_errors:
        summary = "Processing plan extraction did not produce a valid reviewable plan.\n" + "\n".join(
            f"- {error}" for error in validation_errors
        )
    return {
        "summary": summary,
        "plan": plan,
        "structured_json": json.dumps(plan, indent=2, sort_keys=True),
        "validation_errors": validation_errors,
        "validation_warnings": warnings,
        "llm_request": processing_plan_llm_request_text(review_candidate),
        "variable_contract": processing_plan_variable_contract(
            plan,
            prompt_text=prompt_text,
            source_text=source_text,
            declaration_requirements=declaration_requirements,
        ),
        "diagnostics": processing_plan or {},
        "approved": False,
    }


def processing_plan_variable_contract(plan, prompt_text=None, source_text=None, declaration_requirements=None):
    declaration_text = declaration_requirements_for_prompt(declaration_requirements) if isinstance(declaration_requirements, dict) else str(declaration_requirements or "")
    variables = []
    for item in required_form_global_variables(
        prompt_text,
        source_text=source_text,
        declaration_requirements=declaration_text,
    ):
        add_review_variable(
            variables,
            (item or {}).get("name"),
            "Global",
            (item or {}).get("declaration"),
            "Generation contract",
        )
    requirements = parse_declaration_requirements_text(declaration_text)
    if isinstance(requirements, dict):
        output_names = {"type": "ty_output", "table": "t_output", "work_area": "w_output"} if requirements.get("output_structure_fields") else {}
        if output_names:
            add_review_variable(variables, output_names["type"], "Type", f"TYPES {output_names['type']}.", "Output structure contract")
        for item in requirements.get("parameters") or []:
            if isinstance(item, dict):
                add_review_variable(variables, item.get("name"), "Selection parameter", "", "Declaration requirements")
        for item in requirements.get("select_options") or []:
            if isinstance(item, dict):
                add_review_variable(variables, item.get("name"), "Select-option", "", "Declaration requirements")
    defined = {item["name"].lower() for item in variables}
    used = sorted(processing_plan_variable_references(plan))
    undefined = [
        {"name": name, "source": "Processing plan"}
        for name in used
        if name.lower() not in defined
    ]
    return {
        "variables_to_define": sorted(variables, key=lambda item: item["name"].lower()),
        "used_but_not_defined": undefined,
    }


def add_review_variable(variables, name, kind, declaration, source):
    normalized = normalize_review_variable_name(name)
    if not normalized:
        return
    for existing in variables:
        if existing["name"].lower() == normalized.lower():
            if declaration and not existing.get("declaration"):
                existing["declaration"] = declaration
            return
    variables.append(
        {
            "name": normalized,
            "kind": kind,
            "declaration": str(declaration or ""),
            "source": source,
        }
    )


def processing_plan_variable_references(plan):
    references = set()
    collect_processing_plan_variable_references(plan, references)
    return references


def collect_processing_plan_variable_references(value, references, key=None):
    if isinstance(value, dict):
        for item_key, item_value in value.items():
            collect_processing_plan_variable_references(item_value, references, key=item_key)
    elif isinstance(value, list):
        for item in value:
            collect_processing_plan_variable_references(item, references, key=key)
    elif key in {
        "source",
        "target",
        "into",
        "left",
        "right",
        "numerator",
        "denominator",
        "distinct",
        "receiving_parameter",
        "returning_parameter",
    }:
        name = normalize_review_variable_name(value)
        if name and looks_like_generated_variable_name(name):
            references.add(name)


def normalize_review_variable_name(value):
    text = str(value or "").strip()
    match = re.match(r"^<?([A-Za-z_]\w*)>?(?:[-.][A-Za-z_]\w*)?$", text)
    return match.group(1) if match else ""


def looks_like_generated_variable_name(name):
    return bool(re.match(r"^(?:t|w|p|s|g|l|st|fs)_[A-Za-z0-9_]+$", str(name or ""), re.IGNORECASE))


def processing_plan_review_errors(review_candidate):
    errors = []
    errors.extend(review_candidate.get("validation_errors") or [])
    errors.extend(review_candidate.get("retryable_validation_errors") or [])
    parse_error = str(review_candidate.get("parse_error") or "").strip()
    if parse_error:
        errors.append(f"invalid JSON: {parse_error}")
    return dedupe_list(errors)


def processing_plan_llm_request_text(diagnostics):
    if not isinstance(diagnostics, dict):
        return "Unavailable"
    prompt = str(diagnostics.get("prompt") or "").strip()
    source_text = str(diagnostics.get("source_text") or "").strip()
    if not prompt and not source_text:
        return "Unavailable"
    parts = ["OpenAI Responses API input:"]
    parts.append("")
    parts.append("[system]")
    parts.append(prompt or "Unavailable")
    parts.append("")
    parts.append("[user]")
    parts.append(source_text or "Unavailable")
    return "\n".join(parts)


def processing_plan_review_candidate(processing_plan):
    if not isinstance(processing_plan, dict):
        return {}
    plan = processing_plan.get("plan")
    if processing_plan_has_steps(plan) or processing_plan_has_steps(processing_plan.get("invalid_plan")):
        return processing_plan
    for attempt in reversed(processing_plan.get("attempts") or []):
        if not isinstance(attempt, dict):
            continue
        candidate_plan = attempt.get("plan") or attempt.get("invalid_plan") or attempt.get("parsed_plan_before_normalization")
        if processing_plan_has_steps(candidate_plan):
            selected = dict(attempt)
            selected["plan"] = candidate_plan
            return selected
    return processing_plan


def processing_plan_has_steps(plan):
    steps = (plan or {}).get("processing_steps") if isinstance(plan, dict) else None
    return isinstance(steps, list) and bool(steps)


def processing_plan_validation_warnings(normalization_diagnostics):
    warnings = []
    for item in (normalization_diagnostics or {}).get("rejected_steps") or []:
        reason = str((item or {}).get("reason") or "").strip()
        if reason:
            warnings.append(reason)
    return dedupe_list(warnings)


def summarize_processing_plan(plan):
    steps = []
    summarize_processing_steps((plan or {}).get("processing_steps") or [], steps, depth=0)
    return "\n".join(steps) if steps else "No business-processing steps were extracted."


def summarize_processing_steps(steps, lines, depth=0):
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        operation = str(step.get("operation") or "").upper()
        prefix = "  " * depth
        detail = processing_node_summary_detail(step)
        lines.append(f"{prefix}- {operation}{(': ' + detail) if detail else ''}")
        for key, children in processing_node_child_branches(step):
            if children:
                lines.append(f"{prefix}  {key}:")
                summarize_processing_steps(children, lines, depth + 2)
            else:
                lines.append(f"{prefix}  {key}: []")


def processing_step_summary_detail(step):
    return processing_node_summary_detail(step)


def processing_node_summary_detail(step):
    parts = []
    for key, value in (step or {}).items():
        if key in {"step", "operation"} or processing_node_attribute_is_child_branch(key, value):
            continue
        rendered = render_processing_summary_value(key, value)
        if rendered:
            parts.append(rendered)
    return ", ".join(parts)


def processing_node_child_branches(step):
    branches = []
    for key, value in (step or {}).items():
        if processing_node_attribute_is_child_branch(key, value):
            branches.append((key, value))
    return branches


def processing_node_attribute_is_child_branch(key, value):
    if key in {"steps", "then", "else"} and isinstance(value, list):
        return True
    return bool(value) and isinstance(value, list) and all(isinstance(item, dict) and "operation" in item for item in value)


def render_processing_summary_value(key, value):
    if value is None:
        return ""
    if key == "conditions":
        rendered = format_processing_conditions(value)
        return rendered or "conditions=[]"
    if isinstance(value, dict):
        rendered = render_processing_summary_dict(value)
        return f"{key}={rendered}" if rendered else f"{key}={{}}"
    if isinstance(value, list):
        rendered_items = [render_processing_summary_item(item) for item in value]
        rendered_items = [item for item in rendered_items if item]
        return f"{key}=[" + "; ".join(rendered_items) + "]" if rendered_items else f"{key}=[]"
    text = str(value).strip()
    return f"{key}={text}" if text else ""


def render_processing_summary_item(value):
    if isinstance(value, dict):
        comparison = render_processing_comparison(value)
        return comparison or render_processing_summary_dict(value)
    if isinstance(value, list):
        rendered = [render_processing_summary_item(item) for item in value]
        rendered = [item for item in rendered if item]
        return "[" + "; ".join(rendered) + "]"
    return str(value).strip()


def render_processing_summary_dict(value):
    comparison = render_processing_comparison(value)
    if comparison:
        return comparison
    parts = []
    for key, item in (value or {}).items():
        rendered = render_processing_summary_item(item)
        if rendered:
            parts.append(f"{key} -> {rendered}")
        elif item == {}:
            parts.append(f"{key} -> {{}}")
        elif item == []:
            parts.append(f"{key} -> []")
    return ", ".join(parts)


def render_processing_comparison(value):
    if not isinstance(value, dict):
        return ""
    left = str(value.get("left") or value.get("source") or "").strip()
    operator = str(value.get("operator") or "").strip()
    right = str(value.get("right") or value.get("target") or "").strip()
    if left and operator and right:
        return f"{left} {operator} {right}"
    return ""


def legacy_processing_step_summary_detail(step):
    operation = str((step or {}).get("operation") or "").upper()
    if operation == "CALL_FUNCTION":
        return str(step.get("name") or "")
    if operation == "CALL_STATIC_METHOD":
        class_name = str(step.get("class") or "").strip()
        method_name = str(step.get("method") or "").strip()
        return f"{class_name}=>{method_name}" if class_name and method_name else str(step.get("name") or "")
    if operation == "CALL_METHOD":
        object_name = str(step.get("object") or "").strip()
        method_name = str(step.get("method") or "").strip()
        return f"{object_name}->{method_name}" if object_name and method_name else str(step.get("name") or "")
    if operation == "IF":
        return format_processing_conditions(step.get("conditions"))
    parts = []
    for key in ("source", "target", "into", "condition"):
        if step.get(key):
            parts.append(f"{key}={step.get(key)}")
    if step.get("conditions"):
        parts.append(f"conditions={len(step.get('conditions') or [])}")
    return ", ".join(parts)


def format_processing_conditions(conditions):
    if isinstance(conditions, dict):
        conditions = [conditions]
    parts = []
    for condition in conditions or []:
        if not isinstance(condition, dict):
            text = str(condition or "").strip()
            if text:
                parts.append(text)
            continue
        left = str(condition.get("left") or condition.get("source") or "").strip()
        operator = str(condition.get("operator") or "").strip()
        right = str(condition.get("right") or condition.get("target") or "").strip()
        if left and operator and right:
            parts.append(f"{left} {operator} {right}")
        elif left and operator:
            parts.append(f"{left} {operator}")
        elif left:
            parts.append(left)
    return " AND ".join(parts)


def save_processing_plan_context(job_folder, context):
    (Path(job_folder) / PROCESSING_PLAN_CONTEXT_ARTIFACT).write_text(
        json.dumps(context or {}, indent=2),
        encoding="utf-8",
    )


def load_processing_plan_context(jobs_folder, job_id):
    path = Path(jobs_folder) / job_id / PROCESSING_PLAN_CONTEXT_ARTIFACT
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_processing_plan_proposal(jobs_folder, job_id):
    path = Path(jobs_folder) / job_id / PROCESSING_PLAN_PROPOSAL_ARTIFACT
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    payload = ensure_processing_plan_proposal_summary(payload)
    payload = ensure_processing_plan_proposal_llm_request(payload, jobs_folder, job_id)
    return ensure_processing_plan_proposal_variable_contract(payload, jobs_folder, job_id)


def ensure_processing_plan_proposal_summary(payload):
    if not isinstance(payload, dict):
        return payload
    plan = payload.get("plan")
    if not isinstance(plan, dict):
        return payload
    updated = dict(payload)
    updated["summary"] = summarize_processing_plan(plan)
    return updated


def ensure_processing_plan_proposal_llm_request(payload, jobs_folder, job_id):
    if not isinstance(payload, dict) or payload.get("llm_request"):
        return payload
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    candidate = processing_plan_review_candidate(diagnostics)
    prompt = str(candidate.get("prompt") or diagnostics.get("prompt") or "").strip()
    source_text = str(candidate.get("source_text") or diagnostics.get("source_text") or "").strip()
    if not source_text:
        context = load_processing_plan_context(jobs_folder, job_id)
        input_path = Path(context.get("input_path") or "")
        if input_path.is_file():
            source_text = input_path.read_text(encoding="utf-8")
    payload = dict(payload)
    payload["llm_request"] = processing_plan_llm_request_text({"prompt": prompt, "source_text": source_text})
    return payload


def ensure_processing_plan_proposal_variable_contract(payload, jobs_folder, job_id):
    if not isinstance(payload, dict) or payload.get("variable_contract"):
        return payload
    context = load_processing_plan_context(jobs_folder, job_id)
    input_path = Path(context.get("input_path") or "")
    source_text = input_path.read_text(encoding="utf-8") if input_path.is_file() else ""
    updated = dict(payload)
    updated["variable_contract"] = processing_plan_variable_contract(
        updated.get("plan"),
        prompt_text=context.get("prompt_text"),
        source_text=source_text,
        declaration_requirements=context.get("declaration_requirements"),
    )
    return updated


def load_approved_processing_plan(jobs_folder, job_id):
    path = Path(jobs_folder) / job_id / APPROVED_PROCESSING_PLAN_ARTIFACT
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def approve_processing_plan_for_job(jobs_folder, job_id, plan, allow_validation_errors=False):
    job_folder = Path(jobs_folder) / job_id
    approved_path = job_folder / APPROVED_PROCESSING_PLAN_ARTIFACT
    if approved_path.exists():
        raise RuntimeError("Approved processing plan already exists and cannot be overwritten.")
    context = load_processing_plan_context(jobs_folder, job_id)
    declaration_requirements = context.get("declaration_requirements")
    callable_metadata = context.get("callable_metadata")
    prompt_text = context.get("prompt_text")
    normalized = normalize_processing_plan_with_diagnostics(
        plan,
        base_prompt=prompt_text,
        declaration_requirements=declaration_requirements_for_prompt(declaration_requirements),
        callable_metadata=callable_metadata,
    )
    validation = validate_processing_plan(
        normalized["plan"],
        base_prompt=prompt_text,
        declaration_requirements=declaration_requirements_for_prompt(declaration_requirements),
        callable_metadata=callable_metadata,
        normalization_diagnostics=normalized["diagnostics"],
    )
    candidate = {
        "summary": summarize_processing_plan(normalized["plan"]),
        "plan": normalized["plan"],
        "structured_json": json.dumps(normalized["plan"], indent=2, sort_keys=True),
        "validation_errors": validation["errors"],
        "validation_warnings": processing_plan_validation_warnings(normalized["diagnostics"]),
        "variable_contract": processing_plan_variable_contract(
            normalized["plan"],
            prompt_text=prompt_text,
            source_text=Path(context.get("input_path") or "").read_text(encoding="utf-8")
            if Path(context.get("input_path") or "").is_file()
            else "",
            declaration_requirements=declaration_requirements,
        ),
        "diagnostics": {"normalization_diagnostics": normalized["diagnostics"]},
        "approved": False,
    }
    if not validation["valid"] and not allow_validation_errors:
        (job_folder / PROCESSING_PLAN_PROPOSAL_ARTIFACT).write_text(
            json.dumps(candidate, indent=2),
            encoding="utf-8",
        )
        return {"approved": False, "proposal": candidate}
    approved = dict(candidate)
    approved["approved"] = True
    approved_path.write_text(json.dumps(approved, indent=2), encoding="utf-8")
    return {"approved": True, "proposal": approved}


def reject_processing_plan_for_job(jobs_folder, job_id):
    update_progress(
        jobs_folder,
        job_id,
        "Rejected",
        "Processing plan rejected. ABAP generation was not started.",
        stage="processing_plan_rejected",
    )


def dedupe_list(values):
    seen = set()
    result = []
    for value in values or []:
        text = str(value or "")
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def load_report_skeleton(path=REPORT_SKELETON_PATH):
    skeleton_path = Path(path)
    if not skeleton_path.exists():
        raise FileNotFoundError(f"Report skeleton template is missing: {skeleton_path}")
    return skeleton_path.read_text(encoding="utf-8")


def load_database_read_patterns(path=DATABASE_READ_PATTERNS_PATH):
    patterns_path = Path(path)
    if not patterns_path.exists():
        raise FileNotFoundError(f"Database read patterns template is missing: {patterns_path}")
    return patterns_path.read_text(encoding="utf-8")


def render_create_prompt(prompt_template, source_text, report_skeleton, database_read_patterns):
    return (
        prompt_template.replace("{{REPORT_SKELETON}}", report_skeleton)
        .replace("{{DATABASE_READ_PATTERNS}}", database_read_patterns)
        .replace("{{SPECIFICATION}}", source_text)
    )


def merge_validation_issues(existing, extra):
    merged = []
    seen = set()
    for item in list(existing or []) + list(extra or []):
        key = (
            item.get("rule_id"),
            item.get("line_number"),
            item.get("source_line"),
            item.get("proposed_identifier"),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def specification_requests_alv(source_text):
    return bool(re.search(r"\bALV\b|\bREUSE_ALV\b|\bCL_SALV_TABLE\b|\bCL_GUI_ALV_GRID\b", source_text or "", re.IGNORECASE))


def specification_requests_csv(source_text):
    return bool(re.search(r"\bCSV\b|\bcomma[-\s]?separated\b|\bdownload\b|\bexport\b|\bfile\b", source_text or "", re.IGNORECASE))


def specification_requests_output(source_text):
    return bool(
        re.search(
            r"\boutput\b|\bdisplay\b|\bshow\b|\blist\b|\bwrite\b|\bprint\b|\bALV\b|\bCSV\b|\bdownload\b|\bexport\b|\bfile\b",
            source_text or "",
            re.IGNORECASE,
        )
    )


def contract_identifier_suffix(value):
    suffix = re.sub(r"[^0-9A-Za-z]+", "_", str(value or "").lower())
    suffix = re.sub(r"_+", "_", suffix).strip("_")
    return suffix or "data"


def work_area_name_for_ddic_object(item):
    structure = str((item or {}).get("structure") or "")
    if structure.lower().startswith("st_") and len(structure) > 3:
        return "st_" + contract_identifier_suffix(structure[3:])
    return "st_" + contract_identifier_suffix((item or {}).get("name"))


def callable_identities_from_dependency_analysis(dependency_analysis, callable_metadata=None):
    identities = []
    identities.extend((dependency_analysis or {}).get("callables", []) or [])
    for key in normalize_provider_signatures(callable_metadata).keys():
        if is_callable_metadata_key(key):
            continue
        identities.append(key)
    return dedupe_preserve_case(identities)


def is_callable_metadata_key(key):
    return str(key or "") in {
        "callable_signatures",
        "callables",
        "_diagnostics",
        "diagnostics",
        "technical_mapping",
        "callable_mappings",
        "unresolved",
    }


def form_names_for_generation_contract(source_text, ddic_objects, callable_identities=None):
    form_names = []
    for item in ddic_objects:
        form_names.append("read_" + contract_identifier_suffix(item.get("name")))
    output_requested = specification_requests_output(source_text)
    if ddic_objects or callable_identities or output_requested:
        form_names.append("process_data")
    if output_requested:
        form_names.append("output_data")
    if specification_requests_alv(source_text):
        form_names.append("display_alv")
    if specification_requests_csv(source_text):
        form_names.append("write_csv")
    return dedupe_preserve_case(form_names)


def build_generation_contract(source_text, dependency_analysis, ddic_metadata=None, callable_metadata=None):
    ddic_objects = [
        item
        for item in (dependency_analysis or {}).get("ddic_objects", []) or []
        if isinstance(item, dict) and item.get("name") and item.get("structure") and item.get("table")
    ]
    output_fields = []
    for table_metadata in normalized_tables(ddic_metadata or {}).values():
        output_fields.extend(normalized_fields(table_metadata))
    callable_identities = callable_identities_from_dependency_analysis(dependency_analysis, callable_metadata)
    return {
        "internal_tables": [item["table"] for item in ddic_objects],
        "work_areas": [work_area_name_for_ddic_object(item) for item in ddic_objects],
        "output_structure_fields": dedupe_preserve_order(output_fields),
        "form_names": form_names_for_generation_contract(source_text, ddic_objects, callable_identities),
        "callable_identities": callable_identities,
        "ddic_objects": [
            {
                "name": item["name"],
                "structure": item["structure"],
                "table": item["table"],
                "work_area": work_area_name_for_ddic_object(item),
            }
            for item in ddic_objects
        ],
    }


def append_generation_contract(prompt_text, contract):
    contract = contract or {}
    lines = [
        "Shared generation contract:",
        "- Use this same contract in every generated chunk.",
        "- Use these names exactly and do not invent alternative names.",
        "- Do not invent alternative internal-table, work-area, output-field, structure, type, or FORM names.",
        "- Generate simple classical SAP ECC FORM routines.",
        "- Use the program's global variables, internal tables and work areas directly.",
        "- Do not create local declarations inside FORM routines.",
        "- Do not generate DATA, TYPES, CONSTANTS, FIELD-SYMBOLS, RANGES, or STATICS declarations inside any FORM.",
        "- Every variable required by generated forms must be declared globally by the declarations chunk.",
        "- FORM routines must reuse the exact global names from this shared naming contract.",
        "- Do not invent local names such as lt_*, ls_*, lv_*, wa_*, gt_*, gs_*, or gv_*.",
        "- Do not generate USING, CHANGING or TABLES parameters for FORM routines.",
        "- Do not generate USING, CHANGING or TABLES additions on PERFORM statements.",
        "- Only generate FORM parameters if the functional specification explicitly requires data to be passed between forms.",
        "Exact internal-table names: " + comma_or_none(contract.get("internal_tables")),
        "Exact work-area names: " + comma_or_none(contract.get("work_areas")),
        "Exact output structure fields: " + comma_or_none(contract.get("output_structure_fields")),
        "Exact FORM names: " + comma_or_none(contract.get("form_names")),
        "Exact callable identities: " + comma_or_none(contract.get("callable_identities")),
    ]
    for item in contract.get("ddic_objects", []) or []:
        lines.append(
            f"- {item['name']}: structure {item['structure']}, table {item['table']}, work area {item['work_area']}"
        )
    return f"{prompt_text.rstrip()}\n\n" + "\n".join(lines) + "\n"


def comma_or_none(values):
    values = [str(value) for value in values or [] if str(value)]
    return ", ".join(values) if values else "None"


def dedupe_preserve_order(values):
    seen = set()
    result = []
    for value in values or []:
        item = str(value or "").upper()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def dedupe_preserve_case(values):
    seen = set()
    result = []
    for value in values or []:
        item = str(value or "")
        key = item.upper()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def generate_abap_with_orchestrator(
    prompt_text,
    source_text,
    job_folder,
    abap_generator=None,
    progress_callback=None,
    pre_chunk_progress_callback=None,
    callable_metadata=None,
    ddic_metadata=None,
    declaration_requirements=None,
    approved_processing_plan=None,
    model_settings=None,
    final_assembly_mode=None,
):
    generator = abap_generator or generate_abap
    try:
        result = generate_chunked_abap_program(
            prompt_text,
            source_text,
            abap_generator=generator,
            progress_callback=progress_callback,
            pre_chunk_progress_callback=pre_chunk_progress_callback,
            callable_metadata=callable_metadata,
            ddic_metadata=ddic_metadata,
            declaration_requirements=declaration_requirements,
            approved_processing_plan=approved_processing_plan,
            final_assembly_mode=final_assembly_mode,
        )
    except Exception as exc:
        fallback = generator(prompt_text, source_text)
        response_text, model_name, usage = normalize_llm_result(fallback)
        partial_chunks = exc.chunks if isinstance(exc, ChunkedGenerationError) else []
        processing_plan = exc.processing_plan if isinstance(exc, ChunkedGenerationError) else None
        result = {
            "text": response_text,
            "model": model_name,
            "usage": usage,
            "chunks": partial_chunks,
            "used_fallback": True,
            "fallback_reason": f"{type(exc).__name__}: {exc}",
            "processing_plan": processing_plan,
            "final_assembly_mode": final_assembly_mode,
        }
    if isinstance(result, dict):
        result["model_settings"] = model_settings or {}
    save_chunk_diagnostic(job_folder, result)
    return result


def post_generation_llm_output(llm_result, key):
    if not isinstance(llm_result, dict):
        return str(llm_result or "")
    chunks = llm_result.get("chunks") or []
    if chunks:
        blocks = []
        for chunk in chunks:
            blocks.append(
                "\n".join(
                    [
                        f"===== {chunk.get('name', 'chunk')} =====",
                        str(chunk.get(key) or ""),
                    ]
                ).strip()
            )
        return "\n\n".join(blocks)
    return str(llm_result.get("text") or "")


def record_post_generation_stage(diagnostics, stage, source):
    if diagnostics is None:
        return
    diagnostics.setdefault("stages", []).append(
        {
            "stage": stage,
            "source": str(source or ""),
        }
    )


def record_orchestrator_source_stages(diagnostics, llm_result):
    if not isinstance(llm_result, dict):
        record_post_generation_stage(diagnostics, "complete_source_immediately_after_assembly", str(llm_result or ""))
        return
    stages = llm_result.get("post_generation_source_stages") or []
    for stage in stages:
        if isinstance(stage, dict):
            record_post_generation_stage(diagnostics, stage.get("stage", ""), stage.get("source", ""))


def fixer_diagnostic_stages(fix_result):
    diagnostics = (fix_result or {}).get("diagnostics") or {}
    return [
        {"stage": "complete_source_immediately_before_deterministic_fixer", "source": diagnostics.get("source_before_fixer", "")},
        {"stage": "complete_source_immediately_after_deterministic_fixer", "source": diagnostics.get("source_after_fixer", "")},
    ]


def save_post_generation_diagnostics(job_folder, diagnostics):
    (Path(job_folder) / POST_GENERATION_PROCESSING_DIAGNOSTIC).write_text(
        json.dumps(diagnostics or {"stages": []}, indent=2),
        encoding="utf-8",
    )


def record_callable_diagnostics(dependency_analysis, callable_identities):
    dependency_analysis["final_callable_identities"] = dedupe_strings(
        list(dependency_analysis.get("final_callable_identities", dependency_analysis.get("callables", [])))
        + list(callable_identities or [])
    )


def record_callable_signature_diagnostics(dependency_analysis, callable_metadata):
    signatures = (
        (callable_metadata or {}).get("callable_signatures")
        or (callable_metadata or {}).get("callables")
        or {}
    )
    found = sorted(str(name).upper() for name in signatures)
    unresolved = []
    for item in (callable_metadata or {}).get("unresolved", []) or []:
        if isinstance(item, dict):
            unresolved.append(
                {
                    "identity": str(item.get("identity") or "").upper(),
                    "reason": str(item.get("reason") or "signature not retrieved"),
                }
            )
        else:
            unresolved.append({"identity": str(item or "").upper(), "reason": "signature not retrieved"})

    dependency_analysis["callable_signatures_found"] = found
    dependency_analysis["callable_signatures_included_in_prompt"] = found
    dependency_analysis["unresolved_callable_signatures"] = [
        item for item in unresolved if item.get("identity")
    ]


def classify_post_generation_ddic_candidates(source):
    candidates = extract_post_generation_ddic_names_from_source(source)
    ambiguous = extract_ambiguous_standalone_type_like_names_from_source(source)
    local_names = local_identifier_names(source)
    accepted = []
    rejected = [{"name": name, "reason": "ambiguous standalone type"} for name in ambiguous]
    for candidate in candidates:
        reason = post_generation_ddic_rejection_reason(candidate, local_names)
        if reason:
            rejected.append({"name": candidate, "reason": reason})
        else:
            accepted.append(candidate)
    return {
        "candidates": dedupe_strings(candidates),
        "accepted": dedupe_strings(accepted),
        "rejected": dedupe_rejections(rejected),
        "unresolved": dedupe_strings(ambiguous),
    }


def local_identifier_names(source):
    lines = (source or "").splitlines()
    names = set()
    names.update(str(name).upper() for name in collect_declared_names(source or ""))
    names.update(str(name).upper() for name in collect_local_type_declarations(lines))
    return names


def post_generation_ddic_rejection_reason(candidate, local_names):
    name = str(candidate or "").upper()
    if name in local_names:
        return "local identifier"
    if name == "SY" or name.startswith("SY-"):
        return "system field"
    if name in POST_GENERATION_INVALID_DDIC_FRAGMENTS:
        return "ABAP fragment"
    if not is_valid_ddic_object_name(name):
        return "invalid identifier"
    return ""


def record_post_generation_ddic_diagnostics(dependency_analysis, classified):
    dependency_analysis["post_generation_ddic_candidates"] = dedupe_strings(
        list(dependency_analysis.get("post_generation_ddic_candidates", []))
        + list(classified.get("candidates", []))
    )
    dependency_analysis["rejected_post_generation_ddic_candidates"] = dedupe_rejections(
        list(dependency_analysis.get("rejected_post_generation_ddic_candidates", []))
        + list(classified.get("rejected", []))
    )
    dependency_analysis["unresolved"] = dedupe_strings(
        list(dependency_analysis.get("unresolved", []))
        + list(classified.get("unresolved", []))
    )


def dedupe_strings(values):
    seen = set()
    result = []
    for value in values or []:
        item = str(value or "").upper()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def merge_processing_rule_ddic_dependencies(dependency_analysis, source_text):
    if not isinstance(dependency_analysis, dict):
        return
    processing_rules_text = extract_processing_rules_section(source_text)
    typed_dependencies = extract_typed_ddic_dependencies(processing_rules_text)
    names = []
    for dependency in typed_dependencies:
        if dependency.get("kind") in {"ddic_table", "ddic_structure"}:
            name = dependency.get("name")
        elif dependency.get("kind") == "ddic_field":
            name = dependency.get("object")
        else:
            name = None
        if name and name not in names:
            names.append(name)
    dependency_analysis["processing_rule_ddic_objects"] = list(names)
    dependency_analysis["processing_rule_dependencies"] = typed_dependencies
    existing = {str((item or {}).get("name") or "").upper() for item in dependency_analysis.get("ddic_objects", []) if isinstance(item, dict)}
    added = []
    objects = list(dependency_analysis.get("ddic_objects", []) or [])
    for name in names:
        if name in existing:
            continue
        objects.append(ddic_dependency_object(name))
        existing.add(name)
        added.append(name)
    dependency_analysis["ddic_objects"] = objects
    dependency_analysis["processing_rule_ddic_objects_added"] = added
    if added:
        dependency_analysis["unresolved"] = [item for item in dependency_analysis.get("unresolved", []) or [] if str(item).upper() not in set(added)]


def merge_specification_callable_dependencies(dependency_analysis, source_text):
    if not isinstance(dependency_analysis, dict):
        return
    existing = dedupe_strings(dependency_analysis.get("callables", []))
    discovered = extract_specification_callable_identities(source_text)
    added = [name for name in discovered if name not in existing]
    if added:
        dependency_analysis["callables"] = existing + added
    else:
        dependency_analysis["callables"] = existing
    dependency_analysis["specification_callables"] = discovered
    dependency_analysis["specification_callables_added"] = added
    if added:
        added_set = set(added)
        dependency_analysis["unresolved"] = [
            item for item in dependency_analysis.get("unresolved", []) or [] if str(item).upper() not in added_set
        ]


def extract_specification_callable_identities(source_text):
    text = str(source_text or "")
    identities = []
    for match in re.finditer(
        r"\b(?:function\s+module|call\s+function|function)\s+['`\"]?([A-Za-z][A-Za-z0-9_]{1,29})['`\"]?",
        text,
        re.IGNORECASE,
    ):
        append_callable_identity(identities, match.group(1))
    object_classes = declared_reference_object_classes(text)
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]{1,29})\s*(=>|->)\s*([A-Za-z][A-Za-z0-9_]{1,29})\b", text):
        left = match.group(1)
        operator = match.group(2)
        method = match.group(3)
        if operator == "=>":
            append_callable_identity(identities, f"{left}=>{method}")
            continue
        class_name = object_classes.get(left.lower())
        if class_name:
            append_callable_identity(identities, f"{class_name}=>{method}")
    return dedupe_strings(identities)


def declared_reference_object_classes(source_text):
    result = {}
    declaration_text = re.sub(r'"[^\n]*', "", str(source_text or ""))
    for statement in re.finditer(r"\bDATA\s*:?\s*([\s\S]*?)[.]", declaration_text, re.IGNORECASE):
        for part in split_top_level_declaration_parts(statement.group(1)):
            match = re.search(
                r"\b([A-Za-z][A-Za-z0-9_]{1,29})\b\s+TYPE\s+REF\s+TO\s+([A-Za-z][A-Za-z0-9_]{1,29})\b",
                part,
                re.IGNORECASE,
            )
            if match:
                result[match.group(1).lower()] = match.group(2).upper()
    return result


def split_top_level_declaration_parts(value):
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def append_callable_identity(values, value):
    name = str(value or "").strip().upper().replace("~", "=>")
    if name in CALLABLE_PROSE_STOP_WORDS:
        return
    if re.fullmatch(r"[A-Z][A-Z0-9_]{1,29}(?:(?:=>|->)[A-Z][A-Z0-9_]{1,29})?", name):
        values.append(name.replace("->", "=>"))


CALLABLE_PROSE_STOP_WORDS = {
    "A",
    "AN",
    "AND",
    "OR",
    "THE",
    "METHOD",
    "MODULE",
    "FUNCTION",
}

LLM_ABAP_PREFACE_LINE_RE = re.compile(
    r"^\s*here\s+is\s+(?:the\s+)?(?:complete\s+)?(?:corrected|generated|updated)\s+"
    r"(?:abap\s+)?(?:source|code)\s*:?\s*$",
    re.IGNORECASE,
)


def dedupe_rejections(rejections):
    seen = set()
    result = []
    for item in rejections or []:
        name = str((item or {}).get("name") or "").upper()
        reason = str((item or {}).get("reason") or "")
        key = (name, reason)
        if name and key not in seen:
            seen.add(key)
            result.append({"name": name, "reason": reason})
    return result


def remove_llm_abap_preface_lines(text):
    cleaned_lines = []
    changed = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("*", '"')) and LLM_ABAP_PREFACE_LINE_RE.match(stripped):
            changed = True
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines) if changed else text


def clean_response(response_text):
    text = response_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    report_match = re.search(r"(?im)^\s*REPORT\b", text)
    if report_match:
        text = text[report_match.start():].strip()
        lines = text.splitlines()
        if lines and lines[-1].strip() == "```":
            text = "\n".join(lines[:-1]).strip()
    text = remove_llm_abap_preface_lines(text).strip()
    return text


def normalize_llm_result(llm_result):
    if isinstance(llm_result, dict):
        return (
            llm_result.get("text", ""),
            llm_result.get("model") or Config.OPENAI_MODEL,
            llm_result.get("usage"),
        )
    return llm_result, Config.OPENAI_MODEL, None


def enrich_metadata(
    ddic_names,
    callable_identities,
    current_ddic_metadata=None,
    current_callable_metadata=None,
    ddic_provider=None,
    callable_provider=None,
    progress_callback=None,
):
    ddic_metadata = retrieve_missing_ddic_metadata(
        current_ddic_metadata or {"tables": {}},
        ddic_names,
        ddic_provider or NoOpDdicMetadataProvider(),
        progress_callback=progress_callback,
    )
    existing_signatures = (
        (current_callable_metadata or {}).get("callable_signatures")
        or (current_callable_metadata or {}).get("callables")
        or {}
    )
    existing_names = {str(name).upper() for name in existing_signatures}
    missing_identities = [
        identity
        for identity in list(callable_identities or [])
        if identity.upper() not in existing_names
    ]
    callable_metadata = merge_callable_metadata(
        current_callable_metadata,
        resolve_callable_metadata_for_identities(missing_identities, signature_provider=callable_provider),
    )
    return ddic_metadata, callable_metadata


def build_metrics(
    model_name,
    duration_seconds,
    usage,
    prompt_text,
    source_text,
    generated_abap,
    section_durations=None,
    model_settings=None,
    cost_breakdown=None,
):
    input_tokens = usage.get("input_tokens") if usage else None
    output_tokens = usage.get("output_tokens") if usage else None
    total_tokens = usage.get("total_tokens") if usage else None
    costs = calculate_cost(model_name, input_tokens, output_tokens)

    return {
        "model": model_name,
        "duration_seconds": duration_seconds,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_input_cost": costs["input"],
        "estimated_output_cost": costs["output"],
        "estimated_total_cost": costs["total"],
        "prompt_characters": len(prompt_text),
        "specification_characters": len(source_text),
        "generated_abap_characters": len(generated_abap),
        "generated_abap_lines": len(generated_abap.splitlines()) if generated_abap else 0,
        "section_durations": section_durations or {},
        "model_settings": model_settings or {},
        "cost_breakdown": cost_breakdown or empty_cost_breakdown(),
    }


def add_section_duration(section_durations, section_name, duration_seconds):
    if not isinstance(section_durations, dict):
        return
    if not isinstance(duration_seconds, (int, float)):
        section_durations.setdefault(section_name, duration_seconds)
        return
    section_durations[section_name] = section_durations.get(section_name, 0.0) + float(duration_seconds)


def active_processing_duration(section_durations, fallback=None):
    total = 0.0
    found = False
    for duration_seconds in (section_durations or {}).values():
        if isinstance(duration_seconds, (int, float)):
            total += float(duration_seconds)
            found = True
    if found:
        return total
    return fallback


def usage_for_final_metrics(llm_result, usage, prior_usage=None):
    if not prior_usage:
        return usage
    chunk_usage = [
        chunk.get("usage")
        for chunk in (llm_result or {}).get("chunks", [])
        if isinstance(chunk, dict)
    ]
    current_usage = chunk_usage
    if (llm_result or {}).get("used_fallback"):
        current_usage = current_usage + [usage]
    return aggregate_usage([prior_usage] + current_usage) or usage


def calculate_cost(model_name, input_tokens, output_tokens):
    pricing = Config.MODEL_PRICING.get(model_name, {})
    input_rate = pricing.get("input_per_1m_tokens")
    output_rate = pricing.get("output_per_1m_tokens")
    if input_tokens is None or output_tokens is None or input_rate is None or output_rate is None:
        return {"input": None, "output": None, "total": None}

    input_cost = input_tokens * input_rate / 1_000_000
    output_cost = output_tokens * output_rate / 1_000_000
    return {
        "input": input_cost,
        "output": output_cost,
        "total": input_cost + output_cost,
    }


def save_metrics(job_folder, metrics):
    (Path(job_folder) / "metrics.json").write_text(
        json.dumps(metrics, indent=2),
        encoding="utf-8",
    )


def save_model_settings(job_folder, model_settings):
    (Path(job_folder) / "model_settings.json").write_text(
        json.dumps(model_settings or {}, indent=2),
        encoding="utf-8",
    )


def load_metrics(jobs_folder, job_id):
    metrics_path = Path(jobs_folder) / job_id / "metrics.json"
    if not metrics_path.exists():
        return {}
    return normalize_metrics_for_display(
        json.loads(metrics_path.read_text(encoding="utf-8")),
        job_folder=metrics_path.parent,
    )


def normalize_metrics_for_display(metrics, job_folder=None):
    if not isinstance(metrics, dict):
        return metrics
    updated = dict(metrics)
    duration_seconds = active_processing_duration(metrics.get("section_durations"))
    if duration_seconds is None:
        duration_seconds = metrics.get("duration_seconds")
    if duration_seconds is not None:
        updated["duration_seconds"] = duration_seconds
    if any(updated.get(key) is None for key in ("estimated_input_cost", "estimated_output_cost", "estimated_total_cost")):
        costs = calculate_cost(updated.get("model"), updated.get("input_tokens"), updated.get("output_tokens"))
        updated["estimated_input_cost"] = costs["input"]
        updated["estimated_output_cost"] = costs["output"]
        updated["estimated_total_cost"] = costs["total"]
    breakdown = updated.get("cost_breakdown")
    if not cost_breakdown_has_entries(breakdown) and job_folder:
        breakdown = cost_breakdown_from_job_artifacts(job_folder)
    if not cost_breakdown_has_entries(breakdown):
        breakdown = cost_breakdown_from_metrics(updated)
    updated["cost_breakdown"] = breakdown
    return updated


def empty_cost_breakdown():
    return {"by_model": [], "by_stage": []}


def cost_breakdown_has_entries(breakdown):
    return isinstance(breakdown, dict) and (
        bool(breakdown.get("by_model")) or bool(breakdown.get("by_stage"))
    )


def llm_cost_breakdown_from_result(llm_result):
    if not isinstance(llm_result, dict):
        return empty_cost_breakdown()
    rows = []
    append_llm_cost_row(
        rows,
        "declaration_requirements",
        "Declaration requirements",
        (llm_result.get("declaration_requirements") or {}).get("model"),
        (llm_result.get("declaration_requirements") or {}).get("usage"),
    )
    append_llm_cost_row(
        rows,
        "processing_plan",
        "Processing plan",
        (llm_result.get("processing_plan") or {}).get("model"),
        (llm_result.get("processing_plan") or {}).get("usage"),
    )
    for chunk in llm_result.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        chunk_name = str(chunk.get("name") or chunk.get("chunk") or "chunk")
        append_llm_cost_row(
            rows,
            f"chunk:{chunk_name}",
            chunk_cost_stage_label(chunk_name),
            chunk.get("model"),
            chunk.get("usage"),
        )
    if llm_result.get("used_fallback"):
        append_llm_cost_row(
            rows,
            "fallback_generation",
            "Fallback generation",
            llm_result.get("model"),
            llm_result.get("usage"),
        )
    if not rows:
        append_llm_cost_row(rows, "generation", "Generation", llm_result.get("model"), llm_result.get("usage"))
    return build_cost_breakdown(rows)


def cost_breakdown_from_metrics(metrics):
    rows = []
    append_llm_cost_row(
        rows,
        "generation",
        "Generation",
        metrics.get("model"),
        {
            "input_tokens": metrics.get("input_tokens"),
            "output_tokens": metrics.get("output_tokens"),
            "total_tokens": metrics.get("total_tokens"),
        },
    )
    return build_cost_breakdown(rows)


def cost_breakdown_from_job_artifacts(job_folder):
    job_folder = Path(job_folder)
    rows = []
    chunks_path = job_folder / "abap_generation_chunks.json"
    if chunks_path.exists():
        try:
            chunk_diagnostics = json.loads(chunks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            chunk_diagnostics = {}
        rows.extend((llm_cost_breakdown_from_result(chunk_diagnostics).get("by_stage") or []))
    processing_plan_path = job_folder / PROCESSING_PLAN_DIAGNOSTICS_ARTIFACT
    if processing_plan_path.exists():
        try:
            processing_plan_diagnostics = json.loads(processing_plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            processing_plan_diagnostics = {}
        append_llm_cost_row(
            rows,
            "processing_plan",
            "Processing plan",
            processing_plan_diagnostics.get("model"),
            processing_plan_diagnostics.get("usage"),
        )
    if rows:
        return build_cost_breakdown(dedupe_cost_stage_rows(rows))
    enhancement_path = job_folder / "enhancement_diagnostics.json"
    if enhancement_path.exists():
        try:
            diagnostics = json.loads(enhancement_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty_cost_breakdown()
        rows = []
        append_llm_cost_row(rows, "enhancement", "Enhancement", diagnostics.get("model"), diagnostics.get("usage"))
        return build_cost_breakdown(rows)
    return empty_cost_breakdown()


def dedupe_cost_stage_rows(rows):
    result = []
    seen = set()
    for row in rows or []:
        key = (
            row.get("stage"),
            row.get("model"),
            row.get("input_tokens"),
            row.get("output_tokens"),
            row.get("total_tokens"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def append_llm_cost_row(rows, stage, label, model, usage):
    if not isinstance(usage, dict) or not model:
        return
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    if not any(isinstance(value, int) for value in (input_tokens, output_tokens, total_tokens)):
        return
    costs = calculate_cost(model, input_tokens, output_tokens)
    rows.append(
        {
            "stage": stage,
            "label": label,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_input_cost": costs["input"],
            "estimated_output_cost": costs["output"],
            "estimated_total_cost": costs["total"],
        }
    )


def build_cost_breakdown(rows):
    stage_rows = list(rows or [])
    model_totals = {}
    for row in stage_rows:
        model = row.get("model")
        if not model:
            continue
        total = model_totals.setdefault(
            model,
            {
                "model": model,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_input_cost": 0.0,
                "estimated_output_cost": 0.0,
                "estimated_total_cost": 0.0,
            },
        )
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = row.get(key)
            if isinstance(value, int):
                total[key] += value
        for key in ("estimated_input_cost", "estimated_output_cost", "estimated_total_cost"):
            value = row.get(key)
            if total.get(key) is None or not isinstance(value, (int, float)):
                total[key] = None
            else:
                total[key] += float(value)
    return {
        "by_model": list(model_totals.values()),
        "by_stage": stage_rows,
    }


def chunk_cost_stage_label(chunk_name):
    labels = {
        "declarations": "Declarations chunk",
        "database_read_forms": "Database read chunk",
        "processing_form": "Processing form chunk",
        "output_forms": "Output forms chunk",
        "main_program_flow": "Main flow chunk",
    }
    return labels.get(str(chunk_name or ""), str(chunk_name or "Chunk").replace("_", " ").title())


def save_validation_issues(job_folder, issues):
    (Path(job_folder) / "validation_issues.json").write_text(
        json.dumps(issues, indent=2),
        encoding="utf-8",
    )


def save_ddic_metadata(job_folder, metadata):
    (Path(job_folder) / "ddic_metadata.json").write_text(
        json.dumps(metadata or {"tables": {}}, indent=2),
        encoding="utf-8",
    )


def save_dependency_analysis(job_folder, analysis):
    (Path(job_folder) / "dependency_analysis.json").write_text(
        json.dumps(analysis or {}, indent=2),
        encoding="utf-8",
    )


def maybe_run_sap_syntax_check(
    job_folder,
    jobs_folder,
    job_id,
    source_code,
    sap_syntax_checker,
    code_review_repairer=None,
    callable_metadata=None,
    post_generation_diagnostics=None,
):
    options = load_job_options(jobs_folder, job_id)
    diagnostic = default_syntax_repair_flow_diagnostic(options)
    record_post_generation_stage(post_generation_diagnostics, "before_sap_syntax_check", source_code)
    if not options.get("run_sap_syntax_check"):
        diagnostic["llm_repair_skip_reason"] = "Run SAP syntax check checkbox was not enabled."
        save_syntax_repair_flow_diagnostic(job_folder, diagnostic)
        record_post_generation_stage(post_generation_diagnostics, "after_sap_syntax_check", source_code)
        return source_code
    update_progress(
        jobs_folder,
        job_id,
        "Running",
        "Running SAP syntax check attempt 1 of "
        f"{int(options.get('sap_syntax_check_attempts', 2))}...",
        stage="Running SAP syntax check",
    )
    checker = sap_syntax_checker
    max_syntax_check_attempts = int(options.get("sap_syntax_check_attempts", 2))
    diagnostic["sap_syntax_api_called"] = bool(checker)
    diagnostic["final_abap_source_sent_to_final_sap_syntax_check"] = source_code if checker else ""
    first_result = normalize_empty_sap_syntax_diagnostic_result(checker.check(source_code)) if checker else {
        "requested": True,
        "status": "unavailable",
        "passed": False,
        "errors": [],
        "raw_response": "",
        "technical_message": "SAP syntax check unavailable: checker is not configured",
    }
    diagnostic["syntax_api_response_status"] = first_result.get("status", "")
    diagnostic["normalized_syntax_errors"] = first_result.get("errors", [])
    syntax_check_attempts = [sap_syntax_check_attempt_payload(1, first_result)]
    result = {
        **first_result,
        "initial_result": first_result,
        "repair_attempted": False,
        "repair": None,
        "attempts": syntax_check_attempts,
        "final_result": first_result,
    }
    if not syntax_errors(first_result):
        diagnostic["llm_repair_skip_reason"] = syntax_repair_skip_reason(first_result)
        save_sap_syntax_check(job_folder, result)
        save_syntax_repair_flow_diagnostic(job_folder, diagnostic)
        require_sap_syntax_success(result)
        return source_code
    if max_syntax_check_attempts <= 1:
        diagnostic["llm_repair_skip_reason"] = "SAP syntax check attempt limit reached before LLM repair."
        save_sap_syntax_check(job_folder, result)
        save_syntax_repair_flow_diagnostic(job_folder, diagnostic)
        require_sap_syntax_success(result)
        return source_code

    repairer = code_review_repairer or generate_code_review_repair
    current_source = source_code
    current_result = first_result
    repairs = []
    syntax_check_count = 1
    while syntax_errors(current_result) and syntax_check_count < max_syntax_check_attempts:
        update_progress(
            jobs_folder,
            job_id,
            "Running",
            f"Repairing SAP syntax errors after attempt {syntax_check_count}...",
            stage="Repairing SAP syntax errors",
        )
        repair_prompt = build_sap_syntax_repair_prompt(current_result.get("errors", []))
        repair_target = syntax_repair_target_for_errors(current_source, current_result.get("errors", []))
        source_for_repair = repair_target["source"] if repair_target else current_source
        diagnostic["llm_repair_invoked"] = True
        diagnostic["llm_repair_skip_reason"] = ""
        diagnostic["llm_repair_prompt"] = repair_prompt
        diagnostic["abap_source_passed_to_repair_llm"] = source_for_repair
        record_post_generation_stage(post_generation_diagnostics, "before_syntax_repair_preparation", current_source)
        record_post_generation_stage(post_generation_diagnostics, "source_passed_to_syntax_repair_llm", source_for_repair)
        repair_llm_result = repairer(repair_prompt, source_for_repair)
        repair_response_text, repair_model_name, repair_usage = normalize_llm_result(repair_llm_result)
        diagnostic["raw_llm_response"] = repair_response_text
        repaired_abap = clean_response(repair_response_text)
        diagnostic["raw_abap_returned_by_repair_llm"] = repaired_abap
        record_post_generation_stage(post_generation_diagnostics, "after_syntax_repair_cleanup", repaired_abap)
        if repair_target:
            if has_standalone_end_statement(repaired_abap):
                diagnostic["llm_repair_skip_reason"] = "Syntax repair returned standalone END. in a FORM repair."
                save_sap_syntax_check(job_folder, result)
                save_syntax_repair_flow_diagnostic(job_folder, diagnostic)
                raise RuntimeError("Syntax repair returned standalone END. in a FORM repair.")
            repaired_abap = rebuild_report_with_repaired_form(current_source, repair_target, repaired_abap)
            record_post_generation_stage(post_generation_diagnostics, "after_syntax_repair_rebuild", repaired_abap)
        repair_fix_result = auto_fix_abap(
            repaired_abap,
            callable_signatures=(callable_metadata or {}).get("callable_signatures") or (callable_metadata or {}).get("callables"),
            callable_mappings=callable_metadata or {},
        )
        repaired_abap = repair_fix_result["fixed_source"]
        record_post_generation_stage(post_generation_diagnostics, "after_syntax_repair_deterministic_fixer", repaired_abap)
        diagnostic["abap_after_deterministic_repairs"] = repaired_abap
        diagnostic["deterministic_repairs_ran_after_llm_repair"] = True
        diagnostic["deterministic_repair_fixes_after_llm_repair"] = repair_fix_result["fixes"]
        diagnostic["bapi_message_getdetail_fix_ran_after_llm_repair"] = bapi_message_getdetail_fix_ran(repair_fix_result)
        diagnostic["bapi_message_getdetail_repaired_result_stored"] = bool(
            diagnostic["bapi_message_getdetail_fix_ran_after_llm_repair"]
            and repaired_abap == repair_fix_result["fixed_source"]
        )
        diagnostic["repaired_abap_replaced_original"] = repaired_abap != current_source
        diagnostic["second_sap_syntax_api_called"] = bool(checker)
        diagnostic["exact_abap_sent_to_second_sap_syntax_api"] = repaired_abap
        diagnostic["bapi_message_getdetail_repaired_result_passed_forward"] = bool(
            diagnostic["bapi_message_getdetail_fix_ran_after_llm_repair"]
            and diagnostic["exact_abap_sent_to_second_sap_syntax_api"] == repaired_abap
        )
        diagnostic["final_abap_source_sent_to_final_sap_syntax_check"] = repaired_abap if checker else ""
        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Running SAP syntax check attempt "
            f"{syntax_check_count + 1} of {max_syntax_check_attempts}...",
            stage="Running SAP syntax check",
        )
        current_result = normalize_empty_sap_syntax_diagnostic_result(checker.check(repaired_abap)) if checker else current_result
        syntax_check_count += 1
        diagnostic["second_syntax_check_result"] = current_result
        syntax_check_attempts.append(sap_syntax_check_attempt_payload(syntax_check_count, current_result))
        current_source = repaired_abap
        repairs.append(
            {
                "prompt": repair_prompt,
                "raw_model_response": repair_response_text,
                "model": repair_model_name,
                "usage": repair_usage,
                "repaired_abap": repaired_abap,
                "syntax_result": current_result,
                "deterministic_fix_summary": {
                    "original_issue_count": repair_fix_result["original_issue_count"],
                    "final_issue_count": repair_fix_result["final_issue_count"],
                    "fixes": repair_fix_result["fixes"],
                },
            }
        )
    repaired_abap = current_source
    final_result = current_result
    repair = repairs[-1] if repairs else None
    result = {
        **final_result,
        "initial_result": first_result,
        "repair_attempted": bool(repairs),
        "repair": repair,
        "repairs": repairs,
        "attempts": syntax_check_attempts,
        "syntax_check_attempts": syntax_check_count,
        "max_syntax_check_attempts": max_syntax_check_attempts,
        "final_result": final_result,
    }
    save_sap_syntax_check(job_folder, result)
    if repairs:
        (Path(job_folder) / "sap_syntax_repaired.abap").write_text(repaired_abap, encoding="utf-8")
    save_syntax_repair_flow_diagnostic(job_folder, diagnostic)
    require_sap_syntax_success(result)
    record_post_generation_stage(post_generation_diagnostics, "after_sap_syntax_check", repaired_abap)
    return repaired_abap


def require_sap_syntax_success(result):
    if result and result.get("status") == "passed" and result.get("passed") is True:
        return
    if result and result.get("status") == "failed" and not syntax_errors(result):
        return
    raise RuntimeError(sap_syntax_failure_message(result))


def sap_syntax_failure_message(result):
    status = (result or {}).get("status", "unknown")
    errors = (result or {}).get("errors") or []
    if errors:
        message = errors[0].get("message") or "SAP syntax errors remain."
        return f"Final SAP syntax check did not succeed ({status}): {message}"
    technical_message = (result or {}).get("technical_message")
    if technical_message:
        return f"Final SAP syntax check did not succeed ({status}): {technical_message}"
    return f"Final SAP syntax check did not succeed ({status})."


def sap_syntax_check_attempt_payload(number, result):
    result = normalize_empty_sap_syntax_diagnostic_result(result or {})
    return {
        "number": number,
        "status": result.get("status", "unknown"),
        "passed": bool(result.get("passed")),
        "errors": result.get("errors") or [],
        "technical_message": result.get("technical_message") or "",
        "raw_response": result.get("raw_response") or "",
    }


def syntax_repair_target_for_errors(source_code, errors):
    ranges = form_ranges(source_code)
    if not ranges:
        return None

    target = None
    for error in errors or []:
        line_number = error.get("line")
        if not isinstance(line_number, int):
            continue
        line_index = line_number - 1
        matching_range = next(
            (form_range for form_range in ranges if form_range["start"] <= line_index < form_range["end"]),
            None,
        )
        if not matching_range:
            return None
        if target and (target["start"], target["end"]) != (matching_range["start"], matching_range["end"]):
            return None
        target = matching_range

    if not target:
        return None
    return {
        **target,
        "source": "".join(source_lines_with_endings(source_code)[target["start"]:target["end"]]),
    }


def form_ranges(source_code):
    ranges = []
    start = None
    for index, line in enumerate(source_lines_with_endings(source_code)):
        if start is None and re.match(r"^\s*FORM\b", line, flags=re.IGNORECASE):
            start = index
        elif start is not None and re.match(r"^\s*ENDFORM\b", line, flags=re.IGNORECASE):
            ranges.append({"start": start, "end": index + 1})
            start = None
    return ranges


def rebuild_report_with_repaired_form(source_code, target, repaired_source):
    lines = source_lines_with_endings(source_code)
    replacement = ensure_source_ends_like_line(repaired_source, lines, target["end"])
    return "".join(lines[:target["start"]]) + replacement + "".join(lines[target["end"]:])


def has_standalone_end_statement(source_code):
    return any(
        re.match(r"^\s*END\s*\.\s*$", line, flags=re.IGNORECASE)
        for line in source_lines_with_endings(source_code)
    )


def source_lines_with_endings(source_code):
    return (source_code or "").splitlines(keepends=True)


def ensure_source_ends_like_line(source_code, lines, end_index):
    if not source_code or source_code.endswith(("\n", "\r")) or end_index >= len(lines):
        return source_code
    return source_code + "\n"


def syntax_errors(result):
    return bool(result and result.get("status") == "failed" and actionable_sap_syntax_errors(result))


def actionable_sap_syntax_errors(result):
    return [
        error for error in (result or {}).get("errors") or []
        if not is_empty_sap_syntax_error(error)
    ]


def is_empty_sap_syntax_error(error):
    if not isinstance(error, dict):
        return False
    line = error.get("line")
    has_empty_line = line in (None, "", 0, "0")
    return (
        has_empty_line
        and not str(error.get("message") or "").strip()
        and not str(error.get("word") or "").strip()
        and not str(error.get("source_line") or "").strip()
    )


def normalize_empty_sap_syntax_diagnostic_result(result):
    if not isinstance(result, dict):
        return result
    if result.get("status") != "failed":
        return result
    errors = result.get("errors") or []
    if errors and not actionable_sap_syntax_errors(result):
        normalized = dict(result)
        normalized["status"] = "passed"
        normalized["passed"] = True
        normalized["errors"] = []
        return normalized
    return result


def syntax_repair_skip_reason(result):
    if not result:
        return "SAP syntax check did not return a result."
    if result.get("status") != "failed":
        return f"SAP syntax check status was {result.get('status', 'unknown')}; repair only runs for failed syntax checks."
    if not result.get("errors"):
        return "SAP syntax check returned no normalized syntax errors."
    return ""


def bapi_message_getdetail_fix_ran(repair_fix_result):
    for fix in (repair_fix_result or {}).get("fixes", []):
        text = f"{fix.get('rule_id', '')} {fix.get('description', '')}".upper()
        if "BAPI_MESSAGE_GETDETAIL" in text:
            return True
    return False


def default_syntax_repair_flow_diagnostic(options):
    return {
        "checkbox_enabled": bool((options or {}).get("run_sap_syntax_check")),
        "sap_syntax_api_called": False,
        "syntax_api_response_status": "",
        "normalized_syntax_errors": [],
        "llm_repair_invoked": False,
        "llm_repair_skip_reason": "",
        "llm_repair_prompt": "",
        "abap_source_passed_to_repair_llm": "",
        "raw_llm_response": "",
        "raw_abap_returned_by_repair_llm": "",
        "abap_after_deterministic_repairs": "",
        "exact_abap_sent_to_second_sap_syntax_api": "",
        "final_abap_source_sent_to_final_sap_syntax_check": "",
        "deterministic_repairs_ran_after_llm_repair": False,
        "deterministic_repair_fixes_after_llm_repair": [],
        "bapi_message_getdetail_fix_ran_after_llm_repair": False,
        "bapi_message_getdetail_repaired_result_stored": False,
        "bapi_message_getdetail_repaired_result_passed_forward": False,
        "repaired_abap_replaced_original": False,
        "second_sap_syntax_api_called": False,
        "second_syntax_check_result": None,
        "syntax_repair_prompt_source_routing": {
            "system_prompt": "llm_repair_prompt contains repair instructions and normalized SAP syntax errors.",
            "complete_abap_source": "supplied as the code-review model user message via repairer(repair_prompt, source_code).",
            "callable_metadata": "not supplied to the code-review model; supplied only to deterministic repairs via auto_fix_abap(callable_signatures=..., callable_mappings=...).",
        },
    }


def save_syntax_repair_flow_diagnostic(job_folder, diagnostic):
    lines = [
        "Syntax errors returned by SAP syntax API:",
        json.dumps(diagnostic["normalized_syntax_errors"], indent=2),
        "Complete ABAP source passed to repair LLM:",
        diagnostic["abap_source_passed_to_repair_llm"] or "None",
        "Complete raw response returned by repair LLM:",
        diagnostic["raw_llm_response"] or "None",
        "Complete final ABAP source sent to final SAP syntax check:",
        diagnostic["final_abap_source_sent_to_final_sap_syntax_check"] or "None",
    ]
    (Path(job_folder) / "diagnostic_syntax_repair_flow.txt").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def build_sap_syntax_repair_prompt(errors):
    return (
        "You are repairing an existing ABAP program after SAP syntax validation.\n\n"
        "Correct only the reported SAP syntax errors.\n"
        "Make the smallest possible changes.\n"
        "Do not rewrite unrelated code.\n"
        "Preserve the existing logic, declarations, formatting and comments.\n"
        "Return the complete corrected ABAP source only. Do not use Markdown fences.\n\n"
        "When repairing a LOOP AT statement for an internal table without a header line:\n\n"
        "- Do not introduce inline declarations.\n"
        "- Do not use ASSIGNING FIELD-SYMBOL(...).\n"
        "- Use a separately declared work area.\n"
        "- Use classic ECC-compatible syntax:\n\n"
        "DATA w_line TYPE <table_line_type>.\n"
        "LOOP AT internal_table INTO w_line.\n\n"
        "- Update references inside the loop from internal_table-field to w_line-field.\n"
        "- Reuse an existing compatible work area when one already exists.\n"
        "- Choose a work-area name derived from the internal table name.\n\n"
        "Apply this rule generically to any internal table, not only t_edids.\n\n"
        "Normalized SAP syntax errors:\n"
        f"{json.dumps(errors or [], indent=2)}"
    )


def save_sap_syntax_check(job_folder, result):
    (Path(job_folder) / "sap_syntax_check.json").write_text(
        json.dumps(result or {}, indent=2),
        encoding="utf-8",
    )


def load_sap_syntax_check(jobs_folder, job_id, options=None):
    syntax_path = Path(jobs_folder) / job_id / "sap_syntax_check.json"
    if syntax_path.exists():
        payload = json.loads(syntax_path.read_text(encoding="utf-8"))
        return normalize_sap_syntax_check_for_display(payload)
    requested = bool((options or {}).get("run_sap_syntax_check"))
    return normalize_sap_syntax_check_for_display({
        "requested": requested,
        "status": "not_requested" if not requested else "pending",
        "passed": False,
        "errors": [],
        "raw_response": "",
        "technical_message": "",
    })


def normalize_sap_syntax_check_for_display(payload):
    payload = dict(payload or {})
    attempts = payload.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        attempts = synthesized_sap_syntax_check_attempts(payload)
    payload["attempts"] = [
        sap_syntax_check_attempt_payload(index, attempt)
        for index, attempt in enumerate(attempts, start=1)
    ]
    return payload


def synthesized_sap_syntax_check_attempts(payload):
    payload = payload or {}
    attempts = []
    expected_count = sap_syntax_expected_attempt_count(payload)
    initial_result = payload.get("initial_result")
    if isinstance(initial_result, dict):
        attempts.append(initial_result)
    elif payload.get("requested"):
        attempts.append(payload)

    repairs = payload.get("repairs") if isinstance(payload.get("repairs"), list) else []
    for repair in repairs:
        if isinstance(repair, dict) and isinstance(repair.get("syntax_result"), dict):
            attempts.append(repair["syntax_result"])

    final_result = payload.get("final_result")
    if isinstance(final_result, dict):
        if expected_count and len(attempts) < expected_count:
            while len(attempts) < expected_count - 1:
                attempts.append(sap_syntax_unrecorded_attempt_payload(len(attempts) + 1))
            attempts.append(final_result)
        elif not syntax_attempts_include_result(attempts, final_result):
            attempts.append(final_result)

    return attempts


def sap_syntax_expected_attempt_count(payload):
    try:
        value = int((payload or {}).get("syntax_check_attempts") or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def sap_syntax_unrecorded_attempt_payload(number):
    return {
        "number": number,
        "status": "not_recorded",
        "passed": False,
        "errors": [],
        "technical_message": "This syntax-check attempt was not recorded by the saved job artifact.",
        "raw_response": "",
    }


def syntax_attempts_include_result(attempts, result):
    for attempt in attempts:
        if (
            isinstance(attempt, dict)
            and attempt.get("status") == result.get("status")
            and attempt.get("passed") == result.get("passed")
            and attempt.get("errors") == result.get("errors")
            and attempt.get("raw_response") == result.get("raw_response")
            and attempt.get("technical_message") == result.get("technical_message")
        ):
            return True
    return False


def load_dependency_analysis(jobs_folder, job_id):
    analysis_path = Path(jobs_folder) / job_id / "dependency_analysis.json"
    if not analysis_path.exists():
        return None
    return json.loads(analysis_path.read_text(encoding="utf-8"))


def load_abap_generation_chunks(jobs_folder, job_id):
    diagnostics = load_abap_generation_diagnostics(jobs_folder, job_id)
    chunks = diagnostics.get("chunks", []) if isinstance(diagnostics, dict) else []
    return chunks if isinstance(chunks, list) else []


def load_abap_generation_diagnostics(jobs_folder, job_id):
    chunks_path = Path(jobs_folder) / job_id / "abap_generation_chunks.json"
    if not chunks_path.exists():
        return {}
    diagnostics = json.loads(chunks_path.read_text(encoding="utf-8"))
    if not isinstance(diagnostics, dict):
        return {}
    diagnostics = dict(diagnostics)
    if not isinstance(diagnostics.get("declaration_requirements"), dict):
        context = load_processing_plan_context(jobs_folder, job_id)
        declaration_requirements = context.get("declaration_requirements")
        if isinstance(declaration_requirements, dict):
            diagnostics["declaration_requirements"] = declaration_requirements
    if not processing_plan_diagnostic_has_llm_trace(diagnostics.get("processing_plan")):
        processing_plan_diagnostics = load_processing_plan_diagnostics(jobs_folder, job_id)
        if processing_plan_diagnostics:
            existing = diagnostics.get("processing_plan") if isinstance(diagnostics.get("processing_plan"), dict) else {}
            merged = dict(processing_plan_diagnostics)
            merged.update({key: value for key, value in existing.items() if value is not None})
            diagnostics["processing_plan"] = merged
    diagnostics["chunks"] = enrich_chunk_subtitles(diagnostics.get("chunks"))
    return diagnostics


def enrich_chunk_subtitles(chunks):
    if not isinstance(chunks, list):
        return chunks
    enriched = []
    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            enriched.append(chunk)
            continue
        item = dict(chunk)
        if not item.get("subtitle"):
            subtitle = subtitle_from_chunk_processing_plan(item)
            if subtitle:
                item["subtitle"] = subtitle
        enriched.append(item)
    return enriched


def subtitle_from_chunk_processing_plan(chunk):
    if (chunk or {}).get("name") != "processing_form":
        return ""
    processing_plan = (chunk or {}).get("processing_plan")
    if not isinstance(processing_plan, dict):
        return ""
    plan = processing_plan_payload(processing_plan.get("plan") or processing_plan)
    steps = plan.get("processing_steps") if isinstance(plan, dict) else []
    if not isinstance(steps, list) or len(steps) != 1:
        return ""
    index = processing_plan.get("top_level_step_index")
    return processing_step_subtitle(steps[0], index if isinstance(index, int) else None)


def load_processing_plan_diagnostics(jobs_folder, job_id):
    path = Path(jobs_folder) / job_id / PROCESSING_PLAN_DIAGNOSTICS_ARTIFACT
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def processing_plan_diagnostic_has_llm_trace(value):
    if not isinstance(value, dict):
        return False
    return any(value.get(key) for key in ("prompt", "raw_response", "duration_seconds", "source_text"))


def load_ddic_metadata(jobs_folder, job_id):
    metadata_path = Path(jobs_folder) / job_id / "ddic_metadata.json"
    if not metadata_path.exists():
        return {"tables": {}}
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def load_validation_issues(jobs_folder, job_id):
    issues_path = Path(jobs_folder) / job_id / "validation_issues.json"
    if not issues_path.exists():
        return []
    return json.loads(issues_path.read_text(encoding="utf-8"))


def save_fix_summary(job_folder, fix_result):
    summary = {
        "original_issue_count": fix_result["original_issue_count"],
        "final_issue_count": fix_result["final_issue_count"],
        "fixes": fix_result["fixes"],
        "diagnostics": fix_result.get("diagnostics", {}),
    }
    (Path(job_folder) / "fix_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )


def load_fix_summary(jobs_folder, job_id):
    summary_path = Path(jobs_folder) / job_id / "fix_summary.json"
    if not summary_path.exists():
        return {"original_issue_count": 0, "final_issue_count": 0, "fixes": []}
    return json.loads(summary_path.read_text(encoding="utf-8"))
