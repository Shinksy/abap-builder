import os
from urllib.parse import urlparse

from flask import current_app, has_app_context

from config import Config


def _config_value(name):
    if has_app_context():
        return current_app.config.get(name)
    return getattr(Config, name, None)


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
    return call_openai(prompt_text, source_text, model_name="gpt-5-mini", response_format=response_format)


def generate_dependency_analysis(prompt_text, source_text):
    return call_openai(prompt_text, source_text)


def generate_code_review_repair(prompt_text, source_text):
    return call_openai(prompt_text, source_text)


def call_openai(prompt_text, source_text, model_name=None, response_format=None):
    api_key = _config_value("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing. Set it in this project's .env file.")

    from openai import OpenAI

    model = model_name or _config_value("OPENAI_MODEL") or "gpt-4.1-mini"
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

