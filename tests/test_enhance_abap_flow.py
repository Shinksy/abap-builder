from io import BytesIO
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4
from unittest.mock import patch

from app import create_app
from services.enhance_abap import render_enhance_prompt, run_enhance_abap
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
                data={"existing_abap_file": (BytesIO(b"REPORT zold."), "zold.abap")},
                content_type="multipart/form-data",
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Enter the enhancement specification.", response.data.decode("utf-8"))
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
                self.assertIn("Existing ABAP program", source_text)
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

    def test_render_enhance_prompt_replaces_inputs(self):
        rendered = render_enhance_prompt(
            "{{FUNCTIONAL_SPECIFICATION}}\n---\n{{EXISTING_ABAP}}",
            "REPORT zold.",
            "Add output.",
        )

        self.assertIn("Add output.", rendered)
        self.assertIn("REPORT zold.", rendered)
        self.assertNotIn("{{", rendered)


if __name__ == "__main__":
    unittest.main()
