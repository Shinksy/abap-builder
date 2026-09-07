from pathlib import Path
import os
import shutil
import unittest
from uuid import uuid4
from unittest.mock import Mock, patch

from app import create_app
from config import ENV_FILE_PATH, env_bool, load_env_file
import services.llm as llm_service
from services.llm import (
    call_anthropic,
    generate_abap,
    generate_code_review_repair,
    generate_dependency_analysis,
    reset_current_model_settings,
    sanitize_openai_proxy_environment,
    set_current_model_settings,
)


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
        app = create_app({
            "TESTING": True,
            "OPENAI_API_KEY": "test-key",
            "OPENAI_ABAP_GENERATION_MODEL": "gpt-5.6-sol",
        })
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
        self.assertEqual("gpt-5.6-sol", request["model"])

    def test_generate_dependency_analysis_passes_response_format_to_responses_api(self):
        app = create_app({
            "TESTING": True,
            "OPENAI_API_KEY": "test-key",
            "OPENAI_DEPENDENCY_ANALYSIS_MODEL": "gpt-5.6-luna",
        })
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

            result = generate_dependency_analysis("Prompt", "Source", response_format=response_format)

        self.assertEqual("{\"processing_steps\":[]}", result["text"])
        request = client.responses.create.call_args.kwargs
        self.assertEqual({"format": response_format}, request["text"])
        self.assertEqual("gpt-5.6-luna", request["model"])

    def test_llm_wrappers_use_configured_model_names(self):
        app = create_app({
            "TESTING": True,
            "OPENAI_API_KEY": "test-key",
            "OPENAI_DEPENDENCY_ANALYSIS_MODEL": "gpt-5.6-luna",
            "OPENAI_CODE_REVIEW_MODEL": "gpt-5.6-terra",
        })
        response = Mock()
        response.output_text = "ok"
        response.usage = None
        response.model_dump.return_value = {"id": "response-1"}

        with app.app_context(), patch("openai.OpenAI") as openai_cls:
            client = openai_cls.return_value
            client.responses.create.return_value = response

            generate_dependency_analysis("Prompt", "Source")
            generate_code_review_repair("Prompt", "Source")

        models = [call.kwargs["model"] for call in client.responses.create.call_args_list]
        self.assertEqual(["gpt-5.6-luna", "gpt-5.6-terra"], models)

    def test_llm_wrappers_use_current_job_model_settings(self):
        app = create_app({"TESTING": True, "OPENAI_API_KEY": "test-key"})
        response = Mock()
        response.output_text = "ok"
        response.usage = None
        response.model_dump.return_value = {"id": "response-1"}
        model_settings = {
            "models": {
                "dependency_analysis": "gpt-5.6-terra",
                "abap_generation": "gpt-5.6-sol",
                "code_review": "gpt-5.6-luna",
            }
        }

        with app.app_context(), patch("openai.OpenAI") as openai_cls:
            client = openai_cls.return_value
            client.responses.create.return_value = response
            token = set_current_model_settings(model_settings)
            try:
                generate_dependency_analysis("Prompt", "Source")
                generate_abap("Prompt", "Source")
                generate_code_review_repair("Prompt", "Source")
            finally:
                reset_current_model_settings(token)

        models = [call.kwargs["model"] for call in client.responses.create.call_args_list]
        self.assertEqual(["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"], models)

    def test_llm_wrappers_route_claude_settings_to_anthropic(self):
        app = create_app({
            "TESTING": True,
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_API_URL": "https://anthropic.example.test/v1/messages",
        })
        model_settings = {
            "provider": "anthropic",
            "models": {
                "abap_generation": "claude-sonnet-5",
            },
        }

        with app.app_context(), patch("services.llm.requests.post") as post:
            post.return_value.json.return_value = {
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "REPORT ztest."}],
                "usage": {"input_tokens": 11, "output_tokens": 7},
            }
            token = set_current_model_settings(model_settings)
            try:
                result = generate_abap("Prompt", "Source")
            finally:
                reset_current_model_settings(token)

        self.assertEqual("REPORT ztest.", result["text"])
        self.assertEqual("claude-sonnet-5", result["model"])
        self.assertEqual({"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}, result["usage"])
        request = post.call_args.kwargs
        self.assertEqual("https://anthropic.example.test/v1/messages", post.call_args.args[0])
        self.assertEqual("claude-sonnet-5", request["json"]["model"])
        self.assertEqual("Prompt", request["json"]["system"])
        self.assertEqual([{"role": "user", "content": "Source"}], request["json"]["messages"])
        self.assertEqual("test-key", request["headers"]["x-api-key"])

    def test_llm_wrappers_use_sap_btp_claude_env_when_anthropic_key_is_missing(self):
        app = create_app({
            "TESTING": True,
            "ANTHROPIC_API_KEY": None,
            "SAP_BTP_TOKEN_URL": "https://btp.example.test/oauth/token",
            "SAP_BTP_INFERENCE_URL": "https://btp.example.test/invoke",
            "SAP_BTP_CLIENT_ID": "client-id",
            "SAP_BTP_CLIENT_SECRET": "client-secret",
            "SAP_BTP_AI_RESOURCE_GROUP": "test-resource-group",
            "SAP_BTP_CLAUDE_MAX_TOKENS": 1500,
        })
        model_settings = {
            "provider": "anthropic",
            "models": {"abap_generation": "claude-sonnet-5"},
        }
        token_response = Mock()
        token_response.raise_for_status.return_value = None
        token_response.json.return_value = {"access_token": "token", "expires_in": 3600}
        inference_response = Mock()
        inference_response.raise_for_status.return_value = None
        inference_response.json.return_value = {
            "content": [{"type": "text", "text": "REPORT zbtp."}],
            "usage": {"input_tokens": 12, "output_tokens": 8},
        }

        with app.app_context(), patch("services.llm.requests.post", side_effect=[token_response, inference_response]) as post:
            llm_service._sap_btp_token = None
            llm_service._sap_btp_token_expires_at = 0
            token = set_current_model_settings(model_settings)
            try:
                result = generate_abap("Prompt", "Source")
            finally:
                reset_current_model_settings(token)

        self.assertEqual("REPORT zbtp.", result["text"])
        self.assertEqual("claude-sonnet-5", result["model"])
        self.assertEqual({"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}, result["usage"])
        self.assertEqual("https://btp.example.test/oauth/token", post.call_args_list[0].args[0])
        self.assertEqual(("client-id", "client-secret"), post.call_args_list[0].kwargs["auth"])
        inference_call = post.call_args_list[1]
        self.assertEqual("https://btp.example.test/invoke", inference_call.args[0])
        self.assertEqual("Bearer token", inference_call.kwargs["headers"]["Authorization"])
        self.assertEqual("test-resource-group", inference_call.kwargs["headers"]["AI-Resource-Group"])
        self.assertEqual(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1500,
                "messages": [{
                    "role": "user",
                    "content": [{"type": "text", "text": "Prompt\n\nSource"}],
                }],
            },
            inference_call.kwargs["json"],
        )

    def test_missing_anthropic_api_key_has_clear_error(self):
        app = create_app({
            "TESTING": True,
            "ANTHROPIC_API_KEY": None,
            "SAP_BTP_TOKEN_URL": None,
            "SAP_BTP_INFERENCE_URL": None,
            "SAP_BTP_CLIENT_ID": None,
            "SAP_BTP_CLIENT_SECRET": None,
            "SAP_BTP_AI_RESOURCE_GROUP": None,
        })

        with app.app_context():
            with self.assertRaisesRegex(RuntimeError, "ANTHROPIC_API_KEY is missing"):
                call_anthropic("Prompt", "Source")


if __name__ == "__main__":
    unittest.main()
