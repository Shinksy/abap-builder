import json
from pathlib import Path

from services.model_settings import DEFAULT_MODEL_PRESET, normalize_model_settings


DEFAULT_JOB_OPTIONS = {
    "run_sap_syntax_check": False,
    "sap_syntax_check_attempts": 2,
    "model_preset": DEFAULT_MODEL_PRESET,
}
MIN_SAP_SYNTAX_CHECK_ATTEMPTS = 1
MAX_SAP_SYNTAX_CHECK_ATTEMPTS = 10


def save_job_options(jobs_folder, job_id, options):
    job_folder = Path(jobs_folder) / job_id
    job_folder.mkdir(parents=True, exist_ok=True)
    normalized = normalize_job_options(options)
    (job_folder / "options.json").write_text(
        json.dumps(normalized, indent=2),
        encoding="utf-8",
    )


def load_job_options(jobs_folder, job_id):
    options_path = Path(jobs_folder) / job_id / "options.json"
    if not options_path.exists():
        return DEFAULT_JOB_OPTIONS.copy()
    try:
        loaded = json.loads(options_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULT_JOB_OPTIONS.copy()
    return normalize_job_options(loaded if isinstance(loaded, dict) else {})


def normalize_job_options(options):
    normalized = {**DEFAULT_JOB_OPTIONS, **(options or {})}
    normalized["run_sap_syntax_check"] = bool(normalized.get("run_sap_syntax_check"))
    normalized["sap_syntax_check_attempts"] = clamp_sap_syntax_check_attempts(
        normalized.get("sap_syntax_check_attempts")
    )
    normalized["model_settings"] = normalize_model_settings(
        normalized.get("model_settings") or normalized,
        config={},
    )
    normalized["model_preset"] = normalized["model_settings"]["preset"]
    return normalized


def clamp_sap_syntax_check_attempts(value):
    try:
        attempts = int(value)
    except (TypeError, ValueError):
        attempts = DEFAULT_JOB_OPTIONS["sap_syntax_check_attempts"]
    return max(MIN_SAP_SYNTAX_CHECK_ATTEMPTS, min(MAX_SAP_SYNTAX_CHECK_ATTEMPTS, attempts))
