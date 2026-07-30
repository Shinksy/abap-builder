from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import quote


class MetadataCacheUploadError(ValueError):
    pass


METADATA_TYPE_LABELS = {
    "table": "Table / Structure",
    "function": "Function Module",
    "method": "Class Method",
}


def prepare_metadata_cache_upload(metadata_type, metadata, config):
    selected_type = normalize_metadata_type(metadata_type)
    if not isinstance(metadata, dict):
        raise MetadataCacheUploadError("Metadata upload must contain a JSON object.")
    if selected_type == "table":
        return prepare_table_upload(metadata, config)
    if selected_type == "function":
        return prepare_function_upload(metadata, config)
    return prepare_method_upload(metadata, config)


def save_metadata_cache_upload(prepared_upload):
    destination = Path(prepared_upload["destination_path"])
    root = Path(prepared_upload["cache_root"])
    ensure_child_path(root, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(prepared_upload["metadata"], indent=2), encoding="utf-8")


def normalize_metadata_type(metadata_type):
    selected_type = str(metadata_type or "").strip().lower()
    if selected_type not in METADATA_TYPE_LABELS:
        raise MetadataCacheUploadError("Select a metadata type to upload.")
    return selected_type


def prepare_table_upload(metadata, config):
    normalized = deepcopy(metadata)
    name = normalize_object_name(normalized.get("name"), "Table / Structure metadata is missing required property: name.")
    fields = normalized.get("fields")
    if not isinstance(fields, dict):
        raise MetadataCacheUploadError("Table / Structure metadata must contain a fields object.")
    normalized["name"] = name
    cache_root = Path(config["SAP_DDIC_CACHE_DIR"])
    destination = cache_root / f"{cache_file_stem(name)}.json"
    ensure_child_path(cache_root, destination)
    return prepared_upload("table", name, cache_root, destination, normalized)


def prepare_function_upload(metadata, config):
    normalized = deepcopy(metadata)
    name = normalize_object_name(normalized.get("name"), "Function Module metadata is missing required property: name.")
    parameters = normalized.get("parameters")
    if not isinstance(parameters, dict):
        raise MetadataCacheUploadError("Function Module metadata must contain a parameters object.")
    normalized["name"] = name
    cache_root = Path(config["SAP_CALLABLE_CACHE_DIR"]) / "functions"
    destination = cache_root / f"{cache_file_stem(name)}.json"
    ensure_child_path(cache_root, destination)
    return prepared_upload("function", name, cache_root, destination, normalized)


def prepare_method_upload(metadata, config):
    normalized = deepcopy(metadata)
    class_name = normalize_object_name(normalized.get("class"), "Class Method metadata is missing required property: class.")
    method_name = normalize_object_name(normalized.get("method"), "Class Method metadata is missing required property: method.")
    parameters = normalized.get("parameters")
    if not isinstance(parameters, dict):
        raise MetadataCacheUploadError("Class Method metadata must contain a parameters object.")
    normalized["class"] = class_name
    normalized["method"] = method_name
    logical_name = f"{class_name}=>{method_name}"
    cache_root = Path(config["SAP_CALLABLE_CACHE_DIR"]) / "methods"
    destination = cache_root / f"{cache_file_stem(logical_name)}.json"
    ensure_child_path(cache_root, destination)
    return prepared_upload("method", logical_name, cache_root, destination, normalized)


def prepared_upload(metadata_type, object_name, cache_root, destination, metadata):
    return {
        "metadata_type": metadata_type,
        "metadata_type_label": METADATA_TYPE_LABELS[metadata_type],
        "object_name": object_name,
        "cache_root": str(Path(cache_root)),
        "destination_path": str(Path(destination)),
        "metadata": metadata,
    }


def parse_metadata_upload_json(uploaded_file=None, payload_text=None):
    if payload_text:
        raw_text = payload_text
    else:
        if not uploaded_file:
            raise MetadataCacheUploadError("Select a metadata JSON file to upload.")
        raw_bytes = uploaded_file.read()
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MetadataCacheUploadError("Metadata upload must be a UTF-8 JSON file.") from exc
    try:
        parsed = json.loads(raw_text or "")
    except json.JSONDecodeError as exc:
        raise MetadataCacheUploadError(f"Metadata upload is not valid JSON: {exc.msg}.") from exc
    if not isinstance(parsed, dict):
        raise MetadataCacheUploadError("Metadata upload must contain a JSON object.")
    return parsed


def normalize_object_name(value, error_message):
    name = str(value or "").strip().upper()
    if not name:
        raise MetadataCacheUploadError(error_message)
    if any(part == ".." for part in name.replace("\\", "/").split("/")):
        raise MetadataCacheUploadError("Metadata object names must not contain path traversal.")
    return name


def cache_file_stem(name):
    return quote(str(name or "").strip().upper(), safe="")


def ensure_child_path(root, path):
    root_path = Path(root).resolve(strict=False)
    target_path = Path(path).resolve(strict=False)
    try:
        target_path.relative_to(root_path)
    except ValueError as exc:
        raise MetadataCacheUploadError("Metadata cache destination is outside the configured cache folder.") from exc
