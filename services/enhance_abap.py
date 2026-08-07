import json
import time
from pathlib import Path
from threading import Thread

from services.callable_signature_provider import callable_identities_from_source
from services.create_abap import (
    active_processing_duration,
    add_section_duration,
    build_metrics,
    classify_post_generation_ddic_candidates,
    clean_response,
    enrich_metadata,
    fixer_diagnostic_stages,
    llm_cost_breakdown_from_result,
    maybe_run_sap_syntax_check,
    merge_processing_rule_ddic_dependencies,
    merge_specification_callable_dependencies,
    normalize_llm_result,
    record_callable_diagnostics,
    record_callable_signature_diagnostics,
    record_post_generation_ddic_diagnostics,
    record_post_generation_stage,
    save_ddic_metadata,
    save_dependency_analysis,
    save_fix_summary,
    save_model_settings,
    save_post_generation_diagnostics,
    save_validation_issues,
)
from services.ddic_metadata_context import (
    append_callable_catalogue,
    append_ddic_catalogue,
)
from services.fixer import auto_fix_abap
from services.job_options import load_job_options
from services.llm import generate_code_review_repair, reset_current_model_settings, set_current_model_settings
from services.modifier_guardrails import build_identifier_provenance
from services.progress import update_progress
from services.sap_dependency_analysis import analyze_sap_dependencies, normalize_identifiers
from services.validator import parse_callable_invocations, validate_abap


def start_enhance_abap_job(
    job_id,
    source_path,
    specification_path,
    jobs_folder,
    prompt_path,
    callable_metadata=None,
    signature_provider=None,
    ddic_metadata_provider=None,
    sap_syntax_checker=None,
    enhancement_generator=None,
    code_review_repairer=None,
    dependency_analyzer=None,
):
    thread = Thread(
        target=run_enhance_abap,
        kwargs={
            "job_id": job_id,
            "source_path": source_path,
            "specification_path": specification_path,
            "jobs_folder": jobs_folder,
            "prompt_path": prompt_path,
            "callable_metadata": callable_metadata or {},
            "signature_provider": signature_provider,
            "ddic_metadata_provider": ddic_metadata_provider,
            "sap_syntax_checker": sap_syntax_checker,
            "enhancement_generator": enhancement_generator,
            "code_review_repairer": code_review_repairer,
            "dependency_analyzer": dependency_analyzer,
        },
        daemon=True,
    )
    thread.start()
    return thread


def run_enhance_abap(
    job_id,
    source_path,
    specification_path,
    jobs_folder,
    prompt_path,
    callable_metadata=None,
    signature_provider=None,
    ddic_metadata_provider=None,
    sap_syntax_checker=None,
    enhancement_generator=None,
    code_review_repairer=None,
    dependency_analyzer=None,
):
    job_folder = Path(jobs_folder) / job_id
    job_folder.mkdir(parents=True, exist_ok=True)
    options = load_job_options(jobs_folder, job_id)
    model_settings = options.get("model_settings") or {}
    post_generation_diagnostics = {"stages": [], "model_settings": model_settings}
    section_durations = {}
    model_settings_token = set_current_model_settings(model_settings)

    try:
        save_model_settings(job_folder, model_settings)
        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Reading existing ABAP and enhancement specification...",
            stage="Reading specification",
        )
        existing_abap = Path(source_path).read_text(encoding="utf-8")
        enhancement_specification = Path(specification_path).read_text(encoding="utf-8")
        source_context = enhancement_source_context(existing_abap, enhancement_specification)
        (job_folder / "original_existing.abap").write_text(existing_abap, encoding="utf-8")
        (job_folder / "enhancement_specification.txt").write_text(enhancement_specification, encoding="utf-8")

        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Loading enhancement prompt...",
            stage="Loading prompt and templates",
        )
        prompt_template = Path(prompt_path).read_text(encoding="utf-8")
        prompt_text = render_enhance_prompt(prompt_template, existing_abap, enhancement_specification)

        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Analyzing SAP dependencies...",
            stage="Analyzing dependencies",
        )
        dependency_analysis = analyze_sap_dependencies(
            source_context,
            enabled=True if dependency_analyzer else None,
            llm_analyzer=dependency_analyzer,
        )
        dependency_analysis.setdefault("_diagnostics", {})["model_settings"] = model_settings
        merge_processing_rule_ddic_dependencies(dependency_analysis, enhancement_specification)
        merge_specification_callable_dependencies(dependency_analysis, enhancement_specification)
        add_section_duration(
            section_durations,
            "dependency_analysis",
            (dependency_analysis.get("_diagnostics") or {}).get("duration_seconds")
            if isinstance(dependency_analysis, dict)
            else None,
        )
        dependency_analysis["job_mode"] = "enhance_existing_abap"
        dependency_analysis["pre_generation_ddic_objects"] = list(dependency_analysis.get("ddic_objects", []))
        dependency_analysis["pre_generation_callable_identities"] = list(dependency_analysis.get("callables", []))

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
        pre_generation_ddic_names = normalize_identifiers(dependency_analysis.get("ddic_objects", []), key="name")
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

        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Enhancing existing ABAP...",
            stage="generating_abap",
        )
        started_at = time.perf_counter()
        generator = enhancement_generator or generate_code_review_repair
        llm_result = generator(prompt_text, source_context)
        duration_seconds = time.perf_counter() - started_at
        add_section_duration(section_durations, "generated_abap", duration_seconds)

        response_text, model_name, usage = normalize_llm_result(llm_result)
        enhanced_abap = clean_response(response_text)
        record_post_generation_stage(post_generation_diagnostics, "original_existing_abap", existing_abap)
        record_post_generation_stage(post_generation_diagnostics, "raw_enhanced_abap_from_llm", enhanced_abap)
        save_enhancement_diagnostics(
            job_folder,
            {
                "prompt": prompt_text,
                "source_context": source_context,
                "raw_response": response_text,
                "model": model_name,
                "usage": usage,
                "model_settings": model_settings,
            },
        )

        generated_ddic = classify_post_generation_ddic_candidates(enhanced_abap)
        record_post_generation_ddic_diagnostics(dependency_analysis, generated_ddic)
        generated_callables = callable_identities_from_source(enhanced_abap, parse_callable_invocations)
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
            enhanced_abap,
            callable_signatures=(callable_metadata or {}).get("callable_signatures") or (callable_metadata or {}).get("callables"),
            callable_mappings=callable_metadata or {},
            progress_callback=update_fix_progress,
        )
        add_section_duration(section_durations, "auto_fix", time.monotonic() - auto_fix_started_at)
        final_abap = fix_result["fixed_source"]
        for stage in fixer_diagnostic_stages(fix_result):
            record_post_generation_stage(post_generation_diagnostics, stage["stage"], stage["source"])

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
        validation_issues = list(fix_result["final_issues"])
        provenance = build_identifier_provenance(
            original_source=existing_abap,
            functional_specification=enhancement_specification,
            sap_metadata=ddic_metadata,
        )
        validation_issues = merge_validation_issues_for_enhancement(
            validation_issues,
            validate_abap(final_abap, identifier_provenance=provenance),
        )
        add_section_duration(section_durations, "validation", time.monotonic() - validation_started_at)

        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Saving enhanced ABAP and metrics...",
            stage="Saving results",
        )
        record_post_generation_stage(post_generation_diagnostics, "complete_source_before_optional_sap_syntax_check_save", final_abap)
        (job_folder / "original_generated.abap").write_text(enhanced_abap, encoding="utf-8")
        (job_folder / "generated.abap").write_text(final_abap, encoding="utf-8")
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
            final_abap,
            sap_syntax_checker,
            code_review_repairer=code_review_repairer,
            callable_metadata=callable_metadata,
            post_generation_diagnostics=post_generation_diagnostics,
        )
        add_section_duration(section_durations, "sap_syntax_check", time.monotonic() - sap_syntax_started_at)
        record_post_generation_stage(post_generation_diagnostics, "complete_source_immediately_before_final_save", final_abap)
        (job_folder / "generated.abap").write_text(final_abap, encoding="utf-8")
        save_post_generation_diagnostics(job_folder, post_generation_diagnostics)

        metrics = build_metrics(
            model_name=model_name,
            duration_seconds=active_processing_duration(section_durations, fallback=duration_seconds),
            usage=usage,
            prompt_text=prompt_text,
            source_text=source_context,
            generated_abap=final_abap,
            section_durations=section_durations,
            model_settings=model_settings,
            cost_breakdown=llm_cost_breakdown_from_result(llm_result),
        )
        metrics["job_mode"] = "enhance_existing_abap"
        metrics["source_abap_characters"] = len(existing_abap)
        metrics["enhancement_specification_characters"] = len(enhancement_specification)
        (job_folder / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        update_progress(jobs_folder, job_id, "Complete", "ABAP enhancement complete.", stage="Complete")
    except Exception as exc:
        update_progress(jobs_folder, job_id, "Error", str(exc), stage="Error")
    finally:
        reset_current_model_settings(model_settings_token)


def render_enhance_prompt(prompt_template, existing_abap, enhancement_specification):
    return (
        str(prompt_template or "")
        .replace("{{EXISTING_ABAP}}", str(existing_abap or ""))
        .replace("{{FUNCTIONAL_SPECIFICATION}}", str(enhancement_specification or ""))
    )


def enhancement_source_context(existing_abap, enhancement_specification):
    return (
        "Functional specification for the required enhancement:\n"
        f"{enhancement_specification or ''}\n\n"
        "Existing ABAP program:\n"
        f"{existing_abap or ''}"
    )


def merge_validation_issues_for_enhancement(existing, additional):
    merged = list(existing or [])
    seen = {
        (
            item.get("rule_id"),
            item.get("line_number"),
            item.get("message"),
            item.get("source_line"),
        )
        for item in merged
        if isinstance(item, dict)
    }
    for item in additional or []:
        key = (
            item.get("rule_id"),
            item.get("line_number"),
            item.get("message"),
            item.get("source_line"),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def save_enhancement_diagnostics(job_folder, diagnostics):
    (Path(job_folder) / "enhancement_diagnostics.json").write_text(
        json.dumps(diagnostics or {}, indent=2),
        encoding="utf-8",
    )
