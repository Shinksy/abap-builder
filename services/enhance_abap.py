import json
import re
import time
from difflib import SequenceMatcher, unified_diff
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
from services.abap_source import split_string_segments
from services.fixer import auto_fix_abap
from services.job_options import load_job_options
from services.llm import generate_code_review_repair, reset_current_model_settings, set_current_model_settings
from services.modifier_guardrails import build_identifier_provenance
from services.progress import update_progress
from services.sap_dependency_analysis import analyze_sap_dependencies, normalize_identifiers
from services.validator import parse_callable_invocations, validate_abap


ENHANCEMENT_PROPOSAL_ARTIFACT = "enhancement_proposal.json"
APPROVED_ENHANCEMENT_ARTIFACT = "approved_enhancement.json"
ENHANCEMENT_CHUNKS_ARTIFACT = "enhancement_chunks.json"

ENHANCEMENT_STOPWORDS = {
    "abap",
    "add",
    "added",
    "also",
    "and",
    "attached",
    "change",
    "code",
    "do",
    "does",
    "existing",
    "field",
    "fields",
    "for",
    "from",
    "logic",
    "new",
    "program",
    "read",
    "report",
    "requested",
    "routine",
    "routines",
    "source",
    "table",
    "the",
    "this",
    "to",
    "update",
    "using",
    "with",
}


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
    enhancement_review_required=True,
    approved_enhancement=None,
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
            "enhancement_review_required": enhancement_review_required,
            "approved_enhancement": approved_enhancement,
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
    enhancement_review_required=False,
    approved_enhancement=None,
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
        source_chunks = split_existing_program_chunks(existing_abap)
        affected_chunks = identify_affected_chunks(source_chunks, enhancement_specification)
        source_context = enhancement_source_context_from_chunks(affected_chunks, enhancement_specification)
        (job_folder / "original_existing.abap").write_text(existing_abap, encoding="utf-8")
        (job_folder / "enhancement_specification.txt").write_text(enhancement_specification, encoding="utf-8")
        save_enhancement_chunks(
            job_folder,
            {
                "chunks": chunk_manifest(source_chunks),
                "affected_chunks": chunk_manifest(affected_chunks),
            },
        )

        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Loading enhancement prompt...",
            stage="Loading prompt and templates",
        )
        prompt_template = Path(prompt_path).read_text(encoding="utf-8")
        prompt_text = render_enhance_prompt(prompt_template, source_context, enhancement_specification)

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
        prompt_template = append_ddic_catalogue(prompt_template, ddic_metadata)
        prompt_template = append_callable_catalogue(prompt_template, callable_metadata)
        prompt_text = render_enhance_prompt(prompt_template, source_context, enhancement_specification)

        llm_result = None
        pre_merge_validation_issues = []
        if approved_enhancement is None:
            update_progress(
                jobs_folder,
                job_id,
                "Running",
                "Enhancing affected ABAP chunks...",
                stage="generating_abap",
            )
            started_at = time.perf_counter()
            generator = enhancement_generator or generate_code_review_repair
            enhancement_result = generate_targeted_enhancement(
                original_source=existing_abap,
                chunks=source_chunks,
                affected_chunks=affected_chunks,
                prompt_template=prompt_template,
                enhancement_specification=enhancement_specification,
                generator=generator,
            )
            duration_seconds = time.perf_counter() - started_at
            add_section_duration(section_durations, "generated_abap", duration_seconds)

            llm_result = enhancement_result["llm_result"]
            model_name = enhancement_result["model"]
            usage = enhancement_result["usage"]
            response_text = enhancement_result["raw_response"]
            enhanced_abap = enhancement_result["text"]
            pre_merge_validation_issues = enhancement_result.get("validation_issues", [])
            record_post_generation_stage(post_generation_diagnostics, "original_existing_abap", existing_abap)
            record_post_generation_stage(post_generation_diagnostics, "raw_enhanced_abap_from_llm", enhanced_abap)
            save_enhancement_diagnostics(
                job_folder,
                {
                    "prompt": enhancement_result["prompt"],
                    "source_context": source_context,
                    "raw_response": response_text,
                    "chunks": enhancement_result["chunks"],
                    "model": model_name,
                    "usage": usage,
                    "model_settings": model_settings,
                },
            )
            save_enhancement_chunks(
                job_folder,
                {
                    "chunks": chunk_manifest(source_chunks),
                    "affected_chunks": chunk_manifest(affected_chunks),
                    "processed_chunks": enhancement_result["chunks"],
                },
            )
            if enhancement_review_required:
                save_enhancement_proposal(
                    job_folder,
                    enhancement_review_payload(
                        original_abap=existing_abap,
                        proposed_abap=enhanced_abap,
                        enhancement_specification=enhancement_specification,
                        model=model_name,
                        usage=usage,
                        llm_result=llm_result,
                        chunks=enhancement_result["chunks"],
                    ),
                )
                save_dependency_analysis(job_folder, dependency_analysis)
                save_ddic_metadata(job_folder, ddic_metadata)
                update_progress(
                    jobs_folder,
                    job_id,
                    "Awaiting Review",
                    "Review the proposed ABAP changes before results are generated.",
                    stage="awaiting_enhancement_review",
                )
                return
        else:
            enhanced_abap = str(approved_enhancement.get("proposed_abap") or "")
            model_name = approved_enhancement.get("model") or "approved-enhancement"
            usage = approved_enhancement.get("usage")
            llm_result = approved_enhancement.get("llm_result") or {"model": model_name, "usage": usage}
            pre_merge_validation_issues = approved_enhancement.get("validation_issues") or []
            duration_seconds = 0
            save_enhancement_chunks(
                job_folder,
                {
                    "chunks": chunk_manifest(source_chunks),
                    "affected_chunks": chunk_manifest(affected_chunks),
                    "processed_chunks": approved_enhancement.get("chunks") or [],
                },
            )
            record_post_generation_stage(post_generation_diagnostics, "original_existing_abap", existing_abap)
            record_post_generation_stage(post_generation_diagnostics, "approved_enhanced_abap", enhanced_abap)

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
        cleaned_final_abap = remove_redundant_new_wrapper_forms(existing_abap, final_abap)
        if cleaned_final_abap != final_abap:
            final_abap = cleaned_final_abap
            record_post_generation_stage(post_generation_diagnostics, "after_enhancement_structural_cleanup", final_abap)
        final_abap, declaration_cleanup_issues = remove_duplicate_enhancement_declarations(existing_abap, final_abap)
        if declaration_cleanup_issues:
            record_post_generation_stage(post_generation_diagnostics, "after_enhancement_declaration_dedupe", final_abap)
        repaired_final_abap = repair_orphan_enhancement_declarations(existing_abap, final_abap)
        if repaired_final_abap != final_abap:
            final_abap = repaired_final_abap
            record_post_generation_stage(post_generation_diagnostics, "after_enhancement_declaration_repair", final_abap)
        repaired_final_abap = repair_missing_select_target_structure_components(existing_abap, final_abap)
        if repaired_final_abap != final_abap:
            final_abap = repaired_final_abap
            record_post_generation_stage(post_generation_diagnostics, "after_enhancement_structure_component_repair", final_abap)

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
        validation_issues = merge_validation_issues_for_enhancement(validation_issues, pre_merge_validation_issues)
        validation_issues = merge_validation_issues_for_enhancement(validation_issues, declaration_cleanup_issues)
        provenance = build_identifier_provenance(
            original_source=existing_abap,
            functional_specification=enhancement_specification,
            sap_metadata=ddic_metadata,
        )
        validation_issues = merge_validation_issues_for_enhancement(
            validation_issues,
            validate_abap(final_abap, identifier_provenance=provenance),
        )
        validation_issues = merge_validation_issues_for_enhancement(
            validation_issues,
            validate_enhancement_structure(
                existing_abap,
                final_abap,
                callable_metadata,
                enhancement_specification,
            ),
        )
        add_section_duration(section_durations, "validation", time.monotonic() - validation_started_at)

        blocking_issues = enhancement_blocking_validation_issues(validation_issues)
        if blocking_issues:
            record_post_generation_stage(post_generation_diagnostics, "rejected_enhancement_source", final_abap)
            record_callable_signature_diagnostics(dependency_analysis, callable_metadata)
            save_dependency_analysis(job_folder, dependency_analysis)
            save_ddic_metadata(job_folder, ddic_metadata)
            save_fix_summary(job_folder, fix_result)
            save_validation_issues(job_folder, validation_issues)
            save_post_generation_diagnostics(job_folder, post_generation_diagnostics)
            first_issue = blocking_issues[0]
            raise RuntimeError(
                "Enhancement final validation failed: "
                f"{first_issue.get('rule_id')} - {first_issue.get('message')}"
            )

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


def split_existing_program_chunks(source):
    lines = str(source or "").splitlines()
    if not lines:
        return [program_chunk("global", "GLOBAL", 0, -1, "")]

    form_ranges = []
    index = 0
    while index < len(lines):
        form_match = re.match(r"^\s*FORM\s+([A-Za-z_]\w*)\b", lines[index], re.IGNORECASE)
        if not form_match:
            index += 1
            continue
        start = index
        end = index
        while end < len(lines):
            if re.match(r"^\s*ENDFORM\b", lines[end], re.IGNORECASE):
                break
            end += 1
        if end >= len(lines):
            end = len(lines) - 1
        form_ranges.append((start, end, form_match.group(1)))
        index = end + 1

    chunks = []
    first_form_start = form_ranges[0][0] if form_ranges else len(lines)
    if first_form_start > 0:
        chunks.append(program_chunk("global", "GLOBAL", 0, first_form_start - 1, "\n".join(lines[:first_form_start])))
    for start, end, name in form_ranges:
        chunks.append(program_chunk(f"form:{name.lower()}", "FORM", start, end, "\n".join(lines[start : end + 1]), name))
    if not chunks:
        chunks.append(program_chunk("program", "PROGRAM", 0, len(lines) - 1, "\n".join(lines)))
    return chunks


def program_chunk(chunk_id, chunk_type, start_line, end_line, text, name=None):
    return {
        "id": chunk_id,
        "type": chunk_type,
        "name": name or chunk_id,
        "start_line": start_line,
        "end_line": end_line,
        "text": text,
    }


def identify_affected_chunks(chunks, enhancement_specification):
    explicit_names = enhancement_explicit_identifiers(enhancement_specification)
    explicitly_affected = []
    for chunk in chunks:
        chunk_name = str(chunk.get("name") or "").lower()
        chunk_id_name = str(chunk.get("id") or "").split(":", 1)[-1].lower()
        if chunk_name in explicit_names or chunk_id_name in explicit_names:
            explicitly_affected.append(chunk)
    if explicitly_affected:
        if global_chunk_is_likely_affected(enhancement_keywords(enhancement_specification)):
            global_chunks = [chunk for chunk in chunks if chunk["type"] == "GLOBAL"]
            return global_chunks + [chunk for chunk in explicitly_affected if chunk["type"] != "GLOBAL"]
        return explicitly_affected

    keywords = enhancement_keywords(enhancement_specification)
    affected = []
    for chunk in chunks:
        haystack = search_tokens(chunk.get("name", "") + "\n" + chunk.get("text", ""))
        if chunk["type"] == "GLOBAL" and global_chunk_is_likely_affected(keywords):
            affected.append(chunk)
            continue
        if any(keyword in haystack for keyword in keywords):
            affected.append(chunk)
    if affected:
        return affected
    return chunks[:1]


def enhancement_keywords(text):
    raw_tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_~/-]*", str(text or "").lower())
    keywords = []
    for token in raw_tokens:
        for part in re.split(r"[_/~-]", token):
            part = part.strip("_")
            if len(part) >= 3 and part not in ENHANCEMENT_STOPWORDS and part not in keywords:
                keywords.append(part)
    return keywords


def enhancement_explicit_identifiers(text):
    identifiers = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(text or "").lower()):
        if "_" in token and token not in ENHANCEMENT_STOPWORDS:
            identifiers.add(token)
    return identifiers


def normalized_search_text(text):
    return re.sub(r"[^a-z0-9_]+", " ", str(text or "").lower())


def search_tokens(text):
    normalized = normalized_search_text(text)
    tokens = set(re.findall(r"[a-z0-9_]+", normalized))
    for token in list(tokens):
        tokens.update(part for part in token.split("_") if part)
    return tokens


def global_chunk_is_likely_affected(keywords):
    declaration_indicators = {
        "data",
        "declaration",
        "declarations",
        "type",
        "types",
        "structure",
        "structures",
        "internal",
        "select-options",
        "parameters",
        "alv",
    }
    return bool(set(keywords) & declaration_indicators)


def enhancement_source_context_from_chunks(chunks, enhancement_specification):
    return (
        "Functional specification for the required enhancement:\n"
        f"{enhancement_specification or ''}\n\n"
        "Affected existing ABAP chunks:\n"
        f"{format_chunks_for_context(chunks)}"
    )


def format_chunks_for_context(chunks):
    sections = []
    for chunk in chunks:
        sections.append(
            "\n".join(
                [
                    f"--- Chunk: {chunk['id']} ({chunk['type']}) lines {chunk['start_line'] + 1}-{chunk['end_line'] + 1} ---",
                    chunk.get("text", ""),
                    f"--- End Chunk: {chunk['id']} ---",
                ]
            )
        )
    return "\n\n".join(sections)


def generate_targeted_enhancement(original_source, chunks, affected_chunks, prompt_template, enhancement_specification, generator):
    replacements = {}
    chunk_results = []
    prompts = []
    raw_responses = []
    for chunk in affected_chunks:
        chunk_context = enhancement_chunk_source_context(chunk, enhancement_specification)
        chunk_prompt = render_enhance_prompt(prompt_template, chunk_context, enhancement_specification)
        prompts.append(chunk_prompt)
        llm_result = generator(chunk_prompt, chunk_context)
        response_text, model_name, usage = normalize_llm_result(llm_result)
        updated_text = extract_updated_chunk_text(clean_response(response_text), chunk)
        updated_text = preserve_existing_data_access(chunk.get("text", ""), updated_text)
        updated_text = preserve_authoritative_existing_lines(
            chunk.get("text", ""),
            updated_text,
            enhancement_specification,
        )
        replacements[chunk["id"]] = updated_text
        raw_responses.append(response_text)
        chunk_results.append(
            {
                "name": chunk["id"],
                "chunk": chunk["id"],
                "type": chunk["type"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "prompt": chunk_prompt,
                "source_context": chunk_context,
                "raw_response": response_text,
                "text": updated_text,
                "model": model_name,
                "usage": usage,
            }
        )
    replacements, reconciliation_issues = reconcile_enhancement_chunk_replacements(
        original_source,
        chunks,
        replacements,
        enhancement_specification,
    )
    for result in chunk_results:
        if result["chunk"] in replacements:
            result["text"] = replacements[result["chunk"]]
    merged = merge_enhanced_chunks(original_source, chunks, replacements)
    usage = aggregate_usage([result.get("usage") for result in chunk_results])
    model = next((result.get("model") for result in chunk_results if result.get("model")), None)
    return {
        "text": merged,
        "model": model,
        "usage": usage,
        "prompt": "\n\n".join(prompts),
        "raw_response": "\n\n".join(raw_responses),
        "chunks": chunk_results,
        "validation_issues": reconciliation_issues,
        "llm_result": {"chunks": chunk_results},
    }


def enhancement_chunk_source_context(chunk, enhancement_specification):
    return (
        "Functional specification for the required enhancement:\n"
        f"{enhancement_specification or ''}\n\n"
        "Existing ABAP chunk to update. Return this chunk only, not the whole program:\n"
        f"--- Chunk: {chunk['id']} ({chunk['type']}) lines {chunk['start_line'] + 1}-{chunk['end_line'] + 1} ---\n"
        f"{chunk.get('text', '')}\n"
        f"--- End Chunk: {chunk['id']} ---"
    )


def extract_updated_chunk_text(response_text, chunk):
    text = str(response_text or "")
    marker_pattern = re.compile(
        rf"---\s*Chunk:\s*{re.escape(chunk['id'])}\b[^\n]*---\s*\n(?P<body>.*?)\n---\s*End Chunk:\s*{re.escape(chunk['id'])}\s*---",
        re.IGNORECASE | re.DOTALL,
    )
    match = marker_pattern.search(text)
    if match:
        return match.group("body").strip("\n")
    return text


def preserve_existing_data_access(original_chunk_text, updated_chunk_text):
    original_selects = select_statements_by_table(original_chunk_text)
    if not original_selects:
        return updated_chunk_text

    updated_selects = select_statements(updated_chunk_text)
    selects_by_table = {}
    for statement in updated_selects:
        table = statement.get("table")
        if table:
            selects_by_table.setdefault(table, []).append(statement)

    lines = str(updated_chunk_text or "").splitlines()
    for table, original_for_table in original_selects.items():
        updated_for_table = selects_by_table.get(table, [])
        if len(updated_for_table) <= len(original_for_table):
            continue
        keeper = updated_for_table[0]
        duplicate_selects = updated_for_table[len(original_for_table) :]
        duplicate_fields = []
        for duplicate in duplicate_selects:
            duplicate_fields.extend(duplicate.get("fields", []))
        fields_to_add = fields_missing_from_select(keeper, duplicate_fields)
        for duplicate in sorted(duplicate_selects, key=lambda item: item["start"], reverse=True):
            del lines[duplicate["start"] : duplicate["end"] + 1]
        if fields_to_add:
            insert_fields_into_select(lines, keeper, fields_to_add)
    return "\n".join(lines)


def preserve_authoritative_existing_lines(original_chunk_text, updated_chunk_text, enhancement_specification):
    original_lines = str(original_chunk_text or "").splitlines()
    updated_lines = str(updated_chunk_text or "").splitlines()
    spec_terms = enhancement_spec_terms(enhancement_specification)
    matcher = SequenceMatcher(a=original_lines, b=updated_lines, autojunk=False)
    preserved = []

    for tag, original_start, original_end, updated_start, updated_end in matcher.get_opcodes():
        if tag == "equal":
            preserved.extend(updated_lines[updated_start:updated_end])
            continue
        if tag == "insert":
            preserved.extend(updated_lines[updated_start:updated_end])
            continue
        if tag == "delete":
            preserved.extend(original_lines[original_start:original_end])
            continue
        original_block = original_lines[original_start:original_end]
        updated_block = updated_lines[updated_start:updated_end]
        if len(original_block) == len(updated_block):
            for original_line, updated_line in zip(original_block, updated_block):
                if existing_line_change_is_enhancement_related(original_line, updated_line, spec_terms):
                    preserved.append(updated_line)
                else:
                    preserved.append(original_line)
            continue
        preserved.extend(preserve_replaced_block(original_block, updated_block, spec_terms))
    return "\n".join(preserved)


def preserve_replaced_block(original_block, updated_block, spec_terms):
    preserved = []
    matcher = SequenceMatcher(
        a=[normalize_structural_line(line) for line in original_block],
        b=[normalize_structural_line(line) for line in updated_block],
        autojunk=False,
    )
    for tag, original_start, original_end, updated_start, updated_end in matcher.get_opcodes():
        if tag == "equal":
            preserved.extend(updated_block[updated_start:updated_end])
        elif tag == "insert":
            preserved.extend(updated_block[updated_start:updated_end])
        elif tag == "delete":
            preserved.extend(original_block[original_start:original_end])
        else:
            original_subblock = original_block[original_start:original_end]
            updated_subblock = updated_block[updated_start:updated_end]
            if len(original_subblock) == len(updated_subblock):
                for original_line, updated_line in zip(original_subblock, updated_subblock):
                    if existing_line_change_is_enhancement_related(original_line, updated_line, spec_terms):
                        preserved.append(updated_line)
                    else:
                        preserved.append(original_line)
            else:
                preserved.extend(original_subblock)
                preserved.extend(updated_subblock)
    return preserved


def existing_line_change_is_enhancement_related(original_line, updated_line, spec_terms):
    if normalize_structural_line(original_line) == normalize_structural_line(updated_line):
        return True
    if not spec_terms:
        return False
    original_terms = line_terms(original_line)
    updated_terms = line_terms(updated_line)
    changed_terms = (updated_terms - original_terms) | (original_terms - updated_terms)
    return bool(changed_terms and ((original_terms | updated_terms) & spec_terms))


def enhancement_spec_terms(text):
    terms = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_~/-]*", str(text or "").lower()):
        for part in re.split(r"[_/~-]", token):
            part = part.strip("_")
            if len(part) >= 2 and part not in ENHANCEMENT_STOPWORDS:
                terms.add(part)
    return terms


def line_terms(line):
    terms = set()
    for segment, is_string in split_string_segments(strip_abap_comment(line)):
        text = segment if is_string else segment
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_~/-]*", text.lower()):
            for part in re.split(r"[_/~-]", token):
                part = part.strip("_")
                if len(part) >= 2:
                    terms.add(part)
    return terms


def reconcile_enhancement_chunk_replacements(original_source, chunks, replacements, enhancement_specification):
    ordered_chunks = [chunk for chunk in sorted(chunks, key=lambda item: item["start_line"]) if chunk["id"] in replacements]
    original_forms = collect_form_definitions(original_source)
    original_declarations = collect_enhancement_declarations(original_source)
    original_performs = collect_perform_references(original_source)
    reconciled = dict(replacements)
    owners = {
        "forms": {},
        "declarations": {},
        "selects": {},
        "performs": {},
    }
    issues = []

    for chunk in ordered_chunks:
        chunk_id = chunk["id"]
        text = reconciled.get(chunk_id, "")
        features = introduced_chunk_features(
            original_source=original_source,
            original_chunk_text=chunk.get("text", ""),
            updated_chunk_text=text,
            original_forms=original_forms,
            original_declarations=original_declarations,
            original_performs=original_performs,
        )
        remove_ranges = []

        for form in features["forms"]:
            owner = owners["forms"].get(form["name"])
            if owner is None:
                owners["forms"][form["name"]] = {"chunk": chunk_id, "fingerprint": form["fingerprint"]}
                continue
            if owner["fingerprint"] != form["fingerprint"]:
                issues.append(
                    structural_issue(
                        "ENHANCEMENT_CONTRADICTORY_IMPLEMENTATION",
                        form["line"],
                        f"Enhancement chunks generated conflicting FORM {form['name']} implementations.",
                        form["source_line"],
                        "Regenerate the enhancement so only one coherent implementation owns this routine.",
                        routine=form["name"],
                    )
                )
            remove_ranges.append((form["start"], form["end"]))

        for declaration in features["declarations"]:
            if declaration["name"] not in owners["declarations"]:
                owners["declarations"][declaration["name"]] = chunk_id
                continue
            remove_ranges.append((declaration["line"] - 1, declaration["line"] - 1))

        for statement in features["selects"]:
            key = statement["table"]
            if not key or multiple_database_reads_explicitly_requested(enhancement_specification, key):
                continue
            if key not in owners["selects"]:
                owners["selects"][key] = chunk_id
                continue
            remove_ranges.append((statement["start"], statement["end"]))

        for call in features["performs"]:
            key = call["name"]
            if key not in owners["performs"]:
                owners["performs"][key] = chunk_id
                continue
            remove_ranges.append((call["line"] - 1, call["line"] - 1))

        if remove_ranges:
            cleaned_text = remove_line_ranges(str(text or "").splitlines(), remove_ranges)
            if structurally_equivalent_source(cleaned_text, chunk.get("text", "")):
                cleaned_text = chunk.get("text", "")
            reconciled[chunk_id] = cleaned_text
    return reconciled, issues


def introduced_chunk_features(
    original_source,
    original_chunk_text,
    updated_chunk_text,
    original_forms=None,
    original_declarations=None,
    original_performs=None,
):
    original_forms = original_forms if original_forms is not None else collect_form_definitions(original_source)
    original_declarations = (
        original_declarations if original_declarations is not None else collect_enhancement_declarations(original_source)
    )
    original_performs = original_performs if original_performs is not None else collect_perform_references(original_source)

    original_chunk_selects = select_statements_by_table(original_chunk_text)
    updated_selects_by_table = select_statements_by_table(updated_chunk_text)
    new_selects = []
    for table, updated_selects in updated_selects_by_table.items():
        original_count = len(original_chunk_selects.get(table, []))
        if len(updated_selects) > original_count:
            new_selects.extend(updated_selects[original_count:])

    original_chunk_performs = original_perform_call_keys(collect_perform_references(original_chunk_text))
    global_original_performs = original_perform_call_keys(original_performs)
    new_performs = []
    for name, calls in collect_perform_references(updated_chunk_text).items():
        for call in calls:
            key = perform_call_key(name, call)
            if key not in original_chunk_performs and key not in global_original_performs:
                item = dict(call)
                item["name"] = name
                new_performs.append(item)

    return {
        "forms": introduced_form_definitions(updated_chunk_text, original_forms),
        "declarations": introduced_declarations(updated_chunk_text, original_declarations),
        "selects": new_selects,
        "performs": new_performs,
    }


def introduced_form_definitions(updated_chunk_text, original_forms):
    forms = []
    for name, entries in collect_form_definitions(updated_chunk_text).items():
        if name in original_forms:
            continue
        for entry in entries:
            item = dict(entry)
            item["fingerprint"] = form_definition_fingerprint(updated_chunk_text, entry)
            forms.append(item)
    return forms


def form_definition_fingerprint(source, form_entry):
    lines = str(source or "").splitlines()
    form_lines = lines[form_entry["start"] : form_entry["end"] + 1]
    return "\n".join(normalize_structural_line(line) for line in form_lines if normalize_structural_line(line))


def introduced_declarations(updated_chunk_text, original_declarations):
    declarations = []
    for name, entries in collect_enhancement_declarations(updated_chunk_text).items():
        if name in original_declarations:
            continue
        for entry in entries:
            item = dict(entry)
            item["name"] = name
            declarations.append(item)
    return declarations


def multiple_database_reads_explicitly_requested(enhancement_specification, table_name):
    text = str(enhancement_specification or "").lower()
    table = str(table_name or "").lower()
    if not re.search(r"\b(?:multiple|separate|several|different|twice|again)\s+\w*\s*(?:reads?|selects?|lookups?)\b", text):
        return False
    return not table or table in text or "same table" in text or "same data source" in text


def select_statements_by_table(source):
    grouped = {}
    for statement in select_statements(source):
        table = statement.get("table")
        if table:
            grouped.setdefault(table, []).append(statement)
    return grouped


def select_statements(source):
    lines = str(source or "").splitlines()
    statements = []
    index = 0
    while index < len(lines):
        if not re.match(r"^\s*SELECT\b", lines[index], re.IGNORECASE):
            index += 1
            continue
        start = index
        end = index
        while end < len(lines) and "." not in strip_abap_comment(lines[end]):
            end += 1
        if end >= len(lines):
            end = len(lines) - 1
        text = "\n".join(lines[start : end + 1])
        table = select_table_name(text)
        statements.append(
            {
                "start": start,
                "end": end,
                "text": text,
                "table": table,
                "fields": select_field_names(text),
            }
        )
        index = end + 1
    return statements


def strip_abap_comment(line):
    code = str(line or "")
    quote_index = code.find('"')
    if quote_index >= 0:
        return code[:quote_index]
    return code


def select_table_name(statement_text):
    match = re.search(r"\bFROM\s+([A-Za-z0-9_/]+)\b", str(statement_text or ""), re.IGNORECASE)
    return match.group(1).lower() if match else ""


def select_field_names(statement_text):
    text = str(statement_text or "")
    boundary_matches = [
        match
        for match in (
            re.search(r"\bFROM\b", text, re.IGNORECASE),
            re.search(r"\bINTO\b", text, re.IGNORECASE),
            re.search(r"\bAPPENDING\b", text, re.IGNORECASE),
        )
        if match
    ]
    if not boundary_matches:
        return []
    boundary = min(boundary_matches, key=lambda item: item.start())
    field_text = text[: boundary.start()]
    field_text = re.sub(r"^\s*SELECT\b", "", field_text, flags=re.IGNORECASE).strip()
    field_text = re.sub(r"^(?:SINGLE|DISTINCT)\b", "", field_text, flags=re.IGNORECASE).strip()
    fields = []
    for token in re.findall(r"[A-Za-z0-9_~/]+", field_text):
        keyword = token.lower()
        if keyword in {"as", "single", "distinct", "into", "table", "appending"}:
            continue
        fields.append(token)
    return fields


def select_statement_key(statement):
    return normalize_structural_line((statement or {}).get("text", ""))


def select_target_signature(statement):
    return (
        (statement or {}).get("table") or "",
        (select_target_table((statement or {}).get("text", "")) or "").lower(),
    )


def fields_missing_from_select(select_statement, candidate_fields):
    existing = {normalize_abap_identifier(field) for field in select_statement.get("fields", [])}
    missing = []
    for field in candidate_fields:
        normalized = normalize_abap_identifier(field)
        if not normalized or normalized in existing:
            continue
        existing.add(normalized)
        missing.append(field)
    return missing


def normalize_abap_identifier(identifier):
    return str(identifier or "").replace("~", "-").lower()


def insert_fields_into_select(lines, select_statement, fields):
    from_index = None
    for index in range(select_statement["start"], select_statement["end"] + 1):
        if re.search(r"\bFROM\b", strip_abap_comment(lines[index]), re.IGNORECASE):
            from_index = index
            break
    if from_index is None:
        return

    if from_index == select_statement["start"]:
        line = lines[from_index]
        from_match = re.search(r"\bFROM\b", line, re.IGNORECASE)
        if not from_match:
            return
        insertion = " ".join(fields)
        lines[from_index] = f"{line[:from_match.start()].rstrip()} {insertion} {line[from_match.start():].lstrip()}"
        return

    indent_source_index = from_index - 1
    indent_match = re.match(r"^(\s*)", lines[indent_source_index])
    indent = indent_match.group(1) if indent_match else ""
    lines[from_index:from_index] = [f"{indent}{field}" for field in fields]


def merge_enhanced_chunks(original_source, chunks, replacements):
    original_lines = str(original_source or "").splitlines()
    merged = []
    cursor = 0
    for chunk in sorted(chunks, key=lambda item: item["start_line"]):
        start = chunk["start_line"]
        end = chunk["end_line"]
        if start > cursor:
            merged.extend(original_lines[cursor:start])
        replacement = replacements.get(chunk["id"])
        if replacement is None:
            merged.extend(original_lines[start : end + 1])
        elif replacement:
            merged.extend(str(replacement).splitlines())
        cursor = end + 1
    merged.extend(original_lines[cursor:])
    return "\n".join(merged)


def remove_redundant_new_wrapper_forms(original_source, final_source):
    original_forms = collect_form_definitions(original_source)
    final_forms = collect_form_definitions(final_source)
    new_forms = {name: entries for name, entries in final_forms.items() if name not in original_forms}
    if not new_forms:
        return final_source

    lines = str(final_source or "").splitlines()
    remove_ranges = []
    for name, entries in new_forms.items():
        if len(entries) != 1:
            continue
        form = entries[0]
        body_lines = significant_form_body_lines(lines[form["start"] : form["end"] + 1])
        if not body_lines:
            continue
        outside_text = "\n".join(lines[: form["start"]] + lines[form["end"] + 1 :])
        if not all(line in outside_text for line in body_lines):
            continue
        for call in collect_perform_references(final_source).get(name, []):
            if call["line"] < form["start"] + 1 or call["line"] > form["end"] + 1:
                remove_ranges.append((call["line"] - 1, call["line"] - 1))
        remove_ranges.append((form["start"], form["end"]))

    if not remove_ranges:
        return final_source
    return remove_line_ranges(lines, remove_ranges)


def significant_form_body_lines(form_lines):
    body = []
    for line in form_lines[1:-1]:
        code = strip_abap_comment(line).strip()
        if code:
            body.append(line.strip())
    return body


def remove_line_ranges(lines, ranges):
    keep = list(lines)
    for start, end in sorted(ranges, reverse=True):
        del keep[start : end + 1]
    return "\n".join(keep)


def validate_enhancement_structure(original_source, final_source, callable_metadata=None, enhancement_specification=None):
    issues = []
    original_forms = collect_form_definitions(original_source)
    final_forms = collect_form_definitions(final_source)
    original_declarations = collect_enhancement_declarations(original_source)
    final_declarations = collect_enhancement_declarations(final_source)
    reference_declarations = merge_declaration_maps(final_declarations, collect_form_parameter_declarations(final_source))

    issues.extend(validate_generated_declarations_are_syntactic(original_source, final_source))
    issues.extend(validate_new_missing_references(original_source, final_source, original_declarations, reference_declarations))
    issues.extend(validate_new_orphan_performs(original_source, final_source, final_forms))
    issues.extend(validate_duplicate_enhancement_definitions(original_source, final_source, final_forms, final_declarations))
    issues.extend(validate_new_callables_available(original_source, final_source, callable_metadata))
    issues.extend(validate_referenced_structure_components(original_source, final_source, final_declarations))
    issues.extend(validate_generated_select_targets(original_source, final_source, final_declarations))
    issues.extend(validate_called_generated_routines_have_implementation(original_source, final_source, final_forms))
    issues.extend(validate_previously_populated_tables_not_cleared(original_source, final_source))
    issues.extend(validate_unrelated_existing_statement_changes(original_source, final_source, enhancement_specification))
    return issues


def validate_new_missing_references(original_source, final_source, original_declarations, final_declarations):
    known_names = set(final_declarations)
    known_names.update(collect_form_definitions(final_source))
    issues = []
    for line_number, line in newly_introduced_lines(original_source, final_source):
        code = strip_abap_comment(line)
        if is_declaration_line(code) or is_definition_line(code):
            continue
        for reference in likely_local_references(code):
            if reference in known_names:
                continue
            if reference in original_declarations:
                continue
            issues.append(
                structural_issue(
                    "ENHANCEMENT_MISSING_DEFINITION",
                    line_number,
                    f"New reference {reference} has no matching definition.",
                    line,
                    "Define the referenced identifier or reuse an existing defined identifier.",
                    identifier=reference,
                )
            )
    return issues


def validate_new_orphan_performs(original_source, final_source, final_forms):
    original_calls = collect_perform_references(original_source)
    issues = []
    for name, calls in collect_perform_references(final_source).items():
        if name in final_forms:
            continue
        for call in calls:
            if perform_call_key(name, call) in original_perform_call_keys(original_calls):
                continue
            issues.append(
                structural_issue(
                    "ENHANCEMENT_ORPHAN_ROUTINE_CALL",
                    call["line"],
                    f"New PERFORM {name} has no matching FORM definition.",
                    call["source_line"],
                    "Define the FORM or remove the orphan PERFORM.",
                    routine=name,
                )
            )
    return issues


def validate_duplicate_enhancement_definitions(original_source, final_source, final_forms, final_declarations):
    issues = []
    original_form_counts = {name: len(entries) for name, entries in collect_form_definitions(original_source).items()}
    for name, entries in final_forms.items():
        if len(entries) <= max(1, original_form_counts.get(name, 0)):
            continue
        for duplicate in entries[1:]:
            issues.append(
                structural_issue(
                    "ENHANCEMENT_DUPLICATE_DEFINITION",
                    duplicate["line"],
                    f"Duplicate FORM definition {name} introduced by enhancement.",
                    duplicate["source_line"],
                    "Reuse the existing FORM definition instead of adding a duplicate.",
                    definition=name,
                )
            )

    original_declaration_counts = declaration_counts(collect_enhancement_declarations(original_source))
    final_declaration_counts = declaration_counts(final_declarations)
    for name, count in final_declaration_counts.items():
        if count <= max(1, original_declaration_counts.get(name, 0)):
            continue
        for declaration in final_declarations[name][1:]:
            issues.append(
                structural_issue(
                    "ENHANCEMENT_DUPLICATE_DEFINITION",
                    declaration["line"],
                    f"Duplicate declaration {name} introduced by enhancement.",
                    declaration["source_line"],
                    "Reuse the existing definition instead of adding a duplicate.",
                    definition=name,
                )
            )
    return issues


def validate_generated_declarations_are_syntactic(original_source, final_source):
    original_lines = {normalize_structural_line(line) for line in str(original_source or "").splitlines()}
    issues = []
    chained_kind = None
    last_declaration_kind = None
    for line_number, line in enumerate(str(final_source or "").splitlines(), start=1):
        code = strip_abap_comment(line).strip()
        declarations = semantic_declarations_from_line(line, line_number, chained_kind)
        if declarations:
            last_declaration_kind = declarations[0]["kind"]
        elif normalize_structural_line(line) not in original_lines and declaration_continuation_like(code):
            issues.append(
                structural_issue(
                    "ENHANCEMENT_INVALID_DECLARATION_PLACEMENT",
                    line_number,
                    "Generated declaration-like line is not part of a valid ABAP declaration statement.",
                    line,
                    "Move this item into a valid DATA/TYPES/CONSTANTS/FIELD-SYMBOLS/PARAMETERS/SELECT-OPTIONS declaration or remove it.",
                )
            )
        started_kind = declaration_start_kind(code)
        if started_kind and ":" in code:
            chained_kind = started_kind if not code.rstrip().endswith(".") else None
        elif chained_kind and code.rstrip().endswith("."):
            chained_kind = None
        elif not code and not chained_kind:
            last_declaration_kind = None
    return issues


def validate_referenced_structure_components(original_source, final_source, final_declarations):
    structures = collect_structure_type_components(final_source)
    variable_types = collect_variable_type_references(final_declarations)
    issues = []
    for line_number, line in newly_introduced_lines(original_source, final_source):
        code = strip_abap_comment(line)
        for variable, component in re.findall(r"\b([A-Za-z_]\w*)-([A-Za-z_]\w*)\b", code):
            var_key = variable.lower()
            type_name = variable_types.get(var_key)
            if not type_name or type_name not in structures:
                continue
            if component.lower() in structures[type_name]:
                continue
            issues.append(
                structural_issue(
                    "ENHANCEMENT_UNKNOWN_STRUCTURE_COMPONENT",
                    line_number,
                    f"Generated reference {variable}-{component} is not defined in structure {type_name}.",
                    line,
                    "Use a component that exists in the declared structure or extend the structure definition.",
                    identifier=variable.lower(),
                    component=component.lower(),
                    structure=type_name,
                )
            )
    return issues


def validate_generated_select_targets(original_source, final_source, final_declarations):
    structures = collect_structure_type_components(final_source)
    table_row_types = collect_table_row_type_references(final_declarations)
    original_selects = select_statements(original_source)
    original_select_keys = {select_statement_key(statement) for statement in original_selects}
    original_fields_by_target = {}
    for statement in original_selects:
        key = select_target_signature(statement)
        original_fields_by_target.setdefault(key, set()).update(normalize_abap_identifier(field) for field in statement.get("fields", []))
    issues = []
    for statement in select_statements(final_source):
        statement_key = select_statement_key(statement)
        if statement_key in original_select_keys:
            continue
        target = select_target_table(statement.get("text", ""))
        row_type = table_row_types.get(target.lower()) if target else None
        if not row_type or row_type not in structures:
            continue
        components = structures[row_type]
        original_fields = original_fields_by_target.get(select_target_signature(statement), set())
        for field in statement.get("fields", []):
            if normalize_abap_identifier(field) in original_fields:
                continue
            field_name = field.split("~")[-1].split("-")[-1].lower()
            if field_name == "*" or field_name in components:
                continue
            issues.append(
                structural_issue(
                    "ENHANCEMENT_SELECT_TARGET_MISMATCH",
                    statement["start"] + 1,
                    f"Generated SELECT field {field} is not compatible with target row type {row_type}.",
                    statement["text"],
                    "Select only fields/components that exist in the target structure.",
                    field=field_name,
                    target=target.lower(),
                    structure=row_type,
                )
            )
    return issues


def validate_called_generated_routines_have_implementation(original_source, final_source, final_forms):
    original_forms = collect_form_definitions(original_source)
    original_calls = original_perform_call_keys(collect_perform_references(original_source))
    issues = []
    for name, calls in collect_perform_references(final_source).items():
        if name in original_forms:
            continue
        form_entries = final_forms.get(name, [])
        if not form_entries:
            continue
        form = form_entries[0]
        body = significant_form_body_lines(str(final_source or "").splitlines()[form["start"] : form["end"] + 1])
        has_implementation = any(not line.upper().startswith(("FORM ", "ENDFORM", "*")) for line in body)
        if has_implementation:
            continue
        for call in calls:
            if perform_call_key(name, call) in original_calls:
                continue
            issues.append(
                structural_issue(
                    "ENHANCEMENT_EMPTY_CALLED_ROUTINE",
                    call["line"],
                    f"Generated routine {name} is called but contains no implementation.",
                    call["source_line"],
                    "Implement the called routine or remove the generated PERFORM call.",
                    routine=name,
                )
            )
    return issues


def validate_previously_populated_tables_not_cleared(original_source, final_source):
    populated_tables = tables_populated_by_select(original_source)
    issues = []
    for line_number, line in newly_introduced_lines(original_source, final_source):
        code = strip_abap_comment(line).strip()
        match = re.match(r"^(?:CLEAR|REFRESH)\s+([A-Za-z_]\w*)(?:\[\])?\s*\.", code, re.IGNORECASE)
        if not match:
            continue
        table = match.group(1).lower()
        if table not in populated_tables:
            continue
        issues.append(
            structural_issue(
                "ENHANCEMENT_CLEARS_PREPOPULATED_TABLE",
                line_number,
                f"Generated code clears previously populated internal table {table}.",
                line,
                "Reuse the existing populated table rather than clearing and rebuilding it unless explicitly required.",
                table=table,
            )
        )
    return issues


def validate_unrelated_existing_statement_changes(original_source, final_source, enhancement_specification=None):
    issues = []
    spec_terms = enhancement_spec_terms(enhancement_specification)
    matcher = SequenceMatcher(
        a=str(original_source or "").splitlines(),
        b=str(final_source or "").splitlines(),
        autojunk=False,
    )
    for tag, original_start, original_end, final_start, final_end in matcher.get_opcodes():
        if tag == "equal" or tag == "insert":
            continue
        original_block = str(original_source or "").splitlines()[original_start:original_end]
        final_block = str(final_source or "").splitlines()[final_start:final_end]
        if all(not normalize_structural_line(line) for line in original_block + final_block):
            continue
        if statement_change_is_additive(original_block, final_block):
            continue
        if any(normalize_structural_line(line) in {normalize_structural_line(item) for item in final_block} for line in original_block):
            continue
        if changed_block_is_enhancement_related(original_block, final_block, spec_terms):
            continue
        issues.append(
            structural_issue(
                "ENHANCEMENT_UNRELATED_EXISTING_CHANGE",
                original_start + 1,
                "Generated enhancement modified an existing statement instead of preserving it.",
                "\n".join(original_block),
                "Preserve unrelated existing statements exactly and apply only additive required changes.",
            )
        )
    return issues


def changed_block_is_enhancement_related(original_block, final_block, spec_terms):
    if not spec_terms:
        return False
    for original_line, final_line in zip(original_block, final_block):
        if existing_line_change_is_enhancement_related(original_line, final_line, spec_terms):
            return True
    return False


def statement_change_is_additive(original_block, final_block):
    original_normalized = [normalize_structural_line(line) for line in original_block if normalize_structural_line(line)]
    final_normalized = [normalize_structural_line(line) for line in final_block if normalize_structural_line(line)]
    cursor = 0
    for line in final_normalized:
        if cursor < len(original_normalized) and line == original_normalized[cursor]:
            cursor += 1
    return cursor == len(original_normalized)


def collect_structure_type_components(source):
    structures = {}
    current_name = None
    current_components = set()
    for line in str(source or "").splitlines():
        code = strip_abap_comment(line).strip()
        begin_match = re.match(r"^TYPES\s*:?\s*BEGIN\s+OF\s+([A-Za-z_]\w*)\s*[,.]?\s*$", code, re.IGNORECASE)
        if begin_match:
            current_name = begin_match.group(1).lower()
            current_components = set()
            continue
        if not current_name:
            continue
        if re.match(rf"^END\s+OF\s+{re.escape(current_name)}\s*\.", code, re.IGNORECASE):
            structures[current_name] = current_components
            current_name = None
            current_components = set()
            continue
        component_match = re.match(r"^([A-Za-z_]\w*)\s+(?:TYPE|LIKE)\b", code, re.IGNORECASE)
        if component_match:
            current_components.add(component_match.group(1).lower())
    return structures


def collect_variable_type_references(declarations):
    references = {}
    for name, entries in (declarations or {}).items():
        for entry in entries:
            if entry.get("kind") != "DATA":
                continue
            match = re.search(r"\b(?:type|like)\s+([A-Za-z_]\w*)\b", entry.get("signature", ""), re.IGNORECASE)
            if match:
                references[name] = match.group(1).lower()
                break
    return references


def collect_table_row_type_references(declarations):
    references = {}
    for name, entries in (declarations or {}).items():
        for entry in entries:
            if entry.get("kind") != "DATA":
                continue
            match = re.search(
                r"\b(?:standard|sorted|hashed)?\s*table\s+of\s+([A-Za-z_]\w*)\b",
                entry.get("signature", ""),
                re.IGNORECASE,
            )
            if match:
                references[name] = match.group(1).lower()
                break
    return references


def select_target_table(statement_text):
    match = re.search(
        r"\b(?:INTO|APPENDING)\s+(?:CORRESPONDING\s+FIELDS\s+OF\s+)?TABLE\s+([A-Za-z_]\w*)\b",
        str(statement_text or ""),
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def tables_populated_by_select(source):
    tables = set()
    for statement in select_statements(source):
        target = select_target_table(statement.get("text", ""))
        if target:
            tables.add(target.lower())
    return tables


def enhancement_blocking_validation_issues(issues):
    return [
        issue
        for issue in issues or []
        if isinstance(issue, dict)
        and issue.get("severity", "error") == "error"
        and str(issue.get("rule_id") or "").startswith("ENHANCEMENT_")
    ]


def remove_duplicate_enhancement_declarations(original_source, final_source):
    original_declarations = collect_enhancement_declarations(original_source)
    final_declarations = collect_enhancement_declarations(final_source)
    original_line_counts = {}
    for entries in original_declarations.values():
        for entry in entries:
            key = (entry["name"], normalize_structural_line(entry["source_line"]))
            original_line_counts[key] = original_line_counts.get(key, 0) + 1
    consumed_original_lines = {}
    issues = []
    remove_lines = set()
    seen_new = {}

    for name, entries in final_declarations.items():
        original_entries = original_declarations.get(name, [])
        original_signatures = {entry["signature"] for entry in original_entries if entry.get("signature")}
        for entry in entries:
            normalized_line = normalize_structural_line(entry["source_line"])
            if original_entries:
                original_key = (name, normalized_line)
                consumed = consumed_original_lines.get(original_key, 0)
                if consumed < original_line_counts.get(original_key, 0):
                    consumed_original_lines[original_key] = consumed + 1
                    continue
                if original_signatures and entry.get("signature") not in original_signatures:
                    issues.append(conflicting_duplicate_declaration_issue(entry, name))
                remove_lines.add(entry["line"] - 1)
                continue

            prior = seen_new.get(name)
            if prior is None:
                seen_new[name] = entry
                continue
            if prior.get("signature") != entry.get("signature"):
                issues.append(conflicting_duplicate_declaration_issue(entry, name))
            remove_lines.add(entry["line"] - 1)

    if not remove_lines:
        return final_source, issues
    lines = str(final_source or "").splitlines()
    return "\n".join(line for index, line in enumerate(lines) if index not in remove_lines), issues


def repair_orphan_enhancement_declarations(original_source, final_source):
    original_lines = {normalize_structural_line(line) for line in str(original_source or "").splitlines()}
    lines = str(final_source or "").splitlines()
    repaired = list(lines)
    chained_kind = None
    for index, line in enumerate(lines):
        line_number = index + 1
        code = strip_abap_comment(line).strip()
        declarations = semantic_declarations_from_line(line, line_number, chained_kind)
        if (
            not declarations
            and normalize_structural_line(line) not in original_lines
            and declaration_continuation_like(code)
        ):
            repaired[index] = standalone_declaration_from_orphan_line(line)
            code = strip_abap_comment(repaired[index]).strip()
        started_kind = declaration_start_kind(code)
        if started_kind and ":" in code:
            chained_kind = started_kind if not code.rstrip().endswith(".") else None
        elif chained_kind and code.rstrip().endswith("."):
            chained_kind = None
    return "\n".join(repaired)


def standalone_declaration_from_orphan_line(line):
    indent_match = re.match(r"^(\s*)", str(line or ""))
    indent = indent_match.group(1) if indent_match else ""
    code = strip_abap_comment(line).strip().rstrip(".,")
    kind = "FIELD-SYMBOLS" if code.startswith("<") else "DATA"
    return f"{indent}{kind}: {code}."


def repair_missing_select_target_structure_components(original_source, final_source):
    additions = missing_select_target_structure_components(original_source, final_source)
    if not additions:
        return final_source
    lines = str(final_source or "").splitlines()
    insertions_by_line = {}
    for row_type, components in additions.items():
        end_index = structure_end_line_index(lines, row_type)
        if end_index is None:
            continue
        insertions_by_line.setdefault(end_index, []).extend(component for component in components if component)
    if not insertions_by_line:
        return final_source

    repaired = []
    for index, line in enumerate(lines):
        for component in insertions_by_line.get(index, []):
            repaired.append(component)
        repaired.append(line)
    return "\n".join(repaired)


def missing_select_target_structure_components(original_source, final_source):
    final_declarations = collect_enhancement_declarations(final_source)
    structures = collect_structure_type_components(final_source)
    table_row_types = collect_table_row_type_references(final_declarations)
    original_selects = select_statements(original_source)
    original_select_keys = {select_statement_key(statement) for statement in original_selects}
    original_fields_by_target = {}
    for statement in original_selects:
        key = select_target_signature(statement)
        original_fields_by_target.setdefault(key, set()).update(normalize_abap_identifier(field) for field in statement.get("fields", []))

    additions = {}
    for statement in select_statements(final_source):
        if select_statement_key(statement) in original_select_keys:
            continue
        target = select_target_table(statement.get("text", ""))
        row_type = table_row_types.get(target.lower()) if target else None
        if not row_type or row_type not in structures:
            continue
        original_fields = original_fields_by_target.get(select_target_signature(statement), set())
        for field in statement.get("fields", []):
            normalized_field = normalize_abap_identifier(field)
            field_name = field.split("~")[-1].split("-")[-1].lower()
            if normalized_field in original_fields or field_name == "*" or field_name in structures[row_type]:
                continue
            component_line = structure_component_line_from_select_field(statement, field)
            if component_line:
                additions.setdefault(row_type, [])
                if normalize_structural_line(component_line) not in {
                    normalize_structural_line(existing) for existing in additions[row_type]
                }:
                    additions[row_type].append(component_line)
                structures[row_type].add(field_name)
    return additions


def structure_component_line_from_select_field(statement, field):
    field_name = field.split("~")[-1].split("-")[-1].lower()
    if not field_name or field_name == "*":
        return ""
    type_ref = field.replace("~", "-")
    if "-" not in type_ref:
        source_table = (statement or {}).get("table") or ""
        if not source_table:
            return ""
        type_ref = f"{source_table}-{field_name}"
    return f"         {field_name:<18} TYPE {type_ref.lower()},"


def structure_end_line_index(lines, structure_name):
    for index, line in enumerate(lines):
        code = strip_abap_comment(line).strip()
        if re.match(rf"^END\s+OF\s+{re.escape(structure_name)}\s*\.", code, re.IGNORECASE):
            return index
    return None


def conflicting_duplicate_declaration_issue(entry, name):
    return structural_issue(
        "ENHANCEMENT_CONFLICTING_DUPLICATE_DECLARATION",
        entry["line"],
        f"Generated duplicate declaration {name} conflicts with an existing declaration type.",
        entry["source_line"],
        "Remove the generated duplicate declaration and preserve the original declaration unchanged.",
        definition=name,
    )


def validate_new_callables_available(original_source, final_source, callable_metadata=None):
    available = available_callable_names(callable_metadata)
    original = callable_call_keys(original_source)
    issues = []
    for call in parse_callable_invocations(final_source.splitlines()):
        key = (call.get("name") or "").lower()
        if not key or callable_invocation_key(call) in original:
            continue
        if key in available:
            continue
        issues.append(
            structural_issue(
                "ENHANCEMENT_UNAVAILABLE_CALLABLE",
                call.get("line_number") or 1,
                f"New callable reference {call.get('name')} is not defined or available.",
                call.get("source_line") or "",
                "Remove the callable reference or provide verified callable metadata for it.",
                callable_name=call.get("name"),
            )
        )
    return issues


def collect_form_definitions(source):
    forms = {}
    lines = str(source or "").splitlines()
    index = 0
    while index < len(lines):
        code = strip_abap_comment(lines[index]).strip()
        match = re.match(r"^FORM\s+([A-Za-z_]\w*)\b", code, re.IGNORECASE)
        if not match:
            index += 1
            continue
        start = index
        end = index
        while end < len(lines):
            if re.match(r"^ENDFORM\b", strip_abap_comment(lines[end]).strip(), re.IGNORECASE):
                break
            end += 1
        if end >= len(lines):
            end = len(lines) - 1
        name = match.group(1).lower()
        forms.setdefault(name, []).append({"name": name, "start": start, "end": end, "line": start + 1, "source_line": lines[start]})
        index = end + 1
    return forms


def collect_perform_references(source):
    calls = {}
    for line_number, line in enumerate(str(source or "").splitlines(), start=1):
        code = strip_abap_comment(line).strip()
        match = re.match(r"^PERFORM\s+([A-Za-z_]\w*)\b", code, re.IGNORECASE)
        if match:
            name = match.group(1).lower()
            calls.setdefault(name, []).append({"line": line_number, "source_line": line})
    return calls


def collect_enhancement_declarations(source):
    declarations = {}
    chained_kind = None
    last_declaration_kind = None
    structure_type_name = None
    for line_number, line in enumerate(str(source or "").splitlines(), start=1):
        code = strip_abap_comment(line).strip()
        if structure_type_name:
            if re.match(rf"^END\s+OF\s+{re.escape(structure_type_name)}\s*\.", code, re.IGNORECASE):
                structure_type_name = None
                chained_kind = None
            continue
        begin_match = re.match(r"^TYPES\s*:?\s*BEGIN\s+OF\s+([A-Za-z_]\w*)\s*[,.]?\s*$", code, re.IGNORECASE)
        if begin_match:
            structure_type_name = begin_match.group(1).lower()
            chained_kind = "TYPES" if not code.rstrip().endswith(".") else None
            continue
        declarations_on_line = semantic_declarations_from_line(line, line_number, chained_kind or last_declaration_kind)
        for declaration in declarations_on_line:
            declarations.setdefault(declaration["name"], []).append(declaration)
        if declarations_on_line:
            last_declaration_kind = declarations_on_line[0]["kind"]
        started_kind = declaration_start_kind(code)
        if started_kind and ":" in code:
            chained_kind = started_kind if not code.rstrip().endswith(".") else None
        elif chained_kind and code.rstrip().endswith("."):
            chained_kind = None
    return declarations


def merge_declaration_maps(*maps):
    merged = {}
    for mapping in maps:
        for name, entries in (mapping or {}).items():
            merged.setdefault(name, []).extend(entries)
    return merged


def collect_form_parameter_declarations(source):
    declarations = {}
    lines = str(source or "").splitlines()
    for form in [entry for entries in collect_form_definitions(source).values() for entry in entries]:
        header_lines = []
        for line_number in range(form["start"], form["end"] + 1):
            header_lines.append(lines[line_number])
            if "." in strip_abap_comment(lines[line_number]):
                break
        header_text = "\n".join(strip_abap_comment(line) for line in header_lines)
        for match in re.finditer(
            r"\b(?:USING|CHANGING|TABLES)\s+([A-Za-z_]\w*)\b(?:\s+(?:TYPE|LIKE|STRUCTURE)\s+([A-Za-z0-9_~/.-]+))?",
            header_text,
            re.IGNORECASE,
        ):
            name = match.group(1).lower()
            type_name = match.group(2) or ""
            line_offset = header_text[: match.start()].count("\n")
            source_line = header_lines[min(line_offset, len(header_lines) - 1)]
            signature = f"form-parameter {type_name.lower()}".strip()
            declarations.setdefault(name, []).append(
                {
                    "name": name,
                    "kind": "FORM-PARAMETER",
                    "signature": signature,
                    "line": form["start"] + line_offset + 1,
                    "source_line": source_line,
                }
            )
    return declarations


def semantic_declarations_from_line(line, line_number, chained_kind=None):
    code = strip_abap_comment(line).strip()
    if not code:
        return []
    match = re.match(
        r"^(?P<kind>DATA|TYPES|CONSTANTS|FIELD-SYMBOLS|PARAMETERS|SELECT-OPTIONS)\s*:?\s*(?P<body>.+?)\s*[,.]?\s*$",
        code,
        re.IGNORECASE,
    )
    if not match and chained_kind and declaration_continuation_like(code):
        kind = chained_kind
        body = code
    elif not match:
        return []
    else:
        kind = match.group("kind").upper()
        body = match.group("body").strip()
    declarations = []
    for item in declaration_items(kind, body):
        name = declaration_item_name(kind, item)
        if not name:
            continue
        declarations.append(
            {
                "name": name.lower(),
                "kind": kind,
                "signature": semantic_declaration_signature(kind, item),
                "line": line_number,
                "source_line": line,
            }
        )
    return declarations


def declaration_start_kind(code):
    match = re.match(r"^(DATA|TYPES|CONSTANTS|FIELD-SYMBOLS|PARAMETERS|SELECT-OPTIONS)\b", str(code or ""), re.IGNORECASE)
    return match.group(1).upper() if match else None


def declaration_continuation_like(code):
    return bool(re.match(r"^[<A-Za-z_]\w*>?\s+(?:TYPE|LIKE|AS|FOR|OCCURS|STRUCTURE)\b", str(code or ""), re.IGNORECASE))


def declaration_items(kind, body):
    if kind in {"PARAMETERS", "SELECT-OPTIONS"}:
        return [body]
    if "," not in body:
        return [body]
    return [item.strip() for item in body.split(",") if item.strip()]


def declaration_item_name(kind, item):
    text = str(item or "").strip()
    if kind == "FIELD-SYMBOLS":
        match = re.match(r"<?([A-Za-z_]\w*)>?\b", text, re.IGNORECASE)
    else:
        match = re.match(r"([A-Za-z_]\w*)\b", text, re.IGNORECASE)
    return match.group(1) if match else ""


def semantic_declaration_signature(kind, item):
    text = " ".join(str(item or "").strip().rstrip(".,").lower().split())
    name = declaration_item_name(kind, text)
    if name:
        text = re.sub(rf"^<?{re.escape(name.lower())}>?\b\s*", "", text, count=1)
    return f"{kind.lower()} {text}"


def declaration_counts(declarations):
    return {name: len(entries) for name, entries in declarations.items()}


def newly_introduced_lines(original_source, final_source):
    original_lines = {normalize_structural_line(line) for line in str(original_source or "").splitlines()}
    introduced = []
    for line_number, line in enumerate(str(final_source or "").splitlines(), start=1):
        if normalize_structural_line(line) not in original_lines:
            introduced.append((line_number, line))
    return introduced


def normalize_structural_line(line):
    return " ".join(strip_abap_comment(line).strip().lower().split())


def structurally_equivalent_source(left, right):
    return [
        normalize_structural_line(line)
        for line in str(left or "").splitlines()
        if normalize_structural_line(line)
    ] == [
        normalize_structural_line(line)
        for line in str(right or "").splitlines()
        if normalize_structural_line(line)
    ]


def is_declaration_line(code):
    return bool(re.match(r"^\s*(DATA|TYPES|CONSTANTS|FIELD-SYMBOLS|PARAMETERS|SELECT-OPTIONS|TABLES)\b", code, re.IGNORECASE))


def is_definition_line(code):
    return bool(
        re.match(
            r"^\s*(FORM|ENDFORM|CLASS|METHODS|METHOD|ENDMETHOD|ENDCLASS|USING|CHANGING|TABLES)\b",
            code,
            re.IGNORECASE,
        )
    )


def likely_local_references(code):
    cleaned = remove_string_literals(code)
    references = set()
    for token in re.findall(r"\b(?:[A-Za-z]+_)?[A-Za-z]\w*\b", cleaned):
        lower = token.lower()
        if lower in ABAP_REFERENCE_KEYWORDS:
            continue
        if is_likely_local_identifier(lower):
            references.add(lower)
    return references


def remove_string_literals(text):
    return re.sub(r"'(?:[^']|'')*'", "''", str(text or ""))


def is_likely_local_identifier(name):
    return bool(
        re.match(
            r"^(?:g[stv]?_|l[stv]?_|t_|w_|st_|s_|p_|r_|c_|v_|lv_|lt_|ls_|gt_|gs_|gv_|wa_|fs_)",
            name,
            re.IGNORECASE,
        )
    )


ABAP_REFERENCE_KEYWORDS = {
    "and",
    "append",
    "assigning",
    "at",
    "binary",
    "call",
    "case",
    "changing",
    "clear",
    "continue",
    "data",
    "else",
    "endif",
    "endloop",
    "eq",
    "exporting",
    "false",
    "field",
    "field-symbols",
    "for",
    "from",
    "function",
    "if",
    "importing",
    "in",
    "into",
    "is",
    "loop",
    "method",
    "modify",
    "move",
    "not",
    "or",
    "perform",
    "read",
    "select",
    "single",
    "sort",
    "table",
    "tables",
    "then",
    "to",
    "transporting",
    "true",
    "type",
    "using",
    "where",
    "with",
    "write",
}


def perform_call_key(name, call):
    return name, normalize_structural_line(call.get("source_line", ""))


def original_perform_call_keys(calls):
    return {perform_call_key(name, call) for name, entries in calls.items() for call in entries}


def callable_call_keys(source):
    return {callable_invocation_key(call) for call in parse_callable_invocations(str(source or "").splitlines())}


def callable_invocation_key(call):
    return (str(call.get("name") or "").lower(), normalize_structural_line(call.get("source_line") or ""))


def available_callable_names(callable_metadata):
    names = set()
    if not isinstance(callable_metadata, dict):
        return names
    for key in ("callable_signatures", "callables", "technical_mapping", "technical_mappings"):
        value = callable_metadata.get(key)
        if isinstance(value, dict):
            names.update(str(name).lower() for name in value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    name = item.get("callable") or item.get("name")
                    if name:
                        names.add(str(name).lower())
    return names


def structural_issue(rule_id, line_number, message, source_line, suggested_fix, **extra):
    item = {
        "rule_id": rule_id,
        "severity": "error",
        "line_number": line_number,
        "message": message,
        "source_line": source_line,
        "suggested_fix": suggested_fix,
    }
    item.update(extra)
    return item


def aggregate_usage(usages):
    totals = {}
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
    return totals or None


def chunk_manifest(chunks):
    return [
        {
            "id": chunk["id"],
            "type": chunk["type"],
            "name": chunk["name"],
            "start_line": chunk["start_line"],
            "end_line": chunk["end_line"],
        }
        for chunk in chunks
    ]


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


def enhancement_review_payload(
    original_abap,
    proposed_abap,
    enhancement_specification,
    model=None,
    usage=None,
    llm_result=None,
    chunks=None,
):
    return {
        "summary": enhancement_diff_summary(original_abap, proposed_abap),
        "diff": enhancement_unified_diff(original_abap, proposed_abap),
        "original_abap": original_abap or "",
        "proposed_abap": proposed_abap or "",
        "enhancement_specification": enhancement_specification or "",
        "model": model,
        "usage": usage,
        "llm_result": llm_result,
        "chunks": chunks or [],
    }


def enhancement_diff_summary(original_abap, proposed_abap):
    original_lines = str(original_abap or "").splitlines()
    proposed_lines = str(proposed_abap or "").splitlines()
    diff_lines = list(unified_diff(original_lines, proposed_lines, lineterm=""))
    added = len([line for line in diff_lines if line.startswith("+") and not line.startswith("+++")])
    removed = len([line for line in diff_lines if line.startswith("-") and not line.startswith("---")])
    return f"{added} added line(s), {removed} removed line(s)."


def enhancement_unified_diff(original_abap, proposed_abap):
    diff_lines = unified_diff(
        str(original_abap or "").splitlines(),
        str(proposed_abap or "").splitlines(),
        fromfile="original_existing.abap",
        tofile="proposed_enhancement.abap",
        lineterm="",
    )
    return "\n".join(diff_lines)


def save_enhancement_proposal(job_folder, proposal):
    (Path(job_folder) / ENHANCEMENT_PROPOSAL_ARTIFACT).write_text(
        json.dumps(proposal or {}, indent=2),
        encoding="utf-8",
    )


def save_enhancement_chunks(job_folder, diagnostics):
    (Path(job_folder) / ENHANCEMENT_CHUNKS_ARTIFACT).write_text(
        json.dumps(diagnostics or {}, indent=2),
        encoding="utf-8",
    )


def load_enhancement_generation_diagnostics(jobs_folder, job_id):
    path = Path(jobs_folder) / job_id / ENHANCEMENT_CHUNKS_ARTIFACT
    diagnostics_path = Path(jobs_folder) / job_id / "enhancement_diagnostics.json"
    diagnostics = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        diagnostics.update(payload)
    try:
        payload = json.loads(diagnostics_path.read_text(encoding="utf-8")) if diagnostics_path.exists() else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key not in diagnostics or key == "chunks":
                diagnostics[key] = value
    chunks = diagnostics.get("processed_chunks")
    if chunks is None:
        chunks = diagnostics.get("chunks")
    diagnostics["chunks"] = chunks if isinstance(chunks, list) else []
    return diagnostics


def load_enhancement_generation_chunks(jobs_folder, job_id):
    diagnostics = load_enhancement_generation_diagnostics(jobs_folder, job_id)
    chunks = diagnostics.get("chunks", []) if isinstance(diagnostics, dict) else []
    return chunks if isinstance(chunks, list) else []


def load_enhancement_proposal(jobs_folder, job_id):
    path = Path(jobs_folder) / job_id / ENHANCEMENT_PROPOSAL_ARTIFACT
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def approve_enhancement_for_job(jobs_folder, job_id, proposal=None):
    job_folder = Path(jobs_folder) / job_id
    approved = dict(proposal or load_enhancement_proposal(jobs_folder, job_id))
    if not approved.get("proposed_abap"):
        return {}
    approved["approved"] = True
    (job_folder / APPROVED_ENHANCEMENT_ARTIFACT).write_text(
        json.dumps(approved, indent=2),
        encoding="utf-8",
    )
    return approved


def reject_enhancement_for_job(jobs_folder, job_id):
    update_progress(
        jobs_folder,
        job_id,
        "Rejected",
        "Enhancement changes rejected.",
        stage="awaiting_enhancement_review",
    )


def save_enhancement_diagnostics(job_folder, diagnostics):
    (Path(job_folder) / "enhancement_diagnostics.json").write_text(
        json.dumps(diagnostics or {}, indent=2),
        encoding="utf-8",
    )
