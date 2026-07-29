import json
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StandaloneProjectTest(unittest.TestCase):
    def test_project_imports_and_configures_paths_after_copy_to_new_root(self):
        copy_root = PROJECT_ROOT / f".standalone_copy_{uuid4().hex}"
        try:
            copy_standalone_project(copy_root)

            script = textwrap.dedent(
                """
                import json
                from pathlib import Path
                from app import create_app
                from config import Config

                app = create_app({"TESTING": True})
                payload = {
                    "base_dir": str(Config.BASE_DIR),
                    "upload_folder": app.config["UPLOAD_FOLDER"],
                    "jobs_folder": app.config["JOBS_FOLDER"],
                    "prompt": app.config["CREATE_ABAP_PROMPT"],
                    "routes": sorted(str(rule.rule) for rule in app.url_map.iter_rules()),
                }
                print(json.dumps(payload))
                """
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=copy_root,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout.splitlines()[-1])
            self.assertEqual(Path(payload["base_dir"]).resolve(), copy_root.resolve())
            for key in ("upload_folder", "jobs_folder", "prompt"):
                self.assertTrue(Path(payload[key]).resolve().is_relative_to(copy_root.resolve()), key)
            self.assertIn("/", payload["routes"])
            self.assertIn("/upload", payload["routes"])
        finally:
            shutil.rmtree(copy_root, ignore_errors=True)

    def test_source_tree_contains_no_parent_project_references(self):
        parent_name = "sap-ai-" + "assistant"
        forbidden = {
            "parent project path with slash": parent_name + "/",
            "parent project path with backslash": parent_name + "\\",
            "absolute OneDrive path": "OneDrive - " + "ATOS",
            "absolute GitHub path": "Documents/GitHub/" + parent_name,
            "grandparent path lookup": "parents" + "[2]",
            "double parent path lookup": "parent" + ".parent",
            "python path override": "PYTHON" + "PATH",
            "sys path mutation": "sys" + ".path",
        }
        checked_suffixes = {".py", ".txt", ".html", ".css", ".json", ".md"}
        offenders = []
        for path in PROJECT_ROOT.rglob("*"):
            if should_skip_path(path) or path.suffix.lower() not in checked_suffixes:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for label, needle in forbidden.items():
                if needle in text:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)} contains {label}")
        self.assertEqual([], offenders)


def copy_standalone_project(copy_root):
    copy_root.mkdir(parents=True)
    for name in ("app.py", "config.py", "requirements.txt", ".gitignore"):
        shutil.copy2(PROJECT_ROOT / name, copy_root / name)
    for name in ("services", "prompts", "templates", "static"):
        shutil.copytree(
            PROJECT_ROOT / name,
            copy_root / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )


def should_skip_path(path):
    parts = set(path.relative_to(PROJECT_ROOT).parts)
    return bool(parts & {".git", ".venv", "__pycache__", "jobs", "uploads"}) or any(
        part.startswith((".test_", ".standalone_copy_")) for part in parts
    )


if __name__ == "__main__":
    unittest.main()
