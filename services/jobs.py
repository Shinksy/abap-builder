import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path

from services.create_abap import cost_breakdown_from_job_artifacts, cost_breakdown_has_entries, load_metrics
from services.job_options import load_job_options, save_job_options
from services.progress import get_progress


RERUN_METADATA_ARTIFACT = "rerun.json"


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
    rerun_metadata = load_rerun_metadata(job_folder)
    total_cost = job_total_cost(metrics, job_folder)

    return {
        "job_id": job_id,
        "started_at": started_at,
        "started_at_display": format_datetime(started_at),
        "sort_timestamp": timestamp_for_sort(started_at, job_folder),
        "job_title": job_title(options),
        "specification_name": specification_name(job_folder, uploads_path / job_id),
        "job_mode": mode,
        "job_mode_display": job_mode_label(mode),
        "rerun_of": rerun_metadata.get("source_job_id"),
        "rerun_of_display": short_job_id(rerun_metadata.get("source_job_id")),
        "model_preset": model_settings.get("preset") or options.get("model_preset"),
        "model_preset_label": model_settings.get("preset_label") or "Unavailable",
        "status": progress.get("status") or "Unavailable",
        "duration_seconds": duration_seconds(metrics, progress),
        "duration_display": format_duration(duration_seconds(metrics, progress)),
        "total_cost": total_cost,
        "total_cost_display": format_cost(total_cost),
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


def prepare_rerun_job(jobs_folder, upload_folder, source_job_id, new_job_id):
    jobs_path = Path(jobs_folder)
    uploads_path = Path(upload_folder)
    source_job_folder = jobs_path / source_job_id
    source_upload_folder = uploads_path / source_job_id
    if not source_job_folder.exists() or not source_job_folder.is_dir():
        return None

    new_job_folder = jobs_path / new_job_id
    new_upload_folder = uploads_path / new_job_id
    new_job_folder.mkdir(parents=True, exist_ok=True)
    new_upload_folder.mkdir(parents=True, exist_ok=True)
    mode = inferred_job_mode(source_job_folder, source_upload_folder)
    options = load_job_options(jobs_path, source_job_id)
    options["rerun_of"] = source_job_id
    options["rerun"] = True

    save_job_options(jobs_path, new_job_id, options)

    rerun_metadata = {
        "source_job_id": source_job_id,
        "source_job_short_id": short_job_id(source_job_id),
        "mode": mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if mode == "enhance_existing_abap":
        source_path = copy_existing_input(
            existing_input_path(source_job_folder, source_upload_folder),
            new_upload_folder / "original_existing.abap",
        )
        specification_path = copy_existing_input(
            enhancement_specification_path(source_job_folder, source_upload_folder),
            new_upload_folder / "enhancement_specification.txt",
        )
        if not source_path or not specification_path:
            return None
        rerun_metadata.update(
            {
                "source_path": str(source_path),
                "specification_path": str(specification_path),
            }
        )
        save_rerun_metadata(new_job_folder, rerun_metadata)
        return {
            "mode": mode,
            "source_path": source_path,
            "specification_path": specification_path,
            "rerun_metadata": rerun_metadata,
        }

    input_path = copy_existing_input(
        create_job_specification_path(source_job_folder, source_upload_folder),
        new_upload_folder / rerun_specification_filename(source_job_folder, source_upload_folder),
    )
    if not input_path:
        return None
    rerun_metadata["input_path"] = str(input_path)
    save_rerun_metadata(new_job_folder, rerun_metadata)
    return {
        "mode": "create_abap",
        "input_path": input_path,
        "rerun_metadata": rerun_metadata,
    }


def create_job_specification_path(job_folder, upload_job_folder):
    functional_spec_context = read_json(Path(job_folder) / "functional_specification_context.json")
    functional_spec_source_path = functional_spec_context.get("source_path")
    functional_spec_source = Path(functional_spec_source_path) if functional_spec_source_path else None
    if functional_spec_source and functional_spec_source.exists() and functional_spec_source.is_file():
        return functional_spec_source
    context = read_json(Path(job_folder) / "processing_plan_context.json")
    context_input_path = context.get("input_path")
    context_path = Path(context_input_path) if context_input_path else None
    if context_path and context_path.exists() and context_path.is_file():
        return context_path
    for folder in (Path(upload_job_folder), Path(job_folder)):
        path = first_input_file(folder)
        if path:
            return path
    accepted = Path(job_folder) / "accepted_functional_specification.txt"
    if accepted.exists():
        return accepted
    return None


def existing_input_path(job_folder, upload_job_folder):
    preferred = Path(job_folder) / "original_existing.abap"
    if preferred.exists():
        return preferred
    for folder in (Path(upload_job_folder), Path(job_folder)):
        if not folder.exists():
            continue
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix.lower() in {".abap", ".txt"} and path.name != "enhancement_specification.txt":
                return path
    return None


def enhancement_specification_path(job_folder, upload_job_folder):
    for folder in (Path(job_folder), Path(upload_job_folder)):
        path = folder / "enhancement_specification.txt"
        if path.exists():
            return path
    return None


def first_input_file(folder):
    if not Path(folder).exists():
        return None
    ignored = {
        "status.json",
        "options.json",
        "metrics.json",
        "generated.abap",
        RERUN_METADATA_ARTIFACT,
    }
    for path in sorted(Path(folder).iterdir()):
        if path.is_file() and path.name not in ignored and not path.name.endswith(".json"):
            return path
    return None


def copy_existing_input(source_path, destination_path):
    if not source_path or not Path(source_path).exists():
        return None
    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination)
    return destination


def rerun_specification_filename(job_folder, upload_job_folder):
    path = create_job_specification_path(job_folder, upload_job_folder)
    if not path:
        return "rerun_specification.txt"
    name = Path(path).name
    return name if name else "rerun_specification.txt"


def save_rerun_metadata(job_folder, metadata):
    Path(job_folder, RERUN_METADATA_ARTIFACT).write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )


def load_rerun_metadata(job_folder):
    return read_json(Path(job_folder) / RERUN_METADATA_ARTIFACT)


def short_job_id(job_id):
    text = str(job_id or "").strip()
    return text[:8] if text else ""


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


def job_title(options):
    title = str((options or {}).get("job_title") or "").strip()
    return title if title else "-"


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


def job_total_cost(metrics, job_folder):
    cost = metrics.get("estimated_total_cost") if isinstance(metrics, dict) else None
    if isinstance(cost, (int, float)):
        return cost
    breakdown = (metrics or {}).get("cost_breakdown") if isinstance(metrics, dict) else None
    if not cost_breakdown_has_entries(breakdown):
        breakdown = cost_breakdown_from_job_artifacts(job_folder)
    return total_cost_from_breakdown(breakdown)


def total_cost_from_breakdown(breakdown):
    if not cost_breakdown_has_entries(breakdown):
        return None
    for collection_name in ("by_model", "by_stage"):
        rows = breakdown.get(collection_name) or []
        total = 0.0
        found = False
        for row in rows:
            value = row.get("estimated_total_cost") if isinstance(row, dict) else None
            if isinstance(value, (int, float)):
                total += float(value)
                found = True
        if found:
            return total
    return None


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
