from pathlib import Path
import os
import shutil
import unittest
from uuid import uuid4
from unittest.mock import Mock, patch

from app import create_app
from config import ENV_FILE_PATH, env_bool, load_env_file
from services.llm import generate_abap, sanitize_openai_proxy_environment


class ConfigTest(unittest.TestCase):
    def test_values_from_env_file_are_loaded(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_config_{uuid4().hex}"
        temp_path.mkdir()
        env_path = temp_path / ".env"
        old_values = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "OPENAI_MODEL", "SECRET_KEY")}
        try:
            for key in old_values:
                os.environ.pop(key, None)

            env_path.write_text(
                "\n".join(
                    [
                        "OPENAI_API_KEY=test-key",
                        "OPENAI_MODEL=test-model",
                        "SECRET_KEY=test-secret",
                    ]
                ),
                encoding="utf-8",
            )

            load_env_file(env_path)

            self.assertEqual(os.environ.get("OPENAI_API_KEY"), "test-key")
            self.assertEqual(os.environ.get("OPENAI_MODEL"), "test-model")
            self.assertEqual(os.environ.get("SECRET_KEY"), "test-secret")
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_env_file_overrides_existing_placeholder_value(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_config_{uuid4().hex}"
        temp_path.mkdir()
        env_path = temp_path / ".env"
        old_value = os.environ.get("OPENAI_API_KEY")
        try:
            os.environ["OPENAI_API_KEY"] = "your_api_key_here"
            env_path.write_text("OPENAI_API_KEY=test-key", encoding="utf-8")

            load_env_file(env_path)

            self.assertEqual(os.environ.get("OPENAI_API_KEY"), "test-key")
        finally:
            if old_value is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_value
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_sap_ddic_metadata_enabled_alias_is_supported(self):
        old_values = {key: os.environ.get(key) for key in ("SAP_DDIC_METADATA_ENABLED", "USE_SAP_METADATA")}
        try:
            os.environ.pop("SAP_DDIC_METADATA_ENABLED", None)
            os.environ["USE_SAP_METADATA"] = "true"

            self.assertTrue(env_bool("SAP_DDIC_METADATA_ENABLED", fallback_name="USE_SAP_METADATA"))

            os.environ["SAP_DDIC_METADATA_ENABLED"] = "false"
            self.assertFalse(env_bool("SAP_DDIC_METADATA_ENABLED", fallback_name="USE_SAP_METADATA"))
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_boolean_parser_accepts_common_true_values(self):
        old_value = os.environ.get("TEST_BOOL")
        try:
            for value in ("true", "True", "1", "yes"):
                os.environ["TEST_BOOL"] = value
                self.assertTrue(env_bool("TEST_BOOL"))
        finally:
            if old_value is None:
                os.environ.pop("TEST_BOOL", None)
            else:
                os.environ["TEST_BOOL"] = old_value

    def test_next_app_env_file_path_is_explicit(self):
        self.assertEqual(ENV_FILE_PATH, Path(__file__).resolve().parents[1] / ".env")

    def test_missing_openai_api_key_has_clear_error(self):
        app = create_app({"TESTING": True, "OPENAI_API_KEY": None})

        with app.app_context():
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY is missing"):
                generate_abap("Generate ABAP.", "Create a report.")

    def test_dummy_openai_proxy_environment_is_removed(self):
        proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
        old_values = {key: os.environ.get(key) for key in proxy_names}
        try:
            for key in proxy_names:
                os.environ[key] = "http://127.0.0.1:9"

            sanitize_openai_proxy_environment()

            for key in proxy_names:
                self.assertIsNone(os.environ.get(key))
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_generate_abap_passes_response_format_to_responses_api(self):
        app = create_app({"TESTING": True, "OPENAI_API_KEY": "test-key"})
        response = Mock()
        response.output_text = "{\"processing_steps\":[]}"
        response.usage = None
        response.model_dump.return_value = {"id": "response-1"}

        with app.app_context(), patch("openai.OpenAI") as openai_cls:
            client = openai_cls.return_value
            client.responses.create.return_value = response
            response_format = {
                "type": "json_schema",
                "name": "processing_plan",
                "strict": True,
                "schema": {"type": "object", "properties": {}, "additionalProperties": False},
            }

            result = generate_abap("Prompt", "Source", response_format=response_format)

        self.assertEqual("{\"processing_steps\":[]}", result["text"])
        request = client.responses.create.call_args.kwargs
        self.assertEqual({"format": response_format}, request["text"])
        self.assertEqual("gpt-5-mini", request["model"])


if __name__ == "__main__":
    unittest.main()
