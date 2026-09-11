import os
from pathlib import Path

from dotenv import load_dotenv

from services.model_settings import MODEL_PRESETS, DEFAULT_MODEL_PRESET, allowed_model


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE_PATH = BASE_DIR / ".env"


def load_env_file(env_path):
    path = Path(env_path)
    if not path.exists():
        return

    load_dotenv(path, override=True)
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


load_env_file(ENV_FILE_PATH)


def env_bool(name, default=False, fallback_name=None):
    value = os.environ.get(name)
    if value is None and fallback_name:
        value = os.environ.get(fallback_name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


class Config:
    BASE_DIR = BASE_DIR
    ENV_FILE_PATH = str(ENV_FILE_PATH)
    SAP_DDIC_METADATA_ENABLED_RAW = os.environ.get("SAP_DDIC_METADATA_ENABLED")
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev")
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", str(BASE_DIR / "uploads"))
    JOBS_FOLDER = os.environ.get("JOBS_FOLDER", str(BASE_DIR / "jobs"))
    CREATE_ABAP_PROMPT = os.environ.get(
        "CREATE_ABAP_PROMPT",
        str(BASE_DIR / "prompts" / "create_abap.txt"),
    )
    PREPARE_FUNCTIONAL_SPEC_PROMPT = os.environ.get(
        "PREPARE_FUNCTIONAL_SPEC_PROMPT",
        str(BASE_DIR / "prompts" / "prepare_functional_specification.txt"),
    )
    ENHANCE_ABAP_PROMPT = os.environ.get(
        "ENHANCE_ABAP_PROMPT",
        str(BASE_DIR / "prompts" / "enhance_existing_abap.txt"),
    )
    ABAP_MODIFICATION_DEVELOPER = os.environ.get("ABAP_MODIFICATION_DEVELOPER", "")
    ABAP_MODIFICATION_LOG_NUMBER = os.environ.get("ABAP_MODIFICATION_LOG_NUMBER", "")
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
    ANTHROPIC_API_URL = os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
    ANTHROPIC_API_VERSION = os.environ.get("ANTHROPIC_API_VERSION", "2023-06-01")
    ANTHROPIC_MAX_TOKENS = env_int("ANTHROPIC_MAX_TOKENS", 4096)
    SAP_BTP_TOKEN_URL = os.environ.get("SAP_BTP_TOKEN_URL")
    SAP_BTP_INFERENCE_URL = os.environ.get("SAP_BTP_INFERENCE_URL")
    SAP_BTP_CLIENT_ID = os.environ.get("SAP_BTP_CLIENT_ID")
    SAP_BTP_CLIENT_SECRET = os.environ.get("SAP_BTP_CLIENT_SECRET")
    SAP_BTP_AI_RESOURCE_GROUP = os.environ.get("SAP_BTP_AI_RESOURCE_GROUP")
    SAP_BTP_CLAUDE_ANTHROPIC_VERSION = os.environ.get("SAP_BTP_CLAUDE_ANTHROPIC_VERSION", "bedrock-2023-05-31")
    SAP_BTP_CLAUDE_MAX_TOKENS = env_int("SAP_BTP_CLAUDE_MAX_TOKENS", 1000)
    DEFAULT_OPENAI_MODELS = MODEL_PRESETS[DEFAULT_MODEL_PRESET]["models"]
    OPENAI_MODEL = allowed_model(os.environ.get("OPENAI_MODEL"), DEFAULT_OPENAI_MODELS["general"])
    OPENAI_ABAP_GENERATION_MODEL = allowed_model(
        os.environ.get("OPENAI_ABAP_GENERATION_MODEL"),
        DEFAULT_OPENAI_MODELS["abap_generation"],
    )
    OPENAI_DEPENDENCY_ANALYSIS_MODEL = allowed_model(
        os.environ.get("OPENAI_DEPENDENCY_ANALYSIS_MODEL"),
        OPENAI_MODEL,
    )
    OPENAI_CODE_REVIEW_MODEL = allowed_model(os.environ.get("OPENAI_CODE_REVIEW_MODEL"), OPENAI_MODEL)
    SAP_DEPENDENCY_ANALYSIS_ENABLED = env_bool("SAP_DEPENDENCY_ANALYSIS_ENABLED", default=False)
    SAP_DDIC_METADATA_ENABLED = env_bool("SAP_DDIC_METADATA_ENABLED", fallback_name="USE_SAP_METADATA")
    SAP_API_BASE_URL = os.environ.get("SAP_API_BASE_URL")
    SAP_API_USER = os.environ.get("SAP_API_USER")
    SAP_API_PASSWORD = os.environ.get("SAP_API_PASSWORD")
    SAP_API_TIMEOUT = env_int("SAP_API_TIMEOUT", 30)
    SAP_API_VERIFY = os.environ.get("SAP_API_VERIFY", "true").lower() != "false"
    SAP_API_CLIENT = os.environ.get("SAP_API_CLIENT", "100")
    SAP_METADATA_CACHE_MODE = os.environ.get(
        "SAP_METADATA_CACHE_MODE",
        os.environ.get("SAP_DDIC_METADATA_MODE", "cache_first"),
    )
    SAP_METADATA_CACHE_DIR = os.environ.get("SAP_METADATA_CACHE_DIR", str(BASE_DIR / "cache"))
    SAP_DDIC_METADATA_MODE = SAP_METADATA_CACHE_MODE
    SAP_DDIC_CACHE_DIR = os.environ.get("SAP_DDIC_CACHE_DIR", str(Path(SAP_METADATA_CACHE_DIR) / "ddic"))
    SAP_DDIC_CACHE_PATH = os.environ.get("SAP_DDIC_CACHE_PATH", str(Path(SAP_METADATA_CACHE_DIR) / "ddic_metadata.json"))
    SAP_CALLABLE_METADATA_MODE = os.environ.get("SAP_CALLABLE_METADATA_MODE", SAP_METADATA_CACHE_MODE)
    SAP_CALLABLE_CACHE_DIR = os.environ.get("SAP_CALLABLE_CACHE_DIR", str(Path(SAP_METADATA_CACHE_DIR) / "callables"))
    SAP_SYNTAX_CHECK_URL = os.environ.get("SAP_SYNTAX_CHECK_URL")
    SAP_SYNTAX_CHECK_TIMEOUT_SECONDS = env_int("SAP_SYNTAX_CHECK_TIMEOUT_SECONDS", SAP_API_TIMEOUT)
    SAP_FUNCTION_SIGNATURE_URL = os.environ.get("SAP_FUNCTION_SIGNATURE_URL")
    SAP_METHOD_SIGNATURE_URL = os.environ.get("SAP_METHOD_SIGNATURE_URL")
    SAP_DDIC_CACHE_MAX_TABLES = int(os.environ.get("SAP_DDIC_CACHE_MAX_TABLES", "128"))
    MODEL_PRICING = {
        "gpt-4.1-mini": {
            "input_per_1m_tokens": 0.40,
            "output_per_1m_tokens": 1.60,
        },
        "gpt-5-mini": {
            "input_per_1m_tokens": 0.25,
            "output_per_1m_tokens": 2.00,
        },
        "gpt-5.6-luna": {
            "input_per_1m_tokens": 1.00,
            "output_per_1m_tokens": 6.00,
        },
        "gpt-5.6-terra": {
            "input_per_1m_tokens": 2.50,
            "output_per_1m_tokens": 15.00,
        },
        "gpt-5.6-sol": {
            "input_per_1m_tokens": 5.00,
            "output_per_1m_tokens": 30.00,
        }
    }
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
