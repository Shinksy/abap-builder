import os
import time
from contextvars import ContextVar
from urllib.parse import urlparse

import requests
from flask import current_app, has_app_context

from config import Config


_current_model_settings = ContextVar("current_model_settings", default=None)
_sap_btp_token = None
_sap_btp_token_expires_at = 0


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


def _provider_for_current_settings():
    settings = _current_model_settings.get()
    provider = settings.get("provider") if isinstance(settings, dict) else None
    return str(provider or "openai").strip().lower()


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
    return call_llm(
        prompt_text,
        source_text,
        model_name=_model_for_role("abap_generation", "OPENAI_ABAP_GENERATION_MODEL"),
        response_format=response_format,
    )


def generate_dependency_analysis(prompt_text, source_text, response_format=None):
    return call_llm(
        prompt_text,
        source_text,
        model_name=_model_for_role("dependency_analysis", "OPENAI_DEPENDENCY_ANALYSIS_MODEL"),
        response_format=response_format,
    )


def generate_functional_specification(prompt_text, source_text, response_format=None):
    return call_llm(
        prompt_text,
        source_text,
        model_name=_model_for_role("general", "OPENAI_MODEL"),
        response_format=response_format,
    )


def generate_code_review_repair(prompt_text, source_text):
    return call_llm(prompt_text, source_text, model_name=_model_for_role("code_review", "OPENAI_CODE_REVIEW_MODEL"))


def generate_with_model(model_name):
    def generator(prompt_text, source_text, response_format=None):
        return call_openai(prompt_text, source_text, model_name=model_name, response_format=response_format)

    return generator


def call_llm(prompt_text, source_text, model_name=None, response_format=None):
    if _provider_for_current_settings() == "anthropic":
        return call_anthropic(prompt_text, source_text, model_name=model_name, response_format=response_format)
    return call_openai(prompt_text, source_text, model_name=model_name, response_format=response_format)


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


def call_anthropic(prompt_text, source_text, model_name=None, response_format=None):
    api_key = _config_value("ANTHROPIC_API_KEY")
    if not api_key:
        return call_sap_btp_claude(prompt_text, source_text, model_name=model_name, response_format=response_format)

    model = model_name or "claude-sonnet-5"
    system_prompt = prompt_text
    user_text = source_text
    if response_format:
        user_text = (
            f"{source_text}\n\n"
            "Return only valid JSON that satisfies the requested schema. "
            "Do not wrap the JSON in Markdown fences."
        )
    request = {
        "model": model,
        "max_tokens": _config_value("ANTHROPIC_MAX_TOKENS") or 4096,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_text}],
    }
    response = requests.post(
        _config_value("ANTHROPIC_API_URL") or "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": _config_value("ANTHROPIC_API_VERSION") or "2023-06-01",
            "content-type": "application/json",
        },
        json=request,
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    return {
        "text": _anthropic_text(payload),
        "model": payload.get("model") or model,
        "usage": _extract_anthropic_usage(payload),
        "raw_response_json": payload,
    }


def call_sap_btp_claude(prompt_text, source_text, model_name=None, response_format=None):
    _validate_sap_btp_claude_config()
    token = _sap_btp_access_token()
    combined_prompt = f"{prompt_text}\n\n{source_text}"
    if response_format:
        combined_prompt = (
            f"{combined_prompt}\n\n"
            "Return only valid JSON that satisfies the requested schema. "
            "Do not wrap the JSON in Markdown fences."
        )
    request = {
        "anthropic_version": _config_value("SAP_BTP_CLAUDE_ANTHROPIC_VERSION") or "bedrock-2023-05-31",
        "max_tokens": _config_value("SAP_BTP_CLAUDE_MAX_TOKENS") or 1000,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": combined_prompt}],
            }
        ],
    }
    sanitize_openai_proxy_environment()
    response = requests.post(
        _config_value("SAP_BTP_INFERENCE_URL"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "AI-Resource-Group": _config_value("SAP_BTP_AI_RESOURCE_GROUP"),
        },
        json=request,
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    return {
        "text": _anthropic_text(payload),
        "model": model_name or payload.get("model") or "Claude - SAP BTP",
        "usage": _extract_anthropic_usage(payload),
        "raw_response_json": payload,
    }


def _validate_sap_btp_claude_config():
    missing = [
        name
        for name in (
            "SAP_BTP_TOKEN_URL",
            "SAP_BTP_INFERENCE_URL",
            "SAP_BTP_CLIENT_ID",
            "SAP_BTP_CLIENT_SECRET",
            "SAP_BTP_AI_RESOURCE_GROUP",
        )
        if not _config_value(name)
    ]
    if missing:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is missing and SAP BTP Claude is not fully configured. "
            f"Missing: {', '.join(missing)}."
        )


def _sap_btp_access_token():
    global _sap_btp_token, _sap_btp_token_expires_at
    now = time.time()
    if _sap_btp_token and _sap_btp_token_expires_at > now + 60:
        return _sap_btp_token
    sanitize_openai_proxy_environment()
    response = requests.post(
        _config_value("SAP_BTP_TOKEN_URL"),
        data={"grant_type": "client_credentials"},
        auth=(_config_value("SAP_BTP_CLIENT_ID"), _config_value("SAP_BTP_CLIENT_SECRET")),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("SAP OAuth response did not contain an access_token.")
    _sap_btp_token = token
    _sap_btp_token_expires_at = now + int(payload.get("expires_in", 3600) or 3600)
    return token


def _anthropic_text(payload):
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    choices = payload.get("choices")
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {})
        if isinstance(message.get("content"), str):
            return message["content"]
    text_parts = []
    for block in payload.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
    if text_parts:
        return "".join(text_parts)
    if isinstance(payload.get("content"), str):
        return payload["content"]
    return ""


def _extract_anthropic_usage(payload):
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = None
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
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

