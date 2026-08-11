import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


PROGRESS_STAGES = [
    ("Queued", 0),
    ("Reading specification", 10),
    ("Loading prompt and templates", 15),
    ("Analyzing dependencies", 20),
    ("Loading SAP metadata", 25),
    ("Extracting declaration requirements", 30),
    ("Extracting processing plan", 33),
    ("Processing chunks", 35),
    ("Cleaning generated ABAP", 55),
    ("Running deterministic validation", 65),
    ("Applying safe deterministic fixes", 75),
    ("Re-running deterministic validation", 85),
    ("Saving results", 95),
    ("Complete", 100),
]
STAGE_ALIASES = {
    "extracting_processing_plan": ("Extracting processing plan", 33),
    "awaiting_processing_plan_review": ("Extracting processing plan", 34),
    "awaiting_enhancement_review": ("Processing chunks", 54),
    "processing_plan_approved": ("Extracting processing plan", 34),
    "processing_plan_rejected": ("Extracting processing plan", 34),
    "generating_abap": ("Processing chunks", 35),
}
STAGE_PERCENTAGES = dict(PROGRESS_STAGES)
STAGE_PERCENTAGES.update({alias: percent for alias, (_display_name, percent) in STAGE_ALIASES.items()})
TERMINAL_STATUSES = {"Complete", "Error", "Rejected"}
PAUSED_STATUSES = {"Awaiting Review"}
DDIC_ACTIVITY_MESSAGES = {
    "Calling SAP to get DDIC metadata",
    "Called SAP to get DDIC metadata",
    "Loading DDIC metadata from cache",
    "Loaded DDIC metadata from cache",
}

DEFAULT_PROGRESS = {
    "status": "Queued",
    "stage": "Queued",
    "current_stage": "Queued",
    "current_stage_title": "Queued",
    "message": "Queued for processing.",
    "stage_message": "Queued for processing.",
    "progress_percent": 0,
    "completed_stages": [],
    "started_at": None,
    "updated_at": None,
    "completed_at": None,
    "elapsed_seconds": 0,
    "is_active": True,
    "activity_messages": [],
    "stages": [
        {"name": name, "percent": percent, "completed": False}
        for name, percent in PROGRESS_STAGES
    ],
}


def create_job(jobs_folder):
    job_id = uuid4().hex
    update_progress(jobs_folder, job_id, "Queued", "Queued for processing.", stage="Queued")
    return job_id


def update_progress(jobs_folder, job_id, status, message, stage=None):
    job_folder = Path(jobs_folder) / job_id
    job_folder.mkdir(parents=True, exist_ok=True)
    progress_path = job_folder / "status.json"
    previous = _read_progress(progress_path)
    now = _now()
    current_stage = stage or _stage_for_status(status, previous)
    completed_at = previous.get("completed_at")
    if status in TERMINAL_STATUSES:
        completed_at = now
    activity_messages = list(previous.get("activity_messages", []))
    if is_activity_message(message):
        activity_messages.append(message)

    progress_path.write_text(
        json.dumps(
            _build_progress(
                status=status,
                stage=current_stage,
                message=message,
                started_at=previous.get("started_at") or now,
                updated_at=now,
                completed_at=completed_at,
                activity_messages=activity_messages,
            ),
            indent=2,
        ),
        encoding="utf-8",
    )


def get_progress(jobs_folder, job_id):
    progress_path = Path(jobs_folder) / job_id / "status.json"
    if not progress_path.exists():
        return DEFAULT_PROGRESS.copy()
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULT_PROGRESS.copy()
    return _build_progress(
        status=progress.get("status", DEFAULT_PROGRESS["status"]),
        stage=progress.get("stage") or progress.get("current_stage") or DEFAULT_PROGRESS["stage"],
        message=progress.get("stage_message") or progress.get("message") or DEFAULT_PROGRESS["message"],
        started_at=progress.get("started_at"),
        updated_at=progress.get("updated_at"),
        completed_at=progress.get("completed_at"),
        activity_messages=progress.get("activity_messages", []),
    )


def _read_progress(progress_path):
    if not progress_path.exists():
        return {}
    try:
        return json.loads(progress_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _build_progress(status, stage, message, started_at, updated_at, completed_at, activity_messages=None):
    percent = progress_percent_for_stage(stage)
    if status == "Error":
        percent = min(percent, 95)
    current_stage_name = progress_stage_list_name(stage)
    completed_stages = [
        name
        for name, stage_percent in PROGRESS_STAGES
        if stage_percent < percent or (status == "Complete" and stage_percent == percent)
    ]
    return {
        "status": status,
        "stage": stage,
        "current_stage": stage,
        "current_stage_title": progress_stage_display_title(stage),
        "message": message,
        "stage_message": message,
        "progress_percent": percent,
        "completed_stages": completed_stages,
        "started_at": started_at,
        "updated_at": updated_at,
        "completed_at": completed_at,
        "elapsed_seconds": _elapsed_seconds(started_at, completed_at or updated_at),
        "is_active": status not in TERMINAL_STATUSES and status not in PAUSED_STATUSES,
        "activity_messages": list(activity_messages or []),
        "stages": [
            {
                "name": name,
                "title": progress_stage_display_title(name),
                "percent": stage_percent,
                "completed": name in completed_stages,
                "current": name == current_stage_name,
            }
            for name, stage_percent in PROGRESS_STAGES
        ],
    }


def _stage_for_status(status, previous):
    if status in STAGE_PERCENTAGES:
        return status
    return previous.get("stage") or previous.get("current_stage") or DEFAULT_PROGRESS["stage"]


def progress_percent_for_stage(stage):
    stage_name = str(stage or "")
    chunk_match = re.match(r"^Processing Chunk (\d+) of (\d+)$", stage_name, re.IGNORECASE)
    if chunk_match:
        chunk_index = int(chunk_match.group(1))
        chunk_total = max(1, int(chunk_match.group(2)))
        chunk_index = min(max(1, chunk_index), chunk_total)
        chunk_start = STAGE_PERCENTAGES["Processing chunks"]
        chunk_end = STAGE_PERCENTAGES["Cleaning generated ABAP"]
        return chunk_start + int((chunk_end - chunk_start) * (chunk_index - 1) / chunk_total)
    return STAGE_PERCENTAGES.get(stage, 0)


def progress_stage_list_name(stage):
    if re.match(r"^Processing Chunk \d+ of \d+$", str(stage or ""), re.IGNORECASE):
        return "Processing chunks"
    alias = STAGE_ALIASES.get(str(stage or ""))
    return alias[0] if alias else stage


def progress_stage_display_title(stage):
    stage_name = str(stage or "")
    if "_" in stage_name:
        return "".join(part[:1].upper() + part[1:].lower() for part in stage_name.split("_") if part)
    return progress_stage_list_name(stage_name)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _elapsed_seconds(started_at, finished_at):
    if not started_at or not finished_at:
        return 0
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(finished_at)
    except ValueError:
        return 0
    return max(0, int((finished - started).total_seconds()))


def is_activity_message(message):
    return str(message or "") in DDIC_ACTIVITY_MESSAGES
