from io import BytesIO
import json
from pathlib import Path
import shutil
import time as real_time
import unittest
from uuid import uuid4
from unittest.mock import patch

from app import create_app
from services.enhance_abap import (
    APPROVED_ENHANCEMENT_ARTIFACT,
    ENHANCEMENT_PROPOSAL_ARTIFACT,
    enhancement_review_payload,
    generate_targeted_enhancement,
    identify_affected_chunks,
    preserve_authoritative_existing_lines,
    remove_redundant_new_wrapper_forms,
    remove_duplicate_enhancement_declarations,
    repair_missing_select_target_structure_components,
    repair_orphan_enhancement_declarations,
    reconcile_enhancement_chunk_replacements,
    remove_required_start_of_selection_performs,
    render_enhance_prompt,
    run_enhance_abap,
    split_existing_program_chunks,
    validate_enhancement_structure,
)
from services.create_abap import restore_unrelated_select_endselect_blocks
from services.progress import create_job, get_progress


class EnhanceAbapFlowTest(unittest.TestCase):
    def test_home_uses_tabbed_workflow_layout(self):
        app = create_app({"TESTING": True})

        response = app.test_client().get("/")

        self.assertEqual(response.status_code, 200)
        page = response.data.decode("utf-8")
        self.assertIn('class="tab-view"', page)
        self.assertIn("New Program", page)
        self.assertIn("Enhance Program", page)
        self.assertIn('name="job_title"', page)
        self.assertIn('name="job_title" type="text" maxlength="120" required', page)
        self.assertIn("Upload Meta Cache", page)
        self.assertIn("ABAP Metadata Export Utility", page)
        self.assertIn('id="copy-metadata-export"', page)
        self.assertIn("Copied to clipboard.", page)
        self.assertIn("REPORT zabap_builder_json_generator", page)
        self.assertNotIn('class="mode-grid"', page)

    def test_enhance_upload_saves_inputs_starts_job_and_redirects(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "enhance_existing_abap.txt"
            prompt_path.write_text("Enhance.", encoding="utf-8")
            app = create_app(
                {
                    "TESTING": True,
                    "UPLOAD_FOLDER": str(uploads_folder),
                    "JOBS_FOLDER": str(jobs_folder),
                    "ENHANCE_ABAP_PROMPT": str(prompt_path),
                }
            )

            with patch("app.start_enhance_abap_job") as starter:
                response = app.test_client().post(
                    "/enhance",
                    data={
                        "job_title": "Holiday upload enhancement",
                        "existing_abap_file": (BytesIO(b"REPORT zold."), "zold.abap"),
                        "enhancement_specification": "Add an ALV output.",
                        "sap_syntax_check_attempts": "2",
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].startswith("/progress/"))
            job_id = response.headers["Location"].rsplit("/", 1)[-1]
            self.assertEqual((uploads_folder / job_id / "zold.abap").read_text(encoding="utf-8"), "REPORT zold.")
            self.assertEqual(
                (uploads_folder / job_id / "enhancement_specification.txt").read_text(encoding="utf-8"),
                "Add an ALV output.",
            )
            options = json.loads((jobs_folder / job_id / "options.json").read_text(encoding="utf-8"))
            self.assertEqual(options["job_title"], "Holiday upload enhancement")
            starter.assert_called_once()
            self.assertEqual(starter.call_args.kwargs["job_id"], job_id)
            self.assertEqual(starter.call_args.kwargs["prompt_path"], prompt_path)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_enhance_upload_requires_functional_specification(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            app = create_app(
                {
                    "TESTING": True,
                    "UPLOAD_FOLDER": str(temp_path / "uploads"),
                    "JOBS_FOLDER": str(temp_path / "jobs"),
                }
            )

            response = app.test_client().post(
                "/enhance",
                data={
                    "job_title": "Missing spec job",
                    "existing_abap_file": (BytesIO(b"REPORT zold."), "zold.abap"),
                },
                content_type="multipart/form-data",
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Enter the enhancement specification.", response.data.decode("utf-8"))
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_enhance_upload_requires_job_title(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            app = create_app(
                {
                    "TESTING": True,
                    "UPLOAD_FOLDER": str(temp_path / "uploads"),
                    "JOBS_FOLDER": str(temp_path / "jobs"),
                }
            )

            response = app.test_client().post(
                "/enhance",
                data={
                    "existing_abap_file": (BytesIO(b"REPORT zold."), "zold.abap"),
                    "enhancement_specification": "Add an ALV output.",
                },
                content_type="multipart/form-data",
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Enter a job title.", response.data.decode("utf-8"))
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_run_enhance_abap_reuses_shared_artifacts_and_marks_mode(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            source_path = uploads_folder / "job" / "zold.abap"
            specification_path = uploads_folder / "job" / "enhancement.txt"
            prompt_path = temp_path / "enhance_existing_abap.txt"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(
                "\n".join(
                    [
                        "REPORT zold.",
                        "START-OF-SELECTION.",
                        "  WRITE: / 'Old'.",
                    ]
                ),
                encoding="utf-8",
            )
            specification_path.write_text("Write a new line after the existing output.", encoding="utf-8")
            prompt_path.write_text(
                "Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}",
                encoding="utf-8",
            )
            job_id = create_job(jobs_folder)

            def generator(prompt_text, source_text):
                self.assertIn("Write a new line", prompt_text)
                self.assertIn("REPORT zold.", prompt_text)
                self.assertIn("Existing ABAP chunk", source_text)
                return {
                    "text": "\n".join(
                        [
                            "REPORT zold.",
                            "START-OF-SELECTION.",
                            "  WRITE: / 'Old'.",
                            "  WRITE: / 'New'.",
                        ]
                    ),
                    "model": "test-model",
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                }

            run_enhance_abap(
                job_id,
                source_path,
                specification_path,
                jobs_folder,
                prompt_path,
                enhancement_generator=generator,
            )

            generated = (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8")
            metrics = json.loads((jobs_folder / job_id / "metrics.json").read_text(encoding="utf-8"))
            dependency_analysis = json.loads((jobs_folder / job_id / "dependency_analysis.json").read_text(encoding="utf-8"))
            progress = get_progress(jobs_folder, job_id)

            self.assertIn("WRITE: / 'New'.", generated)
            self.assertEqual(metrics["job_mode"], "enhance_existing_abap")
            self.assertEqual(dependency_analysis["job_mode"], "enhance_existing_abap")
            self.assertTrue((jobs_folder / job_id / "enhancement_diagnostics.json").exists())
            self.assertTrue((jobs_folder / job_id / "fix_summary.json").exists())
            self.assertEqual(progress["status"], "Complete")
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_enhance_upload_pauses_for_change_approval_and_approval_resumes_results(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "enhance_existing_abap.txt"
            prompt_path.write_text("Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}", encoding="utf-8")

            def generator(prompt_text, source_text):
                self.assertIn("Add one output line.", prompt_text)
                self.assertIn("Existing ABAP chunk", source_text)
                return {
                    "text": "\n".join(
                        [
                            "REPORT zold.",
                            "START-OF-SELECTION.",
                            "  WRITE: / 'Old'.",
                            "  WRITE: / 'Approved'.",
                        ]
                    ),
                    "model": "test-model",
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                }

            with patch("services.enhance_abap.generate_code_review_repair", side_effect=generator):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "ENHANCE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()

                upload = client.post(
                    "/enhance",
                    data={
                        "job_title": "Approval enhancement",
                        "existing_abap_file": (
                            BytesIO(b"REPORT zold.\nSTART-OF-SELECTION.\n  WRITE: / 'Old'."),
                            "zold.abap",
                        ),
                        "enhancement_specification": "Add one output line.",
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )
                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                paused = wait_for_status(jobs_folder, job_id, "Awaiting Review", timeout=15)

                self.assertEqual(paused["current_stage"], "awaiting_enhancement_review")
                self.assertTrue((jobs_folder / job_id / ENHANCEMENT_PROPOSAL_ARTIFACT).exists())
                self.assertFalse((jobs_folder / job_id / "generated.abap").exists())

                status = client.get(f"/progress/{job_id}/status")
                self.assertIn(f"/enhancement-review/{job_id}", status.json["review_url"])
                self.assertEqual(status.json["review_label"], "Review proposed changes")

                review = client.get(f"/enhancement-review/{job_id}")
                self.assertEqual(review.status_code, 200)
                self.assertIn(b"Proposed Changes", review.data)
                self.assertIn(b"Technical Diff", review.data)
                self.assertNotIn(b"<summary>Proposed Diff</summary>", review.data)
                self.assertNotIn(b"<details class=\"code-panel\" open>\n          <summary>Technical Diff</summary>", review.data)
                self.assertIn(b"Approve changes", review.data)

                approve = client.post(f"/enhancement-review/{job_id}", data={"action": "approve"}, follow_redirects=False)
                self.assertEqual(approve.status_code, 302)
                completed = wait_for_status(jobs_folder, job_id, "Complete", timeout=15)

            self.assertTrue((jobs_folder / job_id / APPROVED_ENHANCEMENT_ARTIFACT).exists())
            self.assertEqual(completed["status"], "Complete")
            self.assertIn("WRITE: / 'Approved'.", (jobs_folder / job_id / "generated.abap").read_text(encoding="utf-8"))
            metrics = json.loads((jobs_folder / job_id / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["model"], "test-model")
            self.assertEqual(metrics["total_tokens"], 15)
            self.assertTrue(metrics["cost_breakdown"]["by_stage"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_enhancement_review_payload_summarizes_detected_changes_generically(self):
        original = "\n".join(
            [
                "REPORT ztest.",
                "TYPES: BEGIN OF ty_output,",
                "         old_field TYPE string,",
                "       END OF ty_output.",
                "DATA t_output TYPE STANDARD TABLE OF ty_output.",
                "FORM read_data.",
                "  SELECT old_field",
                "    FROM ztable",
                "    INTO TABLE t_output.",
                "ENDFORM.",
                "FORM display_data.",
                "  WRITE old_field.",
                "ENDFORM.",
            ]
        )
        proposed = "\n".join(
            [
                "REPORT ztest.",
                "TYPES: BEGIN OF ty_output,",
                "         old_field TYPE string,",
                "         new_field TYPE string,",
                "       END OF ty_output.",
                "DATA t_output TYPE STANDARD TABLE OF ty_output.",
                "DATA t_extra TYPE STANDARD TABLE OF string.",
                "FORM read_data.",
                "  SELECT old_field",
                "         new_field",
                "    FROM ztable",
                "    INTO TABLE t_output.",
                "ENDFORM.",
                "FORM display_data.",
                "  WRITE new_field.",
                "ENDFORM.",
            ]
        )

        payload = enhancement_review_payload(original, proposed, "Add the requested output field.")

        self.assertEqual(
            payload["proposed_changes"],
            [
                {
                    "category": "Data Structures",
                    "descriptions": ["Added field NEW_FIELD to structure TY_OUTPUT."],
                },
                {
                    "category": "Declarations",
                    "descriptions": ["Added internal table T_EXTRA."],
                },
                {
                    "category": "Database Reads",
                    "descriptions": ["Added field NEW_FIELD to SELECT from table ZTABLE."],
                },
                {
                    "category": "Output / ALV",
                    "descriptions": ["Added output field NEW_FIELD.", "Removed output field OLD_FIELD."],
                },
            ],
        )
        summary_text = json.dumps(payload["proposed_changes"])
        self.assertIn("NEW_FIELD", summary_text)
        self.assertIn("TY_OUTPUT", summary_text)
        self.assertIn("T_EXTRA", summary_text)
        self.assertIn("ZTABLE", summary_text)
        self.assertIn("+         new_field TYPE string,", payload["diff"])

    def test_enhancement_review_payload_names_selection_calls_forms_and_file_objects(self):
        original = "\n".join(
            [
                "REPORT ztest.",
                "FORM run_process.",
                "  DATA w_count TYPE i.",
                "ENDFORM.",
            ]
        )
        proposed = "\n".join(
            [
                "REPORT ztest.",
                "PARAMETERS p_flag TYPE c.",
                "SELECT-OPTIONS s_date FOR sy-datum.",
                "FORM run_process.",
                "  DATA w_count TYPE i.",
                "  w_count = w_count + 1.",
                "  CALL FUNCTION 'Z_DO_WORK'.",
                "  zcl_worker=>run( ).",
                "  OPEN DATASET p_file FOR OUTPUT IN TEXT MODE.",
                "ENDFORM.",
            ]
        )

        payload = enhancement_review_payload(original, proposed, "Add generic processing support.")
        changes_by_category = {
            item["category"]: item["descriptions"]
            for item in payload["proposed_changes"]
        }

        self.assertEqual(
            changes_by_category["Selection Screen"],
            ["Added parameter P_FLAG.", "Added select-option S_DATE."],
        )
        self.assertIn("Updated FORM RUN_PROCESS.", changes_by_category["Processing Logic"])
        self.assertIn("Added function module call Z_DO_WORK.", changes_by_category["Function/Method Calls"])
        self.assertIn("Added method call ZCL_WORKER=>RUN.", changes_by_category["Function/Method Calls"])
        self.assertIn("Updated file handling for P_FILE.", changes_by_category["File Handling"])

    def test_enhancement_reject_does_not_generate_result(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "enhance_existing_abap.txt"
            prompt_path.write_text("Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}", encoding="utf-8")

            with patch(
                "services.enhance_abap.generate_code_review_repair",
                return_value={"text": "REPORT zold.", "model": "test-model", "usage": None},
            ):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "ENHANCE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/enhance",
                    data={
                        "job_title": "Rejected enhancement",
                        "existing_abap_file": (BytesIO(b"REPORT zold."), "zold.abap"),
                        "enhancement_specification": "Keep it simple.",
                    },
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )
                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Awaiting Review")

                reject = client.post(f"/enhancement-review/{job_id}", data={"action": "reject"}, follow_redirects=False)

            self.assertEqual(reject.status_code, 302)
            self.assertEqual(get_progress(jobs_folder, job_id)["status"], "Rejected")
            self.assertFalse((jobs_folder / job_id / "generated.abap").exists())
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_render_enhance_prompt_replaces_inputs(self):
        rendered = render_enhance_prompt(
            "{{FUNCTIONAL_SPECIFICATION}}\n---\n{{EXISTING_ABAP}}",
            "REPORT zold.",
            "Add output.",
        )

        self.assertIn("Add output.", rendered)
        self.assertIn("REPORT zold.", rendered)
        self.assertNotIn("{{", rendered)

    def test_enhancement_prompt_requires_minimal_statement_preservation(self):
        prompt = Path("prompts/enhance_existing_abap.txt").read_text(encoding="utf-8")

        self.assertIn("Make only the minimum source changes required", prompt)
        self.assertIn("Do not regenerate an entire existing statement when only part", prompt)
        self.assertIn("keep every unaffected line of that statement exactly as-is", prompt)
        self.assertIn("Apply the same minimal-edit rule generically", prompt)
        self.assertIn("If the required table or internal table is already being read", prompt)
        self.assertIn("Do not create a second SELECT, READ TABLE, or lookup", prompt)
        self.assertIn("Only create new logic when no appropriate existing statement", prompt)
        self.assertIn("Treat the existing source as authoritative", prompt)
        self.assertIn("Do not rename existing variables, change existing types", prompt)
        self.assertIn("An existing line may only be changed", prompt)

    def test_small_enhancement_preserves_unaffected_statement_lines_exactly(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            source_path = uploads_folder / "job" / "zselect.abap"
            specification_path = uploads_folder / "job" / "enhancement.txt"
            prompt_path = temp_path / "enhance_existing_abap.txt"
            source_path.parent.mkdir(parents=True)
            original = "\n".join(
                [
                    "REPORT zselect.",
                    "",
                    "FORM read_idoc.",
                    "  SELECT docnum",
                    "         mestyp",
                    "    FROM edidc",
                    "    INTO TABLE t_edidc",
                    "    WHERE docnum IN s_docnum",
                    "      AND status = p_status.",
                    "ENDFORM.",
                ]
            )
            source_path.write_text(original, encoding="utf-8")
            specification_path.write_text(
                "Add EDIDC-CREDAT to the existing SELECT field list.",
                encoding="utf-8",
            )
            prompt_path.write_text(Path("prompts/enhance_existing_abap.txt").read_text(encoding="utf-8"), encoding="utf-8")

            def generator(prompt_text, _source_text):
                self.assertIn("Do not regenerate an entire existing statement when only part", prompt_text)
                self.assertIn("keep every unaffected line of that statement exactly as-is", prompt_text)
                return {
                    "text": "\n".join(
                        [
                            "FORM read_idoc.",
                            "  SELECT docnum",
                            "         mestyp",
                            "         credat",
                            "    FROM edidc",
                            "    INTO TABLE t_edidc",
                            "    WHERE docnum IN s_docnum",
                            "      AND status = p_status.",
                            "ENDFORM.",
                        ]
                    ),
                    "model": "test-model",
                    "usage": None,
                }

            run_enhance_abap(
                "job",
                source_path,
                specification_path,
                jobs_folder,
                prompt_path,
                enhancement_generator=generator,
                enhancement_review_required=False,
            )

            generated_lines = (jobs_folder / "job" / "generated.abap").read_text(encoding="utf-8").splitlines()
            original_lines = original.splitlines()
            self.assertEqual(generated_lines[:5], original_lines[:5])
            self.assertEqual(generated_lines[6:], original_lines[5:])
            self.assertEqual(generated_lines[5], "         credat")
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_authoritative_existing_lines_restore_unrelated_declaration_and_statement_changes(self):
        original = "\n".join(
            [
                "FORM process_data.",
                "  DATA w_count TYPE i.",
                "  WRITE: / 'Existing heading'.",
                "  WRITE: / 'Old customer'.",
                "ENDFORM.",
            ]
        )
        updated = "\n".join(
            [
                "FORM process_data.",
                "  DATA w_count TYPE string.",
                "  WRITE: / 'Corrected heading'.",
                "  WRITE: / 'New customer'.",
                "ENDFORM.",
            ]
        )

        preserved = preserve_authoritative_existing_lines(
            original,
            updated,
            "Change Old customer to New customer.",
        )

        self.assertIn("  DATA w_count TYPE i.", preserved)
        self.assertIn("  WRITE: / 'Existing heading'.", preserved)
        self.assertIn("  WRITE: / 'New customer'.", preserved)
        self.assertNotIn("TYPE string", preserved)
        self.assertNotIn("Corrected heading", preserved)

    def test_run_enhance_abap_preserves_unrelated_existing_declarations_and_statements(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            source_path = uploads_folder / "job" / "zpreserve.abap"
            specification_path = uploads_folder / "job" / "enhancement.txt"
            prompt_path = temp_path / "enhance_existing_abap.txt"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(
                "\n".join(
                    [
                        "REPORT zpreserve.",
                        "",
                        "FORM process_data.",
                        "  DATA w_count TYPE i.",
                        "  WRITE: / 'Existing heading'.",
                        "  WRITE: / 'Old customer'.",
                        "ENDFORM.",
                    ]
                ),
                encoding="utf-8",
            )
            specification_path.write_text("Change Old customer to New customer.", encoding="utf-8")
            prompt_path.write_text("Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}", encoding="utf-8")

            def generator(_prompt_text, source_text):
                return {
                    "text": "\n".join(
                        [
                            "FORM process_data.",
                            "  DATA w_count TYPE string.",
                            "  WRITE: / 'Corrected heading'.",
                            "  WRITE: / 'New customer'.",
                            "ENDFORM.",
                        ]
                    ),
                    "model": "test-model",
                    "usage": None,
                }

            run_enhance_abap(
                "job",
                source_path,
                specification_path,
                jobs_folder,
                prompt_path,
                enhancement_generator=generator,
                enhancement_review_required=False,
            )

            generated = (jobs_folder / "job" / "generated.abap").read_text(encoding="utf-8")
            self.assertIn("  DATA w_count TYPE i.", generated)
            self.assertIn("  WRITE: / 'Existing heading'.", generated)
            self.assertIn("  WRITE: / 'New customer'.", generated)
            self.assertNotIn("TYPE string", generated)
            self.assertNotIn("Corrected heading", generated)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_duplicate_select_for_existing_table_is_merged_into_existing_select(self):
        source = "\n".join(
            [
                "REPORT zreuse.",
                "",
                "FORM read_customers.",
                "  SELECT kunnr",
                "         name1",
                "    FROM kna1",
                "    INTO TABLE t_kna1",
                "    WHERE kunnr IN s_kunnr.",
                "ENDFORM.",
            ]
        )

        def generator(_prompt_text, source_text):
            return {
                "text": source_text.replace(
                    "ENDFORM.",
                    "\n  SELECT SINGLE sortl\n    FROM kna1\n    INTO w_sortl\n    WHERE kunnr = w_kunnr.\nENDFORM.",
                ),
                "model": "test-model",
                "usage": None,
            }

        chunks = split_existing_program_chunks(source)
        affected = identify_affected_chunks(chunks, "Add KNA1-SORTL to read_customers.")
        result = generate_targeted_enhancement(
            original_source=source,
            chunks=chunks,
            affected_chunks=affected,
            prompt_template="Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}",
            enhancement_specification="Add KNA1-SORTL to read_customers.",
            generator=generator,
        )

        self.assertIn("         sortl\n    FROM kna1", result["text"])
        self.assertEqual(result["text"].lower().count("from kna1"), 1)
        self.assertNotIn("SELECT SINGLE sortl", result["text"])
        self.assertIn("    INTO TABLE t_kna1", result["text"])
        self.assertIn("    WHERE kunnr IN s_kunnr.", result["text"])

    def test_new_lookup_is_kept_for_genuinely_new_data_source(self):
        source = "\n".join(
            [
                "REPORT zreuse.",
                "",
                "FORM read_materials.",
                "  SELECT matnr",
                "    FROM mara",
                "    INTO TABLE t_mara",
                "    WHERE matnr IN s_matnr.",
                "ENDFORM.",
            ]
        )

        def generator(_prompt_text, source_text):
            return {
                "text": source_text.replace(
                    "ENDFORM.",
                    "\n  SELECT werks\n    FROM marc\n    INTO TABLE t_marc\n    WHERE matnr IN s_matnr.\nENDFORM.",
                ),
                "model": "test-model",
                "usage": None,
            }

        chunks = split_existing_program_chunks(source)
        affected = identify_affected_chunks(chunks, "Add plant data from MARC in read_materials.")
        result = generate_targeted_enhancement(
            original_source=source,
            chunks=chunks,
            affected_chunks=affected,
            prompt_template="Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}",
            enhancement_specification="Add plant data from MARC in read_materials.",
            generator=generator,
        )

        self.assertEqual(result["text"].lower().count("from mara"), 1)
        self.assertEqual(result["text"].lower().count("from marc"), 1)
        self.assertIn("  SELECT werks\n    FROM marc", result["text"])

    def test_duplicate_select_cleanup_preserves_unaffected_routines(self):
        source = "\n".join(
            [
                "REPORT zreuse.",
                "",
                "FORM read_customers.",
                "  SELECT kunnr",
                "         name1",
                "    FROM kna1",
                "    INTO TABLE t_kna1",
                "    WHERE kunnr IN s_kunnr.",
                "ENDFORM.",
                "",
                "FORM display_customers.",
                "  WRITE: / 'unchanged'.",
                "ENDFORM.",
            ]
        )
        unaffected = "FORM display_customers.\n  WRITE: / 'unchanged'.\nENDFORM."

        def generator(_prompt_text, source_text):
            self.assertNotIn("FORM display_customers.", source_text)
            return {
                "text": source_text.replace(
                    "ENDFORM.",
                    "\n  SELECT SINGLE sortl\n    FROM kna1\n    INTO w_sortl\n    WHERE kunnr = w_kunnr.\nENDFORM.",
                ),
                "model": "test-model",
                "usage": None,
            }

        chunks = split_existing_program_chunks(source)
        affected = identify_affected_chunks(chunks, "Add KNA1-SORTL in read_customers.")
        result = generate_targeted_enhancement(
            original_source=source,
            chunks=chunks,
            affected_chunks=affected,
            prompt_template="Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}",
            enhancement_specification="Add KNA1-SORTL in read_customers.",
            generator=generator,
        )

        self.assertIn(unaffected, result["text"])
        self.assertEqual(result["text"].lower().count("from kna1"), 1)

    def test_alv_only_output_enhancement_does_not_select_customer_or_file_chunks(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "TYPES: BEGIN OF ty_pa0002,",
                "         pernr TYPE pernr_d,",
                "         nachn TYPE pad_nachn,",
                "       END OF ty_pa0002.",
                "TYPES: BEGIN OF ty_customer_data,",
                "         name_last TYPE string,",
                "       END OF ty_customer_data.",
                "TYPES: BEGIN OF ty_output,",
                "         nachn TYPE string,",
                "       END OF ty_output.",
                "DATA: st_customer_data TYPE ty_customer_data,",
                "      st_output TYPE ty_output.",
                "FORM define_field_catalog.",
                "  st_alv_fieldcat-fieldname = 'NACHN'.",
                "  st_alv_fieldcat-seltext_l = 'Surname'.",
                "ENDFORM.",
                "FORM read_pa0002.",
                "  SELECT pernr nachn INTO TABLE t_pa0002 FROM pa0002.",
                "ENDFORM.",
                "FORM define_customer_data.",
                "  st_customer_data-name_last = st_pa0002-nachn.",
                "ENDFORM.",
                "FORM build_file_content.",
                "  CONCATENATE st_output-nachn INTO st_content SEPARATED BY ','.",
                "ENDFORM.",
                "FORM check_pernr_name_change.",
                "  PERFORM change_cell USING 'NACHN'.",
                "ENDFORM.",
            ]
        )
        spec = (
            "For the read from PA0002 also extract GBDAT. This is only required on the ALV report "
            "and should be output as GBDAT - Date of Birth. It needs to be placed after the surname. "
            "Do not make any other changes to functionality. Amend the existing SQL read of PA0002. "
            "Do not create new SQL for a read of PA0002."
        )

        chunks = split_existing_program_chunks(source)
        affected = identify_affected_chunks(chunks, spec)
        ids = [chunk["id"] for chunk in affected]

        self.assertIn("global", ids)
        self.assertIn("form:read_pa0002", ids)
        self.assertIn("form:define_field_catalog", ids)
        self.assertNotIn("form:define_customer_data", ids)
        self.assertNotIn("form:build_file_content", ids)
        self.assertNotIn("form:check_pernr_name_change", ids)

    def test_package_select_conversion_keeps_read_data_as_owner(self):
        source = "\n".join(
            [
                "REPORT zpkg.",
                "TABLES: kna1.",
                "TYPES: BEGIN OF ty_data,",
                "         kunnr TYPE kunnr,",
                "         zcust_guid TYPE zcust_guid,",
                "       END OF ty_data.",
                "DATA: t_data TYPE STANDARD TABLE OF ty_data.",
                "DATA: st_data TYPE ty_data.",
                "SELECT-OPTIONS s_kunnr FOR kna1-kunnr.",
                "START-OF-SELECTION.",
                "  PERFORM read_data.",
                "  PERFORM process_data.",
                "END-OF-SELECTION.",
                "  PERFORM output_report.",
                "FORM process_data.",
                "  LOOP AT t_data INTO st_data.",
                "  ENDLOOP.",
                "ENDFORM.",
                "FORM output_report.",
                "  WRITE: / 'done'.",
                "ENDFORM.",
                "FORM read_data.",
                "  SELECT kunnr",
                "         INTO TABLE t_data",
                "         FROM kna1",
                "         WHERE kunnr IN s_kunnr",
                "           AND zcust_guid EQ space.",
                "ENDFORM.",
            ]
        )
        spec = (
            "Add select-option S_KTOKD based on KNA1-KTOKD. "
            "Change the KNA1 read to use PACKAGE SIZE 1000. "
            "Create T_DATA_TEMP with the same line type as T_DATA. "
            "For each package returned from KNA1: clear T_DATA; loop through T_DATA_TEMP; append valid records. "
            "After T_DATA has been populated for the current package, call PROCESS_DATA. "
            "Remove the existing standalone PERFORM PROCESS_DATA from START-OF-SELECTION. "
            "Reuse the existing READ_DATA and PROCESS_DATA logic."
        )

        def generator(_prompt_text, source_text):
            if "START-OF-SELECTION." in source_text:
                return {
                    "text": "\n".join(
                        [
                            "REPORT zpkg.",
                            "TABLES: kna1.",
                            "TYPES: BEGIN OF ty_data,",
                            "         kunnr TYPE kunnr,",
                            "         ktokd TYPE ktokd,",
                            "         zcust_guid TYPE zcust_guid,",
                            "       END OF ty_data.",
                            "DATA: t_data TYPE STANDARD TABLE OF ty_data,",
                            "      t_data_temp TYPE STANDARD TABLE OF ty_data.",
                            "DATA: st_data TYPE ty_data.",
                            "SELECT-OPTIONS s_kunnr FOR kna1-kunnr.",
                            "SELECT-OPTIONS s_ktokd FOR kna1-ktokd DEFAULT 'ZCST'.",
                            "START-OF-SELECTION.",
                            "  PERFORM read_data.",
                            "  CLEAR t_data_temp.",
                            "  SELECT kunnr ktokd zcust_guid",
                            "    INTO TABLE t_data_temp PACKAGE SIZE 1000",
                            "    FROM kna1",
                            "    WHERE zcust_guid EQ space.",
                            "    CLEAR t_data.",
                            "    PERFORM process_data.",
                            "  ENDSELECT.",
                            "END-OF-SELECTION.",
                            "  PERFORM output_report.",
                        ]
                    ),
                    "model": "test-model",
                    "usage": None,
                }
            if "FORM read_data." in source_text:
                return {
                    "text": "\n".join(
                        [
                            "FORM read_data.",
                            "  SELECT kunnr",
                            "         ktokd",
                            "         zcust_guid",
                            "         INTO TABLE t_data_temp",
                            "         PACKAGE SIZE 1000",
                            "         FROM kna1",
                            "         WHERE zcust_guid EQ space.",
                            "    CLEAR t_data.",
                            "    LOOP AT t_data_temp INTO st_data.",
                            "      IF st_data-kunnr IN s_kunnr",
                            "         AND st_data-ktokd IN s_ktokd",
                            "         AND st_data-zcust_guid EQ space.",
                            "        APPEND st_data TO t_data.",
                            "      ENDIF.",
                            "    ENDLOOP.",
                            "    PERFORM process_data.",
                            "    CLEAR t_data_temp.",
                            "  ENDSELECT.",
                            "ENDFORM.",
                        ]
                    ),
                    "model": "test-model",
                    "usage": None,
                }
            return {"text": source_text, "model": "test-model", "usage": None}

        chunks = split_existing_program_chunks(source)
        affected = identify_affected_chunks(chunks, spec)
        result = generate_targeted_enhancement(
            original_source=source,
            chunks=chunks,
            affected_chunks=affected,
            prompt_template="Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}",
            enhancement_specification=spec,
            generator=generator,
        )

        self.assertEqual(result["text"].lower().count("from kna1"), 1)
        self.assertIn("FORM read_data.", result["text"])
        self.assertIn("PACKAGE SIZE 1000", result["text"])
        self.assertNotIn("INTO TABLE t_data\n         ktokd", result["text"])
        self.assertNotIn("PERFORM process_data.\nEND-OF-SELECTION", result["text"])
        issues = validate_enhancement_structure(source, result["text"], enhancement_specification=spec)
        self.assertFalse([issue for issue in issues if issue["rule_id"] == "ENHANCEMENT_CLEARS_PREPOPULATED_TABLE"])

    def test_structural_validation_flags_missing_new_definition(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "DATA w_existing TYPE c.",
                "START-OF-SELECTION.",
                "  w_existing = 'X'.",
            ]
        )
        final = original + "\n  w_missing = 'Y'."

        issues = validate_enhancement_structure(original, final)

        self.assertTrue(
            [
                issue
                for issue in issues
                if issue["rule_id"] == "ENHANCEMENT_MISSING_DEFINITION" and issue.get("identifier") == "w_missing"
            ]
        )

    def test_structural_validation_flags_duplicate_new_definitions(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "DATA w_existing TYPE c.",
                "FORM process_data.",
                "ENDFORM.",
            ]
        )
        final = "\n".join(
            [
                "REPORT zstruct.",
                "DATA w_existing TYPE c.",
                "DATA w_existing TYPE c.",
                "FORM process_data.",
                "ENDFORM.",
                "FORM process_data.",
                "ENDFORM.",
            ]
        )

        issues = validate_enhancement_structure(original, final)
        duplicate_messages = [issue["message"] for issue in issues if issue["rule_id"] == "ENHANCEMENT_DUPLICATE_DEFINITION"]

        self.assertTrue(any("w_existing" in message for message in duplicate_messages))
        self.assertTrue(any("process_data" in message for message in duplicate_messages))

    def test_duplicate_existing_declaration_exact_text_is_removed(self):
        original = "\n".join(["REPORT zstruct.", "DATA w_existing TYPE c.", "START-OF-SELECTION."])
        final = "\n".join(
            [
                "REPORT zstruct.",
                "DATA w_existing TYPE c.",
                "START-OF-SELECTION.",
                "DATA w_existing TYPE c.",
            ]
        )

        cleaned, issues = remove_duplicate_enhancement_declarations(original, final)

        self.assertEqual(cleaned.count("DATA w_existing TYPE c."), 1)
        self.assertEqual(issues, [])

    def test_duplicate_existing_declaration_different_case_is_removed(self):
        original = "\n".join(["REPORT zstruct.", "DATA w_existing TYPE c.", "START-OF-SELECTION."])
        final = "\n".join(
            [
                "REPORT zstruct.",
                "DATA w_existing TYPE c.",
                "START-OF-SELECTION.",
                "data W_EXISTING type c.",
            ]
        )

        cleaned, issues = remove_duplicate_enhancement_declarations(original, final)

        self.assertIn("DATA w_existing TYPE c.", cleaned)
        self.assertNotIn("data W_EXISTING type c.", cleaned)
        self.assertEqual(issues, [])

    def test_duplicate_existing_declaration_different_whitespace_is_removed(self):
        original = "\n".join(["REPORT zstruct.", "DATA w_existing TYPE c.", "START-OF-SELECTION."])
        final = "\n".join(
            [
                "REPORT zstruct.",
                "DATA w_existing TYPE c.",
                "START-OF-SELECTION.",
                "DATA    w_existing    TYPE    c.",
            ]
        )

        cleaned, issues = remove_duplicate_enhancement_declarations(original, final)

        self.assertIn("DATA w_existing TYPE c.", cleaned)
        self.assertNotIn("DATA    w_existing    TYPE    c.", cleaned)
        self.assertEqual(issues, [])

    def test_duplicate_existing_chained_declaration_continuation_is_removed(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "DATA: t_content         TYPE soli_tab,",
                "      t_open_item_value TYPE STANDARD TABLE OF ty_open_item_value.",
                "START-OF-SELECTION.",
            ]
        )
        final = "\n".join(
            [
                "REPORT zstruct.",
                "DATA: t_content         TYPE soli_tab,",
                "      t_open_item_value TYPE STANDARD TABLE OF ty_open_item_value.",
                "      t_open_item_value      type standard table of ty_open_item_value,",
                "      t_zmd_cst0044          TYPE STANDARD TABLE OF zmd_cst0044.",
                "START-OF-SELECTION.",
            ]
        )

        cleaned, issues = remove_duplicate_enhancement_declarations(original, final)

        self.assertIn("      t_open_item_value TYPE STANDARD TABLE OF ty_open_item_value.", cleaned)
        self.assertNotIn("      t_open_item_value      type standard table of ty_open_item_value,", cleaned)
        self.assertIn("      t_zmd_cst0044          TYPE STANDARD TABLE OF zmd_cst0044.", cleaned)
        self.assertEqual(issues, [])

    def test_conflicting_duplicate_existing_declaration_type_is_removed_and_reported(self):
        original = "\n".join(["REPORT zstruct.", "DATA w_existing TYPE c.", "START-OF-SELECTION."])
        final = "\n".join(
            [
                "REPORT zstruct.",
                "DATA w_existing TYPE c.",
                "START-OF-SELECTION.",
                "DATA w_existing TYPE string.",
            ]
        )

        cleaned, issues = remove_duplicate_enhancement_declarations(original, final)

        self.assertIn("DATA w_existing TYPE c.", cleaned)
        self.assertNotIn("DATA w_existing TYPE string.", cleaned)
        self.assertTrue(
            [
                issue
                for issue in issues
                if issue["rule_id"] == "ENHANCEMENT_CONFLICTING_DUPLICATE_DECLARATION"
                and issue.get("definition") == "w_existing"
            ]
        )

    def test_genuinely_new_declaration_remains_unchanged(self):
        original = "\n".join(["REPORT zstruct.", "DATA w_existing TYPE c.", "START-OF-SELECTION."])
        final = "\n".join(
            [
                "REPORT zstruct.",
                "DATA w_existing TYPE c.",
                "DATA t_new_items TYPE STANDARD TABLE OF zitem.",
                "START-OF-SELECTION.",
            ]
        )

        cleaned, issues = remove_duplicate_enhancement_declarations(original, final)

        self.assertIn("DATA t_new_items TYPE STANDARD TABLE OF zitem.", cleaned)
        self.assertEqual(issues, [])

    def test_orphan_generated_declaration_continuation_is_repaired_as_standalone_data(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "DATA: t_existing TYPE STANDARD TABLE OF ty_existing.",
                "FORM process_data.",
                "ENDFORM.",
            ]
        )
        final = "\n".join(
            [
                "REPORT zstruct.",
                "DATA: t_existing TYPE STANDARD TABLE OF ty_existing.",
                "      t_new TYPE STANDARD TABLE OF ztable.",
                "FORM process_data.",
                "      w_new TYPE ztable,",
                "ENDFORM.",
            ]
        )

        repaired = repair_orphan_enhancement_declarations(original, final)

        self.assertIn("DATA: t_new TYPE STANDARD TABLE OF ztable.", repaired)
        self.assertIn("DATA: w_new TYPE ztable.", repaired)
        self.assertFalse(
            [
                issue
                for issue in validate_enhancement_structure(original, repaired, enhancement_specification="Add ZTABLE data.")
                if issue["rule_id"] == "ENHANCEMENT_INVALID_DECLARATION_PLACEMENT"
            ]
        )

    def test_duplicate_cleanup_then_repairs_remaining_generated_declaration(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "DATA: t_open_item_value TYPE STANDARD TABLE OF ty_open_item_value.",
            ]
        )
        final = "\n".join(
            [
                "REPORT zstruct.",
                "DATA: t_open_item_value TYPE STANDARD TABLE OF ty_open_item_value.",
                "      t_open_item_value TYPE STANDARD TABLE OF ty_open_item_value,",
                "      t_zmd_cst0044 TYPE STANDARD TABLE OF zmd_cst0044.",
            ]
        )

        cleaned, issues = remove_duplicate_enhancement_declarations(original, final)
        repaired = repair_orphan_enhancement_declarations(original, cleaned)

        self.assertEqual(issues, [])
        self.assertNotIn("      t_open_item_value TYPE STANDARD TABLE OF ty_open_item_value,", repaired)
        self.assertIn("DATA: t_zmd_cst0044 TYPE STANDARD TABLE OF zmd_cst0044.", repaired)
        self.assertFalse(
            [
                issue
                for issue in validate_enhancement_structure(original, repaired, enhancement_specification="Read ZMD_CST0044.")
                if issue["rule_id"] == "ENHANCEMENT_INVALID_DECLARATION_PLACEMENT"
            ]
        )

    def test_structural_validation_flags_orphan_new_perform(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "START-OF-SELECTION.",
                "  WRITE: / 'ready'.",
            ]
        )
        final = original + "\n  PERFORM missing_form."

        issues = validate_enhancement_structure(original, final)

        self.assertTrue(
            [
                issue
                for issue in issues
                if issue["rule_id"] == "ENHANCEMENT_ORPHAN_ROUTINE_CALL" and issue.get("routine") == "missing_form"
            ]
        )

    def test_structural_validation_flags_unavailable_new_callable(self):
        original = "\n".join(["REPORT zstruct.", "START-OF-SELECTION.", "  WRITE: / 'ready'."])
        final = original + "\n  CALL FUNCTION 'Z_MISSING_FUNCTION'."

        issues = validate_enhancement_structure(original, final, callable_metadata={})

        self.assertTrue(
            [
                issue
                for issue in issues
                if issue["rule_id"] == "ENHANCEMENT_UNAVAILABLE_CALLABLE"
                and issue.get("callable_name") == "Z_MISSING_FUNCTION"
            ]
        )

    def test_structural_validation_accepts_available_new_callable(self):
        original = "\n".join(["REPORT zstruct.", "START-OF-SELECTION.", "  WRITE: / 'ready'."])
        final = original + "\n  CALL FUNCTION 'Z_AVAILABLE_FUNCTION'."

        issues = validate_enhancement_structure(
            original,
            final,
            callable_metadata={"callable_signatures": {"Z_AVAILABLE_FUNCTION": {"parameters": {}}}},
        )

        self.assertFalse([issue for issue in issues if issue["rule_id"] == "ENHANCEMENT_UNAVAILABLE_CALLABLE"])

    def test_final_validation_flags_generated_declaration_outside_valid_statement(self):
        original = "\n".join(["REPORT zstruct.", "FORM process_data.", "ENDFORM."])
        final = "\n".join(["REPORT zstruct.", "FORM process_data.", "  st_bad TYPE ty_bad.", "ENDFORM."])

        issues = validate_enhancement_structure(original, final, enhancement_specification="Add valid data handling.")

        self.assertTrue([issue for issue in issues if issue["rule_id"] == "ENHANCEMENT_INVALID_DECLARATION_PLACEMENT"])

    def test_final_validation_flags_unknown_structure_component(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "TYPES: BEGIN OF ty_customer,",
                "         kunnr TYPE kunnr,",
                "       END OF ty_customer.",
                "DATA st_customer TYPE ty_customer.",
                "START-OF-SELECTION.",
            ]
        )
        final = original + "\n  st_customer-name1 = 'ACME'."

        issues = validate_enhancement_structure(original, final, enhancement_specification="Add customer logic.")

        self.assertTrue(
            [
                issue
                for issue in issues
                if issue["rule_id"] == "ENHANCEMENT_UNKNOWN_STRUCTURE_COMPONENT"
                and issue.get("component") == "name1"
            ]
        )

    def test_final_validation_flags_select_field_not_in_target_structure(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "TYPES: BEGIN OF ty_customer,",
                "         kunnr TYPE kunnr,",
                "       END OF ty_customer.",
                "DATA t_customers TYPE STANDARD TABLE OF ty_customer.",
                "FORM read_customers.",
                "ENDFORM.",
            ]
        )
        final = "\n".join(
            [
                "REPORT zstruct.",
                "TYPES: BEGIN OF ty_customer,",
                "         kunnr TYPE kunnr,",
                "       END OF ty_customer.",
                "DATA t_customers TYPE STANDARD TABLE OF ty_customer.",
                "FORM read_customers.",
                "  SELECT kunnr name1 FROM kna1 INTO TABLE t_customers.",
                "ENDFORM.",
            ]
        )

        issues = validate_enhancement_structure(original, final, enhancement_specification="Read customer data.")

        self.assertTrue(
            [
                issue
                for issue in issues
                if issue["rule_id"] == "ENHANCEMENT_SELECT_TARGET_MISMATCH"
                and issue.get("field") == "name1"
            ]
        )

    def test_final_validation_checks_only_new_fields_when_existing_select_is_extended(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "TYPES: BEGIN OF ty_customer,",
                "         payer TYPE kunn2,",
                "         kunnr TYPE kunnr,",
                "       END OF ty_customer.",
                "DATA t_customers TYPE STANDARD TABLE OF ty_customer.",
                "FORM read_customers.",
                "  SELECT knvp~kunn2",
                "         knb1~kunnr",
                "         INTO TABLE t_customers",
                "         FROM knb1 AS knb1",
                "         INNER JOIN knvp AS knvp",
                "         ON knvp~kunnr EQ knb1~kunnr.",
                "ENDFORM.",
            ]
        )
        final = original.replace("         knb1~kunnr", "         knb1~kunnr\n         kna1~zslsman1")

        issues = validate_enhancement_structure(original, final, enhancement_specification="Add sales manager field.")

        mismatch_fields = [
            issue.get("field")
            for issue in issues
            if issue["rule_id"] == "ENHANCEMENT_SELECT_TARGET_MISMATCH"
        ]
        self.assertNotIn("into", mismatch_fields)
        self.assertNotIn("table", mismatch_fields)
        self.assertNotIn("kunn2", mismatch_fields)
        self.assertEqual(mismatch_fields, ["zslsman1"])

    def test_generated_select_extension_repairs_missing_target_structure_component(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "TYPES: BEGIN OF ty_customers,",
                "         payer     TYPE kunn2,",
                "         kunnr     TYPE kunnr,",
                "         zhomebran TYPE zhomebran,",
                "         sortl     TYPE sortl,",
                "         name2     TYPE name2_gp,",
                "       END   OF ty_customers.",
                "DATA t_customers TYPE STANDARD TABLE OF ty_customers.",
                "DATA st_customers TYPE ty_customers.",
                "FORM read_customers.",
                "  SELECT knvp~kunn2",
                "         knb1~kunnr",
                "         knb1~zhomebran",
                "         kna1~sortl",
                "         kna1~name2",
                "         INTO TABLE t_customers",
                "         FROM knb1 AS knb1",
                "         INNER JOIN knvp AS knvp",
                "         ON knvp~kunnr EQ knb1~kunnr.",
                "ENDFORM.",
                "FORM process_customers.",
                "ENDFORM.",
            ]
        )
        final = original.replace(
            "         kna1~name2\n         INTO TABLE t_customers",
            "         kna1~name2\n         kna1~zslsman1\n         INTO TABLE t_customers",
        ).replace(
            "FORM process_customers.\nENDFORM.",
            "FORM process_customers.\n  WRITE: / st_customers-zslsman1.\nENDFORM.",
        )

        repaired = repair_missing_select_target_structure_components(original, final)

        self.assertIn("         zslsman1           TYPE kna1-zslsman1,", repaired)
        self.assertFalse(
            [
                issue
                for issue in validate_enhancement_structure(original, repaired, enhancement_specification="Add sales manager field.")
                if issue["rule_id"] in {
                    "ENHANCEMENT_UNKNOWN_STRUCTURE_COMPONENT",
                    "ENHANCEMENT_SELECT_TARGET_MISMATCH",
                }
            ]
        )

    def test_generated_select_extension_repairs_plain_field_using_select_table(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "TYPES: BEGIN OF ty_credit,",
                "         kunnr     TYPE kunnr,",
                "         zvenuslim TYPE zvenuslim,",
                "         sortl     TYPE sortl,",
                "       END   OF ty_credit.",
                "DATA t_credit TYPE STANDARD TABLE OF ty_credit.",
                "FORM read_credit_limits.",
                "  SELECT kunnr",
                "         zvenuslim",
                "         sortl",
                "         INTO TABLE t_credit",
                "         FROM kna1.",
                "ENDFORM.",
            ]
        )
        final = original.replace("         sortl\n         INTO TABLE t_credit", "         sortl\n         zslsman1\n         INTO TABLE t_credit")

        repaired = repair_missing_select_target_structure_components(original, final)

        self.assertIn("         zslsman1           TYPE kna1-zslsman1,", repaired)
        self.assertFalse(
            [
                issue
                for issue in validate_enhancement_structure(original, repaired, enhancement_specification="Add sales manager field.")
                if issue["rule_id"] == "ENHANCEMENT_SELECT_TARGET_MISMATCH"
            ]
        )

    def test_same_component_name_in_different_structures_is_not_duplicate_declaration(self):
        original = "REPORT zstruct."
        final = "\n".join(
            [
                "REPORT zstruct.",
                "TYPES: BEGIN OF ty_output,",
                "         zslsman1 TYPE kna1-zslsman1,",
                "       END OF ty_output.",
                "TYPES: BEGIN OF ty_customers,",
                "         zslsman1 TYPE kna1-zslsman1,",
                "       END OF ty_customers.",
            ]
        )

        issues = validate_enhancement_structure(original, final, enhancement_specification="Add sales manager field.")

        self.assertFalse(
            [
                issue
                for issue in issues
                if issue["rule_id"] == "ENHANCEMENT_DUPLICATE_DEFINITION"
                and issue.get("definition") == "zslsman1"
            ]
        )

    def test_final_validation_flags_called_empty_generated_routine(self):
        original = "\n".join(["REPORT zstruct.", "START-OF-SELECTION."])
        final = "\n".join(["REPORT zstruct.", "START-OF-SELECTION.", "  PERFORM build_output.", "", "FORM build_output.", "ENDFORM."])

        issues = validate_enhancement_structure(original, final, enhancement_specification="Build output.")

        self.assertTrue([issue for issue in issues if issue["rule_id"] == "ENHANCEMENT_EMPTY_CALLED_ROUTINE"])

    def test_final_validation_treats_generated_form_parameters_as_defined(self):
        original = "\n".join(["REPORT zstruct.", "START-OF-SELECTION."])
        final = "\n".join(
            [
                "REPORT zstruct.",
                "START-OF-SELECTION.",
                "  PERFORM get_customer USING st_customer-kunnr.",
                "",
                "FORM get_customer USING p_kunnr TYPE kunnr.",
                "  WRITE: / p_kunnr.",
                "ENDFORM.",
            ]
        )

        issues = validate_enhancement_structure(original, final, enhancement_specification="Add customer output.")

        self.assertFalse(
            [
                issue
                for issue in issues
                if issue["rule_id"] == "ENHANCEMENT_MISSING_DEFINITION"
                and issue.get("identifier") == "p_kunnr"
            ]
        )

    def test_final_validation_flags_clearing_previously_populated_table(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "DATA t_customers TYPE STANDARD TABLE OF kna1.",
                "FORM read_customers.",
                "  SELECT * FROM kna1 INTO TABLE t_customers.",
                "ENDFORM.",
            ]
        )
        final = original.replace("ENDFORM.", "  CLEAR t_customers[].\nENDFORM.")

        issues = validate_enhancement_structure(original, final, enhancement_specification="Reuse customer data.")

        self.assertTrue([issue for issue in issues if issue["rule_id"] == "ENHANCEMENT_CLEARS_PREPOPULATED_TABLE"])

    def test_final_validation_allows_explicitly_requested_package_table_clear(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "DATA t_data TYPE STANDARD TABLE OF kna1.",
                "FORM read_customers.",
                "  SELECT * FROM kna1 INTO TABLE t_data.",
                "ENDFORM.",
            ]
        )
        final = original.replace("ENDFORM.", "  CLEAR t_data.\nENDFORM.")

        issues = validate_enhancement_structure(
            original,
            final,
            enhancement_specification="For each package returned from KNA1: clear T_DATA; append valid records.",
        )

        self.assertFalse([issue for issue in issues if issue["rule_id"] == "ENHANCEMENT_CLEARS_PREPOPULATED_TABLE"])

    def test_final_validation_flags_required_start_of_selection_perform_removal(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "START-OF-SELECTION.",
                "  PERFORM read_data.",
                "  PERFORM process_data.",
                "END-OF-SELECTION.",
                "  PERFORM output_report.",
            ]
        )
        final = original + "\nFORM read_data.\n  PERFORM process_data.\nENDFORM."

        issues = validate_enhancement_structure(
            original,
            final,
            enhancement_specification=(
                "After T_DATA has been populated for the current package, call PROCESS_DATA. "
                "Remove the existing standalone PERFORM PROCESS_DATA from START-OF-SELECTION."
            ),
        )

        self.assertTrue(
            [issue for issue in issues if issue["rule_id"] == "ENHANCEMENT_REQUIRED_PERFORM_REMOVAL_MISSING"]
        )

    def test_required_start_of_selection_perform_removal_is_cleaned_before_validation(self):
        spec = (
            "After T_DATA has been populated for the current package, call PROCESS_DATA. "
            "Remove the existing standalone PERFORM PROCESS_DATA from START-OF-SELECTION."
        )
        source = "\n".join(
            [
                "REPORT zstruct.",
                "START-OF-SELECTION.",
                "  PERFORM read_data.",
                "",
                "  PERFORM process_data.",
                "",
                "END-OF-SELECTION.",
                "  PERFORM output_report.",
                "",
                "FORM read_data.",
                "  PERFORM process_data.",
                "ENDFORM.",
            ]
        )

        cleaned = remove_required_start_of_selection_performs(source, spec)

        self.assertIn("FORM read_data.\n  PERFORM process_data.", cleaned)
        self.assertNotIn("START-OF-SELECTION.\n  PERFORM read_data.\n\n  PERFORM process_data.", cleaned)
        issues = validate_enhancement_structure(source, cleaned, enhancement_specification=spec)
        self.assertFalse(
            [issue for issue in issues if issue["rule_id"] == "ENHANCEMENT_REQUIRED_PERFORM_REMOVAL_MISSING"]
        )
        self.assertFalse(
            [issue for issue in issues if issue["rule_id"] == "ENHANCEMENT_UNRELATED_EXISTING_CHANGE"]
        )

    def test_final_validation_flags_unrelated_existing_statement_change(self):
        original = "\n".join(["REPORT zstruct.", "START-OF-SELECTION.", "  WRITE: / 'Heading'."])
        final = "\n".join(["REPORT zstruct.", "START-OF-SELECTION.", "  WRITE: / 'Changed'."])

        issues = validate_enhancement_structure(original, final, enhancement_specification="Add customer email output.")

        self.assertTrue([issue for issue in issues if issue["rule_id"] == "ENHANCEMENT_UNRELATED_EXISTING_CHANGE"])

    def test_redundant_generated_wrapper_call_and_form_are_removed(self):
        original = "\n".join(
            [
                "REPORT zstruct.",
                "START-OF-SELECTION.",
                "  WRITE: / 'Old'.",
            ]
        )
        final = "\n".join(
            [
                "REPORT zstruct.",
                "START-OF-SELECTION.",
                "  WRITE: / 'Old'.",
                "  WRITE: / 'New'.",
                "  PERFORM add_new_output.",
                "",
                "FORM add_new_output.",
                "  WRITE: / 'New'.",
                "ENDFORM.",
            ]
        )

        cleaned = remove_redundant_new_wrapper_forms(original, final)

        self.assertIn("  WRITE: / 'New'.", cleaned)
        self.assertNotIn("PERFORM add_new_output.", cleaned)
        self.assertNotIn("FORM add_new_output.", cleaned)

    def test_run_enhance_abap_rejects_blocking_final_validation_issues(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            source_path = uploads_folder / "job" / "zstruct.abap"
            specification_path = uploads_folder / "job" / "enhancement.txt"
            prompt_path = temp_path / "enhance_existing_abap.txt"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(
                "\n".join(["REPORT zstruct.", "START-OF-SELECTION.", "  WRITE: / 'ready'."]),
                encoding="utf-8",
            )
            specification_path.write_text("Add a missing assignment.", encoding="utf-8")
            prompt_path.write_text("Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}", encoding="utf-8")

            def generator(_prompt_text, source_text):
                return {"text": source_text.replace("  WRITE: / 'ready'.", "  WRITE: / 'ready'.\n  w_missing = 'Y'."), "model": "test-model", "usage": None}

            run_enhance_abap(
                "job",
                source_path,
                specification_path,
                jobs_folder,
                prompt_path,
                enhancement_generator=generator,
                enhancement_review_required=False,
            )

            issues = json.loads((jobs_folder / "job" / "validation_issues.json").read_text(encoding="utf-8"))
            self.assertTrue(
                [
                    issue
                    for issue in issues
                    if issue["rule_id"] == "ENHANCEMENT_MISSING_DEFINITION" and issue.get("identifier") == "w_missing"
                ]
            )
            self.assertFalse((jobs_folder / "job" / "generated.abap").exists())
            self.assertEqual(get_progress(jobs_folder, "job")["status"], "Error")
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_run_enhance_abap_restores_unrelated_fixer_rewrites(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            source_path = uploads_folder / "job" / "zoutput.abap"
            specification_path = uploads_folder / "job" / "enhancement.txt"
            prompt_path = temp_path / "enhance_existing_abap.txt"
            source_path.parent.mkdir(parents=True)
            original = "\n".join(
                [
                    "REPORT zoutput.",
                    "TYPES: BEGIN OF ty_pa0002,",
                    "         pernr TYPE pernr_d,",
                    "         nachn TYPE pad_nachn,",
                    "       END OF ty_pa0002.",
                    "TYPES: BEGIN OF ty_output,",
                    "         nachn TYPE string,",
                    "       END OF ty_output.",
                    "DATA: t_pa0002 TYPE STANDARD TABLE OF ty_pa0002,",
                    "      t_alv_fieldcat TYPE slis_t_fieldcat_alv,",
                    "      st_alv_fieldcat TYPE slis_fieldcat_alv,",
                    "      st_output TYPE ty_output.",
                    "FORM define_field_catalog.",
                    "  st_alv_fieldcat-fieldname = 'NACHN'.",
                    "  st_alv_fieldcat-seltext_l = 'Surname'.",
                    "ENDFORM.",
                    "FORM read_pa0002.",
                    "  SELECT pernr",
                    "         nachn",
                    "         INTO TABLE t_pa0002",
                    "         FROM pa0002.",
                    "ENDFORM.",
                    "FORM map_0006_field_changes USING p_fieldname.",
                    "  DATA: l_source_field TYPE string,",
                    "        l_target_field TYPE string,",
                    "        l_output_field TYPE string.",
                    "  l_source_field = 'ST_PA0006_CURRENT_OUTPUT-' && p_fieldname.",
                    "  l_target_field = 'ST_PA0006_SECOND_OUTPUT-'  && p_fieldname.",
                    "  l_output_field = 'ST_OUTPUT-'                && p_fieldname.",
                    "ENDFORM.",
                    "FORM split_anlnr.",
                    "  DATA: lt_strings TYPE STANDARD TABLE OF string.",
                    "  DATA: lst_strings TYPE string.",
                    "  SPLIT st_pa0032-anlnr AT '-' INTO TABLE lt_strings.",
                    "  LOOP AT lt_strings INTO lst_strings.",
                    "  ENDLOOP.",
                    "ENDFORM.",
                ]
            )
            enhanced = original.replace(
                "         nachn TYPE pad_nachn,\n       END OF ty_pa0002.",
                "         nachn TYPE pad_nachn,\n         gbdat TYPE dats,\n       END OF ty_pa0002.",
            ).replace(
                "         nachn TYPE string,\n       END OF ty_output.",
                "         nachn TYPE string,\n         gbdat TYPE dats,\n       END OF ty_output.",
            ).replace(
                "         nachn\n         INTO TABLE t_pa0002",
                "         nachn\n         gbdat\n         INTO TABLE t_pa0002",
            ).replace(
                "  st_alv_fieldcat-seltext_l = 'Surname'.",
                "  st_alv_fieldcat-seltext_l = 'Surname'.\n"
                "  APPEND st_alv_fieldcat TO t_alv_fieldcat.\n"
                "  CLEAR st_alv_fieldcat.\n"
                "  st_alv_fieldcat-fieldname = 'GBDAT'.\n"
                "  st_alv_fieldcat-seltext_l = 'Date of Birth'.",
            )
            source_path.write_text(original, encoding="utf-8")
            specification_path.write_text(
                "For the read from PA0002 also extract GBDAT. This is only required on the ALV report.",
                encoding="utf-8",
            )
            prompt_path.write_text("Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}", encoding="utf-8")

            run_enhance_abap(
                "job",
                source_path,
                specification_path,
                jobs_folder,
                prompt_path,
                enhancement_review_required=False,
                approved_enhancement={"proposed_abap": enhanced, "model": "approved-test", "usage": None, "chunks": []},
            )

            generated = (jobs_folder / "job" / "generated.abap").read_text(encoding="utf-8")
            self.assertIn("l_source_field = 'ST_PA0006_CURRENT_OUTPUT-' && p_fieldname.", generated)
            self.assertIn("DATA: lt_strings TYPE STANDARD TABLE OF string.", generated)
            self.assertIn("SPLIT st_pa0032-anlnr AT '-' INTO TABLE lt_strings.", generated)
            self.assertIn("gbdat TYPE dats", generated)
            self.assertEqual(get_progress(jobs_folder, "job")["status"], "Complete")
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_restore_select_blocks_preserves_logic_between_similar_blocks(self):
        original = "\n".join(
            [
                "FORM build_file_content.",
                "  SELECT low UP TO 1 ROWS",
                "         INTO w_zzcolleague_location",
                "         FROM hrv1222a",
                "         WHERE attrib EQ 'ZCOLL_LOCN'.",
                "  ENDSELECT.",
                "",
                "  TRANSLATE w_zzcolleague_location TO UPPER CASE.",
                "",
                "  READ TABLE t_site_addresses INTO st_site_addresses",
                "    WITH KEY name = w_zzcolleague_location",
                "    BINARY SEARCH.",
                "",
                "  IF sy-subrc NE 0.",
                "    SELECT low UP TO 1 ROWS",
                "           INTO w_zzcolleague_location",
                "           FROM hrv1222a",
                "           WHERE attrib EQ 'ZCOLL_LOC2'.",
                "    ENDSELECT.",
                "  ENDIF.",
                "ENDFORM.",
            ]
        )
        fixed = original.replace(
            "\n".join(
                [
                    "  SELECT low UP TO 1 ROWS",
                    "         INTO w_zzcolleague_location",
                    "         FROM hrv1222a",
                    "         WHERE attrib EQ 'ZCOLL_LOCN'.",
                    "  ENDSELECT.",
                ]
            ),
            "  SELECT low UP TO 1 ROWS FROM hrv1222a INTO w_zzcolleague_location WHERE attrib EQ 'ZCOLL_LOCN'.\n  ENDSELECT.",
        ).replace(
            "\n".join(
                [
                    "    SELECT low UP TO 1 ROWS",
                    "           INTO w_zzcolleague_location",
                    "           FROM hrv1222a",
                    "           WHERE attrib EQ 'ZCOLL_LOC2'.",
                    "    ENDSELECT.",
                ]
            ),
            "    SELECT low UP TO 1 ROWS FROM hrv1222a INTO w_zzcolleague_location WHERE attrib EQ 'ZCOLL_LOC2'.\n    ENDSELECT.",
        )

        restored, blocks = restore_unrelated_select_endselect_blocks(original, fixed, None)

        self.assertEqual(restored, original)
        self.assertEqual(len(blocks), 2)
        self.assertIn("TRANSLATE w_zzcolleague_location TO UPPER CASE.", restored)
        self.assertIn("READ TABLE t_site_addresses INTO st_site_addresses", restored)
        self.assertIn("WHERE attrib EQ 'ZCOLL_LOC2'.", restored)

    def test_run_enhance_abap_restores_unrelated_select_blocks_after_fixer(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            source_path = uploads_folder / "job" / "zselect.abap"
            specification_path = uploads_folder / "job" / "enhancement.txt"
            prompt_path = temp_path / "enhance_existing_abap.txt"
            source_path.parent.mkdir(parents=True)
            select_block = "\n".join(
                [
                    "  SELECT low UP TO 1 ROWS",
                    "         INTO w_location",
                    "         FROM zlookup",
                    "         WHERE key_field EQ w_key.",
                    "  ENDSELECT.",
                ]
            )
            original = "\n".join(
                [
                    "REPORT zselect.",
                    "DATA: w_location TYPE string,",
                    "      w_key TYPE string.",
                    "FORM build_output.",
                    select_block,
                    "  WRITE: / 'old'.",
                    "ENDFORM.",
                ]
            )
            enhanced = original.replace("  WRITE: / 'old'.", "  WRITE: / 'old'.\n  WRITE: / 'new'.")
            bad_fixed = enhanced.replace(
                "         WHERE key_field EQ w_key.\n  ENDSELECT.",
                "         WHERE key_field EQ w_key.\n"
                "  SELECT low UP TO 1 ROWS FROM zlookup INTO w_location WHERE key_field EQ w_key.\n"
                "  ENDSELECT.",
            )
            source_path.write_text(original, encoding="utf-8")
            specification_path.write_text("Add an output line.", encoding="utf-8")
            prompt_path.write_text("Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}", encoding="utf-8")
            fixer_result = {
                "fixed_source": bad_fixed,
                "original_issues": [],
                "final_issues": [],
                "original_issue_count": 0,
                "final_issue_count": 0,
                "fixes": [],
                "diagnostics": {"source_after_fixer": bad_fixed},
            }

            with patch("services.enhance_abap.auto_fix_abap", return_value=fixer_result):
                run_enhance_abap(
                    "job",
                    source_path,
                    specification_path,
                    jobs_folder,
                    prompt_path,
                    enhancement_review_required=False,
                    approved_enhancement={"proposed_abap": enhanced, "model": "approved-test", "usage": None, "chunks": []},
                )

            generated = (jobs_folder / "job" / "generated.abap").read_text(encoding="utf-8")
            self.assertIn(select_block, generated)
            self.assertNotIn("SELECT low UP TO 1 ROWS FROM zlookup INTO w_location", generated)
            self.assertIn("WRITE: / 'new'.", generated)
            fix_summary = json.loads((jobs_folder / "job" / "fix_summary.json").read_text(encoding="utf-8"))
            self.assertTrue(fix_summary["diagnostics"]["enhancement_restored_unrelated_select_blocks"])
            self.assertEqual(get_progress(jobs_folder, "job")["status"], "Complete")
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_targeted_enhancement_sends_only_affected_routines_and_preserves_unaffected_bytes(self):
        source = "\n".join(
            [
                "REPORT ztarget.",
                "DATA w_flag TYPE c.",
                "",
                "FORM read_customer.",
                "  WRITE: / 'old customer'.",
                "ENDFORM.",
                "",
                "FORM untouched.",
                "  WRITE: / 'do not send'.",
                "ENDFORM.",
                "",
                "FORM output_email.",
                "  WRITE: / 'old email'.",
                "ENDFORM.",
            ]
        )
        calls = []

        def generator(prompt_text, source_text):
            self.assertNotIn("FORM untouched.", source_text)
            self.assertNotIn("do not send", prompt_text)
            calls.append(source_text)
            if "FORM read_customer." in source_text:
                return {"text": source_text.replace("'old customer'", "'new customer'"), "model": "test-model", "usage": None}
            if "FORM output_email." in source_text:
                return {"text": source_text.replace("'old email'", "'new email'"), "model": "test-model", "usage": None}
            self.fail(f"Unexpected chunk sent to generator: {source_text}")

        chunks = split_existing_program_chunks(source)
        affected = identify_affected_chunks(chunks, "Update read_customer and output_email only.")
        result = generate_targeted_enhancement(
            original_source=source,
            chunks=chunks,
            affected_chunks=affected,
            prompt_template="Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}",
            enhancement_specification="Update read_customer and output_email only.",
            generator=generator,
        )

        self.assertEqual(len(calls), 2)
        self.assertIn("FORM read_customer.", calls[0])
        self.assertIn("FORM output_email.", calls[1])
        self.assertIn("FORM untouched.\n  WRITE: / 'do not send'.\nENDFORM.", result["text"])
        self.assertIn("  WRITE: / 'new customer'.", result["text"])
        self.assertIn("  WRITE: / 'new email'.", result["text"])

    def test_multiple_affected_routines_are_processed_separately_and_reassembled_in_original_order(self):
        source = "\n".join(
            [
                "REPORT ztarget.",
                "",
                "FORM alpha.",
                "  WRITE: / 'alpha'.",
                "ENDFORM.",
                "",
                "FORM beta.",
                "  WRITE: / 'beta'.",
                "ENDFORM.",
                "",
                "FORM gamma.",
                "  WRITE: / 'gamma'.",
                "ENDFORM.",
            ]
        )
        calls = []

        def generator(_prompt_text, source_text):
            calls.append(source_text)
            if "FORM alpha." in source_text:
                return {"text": source_text.replace("'alpha'", "'alpha changed'"), "model": "test-model", "usage": None}
            if "FORM gamma." in source_text:
                return {"text": source_text.replace("'gamma'", "'gamma changed'"), "model": "test-model", "usage": None}
            self.fail(f"Unexpected chunk sent to generator: {source_text}")

        chunks = split_existing_program_chunks(source)
        affected = identify_affected_chunks(chunks, "Change alpha and gamma.")
        result = generate_targeted_enhancement(
            original_source=source,
            chunks=chunks,
            affected_chunks=affected,
            prompt_template="Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}",
            enhancement_specification="Change alpha and gamma.",
            generator=generator,
        )

        self.assertEqual(len(calls), 2)
        self.assertIn("FORM alpha.", calls[0])
        self.assertIn("FORM gamma.", calls[1])
        self.assertLess(result["text"].index("FORM alpha."), result["text"].index("FORM beta."))
        self.assertLess(result["text"].index("FORM beta."), result["text"].index("FORM gamma."))
        self.assertIn("FORM beta.\n  WRITE: / 'beta'.\nENDFORM.", result["text"])
        self.assertIn("  WRITE: / 'alpha changed'.", result["text"])
        self.assertIn("  WRITE: / 'gamma changed'.", result["text"])

    def test_multiple_chunks_same_enhancement_keep_one_coherent_implementation(self):
        source = "\n".join(
            [
                "REPORT ztarget.",
                "",
                "FORM alpha.",
                "  WRITE: / 'alpha'.",
                "ENDFORM.",
                "",
                "FORM gamma.",
                "  WRITE: / 'gamma'.",
                "ENDFORM.",
            ]
        )

        def generator(_prompt_text, source_text):
            return {
                "text": source_text.replace(
                    "ENDFORM.",
                    "\n  DATA t_vbak TYPE STANDARD TABLE OF vbak.\n  SELECT vbeln\n    FROM vbak\n    INTO TABLE t_vbak.\n  PERFORM build_output.\nENDFORM.\n\nFORM build_output.\n  WRITE: / 'done'.\nENDFORM.",
                ),
                "model": "test-model",
                "usage": None,
            }

        chunks = split_existing_program_chunks(source)
        affected = identify_affected_chunks(chunks, "Add build_output logic to alpha and gamma.")
        result = generate_targeted_enhancement(
            original_source=source,
            chunks=chunks,
            affected_chunks=affected,
            prompt_template="Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}",
            enhancement_specification="Add build_output logic to alpha and gamma.",
            generator=generator,
        )

        lowered = result["text"].lower()
        self.assertEqual(lowered.splitlines().count("form build_output."), 1)
        self.assertEqual(lowered.count("data t_vbak"), 1)
        self.assertEqual(lowered.count("from vbak"), 1)
        self.assertEqual(lowered.count("perform build_output."), 1)
        self.assertIn("FORM gamma.\n  WRITE: / 'gamma'.\nENDFORM.", result["text"])

    def test_conflicting_duplicate_new_routine_versions_reject_later_duplicate(self):
        source = "\n".join(
            [
                "REPORT ztarget.",
                "",
                "FORM alpha.",
                "  WRITE: / 'alpha'.",
                "ENDFORM.",
                "",
                "FORM gamma.",
                "  WRITE: / 'gamma'.",
                "ENDFORM.",
            ]
        )
        chunks = split_existing_program_chunks(source)
        replacements = {
            "form:alpha": "\n".join(
                [
                    "FORM alpha.",
                    "  PERFORM build_output.",
                    "ENDFORM.",
                    "",
                    "FORM build_output.",
                    "  WRITE: / 'alpha version'.",
                    "ENDFORM.",
                ]
            ),
            "form:gamma": "\n".join(
                [
                    "FORM gamma.",
                    "  PERFORM build_output.",
                    "ENDFORM.",
                    "",
                    "FORM build_output.",
                    "  WRITE: / 'gamma version'.",
                    "ENDFORM.",
                ]
            ),
        }

        reconciled, issues = reconcile_enhancement_chunk_replacements(
            source,
            chunks,
            replacements,
            "Add build_output logic to alpha and gamma.",
        )

        merged = "\n".join(reconciled.values())
        self.assertEqual(merged.lower().splitlines().count("form build_output."), 1)
        self.assertIn("alpha version", merged)
        self.assertNotIn("gamma version", merged)
        self.assertTrue([issue for issue in issues if issue["rule_id"] == "ENHANCEMENT_CONTRADICTORY_IMPLEMENTATION"])

    def test_explicit_multiple_reads_keeps_separate_requested_reads(self):
        source = "\n".join(
            [
                "REPORT ztarget.",
                "",
                "FORM alpha.",
                "  WRITE: / 'alpha'.",
                "ENDFORM.",
                "",
                "FORM gamma.",
                "  WRITE: / 'gamma'.",
                "ENDFORM.",
            ]
        )

        def generator(_prompt_text, source_text):
            return {
                "text": source_text.replace(
                    "ENDFORM.",
                    "\n  SELECT vbeln\n    FROM vbak\n    INTO TABLE t_vbak.\nENDFORM.",
                ),
                "model": "test-model",
                "usage": None,
            }

        chunks = split_existing_program_chunks(source)
        affected = identify_affected_chunks(chunks, "Add separate reads of VBAK in alpha and gamma.")
        result = generate_targeted_enhancement(
            original_source=source,
            chunks=chunks,
            affected_chunks=affected,
            prompt_template="Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}",
            enhancement_specification="Add separate reads of VBAK in alpha and gamma.",
            generator=generator,
        )

        self.assertEqual(result["text"].lower().count("from vbak"), 2)

    def test_run_enhance_abap_writes_targeted_chunk_diagnostics(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            source_path = uploads_folder / "job" / "ztarget.abap"
            specification_path = uploads_folder / "job" / "enhancement.txt"
            prompt_path = temp_path / "enhance_existing_abap.txt"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(
                "\n".join(
                    [
                        "REPORT ztarget.",
                        "",
                        "FORM change_me.",
                        "  WRITE: / 'old'.",
                        "ENDFORM.",
                        "",
                        "FORM keep_me.",
                        "  WRITE: / 'unchanged'.",
                        "ENDFORM.",
                    ]
                ),
                encoding="utf-8",
            )
            specification_path.write_text("Change change_me.", encoding="utf-8")
            prompt_path.write_text("Prompt\n{{FUNCTIONAL_SPECIFICATION}}\n{{EXISTING_ABAP}}", encoding="utf-8")

            def generator(_prompt_text, source_text):
                self.assertIn("FORM change_me.", source_text)
                self.assertNotIn("FORM keep_me.", source_text)
                return {"text": source_text.replace("'old'", "'new'"), "model": "test-model", "usage": None}

            run_enhance_abap(
                "job",
                source_path,
                specification_path,
                jobs_folder,
                prompt_path,
                enhancement_generator=generator,
                enhancement_review_required=False,
            )

            generated = (jobs_folder / "job" / "generated.abap").read_text(encoding="utf-8")
            diagnostics = json.loads((jobs_folder / "job" / "enhancement_chunks.json").read_text(encoding="utf-8"))
            self.assertIn("FORM keep_me.\n  WRITE: / 'unchanged'.\nENDFORM.", generated)
            self.assertEqual([chunk["id"] for chunk in diagnostics["affected_chunks"]], ["form:change_me"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_result_page_shows_enhancement_chunk_diagnostics(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            job_folder = jobs_folder / "job"
            job_folder.mkdir(parents=True)
            (job_folder / "generated.abap").write_text("REPORT ztest.", encoding="utf-8")
            (job_folder / "metrics.json").write_text(
                json.dumps(
                    {
                        "job_mode": "enhance_existing_abap",
                        "duration_seconds": 1.0,
                        "section_durations": {"generated_abap": 0.5},
                    }
                ),
                encoding="utf-8",
            )
            (job_folder / "enhancement_chunks.json").write_text(
                json.dumps(
                    {
                        "chunks": [{"id": "global", "type": "GLOBAL", "start_line": 0, "end_line": 0}],
                        "affected_chunks": [{"id": "form:change_me", "type": "FORM", "start_line": 1, "end_line": 3}],
                        "processed_chunks": [
                            {
                                "name": "form:change_me",
                                "prompt": "Prompt sent to enhancement chunk",
                                "text": "FORM change_me.\n  WRITE: / 'new'.\nENDFORM.",
                                "raw_response": "FORM change_me.\n  WRITE: / 'new'.\nENDFORM.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})

            response = app.test_client().get("/result/job")

            self.assertEqual(response.status_code, 200)
            self.assertIn(b"form:change_me", response.data)
            self.assertIn(b"Prompt sent to enhancement chunk", response.data)
            self.assertNotIn(b"No ABAP generation chunk diagnostics recorded.", response.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_result_page_uses_legacy_enhancement_diagnostics_chunks(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"
        temp_path.mkdir()
        try:
            jobs_folder = temp_path / "jobs"
            job_folder = jobs_folder / "job"
            job_folder.mkdir(parents=True)
            (job_folder / "generated.abap").write_text("REPORT ztest.", encoding="utf-8")
            (job_folder / "metrics.json").write_text(
                json.dumps({"job_mode": "enhance_existing_abap", "duration_seconds": 1.0}),
                encoding="utf-8",
            )
            (job_folder / "enhancement_diagnostics.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {
                                "name": "form:legacy_chunk",
                                "prompt": "Legacy enhancement prompt",
                                "text": "FORM legacy_chunk.\nENDFORM.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            app = create_app({"TESTING": True, "JOBS_FOLDER": str(jobs_folder)})

            response = app.test_client().get("/result/job")

            self.assertEqual(response.status_code, 200)
            self.assertIn(b"form:legacy_chunk", response.data)
            self.assertIn(b"Legacy enhancement prompt", response.data)
            self.assertNotIn(b"No ABAP generation chunk diagnostics recorded.", response.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)


def wait_for_status(jobs_folder, job_id, expected_status, timeout=5):
    deadline = real_time.time() + timeout
    while real_time.time() < deadline:
        progress = get_progress(jobs_folder, job_id)
        if progress["status"] == expected_status:
            return progress
        real_time.sleep(0.05)
    return get_progress(jobs_folder, job_id)


if __name__ == "__main__":
    unittest.main()
