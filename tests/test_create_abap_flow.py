from io import BytesIO
import json
from pathlib import Path
import shutil
from threading import Event
import time as real_time
import unittest
from uuid import uuid4
from unittest.mock import patch

from app import create_app
from config import Config
from services.create_abap import (
    APPROVED_PROCESSING_PLAN_ARTIFACT,
    DATABASE_READ_PATTERNS_PATH,
    PROCESSING_PLAN_CONTEXT_ARTIFACT,
    PROCESSING_PLAN_DIAGNOSTICS_ARTIFACT,
    PROCESSING_PLAN_LLM_SOURCE_ARTIFACT,
    PROCESSING_PLAN_PROPOSAL_ARTIFACT,
    REPORT_SKELETON_PATH,
    active_processing_duration,
    approve_processing_plan_for_job,
    append_generation_contract,
    build_generation_contract,
    calculate_cost,
    cost_breakdown_from_job_artifacts,
    extract_specification_callable_identities,
    enrich_processing_rule_callable_metadata,
    explicit_object_method_identities,
    extract_processing_plan_for_review,
    generate_abap_with_orchestrator,
    load_metrics,
    load_processing_plan_proposal,
    load_database_read_patterns,
    load_report_skeleton,
    merge_processing_rule_ddic_dependencies,
    merge_specification_callable_dependencies,
    normalize_metrics_for_display,
    processing_plan_review_payload,
    render_create_prompt,
    run_create_abap,
    usage_for_final_metrics,
)
from services.functional_spec_preparation import (
    ACCEPTED_FUNCTIONAL_SPEC_ARTIFACT,
    FUNCTIONAL_SPEC_PROPOSAL_ARTIFACT,
)
from services.callable_signature_provider import (
    NoOpCallableSignatureProvider,
    normalize_provider_signatures,
    resolve_callable_metadata,
)
from services.ddic_metadata_provider import (
    DdicMetadataError,
    LocalDdicMetadataCache,
    ModeAwareDdicMetadataProvider,
)
from services.progress import create_job, get_progress, update_progress


class CreateAbapFlowTest(unittest.TestCase):
    def setUp(self):
        self._old_dependency_analysis_enabled = Config.SAP_DEPENDENCY_ANALYSIS_ENABLED
        Config.SAP_DEPENDENCY_ANALYSIS_ENABLED = False

    def tearDown(self):
        Config.SAP_DEPENDENCY_ANALYSIS_ENABLED = self._old_dependency_analysis_enabled

    def run_pipeline_with_declaration_block(self, declaration_block):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a test report.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            chunk_outputs = {
                "declarations": declaration_block,
                "database_read_forms": "FORM read_data.\nENDFORM.",
                "processing_form": "FORM process_data.\nENDFORM.",
                "output_forms": "FORM output_data.\nENDFORM.",
                "main_program_flow": "START-OF-SELECTION.",
            }

            def generator(prompt_text, _source_text):
                if "Extract declaration requirements" in prompt_text:
                    return {"text": json.dumps({"report_name": "ztest"}), "model": "test-model", "usage": None}
                if "Extract business-processing logic" in prompt_text:
                    return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}
                for chunk_name, text in chunk_outputs.items():
                    if f"- Chunk: {chunk_name}" in prompt_text:
                        return {"text": text, "model": "test-model", "usage": None}
                return {"text": "", "model": "test-model", "usage": None}

            with patch("services.create_abap.generate_abap", side_effect=generator):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path)

            return (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8")
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def write_job_fixture(
        self,
        jobs_folder,
        uploads_folder,
        job_id,
        status,
        stage,
        started_at,
        completed_at,
        upload_name,
        generated=False,
        metrics=None,
        options=None,
    ):
        job_folder = Path(jobs_folder) / job_id
        upload_folder = Path(uploads_folder) / job_id
        job_folder.mkdir(parents=True, exist_ok=True)
        upload_folder.mkdir(parents=True, exist_ok=True)
        (upload_folder / upload_name).write_text("Create a test report.", encoding="utf-8")
        status_payload = {
            "status": status,
            "stage": stage,
            "current_stage": stage,
            "message": stage,
            "stage_message": stage,
            "started_at": started_at,
            "updated_at": completed_at or started_at,
            "completed_at": completed_at,
            "activity_messages": [],
        }
        (job_folder / "status.json").write_text(json.dumps(status_payload), encoding="utf-8")
        if generated:
            (job_folder / "generated.abap").write_text("REPORT ztest.", encoding="utf-8")
        if metrics is not None:
            (job_folder / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
        if options is not None:
            (job_folder / "options.json").write_text(json.dumps(options), encoding="utf-8")

    def test_skeleton_and_specification_are_inserted_into_prompt(self):
        rendered = render_create_prompt(
            "Header\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\nBody\n{{SPECIFICATION}}",
            "Create a report for sales orders.",
            "REPORT <report_name>.",
            "FOR ALL ENTRIES IN t_header",
        )

        self.assertIn("REPORT <report_name>.", rendered)
        self.assertIn("FOR ALL ENTRIES IN t_header", rendered)
        self.assertIn("Create a report for sales orders.", rendered)
        self.assertNotIn("{{REPORT_SKELETON}}", rendered)
        self.assertNotIn("{{DATABASE_READ_PATTERNS}}", rendered)
        self.assertNotIn("{{SPECIFICATION}}", rendered)

    def test_database_patterns_are_inserted_into_prompt(self):
        rendered = render_create_prompt(
            "{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}",
            "Create a purchasing report.",
            load_report_skeleton(),
            load_database_read_patterns(),
        )

        self.assertIn("DATA SECTION", rendered)
        self.assertIn("Create a purchasing report.", rendered)
        self.assertIn("FOR ALL ENTRIES IN t_header", rendered)
        self.assertIn("IF t_header[] IS NOT INITIAL.", rendered)
        self.assertIn("Do not generate SQL subqueries", rendered)

    def test_chunked_generation_falls_back_to_full_program_generator(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            calls = []

            def generator(prompt_text, source_text):
                calls.append((prompt_text, source_text))
                if len(calls) == 1:
                    raise RuntimeError("chunk failed")
                return {
                    "text": "REPORT zfallback.",
                    "model": "fallback-model",
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                }

            result = generate_abap_with_orchestrator("Prompt.", "Spec.", temp_path, abap_generator=generator)

            self.assertEqual(result["text"], "REPORT zfallback.")
            self.assertTrue(result["used_fallback"])
            self.assertIn("RuntimeError: chunk failed", result["fallback_reason"])
            self.assertEqual(len(calls), 2)
            diagnostic = json.loads((temp_path / "abap_generation_chunks.json").read_text(encoding="utf-8"))
            self.assertTrue(diagnostic["used_fallback"])
            self.assertEqual(diagnostic["assembled_abap"], "REPORT zfallback.")
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_upload_pauses_for_processing_plan_review_and_approval_resumes_generation(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}", encoding="utf-8")
            responses = chunked_test_responses()
            calls = []

            def generator(prompt_text, source_text):
                calls.append((prompt_text, source_text))
                if "Extract declaration requirements" in prompt_text:
                    return {"text": json.dumps({"report_name": "ztest"}), "model": "test-model", "usage": None}
                if "Extract business-processing logic" in prompt_text:
                    return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}
                chunk_name = next(name for name in responses if f"Chunk: {name}" in prompt_text)
                return {"text": responses[chunk_name], "model": "test-model", "usage": None}

            with patch("services.create_abap.generate_abap", side_effect=generator):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()

                upload = client.post(
                    "/upload",
                    data={"abap_file": (BytesIO(b"Create a test report."), "request.txt")},
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )
                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                paused = wait_for_status(jobs_folder, job_id, "Awaiting Review")

                self.assertEqual(paused["current_stage"], "awaiting_processing_plan_review")
                self.assertTrue((jobs_folder / job_id / PROCESSING_PLAN_PROPOSAL_ARTIFACT).exists())
                self.assertFalse((jobs_folder / job_id / "abap_generation_chunks.json").exists())
                review = client.get(f"/processing-plan/{job_id}")
                self.assertEqual(review.status_code, 200)
                self.assertIn(b"Readable Summary", review.data)
                self.assertIn(b"Structured JSON", review.data)
                self.assertIn(b"Validation Errors", review.data) if b"Validation Errors" in review.data else None

                approve = client.post(f"/processing-plan/{job_id}", data={"action": "approve"}, follow_redirects=False)
                self.assertEqual(approve.status_code, 302)
                wait_for_status(jobs_folder, job_id, "Complete")

            self.assertTrue((jobs_folder / job_id / APPROVED_PROCESSING_PLAN_ARTIFACT).exists())
            diagnostics = json.loads((jobs_folder / job_id / PROCESSING_PLAN_DIAGNOSTICS_ARTIFACT).read_text(encoding="utf-8"))
            self.assertIn("raw_response_json", diagnostics)
            self.assertIn("parsed_plan_before_normalization", diagnostics)
            self.assertIn("processing_plan_trace", diagnostics)
            self.assertIn("final_plan_passed_to_deterministic_validation", diagnostics)
            self.assertTrue((jobs_folder / job_id / "abap_generation_chunks.json").exists())
            self.assertEqual(len([call for call in calls if "Extract business-processing logic" in call[0]]), 1)
            self.assertIn("REPORT ztest.", (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_optional_functional_spec_preparation_requires_acceptance_before_generation(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            create_prompt_path = temp_path / "create_abap.txt"
            prepare_prompt_path = temp_path / "prepare_functional_specification.txt"
            create_prompt_path.write_text("Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}", encoding="utf-8")
            prepare_prompt_path.write_text("Prepare functional specification.", encoding="utf-8")
            responses = chunked_test_responses()
            generation_calls = []
            dependency_calls = []
            preparation_calls = []

            def prepare_generator(prompt_text, source_text):
                preparation_calls.append((prompt_text, source_text))
                return {
                    "text": "# Functional Specification\n\n## Processing Rules\n1. Read VBAK and output VBELN.",
                    "model": "prepare-model",
                    "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                }

            def generator(prompt_text, source_text):
                generation_calls.append((prompt_text, source_text))
                chunk_name = next(name for name in responses if f"Chunk: {name}" in prompt_text)
                return {"text": responses[chunk_name], "model": "test-model", "usage": None}

            def dependency_generator(prompt_text, source_text, response_format=None):
                dependency_calls.append((prompt_text, source_text))
                if "Extract declaration requirements" in prompt_text:
                    return {"text": json.dumps({"report_name": "ztest"}), "model": "test-model", "usage": None}
                if "Extract business-processing logic" in prompt_text:
                    return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}
                return {"text": json.dumps({"ddic_objects": [], "callables": []}), "model": "test-model", "usage": None}

            with patch("services.functional_spec_preparation.generate_functional_specification", side_effect=prepare_generator), patch(
                "services.create_abap.generate_abap",
                side_effect=generator,
            ), patch("services.create_abap.generate_dependency_analysis", side_effect=dependency_generator):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(create_prompt_path),
                        "PREPARE_FUNCTIONAL_SPEC_PROMPT": str(prepare_prompt_path),
                    }
                )
                client = app.test_client()

                upload = client.post(
                    "/upload",
                    data={
                        "prepare_functional_specification": "1",
                        "abap_file": (BytesIO(b"Legacy source spec text."), "source_spec.txt"),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

                self.assertEqual(upload.status_code, 302)
                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                paused = wait_for_status(jobs_folder, job_id, "Awaiting Review")
                self.assertEqual(paused["current_stage"], "awaiting_functional_specification_review")
                self.assertTrue((jobs_folder / job_id / FUNCTIONAL_SPEC_PROPOSAL_ARTIFACT).exists())
                self.assertFalse((jobs_folder / job_id / "abap_generation_chunks.json").exists())
                self.assertEqual(len(preparation_calls), 1)
                self.assertEqual(preparation_calls[0][1], "Legacy source spec text.")

                review = client.get(f"/functional-specification/{job_id}")
                self.assertEqual(review.status_code, 200)
                self.assertIn(b"Original Upload", review.data)
                self.assertIn(b"Prepared Functional Specification", review.data)

                accept = client.post(
                    f"/functional-specification/{job_id}",
                    data={
                        "action": "accept",
                        "prepared_specification": "# Functional Specification\n\n## Processing Rules\n1. Read VBAK and output VBELN.\n2. Edited by reviewer.",
                    },
                    follow_redirects=False,
                )
                self.assertEqual(accept.status_code, 302)
                paused_again = wait_for_stage(jobs_folder, job_id, "awaiting_processing_plan_review")
                self.assertEqual(paused_again["current_stage"], "awaiting_processing_plan_review")
                self.assertEqual(
                    (jobs_folder / job_id / ACCEPTED_FUNCTIONAL_SPEC_ARTIFACT).read_text(encoding="utf-8"),
                    "# Functional Specification\n\n## Processing Rules\n1. Read VBAK and output VBELN.\n2. Edited by reviewer.",
                )
                processing_call = next(call for call in dependency_calls if "Extract business-processing logic" in call[0])
                self.assertIn("Edited by reviewer.", processing_call[1])
                progress_status = client.get(f"/progress/{job_id}/status").get_json()
                self.assertEqual(progress_status["review_url"], f"/processing-plan/{job_id}")
                self.assertEqual(progress_status["review_label"], "Review processing plan")

                approve = client.post(f"/processing-plan/{job_id}", data={"action": "approve"}, follow_redirects=False)
                self.assertEqual(approve.status_code, 302)
                wait_for_status(jobs_folder, job_id, "Complete")
                self.assertTrue((jobs_folder / job_id / "generated.abap").exists())
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_processing_plan_review_saves_processing_rules_source_diagnostic(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}", encoding="utf-8")
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            source_text = (
                "# Functional Specification\n"
                "Intro text.\n\n"
                "## Processing Rules\n"
                "Loop through selected records.\n"
                "Move status to the output row.\n\n"
                "## ALV Output\n"
                "Display columns.\n"
            )
            input_path.write_text(source_text, encoding="utf-8")
            calls = []

            def generator(prompt_text, source_text):
                calls.append((prompt_text, source_text))
                if "Extract declaration requirements" in prompt_text:
                    return {"text": json.dumps({"report_name": "ztest"}), "model": "test-model", "usage": None}
                if "Extract business-processing logic" in prompt_text:
                    return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}
                return {"text": "REPORT should_not_run.", "model": "test-model", "usage": None}

            with patch("services.create_abap.generate_abap", side_effect=generator):
                job_id = create_job(jobs_folder)
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    processing_plan_review_required=True,
                )

            expected = "Loop through selected records.\nMove status to the output row."
            processing_call = next(call for call in calls if "Extract business-processing logic" in call[0])
            self.assertEqual(expected, processing_call[1])
            artifact = jobs_folder / job_id / PROCESSING_PLAN_LLM_SOURCE_ARTIFACT
            self.assertEqual(expected, artifact.read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_processing_plan_edit_is_validated_before_approval(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            client = app.test_client()
            job_id = create_job(jobs_folder)
            job_folder = jobs_folder / job_id
            job_folder.mkdir(parents=True, exist_ok=True)
            (job_folder / PROCESSING_PLAN_CONTEXT_ARTIFACT).write_text(
                json.dumps(
                    {
                        "prompt_text": "Shared generation contract:\nExact FORM names: process_data",
                        "declaration_requirements": {
                            "requirements": {
                                "output_structure_fields": [{"name": "MSG", "type_or_like": "TYPE string"}]
                            }
                        },
                        "callable_metadata": {},
                    }
                ),
                encoding="utf-8",
            )
            (job_folder / PROCESSING_PLAN_PROPOSAL_ARTIFACT).write_text(
                json.dumps({"summary": "No steps", "plan": {"processing_steps": []}, "structured_json": "{\"processing_steps\": []}", "validation_errors": [], "validation_warnings": []}),
                encoding="utf-8",
            )

            response = client.post(
                f"/processing-plan/{job_id}",
                data={
                    "action": "approve_edit",
                    "structured_json": json.dumps(
                        {
                            "processing_steps": [
                                {"step": 1, "operation": "MOVE", "source": "lv_missing", "target": "w_output-UNKNOWN"}
                            ]
                        }
                    ),
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn(b"Validation Errors", response.data)
            self.assertIn(b"Approval did not continue because deterministic validation still found errors.", response.data)
            self.assertIn(b'<details class="validation-panel" open>', response.data)
            self.assertFalse((job_folder / APPROVED_PROCESSING_PLAN_ARTIFACT).exists())
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_processing_plan_review_panels_are_collapsed_by_default(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            client = app.test_client()
            job_id = create_job(jobs_folder)
            job_folder = jobs_folder / job_id
            job_folder.mkdir(parents=True, exist_ok=True)
            upload_path = temp_path / "uploads" / job_id / "request.txt"
            upload_path.parent.mkdir(parents=True)
            upload_path.write_text("Uploaded spec text.", encoding="utf-8")
            (job_folder / PROCESSING_PLAN_CONTEXT_ARTIFACT).write_text(
                json.dumps({"input_path": str(upload_path)}),
                encoding="utf-8",
            )
            (job_folder / PROCESSING_PLAN_PROPOSAL_ARTIFACT).write_text(
                json.dumps(
                    {
                        "summary": "- LOOP: source=t_edidc",
                        "plan": {"processing_steps": []},
                        "structured_json": "{\"processing_steps\": []}",
                        "validation_errors": ["metadata issue"],
                        "validation_warnings": ["normalization warning"],
                        "diagnostics": {
                            "prompt": "Extract business-processing logic.",
                        },
                    }
                ),
                encoding="utf-8",
            )

            response = client.get(f"/processing-plan/{job_id}")
            html = response.data.decode("utf-8")

            self.assertEqual(response.status_code, 200)
            self.assertIn('<a class="button-link" href="/">New</a>', html)
            self.assertIn(f'<a class="button-link" href="/progress/{job_id}">Progress</a>', html)
            self.assertIn('<details class="validation-panel">\n          <summary>Validation Errors</summary>', html)
            self.assertIn('<details class="validation-panel">\n          <summary>Validation Warnings</summary>', html)
            self.assertIn('<details class="code-panel">\n          <summary>LLM Prompt</summary>', html)
            self.assertIn('<details class="code-panel">\n          <summary>Readable Summary</summary>', html)
            self.assertIn("You can still approve the saved proposal", html)
            self.assertIn('<button type="submit" name="action" value="approve">Approve</button>', html)
            self.assertNotIn('value="approve" disabled', html)
            self.assertIn("OpenAI Responses API input:", html)
            self.assertIn("[system]", html)
            self.assertIn("Extract business-processing logic.", html)
            self.assertIn("[user]", html)
            self.assertIn("Uploaded spec text.", html)
            self.assertLess(html.index("<summary>LLM Prompt</summary>"), html.index("<summary>Readable Summary</summary>"))
            self.assertNotIn('<details class="validation-panel" open', html)
            self.assertNotIn('<details class="code-panel" open', html)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_processing_plan_saved_proposal_can_be_approved_with_validation_errors(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            job_id = create_job(jobs_folder)
            job_folder = jobs_folder / job_id
            job_folder.mkdir(parents=True, exist_ok=True)
            (job_folder / PROCESSING_PLAN_CONTEXT_ARTIFACT).write_text(
                json.dumps(
                    {
                        "prompt_text": "Shared generation contract:\nExact FORM names: process_data",
                        "declaration_requirements": {
                            "requirements": {
                                "output_structure_fields": [{"name": "MSG", "type_or_like": "TYPE string"}]
                            }
                        },
                        "callable_metadata": {},
                    }
                ),
                encoding="utf-8",
            )

            result = approve_processing_plan_for_job(
                jobs_folder,
                job_id,
                {"processing_steps": [{"operation": "MOVE", "source": "lv_missing", "target": "w_output-UNKNOWN"}]},
                allow_validation_errors=True,
            )

            self.assertTrue(result["approved"])
            approved = json.loads((job_folder / APPROVED_PROCESSING_PLAN_ARTIFACT).read_text(encoding="utf-8"))
            self.assertTrue(approved["approved"])
            self.assertIn("validation_errors", approved)
            self.assertTrue(approved["validation_errors"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_processing_plan_rejection_stops_generation(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            client = app.test_client()
            job_id = create_job(jobs_folder)
            job_folder = jobs_folder / job_id
            job_folder.mkdir(parents=True, exist_ok=True)
            (job_folder / PROCESSING_PLAN_PROPOSAL_ARTIFACT).write_text(
                json.dumps({"summary": "No steps", "plan": {"processing_steps": []}, "structured_json": "{\"processing_steps\": []}", "validation_errors": [], "validation_warnings": []}),
                encoding="utf-8",
            )

            response = client.post(f"/processing-plan/{job_id}", data={"action": "reject"})

            self.assertEqual(response.status_code, 302)
            progress = get_progress(jobs_folder, job_id)
            self.assertEqual(progress["status"], "Rejected")
            self.assertEqual(progress["current_stage"], "processing_plan_rejected")
            self.assertFalse((job_folder / "abap_generation_chunks.json").exists())
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_processing_plan_reextract_replaces_proposal_without_approval(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}", encoding="utf-8")
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a test report.", encoding="utf-8")
            plan_calls = []

            def generator(prompt_text, source_text):
                if "Extract declaration requirements" in prompt_text:
                    return {"text": json.dumps({"report_name": "ztest"}), "model": "test-model", "usage": None}
                if "Extract business-processing logic" in prompt_text:
                    plan_calls.append(prompt_text)
                    return {"text": json.dumps({"processing_steps": [{"step": 1, "operation": "CLEAR", "target": f"w_plan_{len(plan_calls)}"}]}), "model": "test-model", "usage": None}
                return {"text": "REPORT ztest.", "model": "test-model", "usage": None}

            with patch("services.create_abap.generate_abap", side_effect=generator):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()
                job_id = create_job(jobs_folder)
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    processing_plan_review_required=True,
                )
                wait_for_status(jobs_folder, job_id, "Awaiting Review")
                first = load_processing_plan_proposal(jobs_folder, job_id)["structured_json"]

                response = client.post(f"/processing-plan/{job_id}", data={"action": "reextract"})
                self.assertEqual(response.status_code, 302)
                deadline = real_time.time() + 5
                second = first
                while real_time.time() < deadline and second == first:
                    real_time.sleep(0.05)
                    second = load_processing_plan_proposal(jobs_folder, job_id)["structured_json"]

            self.assertNotEqual(first, second)
            self.assertFalse((jobs_folder / job_id / APPROVED_PROCESSING_PLAN_ARTIFACT).exists())
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_invalid_extracted_processing_plan_pauses_for_review_without_generating_chunks(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}", encoding="utf-8")
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create output.", encoding="utf-8")

            def generator(prompt_text, source_text):
                if "Extract declaration requirements" in prompt_text:
                    return {"text": json.dumps({"output_structure_fields": [{"name": "MSG", "type_or_like": "TYPE string"}]}), "model": "test-model", "usage": None}
                if "Extract business-processing logic" in prompt_text:
                    return {"text": json.dumps({"processing_steps": [{"step": 1, "operation": "MOVE", "source": "lv_missing", "target": "w_output-UNKNOWN"}]}), "model": "test-model", "usage": None}
                return {"text": "REPORT should_not_run.", "model": "test-model", "usage": None}

            with patch("services.create_abap.generate_abap", side_effect=generator):
                job_id = create_job(jobs_folder)
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    processing_plan_review_required=True,
                )

            progress = get_progress(jobs_folder, job_id)
            self.assertEqual(progress["status"], "Awaiting Review")
            proposal = load_processing_plan_proposal(jobs_folder, job_id)
            self.assertTrue(proposal["validation_errors"])
            self.assertIn("lv_missing", proposal["structured_json"])
            self.assertFalse((jobs_folder / job_id / "abap_generation_chunks.json").exists())
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_processing_plan_review_payload_prefers_non_empty_attempt_over_empty_retry(self):
        payload = processing_plan_review_payload(
            {
                "plan": {"processing_steps": []},
                "validation_errors": [],
                "normalization_diagnostics": {"rejected_steps": []},
                "attempts": [
                    {
                        "plan": None,
                        "invalid_plan": {
                            "processing_steps": [
                                {
                                    "step": 1,
                                    "operation": "LOOP",
                                    "source": "t_edidc",
                                    "into": "st_edidc",
                                    "steps": [
                                        {
                                            "step": 2,
                                            "operation": "MOVE",
                                            "source": "st_edidc-DOCNUM",
                                            "target": "w_output-DOCNUM",
                                        }
                                    ],
                                }
                            ]
                        },
                        "validation_errors": ["processing_plan.processing_steps.0 has a metadata issue"],
                        "normalization_diagnostics": {"rejected_steps": []},
                        "prompt": "Extract business-processing logic.",
                        "source_text": "Uploaded spec text.",
                    },
                    {
                        "plan": {"processing_steps": []},
                        "validation_errors": [],
                        "normalization_diagnostics": {"rejected_steps": []},
                    },
                ],
            }
        )

        self.assertIn("t_edidc", payload["structured_json"])
        self.assertNotEqual({"processing_steps": []}, payload["plan"])
        self.assertEqual(["processing_plan.processing_steps.0 has a metadata issue"], payload["validation_errors"])
        self.assertIn("[system]\nExtract business-processing logic.", payload["llm_request"])
        self.assertIn("[user]\nUploaded spec text.", payload["llm_request"])

    def test_processing_plan_review_payload_reports_empty_plan_caused_by_parse_failure(self):
        payload = processing_plan_review_payload(
            {
                "plan": {"processing_steps": []},
                "validation_errors": [],
                "retryable_validation_errors": [
                    "invalid JSON: JSONDecodeError: Expecting ',' delimiter: line 363 column 29 (char 21231)",
                    "processing plan root is not an object",
                ],
                "parse_error": "JSONDecodeError: Expecting ',' delimiter: line 363 column 29 (char 21231)",
                "normalization_diagnostics": {
                    "rejected_steps": [
                        {
                            "reason": "processing plan root is not an object",
                        }
                    ]
                },
            }
        )

        self.assertEqual({"processing_steps": []}, payload["plan"])
        self.assertIn("did not produce a valid reviewable plan", payload["summary"])
        self.assertIn("invalid JSON", payload["summary"])
        self.assertIn("invalid JSON", payload["validation_errors"][0])
        self.assertIn("processing plan root is not an object", payload["validation_errors"])
        self.assertNotEqual("No business-processing steps were extracted.", payload["summary"])

    def test_processing_plan_readable_summary_shows_if_condition_values(self):
        payload = processing_plan_review_payload(
            {
                "plan": {
                    "processing_steps": [
                        {
                            "operation": "IF",
                            "conditions": [
                                {"left": "p_email", "operator": "=", "right": "'X'"},
                                {"left": "p_sender", "operator": "<>", "right": "''"},
                                {"left": "bapi_enqueue_return", "operator": "CONTAINS ERROR"},
                            ],
                            "then": [
                                {
                                    "operation": "CALL_STATIC_METHOD",
                                    "class": "zcl_mailer",
                                    "method": "send",
                                    "input_parameters": {"IV_SENDER": "p_sender"},
                                    "output_parameters": {},
                                }
                            ],
                            "else": [],
                        }
                    ]
                },
                "validation_errors": [],
                "normalization_diagnostics": {"rejected_steps": []},
            }
        )

        self.assertIn("- IF: p_email = 'X' AND p_sender <> '' AND bapi_enqueue_return CONTAINS ERROR", payload["summary"])
        self.assertIn("- CALL_STATIC_METHOD: class=zcl_mailer, method=send, input_parameters=IV_SENDER -> p_sender, output_parameters={}", payload["summary"])
        self.assertIn("else: []", payload["summary"])

    def test_processing_plan_readable_summary_renders_nested_structured_content_generically(self):
        payload = processing_plan_review_payload(
            {
                "plan": {
                    "processing_steps": [
                        {
                            "operation": "LOOP",
                            "source": "t_output",
                            "into": "w_output",
                            "steps": [
                                {
                                    "operation": "READ",
                                    "source": "t_pa0016",
                                    "into": "st_pa0016",
                                    "conditions": [
                                        {"left": "st_pa0016-PERNR", "operator": "=", "right": "w_output-PERNR"},
                                        {"left": "st_pa0016-ENDDA", "operator": ">=", "right": "sy-datum"},
                                    ],
                                },
                                {
                                    "operation": "CALL_FUNCTION",
                                    "name": "Z_SEND_NOTICE",
                                    "input_parameters": {
                                        "IV_PERNR": "w_output-PERNR",
                                        "IV_SENDER": "p_sender",
                                    },
                                    "output_parameters": {
                                        "EV_STATUS": "w_output-STATUS",
                                    },
                                },
                                {
                                    "operation": "NEW_OPERATION",
                                    "custom_attribute": {"alpha": "one", "beta": "two"},
                                    "empty_values": [],
                                    "items": [{"name": "first"}, {"name": "second"}],
                                },
                            ],
                        }
                    ]
                },
                "validation_errors": [],
                "normalization_diagnostics": {"rejected_steps": []},
            }
        )

        summary = payload["summary"]
        self.assertIn("- LOOP: source=t_output, into=w_output", summary)
        self.assertIn(
            "- READ: source=t_pa0016, into=st_pa0016, st_pa0016-PERNR = w_output-PERNR AND st_pa0016-ENDDA >= sy-datum",
            summary,
        )
        self.assertIn(
            "- CALL_FUNCTION: name=Z_SEND_NOTICE, input_parameters=IV_PERNR -> w_output-PERNR, IV_SENDER -> p_sender, output_parameters=EV_STATUS -> w_output-STATUS",
            summary,
        )
        self.assertIn("- NEW_OPERATION: custom_attribute=alpha -> one, beta -> two, empty_values=[], items=[name -> first; name -> second]", summary)

    def test_loading_processing_plan_proposal_refreshes_stale_readable_summary(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            job_id = create_job(jobs_folder)
            job_folder = jobs_folder / job_id
            job_folder.mkdir(parents=True, exist_ok=True)
            (job_folder / PROCESSING_PLAN_PROPOSAL_ARTIFACT).write_text(
                json.dumps(
                    {
                        "summary": "- IF: conditions=1",
                        "plan": {
                            "processing_steps": [
                                {
                                    "operation": "IF",
                                    "conditions": [{"left": "p_email", "operator": "=", "right": "'X'"}],
                                    "then": [],
                                    "else": [],
                                }
                            ]
                        },
                        "structured_json": "{}",
                        "validation_errors": [],
                        "validation_warnings": [],
                    }
                ),
                encoding="utf-8",
            )

            proposal = load_processing_plan_proposal(jobs_folder, job_id)

            self.assertIn("- IF: p_email = 'X'", proposal["summary"])
            self.assertNotIn("conditions=1", proposal["summary"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_generation_resumes_from_approved_plan_without_reextracting(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            calls = []
            responses = chunked_test_responses()

            def generator(prompt_text, source_text):
                calls.append((prompt_text, source_text))
                self.assertNotIn("Extract business-processing logic", prompt_text)
                chunk_name = next(name for name in responses if f"Chunk: {name}" in prompt_text)
                return {"text": responses[chunk_name], "model": "test-model", "usage": None}

            result = generate_abap_with_orchestrator(
                "Shared generation contract:\nExact FORM names: process_data",
                "Functional spec.",
                temp_path,
                abap_generator=generator,
                declaration_requirements={"requirements": {"report_name": "ztest"}},
                approved_processing_plan={"plan": {"processing_steps": []}},
            )

            self.assertEqual(len(calls), 5)
            self.assertEqual(result["processing_plan"]["plan"], {"processing_steps": []})
            self.assertIn("REPORT ztest.", result["text"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_abap_generation_receives_and_emits_method_call_operations(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            calls = []
            plan = {
                "plan": {
                    "processing_steps": [
                        {
                            "operation": "IF",
                            "conditions": [{"left": "p_email", "operator": "=", "right": "'X'"}],
                            "then": [
                                {
                                    "operation": "CALL_STATIC_METHOD",
                                    "class": "ZCL_MAIL_FACTORY",
                                    "method": "CREATE",
                                    "input_parameters": {"IV_SENDER": "p_sender"},
                                    "receiving_parameter": "lo_sender",
                                },
                                {
                                    "operation": "CALL_METHOD",
                                    "object": "lo_sender",
                                    "method": "SEND",
                                    "input_parameters": {"IV_PERNR": "w_output-PERNR"},
                                    "output_parameters": {"EV_STATUS": "w_output-STATUS"},
                                },
                            ],
                            "else": [],
                        }
                    ]
                }
            }

            def generator(prompt_text, _source_text):
                calls.append(prompt_text)
                if "Chunk: declarations" in prompt_text:
                    return {"text": "REPORT ztest.", "model": "test-model", "usage": None}
                if "Chunk: processing_form" in prompt_text:
                    self.assertIn('"operation": "CALL_STATIC_METHOD"', prompt_text)
                    self.assertIn('"class": "ZCL_MAIL_FACTORY"', prompt_text)
                    self.assertIn('"operation": "CALL_METHOD"', prompt_text)
                    self.assertIn('"object": "lo_sender"', prompt_text)
                    return {
                        "text": (
                            "FORM process_data.\n"
                            "  CALL METHOD zcl_mail_factory=>create\n"
                            "    EXPORTING iv_sender = p_sender\n"
                            "    RECEIVING ro_sender = lo_sender.\n"
                            "  CALL METHOD lo_sender->send\n"
                            "    EXPORTING iv_pernr = w_output-pernr\n"
                            "    IMPORTING ev_status = w_output-status.\n"
                            "ENDFORM."
                        ),
                        "model": "test-model",
                        "usage": None,
                    }
                if "Chunk: database_read_forms" in prompt_text:
                    return {"text": "FORM read_data.\nENDFORM.", "model": "test-model", "usage": None}
                if "Chunk: output_forms" in prompt_text:
                    return {"text": "FORM display_output.\nENDFORM.", "model": "test-model", "usage": None}
                if "Chunk: main_program_flow" in prompt_text:
                    return {"text": "START-OF-SELECTION.", "model": "test-model", "usage": None}
                return {"text": "", "model": "test-model", "usage": None}

            result = generate_abap_with_orchestrator(
                "Shared generation contract:\nExact FORM names: process_data",
                "If P_EMAIL = 'X', send mail.",
                temp_path,
                abap_generator=generator,
                declaration_requirements={
                    "requirements": {
                        "report_name": "ztest",
                        "parameters": [{"name": "P_EMAIL"}, {"name": "P_SENDER"}],
                        "global_variables": [
                            {"name": "lo_sender", "declaration": "DATA lo_sender TYPE REF TO zcl_mail_sender."}
                        ],
                        "output_structure_fields": [{"name": "PERNR"}, {"name": "STATUS"}],
                    }
                },
                approved_processing_plan=plan,
            )

            self.assertIn("CALL METHOD zcl_mail_factory=>create", result["text"])
            self.assertIn("CALL METHOD lo_sender->send", result["text"])
            self.assertTrue(any('"operation": "CALL_METHOD"' in call for call in calls))
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_chunked_generation_fallback_preserves_processed_chunk_diagnostics(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            responses = {
                "declarations": "REPORT ztest.",
                "database_read_forms": "FORM read_data.\nENDFORM.",
                "processing_form": "FORM process_data.\n  APPEND w_output TO gt_output.\nENDFORM.",
            }

            def generator(prompt_text, _source_text):
                if "Extract declaration requirements" in prompt_text:
                    return {
                        "text": json.dumps(
                            {
                                "report_name": "ztest",
                                "parameters": [],
                                "select_options": [],
                                "output_structure_fields": [
                                    {"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"}
                                ],
                            }
                        ),
                        "model": "test-model",
                        "usage": None,
                    }
                if "Extract business-processing logic" in prompt_text:
                    return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}
                for chunk_name, text in responses.items():
                    if f"Chunk: {chunk_name}" in prompt_text:
                        return {"text": text, "model": "test-model", "usage": None}
                return {"text": "REPORT zfallback.", "model": "fallback-model", "usage": None}

            result = generate_abap_with_orchestrator(
                "Shared generation contract:\nExact FORM names: process_data",
                "Create output records.",
                temp_path,
                abap_generator=generator,
            )

            self.assertTrue(result["used_fallback"])
            self.assertEqual(result["text"], "REPORT zfallback.")
            self.assertEqual([chunk["name"] for chunk in result["chunks"]], ["declarations", "database_read_forms", "processing_form"])
            self.assertIn("gt_output", result["chunks"][2]["error"])
            diagnostic = json.loads((temp_path / "abap_generation_chunks.json").read_text(encoding="utf-8"))
            self.assertTrue(diagnostic["used_fallback"])
            self.assertEqual([chunk["name"] for chunk in diagnostic["chunks"]], ["declarations", "database_read_forms", "processing_form"])
            self.assertIn("gt_output", diagnostic["chunks"][2]["error"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_report_skeleton_declares_types_before_data(self):
        skeleton = load_report_skeleton()

        self.assertLess(skeleton.index("* Types"), skeleton.index("* Internal tables"))
        self.assertLess(skeleton.index("* Types"), skeleton.index("* Structures and work areas"))

    def test_missing_database_patterns_has_clear_error(self):
        missing_path = Path(__file__).resolve().parents[1] / f".test_missing_{uuid4().hex}" / "database_read_patterns.abap"

        with self.assertRaises(FileNotFoundError) as context:
            load_database_read_patterns(missing_path)

        self.assertIn("Database read patterns template is missing:", str(context.exception))
        self.assertIn("database_read_patterns.abap", str(context.exception))

    def test_missing_report_skeleton_has_clear_error(self):
        missing_path = Path(__file__).resolve().parents[1] / f".test_missing_{uuid4().hex}" / "report_skeleton.abap"

        with self.assertRaises(FileNotFoundError) as context:
            load_report_skeleton(missing_path)

        self.assertIn("Report skeleton template is missing:", str(context.exception))
        self.assertIn("report_skeleton.abap", str(context.exception))

    def test_home_includes_sap_syntax_check_attempts_input(self):
        app = create_app({"TESTING": True})
        client = app.test_client()

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="sap_syntax_check_attempts"', response.data)
        self.assertIn(b'type="number"', response.data)
        self.assertIn(b'min="1"', response.data)
        self.assertIn(b'max="10"', response.data)
        self.assertIn(b'value="2"', response.data)

    def test_home_includes_openai_model_presets_and_advanced_fields(self):
        app = create_app({"TESTING": True})

        response = app.test_client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'for="tab-settings">Settings</label>', response.data)
        self.assertIn(b'<section class="tab-panel tab-panel-settings">', response.data)
        self.assertIn(b'value="economy" checked', response.data)
        self.assertIn(b'value="balanced"', response.data)
        self.assertIn(b'value="best_quality"', response.data)
        self.assertIn(b'value="advanced"', response.data)
        self.assertIn(b'form="new-program-form"', response.data)
        self.assertIn(b'form="enhance-program-form"', response.data)
        self.assertIn(b'General: GPT-5.6 Luna', response.data)
        self.assertIn(b'Dependency: GPT-5.6 Luna', response.data)
        self.assertIn(b'Generation: GPT-5.6 Terra', response.data)
        self.assertIn(b'Review: GPT-5.6 Luna', response.data)
        self.assertIn(b'General: GPT-5.6 Terra', response.data)
        self.assertIn(b'Generation: GPT-5.6 Sol', response.data)
        self.assertIn(b'Review: GPT-5.6 Sol', response.data)
        self.assertIn(b'name="OPENAI_MODEL"', response.data)
        self.assertIn(b'name="OPENAI_ABAP_GENERATION_MODEL"', response.data)
        self.assertIn(b'name="OPENAI_DEPENDENCY_ANALYSIS_MODEL"', response.data)
        self.assertIn(b'name="OPENAI_CODE_REVIEW_MODEL"', response.data)
        self.assertIn(b'GPT-5 Mini', response.data)
        self.assertEqual(8, response.data.count(b'value="gpt-5-mini"'))

    def test_upload_saves_default_economy_model_settings(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            uploads_folder = temp_path / "uploads"
            app = create_app({
                "TESTING": True,
                "UPLOAD_FOLDER": str(uploads_folder),
                "JOBS_FOLDER": str(jobs_folder),
            })

            with patch("app.start_create_abap_job"):
                response = app.test_client().post(
                    "/upload",
                    data={"abap_file": (BytesIO(b"Create a test report."), "request.txt")},
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 302)
            job_id = response.headers["Location"].rsplit("/", 1)[-1]
            options = json.loads((jobs_folder / job_id / "options.json").read_text(encoding="utf-8"))
            self.assertEqual("app", options["final_assembly_mode"])
            self.assertEqual("economy", options["model_settings"]["preset"])
            self.assertEqual(
                {
                    "general": "gpt-5.6-luna",
                    "dependency_analysis": "gpt-5.6-luna",
                    "abap_generation": "gpt-5.6-terra",
                    "code_review": "gpt-5.6-luna",
                },
                options["model_settings"]["models"],
            )
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_upload_accepts_pasted_specification_text(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            uploads_folder = temp_path / "uploads"
            app = create_app({
                "TESTING": True,
                "UPLOAD_FOLDER": str(uploads_folder),
                "JOBS_FOLDER": str(jobs_folder),
            })

            with patch("app.start_create_abap_job") as start_job:
                response = app.test_client().post(
                    "/upload",
                    data={"specification_text": "Create a pasted specification report."},
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 302)
            job_id = response.headers["Location"].rsplit("/", 1)[-1]
            input_path = uploads_folder / job_id / "pasted_specification.txt"
            self.assertEqual(input_path.read_text(encoding="utf-8"), "Create a pasted specification report.")
            self.assertEqual(start_job.call_args.kwargs["input_path"], input_path)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_upload_saves_llm_final_assembly_mode(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            uploads_folder = temp_path / "uploads"
            app = create_app({
                "TESTING": True,
                "UPLOAD_FOLDER": str(uploads_folder),
                "JOBS_FOLDER": str(jobs_folder),
            })

            with patch("app.start_create_abap_job"):
                response = app.test_client().post(
                    "/upload",
                    data={
                        "abap_file": (BytesIO(b"Create a test report."), "request.txt"),
                        "final_assembly_mode": "llm",
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 302)
            job_id = response.headers["Location"].rsplit("/", 1)[-1]
            options = json.loads((jobs_folder / job_id / "options.json").read_text(encoding="utf-8"))
            self.assertEqual("llm", options["final_assembly_mode"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_upload_saves_advanced_model_settings(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            uploads_folder = temp_path / "uploads"
            app = create_app({
                "TESTING": True,
                "UPLOAD_FOLDER": str(uploads_folder),
                "JOBS_FOLDER": str(jobs_folder),
            })

            with patch("app.start_create_abap_job"):
                response = app.test_client().post(
                    "/upload",
                    data={
                        "abap_file": (BytesIO(b"Create a test report."), "request.txt"),
                        "model_preset": "advanced",
                        "OPENAI_MODEL": "gpt-5-mini",
                        "OPENAI_DEPENDENCY_ANALYSIS_MODEL": "gpt-5-mini",
                        "OPENAI_ABAP_GENERATION_MODEL": "gpt-5-mini",
                        "OPENAI_CODE_REVIEW_MODEL": "gpt-5-mini",
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 302)
            job_id = response.headers["Location"].rsplit("/", 1)[-1]
            options = json.loads((jobs_folder / job_id / "options.json").read_text(encoding="utf-8"))
            self.assertEqual("advanced", options["model_settings"]["preset"])
            self.assertEqual("gpt-5-mini", options["model_settings"]["models"]["general"])
            self.assertEqual("gpt-5-mini", options["model_settings"]["models"]["dependency_analysis"])
            self.assertEqual("gpt-5-mini", options["model_settings"]["models"]["abap_generation"])
            self.assertEqual("gpt-5-mini", options["model_settings"]["models"]["code_review"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_jobs_page_lists_newest_jobs_with_view_actions(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            uploads_folder = temp_path / "uploads"
            completed_job = "completed_job"
            failed_job = "failed_job"
            self.write_job_fixture(
                jobs_folder,
                uploads_folder,
                completed_job,
                status="Complete",
                stage="Complete",
                started_at="2026-08-06T10:00:00+00:00",
                completed_at="2026-08-06T10:00:12+00:00",
                upload_name="old_spec.txt",
                generated=True,
                metrics={
                    "job_mode": "create_abap",
                    "duration_seconds": 12.5,
                    "estimated_input_cost": 0.012345,
                    "estimated_output_cost": 0.111111,
                    "estimated_total_cost": 0.123456,
                    "model_settings": {"preset": "balanced", "preset_label": "Balanced"},
                },
                options={"model_settings": {"preset": "balanced", "preset_label": "Balanced", "models": {}}},
            )
            self.write_job_fixture(
                jobs_folder,
                uploads_folder,
                failed_job,
                status="Error",
                stage="Analyzing dependencies",
                started_at="2026-08-07T09:00:00+00:00",
                completed_at="2026-08-07T09:00:05+00:00",
                upload_name="new_spec.txt",
                generated=False,
                metrics=None,
                options={"model_settings": {"preset": "economy", "preset_label": "Economy", "models": {}}},
            )
            app = create_app({
                "TESTING": True,
                "UPLOAD_FOLDER": str(uploads_folder),
                "JOBS_FOLDER": str(jobs_folder),
            })

            response = app.test_client().get("/jobs")

            self.assertEqual(response.status_code, 200)
            html = response.data.decode("utf-8")
            self.assertIn("style.css?v=", html)
            self.assertIn("jobs-date-col", html)
            self.assertIn('href="/jobs"', html)
            self.assertLess(html.index(failed_job), html.index(completed_job))
            self.assertIn("07-08-2026 10:00:00", html)
            self.assertIn("06-08-2026 11:00:00", html)
            self.assertNotIn("<th>Job ID</th>", html)
            self.assertNotIn(f"<code>{failed_job}</code>", html)
            self.assertNotIn(f"<code>{completed_job}</code>", html)
            self.assertIn("new_spec.txt", html)
            self.assertIn("old_spec.txt", html)
            self.assertIn("Error", html)
            self.assertIn("Complete", html)
            self.assertIn("Economy", html)
            self.assertIn("Balanced", html)
            self.assertIn("12.50s", html)
            self.assertIn("$0.123456", html)
            self.assertIn(f'href="/progress/{failed_job}"', html)
            self.assertIn(f'href="/result/{completed_job}"', html)
            self.assertIn("return confirm(", html)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_jobs_view_links_open_existing_pages(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            uploads_folder = temp_path / "uploads"
            completed_job = "completed_job"
            running_job = "running_job"
            self.write_job_fixture(
                jobs_folder,
                uploads_folder,
                completed_job,
                status="Complete",
                stage="Complete",
                started_at="2026-08-07T08:00:00+00:00",
                completed_at="2026-08-07T08:00:03+00:00",
                upload_name="complete_spec.txt",
                generated=True,
                metrics={"job_mode": "create_abap", "duration_seconds": 3.0},
            )
            self.write_job_fixture(
                jobs_folder,
                uploads_folder,
                running_job,
                status="Running",
                stage="Processing Chunk 1 of 5",
                started_at="2026-08-07T09:00:00+00:00",
                completed_at=None,
                upload_name="running_spec.txt",
                generated=False,
                metrics=None,
            )
            app = create_app({
                "TESTING": True,
                "UPLOAD_FOLDER": str(uploads_folder),
                "JOBS_FOLDER": str(jobs_folder),
            })
            client = app.test_client()

            result_response = client.get(f"/result/{completed_job}")
            progress_response = client.get(f"/progress/{running_job}")

            self.assertEqual(result_response.status_code, 200)
            self.assertIn(b"Generated ABAP", result_response.data)
            self.assertEqual(progress_response.status_code, 200)
            self.assertIn(b"Processing chunks", progress_response.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_jobs_delete_removes_job_and_upload_files(self):
        jobs_folder = Path("jobs")
        uploads_folder = Path("uploads")
        job_id = "delete_job"
        app = create_app({
            "TESTING": True,
            "UPLOAD_FOLDER": str(uploads_folder),
            "JOBS_FOLDER": str(jobs_folder),
        })

        with patch("app.delete_job", return_value=True) as delete_job_mock:
            response = app.test_client().post(f"/jobs/{job_id}/delete")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/jobs")
        delete_job_mock.assert_called_once()
        called_jobs_folder, called_uploads_folder, called_job_id = delete_job_mock.call_args.args
        self.assertEqual(Path(called_jobs_folder), jobs_folder)
        self.assertEqual(Path(called_uploads_folder), uploads_folder)
        self.assertEqual(called_job_id, job_id)

    def test_new_jobs_begin_at_queued(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            job_id = create_job(temp_path)
            progress = get_progress(temp_path, job_id)

            self.assertEqual(progress["status"], "Queued")
            self.assertEqual(progress["current_stage"], "Queued")
            self.assertEqual(progress["stage_message"], "Queued for processing.")
            self.assertEqual(progress["progress_percent"], 0)
            self.assertTrue(progress["is_active"])
            self.assertIsNotNone(progress["started_at"])
            self.assertIsNotNone(progress["updated_at"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_create_abap_flow(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            self._run_flow(temp_path)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_create_stages_update_progress(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a test report.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            stages = []

            def record_progress(jobs_folder_arg, job_id_arg, status, message, stage=None):
                stages.append(stage)
                update_progress(jobs_folder_arg, job_id_arg, status, message, stage=stage)

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": "REPORT zphase2.", "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]), patch(
                "services.create_abap.update_progress",
                side_effect=record_progress,
            ):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path)

            self.assertEqual(
                stages,
                [
                    "Reading specification",
                    "Loading prompt and templates",
                    "Analyzing dependencies",
                    "Loading SAP metadata",
                    "Extracting declaration requirements",
                    "Extracting processing plan",
                    "Processing Chunk 1 of 5",
                    "Processing Chunk 2 of 5",
                    "Processing Chunk 3 of 5",
                    "Processing Chunk 4 of 5",
                    "Processing Chunk 5 of 5",
                    "Cleaning generated ABAP",
                    "Running deterministic validation",
                    "Applying safe deterministic fixes",
                    "Re-running deterministic validation",
                    "Saving results",
                    "Complete",
                ],
            )
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_processing_chunk_stage_is_visible_while_mocked_call_is_active(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a test report.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)

            def generate_while_checking_progress(prompt_text, source_text):
                progress = get_progress(jobs_folder, job_id)
                if "Extract declaration requirements" in prompt_text:
                    self.assertEqual(progress["current_stage"], "Extracting declaration requirements")
                    self.assertEqual(progress["stage_message"], "Extracting declaration requirements...")
                    self.assertEqual(progress["progress_percent"], 30)
                elif "Extract business-processing logic" in prompt_text:
                    self.assertEqual(progress["current_stage"], "Extracting processing plan")
                    self.assertEqual(progress["stage_message"], "Extracting processing plan...")
                    self.assertEqual(progress["progress_percent"], 33)
                else:
                    self.assertRegex(progress["current_stage"], r"^Processing Chunk \d+ of 5$")
                    self.assertRegex(progress["stage_message"], r"^Processing Chunk \d+ of 5$")
                    self.assertGreaterEqual(progress["progress_percent"], 35)
                return {"text": "REPORT zphase2.", "model": "test-model", "usage": None}

            with patch(
                "services.create_abap.generate_abap",
                side_effect=generate_while_checking_progress,
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_processing_chunk_progress_message_includes_subtitle(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Loop over the prepared header rows.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            approved_processing_plan = {
                "plan": {
                    "processing_steps": [
                        {
                            "operation": "LOOP",
                            "source": "t_hdr",
                            "into": "st_hdr",
                            "steps": [],
                        }
                    ]
                }
            }
            declaration_requirements = {
                "requirements": {
                    "report_name": "ztest",
                    "global_variables": [
                        {"name": "t_hdr", "declaration": "DATA t_hdr TYPE STANDARD TABLE OF ty_hdr."},
                        {"name": "st_hdr", "declaration": "DATA st_hdr TYPE ty_hdr."},
                    ],
                    "output_structure_fields": [],
                }
            }

            def generator(prompt_text, _source_text):
                if "Chunk: processing_form" in prompt_text:
                    progress = get_progress(jobs_folder, job_id)
                    self.assertEqual(progress["current_stage"], "Processing Chunk 3 of 5")
                    self.assertEqual(progress["stage_message"], "Processing Chunk 3 of 5 - Step 1: Loop t_hdr")
                    return {"text": "FORM process_data.\n  LOOP AT t_hdr INTO st_hdr.\n  ENDLOOP.\nENDFORM.", "model": "test-model", "usage": None}
                if "Chunk: declarations" in prompt_text:
                    return {"text": "REPORT ztest.", "model": "test-model", "usage": None}
                if "Chunk: database_read_forms" in prompt_text:
                    return {"text": "FORM read_data.\nENDFORM.", "model": "test-model", "usage": None}
                if "Chunk: output_forms" in prompt_text:
                    return {"text": "FORM output_data.\nENDFORM.", "model": "test-model", "usage": None}
                if "Chunk: main_program_flow" in prompt_text:
                    return {"text": "START-OF-SELECTION.", "model": "test-model", "usage": None}
                return {"text": "", "model": "test-model", "usage": None}

            with patch("services.create_abap.generate_abap", side_effect=generator):
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    approved_processing_plan=approved_processing_plan,
                    prepared_declaration_requirements=declaration_requirements,
                )
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_progress_page_uses_display_titles_without_duplicate_processing_plan_stage(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            job_id = create_job(jobs_folder)
            update_progress(
                jobs_folder,
                job_id,
                "Awaiting Review",
                "Review the extracted processing plan.",
                stage="awaiting_processing_plan_review",
            )

            client = app.test_client()
            payload = client.get(f"/progress/{job_id}/status").get_json()
            html = client.get(f"/progress/{job_id}").data.decode("utf-8")
            stage_titles = [stage["title"] for stage in payload["stages"]]

            self.assertEqual(payload["current_stage"], "awaiting_processing_plan_review")
            self.assertEqual(payload["current_stage_title"], "AwaitingProcessingPlanReview")
            self.assertEqual(1, stage_titles.count("Extracting processing plan"))
            self.assertNotIn("extracting_processing_plan", stage_titles)
            self.assertNotIn("awaiting_processing_plan_review", html)
            self.assertIn("AwaitingProcessingPlanReview", html)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_post_generation_diagnostics_capture_each_source_stage(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a test report.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            declaration_chunk = "\n".join(
                [
                    "PARAMETERS: p_zmdid TYPE zmdid,",
                    "            p_idoc AS CHECKBOX,",
                    "            p_alv RADIOBUTTON GROUP rad1 DEFAULT 'X',",
                    "            p_file RADIOBUTTON GROUP rad1.",
                    "SELECT-OPTIONS: s_id01 FOR zmd_mpe0001-identifier,",
                    "                s_id06 FOR zmd_mpe0006-identifier,",
                    "                s_credat FOR edidc-credat,",
                    "                s_mestyp FOR edidc-mestyp,",
                    "                s_status FOR edidc-status.",
                ]
            )
            chunk_outputs = {
                "declarations": declaration_chunk,
                "database_read_forms": "FORM read_data.\nENDFORM.",
                "processing_form": "FORM process_data.\nENDFORM.",
                "output_forms": "FORM output_data.\nENDFORM.",
                "main_program_flow": "START-OF-SELECTION.",
            }

            def generator(prompt_text, _source_text):
                if "Extract declaration requirements" in prompt_text:
                    return {"text": json.dumps({"report_name": "ztest"}), "model": "test-model", "usage": None}
                if "Extract business-processing logic" in prompt_text:
                    return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}
                for chunk_name, text in chunk_outputs.items():
                    if f"- Chunk: {chunk_name}" in prompt_text:
                        return {"text": text, "model": "test-model", "usage": None}
                return {"text": "", "model": "test-model", "usage": None}

            with patch("services.create_abap.generate_abap", side_effect=generator):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path)

            diagnostics = json.loads(
                (jobs_folder / job_id / "post_generation_processing.json").read_text(encoding="utf-8")
            )
            stages = {stage["stage"]: stage["source"] for stage in diagnostics["stages"]}
            self.assertIn(declaration_chunk, stages["raw_declarations_llm_response"])
            self.assertIn(declaration_chunk, stages["declarations_after_extraction_or_parsing"])
            self.assertTrue(stages["complete_source_immediately_after_assembly"].startswith(declaration_chunk))
            self.assertTrue(stages["complete_source_immediately_before_deterministic_fixer"].startswith(declaration_chunk))
            self.assertTrue(stages["complete_source_immediately_after_deterministic_fixer"].startswith(declaration_chunk))
            self.assertTrue(stages["complete_source_immediately_before_final_save"].startswith(declaration_chunk))
            self.assertEqual(
                (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8"),
                stages["complete_source_immediately_before_final_save"],
            )
            app = create_app({"JOBS_FOLDER": str(jobs_folder), "UPLOAD_FOLDER": str(uploads_folder)})
            response = app.test_client().get(f"/result/{job_id}")
            self.assertEqual(response.status_code, 200)
            diagnostics = json.loads(
                (jobs_folder / job_id / "post_generation_processing.json").read_text(encoding="utf-8")
            )
            stages = {stage["stage"]: stage["source"] for stage in diagnostics["stages"]}
            self.assertEqual(
                stages["complete_source_actually_displayed_on_result_page"],
                (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8"),
            )
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_final_pipeline_reinserts_required_tables_after_fixer_drops_them(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a report for ZMD MPE IDoc errors.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            declaration_requirements = {
                "report_name": "ztest",
                "parameters": [],
                "select_options": [{"name": "s_docnum", "for_field": "EDIDC-DOCNUM"}],
                "tables_declarations": ["ZMD_MPE0001", "ZMD_MPE0006", "EDIDC"],
                "output_structure_fields": [],
            }
            chunk_outputs = {
                "declarations": "\n".join(
                    [
                        "REPORT ztest.",
                        "TABLES: zmd_mpe0001,",
                        "        zmd_mpe0006,",
                        "        edidc.",
                        "SELECT-OPTIONS s_docnum FOR edidc-docnum.",
                    ]
                ),
                "database_read_forms": "FORM read_data.\nENDFORM.",
                "processing_form": "FORM process_data.\nENDFORM.",
                "output_forms": "FORM output_data.\nENDFORM.",
                "main_program_flow": "START-OF-SELECTION.",
            }
            fixed_without_tables = "\n".join(
                [
                    "REPORT z_report    LINE-SIZE 132",
                    "                   LINE-COUNT 65",
                    "                   MESSAGE-ID 38",
                    "                   NO STANDARD PAGE HEADING.",
                    "* Table declarations",
                    "",
                    "* Types",
                    "TYPES ty_output TYPE c.",
                    "SELECT-OPTIONS s_docnum FOR edidc-docnum.",
                ]
            )

            def generator(prompt_text, _source_text):
                if "Extract declaration requirements" in prompt_text:
                    return {"text": json.dumps(declaration_requirements), "model": "test-model", "usage": None}
                if "Extract business-processing logic" in prompt_text:
                    return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}
                for chunk_name, text in chunk_outputs.items():
                    if f"- Chunk: {chunk_name}" in prompt_text:
                        return {"text": text, "model": "test-model", "usage": None}
                return {"text": "", "model": "test-model", "usage": None}

            with patch("services.create_abap.generate_abap", side_effect=generator), patch(
                "services.create_abap.auto_fix_abap",
                return_value={"fixed_source": fixed_without_tables, "fixes": [], "final_issues": []},
            ):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path)

            fixed = (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8")
            self.assertIn("*  Report      : z_report", fixed)
            self.assertLess(fixed.index("NO STANDARD PAGE HEADING."), fixed.index("*  Report      : z_report"))
            self.assertLess(fixed.index("*  Revision History"), fixed.index("TABLES zmd_mpe0001."))
            self.assertIn("TABLES zmd_mpe0001.", fixed)
            self.assertIn("TABLES zmd_mpe0006.", fixed)
            self.assertIn("TABLES edidc.", fixed)
            self.assertLess(fixed.index("NO STANDARD PAGE HEADING."), fixed.index("TABLES zmd_mpe0001."))
            self.assertLess(fixed.index("TABLES zmd_mpe0001."), fixed.index("TABLES zmd_mpe0006."))
            self.assertLess(fixed.index("TABLES zmd_mpe0006."), fixed.index("TABLES edidc."))
            self.assertLess(fixed.index("TABLES edidc."), fixed.index("* Types"))
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_production_pipeline_preserves_selection_screen_chained_declarations(self):
        declaration_block = "\n".join(
            [
                "SELECT-OPTIONS: s_id01 FOR zmd_mpe0001-identifier,",
                "                s_id06 FOR zmd_mpe0006-identifier,",
                "                s_credat FOR edidc-credat,",
                "                s_mestyp FOR edidc-mestyp,",
                "                s_status FOR edidc-status.",
                "",
                "PARAMETERS: p_zmdid TYPE zmdid,",
                "            p_idoc AS CHECKBOX,",
                "            p_alv RADIOBUTTON GROUP rad1 DEFAULT 'X',",
                "            p_file RADIOBUTTON GROUP rad1.",
            ]
        )

        final_source = self.run_pipeline_with_declaration_block(declaration_block)

        self.assertIn(declaration_block, final_source)
        self.assertLess(final_source.index("SELECT-OPTIONS: s_id01"), final_source.index("PARAMETERS: p_zmdid"))
        self.assertLess(final_source.index("SELECT-OPTIONS: s_id01"), final_source.index("                s_id06"))
        self.assertLess(final_source.index("PARAMETERS: p_zmdid"), final_source.index("            p_idoc"))

    def test_production_pipeline_preserves_parameters_then_select_options_chained_declarations(self):
        declaration_block = "\n".join(
            [
                "PARAMETERS: p_zmdid TYPE zmdid,",
                "            p_idoc AS CHECKBOX,",
                "            p_alv  RADIOBUTTON GROUP rad1 DEFAULT 'X',",
                "            p_file RADIOBUTTON GROUP rad1.",
                "",
                "SELECT-OPTIONS: s_id01   FOR zmd_mpe0001-identifier,",
                "                s_id06   FOR zmd_mpe0006-identifier,",
                "                s_credat FOR edidc-credat,",
                "                s_mestyp FOR edidc-mestyp,",
                "                s_status FOR edidc-status.",
            ]
        )

        final_source = self.run_pipeline_with_declaration_block(declaration_block)

        self.assertIn(declaration_block, final_source)
        self.assertLess(final_source.index("PARAMETERS: p_zmdid"), final_source.index("SELECT-OPTIONS: s_id01"))
        self.assertLess(final_source.index("PARAMETERS: p_zmdid"), final_source.index("            p_idoc"))
        self.assertLess(final_source.index("SELECT-OPTIONS: s_id01"), final_source.index("                s_id06"))

    def test_production_pipeline_preserves_generic_chained_declarations(self):
        blocks = {
            "DATA": "\n".join(
                [
                    "DATA: count TYPE i,",
                    "      message TYPE c LENGTH 40.",
                ]
            ),
            "TYPES": "\n".join(
                [
                    "TYPES: ty_count TYPE i,",
                    "       ty_message TYPE c LENGTH 40.",
                ]
            ),
            "CONSTANTS": "\n".join(
                [
                    "CONSTANTS: c_active TYPE c VALUE 'X',",
                    "           c_inactive TYPE c VALUE space.",
                ]
            ),
            "TABLES": "\n".join(
                [
                    "TABLES: edidc,",
                    "        edids.",
                ]
            ),
        }

        for declaration_type, declaration_block in blocks.items():
            with self.subTest(declaration_type=declaration_type):
                final_source = self.run_pipeline_with_declaration_block(declaration_block)

                self.assertIn(declaration_block, final_source)
                self.assertLess(final_source.index(declaration_block.splitlines()[0]), final_source.index(declaration_block.splitlines()[1]))

    def test_upload_redirects_while_llm_call_is_active(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text(
                "Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}",
                encoding="utf-8",
            )
            llm_started = Event()
            release_llm = Event()

            def blocked_generate(prompt_text, source_text):
                llm_started.set()
                self.assertTrue(release_llm.wait(5))
                return {"text": "REPORT zphase2.", "model": "test-model", "usage": None}

            with patch(
                "services.create_abap.generate_abap",
                side_effect=blocked_generate,
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()

                upload = client.post(
                    "/upload",
                    data={"abap_file": (BytesIO(b"Create a test report."), "request.txt")},
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )
                self.assertEqual(upload.status_code, 302)
                self.assertTrue(upload.headers["Location"].startswith("/progress/"))
                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                self.assertTrue(llm_started.wait(2))

                progress = client.get(f"/progress/{job_id}")
                self.assertEqual(progress.status_code, 200)
                self.assertIn(b"Extracting declaration requirements", progress.data)
                self.assertNotIn(b"Calling LLM", progress.data)
                self.assertIn(b"pollProgress", progress.data)
                status = client.get(f"/progress/{job_id}/status")
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.get_json()["current_stage"], "Extracting declaration requirements")

                release_llm.set()
                completed = wait_for_status(jobs_folder, job_id, "Complete")
                self.assertEqual(completed["progress_percent"], 100)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_checked_upload_runs_sap_syntax_check_after_repairs(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            syntax_checker = RecordingSyntaxChecker(
                [
                    {
                        "requested": True,
                        "status": "failed",
                        "passed": False,
                        "errors": [
                            {
                                "line": 1,
                                "column": None,
                                "severity": "E",
                                "message": "Syntax issue from SAP.",
                                "word": "REPORT",
                                "source_line": "REPORT zphase2.",
                            }
                        ],
                        "raw_response": "<sap>first</sap>",
                        "technical_message": "",
                    },
                    {
                        "requested": True,
                        "status": "passed",
                        "passed": True,
                        "errors": [],
                        "raw_response": "<sap>second</sap>",
                        "technical_message": "",
                    },
                ]
            )
            repairer = RecordingCodeReviewRepairer("REPORT zphase2_fixed.")

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": "REPORT zphase2.", "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                        "SAP_SYNTAX_CHECKER": syntax_checker,
                        "CODE_REVIEW_REPAIRER": repairer,
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/upload",
                    data={
                        "abap_file": (BytesIO(b"Create a test report."), "request.txt"),
                        "run_sap_syntax_check": "1",
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Complete")
                self.assertEqual(syntax_checker.sources, ["REPORT zphase2.", "REPORT zphase2_fixed."])
                self.assertEqual(repairer.sources, ["REPORT zphase2."])
                self.assertIn(
                    (
                        "You are repairing an existing ABAP program after SAP syntax validation.\n\n"
                        "Correct only the reported SAP syntax errors.\n"
                        "Make the smallest possible changes.\n"
                        "Do not rewrite unrelated code.\n"
                        "Preserve the existing logic, declarations, formatting and comments.\n"
                        "Return the complete corrected ABAP source only. Do not use Markdown fences."
                    ),
                    repairer.prompts[0],
                )
                self.assertIn(
                    (
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
                        "Apply this rule generically to any internal table, not only t_edids."
                    ),
                    repairer.prompts[0],
                )
                options = json.loads((jobs_folder / job_id / "options.json").read_text(encoding="utf-8"))
                self.assertTrue(options["run_sap_syntax_check"])
                self.assertEqual(options["sap_syntax_check_attempts"], 2)
                saved = json.loads((jobs_folder / job_id / "sap_syntax_check.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["status"], "passed")
                self.assertTrue(saved["repair_attempted"])
                self.assertEqual(saved["initial_result"]["raw_response"], "<sap>first</sap>")
                self.assertEqual(saved["repair"]["raw_model_response"], "REPORT zphase2_fixed.")
                self.assertEqual(saved["repair"]["repaired_abap"], "REPORT zphase2_fixed.")
                self.assertEqual(saved["final_result"]["raw_response"], "<sap>second</sap>")
                self.assertEqual(
                    (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8"),
                    "REPORT zphase2_fixed.",
                )
                diagnostic = (jobs_folder / job_id / "diagnostic_syntax_repair_flow.txt").read_text(encoding="utf-8")
                self.assertIn("Syntax errors returned by SAP syntax API:", diagnostic)
                self.assertIn('"message": "Syntax issue from SAP."', diagnostic)
                self.assertIn("Complete ABAP source passed to repair LLM:\nREPORT zphase2.", diagnostic)
                self.assertIn("Complete raw response returned by repair LLM:\nREPORT zphase2_fixed.", diagnostic)
                self.assertIn(
                    "Complete final ABAP source sent to final SAP syntax check:\nREPORT zphase2_fixed.",
                    diagnostic,
                )
                self.assertNotIn("Run SAP syntax check checkbox enabled:", diagnostic)
                self.assertNotIn("SAP syntax API called:", diagnostic)
                self.assertNotIn("Exact LLM syntax repair prompt:", diagnostic)
                self.assertNotIn("ABAP immediately after deterministic repairs:", diagnostic)
                self.assertNotIn("Second SAP syntax API called:", diagnostic)
                self.assertNotIn("BAPI_MESSAGE_GETDETAIL fixer ran after LLM repair:", diagnostic)

                result = client.get(f"/result/{job_id}")
                self.assertEqual(result.status_code, 200)
                self.assertIn(b"SAP Syntax Check", result.data)
                self.assertIn(b"Status: passed", result.data)
                self.assertIn(b"No SAP syntax errors found.", result.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_sap_syntax_check_receives_saved_assembled_report(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a test report.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            job_id = create_job(jobs_folder)
            (jobs_folder / job_id / "options.json").write_text(
                json.dumps({"run_sap_syntax_check": True, "sap_syntax_check_attempts": 2}),
                encoding="utf-8",
            )

            class SavedReportSyntaxChecker:
                def __init__(self):
                    self.sources = []
                    self.saved_sources = []

                def check(self, source_code):
                    self.sources.append(source_code)
                    self.saved_sources.append((jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8"))
                    return {
                        "requested": True,
                        "status": "passed",
                        "passed": True,
                        "errors": [],
                        "raw_response": "<sap>passed</sap>",
                        "technical_message": "",
                    }

            syntax_checker = SavedReportSyntaxChecker()
            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": "REPORT zassembled.", "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    sap_syntax_checker=syntax_checker,
                )

            self.assertEqual(syntax_checker.sources, ["REPORT zassembled."])
            self.assertEqual(syntax_checker.saved_sources, ["REPORT zassembled."])
            self.assertEqual(
                (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8"),
                "REPORT zassembled.",
            )
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_sap_syntax_repair_rebuilds_report_with_only_repaired_form(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            generated_source = "\n".join(
                [
                    "REPORT zforms.",
                    "FORM first_form.",
                    "  WRITE: / 'unchanged'.",
                    "ENDFORM.",
                    "FORM second_form.",
                    "  WRITE bad.",
                    "ENDFORM.",
                ]
            )
            repaired_form = "\n".join(
                [
                    "FORM second_form.",
                    "  WRITE: / 'fixed'.",
                    "ENDFORM.",
                ]
            )
            affected_form = "\n".join(
                [
                    "FORM second_form.",
                    "  WRITE bad.",
                    "ENDFORM.",
                ]
            )
            repaired_source = "\n".join(
                [
                    "REPORT zforms.",
                    "FORM first_form.",
                    "  WRITE: / 'unchanged'.",
                    "ENDFORM.",
                    "FORM second_form.",
                    "  WRITE: / 'fixed'.",
                    "ENDFORM.",
                ]
            )
            syntax_checker = RecordingSyntaxChecker(
                [
                    {
                        "requested": True,
                        "status": "failed",
                        "passed": False,
                        "errors": [
                            {
                                "line": 6,
                                "column": None,
                                "severity": "E",
                                "message": "Syntax issue inside second FORM.",
                                "word": "BAD",
                                "source_line": "  WRITE bad.",
                            }
                        ],
                        "raw_response": "<sap>first</sap>",
                        "technical_message": "",
                    },
                    {
                        "requested": True,
                        "status": "passed",
                        "passed": True,
                        "errors": [],
                        "raw_response": "<sap>second</sap>",
                        "technical_message": "",
                    },
                ]
            )
            repairer = RecordingCodeReviewRepairer(repaired_form)

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": generated_source, "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                        "SAP_SYNTAX_CHECKER": syntax_checker,
                        "CODE_REVIEW_REPAIRER": repairer,
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/upload",
                    data={
                        "abap_file": (BytesIO(b"Create a test report."), "request.txt"),
                        "run_sap_syntax_check": "1",
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Complete")
                self.assertEqual(repairer.sources, [affected_form])
                self.assertEqual(syntax_checker.sources, [generated_source, repaired_source])
                self.assertIn("FORM first_form.\n  WRITE: / 'unchanged'.\nENDFORM.", syntax_checker.sources[1])
                self.assertNotIn("WRITE bad.", syntax_checker.sources[1])
                self.assertEqual(
                    (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8"),
                    repaired_source,
                )
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_sap_syntax_repair_rejects_standalone_end_in_repaired_form(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            generated_source = "\n".join(
                [
                    "REPORT zforms.",
                    "FORM first_form.",
                    "  WRITE: / 'unchanged'.",
                    "ENDFORM.",
                    "FORM second_form.",
                    "  WRITE bad.",
                    "ENDFORM.",
                ]
            )
            syntax_checker = RecordingSyntaxChecker(
                [
                    {
                        "requested": True,
                        "status": "failed",
                        "passed": False,
                        "errors": [
                            {
                                "line": 6,
                                "column": None,
                                "severity": "E",
                                "message": "Syntax issue inside second FORM.",
                                "word": "BAD",
                                "source_line": "  WRITE bad.",
                            }
                        ],
                        "raw_response": "<sap>first</sap>",
                        "technical_message": "",
                    }
                ]
            )
            repairer = RecordingCodeReviewRepairer("FORM second_form.\n  WRITE: / 'fixed'.\nEND.")

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": generated_source, "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                        "SAP_SYNTAX_CHECKER": syntax_checker,
                        "CODE_REVIEW_REPAIRER": repairer,
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/upload",
                    data={
                        "abap_file": (BytesIO(b"Create a test report."), "request.txt"),
                        "run_sap_syntax_check": "1",
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                progress = wait_for_status(jobs_folder, job_id, "Error")
                self.assertEqual(len(syntax_checker.sources), 1)
                self.assertIn("Syntax repair returned standalone END. in a FORM repair.", progress["stage_message"])
                diagnostic = (jobs_folder / job_id / "diagnostic_syntax_repair_flow.txt").read_text(encoding="utf-8")
                self.assertIn("Complete raw response returned by repair LLM:\nFORM second_form.", diagnostic)
                self.assertIn("\nEND.", diagnostic)
                self.assertIn(
                    "Complete final ABAP source sent to final SAP syntax check:\n" + generated_source,
                    diagnostic,
                )
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_checked_upload_can_run_three_sap_syntax_check_attempts(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            syntax_checker = RecordingSyntaxChecker(
                [
                    syntax_failure("First SAP error.", "REPORT zphase2."),
                    syntax_failure("Second SAP error.", "REPORT zphase2_fixed1."),
                    {
                        "requested": True,
                        "status": "passed",
                        "passed": True,
                        "errors": [],
                        "raw_response": "<sap>third</sap>",
                        "technical_message": "",
                    },
                ]
            )
            repairer = SequenceCodeReviewRepairer(["REPORT zphase2_fixed1.", "REPORT zphase2_fixed2."])

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": "REPORT zphase2.", "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                        "SAP_SYNTAX_CHECKER": syntax_checker,
                        "CODE_REVIEW_REPAIRER": repairer,
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/upload",
                    data={
                        "abap_file": (BytesIO(b"Create a test report."), "request.txt"),
                        "run_sap_syntax_check": "1",
                        "sap_syntax_check_attempts": "3",
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Complete")
                self.assertEqual(syntax_checker.sources, ["REPORT zphase2.", "REPORT zphase2_fixed1.", "REPORT zphase2_fixed2."])
                self.assertEqual(repairer.sources, ["REPORT zphase2.", "REPORT zphase2_fixed1."])
                saved = json.loads((jobs_folder / job_id / "sap_syntax_check.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["status"], "passed")
                self.assertEqual(saved["syntax_check_attempts"], 3)
                self.assertEqual(len(saved["repairs"]), 2)
                self.assertEqual(
                    (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8"),
                    "REPORT zphase2_fixed2.",
                )
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_sap_syntax_errors_remaining_after_one_repair_stop_report_return(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            syntax_checker = RecordingSyntaxChecker(
                [
                    syntax_failure("First SAP error.", "REPORT zphase2."),
                    syntax_failure("Second SAP error.", "REPORT zphase2_fixed."),
                ]
            )

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": "REPORT zphase2.", "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                        "SAP_SYNTAX_CHECKER": syntax_checker,
                        "CODE_REVIEW_REPAIRER": RecordingCodeReviewRepairer("REPORT zphase2_fixed."),
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/upload",
                    data={
                        "abap_file": (BytesIO(b"Create a test report."), "request.txt"),
                        "run_sap_syntax_check": "1",
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                progress = wait_for_status(jobs_folder, job_id, "Error")
                self.assertEqual(len(syntax_checker.sources), 2)
                saved = json.loads((jobs_folder / job_id / "sap_syntax_check.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["errors"][0]["message"], "Second SAP error.")
                self.assertIn("Final SAP syntax check did not succeed (failed): Second SAP error.", progress["stage_message"])
                diagnostic = (jobs_folder / job_id / "diagnostic_syntax_repair_flow.txt").read_text(encoding="utf-8")
                self.assertIn("Syntax errors returned by SAP syntax API:", diagnostic)
                self.assertIn('"message": "First SAP error."', diagnostic)
                self.assertIn("Complete ABAP source passed to repair LLM:\nREPORT zphase2.", diagnostic)
                self.assertIn("Complete raw response returned by repair LLM:\nREPORT zphase2_fixed.", diagnostic)
                self.assertNotIn("Second SAP syntax API called:", diagnostic)
                self.assertNotIn('"message": "Second SAP error."', diagnostic)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_unchecked_upload_does_not_run_sap_syntax_check(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            syntax_checker = RecordingSyntaxChecker([{"status": "passed", "errors": []}])

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": "REPORT zphase2.", "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                        "SAP_SYNTAX_CHECKER": syntax_checker,
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/upload",
                    data={"abap_file": (BytesIO(b"Create a test report."), "request.txt")},
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Complete")
                self.assertEqual(syntax_checker.sources, [])
                self.assertFalse((jobs_folder / job_id / "sap_syntax_check.json").exists())
                options = json.loads((jobs_folder / job_id / "options.json").read_text(encoding="utf-8"))
                self.assertFalse(options["run_sap_syntax_check"])
                self.assertEqual(options["sap_syntax_check_attempts"], 2)
                diagnostic = (jobs_folder / job_id / "diagnostic_syntax_repair_flow.txt").read_text(encoding="utf-8")
                self.assertIn("Syntax errors returned by SAP syntax API:\n[]", diagnostic)
                self.assertIn("Complete ABAP source passed to repair LLM:\nNone", diagnostic)
                self.assertIn("Complete raw response returned by repair LLM:\nNone", diagnostic)
                self.assertNotIn("Run SAP syntax check checkbox enabled:", diagnostic)
                self.assertNotIn("SAP syntax API called:", diagnostic)
                self.assertNotIn("LLM repair step invoked:", diagnostic)
                self.assertNotIn("LLM repair skip reason:", diagnostic)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_sap_syntax_check_attempts_are_capped_at_ten(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": "REPORT zphase2.", "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/upload",
                    data={
                        "abap_file": (BytesIO(b"Create a test report."), "request.txt"),
                        "sap_syntax_check_attempts": "99",
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Complete")
                options = json.loads((jobs_folder / job_id / "options.json").read_text(encoding="utf-8"))
                self.assertEqual(options["sap_syntax_check_attempts"], 10)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_create_abap_error_redirects_to_progress(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_error_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")

            with patch(
                "services.create_abap.generate_abap",
                side_effect=RuntimeError("Connection error."),
            ):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()

                upload = client.post(
                    "/upload",
                    data={"abap_file": (BytesIO(b"Create a test report."), "request.txt")},
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )
                self.assertEqual(upload.status_code, 302)

                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Error")
                progress = client.get(f"/progress/{job_id}")
                self.assertEqual(progress.status_code, 200)
                self.assertIn(b"Error", progress.data)
                self.assertIn(b"Connection error.", progress.data)
                saved_progress = get_progress(jobs_folder, job_id)
                self.assertEqual(saved_progress["status"], "Error")
                self.assertEqual(saved_progress["current_stage"], "Error")
                self.assertFalse(saved_progress["is_active"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_upload_ignores_user_entered_callable_metadata(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            generated_abap = "\n".join(
                [
                    "CALL FUNCTION 'Z_TEST_FUNCTION'",
                    "  TABLES",
                    "    MESSAGE = old_target.",
                ]
            )

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": generated_abap, "model": "test-model", "usage": None},
            ):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/upload",
                    data={
                        "abap_file": (BytesIO(b"Create a test report."), "request.txt"),
                        "callable_metadata": json.dumps(callable_metadata()),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Complete")
                fixed = (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8")
                self.assertIn("  TABLES", fixed)
                self.assertIn("    MESSAGE = old_target.", fixed)
                self.assertNotIn("source_structure-field1", fixed)
                self.assertNotIn("target_variable", fixed)
                issues = json.loads((jobs_folder / job_id / "validation_issues.json").read_text(encoding="utf-8"))
                self.assertFalse([issue for issue in issues if issue["rule_id"].startswith("CALLABLE_")])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_internal_callable_metadata_is_used_by_create_flow(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a test report.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            job_id = create_job(jobs_folder)
            generated_abap = "\n".join(
                [
                    "CALL FUNCTION 'Z_TEST_FUNCTION'",
                    "  TABLES",
                    "    MESSAGE = old_target.",
                ]
            )

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": generated_abap, "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path, callable_metadata=callable_metadata())

            fixed = (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8")
            self.assertIn("    ID = source_structure-field1", fixed)
            self.assertIn("    MESSAGE = target_variable.", fixed)
            self.assertNotIn("TABLES", fixed)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_dependency_callables_are_looked_up_before_generation_and_rendered(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a report using a SAP function and method.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            provider = SelectiveSignatureProvider()

            def dependency_analyzer(_prompt, _source):
                return {
                    "text": json.dumps(
                        {
                            "ddic_objects": [],
                            "callables": ["Z_DEP_FUNCTION", "ZCL_DEP=>RUN", "Z_MISSING_FUNCTION"],
                            "local_identifiers": [],
                            "declarations": [],
                            "operations": [],
                            "form_names": [],
                            "execution_order": [],
                            "unresolved": [],
                        }
                    )
                }

            def generate_with_signature_catalogue(prompt_text, source_text):
                if "Extract declaration requirements" in prompt_text:
                    return {"text": json.dumps({"report_name": "ztest"}), "model": "test-model", "usage": None}
                if "Extract business-processing logic" in prompt_text:
                    return {
                        "text": json.dumps(
                            {
                                "processing_steps": [
                                    {"step": 1, "operation": "CALL_FUNCTION", "name": "Z_DEP_FUNCTION"},
                                    {"step": 2, "operation": "CALL_FUNCTION", "name": "ZCL_DEP=>RUN"},
                                ]
                            }
                        ),
                        "model": "test-model",
                        "usage": None,
                    }
                if "Chunk: processing_form" in prompt_text:
                    self.assertIn("SAP callable signature catalogue:", prompt_text)
                    self.assertIn("Z_DEP_FUNCTION", prompt_text)
                    self.assertIn("IV_INPUT [IMPORTING STRING]", prompt_text)
                    self.assertIn("ZCL_DEP=>RUN", prompt_text)
                    self.assertNotIn("Z_POST_FUNCTION", prompt_text)
                return {"text": "REPORT ztest.\nCALL FUNCTION 'Z_POST_FUNCTION'.", "model": "test-model", "usage": None}

            with patch(
                "services.create_abap.generate_abap",
                side_effect=generate_with_signature_catalogue,
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    signature_provider=provider,
                    dependency_analyzer=dependency_analyzer,
                )

            self.assertEqual(provider.requests, [["Z_DEP_FUNCTION", "ZCL_DEP=>RUN", "Z_MISSING_FUNCTION"]])
            analysis = json.loads((jobs_folder / job_id / "dependency_analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(
                analysis["callable_signatures_included_in_prompt"],
                ["ZCL_DEP=>RUN", "Z_DEP_FUNCTION"],
            )
            self.assertEqual(
                analysis["unresolved_callable_signatures"],
                [{"identity": "Z_MISSING_FUNCTION", "reason": "signature not retrieved"}],
            )

            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            result = app.test_client().get(f"/result/{job_id}")
            self.assertEqual(result.status_code, 200)
            self.assertIn(b"Callable signatures included in generation prompt", result.data)
            self.assertIn(b"Z_DEP_FUNCTION", result.data)
            self.assertIn(b"ZCL_DEP=&gt;RUN", result.data)
            self.assertIn(b"Unresolved callable signatures", result.data)
            self.assertIn(b"Z_MISSING_FUNCTION", result.data)
            self.assertIn(b"signature not retrieved", result.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_specification_referenced_function_modules_and_methods_are_added_before_metadata_lookup(self):
        source_text = "\n".join(
            [
                "Use function module Z_FIRST_STAGE.",
                "If P_SECOND EQ 'X'.",
                "  DATA lo_worker TYPE REF TO zcl_second_stage.",
                "  Call function Z_SECOND_STAGE.",
                "  Call lo_worker->execute.",
                "  Also call zcl_static_stage=>run.",
            ]
        )
        dependency_analysis = {
            "ddic_objects": [],
            "callables": ["Z_FIRST_STAGE"],
            "unresolved": ["Z_SECOND_STAGE"],
        }

        merge_specification_callable_dependencies(dependency_analysis, source_text)

        self.assertEqual(
            ["Z_FIRST_STAGE", "Z_SECOND_STAGE", "ZCL_SECOND_STAGE=>EXECUTE", "ZCL_STATIC_STAGE=>RUN"],
            dependency_analysis["callables"],
        )
        self.assertEqual(
            ["Z_FIRST_STAGE", "Z_SECOND_STAGE", "ZCL_SECOND_STAGE=>EXECUTE", "ZCL_STATIC_STAGE=>RUN"],
            extract_specification_callable_identities(source_text),
        )
        self.assertEqual(
            ["Z_SECOND_STAGE", "ZCL_SECOND_STAGE=>EXECUTE", "ZCL_STATIC_STAGE=>RUN"],
            dependency_analysis["specification_callables_added"],
        )
        self.assertEqual([], dependency_analysis["unresolved"])

    def test_noop_provider_skips_callable_validation_without_failure(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a test report.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            job_id = create_job(jobs_folder)
            generated_abap = "\n".join(["CALL FUNCTION 'Z_TEST_FUNCTION'", "  TABLES", "    MESSAGE = text."])

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": generated_abap, "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    signature_provider=NoOpCallableSignatureProvider(),
                )

            progress = get_progress(jobs_folder, job_id)
            self.assertEqual(progress["status"], "Complete")
            issues = json.loads((jobs_folder / job_id / "validation_issues.json").read_text(encoding="utf-8"))
            self.assertFalse([issue for issue in issues if issue["rule_id"].startswith("CALLABLE_")])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_ddic_metadata_is_retrieved_before_llm_and_added_to_internal_prompt(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a report using SAP table EDIDC.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            provider = StaticDdicProvider(ddic_metadata())
            prompts = []

            def generate_with_catalogue(prompt_text, source_text):
                prompts.append(prompt_text)
                self.assertEqual(provider.requested, ["EDIDC"])
                if "Extract declaration requirements" in prompt_text:
                    return {"text": json.dumps({"report_name": "ztest"}), "model": "test-model", "usage": None}
                if "Extract business-processing logic" in prompt_text:
                    return {
                        "text": json.dumps(
                            {
                                "processing_steps": [
                                    {"step": 1, "operation": "LOOP", "source": "t_edidc"},
                                ]
                            }
                        ),
                        "model": "test-model",
                        "usage": None,
                    }
                if "Chunk: declarations" in prompt_text:
                    return {"text": "REPORT ztest.", "model": "test-model", "usage": None}
                if "Chunk: database_read_forms" in prompt_text:
                    return {"text": "FORM read_edidc.\nENDFORM.", "model": "test-model", "usage": None}
                if "Chunk: processing_form" in prompt_text:
                    return {"text": "FORM process_data.\n  LOOP AT t_edidc INTO st_edidc.\n  ENDLOOP.\nENDFORM.", "model": "test-model", "usage": None}
                if "Chunk: output_forms" in prompt_text:
                    return {"text": "FORM output_data.\nENDFORM.", "model": "test-model", "usage": None}
                if "Chunk: main_program_flow" in prompt_text:
                    return {"text": "PERFORM read_edidc.\nPERFORM process_data.", "model": "test-model", "usage": None}
                return {
                    "text": "REPORT ztest.\nDATA w_message_type TYPE edidc-mestyp.",
                    "model": "test-model",
                    "usage": None,
                }

            with patch(
                "services.create_abap.generate_abap",
                side_effect=generate_with_catalogue,
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    ddic_metadata_provider=provider,
                )

            progress = get_progress(jobs_folder, job_id)
            self.assertEqual(progress["status"], "Complete")
            prompts_by_chunk = prompts_by_chunk_name(prompts)
            self.assertIn("SAP DDIC metadata catalogue:", prompts_by_chunk["declarations"])
            self.assertIn("SAP DDIC metadata catalogue:", prompts_by_chunk["database_read_forms"])
            self.assertIn("EDIDC:", prompts_by_chunk["database_read_forms"])
            self.assertIn("EDIDC: DOCNUM", prompts_by_chunk["database_read_forms"])
            self.assertNotIn("MESTYP", prompts_by_chunk["database_read_forms"])
            self.assertIn("SAP DDIC metadata catalogue:", prompts_by_chunk["processing_form"])
            self.assertIn("EDIDC: no fields selected", prompts_by_chunk["processing_form"])
            self.assertNotIn("DOCNUM", prompts_by_chunk["processing_form"])
            self.assertNotIn("MESTYP", prompts_by_chunk["processing_form"])
            self.assertNotIn("SAP DDIC metadata catalogue:", prompts_by_chunk["output_forms"])
            self.assertNotIn("SAP DDIC metadata catalogue:", prompts_by_chunk["main_program_flow"])
            saved_metadata = json.loads((jobs_folder / job_id / "ddic_metadata.json").read_text(encoding="utf-8"))
            self.assertIn("EDIDC", saved_metadata["tables"])
            self.assertEqual(provider.requests, [["EDIDC"]])
            issues = json.loads((jobs_folder / job_id / "validation_issues.json").read_text(encoding="utf-8"))
            self.assertFalse([issue for issue in issues if issue["rule_id"] == "ABAP_UNVERIFIED_DDIC_IDENTIFIER"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_ddic_metadata_progress_messages_are_visible_before_generation(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a report using SAP table EDIDC.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            provider = ProgressDdicProvider(ddic_metadata())

            def generate_after_ddic_progress(_prompt_text, _source_text):
                progress = get_progress(jobs_folder, job_id)
                self.assertIn("Calling SAP to get DDIC metadata", progress["activity_messages"])
                self.assertIn("Called SAP to get DDIC metadata", progress["activity_messages"])
                return {"text": "REPORT ztest.", "model": "test-model", "usage": None}

            with patch(
                "services.create_abap.generate_abap",
                side_effect=generate_after_ddic_progress,
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path, ddic_metadata_provider=provider)

            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            progress_page = app.test_client().get(f"/progress/{job_id}")
            self.assertIn(b"Calling SAP to get DDIC metadata", progress_page.data)
            self.assertIn(b"Called SAP to get DDIC metadata", progress_page.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_application_continues_when_sap_unavailable_and_metadata_is_cached(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text(
                "\n".join(
                    [
                        "Create a report using SAP table PA0000.",
                        "# START PROCESSING RULES",
                        "Move PA0000-PERNR to the output row.",
                        "# END PROCESSING RULES",
                    ]
                ),
                encoding="utf-8",
            )
            prompt_path.write_text("Generate ABAP.\n{{SPECIFICATION}}", encoding="utf-8")
            cache_dir = temp_path / "cache" / "ddic"
            cache_dir.mkdir(parents=True)
            (cache_dir / "PA0000.json").write_text(
                json.dumps(ddic_table("PA0000", ["PERNR"])),
                encoding="utf-8",
            )
            provider = ModeAwareDdicMetadataProvider(
                LocalDdicMetadataCache(cache_dir),
                sap_provider=FailingDdicProvider(),
                mode="sap_first",
            )
            job_id = create_job(jobs_folder)

            def generator(prompt_text, _source_text, response_format=None):
                if "Extract declaration requirements" in prompt_text:
                    return {
                        "text": json.dumps(
                            {
                                "report_name": "ztest",
                                "output_structure_fields": [
                                    {"name": "PERNR", "type_or_like": "TYPE PA0000-PERNR"}
                                ],
                            }
                        ),
                        "model": "test-model",
                        "usage": None,
                    }
                if "Extract business-processing logic" in prompt_text:
                    return {
                        "text": json.dumps(
                            {
                                "processing_steps": [
                                    {
                                        "step": 1,
                                        "operation": "MOVE",
                                        "source": "PA0000-PERNR",
                                        "target": "W_OUTPUT-PERNR",
                                    }
                                ]
                            }
                        ),
                        "model": "test-model",
                        "usage": None,
                    }
                return {"text": "REPORT ztest.", "model": "test-model", "usage": None}

            with patch("services.create_abap.generate_abap", side_effect=generator):
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    ddic_metadata_provider=provider,
                    processing_plan_review_required=True,
                )

            progress = get_progress(jobs_folder, job_id)
            self.assertEqual(progress["status"], "Awaiting Review")
            saved_metadata = json.loads((jobs_folder / job_id / "ddic_metadata.json").read_text(encoding="utf-8"))
            self.assertIn("PA0000", saved_metadata["tables"])
            diagnostics = json.loads((jobs_folder / job_id / PROCESSING_PLAN_DIAGNOSTICS_ARTIFACT).read_text(encoding="utf-8"))
            self.assertEqual([], diagnostics["processing_contract_diagnostics"]["validation_errors"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_dependency_analysis_drives_pre_generation_provider_lookups(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            events = []
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text(
                "\n".join(
                    [
                        "Use SAP table EDIDC.",
                        "TYPES st_output TYPE char10.",
                        "Use function module Z_TEST_FUNCTION.",
                    ]
                ),
                encoding="utf-8",
            )
            prompt_path.write_text("Generate ABAP.\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            ddic_provider = StaticDdicProvider(ddic_metadata(), events=events)
            signature_provider = RecordingSignatureProvider(events=events)
            prompts = []
            chunk_sources = []

            def analyzer(prompt, source):
                events.append("analysis")
                self.assertIn("Do not generate any ABAP.", prompt)
                self.assertIn("ST_OUTPUT", source.upper())
                return {
                    "text": json.dumps(
                        {
                            "ddic_objects": [
                                {"name": "EDIDC", "structure": "st_edidc", "table": "t_edidc"},
                            ],
                            "callables": ["Z_TEST_FUNCTION", "CL_TEST_SERVICE=>RUN"],
                            "local_identifiers": ["ST_OUTPUT"],
                            "declarations": [],
                            "operations": [],
                            "form_names": [],
                            "execution_order": [],
                            "unresolved": [],
                        }
                    )
                }

            def generate_with_metadata(prompt_text, source_text):
                prompts.append(prompt_text)
                chunk_sources.append(source_text)
                events.append("generate")
                self.assertLess(events.index("analysis"), events.index("ddic"))
                self.assertLess(events.index("ddic"), events.index("signature"))
                self.assertLess(events.index("signature"), events.index("generate"))
                self.assertNotIn("Generate one complete classical SAP ECC ABAP report from the supplied functional specification.", prompt_text)
                self.assertNotIn("FORM <read_data_form>.", prompt_text)
                if "Extract declaration requirements" in prompt_text:
                    return {"text": json.dumps({"report_name": "ztest"}), "model": "test-model", "usage": None}
                if "Extract business-processing logic" in prompt_text:
                    return {
                        "text": json.dumps(
                            {
                                "processing_steps": [
                                    {"step": 1, "operation": "CALL_FUNCTION", "name": "Z_TEST_FUNCTION"},
                                ]
                            }
                        ),
                        "model": "test-model",
                        "usage": None,
                    }
                return {"text": "REPORT ztest.\nCALL FUNCTION 'Z_TEST_FUNCTION'.", "model": "test-model", "usage": None}

            with patch("services.create_abap.generate_abap", side_effect=generate_with_metadata), patch(
                "services.create_abap.time.perf_counter",
                side_effect=[1.0, 1.1, 2.0, 2.5],
            ):
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    signature_provider=signature_provider,
                    ddic_metadata_provider=ddic_provider,
                    dependency_analyzer=analyzer,
                )

            self.assertEqual(ddic_provider.requests, [["EDIDC"]])
            self.assertEqual(signature_provider.requests[0], ["Z_TEST_FUNCTION", "CL_TEST_SERVICE=>RUN"])
            self.assertEqual(chunk_sources[0], input_path.read_text(encoding="utf-8"))
            self.assertEqual(chunk_sources[1], input_path.read_text(encoding="utf-8"))
            self.assertTrue(all(source != input_path.read_text(encoding="utf-8") for source in chunk_sources[2:]))
            prompts_by_chunk = prompts_by_chunk_name(prompts)
            declarations_prompt = prompts_by_chunk["declarations"]
            database_prompt = prompts_by_chunk["database_read_forms"]
            processing_prompt = prompts_by_chunk["processing_form"]
            output_prompt = prompts_by_chunk["output_forms"]
            main_prompt = prompts_by_chunk["main_program_flow"]
            self.assertIn("SAP DDIC metadata catalogue:", declarations_prompt)
            self.assertNotIn("Functional specification:", declarations_prompt)
            self.assertIn("SAP DDIC metadata catalogue:", database_prompt)
            self.assertIn("Database-read requirements:", database_prompt)
            self.assertIn("Relevant specification excerpts:\n- Use SAP table EDIDC.", database_prompt)
            self.assertIn("Exact internal-table names: t_edidc", database_prompt)
            self.assertIn("Exact work-area names: st_edidc", database_prompt)
            self.assertIn("Exact database FORM names: read_edidc", database_prompt)
            self.assertNotIn("TYPES st_output TYPE char10.", database_prompt)
            self.assertNotIn("Use function module Z_TEST_FUNCTION.", database_prompt)
            self.assertNotIn("SAP callable signature catalogue:", database_prompt)
            self.assertNotIn("Exact output structure fields:", database_prompt)
            self.assertNotIn("process_data", database_prompt)
            self.assertNotIn("read_edid4", database_prompt)
            self.assertNotIn("read_edids", database_prompt)
            self.assertIn("SAP callable signature catalogue:", processing_prompt)
            self.assertIn("Structured processing plan:", processing_prompt)
            self.assertIn('"name": "Z_TEST_FUNCTION"', processing_prompt)
            self.assertNotIn("Relevant specification excerpts:", processing_prompt)
            self.assertNotIn("SAP DDIC metadata catalogue:", processing_prompt)
            self.assertIn("Exact callable identities: Z_TEST_FUNCTION", processing_prompt)
            self.assertNotIn("CL_TEST_SERVICE=>RUN", processing_prompt)
            self.assertIn("Exact processing FORM names: process_data", processing_prompt)
            self.assertNotIn("Exact output structure fields:", output_prompt)
            self.assertIn("Output requirements:", output_prompt)
            self.assertNotIn("SAP DDIC metadata catalogue:", output_prompt)
            self.assertNotIn("Use SAP table EDIDC.", output_prompt)
            self.assertNotIn("SAP callable signature catalogue:", output_prompt)
            self.assertIn("Main-flow requirements:", main_prompt)
            self.assertIn("Exact FORM names: read_edidc, process_data", main_prompt)
            self.assertNotIn("SAP DDIC metadata catalogue:", main_prompt)
            saved_analysis = json.loads((jobs_folder / job_id / "dependency_analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_analysis["ddic_objects"], [{"name": "EDIDC", "structure": "st_edidc", "table": "t_edidc"}])
            self.assertEqual(saved_analysis["pre_generation_callable_identities"], ["Z_TEST_FUNCTION", "CL_TEST_SERVICE=>RUN"])
            self.assertEqual(saved_analysis["final_callable_identities"], ["Z_TEST_FUNCTION", "CL_TEST_SERVICE=>RUN"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_processing_rule_object_methods_are_prefetched_from_signature_provider(self):
        provider = RecordingSignatureProvider()
        rules = "\n".join(
            [
                "Call instance method SEND_REQUEST->SET_DOCUMENT.",
                "Call instance method SEND_REQUEST->ADD_RECIPIENT.",
                "Stores the result returned by SEND_REQUEST->SEND.",
                "Call instance method SEND_REQUEST->SEND.",
            ]
        )

        metadata = enrich_processing_rule_callable_metadata(rules, {"callable_signatures": {}}, provider)

        self.assertEqual(
            [
                "SEND_REQUEST=>SET_DOCUMENT",
                "SEND_REQUEST=>ADD_RECIPIENT",
                "SEND_REQUEST=>SEND",
            ],
            provider.requests[0],
        )
        self.assertEqual(
            provider.requests[0],
            explicit_object_method_identities(rules),
        )
        self.assertEqual(
            set(provider.requests[0]),
            set(normalize_provider_signatures(metadata)),
        )

    def test_processing_plan_review_extraction_uses_dependency_analysis_model_role(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            job_id = create_job(jobs_folder)
            job_folder = jobs_folder / job_id
            calls = []

            def dependency_generator(prompt_text, source_text, response_format=None):
                calls.append((prompt_text, source_text, response_format))
                if "Extract declaration requirements" in prompt_text:
                    return {
                        "text": json.dumps({"output_structure_fields": []}),
                        "model": "gpt-5.6-luna",
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    }
                return {
                    "text": json.dumps({"processing_steps": []}),
                    "model": "gpt-5.6-luna",
                    "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                }

            with patch("services.create_abap.generate_dependency_analysis", side_effect=dependency_generator):
                declaration_requirements, processing_plan = extract_processing_plan_for_review(
                    job_folder,
                    jobs_folder,
                    job_id,
                    "Shared generation contract:\nExact callable identities: None\n",
                    "# Processing Rules\nNo business processing required.",
                    callable_metadata={},
                    ddic_metadata={"tables": {}},
                )

            self.assertEqual("gpt-5.6-luna", declaration_requirements["model"])
            self.assertEqual("gpt-5.6-luna", processing_plan["model"])
            self.assertIn("Extract declaration requirements", calls[0][0])
            self.assertIn("Extract business-processing logic", calls[1][0])
            self.assertIsNotNone(calls[1][2])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_processing_rule_ddic_dependencies_are_merged_before_metadata_lookup(self):
        dependency_analysis = {
            "ddic_objects": [{"name": "EDIDC", "structure": "st_edidc", "table": "t_edidc"}],
            "callables": [],
            "unresolved": ["ZMD_MPE0001"],
        }
        source_text = (
            "# Processing Rules\n\n"
            "START PROCESSING RULES\n"
            "Type: Select-Option\n"
            "Control Type: Radio Button\n"
            "CALL_FUNCTION\n"
            "Process each record in t_edidc.\n"
            "Search ZMD_MPE0001 using ZMD_MPE0001-DOCNUM_IN = EDIDC-DOCNUM.\n"
            "Otherwise search ZMD_MPE0006 using ZMD_MPE0006-DOCNUM_IN = EDIDC-DOCNUM.\n"
            "Reference Field: PA0000-PERNR.\n"
            "Send emails from **[donotreply@booker.co.uk](mailto:donotreply@booker.co.uk)**."
        )

        merge_processing_rule_ddic_dependencies(dependency_analysis, source_text)

        names = [item["name"] for item in dependency_analysis["ddic_objects"]]
        self.assertEqual(["EDIDC", "ZMD_MPE0001", "ZMD_MPE0006", "PA0000"], names)
        self.assertEqual(["ZMD_MPE0001", "ZMD_MPE0006", "PA0000"], dependency_analysis["processing_rule_ddic_objects_added"])
        self.assertNotIn("BOOKER", names)
        self.assertNotIn("SELECT", names)
        self.assertNotIn("RADIO", names)
        self.assertNotIn("CALL", names)
        self.assertNotIn("START", names)
        self.assertNotIn("PROCESSING", names)
        dependency_keys = {
            (item.get("kind"), item.get("name"), item.get("object"), item.get("field"))
            for item in dependency_analysis["processing_rule_dependencies"]
        }
        self.assertIn(("ddic_field", "PA0000-PERNR", "PA0000", "PERNR"), dependency_keys)
        self.assertNotIn("ZMD_MPE0001", dependency_analysis["unresolved"])

    def test_generation_contract_derives_identifiers_from_dependencies_and_spec(self):
        contract = build_generation_contract(
            "Read SAP table VBAK and export the result as CSV.",
            {
                "ddic_objects": [
                    {"name": "VBAK", "structure": "st_vbak", "table": "t_vbak"},
                ],
                "callables": ["BAPI_SALESORDER_GETLIST"],
            },
            {
                "VBAK": {
                    "fields": {
                        "VBELN": {"name": "VBELN"},
                        "AUDAT": {"name": "AUDAT"},
                    }
                }
            },
            {
                "callable_signatures": {
                    "BAPI_SALESORDER_GETLIST": {"parameters": []},
                },
                "_diagnostics": {"unresolved": []},
            },
        )

        self.assertEqual(contract["internal_tables"], ["t_vbak"])
        self.assertEqual(contract["work_areas"], ["st_vbak"])
        self.assertEqual(contract["output_structure_fields"], ["VBELN", "AUDAT"])
        self.assertEqual(contract["callable_identities"], ["BAPI_SALESORDER_GETLIST"])
        self.assertNotIn("callable_signatures", contract["callable_identities"])
        self.assertNotIn("_diagnostics", contract["callable_identities"])
        self.assertEqual(contract["form_names"], ["read_vbak", "process_data", "output_data", "write_csv"])
        self.assertNotIn("read_edidc", contract["form_names"])
        self.assertNotIn("display_alv", contract["form_names"])

    def test_generation_contract_includes_simple_global_form_routine_rule(self):
        prompt = append_generation_contract(
            "Base prompt.",
            {
                "internal_tables": ["t_vbak"],
                "work_areas": ["st_vbak"],
                "output_structure_fields": ["VBELN"],
                "form_names": ["read_vbak", "process_data"],
                "callable_identities": [],
            },
        )

        self.assertIn("- Generate simple classical SAP ECC FORM routines.", prompt)
        self.assertIn("- Use the program's global variables, internal tables and work areas directly.", prompt)
        self.assertIn("- Do not create local declarations inside FORM routines.", prompt)
        self.assertIn(
            "- Do not generate DATA, TYPES, CONSTANTS, FIELD-SYMBOLS, RANGES, or STATICS declarations inside any FORM.",
            prompt,
        )
        self.assertIn(
            "- Every variable required by generated forms must be declared globally by the declarations chunk.",
            prompt,
        )
        self.assertIn(
            "- FORM routines must reuse the exact global names from this shared naming contract.",
            prompt,
        )
        self.assertIn("- Do not invent local names such as lt_*, ls_*, lv_*, wa_*, gt_*, gs_*, or gv_*.", prompt)
        self.assertIn("- Do not generate USING, CHANGING or TABLES parameters for FORM routines.", prompt)
        self.assertIn("- Do not generate USING, CHANGING or TABLES additions on PERFORM statements.", prompt)
        self.assertIn(
            "- Only generate FORM parameters if the functional specification explicitly requires data to be passed between forms.",
            prompt,
        )

    def test_gpt_5_mini_cost_is_calculated_from_aggregated_chunk_usage(self):
        cost = calculate_cost("gpt-5-mini", input_tokens=5000, output_tokens=2500)

        self.assertEqual(cost["input"], 0.00125)
        self.assertEqual(cost["output"], 0.005)
        self.assertEqual(cost["total"], 0.00625)

    def test_gpt_56_terra_cost_is_calculated(self):
        cost = calculate_cost("gpt-5.6-terra", input_tokens=36144, output_tokens=8839)

        self.assertEqual(cost["input"], 0.09036)
        self.assertEqual(cost["output"], 0.132585)
        self.assertEqual(cost["total"], 0.222945)

    def test_existing_metrics_display_backfills_known_model_costs(self):
        metrics = normalize_metrics_for_display(
            {
                "model": "gpt-5.6-terra",
                "input_tokens": 36144,
                "output_tokens": 8839,
                "total_tokens": 44983,
                "estimated_input_cost": None,
                "estimated_output_cost": None,
                "estimated_total_cost": None,
            }
        )

        self.assertEqual(metrics["estimated_input_cost"], 0.09036)
        self.assertEqual(metrics["estimated_output_cost"], 0.132585)
        self.assertEqual(metrics["estimated_total_cost"], 0.222945)

    def test_cost_breakdown_is_built_from_job_artifacts(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            job_folder = temp_path / "jobs" / "job"
            job_folder.mkdir(parents=True)
            (job_folder / "abap_generation_chunks.json").write_text(
                json.dumps(
                    {
                        "declaration_requirements": {
                            "model": "gpt-5.6-luna",
                            "usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                        },
                        "chunks": [
                            {
                                "name": "declarations",
                                "model": "gpt-5.6-terra",
                                "usage": {"input_tokens": 2000, "output_tokens": 200, "total_tokens": 2200},
                            },
                            {
                                "name": "processing_form",
                                "model": "gpt-5.6-sol",
                                "usage": {"input_tokens": 3000, "output_tokens": 300, "total_tokens": 3300},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (job_folder / PROCESSING_PLAN_DIAGNOSTICS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "model": "gpt-5.6-luna",
                        "usage": {"input_tokens": 4000, "output_tokens": 400, "total_tokens": 4400},
                    }
                ),
                encoding="utf-8",
            )

            breakdown = cost_breakdown_from_job_artifacts(job_folder)

            self.assertEqual(
                [row["label"] for row in breakdown["by_stage"]],
                ["Declaration requirements", "Declarations chunk", "Processing form chunk", "Processing plan"],
            )
            totals_by_model = {row["model"]: row for row in breakdown["by_model"]}
            self.assertEqual(totals_by_model["gpt-5.6-luna"]["input_tokens"], 5000)
            self.assertEqual(totals_by_model["gpt-5.6-luna"]["output_tokens"], 500)
            self.assertEqual(totals_by_model["gpt-5.6-luna"]["estimated_total_cost"], 0.008)
            self.assertEqual(totals_by_model["gpt-5.6-terra"]["estimated_total_cost"], 0.008)
            self.assertEqual(totals_by_model["gpt-5.6-sol"]["estimated_total_cost"], 0.024)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_load_metrics_backfills_cost_breakdown_for_existing_job_metrics(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            job_folder = jobs_folder / "job"
            job_folder.mkdir(parents=True)
            (job_folder / "metrics.json").write_text(
                json.dumps(
                    {
                        "model": "gpt-5.6-terra",
                        "input_tokens": 2000,
                        "output_tokens": 200,
                        "total_tokens": 2200,
                        "estimated_input_cost": None,
                        "estimated_output_cost": None,
                        "estimated_total_cost": None,
                    }
                ),
                encoding="utf-8",
            )
            (job_folder / "abap_generation_chunks.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {
                                "name": "main_program_flow",
                                "model": "gpt-5.6-terra",
                                "usage": {"input_tokens": 2000, "output_tokens": 200, "total_tokens": 2200},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            metrics = load_metrics(jobs_folder, "job")

            self.assertEqual(metrics["estimated_total_cost"], 0.008)
            self.assertEqual(metrics["cost_breakdown"]["by_stage"][0]["label"], "Main flow chunk")
            self.assertEqual(metrics["cost_breakdown"]["by_model"][0]["estimated_total_cost"], 0.008)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_old_first_select_diagnostic_pipeline_is_not_used(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Use SAP table EDIDC.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            def analyzer(_prompt, _source):
                return {
                    "text": json.dumps(
                        {
                            "ddic_objects": [{"name": "EDIDC", "structure": "st_edidc", "table": "t_edidc", "kind": "table", "fields": ["DOCNUM"]}],
                            "callables": [],
                            "local_identifiers": [],
                            "declarations": [
                                {"name": "t_edidc", "kind": "internal_table", "line_type": "EDIDC"},
                                {"name": "w_edidc", "kind": "work_area", "abap_type": "EDIDC"},
                            ],
                            "operations": [
                                {
                                    "operation": "select",
                                    "id": "select_edidc",
                                    "form_name": "select_data",
                                    "source_table": "EDIDC",
                                    "target_table": "t_edidc",
                                    "target_work_area": "w_edidc",
                                    "fields": ["DOCNUM"],
                                    "cardinality": "MULTIPLE",
                                    "access_method": "SELECT",
                                    "conditions": [],
                                },
                                {
                                    "operation": "move",
                                    "id": "move_output",
                                    "form_name": "build_output",
                                    "source": "w_edidc-docnum",
                                    "target": "w_output-docnum",
                                    "access_method": "MOVE",
                                    "cardinality": "SINGLE",
                                },
                            ],
                            "form_names": ["select_data", "build_output"],
                            "execution_order": [
                                {"order": 1, "operation_ref": "select_edidc", "form_name": "select_data"},
                                {"order": 2, "operation_ref": "move_output", "form_name": "build_output"},
                            ],
                            "unresolved": [],
                        }
                    )
                }

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": "REPORT zwhole_program.", "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    dependency_analyzer=analyzer,
                )

            self.assertFalse((jobs_folder / job_id / "diagnostic_first_select_form.abap").exists())
            self.assertFalse((jobs_folder / job_id / "diagnostic_first_select_form_error.txt").exists())
            self.assertIn("REPORT zwhole_program.", (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8"))
            saved_analysis = json.loads((jobs_folder / job_id / "dependency_analysis.json").read_text(encoding="utf-8"))
            self.assertNotIn("operations", saved_analysis)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_generated_missing_table_triggers_second_ddic_lookup_and_final_metadata(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a report using SAP table EDIDC.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            provider = StaticDdicProvider(
                {
                    "tables": {
                        "EDIDC": ddic_table("EDIDC", ["DOCNUM", "MESTYP"]),
                        "EDID4": ddic_table("EDID4", ["DOCNUM", "SDATA"]),
                    }
                }
            )

            with patch(
                "services.create_abap.generate_abap",
                return_value={
                    "text": "REPORT ztest.\nDATA w_payload TYPE edid4-sdata.",
                    "model": "test-model",
                    "usage": None,
                },
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path, ddic_metadata_provider=provider)

            self.assertEqual(provider.requests, [["EDIDC"], ["EDID4"]])
            saved_metadata = json.loads((jobs_folder / job_id / "ddic_metadata.json").read_text(encoding="utf-8"))
            self.assertIn("EDIDC", saved_metadata["tables"])
            self.assertIn("EDID4", saved_metadata["tables"])
            issues = json.loads((jobs_folder / job_id / "validation_issues.json").read_text(encoding="utf-8"))
            self.assertFalse([issue for issue in issues if issue["rule_id"] == "ABAP_UNVERIFIED_DDIC_IDENTIFIER"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_generated_cached_table_does_not_trigger_second_ddic_lookup(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a report using SAP table EDIDC.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            provider = StaticDdicProvider({"tables": {"EDIDC": ddic_table("EDIDC", ["DOCNUM", "MESTYP"])}})

            with patch(
                "services.create_abap.generate_abap",
                return_value={
                    "text": "REPORT ztest.\nDATA w_message_type TYPE edidc-mestyp.",
                    "model": "test-model",
                    "usage": None,
                },
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path, ddic_metadata_provider=provider)

            self.assertEqual(provider.requests, [["EDIDC"]])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_post_generation_local_declarations_are_not_sent_to_ddic_provider(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a report with a local output structure.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            provider = StaticDdicProvider({"tables": {}})

            with patch(
                "services.create_abap.generate_abap",
                return_value={
                    "text": "\n".join(
                        [
                            "REPORT ztest.",
                            "LINE-SIZE 80.",
                            "TYPES st_output TYPE char10.",
                            "DATA w_output TYPE st_output.",
                            "DATA t_output TYPE STANDARD TABLE OF st_output.",
                            "IF sy-subrc = 0.",
                            "ENDIF.",
                            "SY-SUBRC = 0.",
                            "NON-UNICODE.",
                        ]
                    ),
                    "model": "test-model",
                    "usage": None,
                },
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path, ddic_metadata_provider=provider)

            self.assertEqual(provider.requests, [])
            saved_analysis = json.loads((jobs_folder / job_id / "dependency_analysis.json").read_text(encoding="utf-8"))
            rejected = {
                item["name"]: item["reason"]
                for item in saved_analysis["rejected_post_generation_ddic_candidates"]
            }
            self.assertEqual(rejected["LINE"], "ABAP fragment")
            self.assertEqual(rejected["NON"], "ABAP fragment")
            self.assertEqual(rejected["SY"], "system field")
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_post_generation_standalone_type_like_references_are_unresolved_not_fetched(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a report with generated output data.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)
            provider = StaticDdicProvider({"tables": {"EDID4": ddic_table("EDID4", ["SDATA"])}})

            with patch(
                "services.create_abap.generate_abap",
                return_value={
                    "text": "\n".join(
                        [
                            "REPORT ztest.",
                            "DATA w_message TYPE zmessage_data.",
                            "DATA w_other LIKE ymessage_data.",
                            "DATA w_payload TYPE edid4-sdata.",
                        ]
                    ),
                    "model": "test-model",
                    "usage": None,
                },
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path, ddic_metadata_provider=provider)

            self.assertEqual(provider.requests, [["EDID4"]])
            saved_analysis = json.loads((jobs_folder / job_id / "dependency_analysis.json").read_text(encoding="utf-8"))
            self.assertIn("ZMESSAGE_DATA", saved_analysis["unresolved"])
            self.assertIn("YMESSAGE_DATA", saved_analysis["unresolved"])
            rejected = {
                item["name"]: item["reason"]
                for item in saved_analysis["rejected_post_generation_ddic_candidates"]
            }
            self.assertEqual(rejected["ZMESSAGE_DATA"], "ambiguous standalone type")
            self.assertEqual(rejected["YMESSAGE_DATA"], "ambiguous standalone type")
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_generated_guessed_ddic_field_is_reported_by_final_acceptance(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a report using SAP table EDIDC.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.\n{{SPECIFICATION}}", encoding="utf-8")
            job_id = create_job(jobs_folder)

            with patch(
                "services.create_abap.generate_abap",
                return_value={
                    "text": "REPORT ztest.\nDATA w_bad TYPE edidc-mestyq.",
                    "model": "test-model",
                    "usage": None,
                },
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(
                    job_id,
                    input_path,
                    jobs_folder,
                    prompt_path,
                    ddic_metadata_provider=StaticDdicProvider(ddic_metadata()),
                )

            issues = json.loads((jobs_folder / job_id / "validation_issues.json").read_text(encoding="utf-8"))
            ddic_issues = [issue for issue in issues if issue["rule_id"] == "ABAP_UNVERIFIED_DDIC_IDENTIFIER"]
            self.assertEqual(len(ddic_issues), 1)
            self.assertEqual(ddic_issues[0]["proposed_identifier"], "EDIDC-MESTYQ")
            self.assertEqual(ddic_issues[0]["closest_identifier"], "EDIDC-MESTYP")
            self.assertEqual(ddic_issues[0]["closest_source"], "sap-metadata")
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_signature_provider_supplies_callable_metadata(self):
        class StaticProvider:
            def __init__(self):
                self.requested = None

            def get_signatures(self, callable_identities):
                self.requested = callable_identities
                return callable_metadata()["callable_signatures"]

        provider = StaticProvider()
        source = "\n".join(["CALL FUNCTION 'Z_TEST_FUNCTION'", "  TABLES", "    MESSAGE = text."])

        metadata = resolve_callable_metadata(
            source,
            lambda lines: [{"name": "Z_TEST_FUNCTION"}],
            signature_provider=provider,
        )

        self.assertEqual(provider.requested, ["Z_TEST_FUNCTION"])
        self.assertIn("Z_TEST_FUNCTION", metadata["callable_signatures"])

    def test_internal_metadata_takes_precedence_over_provider(self):
        class FailingProvider:
            def get_signatures(self, callable_identities):
                raise AssertionError("Provider should not be called when internal metadata exists.")

        metadata = callable_metadata()

        resolved = resolve_callable_metadata("", lambda lines: [], internal_metadata=metadata, signature_provider=FailingProvider())

        self.assertIs(resolved, metadata)

    def test_create_result_repairs_list_processing_leave_report_without_regeneration(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create an interactive list report.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            job_id = create_job(jobs_folder)
            generated_abap = "\n".join(
                [
                    "REPORT ztest.",
                    "AT USER-COMMAND.",
                    "  WRITE: / 'Back'.",
                    "  LEAVE REPORT.",
                    "  WRITE: / 'After'.",
                ]
            )

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": generated_abap, "model": "test-model", "usage": None},
            ) as generate_mock, patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path)

            self.assertEqual(generate_mock.call_count, 7)
            fixed = (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8")
            self.assertEqual(
                fixed,
                "\n".join(
                    [
                        "REPORT ztest.",
                        "AT USER-COMMAND.",
                        "  WRITE: / 'Back'.",
                        "  LEAVE LIST-PROCESSING.",
                        "  WRITE: / 'After'.",
                    ]
                ),
            )
            fixes = json.loads((jobs_folder / job_id / "fix_summary.json").read_text(encoding="utf-8"))["fixes"]
            self.assertEqual(
                [fix["rule_id"] for fix in fixes],
                ["ABAP_LIST_PROCESSING_EXIT_MISMATCH"],
            )
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_prompt_includes_list_processing_control_flow_guidance(self):
        prompt = Path("prompts/create_abap.txt").read_text(encoding="utf-8")

        self.assertIn("Use LEAVE LIST-PROCESSING to leave interactive list processing", prompt)
        self.assertIn("Preserve LEAVE LIST-PROCESSING when it already exists", prompt)
        self.assertIn("Do not generate LEAVE REPORT", prompt)
        self.assertIn("LEAVE SCREEN, LEAVE TO SCREEN, LEAVE PROGRAM, LEAVE REPORT, EXIT, CHECK, RETURN, and STOP", prompt)

    def test_prompt_includes_generic_alv_output_guidance(self):
        prompt = Path("prompts/create_abap.txt").read_text(encoding="utf-8")

        self.assertIn("When the specification requests ALV output:", prompt)
        self.assertIn("Generate actual ALV output using REUSE_ALV_GRID_DISPLAY or the ALV mechanism explicitly required by the specification.", prompt)
        self.assertIn("Do not generate WRITE, ULINE, SKIP or classical list output as an ALV substitute.", prompt)
        self.assertIn("Build and pass the field catalogue to the ALV function.", prompt)
        self.assertIn("Pass the output internal table through T_OUTTAB.", prompt)
        self.assertIn("Declare all ALV variables in a scope where the ALV form can access them.", prompt)
        self.assertIn("Include or exclude optional columns according to the specification.", prompt)

    def test_alv_request_with_write_output_is_saved_as_validation_issue(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            input_path = uploads_folder / "job" / "request.txt"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("Create a report with ALV output.", encoding="utf-8")
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            job_id = create_job(jobs_folder)
            generated_abap = "\n".join(
                [
                    "REPORT ztest.",
                    "FORM display_output.",
                    "  WRITE: / 'Output'.",
                    "ENDFORM.",
                ]
            )

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": generated_abap, "model": "test-model", "usage": None},
            ), patch("services.create_abap.time.perf_counter", side_effect=[1.0, 1.5]):
                run_create_abap(job_id, input_path, jobs_folder, prompt_path)

            issues = json.loads((jobs_folder / job_id / "validation_issues.json").read_text(encoding="utf-8"))
            self.assertTrue([issue for issue in issues if issue["rule_id"] == "ALV_REQUESTED_WITH_CLASSICAL_LIST_OUTPUT"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_progress_page_polls_only_while_active(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            client = app.test_client()
            job_id = create_job(jobs_folder)

            active = client.get(f"/progress/{job_id}")
            self.assertEqual(active.status_code, 200)
            self.assertIn(b"setInterval", active.data)
            self.assertIn(b"/status", active.data)
            self.assertIn(b"return true;", active.data)
            self.assertIn(b"pollAndMaybeStop();", active.data)
            self.assertIn(b"true", active.data)
            self.assertIn(b"Elapsed time:", active.data)
            active_status = client.get(f"/progress/{job_id}/status")
            self.assertEqual(active_status.status_code, 200)
            self.assertTrue(active_status.get_json()["is_active"])

            update_progress(jobs_folder, job_id, "Complete", "ABAP generation complete.", stage="Complete")
            complete = client.get(f"/progress/{job_id}")
            self.assertEqual(complete.status_code, 200)
            self.assertIn(b"false", complete.data)
            complete_status = client.get(f"/progress/{job_id}/status")
            self.assertEqual(complete_status.status_code, 200)
            self.assertFalse(complete_status.get_json()["is_active"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_result_page_displays_dependency_analysis_artifact(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            client = app.test_client()
            job_id = create_job(jobs_folder)
            job_folder = jobs_folder / job_id
            job_folder.mkdir(parents=True, exist_ok=True)
            (job_folder / "generated.abap").write_text("REPORT ztest.", encoding="utf-8")
            (job_folder / "dependency_analysis.json").write_text(
                json.dumps(
                    {
                        "ddic_objects": [{"name": "EDIDC", "structure": "st_edidc", "table": "t_edidc"}],
                        "callables": ["Z_TEST_FUNCTION", "CL_TEST=>RUN"],
                        "local_identifiers": ["ST_OUTPUT"],
                        "unresolved": ["UNKNOWN_THING"],
                        "pre_generation_ddic_objects": [{"name": "EDIDC", "structure": "st_edidc", "table": "t_edidc"}],
                        "pre_generation_callable_identities": ["Z_TEST_FUNCTION", "CL_TEST=>RUN"],
                        "final_callable_identities": ["Z_TEST_FUNCTION", "CL_TEST=>RUN", "Z_OTHER_FUNCTION"],
                        "post_generation_ddic_candidates": ["EDID4", "SY"],
                        "rejected_post_generation_ddic_candidates": [{"name": "SY", "reason": "system field"}],
                        "rejected_analysis_entries": [
                            {
                                "name": "ZDEMO_ID",
                                "category": "ddic_objects",
                                "reason": "not a table, structure, or view",
                            }
                        ],
                        "_diagnostics": {
                            "used_fallback": False,
                            "prompt": "Return SAP dependency analysis as JSON only.",
                            "input": [
                                {"role": "system", "content": "Return SAP dependency analysis as JSON only."},
                                {"role": "user", "content": "Use SAP table EDIDC."},
                            ],
                            "raw_response": "{\"ddic_objects\":[\"EDIDC\"]}",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (job_folder / "ddic_metadata.json").write_text(
                json.dumps({"tables": {"EDIDC": {}, "EDID4": {}}}),
                encoding="utf-8",
            )
            (job_folder / "metrics.json").write_text(
                json.dumps(
                    {
                        "duration_seconds": 6.5,
                        "section_durations": {
                            "validation": 0.11,
                            "auto_fix": 0.22,
                            "sap_syntax_check": 0.33,
                            "sap_metadata_requests": 0.44,
                            "dependency_analysis": 0.55,
                            "generated_abap": 0.66,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (job_folder / "abap_generation_chunks.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {
                                "name": "declarations",
                                "subtitle": "Report declarations",
                                "prompt": "Chunk prompt sent to the model.",
                                "filtered_ddic_metadata": "SAP DDIC metadata catalogue:\n- EDIDC: DOCNUM",
                                "ddic_metadata_filter": {
                                    "fields_extracted_from_specification": ["EDIDC-DOCNUM"],
                                    "matched_sap_metadata_fields": ["EDIDC-DOCNUM"],
                                    "complete_row_type_objects": ["EDIDC"],
                                    "final_filtered_metadata": "SAP DDIC metadata catalogue:\n- EDIDC: DOCNUM",
                                },
                                "declaration_requirements": {
                                    "requirements": {
                                        "report_name": "ztest",
                                        "parameters": [],
                                        "select_options": [],
                                    },
                                    "raw_response": "{\"report_name\":\"ztest\"}",
                                },
                                "declaration_naming_contract": (
                                    "Exact internal-table names: t_edidc\n"
                                    "Exact work-area names: w_edidc\n"
                                    "- EDIDC: structure st_edidc, table t_edidc, work area w_edidc"
                                ),
                                "text": "REPORT ztest.",
                                "duration_seconds": 1.25,
                            },
                            {
                                "name": "processing_form",
                                "prompt": "Processing prompt.",
                                "processing_plan": {
                                    "plan": {
                                        "processing_steps": [
                                            {
                                                "operation": "LOOP",
                                                "source": "t_edidc",
                                                "into": "st_edidc",
                                                "steps": [{"operation": "MOVE", "source": "st_edidc-DOCNUM", "target": "DOCNUM"}],
                                            }
                                        ]
                                    },
                                    "top_level_step_index": 1,
                                },
                                "text": "FORM process_data.\nENDFORM.",
                                "duration_seconds": 2.0,
                            }
                        ],
                        "assembled_abap": "REPORT ztest.",
                        "used_fallback": False,
                        "declaration_requirements": {
                            "prompt": "Extract declaration requirements.",
                            "raw_response": "{\"report_name\":\"ztest\"}",
                            "requirements": {"report_name": "ztest"},
                            "duration_seconds": 2.5,
                        },
                        "processing_plan": {
                            "prompt": "Extract business-processing logic.",
                            "raw_response": "{\"processing_steps\":[]}",
                            "plan": {"processing_steps": []},
                            "duration_seconds": 3.75,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = client.get(f"/result/{job_id}")

            self.assertEqual(result.status_code, 200)
            self.assertIn(b"Developer Diagnostics", result.data)
            self.assertIn(b"declarations", result.data)
            self.assertIn(b"Report declarations", result.data)
            self.assertIn(b"Step 1: Loop t_edidc with 1 nested step", result.data)
            self.assertIn(b"Chunk subtitle", result.data)
            self.assertIn(b"1.25s", result.data)
            self.assertIn(b"declaration_requirements", result.data)
            self.assertIn(b"2.50s", result.data)
            self.assertIn(b"processing_plan", result.data)
            self.assertIn(b"3.75s", result.data)
            self.assertIn(b"Extract declaration requirements.", result.data)
            self.assertIn(b"Extract business-processing logic.", result.data)
            self.assertIn(b"Exact prompt sent to the LLM", result.data)
            self.assertIn(b"Chunk prompt sent to the model.", result.data)
            self.assertIn(b"Final DECLARATION_REQUIREMENTS", result.data)
            self.assertIn(b"Final declaration naming contract", result.data)
            self.assertIn(b"report_name", result.data)
            self.assertIn(b"Fields extracted from the functional specification", result.data)
            self.assertIn(b"Matched SAP metadata fields", result.data)
            self.assertIn(b"Complete row-type objects required", result.data)
            self.assertIn(b"EDIDC-DOCNUM", result.data)
            self.assertIn(b"Filtered DDIC metadata sent to this chunk", result.data)
            self.assertIn(b"Raw LLM response", result.data)
            self.assertNotIn(b"SAP Dependency Analysis", result.data)
            self.assertIn(b"Dependency Analysis Diagnostics", result.data)
            self.assertIn(b"Dependency Analysis Prompt", result.data)
            self.assertIn(b"Return SAP dependency analysis as JSON only.", result.data)
            self.assertNotIn(b"Dependency Analysis Input", result.data)
            self.assertIn(b"Raw LLM response", result.data)
            self.assertIn(b"Normalized dependency analysis", result.data)
            self.assertIn(b"Final callable identities passed to SAP", result.data)
            self.assertIn(b"Rejected analysis entries with reasons", result.data)
            self.assertIn("Processing time: 0.11s", result_section(result.data, "Validation"))
            self.assertIn("Processing time: 0.22s", result_section(result.data, "Auto Fix"))
            self.assertIn("Processing time: 0.33s", result_section(result.data, "SAP Syntax Check"))
            self.assertIn("Processing time: 0.55s", result_section(result.data, "Dependency Analysis Diagnostics"))
            self.assertIn("Processing time: 0.66s", result_section(result.data, "Generated ABAP"))
            self.assertIn(b"EDIDC", result.data)
            self.assertIn(b"ZDEMO_ID", result.data)
            self.assertIn(b"not a table, structure, or view", result.data)
            self.assertIn(b"Z_TEST_FUNCTION", result.data)
            self.assertIn(b"Z_OTHER_FUNCTION", result.data)
            self.assertIn(b"CL_TEST=&gt;RUN", result.data)
            self.assertIn(b"ST_OUTPUT", result.data)
            self.assertIn(b"UNKNOWN_THING", result.data)
            self.assertIn(b"EDID4", result.data)
            metadata_panel = result_section(result.data, "SAP Metadata Requests")
            self.assertIn("Processing time: 0.44s", metadata_panel)
            self.assertIn("DDIC tables and structures", metadata_panel)
            self.assertIn("EDID4, EDIDC", metadata_panel)
            self.assertIn("Function modules", metadata_panel)
            self.assertIn("Z_TEST_FUNCTION", metadata_panel)
            self.assertIn("Class/interface methods", metadata_panel)
            self.assertIn("CL_TEST=&gt;RUN", metadata_panel)
            self.assertNotIn("Z_OTHER_FUNCTION", metadata_panel)
            self.assertNotIn("ST_OUTPUT", metadata_panel)
            self.assertNotIn("UNKNOWN_THING", metadata_panel)
            self.assertNotIn("ZDEMO_ID", metadata_panel)
            self.assertNotIn("SY", metadata_panel)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_result_page_shows_chunk_fallback_reason_when_no_chunks_exist(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            client = app.test_client()
            job_id = create_job(jobs_folder)
            job_folder = jobs_folder / job_id
            job_folder.mkdir(parents=True, exist_ok=True)
            (job_folder / "generated.abap").write_text("REPORT ztest.", encoding="utf-8")
            (job_folder / "abap_generation_chunks.json").write_text(
                json.dumps(
                    {
                        "chunks": [],
                        "assembled_abap": "REPORT ztest.",
                        "used_fallback": True,
                        "fallback_reason": "ValueError: output_forms referenced undeclared global variable(s): it_fieldcat",
                    }
                ),
                encoding="utf-8",
            )

            result = client.get(f"/result/{job_id}")

            self.assertEqual(result.status_code, 200)
            self.assertIn(b"Chunked ABAP generation fell back to full-program generation.", result.data)
            self.assertIn(b"Fallback reason", result.data)
            self.assertIn(b"it_fieldcat", result.data)
            self.assertNotIn(b"No ABAP generation chunk diagnostics recorded.", result.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_result_page_tolerates_null_top_level_generation_diagnostics(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            client = app.test_client()
            job_id = create_job(jobs_folder)
            job_folder = jobs_folder / job_id
            job_folder.mkdir(parents=True, exist_ok=True)
            (job_folder / "generated.abap").write_text("REPORT ztest.", encoding="utf-8")
            (job_folder / PROCESSING_PLAN_CONTEXT_ARTIFACT).write_text(
                json.dumps(
                    {
                        "declaration_requirements": {
                            "prompt": "Saved declaration prompt.",
                            "raw_response": "{\"report_name\":\"ztest\"}",
                            "requirements": {"report_name": "ztest"},
                            "duration_seconds": 1.5,
                        }
                    }
                ),
                encoding="utf-8",
            )
            (job_folder / PROCESSING_PLAN_DIAGNOSTICS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "prompt": "Saved processing plan prompt.",
                        "raw_response": "{\"processing_steps\":[]}",
                        "plan": {"processing_steps": []},
                        "duration_seconds": 2.5,
                    }
                ),
                encoding="utf-8",
            )
            (job_folder / "abap_generation_chunks.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {
                                "name": "declarations",
                                "prompt": "Declarations prompt.",
                                "text": "REPORT ztest.",
                            }
                        ],
                        "assembled_abap": "REPORT ztest.",
                        "used_fallback": False,
                        "declaration_requirements": None,
                        "processing_plan": {"plan": {"processing_steps": []}},
                    }
                ),
                encoding="utf-8",
            )

            result = client.get(f"/result/{job_id}")

            self.assertEqual(result.status_code, 200)
            self.assertIn(b"Generated ABAP", result.data)
            self.assertIn(b"declaration_requirements", result.data)
            self.assertIn(b"1.50s", result.data)
            self.assertIn(b"2.50s", result.data)
            self.assertIn(b"Saved declaration prompt.", result.data)
            self.assertIn(b"Saved processing plan prompt.", result.data)
            self.assertIn(b"{&#34;processing_steps&#34;:[]}", result.data)
            self.assertIn(b"Declarations prompt.", result.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_result_page_shows_processed_chunks_when_chunked_generation_falls_back(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            client = app.test_client()
            job_id = create_job(jobs_folder)
            job_folder = jobs_folder / job_id
            job_folder.mkdir(parents=True, exist_ok=True)
            (job_folder / "generated.abap").write_text("REPORT zfallback.", encoding="utf-8")
            (job_folder / "abap_generation_chunks.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {
                                "name": "declarations",
                                "prompt": "Declarations prompt.",
                                "text": "REPORT ztest.",
                                "duration_seconds": 1.0,
                            },
                            {
                                "name": "output_forms",
                                "prompt": "Output prompt.",
                                "text": "FORM output_data.\nENDFORM.",
                                "duration_seconds": 2.0,
                                "error": "ValueError: output_forms referenced undeclared global variable(s): w_record",
                            },
                        ],
                        "assembled_abap": "REPORT zfallback.",
                        "used_fallback": True,
                        "fallback_reason": "ValueError: output_forms referenced undeclared global variable(s): w_record",
                    }
                ),
                encoding="utf-8",
            )

            result = client.get(f"/result/{job_id}")

            self.assertEqual(result.status_code, 200)
            self.assertIn(b"Chunked ABAP generation fell back to full-program generation.", result.data)
            self.assertIn(b"declarations", result.data)
            self.assertIn(b"output_forms", result.data)
            self.assertIn(b"Chunk error", result.data)
            self.assertIn(b"w_record", result.data)
            self.assertNotIn(b"No ABAP generation chunk diagnostics recorded.", result.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_result_page_labels_dependency_fallback_and_missing_artifact(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})
            client = app.test_client()
            job_id = create_job(jobs_folder)
            job_folder = jobs_folder / job_id
            job_folder.mkdir(parents=True, exist_ok=True)
            (job_folder / "generated.abap").write_text("REPORT ztest.", encoding="utf-8")
            (job_folder / "dependency_analysis.json").write_text(
                json.dumps(
                    {
                        "ddic_objects": [],
                        "callables": [],
                        "local_identifiers": [],
                        "unresolved": [],
                        "_diagnostics": {"used_fallback": True, "fallback_reason": "disabled"},
                    }
                ),
                encoding="utf-8",
            )

            fallback = client.get(f"/result/{job_id}")
            self.assertNotIn(b"SAP Dependency Analysis", fallback.data)
            self.assertIn(b"Dependency Analysis Diagnostics", fallback.data)
            metadata_panel = result_section(fallback.data, "SAP Metadata Requests")
            self.assertIn("None", metadata_panel)
            self.assertNotIn("DDIC tables and structures", metadata_panel)
            self.assertNotIn("Function modules", metadata_panel)
            self.assertNotIn("Class/interface methods", metadata_panel)

            missing_job_id = create_job(jobs_folder)
            missing_job_folder = jobs_folder / missing_job_id
            missing_job_folder.mkdir(parents=True, exist_ok=True)
            (missing_job_folder / "generated.abap").write_text("REPORT ztest.", encoding="utf-8")
            missing = client.get(f"/result/{missing_job_id}")
            self.assertIn(b"No dependency analysis recorded", missing.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_active_processing_duration_sums_section_work(self):
        self.assertEqual(
            active_processing_duration(
                {
                    "dependency_analysis": 2.0,
                    "sap_metadata_requests": 0.5,
                    "declaration_requirements": 3.0,
                    "processing_plan_extraction": 4.0,
                    "generated_abap": 5.0,
                    "validation": None,
                },
                fallback=99.0,
            ),
            14.5,
        )
        self.assertEqual(active_processing_duration({}, fallback=99.0), 99.0)

    def test_existing_metrics_display_uses_active_section_duration(self):
        metrics = normalize_metrics_for_display(
            {
                "duration_seconds": 240.0,
                "section_durations": {
                    "dependency_analysis": 2.0,
                    "sap_metadata_requests": 0.5,
                    "generated_abap": 5.0,
                },
            }
        )

        self.assertEqual(metrics["duration_seconds"], 7.5)

    def test_approval_resume_usage_uses_prior_usage_and_chunk_usage_once(self):
        usage = usage_for_final_metrics(
            {
                "usage": {"input_tokens": 999, "output_tokens": 999, "total_tokens": 1998},
                "chunks": [
                    {"usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}},
                    {"usage": {"input_tokens": 200, "output_tokens": 75, "total_tokens": 275}},
                ],
            },
            {"input_tokens": 999, "output_tokens": 999, "total_tokens": 1998},
            prior_usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

        self.assertEqual(usage, {"input_tokens": 310, "output_tokens": 130, "total_tokens": 440})
        fallback_usage = usage_for_final_metrics(
            {
                "used_fallback": True,
                "usage": {"input_tokens": 300, "output_tokens": 100, "total_tokens": 400},
                "chunks": [
                    {"usage": {"input_tokens": 25, "output_tokens": 10, "total_tokens": 35}},
                ],
            },
            {"input_tokens": 300, "output_tokens": 100, "total_tokens": 400},
            prior_usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
        self.assertEqual(fallback_usage, {"input_tokens": 335, "output_tokens": 115, "total_tokens": 450})

    def _run_flow(self, tmp_path):
        uploads_folder = tmp_path / "uploads"
        jobs_folder = tmp_path / "jobs"
        prompt_path = tmp_path / "create_abap.txt"
        prompt_template = "Generate ABAP.\n{{REPORT_SKELETON}}\n{{DATABASE_READ_PATTERNS}}\n{{SPECIFICATION}}"
        prompt_path.write_text(prompt_template, encoding="utf-8")
        source_text = "Create a test report."
        expected_prompt = render_create_prompt(
            prompt_template,
            source_text,
            REPORT_SKELETON_PATH.read_text(encoding="utf-8"),
            DATABASE_READ_PATTERNS_PATH.read_text(encoding="utf-8"),
        )
        old_pricing = Config.MODEL_PRICING
        Config.MODEL_PRICING = {
            "test-model": {
                "input_per_1m_tokens": 1.00,
                "output_per_1m_tokens": 2.00,
            }
        }

        try:
            with patch(
                "services.create_abap.generate_abap",
                return_value={
                    "text": "```abap\nREPORT zphase2.\n```",
                    "model": "test-model",
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "total_tokens": 1500,
                    },
                }
            ) as generate_mock, patch("services.create_abap.time.perf_counter", side_effect=[10.0, 10.25]):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()

                home = client.get("/")
                self.assertEqual(home.status_code, 200)
                self.assertNotIn(b"Callable metadata JSON", home.data)
                self.assertNotIn(b"name=\"callable_metadata\"", home.data)
                self.assertNotIn(b"id=\"callable_metadata\"", home.data)

                upload = client.post(
                    "/upload",
                    data={"abap_file": (BytesIO(source_text.encode("utf-8")), "request.txt")},
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )
                self.assertEqual(upload.status_code, 302)
                self.assertTrue(upload.headers["Location"].startswith("/progress/"))

                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Complete")
                self.assertEqual(generate_mock.call_count, 7)
                extraction_prompt, extraction_source = generate_mock.call_args_list[0].args
                self.assertIn("Extract declaration requirements", extraction_prompt)
                self.assertEqual(extraction_source, source_text)
                processing_plan_prompt, processing_plan_source = generate_mock.call_args_list[1].args
                self.assertIn("Extract business-processing logic", processing_plan_prompt)
                self.assertEqual(processing_plan_source, source_text)
                first_prompt, first_source = generate_mock.call_args_list[2].args
                self.assertIn("Chunk: declarations", first_prompt)
                self.assertIn("Declarations-specific prompt:", first_prompt)
                self.assertNotIn("Functional specification:", first_prompt)
                self.assertNotIn("Create a test report.", first_prompt)
                self.assertIn("Shared generation contract:", first_prompt)
                self.assertNotIn("Exact FORM names:", first_prompt)
                self.assertNotIn("SAP callable signature catalogue:", first_prompt)
                self.assertNotIn("START-OF-SELECTION", first_prompt)
                self.assertNotIn("Full-program final review", first_prompt)
                self.assertNotIn("DATABASE_READ_PATTERNS", first_prompt)
                second_prompt = generate_mock.call_args_list[3].args[0]
                self.assertIn("Chunk: database_read_forms", second_prompt)
                self.assertIn("Database-read chunk prompt:", second_prompt)
                self.assertNotIn(expected_prompt, second_prompt)
                self.assertNotIn("{{REPORT_SKELETON}}", second_prompt)
                self.assertNotIn("{{DATABASE_READ_PATTERNS}}", second_prompt)
                self.assertNotEqual(first_source, source_text)
                self.assertTrue((uploads_folder / job_id / "request.txt").exists())
                self.assertTrue((jobs_folder / job_id / "status.json").exists())
                self.assertTrue((jobs_folder / job_id / "abap_generation_chunks.json").exists())
                self.assertEqual(
                    (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8"),
                    "REPORT zphase2.",
                )

                metrics = json.loads((jobs_folder / job_id / "metrics.json").read_text(encoding="utf-8"))
                self.assertEqual(metrics["model"], "test-model")
                self.assertAlmostEqual(
                    metrics["duration_seconds"],
                    sum(metrics["section_durations"].values()),
                )
                self.assertGreaterEqual(metrics["duration_seconds"], 0.25)
                self.assertEqual(metrics["input_tokens"], 7000)
                self.assertEqual(metrics["output_tokens"], 3500)
                self.assertEqual(metrics["total_tokens"], 10500)
                self.assertGreater(metrics["prompt_characters"], len(first_prompt))
                self.assertEqual(metrics["specification_characters"], len(source_text))
                self.assertEqual(metrics["generated_abap_characters"], len("REPORT zphase2."))
                self.assertEqual(metrics["generated_abap_lines"], 1)
                self.assertEqual(metrics["estimated_input_cost"], 0.007)
                self.assertEqual(metrics["estimated_output_cost"], 0.007)
                self.assertEqual(metrics["estimated_total_cost"], 0.014)
                for section_name in (
                    "validation",
                    "auto_fix",
                    "sap_syntax_check",
                    "sap_metadata_requests",
                    "dependency_analysis",
                    "generated_abap",
                ):
                    self.assertIsInstance(metrics["section_durations"][section_name], float)

                progress = client.get(f"/progress/{job_id}")
                self.assertEqual(progress.status_code, 200)
                self.assertIn(b"Complete", progress.data)
                self.assertIn(b"100% complete", progress.data)
                self.assertIn(b"Elapsed time:", progress.data)
                self.assertIn(b"pollProgress", progress.data)
                self.assertIn(b"Download generated.abap", progress.data)
                saved_progress = get_progress(jobs_folder, job_id)
                self.assertEqual(saved_progress["status"], "Complete")
                self.assertEqual(saved_progress["current_stage"], "Complete")
                self.assertEqual(saved_progress["progress_percent"], 100)

                result = client.get(f"/result/{job_id}")
                self.assertEqual(result.status_code, 200)
                self.assertIn(b"REPORT zphase2.", result.data)
                self.assertIn(b"Token Usage", result.data)
                self.assertIn(b"10,500", result.data)
                self.assertIn(b"$0.014000", result.data)
                self.assertIn(b'<details class="metric-card">', result.data)
                self.assertIn(b'<details class="code-panel">', result.data)
                self.assertNotIn(b'<details class="code-panel" open', result.data)
                self.assertIn(b'id="copy-abap"', result.data)
                self.assertIn(b'class="generated-abap-block"', result.data)
                self.assertIn(b"Copied to clipboard.", result.data)
                self.assertIn(b'id="generated-abap"', result.data)
                self.assertIn(b"if (copyButton && copyMessage && generatedAbap)", result.data)
                self.assertIn(b"No deterministic issues found.", result.data)

                download = client.get(f"/download/{job_id}")
                self.assertEqual(download.status_code, 200)
                self.assertEqual(download.data, b"REPORT zphase2.")
                download.close()
        finally:
            Config.MODEL_PRICING = old_pricing

    def test_missing_token_usage_is_unavailable(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")

            with patch(
                "services.create_abap.generate_abap",
                return_value={
                    "text": "REPORT zphase2.",
                    "model": "test-model",
                    "usage": None,
                },
            ):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/upload",
                    data={"abap_file": (BytesIO(b"Create a test report."), "request.txt")},
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )

                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Complete")
                metrics = json.loads((jobs_folder / job_id / "metrics.json").read_text(encoding="utf-8"))
                self.assertIsNone(metrics["input_tokens"])
                self.assertIsNone(metrics["output_tokens"])
                self.assertIsNone(metrics["total_tokens"])
                self.assertIsNone(metrics["estimated_total_cost"])

                result = client.get(f"/result/{job_id}")
                self.assertEqual(result.status_code, 200)
                self.assertIn(b"Unavailable", result.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)


def wait_for_status(jobs_folder, job_id, expected_status, timeout=5):
    deadline = real_time.time() + timeout
    while real_time.time() < deadline:
        progress = get_progress(jobs_folder, job_id)
        if progress["status"] == expected_status:
            return progress
        real_time.sleep(0.02)
    return get_progress(jobs_folder, job_id)


def wait_for_stage(jobs_folder, job_id, expected_stage, timeout=5):
    deadline = real_time.time() + timeout
    while real_time.time() < deadline:
        progress = get_progress(jobs_folder, job_id)
        if progress["current_stage"] == expected_stage:
            return progress
        real_time.sleep(0.02)
    return get_progress(jobs_folder, job_id)


def prompts_by_chunk_name(prompts):
    result = {}
    for prompt in prompts:
        for line in str(prompt or "").splitlines():
            if line.startswith("- Chunk: "):
                result[line.split(": ", 1)[1]] = prompt
                break
    return result


def callable_metadata():
    return {
        "callable_signatures": {
            "Z_TEST_FUNCTION": {
                "parameters": {
                    "ID": {"direction": "EXPORTING", "abap_type": "char10", "required": True},
                    "MESSAGE": {"direction": "IMPORTING", "abap_type": "string", "required": True},
                }
            }
        },
        "technical_mapping": {
            "callable": "Z_TEST_FUNCTION",
            "parameter_mappings": {
                "EXPORTING": {"ID": "source_structure-field1"},
                "IMPORTING": {"MESSAGE": "target_variable"},
            },
        },
    }


class StaticDdicProvider:
    def __init__(self, metadata, events=None):
        self.metadata = metadata
        self.requested = None
        self.requests = []
        self.events = events

    def get_tables(self, table_names):
        names = list(table_names)
        if self.events is not None:
            self.events.append("ddic")
        self.requested = names
        self.requests.append(names)
        tables = self.metadata.get("tables", {})
        return {"tables": {name: tables.get(name, ddic_table(name, [])) for name in names}}


class FailingDdicProvider:
    def get_tables(self, table_names, progress_callback=None):
        raise DdicMetadataError("SAP unavailable")


class ProgressDdicProvider(StaticDdicProvider):
    def get_tables(self, table_names, progress_callback=None):
        names = list(table_names)
        if progress_callback:
            progress_callback("Calling SAP to get DDIC metadata")
        metadata = super().get_tables(names)
        if progress_callback:
            progress_callback("Called SAP to get DDIC metadata")
        return metadata


class RecordingSignatureProvider:
    def __init__(self, events=None):
        self.requests = []
        self.events = events

    def get_signatures(self, callable_identities):
        names = list(callable_identities)
        if self.events is not None and names:
            self.events.append("signature")
        self.requests.append(names)
        return {
            "callable_signatures": {
                name: {
                    "parameters": {
                        "VALUE": {"direction": "EXPORTING", "abap_type": "char10", "required": False}
                    }
                }
                for name in names
            }
        }


class SelectiveSignatureProvider:
    def __init__(self):
        self.requests = []

    def get_signatures(self, callable_identities):
        names = list(callable_identities)
        self.requests.append(names)
        return {
            "callable_signatures": {
                "Z_DEP_FUNCTION": {
                    "parameters": {
                        "IV_INPUT": {"direction": "IMPORTING", "abap_type": "STRING", "required": True}
                    }
                },
                "ZCL_DEP=>RUN": {
                    "parameters": {
                        "IV_INPUT": {"direction": "IMPORTING", "abap_type": "STRING", "required": True}
                    },
                    "returning": {"name": "RV_RESULT", "direction": "RETURNING", "abap_type": "STRING"},
                },
            },
            "_diagnostics": {
                "unresolved": [
                    {"identity": "Z_MISSING_FUNCTION", "reason": "signature not retrieved"}
                ]
            },
        }


class RecordingSyntaxChecker:
    def __init__(self, results):
        self.results = list(results)
        self.sources = []

    def check(self, source_code):
        self.sources.append(source_code)
        if len(self.sources) <= len(self.results):
            return self.results[len(self.sources) - 1]
        return self.results[-1]


class RecordingCodeReviewRepairer:
    def __init__(self, response_text):
        self.response_text = response_text
        self.prompts = []
        self.sources = []

    def __call__(self, prompt_text, source_text):
        self.prompts.append(prompt_text)
        self.sources.append(source_text)
        return {"text": self.response_text, "model": "test-model", "usage": None}


class SequenceCodeReviewRepairer:
    def __init__(self, response_texts):
        self.response_texts = list(response_texts)
        self.prompts = []
        self.sources = []

    def __call__(self, prompt_text, source_text):
        self.prompts.append(prompt_text)
        self.sources.append(source_text)
        index = min(len(self.sources) - 1, len(self.response_texts) - 1)
        return {"text": self.response_texts[index], "model": "test-model", "usage": None}


def syntax_failure(message, source_line):
    return {
        "requested": True,
        "status": "failed",
        "passed": False,
        "errors": [
            {
                "line": 1,
                "column": None,
                "severity": "E",
                "message": message,
                "word": "REPORT",
                "source_line": source_line,
            }
        ],
        "raw_response": f"<sap>{message}</sap>",
        "technical_message": "",
    }


def ddic_table(table_name, fields):
    return {
        "name": table_name,
        "field_count": len(fields),
        "fields": {field: {"name": field} for field in fields},
        "field_order": fields,
    }


def ddic_metadata():
    return {
        "tables": {
            "EDIDC": {
                "name": "EDIDC",
                "fields": {
                    "DOCNUM": {
                        "name": "DOCNUM",
                        "datatype": "NUMC",
                        "length": 16,
                        "decimals": 0,
                        "description": "IDoc number",
                        "key": True,
                    },
                    "MESTYP": {
                        "name": "MESTYP",
                        "datatype": "CHAR",
                        "length": 30,
                        "decimals": 0,
                        "description": "Message Type",
                        "key": False,
                    },
                },
            }
        }
    }


def result_section(response_data, summary):
    html = response_data.decode("utf-8")
    marker = f"<summary>{summary}</summary>"
    start = html.index(marker)
    remainder = html[start:]
    next_panel = remainder.find('<details class="validation-panel">', len(marker))
    if next_panel == -1:
        return remainder
    return remainder[:next_panel]


def chunked_test_responses():
    return {
        "declarations": "REPORT ztest.",
        "database_read_forms": "FORM read_data.\nENDFORM.",
        "processing_form": "FORM process_data.\nENDFORM.",
        "output_forms": "FORM output_data.\nENDFORM.",
        "main_program_flow": "START-OF-SELECTION.",
    }


if __name__ == "__main__":
    unittest.main()
