MODEL_ROLES = (
    "general",
    "dependency_analysis",
    "abap_generation",
    "code_review",
)

MODEL_ROLE_ENV_KEYS = {
    "general": "OPENAI_MODEL",
    "dependency_analysis": "OPENAI_DEPENDENCY_ANALYSIS_MODEL",
    "abap_generation": "OPENAI_ABAP_GENERATION_MODEL",
    "code_review": "OPENAI_CODE_REVIEW_MODEL",
}

ALLOWED_OPENAI_MODELS = (
    "gpt-5-mini",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
)

MODEL_PRESETS = {
    "economy": {
        "label": "Economy",
        "models": {
            "general": "gpt-5.6-luna",
            "dependency_analysis": "gpt-5.6-luna",
            "abap_generation": "gpt-5.6-terra",
            "code_review": "gpt-5.6-luna",
        },
    },
    "balanced": {
        "label": "Balanced",
        "models": {
            "general": "gpt-5.6-luna",
            "dependency_analysis": "gpt-5.6-luna",
            "abap_generation": "gpt-5.6-terra",
            "code_review": "gpt-5.6-terra",
        },
    },
    "best_quality": {
        "label": "Best Quality",
        "models": {
            "general": "gpt-5.6-terra",
            "dependency_analysis": "gpt-5.6-terra",
            "abap_generation": "gpt-5.6-sol",
            "code_review": "gpt-5.6-sol",
        },
    },
}

DEFAULT_MODEL_PRESET = "balanced"


def allowed_model(value, fallback):
    text = str(value or "").strip()
    return text if text in ALLOWED_OPENAI_MODELS else fallback


def env_default_model_settings(config):
    balanced = MODEL_PRESETS[DEFAULT_MODEL_PRESET]["models"]
    general = allowed_model(config.get("OPENAI_MODEL"), balanced["general"])
    return {
        "general": general,
        "dependency_analysis": allowed_model(
            config.get("OPENAI_DEPENDENCY_ANALYSIS_MODEL"),
            general,
        ),
        "abap_generation": allowed_model(
            config.get("OPENAI_ABAP_GENERATION_MODEL"),
            balanced["abap_generation"],
        ),
        "code_review": allowed_model(
            config.get("OPENAI_CODE_REVIEW_MODEL"),
            general,
        ),
    }


def model_preset_payloads():
    return [
        {"name": name, "label": preset["label"], "models": dict(preset["models"])}
        for name, preset in MODEL_PRESETS.items()
    ]


def model_settings_for_template(config):
    return {
        "allowed_models": list(ALLOWED_OPENAI_MODELS),
        "model_display_names": {
            "gpt-5-mini": "GPT-5 Mini",
            "gpt-5.6-luna": "GPT-5.6 Luna",
            "gpt-5.6-terra": "GPT-5.6 Terra",
            "gpt-5.6-sol": "GPT-5.6 Sol",
        },
        "presets": model_preset_payloads(),
        "default_preset": DEFAULT_MODEL_PRESET,
        "role_labels": {
            "general": "General",
            "dependency_analysis": "Dependency Analysis",
            "abap_generation": "ABAP Generation",
            "code_review": "Code Review",
        },
        "role_env_keys": dict(MODEL_ROLE_ENV_KEYS),
        "env_defaults": env_default_model_settings(config),
    }


def normalize_model_settings(options=None, config=None):
    options = options or {}
    config = config or {}
    preset = str(options.get("model_preset") or options.get("preset") or DEFAULT_MODEL_PRESET).strip().lower()
    saved_models = options.get("models") if isinstance(options.get("models"), dict) else {}
    if preset in MODEL_PRESETS:
        models = dict(MODEL_PRESETS[preset]["models"])
        label = MODEL_PRESETS[preset]["label"]
    elif preset == "advanced":
        defaults = env_default_model_settings(config)
        models = {
            role: allowed_model(
                options.get(MODEL_ROLE_ENV_KEYS[role]) or options.get(role) or saved_models.get(role),
                defaults[role],
            )
            for role in MODEL_ROLES
        }
        label = "Advanced"
    else:
        preset = DEFAULT_MODEL_PRESET
        models = dict(MODEL_PRESETS[preset]["models"])
        label = MODEL_PRESETS[preset]["label"]
    return {
        "provider": "openai",
        "preset": preset,
        "preset_label": label,
        "models": models,
    }


def model_options_from_form(form):
    preset = str((form or {}).get("model_preset") or DEFAULT_MODEL_PRESET).strip().lower()
    options = {"model_preset": preset}
    if preset == "advanced":
        for role, env_key in MODEL_ROLE_ENV_KEYS.items():
            options[env_key] = str((form or {}).get(env_key) or "").strip()
    return options
