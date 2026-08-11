import json
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from config import Config
from services.callable_signature_provider import get_configured_callable_signature_provider
from services.ddic_metadata_provider import get_configured_ddic_metadata_provider
from services.job_options import load_job_options, save_job_options
from services.jobs import delete_job, list_jobs
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
        context = {
            "metadata_type_labels": METADATA_TYPE_LABELS,
            "active_tab": active_tab,
            "metadata_export_code": load_metadata_export_template(),
            "model_settings": model_settings_for_template(app.config),
        }
        context.update(kwargs)
        return context

    @app.get("/")
    def home():
        return render_template("home.html", **home_template_context())

    @app.get("/jobs")
    def jobs():
        return render_template("jobs.html", jobs=list_jobs(jobs_folder, upload_folder))

    @app.post("/jobs/<job_id>/delete")
    def delete_job_route(job_id):
        if not delete_job(jobs_folder, upload_folder, job_id):
            abort(404)
        return redirect(url_for("jobs"))

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
        uploaded_file = request.files.get("abap_file")
        if not uploaded_file or not uploaded_file.filename:
            return render_template(
                "home.html",
                **home_template_context(active_tab="new", error="Select an ABAP file to upload."),
            ), 400

        job_id = create_job(jobs_folder)
        run_sap_syntax_check = request.form.get("run_sap_syntax_check") == "1"
        model_settings = normalize_model_settings(model_options_from_form(request.form), app.config)
        save_job_options(
            jobs_folder,
            job_id,
            {
                "run_sap_syntax_check": run_sap_syntax_check,
                "sap_syntax_check_attempts": request.form.get("sap_syntax_check_attempts"),
                "model_settings": model_settings,
            },
        )
        filename = secure_filename(uploaded_file.filename)
        job_upload_folder = upload_folder / job_id
        job_upload_folder.mkdir(parents=True, exist_ok=True)
        input_path = job_upload_folder / filename
        uploaded_file.save(input_path)

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
        uploaded_file = request.files.get("existing_abap_file")
        enhancement_specification = request.form.get("enhancement_specification", "").strip()
        if not uploaded_file or not uploaded_file.filename:
            return render_template(
                "home.html",
                **home_template_context(active_tab="enhance", error="Select an existing ABAP program to enhance."),
            ), 400
        if not enhancement_specification:
            return render_template(
                "home.html",
                **home_template_context(active_tab="enhance", error="Enter the enhancement specification."),
            ), 400

        job_id = create_job(jobs_folder)
        run_sap_syntax_check = request.form.get("run_sap_syntax_check") == "1"
        model_settings = normalize_model_settings(model_options_from_form(request.form), app.config)
        save_job_options(
            jobs_folder,
            job_id,
            {
                "run_sap_syntax_check": run_sap_syntax_check,
                "sap_syntax_check_attempts": request.form.get("sap_syntax_check_attempts"),
                "model_settings": model_settings,
            },
        )
        job_upload_folder = upload_folder / job_id
        job_upload_folder.mkdir(parents=True, exist_ok=True)
        source_path = job_upload_folder / secure_filename(uploaded_file.filename)
        specification_path = job_upload_folder / "enhancement_specification.txt"
        uploaded_file.save(source_path)
        specification_path.write_text(enhancement_specification, encoding="utf-8")

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
        if progress_payload.get("status") == "Awaiting Review":
            progress_payload["review_url"] = review_url_for_job(jobs_folder, job_id)
            progress_payload["review_label"] = review_label_for_job(jobs_folder, job_id)
            if not (jobs_folder / job_id / "enhancement_proposal.json").exists():
                progress_payload["processing_plan_review_url"] = url_for("processing_plan_review", job_id=job_id)
        return jsonify(progress_payload)

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
        generated_path = jobs_folder / job_id / "generated.abap"
        if not generated_path.exists():
            abort(404)
        generated_abap = generated_path.read_text(encoding="utf-8")
        record_result_page_source(jobs_folder / job_id, generated_abap)
        dependency_analysis = load_dependency_analysis(jobs_folder, job_id)
        ddic_metadata = load_ddic_metadata(jobs_folder, job_id)
        options = load_job_options(jobs_folder, job_id)
        metrics = load_metrics(jobs_folder, job_id)
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
        generated_path = jobs_folder / job_id / "generated.abap"
        if not generated_path.exists():
            abort(404)
        return send_file(generated_path, as_attachment=True, download_name="generated.abap")

    return app


def review_url_for_job(jobs_folder, job_id):
    job_folder = Path(jobs_folder) / job_id
    if (job_folder / "enhancement_proposal.json").exists():
        return url_for("enhancement_review", job_id=job_id)
    return url_for("processing_plan_review", job_id=job_id)


def review_label_for_job(jobs_folder, job_id):
    job_folder = Path(jobs_folder) / job_id
    if (job_folder / "enhancement_proposal.json").exists():
        return "Review proposed changes"
    return "Review processing plan"


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
    app.run(debug=True)
