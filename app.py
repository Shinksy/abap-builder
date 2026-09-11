import json
import re
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from config import Config
from services.callable_signature_provider import get_configured_callable_signature_provider
from services.ddic_metadata_provider import get_configured_ddic_metadata_provider
from services.job_options import load_job_options, save_job_options
from services.jobs import (
    create_job_specification_path,
    delete_job,
    enhancement_specification_path,
    existing_input_path,
    inferred_job_mode,
    list_jobs,
    load_rerun_metadata,
    prepare_rerun_job,
)
from services.llm import generate_code_review_repair
from services.model_settings import model_options_from_form, model_settings_for_template, normalize_model_settings
from services.metadata_cache_upload import (
    MetadataCacheUploadError,
    METADATA_TYPE_LABELS,
    parse_metadata_upload_json,
    prepare_metadata_cache_upload,
    save_metadata_cache_upload,
)
from services.progress import create_job, get_progress, update_progress
from services.functional_spec_preparation import (
    accept_functional_spec_for_job,
    load_functional_spec_context,
    load_functional_spec_proposal,
    reject_functional_spec_for_job,
    start_prepare_functional_spec_job,
)
from services.sap_syntax_check import get_configured_sap_syntax_checker
from services.create_abap import (
    approve_processing_plan_for_job,
    load_abap_generation_chunks,
    load_abap_generation_diagnostics,
    load_approved_processing_plan,
    load_ddic_metadata,
    load_dependency_analysis,
    load_fix_summary,
    load_metrics,
    load_processing_plan_context,
    load_processing_plan_proposal,
    load_sap_syntax_check,
    load_validation_issues,
    record_post_generation_stage,
    reject_processing_plan_for_job,
    save_post_generation_diagnostics,
    start_create_abap_job,
)
from services.enhance_abap import (
    approve_enhancement_for_job,
    load_enhancement_generation_chunks,
    load_enhancement_generation_diagnostics,
    load_enhancement_proposal,
    reject_enhancement_for_job,
    start_enhance_abap_job,
)


def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    explicit_ddic_provider = config_overrides and "DDIC_METADATA_PROVIDER" in config_overrides
    explicit_signature_provider = config_overrides and "CALLABLE_SIGNATURE_PROVIDER" in config_overrides
    explicit_syntax_checker = config_overrides and "SAP_SYNTAX_CHECKER" in config_overrides
    explicit_code_review_repairer = config_overrides and "CODE_REVIEW_REPAIRER" in config_overrides
    if config_overrides:
        app.config.update(config_overrides)
    if not explicit_ddic_provider:
        app.config["DDIC_METADATA_PROVIDER"] = get_configured_ddic_metadata_provider(app.config)
    if not explicit_signature_provider:
        app.config["CALLABLE_SIGNATURE_PROVIDER"] = get_configured_callable_signature_provider(app.config)
    if not explicit_syntax_checker:
        app.config["SAP_SYNTAX_CHECKER"] = get_configured_sap_syntax_checker(app.config)
    if not explicit_code_review_repairer:
        app.config["CODE_REVIEW_REPAIRER"] = generate_code_review_repair
    log_ddic_metadata_startup(app)

    upload_folder = Path(app.config["UPLOAD_FOLDER"])
    jobs_folder = Path(app.config["JOBS_FOLDER"])
    upload_folder.mkdir(parents=True, exist_ok=True)
    jobs_folder.mkdir(parents=True, exist_ok=True)

    @app.context_processor
    def asset_context():
        return {"asset_version": static_asset_version("style.css")}

    def home_template_context(active_tab="new", **kwargs):
        rerun_context = kwargs.pop("rerun_context", None) or {}
        context = {
            "metadata_type_labels": METADATA_TYPE_LABELS,
            "active_tab": active_tab,
            "metadata_export_code": load_metadata_export_template(),
            "model_settings": model_settings_for_template(app.config),
            "rerun_context": rerun_context,
            "model_preset_default": rerun_context.get("model_preset") or model_settings_for_template(app.config)["default_preset"],
        }
        context.update(kwargs)
        return context

    @app.get("/")
    def home():
        rerun_context = (
            load_rerun_source_form_context(jobs_folder, upload_folder, request.args.get("rerun_source_job_id"))
            or load_rerun_form_context(jobs_folder, upload_folder, request.args.get("rerun_job_id"))
        )
        return render_template(
            "home.html",
            **home_template_context(
                active_tab=rerun_context.get("active_tab") or "new",
                rerun_context=rerun_context,
            ),
        )

    @app.get("/jobs")
    def jobs():
        return render_template("jobs.html", jobs=list_jobs(jobs_folder, upload_folder))

    @app.post("/jobs/<job_id>/delete")
    def delete_job_route(job_id):
        if not delete_job(jobs_folder, upload_folder, job_id):
            abort(404)
        return redirect(url_for("jobs"))

    @app.post("/jobs/<job_id>/rerun")
    def rerun_job_route(job_id):
        if not load_rerun_source_form_context(jobs_folder, upload_folder, job_id):
            abort(404)
        return redirect(url_for("home", rerun_source_job_id=job_id))

    @app.get("/jobs/<job_id>/rerun/enhancement")
    def enhancement_rerun_review(job_id):
        context = load_enhancement_rerun_context(jobs_folder, job_id)
        if not context:
            abort(404)
        return render_template(
            "enhancement_rerun_review.html",
            job_id=job_id,
            context=context,
            progress=get_progress(jobs_folder, job_id),
        )

    @app.post("/jobs/<job_id>/rerun/enhancement")
    def enhancement_rerun_action(job_id):
        context = load_enhancement_rerun_context(jobs_folder, job_id)
        if not context:
            abort(404)
        action = request.form.get("action")
        if action == "cancel":
            delete_job(jobs_folder, upload_folder, job_id)
            return redirect(url_for("jobs"))
        if action == "start":
            updated_specification = normalize_enhancement_specification_text(
                request.form.get("enhancement_specification", "")
            )
            if not updated_specification:
                return render_template(
                    "enhancement_rerun_review.html",
                    job_id=job_id,
                    context={
                        **context,
                        "enhancement_specification": normalize_enhancement_specification_text(
                            request.form.get("enhancement_specification", "")
                        ),
                    },
                    progress=get_progress(jobs_folder, job_id),
                    error="Enter the enhancement specification before re-running.",
                ), 400
            write_enhancement_specification(context["specification_path"], updated_specification)
            update_progress(
                jobs_folder,
                job_id,
                "Running",
                "Enhancement specification confirmed. Re-run is starting.",
                stage="Reading specification",
            )
            start_enhance_abap_job(
                job_id=job_id,
                source_path=Path(context["source_path"]),
                specification_path=Path(context["specification_path"]),
                jobs_folder=jobs_folder,
                prompt_path=Path(app.config["ENHANCE_ABAP_PROMPT"]),
                signature_provider=app.config.get("CALLABLE_SIGNATURE_PROVIDER"),
                ddic_metadata_provider=app.config.get("DDIC_METADATA_PROVIDER"),
                sap_syntax_checker=app.config.get("SAP_SYNTAX_CHECKER"),
                code_review_repairer=app.config.get("CODE_REVIEW_REPAIRER"),
            )
            return redirect(url_for("progress", job_id=job_id))
        abort(400)

    @app.post("/metadata-cache/upload")
    def upload_metadata_cache_file():
        metadata_type = request.form.get("metadata_type")
        try:
            metadata = parse_metadata_upload_json(
                uploaded_file=request.files.get("metadata_file"),
                payload_text=request.form.get("metadata_payload"),
            )
            prepared_upload = prepare_metadata_cache_upload(metadata_type, metadata, app.config)
        except MetadataCacheUploadError as exc:
            return render_template(
                "home.html",
                **home_template_context(active_tab="metadata", metadata_error=str(exc)),
            ), 400
        destination_path = Path(prepared_upload["destination_path"])
        prepared_upload["destination_display_path"] = display_path(destination_path)
        if destination_path.exists() and request.form.get("confirm_overwrite") != "1":
            return render_template(
                "home.html",
                **home_template_context(
                    active_tab="metadata",
                    metadata_upload_confirm=prepared_upload,
                    metadata_payload=json.dumps(prepared_upload["metadata"]),
                ),
            ), 409
        save_metadata_cache_upload(prepared_upload)
        return render_template(
            "home.html",
            **home_template_context(
                active_tab="metadata",
                metadata_success=(
                    f"Uploaded {prepared_upload['metadata_type_label']} metadata for "
                    f"{prepared_upload['object_name']} to {prepared_upload['destination_display_path']}."
                ),
            ),
        )

    @app.post("/upload")
    def upload_file():
        rerun_job_id = request.form.get("rerun_job_id", "").strip()
        rerun_source_job_id = request.form.get("rerun_source_job_id", "").strip()
        rerun_context = (
            load_rerun_source_form_context(jobs_folder, upload_folder, rerun_source_job_id)
            or load_rerun_form_context(jobs_folder, upload_folder, rerun_job_id)
        )
        uploaded_file = request.files.get("abap_file")
        job_title = request.form.get("job_title", "").strip()
        pasted_specification = request.form.get("specification_text", "").strip()
        has_uploaded_file = bool(uploaded_file and uploaded_file.filename)
        if not job_title:
            return render_template(
                "home.html",
                **home_template_context(active_tab="new", error="Enter a job title."),
            ), 400
        if not has_uploaded_file and not pasted_specification:
            return render_template(
                "home.html",
                **home_template_context(
                    active_tab="new",
                    rerun_context={**rerun_context, "job_title": job_title, "specification_text": pasted_specification},
                    error="Upload a specification file or paste the specification text.",
                ),
            ), 400

        job_id = rerun_job_id if rerun_context.get("rerun_job_id") and rerun_context.get("mode") == "create_abap" else create_job(jobs_folder)
        prepared_rerun = None
        if rerun_context.get("is_source_rerun") and rerun_context.get("mode") == "create_abap":
            prepared_rerun = prepare_rerun_job(jobs_folder, upload_folder, rerun_context["source_job_id"], job_id)
            if not prepared_rerun:
                delete_job(jobs_folder, upload_folder, job_id)
                abort(404)
        run_sap_syntax_check = request.form.get("run_sap_syntax_check") == "1"
        model_settings = normalize_model_settings(model_options_from_form(request.form), app.config)
        save_job_options(
            jobs_folder,
            job_id,
            {
                "job_title": job_title,
                "run_sap_syntax_check": run_sap_syntax_check,
                "sap_syntax_check_attempts": request.form.get("sap_syntax_check_attempts"),
                "final_assembly_mode": request.form.get("final_assembly_mode"),
                "prepare_functional_specification": request.form.get("prepare_functional_specification") == "1",
                "model_settings": model_settings,
                "rerun_of": rerun_context.get("source_job_id") if rerun_context else None,
                "rerun": bool(rerun_context),
            },
        )
        job_upload_folder = upload_folder / job_id
        job_upload_folder.mkdir(parents=True, exist_ok=True)
        if has_uploaded_file:
            filename = secure_filename(uploaded_file.filename)
            input_path = job_upload_folder / filename
            uploaded_file.save(input_path)
        else:
            input_path = (
                Path(prepared_rerun["input_path"])
                if prepared_rerun
                else Path(rerun_context["input_path"])
                if rerun_context.get("input_path")
                else job_upload_folder / "pasted_specification.txt"
            )
            input_path.write_text(pasted_specification, encoding="utf-8")

        if request.form.get("prepare_functional_specification") == "1":
            start_prepare_functional_spec_job(
                job_id=job_id,
                source_path=input_path,
                jobs_folder=jobs_folder,
                prompt_path=Path(app.config["PREPARE_FUNCTIONAL_SPEC_PROMPT"]),
                model_settings=model_settings,
            )
            return redirect(url_for("progress", job_id=job_id))

        start_create_abap_job(
            job_id=job_id,
            input_path=input_path,
            jobs_folder=jobs_folder,
            prompt_path=Path(app.config["CREATE_ABAP_PROMPT"]),
            signature_provider=app.config.get("CALLABLE_SIGNATURE_PROVIDER"),
            ddic_metadata_provider=app.config.get("DDIC_METADATA_PROVIDER"),
            sap_syntax_checker=app.config.get("SAP_SYNTAX_CHECKER"),
            code_review_repairer=app.config.get("CODE_REVIEW_REPAIRER"),
        )

        return redirect(url_for("progress", job_id=job_id))

    @app.post("/enhance")
    def enhance_file():
        rerun_job_id = request.form.get("rerun_job_id", "").strip()
        rerun_source_job_id = request.form.get("rerun_source_job_id", "").strip()
        rerun_context = (
            load_rerun_source_form_context(jobs_folder, upload_folder, rerun_source_job_id)
            or load_rerun_form_context(jobs_folder, upload_folder, rerun_job_id)
        )
        uploaded_file = request.files.get("existing_abap_file")
        job_title = request.form.get("job_title", "").strip()
        enhancement_specification = normalize_enhancement_specification_text(
            request.form.get("enhancement_specification", "")
        )
        if not job_title:
            return render_template(
                "home.html",
                **home_template_context(active_tab="enhance", error="Enter a job title."),
            ), 400
        has_uploaded_file = bool(uploaded_file and uploaded_file.filename)
        if not has_uploaded_file and rerun_context.get("mode") != "enhance_existing_abap":
            return render_template(
                "home.html",
                **home_template_context(active_tab="enhance", error="Select an existing ABAP program to enhance."),
            ), 400
        if not enhancement_specification:
            return render_template(
                "home.html",
                **home_template_context(active_tab="enhance", error="Enter the enhancement specification."),
            ), 400

        job_id = rerun_job_id if rerun_context.get("rerun_job_id") and rerun_context.get("mode") == "enhance_existing_abap" else create_job(jobs_folder)
        prepared_rerun = None
        if rerun_context.get("is_source_rerun") and rerun_context.get("mode") == "enhance_existing_abap":
            prepared_rerun = prepare_rerun_job(jobs_folder, upload_folder, rerun_context["source_job_id"], job_id)
            if not prepared_rerun:
                delete_job(jobs_folder, upload_folder, job_id)
                abort(404)
        run_sap_syntax_check = request.form.get("run_sap_syntax_check") == "1"
        model_settings = normalize_model_settings(model_options_from_form(request.form), app.config)
        save_job_options(
            jobs_folder,
            job_id,
            {
                "job_title": job_title,
                "run_sap_syntax_check": run_sap_syntax_check,
                "sap_syntax_check_attempts": request.form.get("sap_syntax_check_attempts"),
                "model_settings": model_settings,
                "rerun_of": rerun_context.get("source_job_id") if rerun_context else None,
                "rerun": bool(rerun_context),
            },
        )
        job_upload_folder = upload_folder / job_id
        job_upload_folder.mkdir(parents=True, exist_ok=True)
        if has_uploaded_file:
            source_path = job_upload_folder / secure_filename(uploaded_file.filename)
            uploaded_file.save(source_path)
        else:
            source_path = Path(prepared_rerun["source_path"]) if prepared_rerun else Path(rerun_context["source_path"])
        specification_path = (
            Path(prepared_rerun["specification_path"])
            if prepared_rerun
            else Path(rerun_context["specification_path"])
            if rerun_context.get("specification_path")
            else job_upload_folder / "enhancement_specification.txt"
        )
        write_enhancement_specification(specification_path, enhancement_specification)

        start_enhance_abap_job(
            job_id=job_id,
            source_path=source_path,
            specification_path=specification_path,
            jobs_folder=jobs_folder,
            prompt_path=Path(app.config["ENHANCE_ABAP_PROMPT"]),
            signature_provider=app.config.get("CALLABLE_SIGNATURE_PROVIDER"),
            ddic_metadata_provider=app.config.get("DDIC_METADATA_PROVIDER"),
            sap_syntax_checker=app.config.get("SAP_SYNTAX_CHECKER"),
            code_review_repairer=app.config.get("CODE_REVIEW_REPAIRER"),
        )

        return redirect(url_for("progress", job_id=job_id))

    @app.get("/progress/<job_id>")
    def progress(job_id):
        job_progress = get_progress(jobs_folder, job_id)
        job_progress["has_result"] = has_abap_result(jobs_folder, job_id)
        return render_template(
            "progress.html",
            job_id=job_id,
            progress=job_progress,
            review_url=review_url_for_job(jobs_folder, job_id),
            review_label=review_label_for_job(jobs_folder, job_id),
        )

    @app.get("/progress/<job_id>/status")
    def progress_status(job_id):
        progress_payload = get_progress(jobs_folder, job_id)
        progress_payload["has_result"] = has_abap_result(jobs_folder, job_id)
        if progress_payload.get("status") == "Awaiting Review":
            progress_payload["review_url"] = review_url_for_job(jobs_folder, job_id)
            progress_payload["review_label"] = review_label_for_job(jobs_folder, job_id)
            if (
                (jobs_folder / job_id / "functional_specification_proposal.json").exists()
                and not (jobs_folder / job_id / "accepted_functional_specification.txt").exists()
            ):
                progress_payload["functional_spec_review_url"] = url_for("functional_spec_review", job_id=job_id)
            elif not (jobs_folder / job_id / "enhancement_proposal.json").exists():
                progress_payload["processing_plan_review_url"] = url_for("processing_plan_review", job_id=job_id)
        return jsonify(progress_payload)

    @app.get("/functional-specification/<job_id>")
    def functional_spec_review(job_id):
        proposal = load_functional_spec_proposal(jobs_folder, job_id)
        if not proposal:
            abort(404)
        return render_template(
            "functional_spec_review.html",
            job_id=job_id,
            proposal=proposal,
            progress=get_progress(jobs_folder, job_id),
        )

    @app.post("/functional-specification/<job_id>")
    def functional_spec_action(job_id):
        action = request.form.get("action")
        if action == "reject":
            reject_functional_spec_for_job(jobs_folder, job_id)
            return redirect(url_for("progress", job_id=job_id))
        if action == "accept":
            proposal = load_functional_spec_proposal(jobs_folder, job_id)
            if not proposal:
                abort(404)
            accepted_path = accept_functional_spec_for_job(
                jobs_folder,
                job_id,
                request.form.get("prepared_specification"),
            )
            if not accepted_path:
                proposal = dict(proposal)
                return render_template(
                    "functional_spec_review.html",
                    job_id=job_id,
                    proposal=proposal,
                    progress=get_progress(jobs_folder, job_id),
                    error="Enter a prepared functional specification before accepting.",
                ), 400
            context = load_functional_spec_context(jobs_folder, job_id)
            update_progress(
                jobs_folder,
                job_id,
                "Running",
                "Functional specification accepted. ABAP generation is starting.",
                stage="functional_specification_accepted",
            )
            start_create_abap_job(
                job_id=job_id,
                input_path=accepted_path,
                jobs_folder=jobs_folder,
                prompt_path=Path(app.config["CREATE_ABAP_PROMPT"]),
                signature_provider=app.config.get("CALLABLE_SIGNATURE_PROVIDER"),
                ddic_metadata_provider=app.config.get("DDIC_METADATA_PROVIDER"),
                sap_syntax_checker=app.config.get("SAP_SYNTAX_CHECKER"),
                code_review_repairer=app.config.get("CODE_REVIEW_REPAIRER"),
                prior_section_durations=context.get("section_durations"),
                prior_usage=context.get("usage"),
            )
            return redirect(url_for("progress", job_id=job_id))
        abort(400)

    @app.get("/enhancement-review/<job_id>")
    def enhancement_review(job_id):
        proposal = load_enhancement_proposal(jobs_folder, job_id)
        if not proposal:
            abort(404)
        return render_template(
            "enhancement_review.html",
            job_id=job_id,
            proposal=proposal,
            progress=get_progress(jobs_folder, job_id),
        )

    @app.post("/enhancement-review/<job_id>")
    def enhancement_action(job_id):
        action = request.form.get("action")
        if action == "reject":
            reject_enhancement_for_job(jobs_folder, job_id)
            return redirect(url_for("progress", job_id=job_id))
        if action == "approve":
            proposal = load_enhancement_proposal(jobs_folder, job_id)
            approved = approve_enhancement_for_job(jobs_folder, job_id, proposal)
            if not approved:
                abort(400)
            job_folder = jobs_folder / job_id
            update_progress(
                jobs_folder,
                job_id,
                "Running",
                "Enhancement changes approved. Final processing is starting.",
                stage="generating_abap",
            )
            start_enhance_abap_job(
                job_id=job_id,
                source_path=job_folder / "original_existing.abap",
                specification_path=job_folder / "enhancement_specification.txt",
                jobs_folder=jobs_folder,
                prompt_path=Path(app.config["ENHANCE_ABAP_PROMPT"]),
                signature_provider=app.config.get("CALLABLE_SIGNATURE_PROVIDER"),
                ddic_metadata_provider=app.config.get("DDIC_METADATA_PROVIDER"),
                sap_syntax_checker=app.config.get("SAP_SYNTAX_CHECKER"),
                code_review_repairer=app.config.get("CODE_REVIEW_REPAIRER"),
                enhancement_review_required=False,
                approved_enhancement=approved,
            )
            return redirect(url_for("progress", job_id=job_id))
        abort(400)

    @app.get("/processing-plan/<job_id>")
    def processing_plan_review(job_id):
        proposal = load_processing_plan_proposal(jobs_folder, job_id)
        if not proposal:
            abort(404)
        return render_template(
            "processing_plan_review.html",
            job_id=job_id,
            proposal=proposal,
            progress=get_progress(jobs_folder, job_id),
        )

    @app.post("/processing-plan/<job_id>")
    def processing_plan_action(job_id):
        action = request.form.get("action")
        if action == "reject":
            reject_processing_plan_for_job(jobs_folder, job_id)
            return redirect(url_for("progress", job_id=job_id))
        if action == "reextract":
            context = load_processing_plan_context(jobs_folder, job_id)
            input_path = Path(context.get("input_path") or "")
            prompt_path = Path(context.get("prompt_path") or app.config["CREATE_ABAP_PROMPT"])
            start_create_abap_job(
                job_id=job_id,
                input_path=input_path,
                jobs_folder=jobs_folder,
                prompt_path=prompt_path,
                signature_provider=app.config.get("CALLABLE_SIGNATURE_PROVIDER"),
                ddic_metadata_provider=app.config.get("DDIC_METADATA_PROVIDER"),
                sap_syntax_checker=app.config.get("SAP_SYNTAX_CHECKER"),
                code_review_repairer=app.config.get("CODE_REVIEW_REPAIRER"),
                processing_plan_review_required=True,
            )
            return redirect(url_for("progress", job_id=job_id))
        if action in {"approve", "approve_edit"}:
            proposal = load_processing_plan_proposal(jobs_folder, job_id)
            if action == "approve_edit":
                try:
                    plan = json.loads(request.form.get("structured_json") or "")
                except json.JSONDecodeError:
                    proposal["validation_errors"] = ["Edited processing plan is not valid JSON."]
                    proposal["review_action_message"] = "Approval did not continue because the edited JSON is not valid."
                    proposal["review_action_failed"] = True
                    return render_template(
                        "processing_plan_review.html",
                        job_id=job_id,
                        proposal=proposal,
                        progress=get_progress(jobs_folder, job_id),
                    ), 400
            else:
                plan = proposal.get("plan")
            result = approve_processing_plan_for_job(
                jobs_folder,
                job_id,
                plan,
                allow_validation_errors=action == "approve",
            )
            if not result["approved"]:
                proposal = dict(result["proposal"])
                proposal["review_action_message"] = "Approval did not continue because deterministic validation still found errors."
                proposal["review_action_failed"] = True
                return render_template(
                    "processing_plan_review.html",
                    job_id=job_id,
                    proposal=proposal,
                    progress=get_progress(jobs_folder, job_id),
                ), 400
            context = load_processing_plan_context(jobs_folder, job_id)
            approved_plan = load_approved_processing_plan(jobs_folder, job_id)
            update_progress(
                jobs_folder,
                job_id,
                "Running",
                "Processing plan approved. ABAP generation is starting.",
                stage="processing_plan_approved",
            )
            start_create_abap_job(
                job_id=job_id,
                input_path=Path(context.get("input_path") or ""),
                jobs_folder=jobs_folder,
                prompt_path=Path(context.get("prompt_path") or app.config["CREATE_ABAP_PROMPT"]),
                callable_metadata=context.get("callable_metadata") or {},
                signature_provider=app.config.get("CALLABLE_SIGNATURE_PROVIDER"),
                ddic_metadata_provider=app.config.get("DDIC_METADATA_PROVIDER"),
                sap_syntax_checker=app.config.get("SAP_SYNTAX_CHECKER"),
                code_review_repairer=app.config.get("CODE_REVIEW_REPAIRER"),
                processing_plan_review_required=False,
                approved_processing_plan={"plan": approved_plan.get("plan")},
                prepared_declaration_requirements=context.get("declaration_requirements"),
                prior_section_durations=context.get("section_durations"),
                prior_usage=context.get("usage"),
            )
            return redirect(url_for("progress", job_id=job_id))
        abort(400)

    @app.get("/result/<job_id>")
    def result(job_id):
        if not is_abap_result_ready(jobs_folder, job_id):
            return redirect(url_for("progress", job_id=job_id))
        generated_result = load_final_abap_for_job(jobs_folder, job_id)
        if not generated_result["text"]:
            abort(404)
        generated_abap = generated_result["text"]
        record_result_page_source(jobs_folder / job_id, generated_abap)
        dependency_analysis = load_dependency_analysis(jobs_folder, job_id)
        ddic_metadata = load_ddic_metadata(jobs_folder, job_id)
        options = load_job_options(jobs_folder, job_id)
        metrics = load_metrics(jobs_folder, job_id)
        job_progress = get_progress(jobs_folder, job_id)
        if metrics.get("job_mode") == "enhance_existing_abap":
            abap_generation_diagnostics = load_enhancement_generation_diagnostics(jobs_folder, job_id)
            abap_generation_chunks = load_enhancement_generation_chunks(jobs_folder, job_id)
        else:
            abap_generation_diagnostics = load_abap_generation_diagnostics(jobs_folder, job_id)
            abap_generation_chunks = load_abap_generation_chunks(jobs_folder, job_id)
        return render_template(
            "result.html",
            job_id=job_id,
            generated_abap=generated_abap,
            generated_abap_source_label=generated_result["label"],
            job_progress=job_progress,
            metrics=metrics,
            fix_summary=load_fix_summary(jobs_folder, job_id),
            validation_issues=load_validation_issues(jobs_folder, job_id),
            dependency_analysis=dependency_analysis,
            ddic_metadata=ddic_metadata,
            sap_metadata_requests=sap_metadata_requests(dependency_analysis, ddic_metadata),
            sap_syntax_check=load_sap_syntax_check(jobs_folder, job_id, options),
            abap_generation_diagnostics=abap_generation_diagnostics,
            abap_generation_chunks=abap_generation_chunks,
        )

    @app.get("/download/<job_id>")
    def download(job_id):
        if not is_abap_result_ready(jobs_folder, job_id):
            return redirect(url_for("progress", job_id=job_id))
        generated_path = final_abap_path_for_job(jobs_folder, job_id)
        if generated_path.exists():
            return send_file(generated_path, as_attachment=True, download_name=generated_path.name)
        generated_result = load_final_abap_for_job(jobs_folder, job_id)
        if not generated_result["text"]:
            abort(404)
        return Response(
            generated_result["text"],
            mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename=generated.abap"},
        )

    return app


def final_abap_path_for_job(jobs_folder, job_id):
    job_folder = Path(jobs_folder) / job_id
    repaired_path = job_folder / "sap_syntax_repaired.abap"
    if repaired_path.exists():
        return repaired_path
    return job_folder / "generated.abap"


def load_final_abap_for_job(jobs_folder, job_id):
    job_folder = Path(jobs_folder) / job_id
    for filename, label in (
        ("sap_syntax_repaired.abap", "SAP syntax repaired ABAP"),
        ("generated.abap", "generated ABAP"),
        ("original_generated.abap", "original generated ABAP"),
    ):
        path = job_folder / filename
        if path.exists():
            return {"text": path.read_text(encoding="utf-8"), "label": label, "path": path}
    chunks_path = job_folder / "abap_generation_chunks.json"
    if chunks_path.exists():
        try:
            chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            chunks = {}
        assembled = str((chunks or {}).get("assembled_abap") or "").strip()
        if assembled:
            return {"text": assembled, "label": "assembled ABAP diagnostic", "path": chunks_path}
        final_assembly = (chunks or {}).get("final_assembly") or {}
        final_text = str(final_assembly.get("text") or "").strip() if isinstance(final_assembly, dict) else ""
        if final_text:
            return {"text": final_text, "label": "final assembly diagnostic", "path": chunks_path}
    return {"text": "", "label": "", "path": None}


def has_abap_result(jobs_folder, job_id):
    return is_abap_result_ready(jobs_folder, job_id)


def is_abap_result_ready(jobs_folder, job_id):
    if not load_final_abap_for_job(jobs_folder, job_id)["text"]:
        return False
    progress = get_progress(jobs_folder, job_id)
    return not bool(progress.get("is_active"))


def review_url_for_job(jobs_folder, job_id):
    job_folder = Path(jobs_folder) / job_id
    rerun_metadata = load_rerun_metadata(job_folder)
    if (
        (job_folder / "functional_specification_proposal.json").exists()
        and not (job_folder / "accepted_functional_specification.txt").exists()
    ):
        return url_for("functional_spec_review", job_id=job_id)
    if (job_folder / "enhancement_proposal.json").exists():
        return url_for("enhancement_review", job_id=job_id)
    if rerun_metadata.get("mode") == "enhance_existing_abap" and not (job_folder / "enhancement_proposal.json").exists():
        return url_for("home", rerun_job_id=job_id)
    if rerun_metadata.get("mode") == "create_abap" and not (job_folder / "processing_plan_proposal.json").exists():
        return url_for("home", rerun_job_id=job_id)
    return url_for("processing_plan_review", job_id=job_id)


def review_label_for_job(jobs_folder, job_id):
    job_folder = Path(jobs_folder) / job_id
    rerun_metadata = load_rerun_metadata(job_folder)
    if (
        (job_folder / "functional_specification_proposal.json").exists()
        and not (job_folder / "accepted_functional_specification.txt").exists()
    ):
        return "Review Structured Specification"
    if (job_folder / "enhancement_proposal.json").exists():
        return "Review proposed changes"
    if rerun_metadata.get("mode") == "enhance_existing_abap" and not (job_folder / "enhancement_proposal.json").exists():
        return "Review enhancement details"
    if rerun_metadata.get("mode") == "create_abap" and not (job_folder / "processing_plan_proposal.json").exists():
        return "Review generation details"
    return "Review processing plan"


def load_enhancement_rerun_context(jobs_folder, job_id):
    job_folder = Path(jobs_folder) / job_id
    metadata = load_rerun_metadata(job_folder)
    if metadata.get("mode") != "enhance_existing_abap":
        return {}
    source_path = Path(metadata.get("source_path") or "")
    specification_path = Path(metadata.get("specification_path") or "")
    if not source_path.exists() or not specification_path.exists():
        return {}
    options = load_job_options(jobs_folder, job_id)
    return {
        "source_job_id": metadata.get("source_job_id") or "",
        "source_job_short_id": metadata.get("source_job_short_id") or "",
        "source_path": str(source_path),
        "source_name": source_path.name,
        "source_text": source_path.read_text(encoding="utf-8"),
        "specification_path": str(specification_path),
        "enhancement_specification": normalize_enhancement_specification_text(
            specification_path.read_text(encoding="utf-8")
        ),
        "job_title": options.get("job_title") or "",
    }


def load_rerun_form_context(jobs_folder, upload_folder, job_id):
    job_id = str(job_id or "").strip()
    if not job_id:
        return {}
    job_folder = Path(jobs_folder) / job_id
    metadata = load_rerun_metadata(job_folder)
    mode = metadata.get("mode")
    if mode not in {"create_abap", "enhance_existing_abap"}:
        return {}
    options = load_job_options(jobs_folder, job_id)
    model_settings = options.get("model_settings") or {}
    context = {
        "job_id": job_id,
        "rerun_job_id": job_id,
        "mode": mode,
        "active_tab": "enhance" if mode == "enhance_existing_abap" else "new",
        "source_job_id": metadata.get("source_job_id") or "",
        "source_job_short_id": metadata.get("source_job_short_id") or "",
        "job_title": options.get("job_title") or "",
        "run_sap_syntax_check": bool(options.get("run_sap_syntax_check")),
        "sap_syntax_check_attempts": options.get("sap_syntax_check_attempts") or 2,
        "final_assembly_mode": options.get("final_assembly_mode") or "app",
        "prepare_functional_specification": bool(options.get("prepare_functional_specification")),
        "model_preset": model_settings.get("preset") or options.get("model_preset") or "",
    }
    if mode == "enhance_existing_abap":
        source_path = Path(metadata.get("source_path") or "")
        specification_path = Path(metadata.get("specification_path") or "")
        if not source_path.exists() or not specification_path.exists():
            return {}
        context.update(
            {
                "source_path": str(source_path),
                "source_name": source_path.name,
                "source_text": source_path.read_text(encoding="utf-8"),
                "specification_path": str(specification_path),
                "enhancement_specification": normalize_enhancement_specification_text(
                    specification_path.read_text(encoding="utf-8")
                ),
            }
        )
        return context
    input_path = Path(metadata.get("input_path") or "")
    if not input_path.exists():
        return {}
    display_path = input_path
    source_job_id = metadata.get("source_job_id")
    if source_job_id:
        source_original_path = create_job_specification_path(
            Path(jobs_folder) / source_job_id,
            Path(upload_folder) / source_job_id,
        )
        if source_original_path:
            display_path = Path(source_original_path)
    context.update(
        {
            "input_path": str(input_path),
            "input_name": display_path.name,
            "specification_text": display_path.read_text(encoding="utf-8"),
        }
    )
    return context


def load_rerun_source_form_context(jobs_folder, upload_folder, source_job_id):
    source_job_id = str(source_job_id or "").strip()
    if not source_job_id:
        return {}
    job_folder = Path(jobs_folder) / source_job_id
    upload_job_folder = Path(upload_folder) / source_job_id
    if not job_folder.exists() or not job_folder.is_dir():
        return {}
    mode = inferred_job_mode(job_folder, upload_job_folder)
    options = load_job_options(jobs_folder, source_job_id)
    model_settings = options.get("model_settings") or {}
    context = {
        "mode": mode,
        "is_source_rerun": True,
        "active_tab": "enhance" if mode == "enhance_existing_abap" else "new",
        "source_job_id": source_job_id,
        "source_job_short_id": source_job_id[:8],
        "job_title": options.get("job_title") or "",
        "run_sap_syntax_check": bool(options.get("run_sap_syntax_check")),
        "sap_syntax_check_attempts": options.get("sap_syntax_check_attempts") or 2,
        "final_assembly_mode": options.get("final_assembly_mode") or "app",
        "prepare_functional_specification": bool(options.get("prepare_functional_specification")),
        "model_preset": model_settings.get("preset") or options.get("model_preset") or "",
    }
    if mode == "enhance_existing_abap":
        source_path = existing_input_path(job_folder, upload_job_folder)
        specification_path = enhancement_specification_path(job_folder, upload_job_folder)
        if not source_path or not specification_path:
            return {}
        context.update(
            {
                "source_path": str(source_path),
                "source_name": Path(source_path).name,
                "source_text": Path(source_path).read_text(encoding="utf-8"),
                "specification_path": str(specification_path),
                "enhancement_specification": normalize_enhancement_specification_text(
                    Path(specification_path).read_text(encoding="utf-8")
                ),
            }
        )
        return context
    input_path = create_job_specification_path(job_folder, upload_job_folder)
    if not input_path:
        return {}
    context.update(
        {
            "input_path": str(input_path),
            "input_name": Path(input_path).name,
            "specification_text": Path(input_path).read_text(encoding="utf-8"),
        }
    )
    return context


def normalize_enhancement_specification_text(text):
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\n{3,}", "\n\n", normalized)


def write_enhancement_specification(path, text):
    Path(path).write_text(normalize_enhancement_specification_text(text), encoding="utf-8", newline="\n")


def record_result_page_source(job_folder, generated_abap):
    diagnostic_path = Path(job_folder) / "post_generation_processing.json"
    if diagnostic_path.exists():
        import json

        diagnostics = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    else:
        diagnostics = {"stages": []}
    diagnostics["stages"] = [
        stage
        for stage in diagnostics.get("stages", [])
        if stage.get("stage") != "complete_source_actually_displayed_on_result_page"
    ]
    record_post_generation_stage(
        diagnostics,
        "complete_source_actually_displayed_on_result_page",
        generated_abap,
    )
    save_post_generation_diagnostics(job_folder, diagnostics)


def display_path(path):
    try:
        return str(Path(path).resolve(strict=False).relative_to(Config.BASE_DIR))
    except ValueError:
        return str(path)


def load_metadata_export_template():
    path = Config.BASE_DIR / "templates" / "zabap_builder_metadata_export.abap"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def static_asset_version(filename):
    path = Config.BASE_DIR / "static" / filename
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def log_ddic_metadata_startup(app):
    provider = app.config.get("DDIC_METADATA_PROVIDER")
    print(f"DDIC metadata .env path loaded: {app.config.get('ENV_FILE_PATH')}")
    print(f"DDIC metadata enabled raw: {app.config.get('SAP_DDIC_METADATA_ENABLED_RAW')}")
    print(f"DDIC metadata enabled parsed: {app.config.get('SAP_DDIC_METADATA_ENABLED')}")
    print(f"DDIC metadata mode: {app.config.get('SAP_DDIC_METADATA_MODE')}")
    print(f"DDIC provider selected: {type(provider).__name__}")
    print(f"SAP API base URL configured: {bool(app.config.get('SAP_API_BASE_URL'))}")


def sap_metadata_requests(dependency_analysis, ddic_metadata):
    dependency_analysis = dependency_analysis or {}
    ddic_names = sorted(str(name).upper() for name in (ddic_metadata or {}).get("tables", {}) if name)
    callable_names = dependency_analysis.get("pre_generation_callable_identities")
    if callable_names is None:
        callable_names = dependency_analysis.get("callables", [])
    functions = []
    methods = []
    for name in callable_names or []:
        normalized = str(name or "").strip().upper()
        if not normalized:
            continue
        if "=>" in normalized:
            methods.append(normalized)
        else:
            functions.append(normalized)
    return {
        "ddic": ddic_names,
        "functions": sorted(set(functions)),
        "methods": sorted(set(methods)),
    }

app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
