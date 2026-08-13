import os
from contextvars import ContextVar
from urllib.parse import urlparse

from flask import current_app, has_app_context

from config import Config


_current_model_settings = ContextVar("current_model_settings", default=None)


def _config_value(name):
    if has_app_context():
        return current_app.config.get(name)
    return getattr(Config, name, None)


def set_current_model_settings(model_settings):
    return _current_model_settings.set(model_settings if isinstance(model_settings, dict) else None)


def reset_current_model_settings(token):
    _current_model_settings.reset(token)


def _model_for_role(role, config_key):
    settings = _current_model_settings.get()
    models = settings.get("models") if isinstance(settings, dict) else {}
    if isinstance(models, dict) and models.get(role):
        return models[role]
    return _config_value(config_key)


def sanitize_openai_proxy_environment():
    proxy_names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    for name in proxy_names:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if host in {"127.0.0.1", "localhost", "::1"} and parsed.port == 9:
            os.environ.pop(name, None)


def generate_abap(prompt_text, source_text, response_format=None):
    return call_openai(
        prompt_text,
        source_text,
        model_name=_model_for_role("abap_generation", "OPENAI_ABAP_GENERATION_MODEL"),
        response_format=response_format,
    )


def generate_dependency_analysis(prompt_text, source_text, response_format=None):
    return call_openai(
        prompt_text,
        source_text,
        model_name=_model_for_role("dependency_analysis", "OPENAI_DEPENDENCY_ANALYSIS_MODEL"),
        response_format=response_format,
    )


def generate_functional_specification(prompt_text, source_text, response_format=None):
    return call_openai(
        prompt_text,
        source_text,
        model_name=_model_for_role("general", "OPENAI_MODEL"),
        response_format=response_format,
    )


def generate_code_review_repair(prompt_text, source_text):
    return call_openai(prompt_text, source_text, model_name=_model_for_role("code_review", "OPENAI_CODE_REVIEW_MODEL"))


def generate_with_model(model_name):
    def generator(prompt_text, source_text, response_format=None):
        return call_openai(prompt_text, source_text, model_name=model_name, response_format=response_format)

    return generator


def call_openai(prompt_text, source_text, model_name=None, response_format=None):
    api_key = _config_value("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing. Set it in this project's .env file.")

    from openai import OpenAI

    model = model_name or _model_for_role("general", "OPENAI_MODEL") or "gpt-5.6-luna"
    sanitize_openai_proxy_environment()
    client = OpenAI(api_key=api_key)
    request = {
        "model": model,
        "input": [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": source_text},
        ],
    }
    if response_format:
        request["text"] = {"format": response_format}
    response = client.responses.create(**request)
    return {
        "text": response.output_text,
        "model": model,
        "usage": _extract_usage(response),
        "raw_response_json": _response_json(response),
    }


def _extract_usage(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    def value(name):
        if isinstance(usage, dict):
            return usage.get(name)
        return getattr(usage, name, None)

    return {
        "input_tokens": value("input_tokens"),
        "output_tokens": value("output_tokens"),
        "total_tokens": value("total_tokens"),
    }


def _response_json(response):
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    if hasattr(response, "to_dict"):
        return response.to_dict()
    return str(response)

