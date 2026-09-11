import re
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher


@dataclass
class ModificationConvention:
    identifier: str
    next_identifier: str
    id_label: str
    date_label: str
    name_label: str
    log_label: str
    description_label: str
    entry_start: int
    insert_at: int
    entry_lines: list
    marker_prefix: str
    date_format: str


def apply_modification_history(
    original_source,
    final_source,
    changed_source=None,
    developer_name=None,
    log_number=None,
    description=None,
    current_date=None,
):
    convention = detect_modification_convention(original_source)
    if not convention:
        return {
            "source": final_source or "",
            "issues": [modification_history_warning(original_source)],
            "modification_id": "",
        }

    current_date = current_date or date.today()
    new_source = mark_changed_abap_lines(
        original_source,
        final_source,
        convention.next_identifier,
        convention.marker_prefix,
        changed_source=changed_source,
    )
    new_source = insert_modification_history_entry(
        new_source,
        convention,
        developer_name=developer_name,
        log_number=log_number,
        description=description,
        current_date=current_date,
    )
    return {
        "source": new_source,
        "issues": [],
        "modification_id": convention.next_identifier,
    }


def detect_modification_convention(source):
    lines = str(source or "").splitlines()
    header_end = header_scan_end(lines)
    header_lines = lines[:header_end]
    history_index = find_history_heading(header_lines)
    if history_index is None:
        return None
    separators = [
        index
        for index in range(history_index + 1, len(header_lines))
        if re.match(r"^\*{5,}\s*$", header_lines[index].strip())
    ]
    if len(separators) < 2:
        return None
    entries = []
    for start, end in zip(separators, separators[1:]):
        parsed = parse_history_entry(header_lines[start + 1 : end])
        if parsed:
            parsed["start"] = start
            parsed["end"] = end
            parsed["lines"] = header_lines[start + 1 : end]
            entries.append(parsed)
    if not entries:
        return None

    id_choice = choose_identifier_field(entries, "\n".join(lines[header_end:]))
    if not id_choice:
        return None
    date_label = choose_label(entries, date_value, preferred=("date",), exclude={id_choice["label"]})
    if not date_label:
        return None
    template_entry = entries[-1]
    marker_prefix = detect_marker_prefix("\n".join(lines[header_end:]), id_choice["values"]) or '"'
    return ModificationConvention(
        identifier=id_choice["latest"],
        next_identifier=increment_identifier(id_choice["latest"]),
        id_label=id_choice["label"],
        date_label=date_label,
        name_label=choose_label(
            entries,
            lambda value: bool(re.search(r"[A-Za-z]", value)),
            preferred=("name", "developer", "author"),
            exclude={id_choice["label"], date_label},
        )
        or "",
        log_label=choose_label(
            entries,
            lambda value: bool(value.strip()),
            preferred=("log", "request", "ticket", "number"),
            exclude={id_choice["label"], date_label},
        )
        or "",
        description_label=choose_label(
            entries,
            lambda value: bool(value.strip()),
            preferred=("description", "desc", "summary"),
            exclude={id_choice["label"], date_label},
        )
        or "",
        entry_start=template_entry["start"] + 1,
        insert_at=template_entry["end"],
        entry_lines=[header_lines[template_entry["start"]]] + list(template_entry["lines"]),
        marker_prefix=marker_prefix,
        date_format=detect_date_format(first_label_value(entries, date_label)),
    )


def header_scan_end(lines):
    saw_comment_block = False
    for index, line in enumerate(lines[:200]):
        stripped = line.strip()
        if stripped.startswith("*"):
            saw_comment_block = True
            continue
        if not stripped:
            continue
        if re.match(r"^(REPORT|PROGRAM)\b", stripped, re.IGNORECASE):
            continue
        if saw_comment_block:
            return index
    return min(len(lines), 200)


def find_history_heading(lines):
    for index, line in enumerate(lines):
        text = comment_text(line).lower()
        if "history" in text and re.search(r"\b(revision|modification|change)\b", text):
            return index
    return None


def parse_history_entry(lines):
    fields = []
    for line_index, line in enumerate(lines):
        text = comment_text(line)
        for match in re.finditer(r"(^|\s{2,})([A-Za-z][A-Za-z0-9 /_-]{1,24}?)\s*:\s*", text):
            fields.append(
                {
                    "label": normalize_label(match.group(2)),
                    "line": line_index,
                    "label_start": match.start(2),
                    "value_start": match.end(),
                }
            )
    if not fields:
        return {}
    for index, field in enumerate(fields):
        line = comment_text(lines[field["line"]])
        next_on_line = next((item for item in fields[index + 1 :] if item["line"] == field["line"]), None)
        end = next_on_line["label_start"] if next_on_line else len(line)
        field["value"] = line[field["value_start"] : end].strip(" *")
    values = {}
    for field in fields:
        if field["value"]:
            values.setdefault(field["label"], []).append(field["value"])
    return {"fields": fields, "values": values}


def choose_identifier_field(entries, body_text):
    candidates = {}
    for entry in entries:
        for label, values in entry["values"].items():
            for value in values:
                if not incrementable_identifier(value):
                    continue
                candidates.setdefault(label, []).append(value.strip())
    scored = []
    for label, values in candidates.items():
        latest = latest_identifier(values)
        marker_count = sum(len(re.findall(rf'"\s*{re.escape(value)}\b', body_text, re.IGNORECASE)) for value in values)
        score = marker_count * 100 + len(values)
        if re.search(r"\b(mod|revision|id|marker)\b", label, re.IGNORECASE):
            score += 20
        if re.search(r"\b(log|request|ticket)\b", label, re.IGNORECASE):
            score -= 20
        scored.append((score, label, values, latest))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return {"label": scored[0][1], "values": scored[0][2], "latest": scored[0][3]}


def choose_label(entries, predicate, preferred=(), exclude=None):
    exclude = set(exclude or [])
    labels = []
    for entry in entries:
        for label, values in entry["values"].items():
            if label in exclude:
                continue
            if any(predicate(value) for value in values):
                labels.append(label)
    if not labels:
        return ""
    for word in preferred:
        matches = [label for label in labels if word.lower() in label.lower()]
        if matches:
            return most_common(matches)
    return most_common(labels)


def first_label_value(entries, label):
    for entry in entries:
        values = entry["values"].get(label) or []
        if values:
            return values[0]
    return ""


def incrementable_identifier(value):
    return bool(re.match(r"^([A-Za-z_][A-Za-z0-9_]*?)(\d+)$", str(value or "").strip()))


def latest_identifier(values):
    parsed = []
    for value in values:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*?)(\d+)$", value.strip())
        if match:
            parsed.append((match.group(1), len(match.group(2)), int(match.group(2)), value.strip()))
    parsed.sort(key=lambda item: item[2])
    return parsed[-1][3]


def increment_identifier(value):
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*?)(\d+)$", str(value or "").strip())
    if not match:
        return ""
    prefix, number = match.groups()
    return f"{prefix}{int(number) + 1:0{len(number)}d}"


def date_value(value):
    return bool(
        re.match(r"^\d{2}\.\d{2}\.\d{4}$", value.strip())
        or re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip())
        or re.match(r"^\d{2}/\d{2}/\d{4}$", value.strip())
    )


def detect_date_format(value):
    text = str(value or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return "%Y-%m-%d"
    if re.match(r"^\d{2}/\d{2}/\d{4}$", text):
        return "%d/%m/%Y"
    return "%d.%m.%Y"


def insert_modification_history_entry(source, convention, developer_name=None, log_number=None, description=None, current_date=None):
    lines = str(source or "").splitlines()
    entry_lines = list(convention.entry_lines)
    replacements = {
        convention.date_label: (current_date or date.today()).strftime(convention.date_format),
        convention.id_label: convention.next_identifier,
    }
    if convention.name_label:
        replacements[convention.name_label] = str(developer_name or "").strip()
    if convention.log_label:
        replacements[convention.log_label] = str(log_number or "").strip()
    if convention.description_label:
        replacements[convention.description_label] = short_description(description)
    entry_lines = [replace_entry_values(line, replacements) for line in entry_lines]
    insert_at = min(max(convention.insert_at, 0), len(lines))
    lines[insert_at:insert_at] = entry_lines
    return "\n".join(lines)


def replace_entry_values(line, replacements):
    text = comment_text(line)
    matches = list(re.finditer(r"(^|\s{2,})([A-Za-z][A-Za-z0-9 /_-]{1,24}?)\s*:\s*", text))
    if not matches:
        return line
    rebuilt = text
    offset = 0
    for index, match in enumerate(matches):
        label = normalize_label(match.group(2))
        if label not in replacements:
            continue
        start = match.end() + offset
        end = (matches[index + 1].start(2) + offset) if index + 1 < len(matches) else len(rebuilt)
        end = max(start, end)
        value = str(replacements[label] or "")
        width = end - start
        replacement = value[:width].ljust(width)
        rebuilt = rebuilt[:start] + replacement + rebuilt[end:]
        offset += len(replacement) - width
    return rebuild_comment_line(line, rebuilt)


def mark_changed_abap_lines(original_source, final_source, mod_id, marker_prefix, changed_source=None):
    original_lines = str(original_source or "").splitlines()
    final_lines = str(final_source or "").splitlines()
    reference_lines = str(changed_source if changed_source is not None else final_source or "").splitlines()
    original_codes = {normalized_markable_code(line) for line in original_lines}
    original_codes.discard("")
    changed_codes = counted_changed_codes(original_lines, reference_lines, original_codes)
    if not changed_codes:
        return "\n".join(final_lines)
    marked_counts = {}
    marked = []
    for line in final_lines:
        code = normalized_markable_code(line)
        marked_count = marked_counts.get(code, 0)
        if (
            code
            and marked_count < changed_codes.get(code, 0)
            and is_markable_abap_line(line)
            and not line_has_marker(line, mod_id)
        ):
            marked.append(add_line_marker(line, mod_id, marker_prefix))
            marked_counts[code] = marked_count + 1
        else:
            marked.append(line)
    return "\n".join(marked)


def counted_changed_codes(original_lines, changed_lines, original_codes):
    counts = {}
    matcher = SequenceMatcher(a=original_lines, b=changed_lines, autojunk=False)
    for tag, _original_start, _original_end, changed_start, changed_end in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            for line in changed_lines[changed_start:changed_end]:
                code = normalized_markable_code(line)
                if code and code not in original_codes:
                    counts[code] = counts.get(code, 0) + 1
    return counts


def normalized_markable_code(line):
    code = line.split('"', 1)[0].strip()
    if not code or code.startswith("*"):
        return ""
    return re.sub(r"\s+", " ", code).upper()


def is_markable_abap_line(line):
    code = line.split('"', 1)[0].strip()
    return bool(code) and not code.startswith("*")


def add_line_marker(line, mod_id, marker_prefix):
    prefix = marker_prefix or '"'
    if prefix.strip() == '"':
        marker = f'{prefix}{mod_id}' if prefix == '"' else f'{prefix}{mod_id}'
    else:
        marker = f'{prefix}{mod_id}'
    return f"{line.rstrip()}  {marker}"


def line_has_marker(line, mod_id):
    return bool(re.search(rf'"\s*{re.escape(mod_id)}\b', line, re.IGNORECASE))


def detect_marker_prefix(body_text, identifiers):
    counts = {}
    for identifier in identifiers:
        for match in re.finditer(rf'(" ?){re.escape(identifier)}\b', body_text, re.IGNORECASE):
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)[0][0]


def modification_history_warning(source):
    return {
        "rule_id": "ENHANCEMENT_MODIFICATION_HISTORY_UNDETECTED",
        "severity": "warning",
        "line_number": 1,
        "message": "Modification history convention was not recognised; no deterministic modification history entry or change markers were added.",
        "source_line": first_non_blank_line(source),
        "suggested_fix": "Add or clarify an existing modification history convention before applying automatic modification markers.",
    }


def short_description(text):
    flattened = re.sub(r"\s+", " ", str(text or "").strip())
    if not flattened:
        return ""
    sentence = re.split(r"(?<=[.!?])\s+", flattened, maxsplit=1)[0].strip()
    return sentence[:60]


def normalize_label(label):
    return re.sub(r"\s+", " ", str(label or "").strip())


def most_common(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)[0][0]


def comment_text(line):
    text = str(line or "")
    if text.startswith("*"):
        text = text[1:]
    if text.endswith("*"):
        text = text[:-1]
    return text


def rebuild_comment_line(original_line, text):
    line = str(original_line or "")
    prefix = "*" if line.startswith("*") else ""
    suffix = "*" if line.endswith("*") else ""
    rebuilt = prefix + text
    if suffix:
        rebuilt = rebuilt.rstrip().ljust(max(len(line) - 1, len(rebuilt))) + suffix
    return rebuilt


def first_non_blank_line(source):
    for line in str(source or "").splitlines():
        if line.strip():
            return line
    return ""
