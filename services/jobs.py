import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path

from services.create_abap import load_metrics
from services.job_options import load_job_options
from services.progress import get_progress


def list_jobs(jobs_folder, upload_folder):
    jobs_path = Path(jobs_folder)
    uploads_path = Path(upload_folder)
    if not jobs_path.exists():
        return []

    rows = []
    for job_folder in jobs_path.iterdir():
        if not job_folder.is_dir():
            continue
        rows.append(job_summary(jobs_path, uploads_path, job_folder.name))
    return sorted(rows, key=lambda row: row["sort_timestamp"], reverse=True)


def job_summary(jobs_folder, upload_folder, job_id):
    jobs_path = Path(jobs_folder)
    uploads_path = Path(upload_folder)
    job_folder = jobs_path / job_id
    progress = get_progress(jobs_path, job_id)
    metrics = load_metrics(jobs_path, job_id)
    options = load_job_options(jobs_path, job_id)
    model_settings = (
        options.get("model_settings")
        or metrics.get("model_settings")
        or {}
    )
    started_at = progress.get("started_at") or folder_timestamp(job_folder)
    generated_exists = (
        ((job_folder / "generated.abap").exists() or (job_folder / "sap_syntax_repaired.abap").exists())
        and not bool(progress.get("is_active"))
    )
    mode = metrics.get("job_mode") or inferred_job_mode(job_folder, uploads_path / job_id)

    return {
        "job_id": job_id,
        "started_at": started_at,
        "started_at_display": format_datetime(started_at),
        "sort_timestamp": timestamp_for_sort(started_at, job_folder),
        "specification_name": specification_name(job_folder, uploads_path / job_id),
        "job_mode": mode,
        "job_mode_display": job_mode_label(mode),
        "model_preset": model_settings.get("preset") or options.get("model_preset"),
        "model_preset_label": model_settings.get("preset_label") or "Unavailable",
        "status": progress.get("status") or "Unavailable",
        "duration_seconds": duration_seconds(metrics, progress),
        "duration_display": format_duration(duration_seconds(metrics, progress)),
        "total_cost": metrics.get("estimated_total_cost"),
        "total_cost_display": format_cost(metrics.get("estimated_total_cost")),
        "view_target": "result" if generated_exists else "progress",
        "has_result": generated_exists,
    }


def delete_job(jobs_folder, upload_folder, job_id):
    jobs_path = Path(jobs_folder)
    uploads_path = Path(upload_folder)
    job_folder = jobs_path / job_id
    upload_job_folder = uploads_path / job_id
    if not job_folder.exists():
        return False
    remove_tree_inside(jobs_path, job_folder)
    if upload_job_folder.exists():
        remove_tree_inside(uploads_path, upload_job_folder)
    return True


def remove_tree_inside(root, target):
    root_path = Path(root).resolve()
    target_path = Path(target).resolve()
    if root_path == target_path or root_path not in target_path.parents:
        raise ValueError(f"Refusing to delete outside expected folder: {target_path}")
    shutil.rmtree(target_path, onexc=make_writable_and_retry)


def make_writable_and_retry(function, path, _excinfo):
    os.chmod(path, stat.S_IWRITE)
    function(path)


def specification_name(job_folder, upload_job_folder):
    for folder in (Path(upload_job_folder), Path(job_folder)):
        if not folder.exists():
            continue
        files = sorted(path for path in folder.iterdir() if path.is_file())
        preferred = [
            path
            for path in files
            if path.name not in {"status.json", "options.json", "metrics.json", "generated.abap"}
            and not path.name.endswith(".json")
        ]
        if preferred:
            return preferred[0].name
    context = read_json(Path(job_folder) / "processing_plan_context.json")
    input_path = context.get("input_path")
    if input_path:
        return Path(input_path).name
    return "Unavailable"


def inferred_job_mode(job_folder, upload_job_folder):
    if (Path(job_folder) / "enhancement_specification.txt").exists():
        return "enhance_existing_abap"
    if (Path(upload_job_folder) / "enhancement_specification.txt").exists():
        return "enhance_existing_abap"
    return "create_abap"


def job_mode_label(mode):
    if mode == "enhance_existing_abap":
        return "Enhance Existing ABAP"
    return "Generate New Program"


def duration_seconds(metrics, progress):
    value = metrics.get("duration_seconds")
    if isinstance(value, (int, float)):
        return float(value)
    value = progress.get("elapsed_seconds")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def format_duration(value):
    if isinstance(value, (int, float)):
        return f"{value:.2f}s"
    return "Unavailable"


def format_cost(value):
    if isinstance(value, (int, float)):
        return f"${value:.6f}"
    return "Unavailable"


def format_datetime(value):
    parsed = parse_datetime(value)
    if not parsed:
        return "Unavailable"
    return parsed.astimezone().strftime("%d-%m-%Y %H:%M:%S")


def timestamp_for_sort(value, job_folder):
    parsed = parse_datetime(value)
    if parsed:
        return parsed.timestamp()
    try:
        return Path(job_folder).stat().st_mtime
    except OSError:
        return 0


def folder_timestamp(job_folder):
    try:
        return datetime.fromtimestamp(Path(job_folder).stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_json(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
