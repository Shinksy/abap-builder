import re
from difflib import SequenceMatcher

from services.ddic_metadata_context import (
    extract_relevant_ddic_names,
    extract_relevant_ddic_names_from_source,
    retrieve_ddic_metadata,
)
from services.fixer import fix_list_processing_leave_report
from services.validator import validate_abap


CONTROL_FLOW_PATTERN = re.compile(
    r"^\s*(LEAVE\s+LIST-PROCESSING|LEAVE\s+REPORT|LEAVE\s+PROGRAM|LEAVE\s+SCREEN|LEAVE\s+TO\s+SCREEN\s+\d+|RETURN|EXIT|CHECK\b.*|STOP|CONTINUE)\s*\.",
    re.IGNORECASE,
)

PROTECTED_RULES = {
    "ABAP_INVALID_PARAMETER_DECLARATION",
    "ABAP_BROKEN_CHAINED_DECLARATION",
    "ABAP_UNVERIFIED_DDIC_IDENTIFIER",
}
ABAP_HYPHEN_KEYWORDS = {
    "LIST-PROCESSING",
    "USER-COMMAND",
    "LINE-SELECTION",
    "START-OF-SELECTION",
    "END-OF-SELECTION",
    "TOP-OF-PAGE",
    "END-OF-PAGE",
    "SELECT-OPTIONS",
    "FIELD-SYMBOLS",
}


def accept_modified_source(
    original_source,
    proposed_source,
    approved_ranges=None,
    functional_specification="",
    sap_metadata=None,
    ddic_metadata_provider=None,
    change_plan=None,
):
    approved_ranges = approved_ranges or []
    if sap_metadata is None and ddic_metadata_provider is not None:
        sap_metadata = retrieve_ddic_metadata(
            extract_relevant_ddic_names_from_source(original_source, proposed_source)
            + extract_relevant_ddic_names(functional_specification),
            ddic_metadata_provider,
        )
    provenance = build_identifier_provenance(
        original_source=original_source,
        functional_specification=functional_specification,
        sap_metadata=sap_metadata,
        change_plan=change_plan,
    )
    proposed_lines = proposed_source.splitlines()
    original_lines = original_source.splitlines()
    _preview_lines, pre_restore_ddic_issues = restore_unapproved_ddic_mutations(
        original_lines,
        proposed_lines,
        provenance,
    )
    restored_lines = restore_unapproved_line_changes(original_lines, proposed_lines, approved_ranges)
    issues = []
    issues.extend(control_flow_preservation_issues(original_lines, proposed_lines, approved_ranges))
    issues.extend(pre_restore_ddic_issues)
    restored_lines, ddic_restore_issues = restore_unapproved_ddic_mutations(
        original_lines,
        restored_lines,
        provenance,
    )
    issues.extend(dedupe_issue_list(ddic_restore_issues, issues))

    final_source = "\n".join(restored_lines)
    final_source, list_fixes = fix_list_processing_leave_report(final_source)
    issues.extend({"rule_id": fix["rule_id"], "message": fix["description"]} for fix in list_fixes)
    validation_issues = validate_abap(
        final_source,
        identifier_provenance=provenance,
    )
    issues.extend(item for item in validation_issues if item["rule_id"] in PROTECTED_RULES)

    blocking = [item for item in issues if item["rule_id"] in PROTECTED_RULES and not item.get("restored")]
    changed = final_source != original_source
    return {
        "accepted": changed and not blocking,
        "final_source": final_source if changed and not blocking else original_source,
        "issues": issues,
        "full_regeneration_used": False,
    }


def restore_unapproved_line_changes(original_lines, proposed_lines, approved_ranges):
    if len(original_lines) != len(proposed_lines):
        return restore_by_diff(original_lines, proposed_lines, approved_ranges)
    restored = []
    for index, proposed_line in enumerate(proposed_lines, start=1):
        if proposed_line != original_lines[index - 1] and not is_approved_line(index, approved_ranges):
            restored.append(original_lines[index - 1])
        else:
            restored.append(proposed_line)
    return restored


def restore_by_diff(original_lines, proposed_lines, approved_ranges):
    restored = []
    matcher = SequenceMatcher(a=original_lines, b=proposed_lines)
    for tag, original_start, original_end, proposed_start, proposed_end in matcher.get_opcodes():
        original_numbers = range(original_start + 1, original_end + 1)
        if tag == "equal":
            restored.extend(proposed_lines[proposed_start:proposed_end])
        elif tag == "insert" and (
            is_approved_line(original_start, approved_ranges)
            or is_approved_line(original_start + 1, approved_ranges)
        ):
            restored.extend(proposed_lines[proposed_start:proposed_end])
        elif original_numbers and all(is_approved_line(number, approved_ranges) for number in original_numbers):
            restored.extend(proposed_lines[proposed_start:proposed_end])
        else:
            restored.extend(original_lines[original_start:original_end])
    return restored


def control_flow_preservation_issues(original_lines, proposed_lines, approved_ranges):
    issues = []
    for index, original_line in enumerate(original_lines, start=1):
        if index > len(proposed_lines) or is_approved_line(index, approved_ranges):
            continue
        original_statement = normalized_control_flow_statement(original_line)
        proposed_statement = normalized_control_flow_statement(proposed_lines[index - 1])
        if original_statement and proposed_statement and original_statement != proposed_statement:
            issues.append(
                {
                    "rule_id": "ABAP_UNAPPROVED_CONTROL_FLOW_CHANGE",
                    "line_number": index,
                    "message": "Control-flow statement changed outside approved edit ranges.",
                    "source_line": proposed_lines[index - 1],
                    "original_statement": original_statement,
                    "proposed_statement": proposed_statement,
                }
            )
    return issues


def normalized_control_flow_statement(line):
    code = line.split('"', 1)[0]
    match = CONTROL_FLOW_PATTERN.match(code)
    if not match:
        return None
    return " ".join(match.group(1).upper().split())


def is_approved_line(line_number, approved_ranges):
    return any(start <= line_number <= end for start, end in approved_ranges)


def dedupe_issue_list(candidates, existing):
    seen = {
        (
            item.get("rule_id"),
            item.get("line_number"),
            item.get("proposed_identifier"),
        )
        for item in existing
    }
    deduped = []
    for item in candidates:
        key = (
            item.get("rule_id"),
            item.get("line_number"),
            item.get("proposed_identifier"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def build_identifier_provenance(original_source="", functional_specification="", sap_metadata=None, change_plan=None):
    provenance = {}
    add_identifiers(provenance, qualified_ddic_identifiers(original_source), "existing-source")
    add_identifiers(provenance, qualified_ddic_identifiers(functional_specification), "functional-specification")
    add_identifiers(provenance, metadata_identifiers(sap_metadata), "sap-metadata")
    add_identifiers(provenance, change_plan_identifiers(change_plan), "approved-change-plan")
    return provenance


def add_identifiers(provenance, identifiers, source):
    for identifier in identifiers:
        key = normalize_identifier(identifier)
        if key:
            provenance.setdefault(key, {"identifier": identifier.upper(), "source": source})


def qualified_ddic_identifiers(text):
    if not text:
        return set()
    return {
        match.group(0).upper()
        for match in re.finditer(r"\b[A-Za-z_]\w*-[A-Za-z_]\w*\b", text)
        if match.group(0).upper() not in ABAP_HYPHEN_KEYWORDS
    }


def metadata_identifiers(sap_metadata):
    identifiers = set()
    if not sap_metadata:
        return identifiers
    if isinstance(sap_metadata, dict):
        for key in ("ddic_fields", "fields", "identifiers"):
            values = sap_metadata.get(key)
            if isinstance(values, (list, tuple, set)):
                identifiers.update(str(value).upper() for value in values if "-" in str(value))
        tables = sap_metadata.get("tables") if isinstance(sap_metadata.get("tables"), dict) else sap_metadata
        if isinstance(tables, dict):
            for table_name, table_metadata in tables.items():
                if isinstance(table_metadata, dict):
                    fields = table_metadata.get("fields") or table_metadata.get("components") or []
                elif isinstance(table_metadata, (list, tuple, set)):
                    fields = table_metadata
                else:
                    fields = []
                for field_name in fields:
                    identifiers.add(f"{table_name}-{field_name}".upper())
    return identifiers


def change_plan_identifiers(change_plan):
    identifiers = set()
    if not change_plan:
        return identifiers
    entries = []
    if isinstance(change_plan, dict):
        entries.extend(change_plan.get("identifier_provenance", []))
        entries.extend(change_plan.get("approved_identifiers", []))
    elif isinstance(change_plan, list):
        entries.extend(change_plan)
    for entry in entries:
        if isinstance(entry, dict):
            identifier = entry.get("identifier")
            source = entry.get("source")
            if identifier and source != "generated-local" and "-" in identifier:
                identifiers.add(identifier.upper())
        elif isinstance(entry, str) and "-" in entry:
            identifiers.add(entry.upper())
    return identifiers


def restore_unapproved_ddic_mutations(original_lines, restored_lines, provenance):
    fixed_lines = list(restored_lines)
    issues = []
    for index, line in enumerate(restored_lines):
        original_line = original_lines[index] if index < len(original_lines) else ""
        original_identifiers = qualified_ddic_identifiers(original_line)
        if not original_identifiers:
            continue
        for proposed_identifier in qualified_ddic_identifiers(line):
            if normalize_identifier(proposed_identifier) in provenance:
                continue
            closest = closest_identifier(proposed_identifier, original_identifiers)
            if not closest or not suspicious_identifier_match(proposed_identifier, closest):
                continue
            fixed_lines[index] = replace_identifier_preserving_case(fixed_lines[index], proposed_identifier, closest)
            issues.append(unverified_identifier_issue(index + 1, line, proposed_identifier, closest, "existing-source"))
    return fixed_lines, issues


def replace_identifier_preserving_case(line, proposed_identifier, replacement):
    return re.sub(
        rf"\b{re.escape(proposed_identifier)}\b",
        replacement.lower() if proposed_identifier.islower() else replacement,
        line,
        flags=re.IGNORECASE,
    )


def unverified_identifier_issue(line_number, source_line, proposed_identifier, closest_identifier_text, closest_source):
    return {
        "rule_id": "ABAP_UNVERIFIED_DDIC_IDENTIFIER",
        "severity": "error",
        "line_number": line_number,
        "message": f"Unverified SAP identifier {proposed_identifier.upper()} is not approved; closest known identifier is {closest_identifier_text.upper()} from {closest_source}.",
        "source_line": source_line,
        "suggested_fix": "Use an approved SAP identifier from source, specification, SAP metadata, or the approved change plan.",
        "proposed_identifier": proposed_identifier.upper(),
        "closest_identifier": closest_identifier_text.upper() if closest_identifier_text else None,
        "closest_source": closest_source,
        "restored": True,
    }


def normalize_identifier(identifier):
    return identifier.upper() if identifier else ""


def closest_identifier(identifier, candidates):
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: levenshtein_distance(normalize_identifier(identifier), normalize_identifier(candidate)))


def suspicious_identifier_match(identifier, candidate):
    proposed = normalize_identifier(identifier)
    known = normalize_identifier(candidate)
    table_proposed, _, field_proposed = proposed.partition("-")
    table_known, _, field_known = known.partition("-")
    if table_proposed != table_known:
        return levenshtein_distance(proposed, known) <= 2
    return (
        levenshtein_distance(field_proposed, field_known) <= 2
        or field_proposed == field_known + field_known[-1:]
        or field_known == field_proposed + field_proposed[-1:]
        or sorted([field_proposed, field_known], key=len)[0] + "S" == sorted([field_proposed, field_known], key=len)[1]
    )


def levenshtein_distance(left, right):
    if left == right:
        return 0
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[right_index] + 1,
                    current[right_index - 1] + 1,
                    previous[right_index - 1] + (0 if left_char == right_char else 1),
                )
            )
        previous = current
    return previous[-1]
