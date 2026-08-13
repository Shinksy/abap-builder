import json
import time
from pathlib import Path
from threading import Thread

from services.llm import (
    generate_functional_specification,
    reset_current_model_settings,
    set_current_model_settings,
)
from services.progress import update_progress


FUNCTIONAL_SPEC_PROPOSAL_ARTIFACT = "functional_specification_proposal.json"
FUNCTIONAL_SPEC_CONTEXT_ARTIFACT = "functional_specification_context.json"
ACCEPTED_FUNCTIONAL_SPEC_ARTIFACT = "accepted_functional_specification.txt"


def start_prepare_functional_spec_job(
    job_id,
    source_path,
    jobs_folder,
    prompt_path,
    model_settings=None,
    spec_generator=None,
):
    thread = Thread(
        target=run_prepare_functional_spec,
        kwargs={
            "job_id": job_id,
            "source_path": source_path,
            "jobs_folder": jobs_folder,
            "prompt_path": prompt_path,
            "model_settings": model_settings or {},
            "spec_generator": spec_generator,
        },
        daemon=True,
    )
    thread.start()
    return thread


def run_prepare_functional_spec(
    job_id,
    source_path,
    jobs_folder,
    prompt_path,
    model_settings=None,
    spec_generator=None,
):
    job_folder = Path(jobs_folder) / job_id
    job_folder.mkdir(parents=True, exist_ok=True)
    model_settings = model_settings or {}
    generator = spec_generator or generate_functional_specification
    model_settings_token = set_current_model_settings(model_settings)
    started_at = time.perf_counter()
    try:
        update_progress(
            jobs_folder,
            job_id,
            "Running",
            "Preparing functional specification...",
            stage="preparing_functional_specification",
        )
        source_text = Path(source_path).read_text(encoding="utf-8")
        prompt_text = Path(prompt_path).read_text(encoding="utf-8")
        result = generator(prompt_text, source_text)
        converted_text = clean_prepared_specification(result.get("text") if isinstance(result, dict) else result)
        if not converted_text:
            raise RuntimeError("The functional specification preparation LLM returned an empty specification.")
        payload = {
            "source_path": str(source_path),
            "prompt_path": str(prompt_path),
            "original_specification": source_text,
            "converted_specification": converted_text,
            "model": result.get("model") if isinstance(result, dict) else None,
            "usage": result.get("usage") if isinstance(result, dict) else None,
            "duration_seconds": time.perf_counter() - started_at,
        }
        if isinstance(result, dict) and result.get("raw_response_json") is not None:
            payload["raw_response_json"] = result.get("raw_response_json")
        save_functional_spec_proposal(job_folder, payload)
        save_functional_spec_context(
            job_folder,
            {
                "source_path": str(source_path),
                "prompt_path": str(prompt_path),
                "model_settings": model_settings,
                "usage": payload.get("usage"),
                "section_durations": {
                    "functional_specification_preparation": payload.get("duration_seconds"),
                },
            },
        )
        update_progress(
            jobs_folder,
            job_id,
            "Awaiting Review",
            "Review the prepared functional specification before ABAP generation.",
            stage="awaiting_functional_specification_review",
        )
    except Exception as exc:
        update_progress(
            jobs_folder,
            job_id,
            "Error",
            f"Functional specification preparation failed: {exc}",
            stage="preparing_functional_specification",
        )
        raise
    finally:
        reset_current_model_settings(model_settings_token)


def clean_prepared_specification(text):
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def save_functional_spec_proposal(job_folder, payload):
    Path(job_folder, FUNCTIONAL_SPEC_PROPOSAL_ARTIFACT).write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


def save_functional_spec_context(job_folder, context):
    Path(job_folder, FUNCTIONAL_SPEC_CONTEXT_ARTIFACT).write_text(
        json.dumps(context, indent=2),
        encoding="utf-8",
    )


def load_functional_spec_proposal(jobs_folder, job_id):
    path = Path(jobs_folder) / job_id / FUNCTIONAL_SPEC_PROPOSAL_ARTIFACT
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def load_functional_spec_context(jobs_folder, job_id):
    path = Path(jobs_folder) / job_id / FUNCTIONAL_SPEC_CONTEXT_ARTIFACT
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def accept_functional_spec_for_job(jobs_folder, job_id, edited_specification):
    text = str(edited_specification or "").strip()
    if not text:
        return None
    job_folder = Path(jobs_folder) / job_id
    path = job_folder / ACCEPTED_FUNCTIONAL_SPEC_ARTIFACT
    path.write_text(text, encoding="utf-8")
    return path


def reject_functional_spec_for_job(jobs_folder, job_id):
    update_progress(
        jobs_folder,
        job_id,
        "Rejected",
        "Functional specification preparation rejected.",
        stage="functional_specification_rejected",
    )
