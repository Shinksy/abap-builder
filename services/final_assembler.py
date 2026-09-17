import re

from services.abap_source import split_code_and_comment, statement_ends


APP_FINAL_ASSEMBLY_MODE = "app"
LLM_FINAL_ASSEMBLY_MODE = "llm"
FINAL_ASSEMBLY_MODES = {APP_FINAL_ASSEMBLY_MODE, LLM_FINAL_ASSEMBLY_MODE}


def normalize_final_assembly_mode(value):
    mode = str(value or "").strip().lower()
    if mode in FINAL_ASSEMBLY_MODES:
        return mode
    return APP_FINAL_ASSEMBLY_MODE


def assemble_final_abap_from_chunks(chunks):
    return assemble_final_abap_from_chunks_with_diagnostics(chunks)["text"]


def assemble_final_abap_from_chunks_with_diagnostics(chunks):
    sections = {
        "declarations": [],
        "other_global": [],
        "main_event": [],
        "forms": [],
    }
    diagnostics = {
        "assembler_selected": "Python",
        "chunks_received": [],
        "chunks_included": [],
        "assembly_order": [],
        "unplaced_chunks": [],
        "missing_chunks": [],
        "final_assembled_source_length": 0,
    }
    for chunk in chunks or []:
        name = str((chunk or {}).get("name") or "chunk")
        text = str((chunk or {}).get("text") or "")
        diagnostics["chunks_received"].append(
            {
                "name": name,
                "source_length": len(text),
                "has_text": bool(text.strip()),
            }
        )
        if not text.strip():
            diagnostics["missing_chunks"].append({"name": name, "reason": "empty generated chunk text"})
            continue
        if name == "declarations":
            declaration_units = declaration_statement_units(text)
            split = split_chunk(text)
            sections["declarations"].extend(declaration_units)
            record_included_chunk(
                diagnostics,
                name,
                declarations=len(declaration_units),
            )
            record_unplaced_units(diagnostics, name, "main event statements in declarations chunk", split["main_event"])
            record_unplaced_units(diagnostics, name, "FORM routines in declarations chunk", split["forms"])
            continue
        split = split_chunk(text)
        if name == "main_program_flow":
            sections["main_event"].extend(split["main_event"])
            record_unplaced_units(diagnostics, name, "FORM routines in main program flow chunk", split["forms"])
        else:
            sections["forms"].extend(split["forms"])
            record_unplaced_units(diagnostics, name, "main event statements outside main program flow chunk", split["main_event"])
        sections["other_global"].extend(split["other"])
        record_included_chunk(
            diagnostics,
            name,
            global_other=len(split["other"]),
            main_event=len(split["main_event"]),
            forms=len(split["forms"]),
        )
    global_units = dedupe_global_declarations(sections["declarations"] + sections["other_global"])
    form_units = dedupe_form_units(sections["forms"])
    assembled_sections = [
        ("global_declarations", join_units(global_units)),
        ("main_event", join_units(sections["main_event"])),
        ("forms", join_units(form_units)),
    ]
    source = "\n".join(
        section
        for _name, section in assembled_sections
        if section
    )
    diagnostics["assembly_order"] = [
        {"section": name, "source_length": len(section)}
        for name, section in assembled_sections
        if section
    ]
    diagnostics["final_assembled_source_length"] = len(source)
    return {"text": source, "diagnostics": diagnostics}


def record_included_chunk(diagnostics, name, declarations=0, global_other=0, main_event=0, forms=0):
    included_counts = {
        "declarations": declarations,
        "global_other": global_other,
        "main_event": main_event,
        "forms": forms,
    }
    total = sum(included_counts.values())
    if not total:
        diagnostics["unplaced_chunks"].append({"name": name, "reason": "no placeable ABAP statement units"})
        return
    diagnostics["chunks_included"].append(
        {
            "name": name,
            "sections": {key: value for key, value in included_counts.items() if value},
        }
    )


def record_unplaced_units(diagnostics, chunk_name, reason, units):
    count = len(units or [])
    if not count:
        return
    diagnostics["unplaced_chunks"].append(
        {
            "name": chunk_name,
            "reason": reason,
            "statement_units": count,
        }
    )


def declaration_statement_units(source):
    units = []
    for unit in abap_statement_units(source):
        first_code = first_statement_code_line(unit)
        if re.match(r"^FORM\b", first_code, re.IGNORECASE) or is_main_event_line(first_code):
            continue
        units.append(unit)
    return units


def split_chunk(source):
    sections = {"main_event": [], "forms": [], "other": []}
    for unit in abap_statement_units(source):
        first_code = first_statement_code_line(unit)
        if re.match(r"^FORM\b", first_code, re.IGNORECASE):
            sections["forms"].append(unit)
        elif is_main_event_line(first_code):
            sections["main_event"].append(unit)
        else:
            sections["other"].append(unit)
    return sections


def dedupe_global_declarations(units):
    result = []
    indexes_by_key = {}
    scores_by_key = {}
    for unit in units or []:
        key = declaration_key(unit)
        if not key:
            result.append(unit)
            continue
        score = declaration_score(unit)
        if key in indexes_by_key:
            if score > scores_by_key[key]:
                result[indexes_by_key[key]] = unit
                scores_by_key[key] = score
            continue
        indexes_by_key[key] = len(result)
        scores_by_key[key] = score
        result.append(unit)
    return result


def declaration_key(unit):
    first_code = first_statement_code_line(unit)
    normalized = re.sub(r"\s+", " ", first_code).strip()
    patterns = [
        (r"^REPORT\s+([A-Za-z][A-Za-z0-9_]*)\b", "REPORT"),
        (r"^TABLES\s*:?\s*([A-Za-z][A-Za-z0-9_]*)\b", "TABLES"),
        (r"^TYPES\s*:?\s+BEGIN\s+OF\s+([A-Za-z][A-Za-z0-9_]*)\b", "TYPES"),
        (r"^TYPES\s*:?\s+([A-Za-z][A-Za-z0-9_]*)\s+TYPE\b", "TYPES"),
        (r"^DATA\s*:?\s+BEGIN\s+OF\s+([A-Za-z][A-Za-z0-9_]*)\b", "DATA"),
        (r"^DATA\s*:?\s+([A-Za-z][A-Za-z0-9_]*)\s+TYPE\s+STANDARD\s+TABLE\b", "DATA"),
        (r"^DATA\s*:?\s+([A-Za-z][A-Za-z0-9_]*)\s+TYPE\b", "DATA"),
        (r"^CONSTANTS\s*:?\s+([A-Za-z][A-Za-z0-9_]*)\s+TYPE\b", "CONSTANTS"),
    ]
    for pattern, kind in patterns:
        match = re.match(pattern, normalized, re.IGNORECASE)
        if match:
            return kind, match.group(1).lower()
    unit_text = "\n".join(str(line) for line in unit).strip()
    if not unit_text:
        return "BLANK", len(unit_text)
    return "STATEMENT", re.sub(r"\s+", " ", unit_text).upper()


def declaration_score(unit):
    text = "\n".join(str(line) for line in unit)
    normalized = re.sub(r"\s+", " ", text).upper()
    if "TYPE STANDARD TABLE OF" in normalized:
        return 40
    if re.search(r"\bBEGIN\s+OF\b", normalized):
        return 30 if len(unit or []) > 1 else 20
    return 10


def dedupe_form_units(units):
    result = []
    seen = set()
    for unit in units or []:
        key = form_key(unit)
        if key in seen:
            continue
        seen.add(key)
        result.append(unit)
    return result


def form_key(unit):
    first_code = first_statement_code_line(unit)
    match = re.match(r"^FORM\s+([A-Za-z][A-Za-z0-9_]*)\b", first_code, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return re.sub(r"\s+", " ", "\n".join(str(line) for line in unit)).upper()


def abap_statement_units(source):
    units = []
    current = []
    in_form = False
    for line in str(source or "").splitlines():
        stripped = line.strip()
        if not current and not stripped:
            units.append([line])
            continue
        if re.match(r"^FORM\b", stripped, re.IGNORECASE):
            in_form = True
        current.append(line)
        if in_form:
            if re.match(r"^ENDFORM\b", stripped, re.IGNORECASE):
                units.append(current)
                current = []
                in_form = False
            continue
        code = split_code_and_comment(line)[0].strip()
        if statement_ends(code):
            units.append(current)
            current = []
    if current:
        units.append(current)
    return units


def first_statement_code_line(unit):
    for line in unit or []:
        code = split_code_and_comment(line)[0].strip()
        if code:
            return code
    return ""


def is_main_event_line(stripped):
    return bool(re.match(r"^(START-OF-SELECTION|END-OF-SELECTION|INITIALIZATION|AT\s+SELECTION-SCREEN|PERFORM)\b", stripped, re.IGNORECASE))


def join_units(units):
    lines = []
    for unit in units or []:
        lines.extend(unit)
    while lines and not str(lines[0]).strip():
        lines.pop(0)
    while lines and not str(lines[-1]).strip():
        lines.pop()
    return "\n".join(str(line) for line in lines).strip()
