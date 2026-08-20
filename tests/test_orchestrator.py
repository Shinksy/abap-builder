import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from services.orchestrator import (
    CHUNK_PROMPT_PATHS,
    DECLARATION_REQUIREMENTS_PROMPT_PATH,
    DECLARATIONS_CHUNK_PROMPT_PATH,
    PROCESSING_PLAN_PROMPT_PATH,
    ProcessingContractValidationError,
    ProcessingPlanValidationError,
    assemble_abap_chunks,
    build_processing_contract,
    chunk_ddic_diagnostics,
    chunk_prompt_text,
    extract_declaration_requirements,
    extract_processing_plan,
    extract_processing_rules_section,
    generate_chunked_abap_program,
    processing_plan_response_format,
    discover_processing_rule_dependencies,
    ensure_database_read_declarations,
    ensure_form_chunk_uses_declared_globals,
    ensure_required_global_declarations,
    ensure_required_tables_declarations,
    ensure_standard_report_header,
    group_declaration_statements_by_prefix,
    normalize_declaration_requirements,
    normalize_processing_plan,
    normalize_processing_plan_with_diagnostics,
    validate_generated_processing_completeness,
    validate_processing_plan,
)
from services.create_abap import append_generation_contract


class OrchestratorTest(unittest.TestCase):
    def test_declarations_chunk_prompt_is_loaded_from_prompt_file(self):
        self.assertTrue(DECLARATIONS_CHUNK_PROMPT_PATH.exists())
        prompt_text = DECLARATIONS_CHUNK_PROMPT_PATH.read_text(encoding="utf-8")

        self.assertIn("Declarations-specific prompt:", prompt_text)
        self.assertIn("{{DDIC_METADATA}}", prompt_text)
        self.assertIn("{{DECLARATION_REQUIREMENTS}}", prompt_text)
        self.assertIn("{{CHUNK_CONTRACT}}", prompt_text)
        self.assertNotIn("{{FUNCTIONAL_SPECIFICATION}}", prompt_text)
        self.assertNotIn("SAP callable signature catalogue", prompt_text)
        self.assertNotIn("START-OF-SELECTION", prompt_text)
        self.assertNotIn("FORM names", prompt_text)

    def test_declaration_requirements_extraction_prompt_is_loaded_from_prompt_file(self):
        self.assertTrue(DECLARATION_REQUIREMENTS_PROMPT_PATH.exists())
        prompt_text = DECLARATION_REQUIREMENTS_PROMPT_PATH.read_text(encoding="utf-8")

        self.assertIn("Extract declaration requirements", prompt_text)
        self.assertIn('"report_name"', prompt_text)
        self.assertIn('"parameters"', prompt_text)
        self.assertIn('"select_options"', prompt_text)
        self.assertIn('"output_structure_fields"', prompt_text)
        self.assertIn('"type_or_like"', prompt_text)
        self.assertIn('"global_variables"', prompt_text)
        self.assertIn('"declaration"', prompt_text)
        self.assertIn('"include_when"', prompt_text)
        self.assertIn('"as_checkbox"', prompt_text)
        self.assertIn('"radiobutton_group"', prompt_text)
        self.assertIn('"default"', prompt_text)
        self.assertIn("Build output_structure_fields only from fields explicitly specified as output fields", prompt_text)
        self.assertIn("When an output field maps to an SAP table or structure field", prompt_text)
        self.assertNotIn("Generate one complete classical SAP ECC ABAP report", prompt_text)

    def test_processing_plan_extraction_prompt_is_loaded_from_prompt_file(self):
        self.assertTrue(PROCESSING_PLAN_PROMPT_PATH.exists())
        prompt_text = PROCESSING_PLAN_PROMPT_PATH.read_text(encoding="utf-8")

        self.assertIn("Extract the business-processing logic", prompt_text)
        self.assertIn("processing_steps", prompt_text)
        self.assertIn("LOOP: source, into, steps", prompt_text)
        self.assertIn("CALL_FUNCTION: name", prompt_text)
        self.assertIn("CALL_STATIC_METHOD: class, method", prompt_text)
        self.assertIn("CALL_METHOD: object, method", prompt_text)
        self.assertIn("Preserve the order of the functional specification", prompt_text)
        self.assertIn("include only fields required to locate a row in the already-populated internal table", prompt_text)
        self.assertIn("Do not put SQL WHERE filters, selection parameters, select-options, constants", prompt_text)
        self.assertNotIn('"source": "t_<table>"', prompt_text)

    def test_processing_plan_is_extracted_and_passed_to_processing_chunk(self):
        captured = []
        responses = {
            "declarations": "REPORT ztest.",
            "database_read_forms": "FORM read_edidc.\nENDFORM.",
            "processing_form": "FORM process_data.\nENDFORM.",
            "output_forms": "",
            "main_program_flow": "START-OF-SELECTION.",
        }
        plan = {
            "processing_steps": [
                {"step": 1, "operation": "LOOP", "source": "t_edidc"},
                {"step": 2, "operation": "READ", "source": "t_edids", "match": "docnum = docnum"},
                {"step": 3, "operation": "CALL_FUNCTION", "name": "BAPI_MESSAGE_GETDETAIL"},
                {"step": 4, "operation": "APPEND", "source": "w_output", "target": "t_output"},
            ]
        }

        def generator(prompt_text, source_text):
            captured.append((prompt_text, source_text))
            if "Extract declaration requirements" in prompt_text:
                return {"text": json.dumps({"report_name": "ztest"}), "model": "test-model", "usage": None}
            if "Extract business-processing logic" in prompt_text:
                return {"text": json.dumps(plan), "model": "test-model", "usage": None}
            chunk_name = next(name for name in responses if f"Chunk: {name}" in prompt_text)
            return {"text": responses[chunk_name], "model": "test-model", "usage": None}

        result = generate_chunked_abap_program(
            (
                "SAP callable signature catalogue:\n"
                "- BAPI_MESSAGE_GETDETAIL: RETURN [EXPORTING BAPIRET2]\n"
                "Shared generation contract:\n"
                "Exact FORM names: process_data\n"
                "Exact callable identities: BAPI_MESSAGE_GETDETAIL"
            ),
            "For each selected IDoc, get status, call BAPI_MESSAGE_GETDETAIL, and append output.",
            abap_generator=generator,
        )

        self.assertIn("Processing contract supplied by validated application artefacts", captured[1][0])
        self.assertIn('"BAPI_MESSAGE_GETDETAIL"', captured[1][0])
        self.assertNotIn("Available SAP callable metadata:", captured[1][0])
        expected_plan = {
            "processing_steps": [
                {
                    "step": 1,
                    "operation": "LOOP",
                    "source": "t_edidc",
                    "into": "st_edidc",
                    "steps": [
                        {
                            "step": 2,
                            "operation": "CALL_FUNCTION",
                            "name": "BAPI_MESSAGE_GETDETAIL",
                            "input_parameters": {},
                            "output_parameters": {},
                        },
                        {"step": 3, "operation": "APPEND", "source": "w_output", "target": "t_output"},
                    ],
                }
            ]
        }
        self.assertEqual(result["processing_plan"]["plan"], expected_plan)
        processing_chunk = next(chunk for chunk in result["chunks"] if chunk["name"] == "processing_form")
        self.assertEqual(processing_chunk["processing_plan"]["plan"], expected_plan)
        self.assertIn("Structured processing plan:", processing_chunk["prompt"])
        self.assertIn('"operation": "CALL_FUNCTION"', processing_chunk["prompt"])
        self.assertIn('"name": "BAPI_MESSAGE_GETDETAIL"', processing_chunk["prompt"])

    def test_processing_form_generation_splits_by_top_level_processing_step(self):
        processing_prompts = []
        plan = {
            "plan": {
                "processing_steps": [
                    {
                        "step": 1,
                        "operation": "LOOP",
                        "source": "t_edidc",
                        "into": "st_edidc",
                        "steps": [
                            {
                                "step": 2,
                                "operation": "CALL_FUNCTION",
                                "name": "BAPI_ONE",
                                "input_parameters": {"IV_DOCNUM": "st_edidc-DOCNUM"},
                            }
                        ],
                    },
                    {
                        "step": 3,
                        "operation": "LOOP",
                        "source": "t_edids",
                        "into": "st_edids",
                        "steps": [
                            {
                                "step": 4,
                                "operation": "CALL_FUNCTION",
                                "name": "BAPI_TWO",
                                "input_parameters": {"IV_STATUS": "st_edids-STATUS"},
                            }
                        ],
                    },
                ]
            }
        }
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM [TYPE EDIDC-DOCNUM], CREDAT [TYPE EDIDC-CREDAT]\n"
            "- EDIDS: STATUS [TYPE EDIDS-STATUS], LOGDAT [TYPE EDIDS-LOGDAT]\n"
            "SAP callable signature catalogue:\n"
            "- BAPI_ONE: IV_DOCNUM [IMPORTING EDIDC-DOCNUM], IX_UNUSED [IMPORTING CHAR]\n"
            "- BAPI_TWO: IV_STATUS [IMPORTING EDIDS-STATUS], IY_UNUSED [IMPORTING CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc, t_edids\n"
            "Exact work-area names: st_edidc, st_edids\n"
            "Exact FORM names: process_data\n"
            "Exact callable identities: BAPI_ONE, BAPI_TWO\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
            "- EDIDS: structure st_edids, table t_edids, work area st_edids"
        )

        def generator(prompt_text, _source_text):
            if "Chunk: processing_form" in prompt_text:
                processing_prompts.append(prompt_text)
                if "BAPI_ONE" in prompt_text:
                    return {"text": "FORM process_alpha.\nENDFORM.", "model": "test-model", "usage": None}
                return {"text": "FORM process_beta.\nENDFORM.", "model": "test-model", "usage": None}
            if "Chunk: declarations" in prompt_text:
                return {"text": "REPORT ztest.", "model": "test-model", "usage": None}
            if "Chunk: database_read_forms" in prompt_text:
                return {"text": "FORM read_data.\nENDFORM.", "model": "test-model", "usage": None}
            if "Chunk: output_forms" in prompt_text:
                return {"text": "FORM output_data.\nENDFORM.", "model": "test-model", "usage": None}
            if "Chunk: main_program_flow" in prompt_text:
                return {"text": "START-OF-SELECTION.", "model": "test-model", "usage": None}
            return {"text": "", "model": "test-model", "usage": None}

        result = generate_chunked_abap_program(
            base_prompt,
            "Read EDIDC-DOCNUM and EDIDS-STATUS. Processing is defined by the approved plan.",
            abap_generator=generator,
            declaration_requirements={"requirements": {"report_name": "ztest"}},
            approved_processing_plan=plan,
        )

        self.assertEqual(len(processing_prompts), 2)
        first_prompt, second_prompt = processing_prompts
        self.assertIn('"source": "t_edidc"', first_prompt)
        self.assertIn('"name": "BAPI_ONE"', first_prompt)
        self.assertIn("- EDIDC: DOCNUM [TYPE EDIDC-DOCNUM]", first_prompt)
        self.assertIn("- BAPI_ONE: IV_DOCNUM [IMPORTING EDIDC-DOCNUM]", first_prompt)
        self.assertNotIn("t_edids", first_prompt)
        self.assertNotIn("BAPI_TWO", first_prompt)
        self.assertNotIn("IX_UNUSED", first_prompt)
        self.assertIn('"source": "t_edids"', second_prompt)
        self.assertIn('"name": "BAPI_TWO"', second_prompt)
        self.assertIn("- EDIDS: STATUS [TYPE EDIDS-STATUS]", second_prompt)
        self.assertIn("- BAPI_TWO: IV_STATUS [IMPORTING EDIDS-STATUS]", second_prompt)
        self.assertNotIn("t_edidc", second_prompt)
        self.assertNotIn("BAPI_ONE", second_prompt)
        self.assertNotIn("IY_UNUSED", second_prompt)
        processing_chunks = [chunk for chunk in result["chunks"] if chunk["name"] == "processing_form"]
        self.assertEqual(
            [chunk["subtitle"] for chunk in processing_chunks],
            ["Step 1: Loop t_edidc with 1 nested step", "Step 2: Loop t_edids with 1 nested step"],
        )
        self.assertLess(result["text"].index("FORM process_alpha"), result["text"].index("FORM process_beta"))

    def test_static_and_instance_method_calls_flow_from_processing_plan_extraction_to_abap_generation(self):
        captured = []
        processing_plan = {
            "processing_steps": [
                {
                    "operation": "LOOP",
                    "source": "t_pa0000",
                    "into": "st_pa0000",
                    "steps": [
                        {"operation": "CLEAR", "target": "w_output"},
                        {
                            "operation": "CALL_STATIC_METHOD",
                            "class": "zcl_rule_factory",
                            "method": "create",
                            "input_parameters": {"IV_KEY": "p_rule"},
                            "returning_parameter": "lo_rule",
                        },
                        {
                            "operation": "CALL_METHOD",
                            "object": "lo_rule",
                            "method": "execute",
                            "input_parameters": {"IV_PERNR": "st_pa0000-PERNR"},
                            "output_parameters": {"EV_STATUS": "w_output-STATUS"},
                        },
                        {"operation": "MOVE", "source": "st_pa0000-PERNR", "target": "w_output-PERNR"},
                        {"operation": "APPEND", "source": "w_output", "target": "t_output"},
                    ],
                }
            ]
        }
        prompt_text = (
            "SAP DDIC metadata catalogue:\n"
            "- PA0000: PERNR [TYPE PA0000-PERNR]\n"
            "SAP callable signature catalogue:\n"
            "- ZCL_RULE_FACTORY=>CREATE: IV_KEY [IMPORTING CHAR], RO_RULE [RETURNING REF TO OBJECT]\n"
            "- ZCL_RULE=>EXECUTE: IV_PERNR [IMPORTING PA0000-PERNR], EV_STATUS [EXPORTING CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_pa0000\n"
            "Exact work-area names: st_pa0000\n"
            "Exact FORM names: process_data\n"
            "Exact callable identities: ZCL_RULE_FACTORY=>CREATE, ZCL_RULE=>EXECUTE\n"
            "- PA0000: structure st_pa0000, table t_pa0000, work area st_pa0000"
        )
        source_text = (
            "## Processing Rules\n"
            "DATA lo_rule TYPE REF TO zcl_rule.\n"
            "For each record in t_pa0000, call static method ZCL_RULE_FACTORY=>CREATE with IV_KEY from p_rule and return the rule object to lo_rule.\n"
            "Then call instance method lo_rule->execute with IV_PERNR from st_pa0000-PERNR and output EV_STATUS to STATUS.\n"
            "Append the output record.\n"
        )

        def generator(prompt, source):
            captured.append(prompt)
            if "Extract business-processing logic" in prompt:
                return {"text": json.dumps(processing_plan), "model": "test-model", "usage": None}
            if "Chunk: declarations" in prompt:
                return {"text": "REPORT ztest.\nDATA lo_rule TYPE REF TO zcl_rule.", "model": "test-model", "usage": None}
            if "Chunk: processing_form" in prompt:
                self.assertIn('"operation": "CALL_STATIC_METHOD"', prompt)
                self.assertIn('"class": "ZCL_RULE_FACTORY"', prompt)
                self.assertIn('"returning_parameter": "lo_rule"', prompt)
                self.assertIn('"operation": "CALL_METHOD"', prompt)
                self.assertIn('"object": "lo_rule"', prompt)
                self.assertIn('"name": "ZCL_RULE=>EXECUTE"', prompt)
                self.assertIn("- ZCL_RULE_FACTORY=>CREATE: IV_KEY [IMPORTING CHAR], RO_RULE [RETURNING REF TO OBJECT]", prompt)
                self.assertIn("- ZCL_RULE=>EXECUTE: IV_PERNR [IMPORTING PA0000-PERNR], EV_STATUS [EXPORTING CHAR]", prompt)
                return {
                    "text": (
                        "FORM process_data.\n"
                        "  CALL METHOD zcl_rule_factory=>create\n"
                        "    EXPORTING iv_key = p_rule\n"
                        "    RECEIVING ro_rule = lo_rule.\n"
                        "  CALL METHOD lo_rule->execute\n"
                        "    EXPORTING iv_pernr = st_pa0000-pernr\n"
                        "    IMPORTING ev_status = w_output-status.\n"
                        "ENDFORM."
                    ),
                    "model": "test-model",
                    "usage": None,
                }
            if "Chunk: output_forms" in prompt:
                return {"text": "FORM output_data.\nENDFORM.", "model": "test-model", "usage": None}
            if "Chunk: main_program_flow" in prompt:
                return {"text": "START-OF-SELECTION.\n  PERFORM process_data.", "model": "test-model", "usage": None}
            return {"text": "", "model": "test-model", "usage": None}

        result = generate_chunked_abap_program(
            prompt_text,
            source_text,
            abap_generator=generator,
            declaration_requirements={
                "requirements": {
                    "report_name": "ztest",
                    "parameters": [{"name": "p_rule"}],
                    "global_variables": [
                        {"name": "lo_rule", "declaration": "DATA lo_rule TYPE REF TO zcl_rule."}
                    ],
                    "output_structure_fields": [
                        {"name": "PERNR", "type_or_like": "TYPE PA0000-PERNR"},
                        {"name": "STATUS", "type_or_like": "TYPE c LENGTH 1"},
                    ],
                }
            },
        )

        normalized_steps = result["processing_plan"]["plan"]["processing_steps"][0]["steps"]
        self.assertEqual("CALL_STATIC_METHOD", normalized_steps[1]["operation"])
        self.assertEqual("ZCL_RULE_FACTORY=>CREATE", normalized_steps[1]["name"])
        self.assertEqual("lo_rule", normalized_steps[1]["returning_parameter"])
        self.assertEqual("CALL_METHOD", normalized_steps[2]["operation"])
        self.assertEqual("ZCL_RULE=>EXECUTE", normalized_steps[2]["name"])
        self.assertEqual("lo_rule", normalized_steps[2]["object"])
        self.assertIn("CALL METHOD zcl_rule_factory=>create", result["text"])
        self.assertIn("CALL METHOD lo_rule->execute", result["text"])
        self.assertTrue(any("Processing contract supplied by validated application artefacts" in prompt for prompt in captured))

    def test_normalizes_output_structure_fields_to_typed_objects_from_ddic(self):
        requirements = {
            "output_structure_fields": [
                "VBELN",
                {"name": "AUDAT", "type_or_like": "TYPE VBAK-AUDAT", "include_when": "only for header output"},
                {"name": "TEXT", "type_or_like": "TYPE c LENGTH 40"},
            ]
        }

        normalized = normalize_declaration_requirements(
            requirements,
            ddic_catalogue="- VBAK: VBELN, AUDAT\n- VBAP: POSNR",
        )

        self.assertEqual(
            normalized["output_structure_fields"],
            [
                {"name": "VBELN", "type_or_like": "TYPE VBAK-VBELN"},
                {
                    "name": "AUDAT",
                    "type_or_like": "TYPE VBAK-AUDAT",
                    "include_when": "only for header output",
                },
                {"name": "TEXT", "type_or_like": "TYPE c LENGTH 40"},
            ],
        )

    def test_normalizes_radiobutton_group_to_abap_four_character_limit(self):
        normalized = normalize_declaration_requirements(
            {
                "parameters": [
                    {
                        "name": "p_alv",
                        "type_or_like": "",
                        "as_checkbox": False,
                        "radiobutton_group": "r_mode",
                        "default": "X",
                    },
                    {
                        "name": "p_file",
                        "type_or_like": "",
                        "as_checkbox": False,
                        "radiobutton_group": "r_mode",
                        "default": "",
                    },
                    {
                        "name": "p_other",
                        "type_or_like": "",
                        "as_checkbox": False,
                        "radiobutton_group": "rad1",
                        "default": "",
                    },
                ]
            }
        )

        groups = [parameter["radiobutton_group"] for parameter in normalized["parameters"]]
        self.assertEqual(groups, ["r001", "r001", "rad1"])
        self.assertTrue(all(len(group) <= 4 for group in groups if group))

    def test_leaves_output_structure_field_type_empty_when_ddic_is_ambiguous_or_unverified(self):
        requirements = {
            "output_structure_fields": [
                "DOCNUM",
                {"name": "BAD", "type_or_like": "TYPE ZMADEUP-BAD"},
                {"name": "LEGACY", "source_field": "EDIDC-MESTYP"},
            ]
        }

        normalized = normalize_declaration_requirements(
            requirements,
            ddic_catalogue="- EDIDC: DOCNUM, MESTYP\n- EDID4: DOCNUM",
        )

        self.assertEqual(
            normalized["output_structure_fields"],
            [
                {"name": "DOCNUM", "type_or_like": ""},
                {"name": "BAD", "type_or_like": ""},
                {"name": "LEGACY", "type_or_like": "TYPE EDIDC-MESTYP"},
            ],
        )

    def test_populates_and_enforces_required_tables_declarations(self):
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM, CREDAT\n"
            "- EDIDS: DOCNUM, STATUS\n"
            "- ZIGNORED: VALUE\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc, t_edids\n"
            "Exact work-area names: st_edidc, st_edids\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
            "- EDIDS: structure st_edids, table t_edids, work area st_edids"
        )
        responses = {
            "declarations": "\n".join(
                [
                    "REPORT ztest.",
                    "SELECT-OPTIONS s_docnum FOR edidc-docnum.",
                    "SELECT-OPTIONS s_status FOR edids-status.",
                ]
            ),
            "database_read_forms": "FORM read_edidc.\nENDFORM.",
            "processing_form": "FORM process_data.\nENDFORM.",
            "output_forms": "",
            "main_program_flow": "START-OF-SELECTION.",
        }

        def generator(prompt_text, _source_text):
            if "Extract declaration requirements" in prompt_text:
                return {
                    "text": json.dumps(
                        {
                            "report_name": "ztest",
                            "parameters": [],
                            "select_options": [
                                {"name": "s_docnum", "for_field": "EDIDC-DOCNUM"},
                                {"name": "s_status", "for_field": "EDIDS-STATUS"},
                            ],
                            "tables_declarations": [],
                            "output_structure_fields": [],
                        }
                    ),
                    "model": "test-model",
                    "usage": None,
                }
            if "Extract business-processing logic" in prompt_text:
                return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}
            chunk_name = next(name for name in responses if f"Chunk: {name}" in prompt_text)
            return {"text": responses[chunk_name], "model": "test-model", "usage": None}

        result = generate_chunked_abap_program(
            base_prompt,
            "Select EDIDC-DOCNUM and EDIDS-STATUS.",
            abap_generator=generator,
        )

        self.assertEqual(
            result["declaration_requirements"]["requirements"]["tables_declarations"],
            ["EDIDC", "EDIDS"],
        )
        declaration_chunk = result["chunks"][0]
        self.assertIn("Required TABLES declarations: EDIDC, EDIDS", declaration_chunk["prompt"])
        self.assertIn("TABLES edidc.", declaration_chunk["text"])
        self.assertIn("TABLES edids.", declaration_chunk["text"])
        self.assertIn("TABLES edidc.", result["text"])
        self.assertLess(result["text"].index("TABLES edidc."), result["text"].index("SELECT-OPTIONS"))

    def test_ensure_required_tables_declarations_only_adds_missing_tables(self):
        fixed = ensure_required_tables_declarations(
            "\n".join(
                [
                    "REPORT ztest.",
                    "TABLES: edidc.",
                    "SELECT-OPTIONS s_docnum FOR edidc-docnum.",
                    "SELECT-OPTIONS s_status FOR edids-status.",
                ]
            ),
            json.dumps({"tables_declarations": ["EDIDC", "EDIDS", "EDIDC"]}),
        )

        self.assertEqual(fixed.count("TABLES edidc."), 0)
        self.assertIn("TABLES: edidc.", fixed)
        self.assertIn("TABLES edids.", fixed)
        self.assertLess(fixed.index("TABLES edids."), fixed.index("SELECT-OPTIONS"))

    def test_ensure_required_tables_declarations_adds_block_when_llm_returns_none(self):
        fixed = ensure_required_tables_declarations(
            "\n".join(
                [
                    "REPORT ztest.",
                    "TYPES ty_edidc TYPE edidc.",
                    "DATA st_edidc TYPE ty_edidc.",
                    "SELECT-OPTIONS s_docnum FOR edidc-docnum.",
                ]
            ),
            json.dumps({"tables_declarations": ["EDIDC", "EDIDS"]}),
        )

        self.assertIn(
            "\n".join(
                [
                    "REPORT ztest.",
                    "TABLES edidc.",
                    "TABLES edids.",
                    "TYPES ty_edidc TYPE edidc.",
                ]
            ),
            fixed,
        )
        self.assertLess(fixed.index("TABLES edidc."), fixed.index("TABLES edids."))
        self.assertLess(fixed.index("TABLES edids."), fixed.index("TYPES"))

    def test_ensure_required_tables_declarations_preserves_partial_existing_entry(self):
        fixed = ensure_required_tables_declarations(
            "\n".join(
                [
                    "REPORT ztest.",
                    "TABLES: edids.",
                    "DATA st_edids TYPE edids.",
                ]
            ),
            json.dumps({"tables_declarations": ["EDIDC", "EDIDS"]}),
        )

        self.assertIn("TABLES edidc.", fixed)
        self.assertIn("TABLES: edids.", fixed)
        self.assertEqual(fixed.count("TABLES: edids."), 1)
        self.assertLess(fixed.index("TABLES edidc."), fixed.index("TABLES: edids."))
        self.assertLess(fixed.index("TABLES: edids."), fixed.index("DATA"))

    def test_ensure_required_tables_declarations_removes_duplicate_required_entries(self):
        fixed = ensure_required_tables_declarations(
            "\n".join(
                [
                    "REPORT ztest.",
                    "TABLES: edidc, edidc.",
                    "TABLES edids.",
                    "PARAMETERS p_doc TYPE edidc-docnum.",
                ]
            ),
            json.dumps({"tables_declarations": ["EDIDC", "EDIDS", "EDIDC"]}),
        )

        self.assertEqual(fixed.count("edidc"), 2)
        self.assertEqual(fixed.count("TABLES edidc."), 1)
        self.assertEqual(fixed.count("TABLES edids."), 1)
        self.assertNotIn("TABLES: edidc, edidc.", fixed)
        self.assertLess(fixed.index("TABLES edidc."), fixed.index("TABLES edids."))
        self.assertLess(fixed.index("TABLES edids."), fixed.index("PARAMETERS"))

    def test_ensure_required_tables_declarations_leaves_complete_entries_unchanged(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "TABLES: edidc,",
                "        edids.",
                "SELECT-OPTIONS s_docnum FOR edidc-docnum.",
            ]
        )

        fixed = ensure_required_tables_declarations(
            source,
            json.dumps({"tables_declarations": ["EDIDC", "EDIDS"]}),
        )

        self.assertEqual(fixed, source)

    def test_form_globals_are_added_to_requirements_and_form_chunks_are_allow_listed(self):
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc\n"
            "Exact work-area names: st_edidc\n"
            "Exact output structure fields: DOCNUM\n"
            "Exact FORM names: read_edidc, process_data, output_data, display_alv, write_csv\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc"
        )
        responses = {
            "declarations": "\n".join(
                [
                    "REPORT ztest.",
                    "TYPES: BEGIN OF ty_output,",
                    "         docnum TYPE edidc-docnum,",
                    "       END OF ty_output.",
                    "TYPES ty_edidc TYPE edidc.",
                ]
            ),
            "database_read_forms": "FORM read_edidc.\n  SELECT docnum INTO TABLE t_edidc FROM edidc.\nENDFORM.",
            "processing_form": "FORM process_data.\n  LOOP AT t_edidc INTO st_edidc.\n  ENDLOOP.\nENDFORM.",
            "output_forms": "FORM write_csv.\n  w_filename = 'out.csv'.\nENDFORM.",
            "main_program_flow": "START-OF-SELECTION.",
        }

        def generator(prompt_text, _source_text):
            if "Extract declaration requirements" in prompt_text:
                return {
                    "text": json.dumps(
                        {
                            "report_name": "ztest",
                            "parameters": [],
                            "select_options": [],
                            "tables_declarations": [],
                            "global_variables": [],
                            "output_structure_fields": [
                                {"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"}
                            ],
                        }
                    ),
                    "model": "test-model",
                    "usage": None,
                }
            if "Extract business-processing logic" in prompt_text:
                return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}
            chunk_name = next(name for name in responses if f"Chunk: {name}" in prompt_text)
            return {"text": responses[chunk_name], "model": "test-model", "usage": None}

        result = generate_chunked_abap_program(
            base_prompt,
            "Read EDIDC-DOCNUM, display it in ALV, and export it to CSV.",
            abap_generator=generator,
        )

        globals_by_name = {
            item["name"]: item["declaration"]
            for item in result["declaration_requirements"]["requirements"]["global_variables"]
        }
        self.assertEqual(globals_by_name["t_edidc"], "DATA t_edidc TYPE STANDARD TABLE OF ty_edidc.")
        self.assertEqual(globals_by_name["st_edidc"], "DATA st_edidc TYPE ty_edidc.")
        self.assertEqual(globals_by_name["t_output"], "DATA t_output TYPE STANDARD TABLE OF ty_output.")
        self.assertEqual(globals_by_name["w_output"], "DATA w_output TYPE ty_output.")
        self.assertEqual(globals_by_name["t_fieldcat"], "DATA t_fieldcat TYPE slis_t_fieldcat_alv.")
        self.assertEqual(globals_by_name["w_fieldcat"], "DATA w_fieldcat TYPE slis_fieldcat_alv.")
        self.assertEqual(globals_by_name["w_filename"], "DATA w_filename TYPE string.")
        self.assertEqual(globals_by_name["w_csv_line"], "DATA w_csv_line TYPE string.")

        prompts = {chunk["name"]: chunk["prompt"] for chunk in result["chunks"]}
        for chunk_name in ("database_read_forms", "output_forms"):
            self.assertIn(
                "Allowed global variables for FORM chunks: "
                "t_edidc, st_edidc, t_output, w_output, t_fieldcat, w_fieldcat, w_filename, w_csv_line",
                prompts[chunk_name],
            )
            self.assertIn(
                "Do not reference any global variable that is not listed in the allowed global variables for FORM chunks.",
                prompts[chunk_name],
            )
        self.assertNotIn("Allowed global variables for FORM chunks:", prompts["processing_form"])

        declaration_text = result["chunks"][0]["text"]
        self.assertIn("DATA t_edidc TYPE STANDARD TABLE OF ty_edidc.", declaration_text)
        self.assertIn("DATA t_output TYPE STANDARD TABLE OF ty_output.", declaration_text)
        self.assertIn("DATA w_filename TYPE string.", declaration_text)
        self.assertIn("DATA w_csv_line TYPE string.", declaration_text)
        self.assertLess(declaration_text.index("DATA t_edidc"), result["text"].index("FORM read_edidc."))

    def test_ensure_required_global_declarations_only_adds_missing_globals(self):
        fixed = ensure_required_global_declarations(
            "\n".join(
                [
                    "REPORT ztest.",
                    "DATA t_edidc TYPE STANDARD TABLE OF st_edidc.",
                    "SELECT-OPTIONS s_docnum FOR edidc-docnum.",
                ]
            ),
            json.dumps(
                {
                    "global_variables": [
                        {
                            "name": "t_edidc",
                            "declaration": "DATA t_edidc TYPE STANDARD TABLE OF st_edidc.",
                        },
                        {
                            "name": "w_filename",
                            "declaration": "DATA w_filename TYPE string.",
                        },
                    ]
                }
            ),
        )

        self.assertEqual(fixed.count("DATA t_edidc TYPE STANDARD TABLE OF st_edidc."), 1)
        self.assertIn("DATA w_filename TYPE string.", fixed)
        self.assertLess(fixed.index("DATA w_filename"), fixed.index("SELECT-OPTIONS"))

    def test_form_chunk_rejects_undeclared_global_style_variables(self):
        declaration_requirements = json.dumps(
            {
                "global_variables": [
                    {
                        "name": "t_output",
                        "declaration": "DATA t_output TYPE STANDARD TABLE OF ty_output.",
                    }
                ],
                "output_structure_fields": [
                    {"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"}
                ],
            }
        )

        with self.assertRaisesRegex(ValueError, "gt_output"):
            ensure_form_chunk_uses_declared_globals(
                "FORM output_data.\n  APPEND w_output TO gt_output.\nENDFORM.",
                "output_forms",
                "Shared generation contract:\nExact FORM names: output_data",
                declaration_requirements=declaration_requirements,
            )

    def test_form_chunk_allows_call_function_parameter_names_that_look_global(self):
        ensure_form_chunk_uses_declared_globals(
            "\n".join(
                [
                    "FORM display_alv.",
                    "  CALL FUNCTION 'REUSE_ALV_GRID_DISPLAY'",
                    "    TABLES",
                    "      it_outtab = t_output",
                    "      it_fieldcat = t_fieldcat.",
                    "ENDFORM.",
                ]
            ),
            "output_forms",
            "Shared generation contract:\nExact FORM names: display_alv",
            declaration_requirements=json.dumps(
                {
                    "global_variables": [
                        {
                            "name": "t_output",
                            "declaration": "DATA t_output TYPE STANDARD TABLE OF ty_output.",
                        },
                        {
                            "name": "t_fieldcat",
                            "declaration": "DATA t_fieldcat TYPE slis_t_fieldcat_alv.",
                        },
                    ]
                }
            ),
        )

    def test_normalizes_output_structure_fields_from_verified_callable_parameter_metadata(self):
        requirements = {
            "output_structure_fields": [
                {
                    "name": "ERROR_MESSAGE",
                    "source": "Return ERROR_MESSAGE from Z_MSG_HELPER parameter MESSAGE",
                },
                {
                    "name": "RETURN_TEXT",
                    "callable": "ZCL_MSG=>GET_TEXT",
                    "parameter": "RV_TEXT",
                },
            ]
        }
        callable_metadata = {
            "callable_signatures": {
                "Z_MSG_HELPER": {
                    "parameters": {
                        "MESSAGE": {
                            "direction": "IMPORTING",
                            "abap_type": "BAPIRET2",
                            "field": "MESSAGE",
                        }
                    }
                },
                "ZCL_MSG=>GET_TEXT": {
                    "parameters": {},
                    "returning": {
                        "name": "RV_TEXT",
                        "direction": "RETURNING",
                        "abap_type": "STRING",
                    },
                },
            }
        }

        normalized = normalize_declaration_requirements(
            requirements,
            callable_metadata=callable_metadata,
        )

        self.assertEqual(
            normalized["output_structure_fields"],
            [
                {"name": "ERROR_MESSAGE", "type_or_like": "TYPE BAPIRET2-MESSAGE"},
                {"name": "RETURN_TEXT", "type_or_like": "TYPE STRING"},
            ],
        )

    def test_extract_declaration_requirements_reports_callable_metadata_and_output_field_diagnostics(self):
        callable_metadata = {
            "callable_signatures": {
                "Z_MSG_HELPER": {
                    "parameters": {
                        "MESSAGE": {
                            "direction": "IMPORTING",
                            "abap_type": "BAPIRET2",
                            "field": "MESSAGE",
                        }
                    }
                }
            }
        }
        captured = {}

        def generator(prompt_text, _source_text):
            captured["prompt"] = prompt_text
            return {
                "text": json.dumps(
                    {
                        "report_name": "ztest",
                        "parameters": [],
                        "select_options": [],
                        "output_structure_fields": [
                            {
                                "name": "ERROR_MESSAGE",
                                "source": "Z_MSG_HELPER MESSAGE",
                            }
                        ],
                    }
                )
            }

        result = extract_declaration_requirements(
            "Output error message from Z_MSG_HELPER MESSAGE.",
            generator,
            callable_metadata=callable_metadata,
        )

        self.assertIn("Available SAP callable metadata:", captured["prompt"])
        self.assertIn("Z_MSG_HELPER", captured["prompt"])
        self.assertEqual(
            result["requirements"]["output_structure_fields"],
            [{"name": "ERROR_MESSAGE", "type_or_like": "TYPE BAPIRET2-MESSAGE"}],
        )
        self.assertEqual(
            result["diagnostics"]["callable_metadata_passed_to_requirement_extraction"],
            callable_metadata["callable_signatures"],
        )
        self.assertEqual(
            result["diagnostics"]["final_output_structure_fields"],
            [{"name": "ERROR_MESSAGE", "type_or_like": "TYPE BAPIRET2-MESSAGE"}],
        )
        self.assertEqual(
            result["diagnostics"]["output_field_source_mappings"][0]["source_kind"],
            "callable_parameter",
        )

    def test_all_chunk_prompts_are_loaded_from_prompt_files(self):
        self.assertEqual(
            set(CHUNK_PROMPT_PATHS),
            {"declarations", "database_read_forms", "processing_form", "output_forms", "main_program_flow"},
        )
        placeholders = {
            "declarations": "{{DECLARATION_REQUIREMENTS}}",
            "database_read_forms": "{{DATABASE_READ_REQUIREMENTS}}",
            "processing_form": "{{PROCESSING_REQUIREMENTS}}",
            "output_forms": "{{OUTPUT_REQUIREMENTS}}",
            "main_program_flow": "{{MAIN_FLOW_REQUIREMENTS}}",
        }
        for chunk_name, path in CHUNK_PROMPT_PATHS.items():
            self.assertTrue(path.exists(), chunk_name)
            prompt_text = path.read_text(encoding="utf-8")
            self.assertIn(placeholders[chunk_name], prompt_text)
            self.assertNotIn("{{FUNCTIONAL_SPECIFICATION}}", prompt_text)

    def test_generates_five_chunks_and_assembles_program_order(self):
        responses = {
            "declarations": (
                "REPORT ztest.\n"
                "DATA w_edidc TYPE edidc.\n"
                "SELECT-OPTIONS s_docnum FOR w_edidc-docnum.\n"
                "START-OF-SELECTION.\n"
                "FORM bad_declaration_form.\n"
                "ENDFORM."
            ),
            "database_read_forms": "FORM read_data.\nENDFORM.",
            "processing_form": "FORM process_data.\nENDFORM.",
            "output_forms": "FORM display_data.\nENDFORM.",
            "main_program_flow": "START-OF-SELECTION.\n  PERFORM read_data.\n  PERFORM process_data.\n  PERFORM display_data.",
        }
        calls = []
        progress_updates = []

        def generator(prompt_text, source_text):
            calls.append((prompt_text, source_text))
            if "Extract declaration requirements" in prompt_text:
                return {
                    "text": json.dumps({"report_name": "ztest", "parameters": [], "select_options": []}),
                    "model": "test-model",
                    "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                }
            if "Extract business-processing logic" in prompt_text:
                return {
                    "text": json.dumps({"processing_steps": []}),
                    "model": "test-model",
                    "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                }
            chunk_name = next(name for name in responses if f"Chunk: {name}" in prompt_text)
            return {
                "text": responses[chunk_name],
                "model": "test-model",
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            }

        with patch(
            "services.orchestrator.perf_counter",
            side_effect=[-2.0, -1.9, -1.0, -0.8, 0.0, 0.2, 1.0, 1.3, 2.0, 2.4, 3.0, 3.5, 4.0, 4.6],
        ):
            result = generate_chunked_abap_program(
                "Base prompt with metadata.",
                "Functional spec.",
                abap_generator=generator,
                progress_callback=lambda index, total, chunk: progress_updates.append((index, total, chunk["name"])),
            )

        self.assertEqual([chunk["name"] for chunk in result["chunks"]], ["declarations", "database_read_forms", "processing_form", "output_forms", "main_program_flow"])
        self.assertEqual(
            progress_updates,
            [
                (1, 5, "declarations"),
                (2, 5, "database_read_forms"),
                (3, 5, "processing_form"),
                (4, 5, "output_forms"),
                (5, 5, "main_program_flow"),
            ],
        )
        self.assertEqual([round(chunk["duration_seconds"], 1) for chunk in result["chunks"]], [0.2, 0.3, 0.4, 0.5, 0.6])
        self.assertTrue(all("filtered_ddic_metadata" in chunk for chunk in result["chunks"]))
        self.assertEqual(len(calls), 7)
        self.assertIn("Extract declaration requirements", calls[0][0])
        self.assertEqual(calls[0][1], "Functional spec.")
        self.assertIn("Extract business-processing logic", calls[1][0])
        self.assertEqual(calls[1][1], "Functional spec.")
        self.assertTrue(all(call[1] != "Functional spec." for call in calls[2:]))
        self.assertEqual(result["model"], "test-model")
        self.assertEqual(result["usage"], {"input_tokens": 7, "output_tokens": 14, "total_tokens": 21})
        self.assertAlmostEqual(result["declaration_requirements"]["duration_seconds"], 0.1)
        self.assertAlmostEqual(result["processing_plan"]["duration_seconds"], 0.2)
        self.assertEqual(result["declaration_requirements"]["requirements"]["report_name"], "ztest")
        self.assertEqual(result["processing_plan"]["plan"], {"processing_steps": []})
        self.assertEqual(result["chunks"][0]["declaration_requirements"]["requirements"]["report_name"], "ztest")
        self.assertIn("declaration_naming_contract", result["chunks"][0])
        self.assertEqual(
            result["text"],
            "\n".join(
                [
                    "REPORT ztest.\nDATA w_edidc TYPE edidc.",
                    "SELECT-OPTIONS s_docnum FOR w_edidc-docnum.",
                    "START-OF-SELECTION.\n  PERFORM read_data.\n  PERFORM process_data.\n  PERFORM display_data.",
                    "FORM read_data.\nENDFORM.\nFORM process_data.\nENDFORM.\nFORM display_data.\nENDFORM.",
                ]
            ),
        )

    def test_processing_plan_output_record_creation_skips_output_data_form(self):
        calls = []
        declaration_requirements = {
            "requirements": {
                "report_name": "ztest",
                "output_structure_fields": [
                    {"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"},
                ],
            }
        }
        approved_processing_plan = {
            "plan": {
                "processing_steps": [
                    {"operation": "CLEAR", "target": "w_output"},
                    {"operation": "MOVE", "source": "st_edidc-DOCNUM", "target": "w_output-DOCNUM"},
                    {"operation": "APPEND", "source": "w_output", "target": "t_output"},
                ]
            }
        }
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM [NUMC]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc\n"
            "Exact work-area names: st_edidc\n"
            "Exact output structure fields: DOCNUM\n"
            "Exact FORM names: read_edidc, process_data, output_data\n"
            "Exact callable identities: none\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc"
        )

        def generator(prompt_text, source_text):
            calls.append((prompt_text, source_text))
            self.assertNotIn("Chunk: output_forms", prompt_text)
            if "Chunk: declarations" in prompt_text:
                return {"text": "REPORT ztest.", "model": "test-model", "usage": None}
            if "Chunk: database_read_forms" in prompt_text:
                return {"text": "FORM read_edidc.\nENDFORM.", "model": "test-model", "usage": None}
            if "Chunk: processing_form" in prompt_text:
                return {
                    "text": (
                        "FORM process_data.\n"
                        "  CLEAR w_output.\n"
                        "  MOVE st_edidc-docnum TO w_output-docnum.\n"
                        "  APPEND w_output TO t_output.\n"
                        "ENDFORM."
                    ),
                    "model": "test-model",
                    "usage": None,
                }
            if "Chunk: main_program_flow" in prompt_text:
                self.assertNotIn("output_data", prompt_text)
                return {
                    "text": "START-OF-SELECTION.\n  PERFORM read_edidc.\n  PERFORM process_data.",
                    "model": "test-model",
                    "usage": None,
                }
            self.fail(f"Unexpected chunk prompt: {prompt_text}")

        result = generate_chunked_abap_program(
            base_prompt,
            "Processing plan builds and appends the output record.",
            abap_generator=generator,
            declaration_requirements=declaration_requirements,
            approved_processing_plan=approved_processing_plan,
        )

        self.assertEqual(
            [chunk["name"] for chunk in result["chunks"]],
            [
                "declarations",
                "database_read_forms",
                "processing_form",
                "processing_form",
                "processing_form",
                "main_program_flow",
            ],
        )
        self.assertNotIn("FORM output_data", result["text"])
        self.assertNotIn("PERFORM output_data", result["text"])
        self.assertIn("APPEND w_output TO t_output", result["text"])
        self.assertTrue(all("Chunk: output_forms" not in prompt for prompt, _source in calls))

    def test_assembly_preserves_chained_parameters_and_select_options_as_statements(self):
        declaration_chunk = "\n".join(
            [
                "REPORT ztest.",
                "",
                "PARAMETERS: p_zmdid TYPE zmdid,",
                "            p_idoc AS CHECKBOX,",
                "            p_alv RADIOBUTTON GROUP rad1 DEFAULT 'X',",
                "            p_file RADIOBUTTON GROUP rad1.",
                "",
                "SELECT-OPTIONS: s_id01 FOR zmd_mpe0001-identifier,",
                "                s_id06 FOR zmd_mpe0006-identifier,",
                "                s_credat FOR edidc-credat,",
                "                s_mestyp FOR edidc-mestyp,",
                "                s_status FOR edidc-status.",
            ]
        )

        assembled = assemble_abap_chunks(
            [
                {"name": "declarations", "text": declaration_chunk},
                {"name": "main_program_flow", "text": "START-OF-SELECTION."},
            ]
        )

        self.assertIn(
            "\n".join(
                [
                    "PARAMETERS: p_zmdid TYPE zmdid,",
                    "            p_idoc AS CHECKBOX,",
                    "            p_alv RADIOBUTTON GROUP rad1 DEFAULT 'X',",
                    "            p_file RADIOBUTTON GROUP rad1.",
                ]
            ),
            assembled,
        )
        self.assertIn(
            "\n".join(
                [
                    "SELECT-OPTIONS: s_id01 FOR zmd_mpe0001-identifier,",
                    "                s_id06 FOR zmd_mpe0006-identifier,",
                    "                s_credat FOR edidc-credat,",
                    "                s_mestyp FOR edidc-mestyp,",
                    "                s_status FOR edidc-status.",
                ]
            ),
            assembled,
        )
        self.assertLess(assembled.index("PARAMETERS:"), assembled.index("SELECT-OPTIONS:"))
        self.assertLess(assembled.index("SELECT-OPTIONS:"), assembled.index("START-OF-SELECTION."))

    def test_app_assembly_deduplicates_global_table_declarations(self):
        assembled = assemble_abap_chunks(
            [
                {
                    "name": "declarations",
                    "text": "\n".join(
                        [
                            "REPORT ztest.",
                            "DATA: BEGIN OF t_output OCCURS 0, pernr TYPE pa0000-pernr, END OF t_output.",
                            "TYPES: BEGIN OF ty_output,",
                            "         pernr TYPE pa0000-pernr,",
                            "       END OF ty_output.",
                            "DATA: BEGIN OF t_output OCCURS 0,",
                            "        pernr TYPE pa0000-pernr,",
                            "      END OF t_output.",
                        ]
                    ),
                },
                {"name": "main_program_flow", "text": "START-OF-SELECTION."},
            ],
            final_assembly_mode="app",
        )

        self.assertEqual(1, assembled.lower().count("begin of t_output"))
        self.assertIn("START-OF-SELECTION.", assembled)

    def test_llm_final_assembly_mode_uses_final_model_call(self):
        prompts = []

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            if "Chunk: declarations" in prompt_text:
                return {"text": "REPORT zchunk.", "model": "chunk-model", "usage": {"input_tokens": 1}}
            if "Chunk: database_read_forms" in prompt_text:
                return {"text": "FORM read_data.\nENDFORM.", "model": "chunk-model", "usage": {"input_tokens": 1}}
            if "Chunk: processing_form" in prompt_text:
                return {"text": "FORM process_data.\nENDFORM.", "model": "chunk-model", "usage": {"input_tokens": 1}}
            if "Chunk: main_program_flow" in prompt_text:
                return {"text": "START-OF-SELECTION.", "model": "chunk-model", "usage": {"input_tokens": 1}}
            if "Assemble ABAP generation chunks" in prompt_text:
                self.assertIn("===== declarations =====", source_text)
                self.assertIn("===== main_program_flow =====", source_text)
                return {"text": "REPORT zfinal.", "model": "assembly-model", "usage": {"input_tokens": 2}}
            return {"text": "", "model": "chunk-model", "usage": None}

        result = generate_chunked_abap_program(
            "Shared generation contract:\nExact FORM names: read_data, process_data",
            "Create a report.",
            abap_generator=generator,
            declaration_requirements={"requirements": {"report_name": "ztest"}},
            approved_processing_plan={"plan": {"processing_steps": []}},
            final_assembly_mode="llm",
        )

        self.assertEqual("REPORT zfinal.", result["text"])
        self.assertEqual("llm", result["final_assembly_mode"])
        self.assertEqual("assembly-model", result["model"])
        self.assertTrue(any("Assemble ABAP generation chunks" in prompt for prompt in prompts))

    def test_assembly_preserves_selection_screen_example_exactly(self):
        declaration_chunk = "\n".join(
            [
                "PARAMETERS: p_zmdid TYPE zmdid,",
                "            p_idoc AS CHECKBOX,",
                "            p_alv RADIOBUTTON GROUP rad1 DEFAULT 'X',",
                "            p_file RADIOBUTTON GROUP rad1.",
                "SELECT-OPTIONS: s_id01 FOR zmd_mpe0001-identifier,",
                "                s_id06 FOR zmd_mpe0006-identifier,",
                "                s_credat FOR edidc-credat,",
                "                s_mestyp FOR edidc-mestyp,",
                "                s_status FOR edidc-status.",
            ]
        )

        assembled = assemble_abap_chunks(
            [
                {"name": "declarations", "text": declaration_chunk},
                {"name": "main_program_flow", "text": "START-OF-SELECTION."},
            ]
        )

        self.assertTrue(assembled.startswith(declaration_chunk))
        self.assertIn(declaration_chunk + "\nSTART-OF-SELECTION.", assembled)

    def test_assembly_preserves_chained_parameters_exactly(self):
        self.assert_declaration_chunk_preserved(
            "\n".join(
                [
                    "PARAMETERS: p_first TYPE c LENGTH 1,",
                    "            p_second AS CHECKBOX.",
                ]
            )
        )

    def test_assembly_preserves_chained_select_options_exactly(self):
        self.assert_declaration_chunk_preserved(
            "\n".join(
                [
                    "SELECT-OPTIONS: s_doc FOR edidc-docnum,",
                    "                s_date FOR edidc-credat.",
                ]
            )
        )

    def test_assembly_preserves_chained_data_exactly(self):
        self.assert_declaration_chunk_preserved(
            "\n".join(
                [
                    "DATA: count TYPE i,",
                    "      message TYPE c LENGTH 40.",
                ]
            )
        )

    def test_assembly_preserves_chained_types_exactly(self):
        self.assert_declaration_chunk_preserved(
            "\n".join(
                [
                    "TYPES: ty_count TYPE i,",
                    "       ty_message TYPE c LENGTH 40.",
                ]
            )
        )

    def test_assembly_preserves_chained_constants_exactly(self):
        self.assert_declaration_chunk_preserved(
            "\n".join(
                [
                    "CONSTANTS: c_active TYPE c VALUE 'X',",
                    "           c_inactive TYPE c VALUE space.",
                ]
            )
        )

    def test_assembly_preserves_chained_tables_exactly(self):
        self.assert_declaration_chunk_preserved(
            "\n".join(
                [
                    "TABLES: edidc,",
                    "        edids.",
                ]
            )
        )

    def assert_declaration_chunk_preserved(self, declaration_chunk):
        assembled = assemble_abap_chunks(
            [
                {"name": "declarations", "text": declaration_chunk},
                {"name": "main_program_flow", "text": "START-OF-SELECTION."},
            ]
        )

        self.assertTrue(assembled.startswith(declaration_chunk))
        self.assertIn(declaration_chunk + "\nSTART-OF-SELECTION.", assembled)

    def test_declarations_chunk_prompt_uses_only_declaration_template_context(self):
        declaration_requirements = json.dumps(
            {
                "report_name": "ztest",
                "parameters": [
                    {
                        "name": "p_mestyp",
                        "type_or_like": "LIKE EDIDC-MESTYP",
                        "as_checkbox": False,
                        "radiobutton_group": "",
                        "default": "'ORDERS'",
                    },
                    {
                        "name": "p_show",
                        "type_or_like": "TYPE c",
                        "as_checkbox": True,
                        "radiobutton_group": "",
                        "default": "'X'",
                    },
                ],
                "select_options": [
                    {
                        "name": "s_docnum",
                        "for_field": "w_edidc-docnum",
                        "type_or_like": "",
                        "default": "",
                    }
                ],
                "tables_declarations": ["EDIDC"],
                "global_types": [],
                "internal_tables": [],
                "work_areas": [],
                "constants": [],
                "global_variables": [],
                "output_structure_fields": [
                    {"name": "MESTYP", "source_field": "EDIDC-MESTYP"}
                ],
            },
            indent=2,
        )
        prompt = chunk_prompt_text(
            (
                "You are a Senior SAP ECC ABAP Developer.\n"
                "Generate one complete classical SAP ECC ABAP report from the supplied functional specification.\n"
                "=====================================================================\n"
                "REPORT STRUCTURE\n"
                "=====================================================================\n"
                "START-OF-SELECTION.\n"
                "FORM <read_data_form>.\n"
                "ENDFORM.\n"
                "=====================================================================\n"
                "CLASSICAL SYNTAX\n"
                "=====================================================================\n"
                "Database access rules: use SELECT statements carefully.\n"
                "Processing rules: loop over internal tables.\n"
                "Output rules: display ALV output.\n"
                "Full-program final review: check the complete report.\n"
                "SAP DDIC metadata catalogue:\n"
                "- This catalogue is internal verified metadata. Use these table-field names exactly.\n"
                "- EDIDC: CREDAT, DOCNUM, MESTYP\n"
                "SAP callable signature catalogue:\n"
                "- Z_TEST_FUNCTION: MESSAGE [IMPORTING CHAR]\n"
                "Shared generation contract:\n"
                "Exact internal-table names: t_edidc\n"
                "Exact work-area names: st_edidc\n"
                "Exact output structure fields: DOCNUM, MESTYP\n"
                "Exact FORM names: read_edidc, process_data, output_data\n"
                "Exact callable identities: Z_TEST_FUNCTION\n"
                "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc"
            ),
            {
                "name": "declarations",
                "instruction": "Generate only REPORT, TABLES, TYPES, DATA, constants, PARAMETERS, and SELECT-OPTIONS.",
            },
            source_text="Use SAP table EDIDC. Select EDIDC-CREDAT during database read.",
            declaration_requirements=declaration_requirements,
        )

        self.assertTrue(prompt.startswith("Chunked generation mode:"))
        self.assertIn("Declarations-specific prompt:", prompt)
        self.assertIn("Declaration requirements:\n{", prompt)
        self.assertIn('"type_or_like": "LIKE EDIDC-MESTYP"', prompt)
        self.assertIn('"as_checkbox": true', prompt)
        self.assertIn('"default": "\'ORDERS\'"', prompt)
        self.assertIn("For every entry in tables_declarations", prompt)
        self.assertNotIn("Functional specification:", prompt)
        self.assertNotIn("Select EDIDC-CREDAT during database read.", prompt)
        self.assertIn("SAP DDIC metadata catalogue:", prompt)
        self.assertIn("- EDIDC: DOCNUM, MESTYP", prompt)
        self.assertNotIn("Generate one complete classical SAP ECC ABAP report.", prompt)
        self.assertNotIn("FORM <read_data_form>.", prompt)
        self.assertNotIn("Database access rules", prompt)
        self.assertNotIn("Processing rules", prompt)
        self.assertNotIn("Output rules", prompt)
        self.assertNotIn("SAP callable signature catalogue", prompt)
        self.assertNotIn("Full-program final review", prompt)
        self.assertNotIn("START-OF-SELECTION", prompt)
        self.assertIn("Exact internal-table names: t_edidc", prompt)
        self.assertIn("Exact work-area names: st_edidc", prompt)
        self.assertNotIn("Exact output structure fields:", prompt)
        self.assertNotIn("Exact FORM names:", prompt)
        self.assertNotIn("Exact callable identities:", prompt)
        self.assertIn("- EDIDC: structure st_edidc, table t_edidc, work area st_edidc", prompt)
        self.assertIn("Do not invent alternative names such as t_mpe0001 or wa_edidc.", prompt)

    def test_declarations_chunk_prompt_includes_exact_output_container_names(self):
        declaration_requirements = json.dumps(
            {
                "report_name": "ztest",
                "parameters": [],
                "select_options": [],
                "output_structure_fields": [
                    {"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"}
                ],
            },
            indent=2,
        )

        prompt = chunk_prompt_text(
            (
                "SAP DDIC metadata catalogue:\n"
                "- EDIDC: DOCNUM\n"
                "Shared generation contract:\n"
                "Exact internal-table names: t_edidc\n"
                "Exact work-area names: st_edidc\n"
                "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc"
            ),
            {
                "name": "declarations",
                "instruction": "Generate declarations.",
            },
            declaration_requirements=declaration_requirements,
        )

        expected_contract = (
            "Exact output names: type ty_output (TYPES definition), "
            "internal table t_output (STANDARD TABLE OF ty_output), "
            "work area w_output (TYPE ty_output)"
        )
        self.assertIn(expected_contract, prompt)
        self.assertIn("type ty_output (TYPES definition)", prompt)
        self.assertIn("internal table t_output (STANDARD TABLE OF ty_output)", prompt)
        self.assertIn("work area w_output (TYPE ty_output)", prompt)

    def test_declarations_chunk_prompt_includes_database_read_local_type_contracts(self):
        declaration_requirements = json.dumps(
            {
                "report_name": "ztest",
                "parameters": [],
                "select_options": [],
                "output_structure_fields": [],
            },
            indent=2,
        )
        prompt = chunk_prompt_text(
            (
                "SAP DDIC metadata catalogue:\n"
                "- EDIDC: DOCNUM [NUMC(16); key; IDoc number], CREDAT [DATS(8); Created on], MESTYP [CHAR(30); Message Type]\n"
                "- EDID4: DOCNUM [NUMC(16); key; IDoc number], SEGNAM [CHAR(30); SAP segment name], SDATA [LCHR(1000); Application data]\n"
                "Shared generation contract:\n"
                "Exact internal-table names: t_edidc, t_edid4\n"
                "Exact work-area names: st_edidc, st_edid4\n"
                "Exact FORM names: read_edidc, read_edid4, process_data\n"
                "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
                "- EDID4: structure st_edid4, table t_edid4, work area st_edid4"
            ),
            {
                "name": "declarations",
                "instruction": "Generate declarations.",
            },
            source_text=(
                "# Data Extraction\n"
                "### EDIDC\n"
                "Fields:\n"
                "* DOCNUM\n"
                "* CREDAT\n"
                "### EDID4\n"
                "Fields:\n"
                "* DOCNUM\n"
                "* SEGNAM\n"
                "* SDATA"
            ),
            declaration_requirements=declaration_requirements,
        )

        self.assertIn("Exact database-read local row types:", prompt)
        self.assertIn(
            "- EDIDC: local type ty_edidc; internal table t_edidc TYPE STANDARD TABLE OF ty_edidc; work area st_edidc TYPE ty_edidc; components: docnum TYPE EDIDC-DOCNUM, credat TYPE EDIDC-CREDAT",
            prompt,
        )
        self.assertIn(
            "- EDID4: local type ty_edid4; internal table t_edid4 TYPE STANDARD TABLE OF ty_edid4; work area st_edid4 TYPE ty_edid4; components: docnum TYPE EDID4-DOCNUM, segnam TYPE EDID4-SEGNAM, sdata TYPE EDID4-SDATA",
            prompt,
        )
        self.assertNotIn("segnam TYPE EDIDC-SEGNAM", prompt)
        self.assertNotIn("credat TYPE EDID4-CREDAT", prompt)
        self.assertNotIn("t_edidc type standard table of edidc", prompt.lower())
        self.assertNotIn("t_edid4 type standard table of edid4", prompt.lower())

    def test_database_read_field_order_contract_uses_spec_order_not_metadata_order(self):
        source_text = (
            "# Data Extraction\n"
            "### EDIDS\n"
            "Read Fields\n"
            "* DOCNUM\n"
            "* STATUS\n"
            "* STAMID\n"
            "* STAMNO\n"
            "* STAPA1\n"
            "* STAPA2\n"
            "* STAPA3\n"
            "* STAPA4"
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDS: STATUS [CHAR(2); IDoc Status], STAMID [CHAR(20); Status message ID], STAMNO [CHAR(3); Status message number], STAPA1 [CHAR(50); Status value 1], STAPA2 [CHAR(50); Status value 2], STAPA3 [CHAR(50); Status value 3], STAPA4 [CHAR(50); Status value 4], DOCNUM [NUMC(16); key; IDoc number]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edids\n"
            "Exact work-area names: st_edids\n"
            "Exact FORM names: read_edids\n"
            "- EDIDS: structure st_edids, table t_edids, work area st_edids"
        )

        database_prompt = chunk_prompt_text(
            base_prompt,
            {"name": "database_read_forms", "instruction": "Generate database reads."},
            source_text=source_text,
        )
        declarations_prompt = chunk_prompt_text(
            base_prompt,
            {"name": "declarations", "instruction": "Generate declarations."},
            source_text=source_text,
            declaration_requirements=json.dumps({"parameters": [], "select_options": [], "output_structure_fields": []}),
        )

        expected_order = "EDIDS: DOCNUM, STATUS, STAMID, STAMNO, STAPA1, STAPA2, STAPA3, STAPA4"
        self.assertIn("Exact database-read SELECT field order:", database_prompt)
        self.assertIn(expected_order, database_prompt)
        self.assertIn(
            "- EDIDS: local type ty_edids; internal table t_edids TYPE STANDARD TABLE OF ty_edids; work area st_edids TYPE ty_edids; components: docnum TYPE EDIDS-DOCNUM, status TYPE EDIDS-STATUS, stamid TYPE EDIDS-STAMID, stamno TYPE EDIDS-STAMNO, stapa1 TYPE EDIDS-STAPA1, stapa2 TYPE EDIDS-STAPA2, stapa3 TYPE EDIDS-STAPA3, stapa4 TYPE EDIDS-STAPA4",
            declarations_prompt,
        )

    def test_database_read_field_order_contract_uses_only_read_fields(self):
        source_text = (
            "## Table Reads\n"
            "### ZHEAD\n"
            "Read Fields:\n"
            "* KEY1\n"
            "WHERE Conditions:\n"
            "* FILTER1 using selection option\n"
            "* DATE_FROM LE current_date\n"
            "### ZITEM\n"
            "Read ZITEM as a separate dependent table read using the ZHEAD keys.\n"
            "Read Fields:\n"
            "* KEY1\n"
            "* NAME1\n"
            "* NAME2\n"
            "WHERE Conditions:\n"
            "* KEY1 = ZHEAD-KEY1\n"
            "* DATE_FROM LE current_date\n"
            "* DATE_TO GE current_date"
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHEAD: KEY1 [CHAR(10); key], FILTER1 [CHAR(1)], DATE_FROM [DATS(8)], DATE_TO [DATS(8)]\n"
            "- ZITEM: KEY1 [CHAR(10); key], NAME1 [CHAR(40)], NAME2 [CHAR(40)], DATE_FROM [DATS(8)], DATE_TO [DATS(8)]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_zhead, t_zitem\n"
            "Exact work-area names: st_zhead, st_zitem\n"
            "Exact FORM names: read_zhead, read_zitem\n"
            "- ZHEAD: structure st_zhead, table t_zhead, work area st_zhead\n"
            "- ZITEM: structure st_zitem, table t_zitem, work area st_zitem"
        )

        prompt = chunk_prompt_text(
            base_prompt,
            {"name": "database_read_forms", "instruction": "Generate database reads."},
            source_text=source_text,
        )

        self.assertIn("- ZHEAD: KEY1", prompt)
        self.assertIn("- ZITEM: KEY1, NAME1, NAME2", prompt)
        self.assertNotIn("- ZHEAD: KEY1, FILTER1", prompt)
        self.assertNotIn("- ZITEM: KEY1, NAME1, NAME2, DATE_FROM", prompt)

    def test_database_read_contract_uses_full_metadata_for_read_fields_beyond_compact_catalogue(self):
        source_text = (
            "## Table Reads\n"
            "### ZMD_MPE0001\n"
            "Join to ZMD_MPE0001-DOCNUM_IN = EDIDC-DOCNUM\n"
            "Selection Fields\n"
            "IDENTIFIER\n"
            "Read Fields\n"
            "IDENTIFIER\n"
            "DOCNUM_IN"
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZMD_MPE0001: IDENTIFIER [CHAR(32); key; Identifier]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_zmd_mpe0001\n"
            "Exact work-area names: st_zmd_mpe0001\n"
            "Exact FORM names: read_zmd_mpe0001\n"
            "- ZMD_MPE0001: structure st_zmd_mpe0001, table t_zmd_mpe0001, work area st_zmd_mpe0001"
        )
        ddic_metadata = {
            "tables": {
                "ZMD_MPE0001": {
                    "fields": {
                        "IDENTIFIER": {"datatype": "CHAR", "length": 32, "key": True},
                        "DOCNUM_IN": {"datatype": "NUMC", "length": 16},
                        "COUNTER": {"datatype": "NUMC", "length": 3},
                    },
                    "field_order": ["IDENTIFIER", "DOCNUM_IN", "COUNTER"],
                }
            }
        }

        database_prompt = chunk_prompt_text(
            base_prompt,
            {"name": "database_read_forms", "instruction": "Generate database reads."},
            source_text=source_text,
            ddic_metadata=ddic_metadata,
        )
        declarations = ensure_database_read_declarations(
            "REPORT ztest.",
            base_prompt,
            source_text=source_text,
            declaration_requirements=json.dumps({"parameters": [], "select_options": [], "output_structure_fields": []}),
            ddic_metadata=ddic_metadata,
        )

        self.assertIn("- ZMD_MPE0001: IDENTIFIER, DOCNUM_IN", database_prompt)
        self.assertIn("docnum_in TYPE ZMD_MPE0001-DOCNUM_IN", declarations)

    def test_declaration_post_processing_generates_subset_database_read_row_type(self):
        source_text = (
            "# Data Extraction\n"
            "### ZHDR\n"
            "Read Fields\n"
            "* KEY_FIELD\n"
            "* STATUS"
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHDR: KEY_FIELD [CHAR(10); key], STATUS [CHAR(1)], DESCRIPTION [CHAR(40)]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_zhdr\n"
            "Exact work-area names: st_zhdr\n"
            "Exact FORM names: read_zhdr\n"
            "- ZHDR: structure st_zhdr, table t_zhdr, work area st_zhdr"
        )

        result = ensure_database_read_declarations(
            "REPORT ztest.\nDATA t_zhdr TYPE STANDARD TABLE OF ZHDR.\nDATA st_zhdr TYPE ZHDR.",
            base_prompt,
            source_text=source_text,
            declaration_requirements=json.dumps({"parameters": [], "select_options": [], "output_structure_fields": []}),
        )

        self.assertIn("TYPES: BEGIN OF ty_zhdr,", result)
        self.assertIn("         key_field TYPE ZHDR-KEY_FIELD,", result)
        self.assertIn("         status TYPE ZHDR-STATUS,", result)
        self.assertIn("       END OF ty_zhdr.", result)
        self.assertIn("DATA t_zhdr TYPE STANDARD TABLE OF ty_zhdr.", result)
        self.assertIn("DATA st_zhdr TYPE ty_zhdr.", result)
        self.assertNotIn("DESCRIPTION TYPE ZHDR-DESCRIPTION", result)
        self.assertNotIn("TYPE STANDARD TABLE OF ZHDR", result)

    def test_database_read_declarations_include_actual_select_projection_fields(self):
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZDOC: DOC_DATE [DATS(8); key], SITE [CHAR(4); key], CUSTOMER [CHAR(10); key], DOC_ID [CHAR(10); key], UNIT_CODE [UNIT(3); key], SALES_AMOUNT [CURR(13,2)], COST_AMOUNT [CURR(13,2)]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_zdoc\n"
            "Exact work-area names: st_zdoc\n"
            "Exact FORM names: read_zdoc\n"
            "- ZDOC: structure st_zdoc, table t_zdoc, work area st_zdoc"
        )
        source = "\n".join(
            [
                "REPORT ztest.",
                "TYPES: BEGIN OF ty_zdoc,",
                "         doc_date TYPE zdoc-doc_date,",
                "         site TYPE zdoc-site,",
                "         customer TYPE zdoc-customer,",
                "         sales_amount TYPE zdoc-sales_amount,",
                "         cost_amount TYPE zdoc-cost_amount,",
                "       END OF ty_zdoc.",
                "DATA t_zdoc TYPE STANDARD TABLE OF ty_zdoc.",
                "DATA st_zdoc TYPE ty_zdoc.",
                "FORM read_zdoc.",
                "  SELECT site",
                "         customer",
                "         doc_id",
                "         unit_code",
                "         SUM( sales_amount ) AS sales_amount",
                "         SUM( cost_amount ) AS cost_amount",
                "    FROM zdoc",
                "    INTO CORRESPONDING FIELDS OF TABLE t_zdoc",
                "    GROUP BY site",
                "             customer",
                "             doc_id",
                "             unit_code.",
                "ENDFORM.",
            ]
        )

        result = ensure_database_read_declarations(
            source,
            base_prompt,
            source_text="Read ZDOC and aggregate at document level.",
            declaration_requirements=json.dumps({"parameters": [], "select_options": [], "output_structure_fields": []}),
        )

        self.assertIn("         doc_id TYPE ZDOC-DOC_ID,", result)
        self.assertIn("         unit_code TYPE ZDOC-UNIT_CODE,", result)
        self.assertIn("         sales_amount TYPE ZDOC-SALES_AMOUNT,", result)
        self.assertIn("         cost_amount TYPE ZDOC-COST_AMOUNT,", result)
        self.assertLess(result.index("doc_id TYPE ZDOC-DOC_ID"), result.index("DATA t_zdoc TYPE STANDARD TABLE OF ty_zdoc."))

    def test_declaration_post_processing_uses_ddic_structure_when_all_fields_are_required(self):
        source_text = (
            "# Data Extraction\n"
            "### ZITEM\n"
            "Read Fields\n"
            "* DOCNUM\n"
            "* ITEMNO"
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZITEM: DOCNUM [CHAR(10); key], ITEMNO [NUMC(6)]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_zitem\n"
            "Exact work-area names: st_zitem\n"
            "Exact FORM names: read_zitem\n"
            "- ZITEM: structure st_zitem, table t_zitem, work area st_zitem"
        )

        result = ensure_database_read_declarations(
            "REPORT ztest.\nTYPES: BEGIN OF ty_zitem,\n         docnum TYPE ZITEM-DOCNUM,\n       END OF ty_zitem.",
            base_prompt,
            source_text=source_text,
            declaration_requirements=json.dumps({"parameters": [], "select_options": [], "output_structure_fields": []}),
        )

        self.assertIn("DATA t_zitem TYPE STANDARD TABLE OF ZITEM.", result)
        self.assertIn("DATA st_zitem TYPE ZITEM.", result)
        self.assertNotIn("TYPES: BEGIN OF ty_zitem", result)
        self.assertNotIn("TYPE STANDARD TABLE OF ty_zitem", result)

    def test_declaration_prefix_grouping_matches_latest_job_order(self):
        result = group_declaration_statements_by_prefix(
            "\n".join(
                [
                    "REPORT ZHR_PROBATION_PER_EMAILS.",
                    "TABLES: PA0000,",
                    "        PA0001.",
                    "TYPES: BEGIN OF ty_pa0000,",
                    "         pernr TYPE PA0000-PERNR,",
                    "       END OF ty_pa0000.",
                    "DATA t_pa0000 TYPE STANDARD TABLE OF ty_pa0000.",
                    "DATA st_pa0000 TYPE ty_pa0000.",
                    "TYPES: BEGIN OF ty_pa0001,",
                    "         btrtl TYPE PA0001-BTRTL,",
                    "         persk TYPE PA0001-PERSK,",
                    "         pernr TYPE PA0001-PERNR,",
                    "       END OF ty_pa0001.",
                    "DATA t_pa0001 TYPE STANDARD TABLE OF ty_pa0001.",
                    "DATA st_pa0001 TYPE ty_pa0001.",
                    "TYPES: BEGIN OF ty_pa0016,",
                    "         pernr TYPE PA0016-PERNR,",
                    "         probation_email_sent TYPE PA0016-PROBATION_EMAIL_SENT,",
                    "       END OF ty_pa0016.",
                    "DATA t_pa0016 TYPE STANDARD TABLE OF ty_pa0016.",
                    "DATA st_pa0016 TYPE ty_pa0016.",
                    "TYPES: BEGIN OF ty_output,",
                    "         pernr TYPE PA0000-PERNR,",
                    "       END OF ty_output.",
                    "TYPES ty_email TYPE string.",
                    "DATA t_output TYPE STANDARD TABLE OF ty_output.",
                    "DATA w_output TYPE ty_output.",
                    "PARAMETERS: P_REPORT RADIOBUTTON GROUP emod.",
                    "START-OF-SELECTION.",
                    "  PERFORM process_data.",
                    "FORM process_data.",
                    "ENDFORM.",
                ]
            )
        )

        ordered_markers = [
            "TYPES: BEGIN OF ty_pa0000,",
            "TYPES: BEGIN OF ty_pa0001,",
            "TYPES: BEGIN OF ty_pa0016,",
            "TYPES: BEGIN OF ty_output,",
            "TYPES ty_email TYPE string.",
            "DATA t_pa0000 TYPE STANDARD TABLE OF ty_pa0000.",
            "DATA t_pa0001 TYPE STANDARD TABLE OF ty_pa0001.",
            "DATA t_pa0016 TYPE STANDARD TABLE OF ty_pa0016.",
            "DATA t_output TYPE STANDARD TABLE OF ty_output.",
            "DATA st_pa0000 TYPE ty_pa0000.",
            "DATA st_pa0001 TYPE ty_pa0001.",
            "DATA st_pa0016 TYPE ty_pa0016.",
            "DATA w_output TYPE ty_output.",
            "PARAMETERS: P_REPORT RADIOBUTTON GROUP emod.",
            "START-OF-SELECTION.",
            "FORM process_data.",
        ]
        positions = [result.index(marker) for marker in ordered_markers]
        self.assertEqual(positions, sorted(positions))

    def test_database_read_declaration_replacement_preserves_output_type_in_chained_types(self):
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- PA0000: PERNR [NUMC(8); key], STAT2 [CHAR(1)], BEGDA [DATS(8)], ENDDA [DATS(8)]\n"
            "- PA0001: PERNR [NUMC(8); key], ABKRS [CHAR(2)], KOSTL [CHAR(10)], BEGDA [DATS(8)], ENDDA [DATS(8)]\n"
            "- PA0002: NACHN [CHAR(40)], BEGDA [DATS(8)], ENDDA [DATS(8)]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_pa0000, t_pa0001, t_pa0002\n"
            "Exact work-area names: st_pa0000, st_pa0001, st_pa0002\n"
            "- PA0000: structure st_pa0000, table t_pa0000, work area st_pa0000\n"
            "- PA0001: structure st_pa0001, table t_pa0001, work area st_pa0001\n"
            "- PA0002: structure st_pa0002, table t_pa0002, work area st_pa0002"
        )
        source_text = "Read PA0000-PERNR PA0000-STAT2 PA0000-BEGDA PA0000-ENDDA PA0001-PERNR PA0001-ABKRS PA0001-KOSTL PA0001-BEGDA PA0001-ENDDA PA0002-NACHN PA0002-BEGDA PA0002-ENDDA."
        source = "\n".join(
            [
                "REPORT tesco_mobile_file.",
                "TYPES: BEGIN OF ty_pa0000,",
                "         pernr TYPE pa0000-pernr,",
                "       END OF ty_pa0000,",
                "       BEGIN OF ty_pa0001,",
                "         pernr TYPE pa0001-pernr,",
                "       END OF ty_pa0001,",
                "       BEGIN OF ty_pa0002,",
                "         nachn TYPE pa0002-nachn,",
                "       END OF ty_pa0002,",
                "       BEGIN OF ty_output,",
                "         pernr TYPE pa0000-pernr,",
                "         nachn TYPE pa0002-nachn,",
                "       END OF ty_output.",
                "DATA w_output TYPE ty_output.",
            ]
        )

        result = ensure_database_read_declarations(
            source,
            base_prompt,
            source_text=source_text,
            declaration_requirements=json.dumps({"parameters": [], "select_options": [], "output_structure_fields": []}),
        )

        self.assertIn("TYPES: BEGIN OF ty_output,", result)
        self.assertIn("       END OF ty_output.", result)
        self.assertIn("DATA w_output TYPE ty_output.", result)
        self.assertLess(result.index("TYPES: BEGIN OF ty_output,"), result.index("DATA w_output TYPE ty_output."))

    def test_standard_report_header_is_added_after_report_statement(self):
        result = ensure_standard_report_header(
            "\n".join(
                [
                    "REPORT ZHR_PROBATION_PER_EMAILS.",
                    "TYPES: BEGIN OF ty_pa0000,",
                    "         pernr TYPE PA0000-PERNR,",
                    "       END OF ty_pa0000.",
                    "DATA t_pa0000 TYPE STANDARD TABLE OF ty_pa0000.",
                ]
            )
        )

        self.assertIn("*  Report      : ZHR_PROBATION_PER_EMAILS", result)
        self.assertIn("*  Revision History", result)
        self.assertLess(result.index("REPORT ZHR_PROBATION_PER_EMAILS."), result.index("*  Report      : ZHR_PROBATION_PER_EMAILS"))
        self.assertLess(result.index("*  Revision History"), result.index("TYPES: BEGIN OF ty_pa0000,"))
        self.assertEqual(result, ensure_standard_report_header(result))

    def test_standard_report_header_aligns_author_after_long_report_name(self):
        result = ensure_standard_report_header(
            "\n".join(
                [
                    "REPORT ztesco_mobile_file.",
                    "DATA gv_count TYPE i.",
                ]
            )
        )

        self.assertIn(
            "*  Report      : ztesco_mobile_file      Author :                      *",
            result,
        )

    def test_form_chunks_reject_local_data_and_types_declarations(self):
        source = "\n".join(
            [
                "FORM process_data.",
                "  DATA lv_key TYPE ZHDR-KEY_FIELD.",
                "  TYPES: BEGIN OF ty_local,",
                "           key_field TYPE ZHDR-KEY_FIELD,",
                "         END OF ty_local.",
                "ENDFORM.",
            ]
        )

        with self.assertRaisesRegex(ValueError, "generated local declaration"):
            ensure_form_chunk_uses_declared_globals(source, "processing_form")

    def test_shared_no_local_form_rule_reaches_form_generating_chunks(self):
        declaration_requirements = json.dumps(
            {
                "report_name": "ztest",
                "parameters": [],
                "select_options": [],
                "output_structure_fields": [
                    {"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"}
                ],
            },
            indent=2,
        )
        base_prompt = append_generation_contract(
            (
                "SAP DDIC metadata catalogue:\n"
                "- EDIDC: DOCNUM [NUMC(16); key; IDoc number]\n"
            ),
            {
                "internal_tables": ["t_edidc"],
                "work_areas": ["st_edidc"],
                "output_structure_fields": ["DOCNUM"],
                "form_names": ["read_edidc", "process_data", "output_data"],
                "callable_identities": [],
                "ddic_objects": [
                    {
                        "name": "EDIDC",
                        "structure": "st_edidc",
                        "table": "t_edidc",
                        "work_area": "st_edidc",
                    }
                ],
            },
        )
        no_local_rules = [
            "- Do not create local declarations inside FORM routines.",
            "- Do not generate DATA, TYPES, CONSTANTS, FIELD-SYMBOLS, RANGES, or STATICS declarations inside any FORM.",
            "- Every variable required by generated forms must be declared globally by the declarations chunk.",
            "- FORM routines must reuse the exact global names from this shared naming contract.",
            "- Do not invent local names such as lt_*, ls_*, lv_*, wa_*, gt_*, gs_*, or gv_*.",
        ]

        prompts = {
            name: chunk_prompt_text(
                base_prompt,
                {"name": name, "instruction": f"Generate {name}."},
                source_text="Read EDIDC-DOCNUM and display DOCNUM.",
                declaration_requirements=declaration_requirements,
            )
            for name in ("declarations", "database_read_forms", "processing_form", "output_forms", "main_program_flow")
        }

        for chunk_name in ("database_read_forms", "processing_form", "output_forms"):
            for rule in no_local_rules:
                self.assertIn(rule, prompts[chunk_name])
            self.assertNotIn("Default prefixes:", prompts[chunk_name])
            self.assertNotIn("Internal tables  t_", prompts[chunk_name])

        self.assertIn("Exact internal-table names: t_edidc", prompts["declarations"])
        self.assertIn("Exact work-area names: st_edidc", prompts["declarations"])
        self.assertIn("- EDIDC: structure st_edidc, table t_edidc, work area st_edidc", prompts["declarations"])
        self.assertIn(
            "- EDIDC: local type ty_edidc; internal table t_edidc TYPE STANDARD TABLE OF ty_edidc; work area st_edidc TYPE ty_edidc; components: docnum TYPE EDIDC-DOCNUM",
            prompts["declarations"],
        )
        self.assertIn(
            "Exact output names: type ty_output (TYPES definition), internal table t_output (STANDARD TABLE OF ty_output), work area w_output (TYPE ty_output)",
            prompts["declarations"],
        )
        self.assertNotIn("- Do not create local declarations inside FORM routines.", prompts["main_program_flow"])

    def test_database_read_chunk_prompt_uses_only_database_template_context(self):
        base_prompt = (
            "Generate ABAP.\n"
            "Generate one complete classical SAP ECC ABAP report from the supplied functional specification.\n"
            "Database access rules: use SELECT statements carefully.\n"
            "Processing rules: loop over internal tables.\n"
            "Output rules: display ALV output.\n"
            "Full-program final review: check the complete report.\n"
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM [NUMC(16); key; IDoc number], MESTYP [CHAR(30); Message Type]\n"
            "SAP callable signature catalogue:\n"
            "- Z_TEST_FUNCTION: MESSAGE [IMPORTING CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc\n"
            "Exact work-area names: w_edidc\n"
            "Exact output structure fields: DOCNUM, MESTYP\n"
            "Exact FORM names: read_edidc, process_data, output_data, display_alv, write_csv\n"
            "Exact callable identities: Z_TEST_FUNCTION\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area w_edidc"
        )

        prompt = chunk_prompt_text(
            base_prompt,
            {
                "name": "database_read_forms",
                "instruction": (
                    "Generate only database read FORM routines. "
                    "Do not generate REPORT statements, declarations, selection-screen declarations, or event blocks."
                ),
            },
            source_text="Use SAP table EDIDC.",
        )

        self.assertIn("Database-read chunk prompt:", prompt)
        self.assertIn("Database-read requirements:", prompt)
        self.assertIn("Relevant specification excerpts:\n- Use SAP table EDIDC.", prompt)
        self.assertNotIn("Functional specification:", prompt)
        self.assertIn("SAP DDIC metadata catalogue:", prompt)
        self.assertIn("- EDIDC: DOCNUM", prompt)
        self.assertNotIn("no fields selected", prompt)
        self.assertNotIn("MESTYP", prompt)
        self.assertNotIn("CREDAT", prompt)
        self.assertIn("Exact database FORM names: read_edidc", prompt)
        self.assertIn("Exact internal-table names: t_edidc", prompt)
        self.assertIn("Exact work-area names: w_edidc", prompt)
        self.assertIn("- EDIDC: structure st_edidc, table t_edidc, work area w_edidc", prompt)
        self.assertNotIn("Generate one complete classical SAP ECC ABAP report", prompt)
        self.assertNotIn("Database access rules: use SELECT statements carefully.", prompt)
        self.assertNotIn("Processing rules", prompt)
        self.assertNotIn("Output rules", prompt)
        self.assertNotIn("Full-program final review", prompt)
        self.assertNotIn("SAP callable signature catalogue:", prompt)
        self.assertNotIn("Exact output structure fields:", prompt)
        self.assertNotIn("Exact callable identities:", prompt)
        self.assertNotIn("process_data", prompt)
        self.assertNotIn("output_data", prompt)
        self.assertNotIn("display_alv", prompt)
        self.assertNotIn("write_csv", prompt)
        self.assertNotIn("Declarations-specific prompt:", prompt)

    def test_main_flow_chunk_prompt_uses_only_main_flow_template_context(self):
        base_prompt = (
            "Generate ABAP.\n"
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM, MESTYP\n"
            "SAP callable signature catalogue:\n"
            "- Z_TEST_FUNCTION: MESSAGE [IMPORTING CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc\n"
            "Exact work-area names: w_edidc\n"
            "Exact output structure fields: DOCNUM, MESTYP\n"
            "Exact FORM names: read_edidc, process_data, output_data, write_csv\n"
            "Exact callable identities: Z_TEST_FUNCTION\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area w_edidc"
        )

        prompt = chunk_prompt_text(
            base_prompt,
            {"name": "main_program_flow", "instruction": "Generate only the main program flow."},
            source_text="Use SAP table EDIDC.",
        )

        self.assertIn("Main program flow chunk prompt:", prompt)
        self.assertIn("Main-flow requirements:\nRelevant naming contract:", prompt)
        self.assertIn("Exact FORM names: read_edidc, process_data, output_data, write_csv", prompt)
        self.assertNotIn("SAP DDIC metadata catalogue:", prompt)
        self.assertNotIn("SAP callable signature catalogue:", prompt)
        self.assertNotIn("Exact internal-table names:", prompt)
        self.assertNotIn("Exact work-area names:", prompt)
        self.assertNotIn("Exact output structure fields:", prompt)
        self.assertNotIn("Exact callable identities:", prompt)

    def test_processing_and_output_chunks_receive_filtered_ddic_metadata(self):
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: CREDAT, DOCNUM, MESTYP\n"
            "- EDID4: DOCNUM, SDATA, SEGNAM\n"
            "SAP callable signature catalogue:\n"
            "- Z_TEST_FUNCTION: MESSAGE [IMPORTING CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc, t_edid4\n"
            "Exact work-area names: w_edidc, w_edid4\n"
            "Exact output structure fields: DOCNUM, SDATA\n"
            "Exact FORM names: read_edidc, read_edid4, process_data, output_data, write_csv\n"
            "Exact callable identities: Z_TEST_FUNCTION\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area w_edidc\n"
            "- EDID4: structure st_edid4, table t_edid4, work area w_edid4"
        )

        processing_prompt = chunk_prompt_text(
            base_prompt,
            {"name": "processing_form", "instruction": "Generate processing."},
            source_text="Loop EDID4 and append SDATA to output DOCNUM.",
            processing_plan=json.dumps(
                {
                    "processing_steps": [
                        {"step": 1, "operation": "LOOP", "source": "t_edid4"},
                        {"step": 2, "operation": "MOVE", "source": "w_edid4-sdata", "target": "w_output-sdata"},
                        {"step": 3, "operation": "APPEND", "source": "w_output", "target": "t_output"},
                    ]
                },
                indent=2,
            ),
        )
        output_prompt = chunk_prompt_text(
            base_prompt,
            {"name": "output_forms", "instruction": "Generate output."},
            source_text="Export output DOCNUM and SDATA to CSV.",
        )

        self.assertIn("Filtered SAP DDIC metadata required by the plan:", processing_prompt)
        self.assertIn("- EDID4: SDATA", processing_prompt)
        self.assertNotIn("EDIDC:", processing_prompt)
        self.assertNotIn("EDID4: DOCNUM", processing_prompt)
        self.assertNotIn("MESTYP", processing_prompt)
        self.assertNotIn("CREDAT", processing_prompt)
        self.assertIn("Relevant SAP DDIC metadata:", output_prompt)
        self.assertIn("- EDID4: SDATA", output_prompt)
        self.assertNotIn("EDIDC:", output_prompt)
        self.assertNotIn("EDID4: DOCNUM", output_prompt)
        self.assertNotIn("MESTYP", output_prompt)
        self.assertNotIn("CREDAT", output_prompt)

    def test_processing_prompt_uses_structured_plan_as_authoritative_logic(self):
        source_text = (
            "## Dependent Table Reads\n"
            "### EDIDC\n"
            "Read Fields\n"
            "DOCNUM\n"
            "\n"
            "### Processing Rules\n"
            "For each selected IDoc from EDIDC\n"
            "Get the IDoc status variables from EDIDS\n"
            "Get the Error Message using BAPI_MESSAGE_GETDETAIL\n"
            "Use the following EDIDS fields:\n"
            "STAMID\n"
            "STAMNO\n"
            "STAPA1\n"
            "STAPA2\n"
            "STAPA3\n"
            "STAPA4\n"
            "Use only the RETURN parameter as the final error message.\n"
            "Identifier Resolution\n"
            "Determine the MPE identifier using the following priority:\n"
            "ZMD_MPE0001\n"
            "ZMD_MPE0006\n"
            "If no identifier is found, do not produce an output record.\n"
            "Output Record\n"
            "Create one output record containing:\n"
            "MPE_ID\n"
            "IDOC_STATUS\n"
            "ERROR_MESSAGE\n"
            "Include:\n"
            "IDOC_NUMBER\n"
            "only when p_idoc is selected.\n"
            "\n"
            "# ALV Output\n"
            "When p_alv is selected display MPE_ID.\n"
            "\n"
            "Processing Rules\n"
            "Complete all database access before record processing.\n"
            "Resolve identifiers before building the output record.\n"
            "Generate one output record per qualifying IDoc.\n"
            "Acceptance Criteria\n"
            "The report shall compile.\n"
        )

        prompt = chunk_prompt_text(
            (
                "SAP callable signature catalogue:\n"
                "- BAPI_MESSAGE_GETDETAIL: RETURN [EXPORTING BAPIRET2]\n"
                "Shared generation contract:\n"
                "Exact internal-table names: t_edidc, t_edids\n"
                "Exact work-area names: st_edidc, st_edids\n"
                "Exact FORM names: process_data\n"
                "Exact callable identities: BAPI_MESSAGE_GETDETAIL"
            ),
            {"name": "processing_form", "instruction": "Generate processing."},
            source_text=source_text,
            processing_plan=json.dumps(
                {
                    "processing_steps": [
                        {"step": 1, "operation": "LOOP", "source": "t_edidc"},
                        {"step": 2, "operation": "READ", "source": "t_edids", "match": "st_edids-docnum = st_edidc-docnum"},
                        {"step": 3, "operation": "CALL_FUNCTION", "name": "BAPI_MESSAGE_GETDETAIL"},
                        {"step": 4, "operation": "APPEND", "source": "w_output", "target": "t_output"},
                    ]
                },
                indent=2,
            ),
        )

        self.assertIn("Use the structured processing plan as the authoritative source of processing logic.", prompt)
        self.assertIn('"operation": "READ"', prompt)
        self.assertIn('"match": "st_edids-docnum = st_edidc-docnum"', prompt)
        self.assertIn('"name": "BAPI_MESSAGE_GETDETAIL"', prompt)
        self.assertNotIn("For each selected IDoc from EDIDC", prompt)
        self.assertNotIn("STAPA4", prompt)
        self.assertNotIn("ZMD_MPE0001", prompt)
        self.assertNotIn("Relevant specification excerpts:", prompt)
        self.assertNotIn("Relevant naming contract:", prompt)
        self.assertNotIn("When p_alv is selected display MPE_ID.", prompt)
        self.assertNotIn("The report shall compile.", prompt)

    def test_processing_prompt_requires_complete_calculation_and_aggregation_logic(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "CUSTOMER", "type_or_like": "TYPE ZS505-KUNNR"},
                    {"name": "SITE", "type_or_like": "TYPE ZS505-WERKS"},
                    {"name": "DOCUMENT_COUNT", "type_or_like": "TYPE i"},
                    {"name": "SALES_TOTAL", "type_or_like": "TYPE p DECIMALS 2"},
                    {"name": "GROSS_MARGIN", "type_or_like": "TYPE p DECIMALS 2"},
                    {"name": "MARGIN_PERCENT", "type_or_like": "TYPE p DECIMALS 2"},
                ]
            },
            indent=2,
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZS505: KUNNR [CHAR], WERKS [CHAR], VBELN [CHAR], KZWI2 [CURR], WAVWR [CURR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_zs505, t_output\n"
            "Exact work-area names: st_zs505, w_output\n"
            "Exact FORM names: process_data\n"
            "- ZS505: structure st_zs505, table t_zs505, work area st_zs505"
        )

        prompt = chunk_prompt_text(
            base_prompt,
            {"name": "processing_form", "instruction": "Generate processing."},
            source_text="Aggregate by customer and site, count documents, total sales, calculate gross margin and margin percentage.",
            declaration_requirements=declaration_requirements,
            processing_plan=json.dumps(
                {
                    "processing_steps": [
                        {
                            "operation": "AGGREGATE",
                            "source": "t_zs505",
                            "target": "w_output-SALES_TOTAL",
                            "function": "SUM",
                            "group_by": ["st_zs505-KUNNR", "st_zs505-WERKS"],
                            "sources": ["st_zs505-KZWI2"],
                        },
                        {
                            "operation": "COUNT",
                            "source": "t_zs505",
                            "target": "w_output-DOCUMENT_COUNT",
                            "group_by": ["st_zs505-KUNNR", "st_zs505-WERKS"],
                            "distinct": "st_zs505-VBELN",
                        },
                        {
                            "operation": "CALCULATE",
                            "target": "w_output-GROSS_MARGIN",
                            "expression": "w_output-SALES_TOTAL - w_output-COST_TOTAL",
                            "sources": ["w_output-SALES_TOTAL", "w_output-COST_TOTAL"],
                        },
                        {
                            "operation": "PERCENTAGE",
                            "target": "w_output-MARGIN_PERCENT",
                            "numerator": "w_output-GROSS_MARGIN",
                            "denominator": "w_output-SALES_TOTAL",
                            "group_by": ["st_zs505-KUNNR", "st_zs505-WERKS"],
                        },
                        {"operation": "APPEND", "source": "w_output", "target": "t_output"},
                    ]
                },
                indent=2,
            ),
        )

        self.assertIn("Implement every MOVE, CALCULATE, DERIVE, TRANSFORM, AGGREGATE, COUNT, AVERAGE, PERCENTAGE", prompt)
        self.assertIn("Required processing output fields:", prompt)
        self.assertIn("Required output population path: w_output-DOCUMENT_COUNT", prompt)
        self.assertIn("Required output population path: w_output-GROSS_MARGIN", prompt)
        self.assertIn("Required output population path: w_output-MARGIN_PERCENT", prompt)
        self.assertIn("If group_by is present", prompt)
        self.assertIn("Do not return placeholder, comment-only, or field-copy-only processing logic", prompt)

    def test_processing_prompt_filters_metadata_to_structured_plan_requirements(self):
        declaration_requirements = json.dumps(
            {
                "parameters": [
                    {"name": "p_idoc", "type_or_like": "TYPE c", "as_checkbox": True},
                    {"name": "p_unused", "type_or_like": "TYPE c", "as_checkbox": True},
                ],
                "select_options": [],
                "global_variables": [],
                "output_structure_fields": [
                    {"name": "IDOC_STATUS", "type_or_like": "TYPE EDIDS-STATUS"},
                    {"name": "ERROR_MESSAGE", "type_or_like": "TYPE bapi_msg"},
                ],
            },
            indent=2,
        )
        processing_plan = json.dumps(
            {
                "processing_steps": [
                    {"step": 1, "operation": "LOOP", "source": "t_edidc"},
                    {"step": 2, "operation": "READ", "source": "t_edids", "match": "st_edids-docnum = st_edidc-docnum"},
                    {"step": 3, "operation": "MOVE", "source": "st_edids-status", "target": "w_output-idoc_status"},
                    {
                        "step": 4,
                        "operation": "CALL_FUNCTION",
                        "name": "BAPI_MESSAGE_GETDETAIL",
                        "parameters": {
                            "ID": "st_edids-stamid",
                            "NUMBER": "st_edids-stamno",
                            "RETURN": "w_output-error_message",
                        },
                    },
                    {"step": 5, "operation": "IF", "condition": "p_idoc = 'X'"},
                    {"step": 6, "operation": "APPEND", "source": "w_output", "target": "t_output"},
                ]
            },
            indent=2,
        )
        prompt = chunk_prompt_text(
            (
                "SAP DDIC metadata catalogue:\n"
                "- EDIDC: CREDAT [DATS], DOCNUM [NUMC], MESTYP [CHAR]\n"
                "- EDIDS: DOCNUM [NUMC], STATUS [CHAR], STAMID [CHAR], STAMNO [NUMC], STAPA1 [CHAR]\n"
                "- ZUNUSED: FIELD1 [CHAR]\n"
                "SAP callable signature catalogue:\n"
                "- BAPI_MESSAGE_GETDETAIL: ID [IMPORTING CHAR], NUMBER [IMPORTING NUMC], RETURN [EXPORTING BAPIRET2], MESSAGE [EXPORTING CHAR]\n"
                "- Z_UNUSED_FUNCTION: VALUE [IMPORTING CHAR]\n"
                "Shared generation contract:\n"
                "Exact internal-table names: t_edidc, t_edids, t_unused\n"
                "Exact work-area names: st_edidc, st_edids, st_unused\n"
                "Exact FORM names: read_edidc, read_unused, process_data, output_data\n"
                "Exact callable identities: BAPI_MESSAGE_GETDETAIL, Z_UNUSED_FUNCTION\n"
                "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
                "- EDIDS: structure st_edids, table t_edids, work area st_edids\n"
                "- ZUNUSED: structure st_unused, table t_unused, work area st_unused"
            ),
            {"name": "processing_form", "instruction": "Generate processing."},
            source_text="Spec text that should not drive processing prompt filtering. Mentions ZUNUSED FIELD1.",
            declaration_requirements=declaration_requirements,
            processing_plan=processing_plan,
        )

        self.assertEqual(1, prompt.count("Structured processing plan:"))
        self.assertEqual(1, prompt.count("Filtered SAP callable metadata required by the plan:"))
        self.assertEqual(1, prompt.count("Filtered processing chunk contract:"))
        self.assertNotIn("Relevant specification excerpts:", prompt)
        self.assertNotIn("Relevant naming contract:", prompt)

        self.assertIn("- EDIDC: DOCNUM", prompt)
        self.assertIn("- EDIDS: DOCNUM [NUMC], STATUS [CHAR], STAMID [CHAR], STAMNO [NUMC]", prompt)
        self.assertNotIn("CREDAT", prompt)
        self.assertNotIn("MESTYP", prompt)
        self.assertNotIn("STAPA1", prompt)
        self.assertNotIn("ZUNUSED", prompt)

        self.assertIn("- BAPI_MESSAGE_GETDETAIL: ID [IMPORTING CHAR], NUMBER [IMPORTING NUMC], RETURN [EXPORTING BAPIRET2]", prompt)
        self.assertNotIn("MESSAGE [EXPORTING CHAR]", prompt)
        self.assertNotIn("Z_UNUSED_FUNCTION", prompt)

        self.assertIn("Exact internal-table names: t_edidc, t_edids", prompt)
        self.assertIn("Exact work-area names: st_edidc, st_edids", prompt)
        self.assertIn("Exact output names: type ty_output", prompt)
        self.assertIn("Allowed global variables for FORM chunks:", prompt)
        for name in ("t_edidc", "st_edidc", "t_edids", "st_edids", "t_output", "w_output", "p_idoc"):
            self.assertIn(name, prompt)
        self.assertNotIn("t_unused", prompt)
        self.assertNotIn("st_unused", prompt)
        self.assertNotIn("p_unused", prompt)

    def test_processing_plan_normalization_nests_loop_and_canonicalizes_steps(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "IDOC_STATUS", "type_or_like": "TYPE EDIDS-STATUS"},
                    {"name": "ERROR_MESSAGE", "type_or_like": "TYPE bapi_msg"},
                ]
            }
        )
        base_prompt = (
            "SAP callable signature catalogue:\n"
            "- BAPI_MESSAGE_GETDETAIL: ID [IMPORTING CHAR], NUMBER [IMPORTING NUMC], RETURN [EXPORTING BAPIRET2]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc, t_edids, t_unused\n"
            "Exact work-area names: st_edidc, st_edids, st_unused\n"
            "Exact callable identities: BAPI_MESSAGE_GETDETAIL\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
            "- EDIDS: structure st_edids, table t_edids, work area st_edids\n"
            "- ZUNUSED: structure st_unused, table t_unused, work area st_unused"
        )
        normalized = normalize_processing_plan(
            {
                "processing_steps": [
                    {"step": 1, "operation": "loop", "source": "t_edidc"},
                    {"step": 2, "operation": "READ", "source": "t_unused", "match": "st_unused-docnum = st_edidc-docnum"},
                    {"step": 3, "operation": "READ", "source": "t_edids", "match": "st_edids-docnum = st_edidc-docnum"},
                    {"step": 4, "operation": "MOVE", "source": "st_edids-status", "target": "IDOC_STATUS"},
                    {"step": 5, "operation": "IF", "condition": "1 = 0"},
                    {
                        "step": 6,
                        "operation": "CALL_FUNCTION",
                        "name": "BAPI_MESSAGE_GETDETAIL",
                        "parameters": {
                            "ID": "st_edids-stamid",
                            "NUMBER": "st_edids-stamno",
                            "RETURN": "st_edids-stapa1",
                        },
                    },
                    {"step": 7, "operation": "CLEAR", "target": "w_output"},
                    {"step": 8, "operation": "MOVE", "source": "st_edids-stapa1", "target": "ERROR_MESSAGE"},
                    {"step": 9, "operation": "APPEND", "source": "w_output", "target": "t_output"},
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
        )

        self.assertEqual(1, len(normalized["processing_steps"]))
        loop = normalized["processing_steps"][0]
        self.assertEqual("LOOP", loop["operation"])
        self.assertEqual("t_edidc", loop["source"])
        self.assertEqual("st_edidc", loop["into"])
        child_operations = [step["operation"] for step in loop["steps"]]
        self.assertEqual(["READ", "MOVE", "CALL_FUNCTION", "CLEAR", "MOVE", "APPEND"], child_operations)
        read = loop["steps"][0]
        self.assertEqual("t_edids", read["source"])
        self.assertEqual("st_edids", read["into"])
        self.assertEqual(
            [{"left": "st_edids-docnum", "operator": "=", "right": "st_edidc-docnum"}],
            read["conditions"],
        )
        self.assertEqual("w_output-idoc_status", loop["steps"][1]["target"])
        call = loop["steps"][2]
        self.assertEqual({"ID": "st_edids-stamid", "NUMBER": "st_edids-stamno"}, call["input_parameters"])
        self.assertEqual({}, call["output_parameters"])
        self.assertEqual("w_output-error_message", loop["steps"][4]["target"])
        self.assertNotIn("1 = 0", json.dumps(normalized))
        self.assertNotIn("t_unused", json.dumps(normalized))
        self.assertNotIn("st_unused", json.dumps(normalized))

    def test_processing_plan_normalization_removes_select_filters_from_read_conditions(self):
        declaration_requirements = json.dumps(
            {
                "select_options": [{"name": "s_status", "for_field": "ZDET-STATUS"}],
                "parameters": [{"name": "p_type", "type_or_like": "TYPE ZDET-TYPE"}],
                "output_structure_fields": [{"name": "DETAIL_ID", "type_or_like": "TYPE ZDET-DOCNUM"}],
            }
        )
        base_prompt = (
            "Shared generation contract:\n"
            "Exact internal-table names: t_hdr, t_det\n"
            "Exact work-area names: st_hdr, st_det\n"
            "- ZHDR: structure st_hdr, table t_hdr, work area st_hdr\n"
            "- ZDET: structure st_det, table t_det, work area st_det"
        )

        normalized = normalize_processing_plan(
            {
                "processing_steps": [
                    {
                        "operation": "LOOP",
                        "source": "t_hdr",
                        "into": "st_hdr",
                        "steps": [
                            {"operation": "CLEAR", "target": "st_det"},
                            {
                                "operation": "READ",
                                "source": "t_det",
                                "into": "st_det",
                                "conditions": [
                                    {"left": "st_det-DOCNUM", "operator": "=", "right": "st_hdr-DOCNUM"},
                                    {"left": "st_det-STATUS", "operator": "IN", "right": "s_status"},
                                    {"left": "st_det-TYPE", "operator": "=", "right": "p_type"},
                                    {"left": "st_det-FLAG", "operator": "=", "right": "'X'"},
                                ],
                            },
                            {"operation": "MOVE", "source": "st_det-DOCNUM", "target": "DETAIL_ID"},
                        ],
                    }
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
        )

        read = normalized["processing_steps"][0]["steps"][1]
        self.assertEqual(
            [{"left": "st_det-docnum", "operator": "=", "right": "st_hdr-docnum"}],
            read["conditions"],
        )
        serialized = json.dumps(normalized)
        self.assertNotIn("s_status", serialized)
        self.assertNotIn("p_type", serialized)
        self.assertNotIn("FLAG", serialized)

    def test_processing_plan_normalization_qualifies_bare_read_lookup_fields(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "PERNR", "type_or_like": "TYPE PA0002-PERNR"},
                    {"name": "VORNA", "type_or_like": "TYPE PA0002-VORNA"},
                    {"name": "NACHN", "type_or_like": "TYPE PA0002-NACHN"},
                ]
            }
        )
        base_prompt = (
            "Shared generation contract:\n"
            "Exact internal-table names: t_pa0000, t_pa0001, t_pa0002\n"
            "Exact work-area names: st_pa0000, st_pa0001, st_pa0002\n"
            "- PA0000: structure st_pa0000, table t_pa0000, work area st_pa0000\n"
            "- PA0001: structure st_pa0001, table t_pa0001, work area st_pa0001\n"
            "- PA0002: structure st_pa0002, table t_pa0002, work area st_pa0002"
        )

        normalized = normalize_processing_plan(
            {
                "processing_steps": [
                    {
                        "operation": "LOOP",
                        "source": "t_pa0000",
                        "into": "st_pa0000",
                        "steps": [
                            {
                                "operation": "READ",
                                "source": "t_pa0001",
                                "into": "st_pa0001",
                                "conditions": [{"left": "PERNR", "operator": "=", "right": "st_pa0000-PERNR"}],
                            },
                            {
                                "operation": "IF",
                                "conditions": [{"left": "st_pa0001", "operator": "IS NOT INITIAL"}],
                                "then": [
                                    {
                                        "operation": "READ",
                                        "source": "t_pa0002",
                                        "into": "st_pa0002",
                                        "conditions": [{"left": "PERNR", "operator": "=", "right": "st_pa0001-PERNR"}],
                                    },
                                    {"operation": "CLEAR", "target": "w_output"},
                                    {"operation": "MOVE", "source": "st_pa0002-PERNR", "target": "w_output-PERNR"},
                                    {"operation": "MOVE", "source": "st_pa0002-VORNA", "target": "w_output-VORNA"},
                                    {"operation": "MOVE", "source": "st_pa0002-NACHN", "target": "w_output-NACHN"},
                                    {"operation": "APPEND", "source": "w_output", "target": "t_output"},
                                ],
                                "else": [],
                            },
                        ],
                    }
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
        )

        loop = normalized["processing_steps"][0]
        self.assertEqual(["READ", "IF"], [step["operation"] for step in loop["steps"]])
        self.assertEqual(
            [{"left": "st_pa0001-pernr", "operator": "=", "right": "st_pa0000-pernr"}],
            loop["steps"][0]["conditions"],
        )
        nested_read = loop["steps"][1]["then"][0]
        self.assertEqual("READ", nested_read["operation"])
        self.assertEqual(
            [{"left": "st_pa0002-pernr", "operator": "=", "right": "st_pa0001-pernr"}],
            nested_read["conditions"],
        )

    def test_processing_generation_does_not_repeat_select_predicates_after_read_table(self):
        processing_prompts = []
        raw_plan = {
            "processing_steps": [
                {
                    "operation": "LOOP",
                    "source": "t_hdr",
                    "into": "st_hdr",
                    "steps": [
                        {"operation": "CLEAR", "target": "st_det"},
                        {
                            "operation": "READ",
                            "source": "t_det",
                            "into": "st_det",
                            "conditions": [
                                {"left": "st_det-DOCNUM", "operator": "=", "right": "st_hdr-DOCNUM"},
                                {"left": "st_det-STATUS", "operator": "IN", "right": "s_status"},
                                {"left": "st_det-TYPE", "operator": "=", "right": "'A'"},
                            ],
                        },
                        {"operation": "CLEAR", "target": "w_output"},
                        {"operation": "MOVE", "source": "st_det-DOCNUM", "target": "DETAIL_ID"},
                        {"operation": "APPEND", "source": "w_output", "target": "t_output"},
                    ],
                }
            ]
        }
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHDR: DOCNUM [CHAR]\n"
            "- ZDET: DOCNUM [CHAR], STATUS [CHAR], TYPE [CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_hdr, t_det\n"
            "Exact work-area names: st_hdr, st_det\n"
            "Exact output structure fields: DETAIL_ID\n"
            "Exact FORM names: read_data, process_data, output_data\n"
            "Exact callable identities: none\n"
            "- ZHDR: structure st_hdr, table t_hdr, work area st_hdr\n"
            "- ZDET: structure st_det, table t_det, work area st_det"
        )

        def generator(prompt_text, _source_text):
            if "Extract business-processing logic" in prompt_text:
                return {"text": json.dumps(raw_plan), "model": "test-model", "usage": None}
            if "Chunk: declarations" in prompt_text:
                return {"text": "REPORT ztest.", "model": "test-model", "usage": None}
            if "Chunk: database_read_forms" in prompt_text:
                return {"text": "FORM read_data.\nENDFORM.", "model": "test-model", "usage": None}
            if "Chunk: processing_form" in prompt_text:
                processing_prompts.append(prompt_text)
                self.assertNotIn("s_status", prompt_text)
                self.assertNotIn('"right": "\'A\'"', prompt_text)
                return {
                    "text": (
                        "FORM process_data.\n"
                        "  READ TABLE t_det INTO st_det WITH KEY docnum = st_hdr-docnum.\n"
                        "  CLEAR w_output.\n"
                        "  MOVE st_det-docnum TO w_output-detail_id.\n"
                        "  APPEND w_output TO t_output.\n"
                        "ENDFORM."
                    ),
                    "model": "test-model",
                    "usage": None,
                }
            if "Chunk: main_program_flow" in prompt_text:
                return {"text": "START-OF-SELECTION.\n  PERFORM read_data.\n  PERFORM process_data.", "model": "test-model", "usage": None}
            return {"text": "", "model": "test-model", "usage": None}

        result = generate_chunked_abap_program(
            base_prompt,
            "Select ZDET rows with STATUS in s_status and TYPE = 'A', then read by DOCNUM.",
            abap_generator=generator,
            declaration_requirements={
                "requirements": {
                    "report_name": "ztest",
                    "select_options": [{"name": "s_status", "for_field": "ZDET-STATUS"}],
                    "output_structure_fields": [{"name": "DETAIL_ID", "type_or_like": "TYPE ZDET-DOCNUM"}],
                }
            },
        )

        self.assertEqual(1, len(processing_prompts))
        processing_plan = result["processing_plan"]["plan"]
        read = processing_plan["processing_steps"][0]["steps"][1]
        self.assertEqual(
            [{"left": "st_det-docnum", "operator": "=", "right": "st_hdr-docnum"}],
            read["conditions"],
        )
        self.assertIn("WITH KEY docnum = st_hdr-docnum", result["text"])
        self.assertNotIn("s_status", result["text"])
        self.assertNotIn("TYPE = 'A'", result["text"].upper())

    def test_processing_plan_normalization_preserves_valid_nested_loop_children(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "MPE_ID", "type_or_like": "TYPE ZMD_MPE0001-IDENTIFIER"},
                    {"name": "IDOC_STATUS", "type_or_like": "TYPE EDIDS-STATUS"},
                    {"name": "ERROR_MESSAGE", "type_or_like": "TYPE BAPIRET2"},
                    {"name": "IDOC_NUMBER", "type_or_like": "TYPE EDIDC-DOCNUM"},
                ]
            }
        )
        base_prompt = (
            "SAP callable signature catalogue:\n"
            "- BAPI_MESSAGE_GETDETAIL: ID [IMPORTING BAPIRET2], NUMBER [IMPORTING BAPIRET2], RETURN [EXPORTING BAPIRET2]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc, t_edids, t_zmd_mpe0001\n"
            "Exact work-area names: st_edidc, st_edids, st_zmd_mpe0001\n"
            "Exact callable identities: BAPI_MESSAGE_GETDETAIL\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
            "- EDIDS: structure st_edids, table t_edids, work area st_edids\n"
            "- ZMD_MPE0001: structure st_zmd_mpe0001, table t_zmd_mpe0001, work area st_zmd_mpe0001"
        )

        result = normalize_processing_plan_with_diagnostics(
            {
                "processing_steps": [
                    {
                        "step": 1,
                        "operation": "LOOP",
                        "source": "t_edidc",
                        "into": "st_edidc",
                        "steps": [
                            {
                                "step": 2,
                                "operation": "READ",
                                "source": "t_edids",
                                "into": "st_edids",
                                "conditions": [
                                    {"left": "st_edids-DOCNUM", "operator": "=", "right": "st_edidc-DOCNUM"}
                                ],
                            },
                            {
                                "step": 3,
                                "operation": "IF",
                                "conditions": [
                                    {"left": "st_edids-STATUS", "operator": "<>", "right": "''"}
                                ],
                                "then": [
                                    {"step": 4, "operation": "CLEAR", "target": "w_output"},
                                    {"step": 5, "operation": "MOVE", "source": "st_edids-STATUS", "target": "IDOC_STATUS"},
                                    {
                                        "step": 6,
                                        "operation": "CALL_FUNCTION",
                                        "name": "BAPI_MESSAGE_GETDETAIL",
                                        "input_parameters": {
                                            "ID": "st_edids-STAMID",
                                            "NUMBER": "st_edids-STAMNO",
                                        },
                                        "output_parameters": {"RETURN": "w_output-ERROR_MESSAGE"},
                                    },
                                    {"step": 7, "operation": "APPEND", "source": "w_output", "target": "t_output"},
                                ],
                                "else": [
                                    {"step": 8, "operation": "BOGUS", "source": "should_be_rejected"},
                                    {"step": 9, "operation": "MOVE", "source": "st_edidc-DOCNUM", "target": "IDOC_NUMBER"},
                                ],
                            },
                        ],
                    }
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
        )

        plan = result["plan"]
        loop = plan["processing_steps"][0]
        self.assertEqual("LOOP", loop["operation"])
        self.assertEqual(["READ", "IF"], [step["operation"] for step in loop["steps"]])
        if_step = loop["steps"][1]
        self.assertEqual(
            [{"left": "st_edids-status", "operator": "<>", "right": "''"}],
            if_step["conditions"],
        )
        self.assertEqual(["CLEAR", "MOVE", "CALL_FUNCTION", "APPEND"], [step["operation"] for step in if_step["then"]])
        self.assertEqual(["MOVE"], [step["operation"] for step in if_step["else"]])
        self.assertEqual("w_output-idoc_status", if_step["then"][1]["target"])
        self.assertEqual("w_output-idoc_number", if_step["else"][0]["target"])
        self.assertEqual({"ID": "st_edids-stamid", "NUMBER": "st_edids-stamno"}, if_step["then"][2]["input_parameters"])
        self.assertEqual({"RETURN": "w_output-error_message"}, if_step["then"][2]["output_parameters"])
        rejected = result["diagnostics"]["rejected_steps"]
        self.assertEqual(1, len([item for item in rejected if item["reason"] == "unsupported or missing operation"]))
        self.assertTrue(any(item["step"].get("operation") == "BOGUS" for item in rejected if isinstance(item.get("step"), dict)))

    def test_processing_plan_normalization_accepts_contains_error_if_and_preserves_email_branch(self):
        base_prompt = (
            "SAP callable signature catalogue:\n"
            "- BAPI_EMPLOYEE_ENQUEUE: NUMBER [IMPORTING NUMC], RETURN [EXPORTING BAPIRET2]\n"
            "- CL_BCS=>CREATE_PERSISTENT: RETURNING [RETURNING REF]\n"
            "- SEND_REQUEST=>SEND: I_WITH_ERROR_SCREEN [IMPORTING FLAG], RETURNING [RETURNING FLAG]\n"
            "Shared generation contract:\n"
            "Exact callable identities: BAPI_EMPLOYEE_ENQUEUE, CL_BCS=>CREATE_PERSISTENT, SEND_REQUEST=>SEND"
        )

        result = normalize_processing_plan_with_diagnostics(
            {
                "processing_steps": [
                    {
                        "operation": "LOOP",
                        "source": "t_output",
                        "into": "w_output",
                        "steps": [
                            {
                                "operation": "CALL_FUNCTION",
                                "name": "BAPI_EMPLOYEE_ENQUEUE",
                                "input_parameters": {"NUMBER": "w_output-PERNR"},
                                "output_parameters": {"RETURN": "BAPI_ENQUEUE_RETURN"},
                            },
                            {
                                "operation": "IF",
                                "conditions": [
                                    {
                                        "left": "BAPI_ENQUEUE_RETURN",
                                        "operator": "CONTAINS ERROR",
                                        "right": "",
                                    }
                                ],
                                "then": [],
                                "else": [
                                    {
                                        "operation": "CALL_STATIC_METHOD",
                                        "class": "CL_BCS",
                                        "method": "CREATE_PERSISTENT",
                                        "receiving_parameter": "SEND_REQUEST",
                                    },
                                    {
                                        "operation": "CALL_METHOD",
                                        "object": "SEND_REQUEST",
                                        "method": "SEND",
                                        "input_parameters": {"I_WITH_ERROR_SCREEN": " "},
                                        "receiving_parameter": "SEND_RESULT",
                                    },
                                ],
                            },
                        ],
                    }
                ]
            },
            base_prompt=base_prompt,
        )

        loop = result["plan"]["processing_steps"][0]
        enqueue_guard = loop["steps"][1]

        self.assertEqual(
            [{"left": "bapi_enqueue_return", "operator": "CONTAINS ERROR"}],
            enqueue_guard["conditions"],
        )
        self.assertEqual(["CALL_STATIC_METHOD", "CALL_METHOD"], [step["operation"] for step in enqueue_guard["else"]])
        self.assertEqual("CL_BCS=>CREATE_PERSISTENT", enqueue_guard["else"][0]["name"])
        self.assertEqual("SEND_REQUEST=>SEND", enqueue_guard["else"][1]["name"])
        self.assertFalse(
            [
                item
                for item in result["diagnostics"]["rejected_steps"]
                if item.get("reason") == "IF step is missing a valid condition"
            ]
        )

    def test_processing_plan_normalization_accepts_operation_keyed_steps(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "MPE_ID", "type_or_like": "TYPE ZMD_MPE0001-IDENTIFIER"},
                    {"name": "IDOC_STATUS", "type_or_like": "TYPE EDIDC-STATUS"},
                    {"name": "ERROR_MESSAGE", "type_or_like": "TYPE BAPIRET2"},
                    {"name": "IDOC_NUMBER", "type_or_like": "TYPE EDIDC-DOCNUM"},
                ]
            }
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM [NUMC], STATUS [CHAR]\n"
            "- EDIDS: DOCNUM [NUMC], STAMID [CHAR], STAMNO [NUMC], STAPA1 [CHAR], STAPA2 [CHAR], STAPA3 [CHAR], STAPA4 [CHAR]\n"
            "- ZMD_MPE0001: DOCNUM_IN [NUMC], IDENTIFIER [CHAR]\n"
            "SAP callable signature catalogue:\n"
            "- BAPI_MESSAGE_GETDETAIL: ID [IMPORTING BAPIRET2], NUMBER [IMPORTING BAPIRET2], MESSAGE_V1 [IMPORTING BAPIRET2], RETURN [EXPORTING BAPIRET2]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc, t_edids, t_zmd_mpe0001\n"
            "Exact work-area names: st_edidc, st_edids, st_zmd_mpe0001\n"
            "Exact callable identities: BAPI_MESSAGE_GETDETAIL\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
            "- EDIDS: structure st_edids, table t_edids, work area st_edids\n"
            "- ZMD_MPE0001: structure st_zmd_mpe0001, table t_zmd_mpe0001, work area st_zmd_mpe0001"
        )
        canonical_plan = {
            "processing_steps": [
                {
                    "operation": "LOOP",
                    "source": "t_edidc",
                    "into": "st_edidc",
                    "steps": [
                        {
                            "operation": "READ",
                            "source": "t_zmd_mpe0001",
                            "into": "st_zmd_mpe0001",
                            "conditions": [
                                {"left": "st_zmd_mpe0001-DOCNUM_IN", "operator": "=", "right": "st_edidc-DOCNUM"}
                            ],
                        },
                        {
                            "operation": "IF",
                            "conditions": [
                                {"left": "st_zmd_mpe0001-IDENTIFIER", "operator": "NE", "right": ""}
                            ],
                            "then": [
                                {"operation": "CLEAR", "target": "w_output"},
                                {
                                    "operation": "CALL_FUNCTION",
                                    "name": "BAPI_MESSAGE_GETDETAIL",
                                    "input_parameters": {
                                        "ID": "st_edids-STAMID",
                                        "NUMBER": "st_edids-STAMNO",
                                        "MESSAGE_V1": "st_edids-STAPA1",
                                    },
                                    "output_parameters": {"RETURN": "w_output-ERROR_MESSAGE"},
                                },
                                {"operation": "MOVE", "source": "st_zmd_mpe0001-IDENTIFIER", "target": "MPE_ID"},
                                {"operation": "MOVE", "source": "st_edidc-STATUS", "target": "IDOC_STATUS"},
                                {
                                    "operation": "IF",
                                    "conditions": [{"left": "p_idoc", "operator": "=", "right": "X"}],
                                    "then": [
                                        {"operation": "MOVE", "source": "st_edidc-DOCNUM", "target": "IDOC_NUMBER"}
                                    ],
                                    "else": [],
                                },
                                {"operation": "APPEND", "source": "w_output", "target": "t_output"},
                                {"operation": "SORT", "source": "t_output"},
                                {"operation": "DELETE", "source": "t_output", "condition": "duplicate rows"},
                                {"operation": "CONCATENATE", "source": "w_output-MPE_ID", "target": "w_output-ERROR_MESSAGE"},
                            ],
                            "else": [],
                        },
                    ],
                }
            ]
        }
        operation_keyed_plan = {
            "processing_steps": [
                {
                    "LOOP": {
                        "source": "t_edidc",
                        "into": "st_edidc",
                        "steps": [
                            {
                                "READ": {
                                    "source": "t_zmd_mpe0001",
                                    "into": "st_zmd_mpe0001",
                                    "conditions": {
                                        "left": "st_zmd_mpe0001-DOCNUM_IN",
                                        "operator": "=",
                                        "right": "st_edidc-DOCNUM",
                                    },
                                }
                            },
                            {
                                "IF": {
                                    "conditions": {
                                        "left": "st_zmd_mpe0001-IDENTIFIER",
                                        "operator": "NE",
                                        "right": "",
                                    },
                                    "then": [
                                        {"CLEAR": {"target": "w_output"}},
                                        {
                                            "CALL_FUNCTION": {
                                                "name": "BAPI_MESSAGE_GETDETAIL",
                                                "input_parameters": {
                                                    "ID": "st_edids-STAMID",
                                                    "NUMBER": "st_edids-STAMNO",
                                                    "MESSAGE_V1": "st_edids-STAPA1",
                                                },
                                                "output_parameters": {"RETURN": "w_output-ERROR_MESSAGE"},
                                            }
                                        },
                                        {"MOVE": {"source": "st_zmd_mpe0001-IDENTIFIER", "target": "MPE_ID"}},
                                        {"MOVE": {"source": "st_edidc-STATUS", "target": "IDOC_STATUS"}},
                                        {
                                            "IF": {
                                                "conditions": {"left": "p_idoc", "operator": "=", "right": "X"},
                                                "then": [
                                                    {"MOVE": {"source": "st_edidc-DOCNUM", "target": "IDOC_NUMBER"}}
                                                ],
                                                "else": [],
                                            }
                                        },
                                        {"APPEND": {"source": "w_output", "target": "t_output"}},
                                        {"SORT": {"source": "t_output"}},
                                        {"DELETE": {"source": "t_output", "condition": "duplicate rows"}},
                                        {"CONCATENATE": {"source": "w_output-MPE_ID", "target": "w_output-ERROR_MESSAGE"}},
                                    ],
                                    "else": [],
                                }
                            },
                        ],
                    }
                }
            ]
        }

        canonical = normalize_processing_plan_with_diagnostics(
            canonical_plan,
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
        )
        operation_keyed = normalize_processing_plan_with_diagnostics(
            operation_keyed_plan,
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
        )

        self.assertEqual(canonical["plan"], operation_keyed["plan"])
        self.assertFalse(
            [
                item
                for item in operation_keyed["diagnostics"]["rejected_steps"]
                if item["reason"] == "unsupported or missing operation"
            ]
        )

    def test_processing_plan_normalization_preserves_numeric_keyed_nested_steps(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "IDOC_STATUS", "type_or_like": "TYPE EDIDS-STATUS"},
                    {"name": "IDOC_NUMBER", "type_or_like": "TYPE EDIDC-DOCNUM"},
                ]
            }
        )
        base_prompt = (
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc, t_edids\n"
            "Exact work-area names: st_edidc, st_edids\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
            "- EDIDS: structure st_edids, table t_edids, work area st_edids"
        )

        result = normalize_processing_plan_with_diagnostics(
            {
                "processing_steps": {
                    "1": {
                        "step": 1,
                        "operation": "LOOP",
                        "source": "t_edidc",
                        "into": "st_edidc",
                        "steps": {
                            "2": {
                                "step": 2,
                                "operation": "READ",
                                "source": "t_edids",
                                "into": "st_edids",
                                "conditions": [
                                    {"left": "st_edids-docnum", "operator": "=", "right": "st_edidc-docnum"}
                                ],
                            },
                            "3": {
                                "step": 3,
                                "operation": "IF",
                                "conditions": [
                                    {"left": "st_edids-status", "operator": "<>", "right": "''"}
                                ],
                                "then": {
                                    "4": {"step": 4, "operation": "MOVE", "source": "st_edids-status", "target": "IDOC_STATUS"}
                                },
                                "else": {
                                    "5": {"step": 5, "operation": "MOVE", "source": "st_edidc-docnum", "target": "IDOC_NUMBER"}
                                },
                            },
                        },
                    }
                }
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
        )

        loop = result["plan"]["processing_steps"][0]
        self.assertEqual(["READ", "IF"], [step["operation"] for step in loop["steps"]])
        if_step = loop["steps"][1]
        self.assertEqual(["MOVE"], [step["operation"] for step in if_step["then"]])
        self.assertEqual(["MOVE"], [step["operation"] for step in if_step["else"]])
        self.assertFalse(
            any(
                "contained child steps but none remained" in item.get("reason", "")
                for item in result["diagnostics"]["rejected_steps"]
            )
        )

    def test_processing_plan_normalization_preserves_then_steps_and_unary_conditions(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "IDENTIFIER", "type_or_like": "TYPE ZMD_MPE0001-IDENTIFIER"},
                    {"name": "IDOC_STATUS", "type_or_like": "TYPE EDIDC-STATUS"},
                    {"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"},
                ]
            }
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM [NUMC], STATUS [CHAR]\n"
            "- ZMD_MPE0001: DOCNUM_IN [NUMC], IDENTIFIER [CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc, t_zmd_mpe0001\n"
            "Exact work-area names: st_edidc, st_zmd_mpe0001\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
            "- ZMD_MPE0001: structure st_zmd_mpe0001, table t_zmd_mpe0001, work area st_zmd_mpe0001"
        )

        normalized = normalize_processing_plan_with_diagnostics(
            {
                "processing_steps": [
                    {
                        "step": 1,
                        "operation": "LOOP",
                        "source": "t_edidc",
                        "into": "st_edidc",
                        "steps": [
                            {"step": 2, "operation": "CLEAR", "target": "st_zmd_mpe0001"},
                            {
                                "step": 3,
                                "operation": "READ",
                                "source": "t_zmd_mpe0001",
                                "into": "st_zmd_mpe0001",
                                "conditions": [
                                    {"left": "st_zmd_mpe0001-DOCNUM_IN", "operator": "=", "right": "st_edidc-DOCNUM"}
                                ],
                            },
                            {
                                "step": 4,
                                "operation": "IF",
                                "condition": {
                                    "left": "st_zmd_mpe0001-IDENTIFIER",
                                    "operator": "IS NOT INITIAL",
                                    "right": "",
                                },
                                "then_steps": [
                                    {"step": 5, "operation": "CLEAR", "target": "w_output"},
                                    {"step": 6, "operation": "MOVE", "source": "st_zmd_mpe0001-IDENTIFIER", "target": "IDENTIFIER"},
                                    {"step": 7, "operation": "MOVE", "source": "st_edidc-STATUS", "target": "IDOC_STATUS"},
                                    {
                                        "step": 8,
                                        "operation": "IF",
                                        "condition": {"left": "p_idoc", "operator": "=", "right": "X"},
                                        "then_steps": [
                                            {"step": 9, "operation": "MOVE", "source": "st_edidc-DOCNUM", "target": "DOCNUM"}
                                        ],
                                        "else_steps": [],
                                    },
                                    {"step": 10, "operation": "APPEND", "source": "w_output", "target": "t_output"},
                                ],
                                "else_steps": [],
                            },
                        ],
                    }
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
        )
        validation = validate_processing_plan(
            normalized["plan"],
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
            normalization_diagnostics=normalized["diagnostics"],
        )

        loop = normalized["plan"]["processing_steps"][0]
        self.assertEqual(["CLEAR", "READ", "IF"], [step["operation"] for step in loop["steps"]])
        if_step = loop["steps"][2]
        self.assertEqual([{"left": "st_zmd_mpe0001-identifier", "operator": "IS NOT INITIAL"}], if_step["conditions"])
        self.assertEqual(["CLEAR", "MOVE", "MOVE", "IF", "APPEND"], [step["operation"] for step in if_step["then"]])
        self.assertTrue(validation["valid"], validation["errors"])

    def test_processing_plan_validation_reports_contract_and_metadata_errors(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "MSG", "type_or_like": "TYPE ZMSG-TEXT"},
                    {"name": "KEY", "type_or_like": "TYPE ZHDR-KEY"},
                ]
            }
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHDR: KEY [CHAR]\n"
            "- ZMSG: TEXT [CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_hdr\n"
            "Exact work-area names: st_hdr\n"
            "Exact callable identities: Z_LOOKUP\n"
            "- ZHDR: structure st_hdr, table t_hdr, work area st_hdr"
        )
        callable_metadata = {
            "callable_signatures": {
                "Z_LOOKUP": {
                    "parameters": {
                        "IV_KEY": {"direction": "IMPORTING", "abap_type": "ZHDR", "field": "KEY"},
                        "EV_TEXT": {"direction": "EXPORTING", "abap_type": "ZMSG", "field": "TEXT"},
                    }
                }
            }
        }

        result = validate_processing_plan(
            {
                "processing_steps": [
                    {"step": 1, "operation": "LOOP", "source": "t_missing", "into": "st_hdr", "steps": [
                        {"step": 2, "operation": "MOVE", "source": "lv_shadow", "target": "w_output-UNKNOWN"},
                        {
                            "step": 3,
                            "operation": "CALL_FUNCTION",
                            "name": "Z_LOOKUP",
                            "input_parameters": {"BAD_PARAM": "st_hdr-key"},
                            "output_parameters": {"IV_KEY": "w_output-msg", "EV_TEXT": "w_output-key"},
                        },
                    ]},
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
            callable_metadata=callable_metadata,
        )

        errors = "\n".join(result["errors"])
        self.assertFalse(result["valid"])
        self.assertIn("table t_missing not found in generation contract", errors)
        self.assertIn("undeclared global object lv_shadow", errors)
        self.assertIn("output field UNKNOWN not found in output structure contract", errors)
        self.assertIn("parameter BAD_PARAM is not in verified metadata", errors)
        self.assertIn("parameter IV_KEY is mapped as output", errors)
        self.assertIn("not compatible with Z_LOOKUP parameter EV_TEXT", errors)
        self.assertIn("required output creation step CLEAR w_output is missing", errors)
        self.assertIn("required output append step APPEND w_output TO t_output is missing", errors)

    def test_processing_plan_validation_requires_each_output_field_population_path(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "CUSTOMER", "type_or_like": "TYPE ZS505-KUNNR"},
                    {"name": "SITE", "type_or_like": "TYPE ZS505-WERKS"},
                    {"name": "DOCUMENT_COUNT", "type_or_like": "TYPE i"},
                    {"name": "SALES_TOTAL", "type_or_like": "TYPE p DECIMALS 2"},
                    {"name": "MARGIN_PERCENT", "type_or_like": "TYPE p DECIMALS 2"},
                ]
            }
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZS505: KUNNR [CHAR], WERKS [CHAR], VBELN [CHAR], KZWI2 [CURR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_zs505\n"
            "Exact work-area names: st_zs505\n"
            "- ZS505: structure st_zs505, table t_zs505, work area st_zs505"
        )

        result = validate_processing_plan(
            {
                "processing_steps": [
                    {
                        "operation": "LOOP",
                        "source": "t_zs505",
                        "into": "st_zs505",
                        "steps": [
                            {"operation": "CLEAR", "target": "w_output"},
                            {"operation": "MOVE", "source": "st_zs505-KUNNR", "target": "w_output-CUSTOMER"},
                            {"operation": "MOVE", "source": "st_zs505-WERKS", "target": "w_output-SITE"},
                            {"operation": "APPEND", "source": "w_output", "target": "t_output"},
                        ],
                    }
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
        )

        errors = "\n".join(result["errors"])
        self.assertFalse(result["valid"])
        self.assertIn("required output field w_output-DOCUMENT_COUNT has no concrete processing step", errors)
        self.assertIn("required output field w_output-SALES_TOTAL has no concrete processing step", errors)
        self.assertIn("required output field w_output-MARGIN_PERCENT has no concrete processing step", errors)

    def test_generated_processing_completeness_flags_copy_only_logic_for_aggregated_outputs(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "CUSTOMER", "type_or_like": "TYPE ZS505-KUNNR"},
                    {"name": "SITE", "type_or_like": "TYPE ZS505-WERKS"},
                    {"name": "DOCUMENT_COUNT", "type_or_like": "TYPE i"},
                    {"name": "SALES_TOTAL", "type_or_like": "TYPE p DECIMALS 2"},
                    {"name": "MARGIN_PERCENT", "type_or_like": "TYPE p DECIMALS 2"},
                ]
            }
        )
        processing_plan = {
            "processing_steps": [
                {
                    "operation": "AGGREGATE",
                    "source": "t_zs505",
                    "target": "w_output-SALES_TOTAL",
                    "function": "SUM",
                    "group_by": ["st_zs505-KUNNR", "st_zs505-WERKS"],
                    "sources": ["st_zs505-KZWI2"],
                },
                {
                    "operation": "COUNT",
                    "source": "t_zs505",
                    "target": "w_output-DOCUMENT_COUNT",
                    "group_by": ["st_zs505-KUNNR", "st_zs505-WERKS"],
                    "distinct": "st_zs505-VBELN",
                },
                {
                    "operation": "PERCENTAGE",
                    "target": "w_output-MARGIN_PERCENT",
                    "numerator": "w_output-SALES_TOTAL",
                    "denominator": "w_output-DOCUMENT_COUNT",
                    "group_by": ["st_zs505-KUNNR", "st_zs505-WERKS"],
                },
            ]
        }
        source = (
            "FORM process_data.\n"
            "  LOOP AT t_zs505 INTO st_zs505.\n"
            "    CLEAR w_output.\n"
            "    w_output-customer = st_zs505-kunnr.\n"
            "    w_output-site = st_zs505-werks.\n"
            "    APPEND w_output TO t_output.\n"
            "  ENDLOOP.\n"
            "ENDFORM.\n"
        )

        issues = validate_generated_processing_completeness(
            source,
            source_text="Aggregate by customer and site, count documents, total sales and calculate margin percentage.",
            processing_plan=processing_plan,
            declaration_requirements=declaration_requirements,
        )
        rule_ids = [issue["rule_id"] for issue in issues]

        self.assertIn("PROCESSING_OUTPUT_FIELD_NOT_POPULATED", rule_ids)
        self.assertIn("PROCESSING_AGGREGATION_NOT_IMPLEMENTED", rule_ids)
        self.assertIn("PROCESSING_CALCULATION_NOT_IMPLEMENTED", rule_ids)
        self.assertIn("DOCUMENT_COUNT", [issue.get("field") for issue in issues])
        self.assertIn("SALES_TOTAL", [issue.get("field") for issue in issues])
        self.assertIn("MARGIN_PERCENT", [issue.get("field") for issue in issues])

    def test_processing_plan_validation_rejects_callable_input_from_output_record(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "MSG", "type_or_like": "TYPE ZTXT-TEXT"},
                ],
                "global_variables": [
                    {"name": "lv_key", "declaration": "DATA lv_key TYPE ZHDR-KEY."},
                ],
            }
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHDR: KEY [CHAR]\n"
            "- ZTXT: TEXT [CHAR]\n"
            "Shared generation contract:\n"
            "Exact callable identities: Z_LOOKUP\n"
            "- ZHDR: structure st_hdr, table t_hdr, work area st_hdr"
        )
        callable_metadata = {
            "callable_signatures": {
                "Z_LOOKUP": {
                    "parameters": {
                        "IV_KEY": {"direction": "IMPORTING", "abap_type": "ZHDR", "field": "KEY"},
                        "EV_TEXT": {"direction": "EXPORTING", "abap_type": "ZTXT", "field": "TEXT"},
                    }
                }
            }
        }

        result = validate_processing_plan(
            {
                "processing_steps": [
                    {
                        "operation": "CALL_FUNCTION",
                        "name": "Z_LOOKUP",
                        "input_parameters": {"IV_KEY": "w_output-MSG"},
                        "output_parameters": {"EV_TEXT": "w_output-MSG"},
                    }
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
            callable_metadata=callable_metadata,
        )

        errors = "\n".join(result["errors"])
        self.assertFalse(result["valid"])
        self.assertIn("processing_plan.processing_steps.0.input_parameters.IV_KEY", errors)
        self.assertIn("maps from output record component w_output-MSG", errors)

    def test_processing_plan_validation_requires_callable_input_from_contract_field_or_declared_variable(self):
        declaration_requirements = json.dumps(
            {
                "global_variables": [
                    {"name": "lv_key", "declaration": "DATA lv_key TYPE ZHDR-KEY."},
                ]
            }
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHDR: KEY [CHAR]\n"
            "Shared generation contract:\n"
            "Exact callable identities: Z_LOOKUP\n"
            "- ZHDR: structure st_hdr, table t_hdr, work area st_hdr"
        )
        callable_metadata = {
            "callable_signatures": {
                "Z_LOOKUP": {
                    "parameters": {
                        "IV_KEY": {"direction": "IMPORTING", "abap_type": "ZHDR", "field": "KEY"},
                    }
                }
            }
        }

        valid_from_field = validate_processing_plan(
            {
                "processing_steps": [
                    {
                        "operation": "CALL_FUNCTION",
                        "name": "Z_LOOKUP",
                        "input_parameters": {"IV_KEY": "st_hdr-KEY"},
                    }
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
            callable_metadata=callable_metadata,
        )
        valid_from_variable = validate_processing_plan(
            {
                "processing_steps": [
                    {
                        "operation": "CALL_FUNCTION",
                        "name": "Z_LOOKUP",
                        "input_parameters": {"IV_KEY": "lv_key"},
                    }
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
            callable_metadata=callable_metadata,
        )
        invalid = validate_processing_plan(
            {
                "processing_steps": [
                    {
                        "operation": "CALL_FUNCTION",
                        "name": "Z_LOOKUP",
                        "input_parameters": {"IV_KEY": "lv_shadow"},
                    }
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
            callable_metadata=callable_metadata,
        )

        self.assertTrue(valid_from_field["valid"], valid_from_field["errors"])
        self.assertTrue(valid_from_variable["valid"], valid_from_variable["errors"])
        self.assertFalse(invalid["valid"])
        self.assertIn(
            "processing_plan.processing_steps.0.input_parameters.IV_KEY CALL_FUNCTION input parameter must map directly",
            "\n".join(invalid["errors"]),
        )

    def test_processing_plan_validation_allows_declared_processing_variable(self):
        declaration_requirements = json.dumps(
            {
                "processing_variables": [
                    {
                        "name": "lv_resolved_identifier",
                        "declaration": "DATA lv_resolved_identifier TYPE ZHDR-KEY.",
                        "source_fields": ["ZHDR-KEY", "ZALT-KEY"],
                    }
                ]
            }
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHDR: KEY [CHAR]\n"
            "Shared generation contract:\n"
            "Exact callable identities: Z_LOOKUP\n"
            "- ZHDR: structure st_hdr, table t_hdr, work area st_hdr"
        )
        callable_metadata = {
            "callable_signatures": {
                "Z_LOOKUP": {
                    "parameters": {
                        "IV_KEY": {"direction": "IMPORTING", "abap_type": "ZHDR", "field": "KEY"},
                    }
                }
            }
        }

        result = validate_processing_plan(
            {
                "processing_steps": [
                    {
                        "operation": "CALL_FUNCTION",
                        "name": "Z_LOOKUP",
                        "input_parameters": {"IV_KEY": "lv_resolved_identifier"},
                    }
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=declaration_requirements,
            callable_metadata=callable_metadata,
        )

        self.assertTrue(result["valid"], result["errors"])

    def test_processing_plan_validation_rejects_move_between_ddic_work_areas(self):
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHDR: KEY [CHAR]\n"
            "- ZTXT: TEXT [CHAR]\n"
            "Shared generation contract:\n"
            "- ZHDR: structure st_hdr, table t_hdr, work area st_hdr\n"
            "- ZTXT: structure st_txt, table t_txt, work area st_txt"
        )

        result = validate_processing_plan(
            {
                "processing_steps": [
                    {"operation": "MOVE", "source": "st_hdr-KEY", "target": "st_txt-TEXT"}
                ]
            },
            base_prompt=base_prompt,
        )

        errors = "\n".join(result["errors"])
        self.assertFalse(result["valid"])
        self.assertIn("processing_plan.processing_steps.0.target", errors)
        self.assertIn("uses DDIC work area st_txt as temporary storage", errors)

    def test_processing_plan_validation_requires_read_clear_or_success_check(self):
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHDR: KEY [CHAR]\n"
            "Shared generation contract:\n"
            "- ZHDR: structure st_hdr, table t_hdr, work area st_hdr"
        )
        unsafe = validate_processing_plan(
            {
                "processing_steps": [
                    {
                        "operation": "READ",
                        "source": "t_hdr",
                        "into": "st_hdr",
                        "conditions": [{"left": "st_hdr-KEY", "operator": "=", "right": "'A'"}],
                    }
                ]
            },
            base_prompt=base_prompt,
        )
        cleared = validate_processing_plan(
            {
                "processing_steps": [
                    {"operation": "CLEAR", "target": "st_hdr"},
                    {
                        "operation": "READ",
                        "source": "t_hdr",
                        "into": "st_hdr",
                        "conditions": [{"left": "st_hdr-KEY", "operator": "=", "right": "'A'"}],
                    },
                ]
            },
            base_prompt=base_prompt,
        )
        checked = validate_processing_plan(
            {
                "processing_steps": [
                    {
                        "operation": "READ",
                        "source": "t_hdr",
                        "into": "st_hdr",
                        "conditions": [{"left": "st_hdr-KEY", "operator": "=", "right": "'A'"}],
                    },
                    {
                        "operation": "IF",
                        "conditions": [{"left": "sy-subrc", "operator": "=", "right": "0"}],
                        "then": [{"operation": "MOVE", "source": "st_hdr-KEY", "target": "lv_key"}],
                        "else": [],
                    },
                ]
            },
            base_prompt=base_prompt,
            declaration_requirements=json.dumps(
                {"global_variables": [{"name": "lv_key", "declaration": "DATA lv_key TYPE ZHDR-KEY."}]}
            ),
        )

        self.assertFalse(unsafe["valid"])
        self.assertIn(
            "processing_plan.processing_steps.0 READ result work area st_hdr is not safely tested",
            "\n".join(unsafe["errors"]),
        )
        self.assertTrue(cleared["valid"], cleared["errors"])
        self.assertTrue(checked["valid"], checked["errors"])

    def test_processing_plan_extraction_does_not_retry_semantic_validation_errors(self):
        declaration_requirements = json.dumps(
            {"output_structure_fields": [{"name": "MSG", "type_or_like": "TYPE ZMSG-TEXT"}]}
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- ZSRC: KEY [CHAR]\n"
            "- ZMSG: TEXT [CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_src\n"
            "Exact work-area names: st_src\n"
            "Exact callable identities: Z_LOOKUP\n"
            "- ZSRC: structure st_src, table t_src, work area st_src"
        )
        callable_metadata = {
            "callable_signatures": {
                "Z_LOOKUP": {
                    "parameters": {
                        "IV_KEY": {"direction": "IMPORTING", "abap_type": "ZSRC", "field": "KEY"},
                        "EV_TEXT": {"direction": "EXPORTING", "abap_type": "ZMSG", "field": "TEXT"},
                    }
                }
            }
        }
        prompts = []
        complete_plan = {
            "processing_steps": [
                {"step": 1, "operation": "CLEAR", "target": "w_output"},
                {"step": 2, "operation": "CALL_FUNCTION", "name": "Z_LOOKUP", "output_parameters": {"EV_TEXT": "w_output-UNKNOWN"}},
                {"step": 3, "operation": "APPEND", "source": "w_output", "target": "t_output"},
            ]
        }
        weaker_retry_plan = {"processing_steps": []}

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            response_payload = complete_plan if len(prompts) == 1 else weaker_retry_plan
            return {
                "text": json.dumps(response_payload),
                "model": "test-model",
                "usage": None,
                "raw_response_json": {"id": f"response-{len(prompts)}", "output_text": json.dumps(response_payload)},
            }

        with self.assertRaises(ProcessingPlanValidationError) as raised:
            extract_processing_plan(
                "Read source rows, call lookup, and append output.",
                generator,
                metadata_context=base_prompt,
                callable_metadata=callable_metadata,
                declaration_requirements=declaration_requirements,
            )

        result = raised.exception.diagnostics
        self.assertEqual(1, len(prompts))
        self.assertNotIn("Validation errors to fix:", prompts[0])
        self.assertEqual(["CLEAR", "CALL_FUNCTION", "APPEND"], [step["operation"] for step in result["plan"]["processing_steps"]])
        self.assertEqual({"EV_TEXT": "w_output-unknown"}, result["plan"]["processing_steps"][1]["output_parameters"])
        self.assertIsNone(result["invalid_plan"])
        self.assertIn("output field UNKNOWN not found", "\n".join(result["validation_errors"]))
        self.assertEqual("response-1", result["raw_response_json"]["id"])
        self.assertEqual(complete_plan, result["parsed_plan_before_normalization"])
        self.assertEqual(result["plan"], result["final_plan_passed_to_deterministic_validation"])
        self.assertEqual(
            [
                "parse_json_response.deserialized",
                "normalize_processing_plan.input",
                "normalize_processing_step_collection.root",
                "normalize_processing_steps.root",
                "prune_unused_read_steps.root",
                "renumber_processing_steps.root",
                "normalize_processing_plan.output",
                "validate_processing_plan.input",
            ],
            [entry["stage"] for entry in result["processing_plan_trace"]],
        )
        modified = result["normalization_diagnostics"]["modified_steps"]
        self.assertTrue(
            any(
                item["code_location"] == "services/orchestrator.py:normalize_processing_step"
                and item["original_step"].get("operation") == "CALL_FUNCTION"
                and item["original_step"].get("output_parameters", {}).get("EV_TEXT") == "w_output-UNKNOWN"
                and item["resulting_step"].get("output_parameters", {}).get("EV_TEXT") == "w_output-unknown"
                and item["reason"] == "normalized CALL_FUNCTION name and parameter mappings"
                for item in modified
            )
        )

    def test_processing_plan_extraction_retries_malformed_operation_structure(self):
        prompts = []

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            if len(prompts) == 1:
                return {"text": json.dumps({"processing_steps": [{"BOGUS": {"source": "t_src"}}]})}
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "No explicit business processing.",
            generator,
            metadata_context="Shared generation contract:\nExact callable identities: none",
            declaration_requirements=json.dumps({"output_structure_fields": []}),
        )

        self.assertEqual(2, len(prompts))
        self.assertIn("The previous processing plan was rejected by deterministic validation.", prompts[1])
        self.assertIn("unsupported or missing operation", prompts[1])
        self.assertEqual({"processing_steps": []}, result["plan"])

    def test_processing_plan_extraction_retries_invalid_json(self):
        prompts = []
        responses = [
            "not json",
            json.dumps({"processing_steps": []}),
        ]

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            return {"text": responses[len(prompts) - 1], "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "No explicit business processing.",
            generator,
            metadata_context="Shared generation contract:\nExact callable identities: none",
            declaration_requirements=json.dumps({"output_structure_fields": []}),
        )

        self.assertEqual(2, len(prompts))
        self.assertIn("invalid JSON", prompts[1])
        self.assertEqual({"processing_steps": []}, result["plan"])

    def test_processing_plan_extraction_requests_structured_json_schema(self):
        response_formats = []

        def generator(prompt_text, source_text, response_format=None):
            response_formats.append(response_format)
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "No explicit business processing.",
            generator,
            metadata_context="Shared generation contract:\nExact callable identities: none",
            declaration_requirements=json.dumps({"output_structure_fields": []}),
        )

        self.assertEqual({"processing_steps": []}, result["plan"])
        self.assertEqual(1, len(response_formats))
        schema_format = response_formats[0]
        self.assertEqual("json_schema", schema_format["type"])
        self.assertEqual("processing_plan", schema_format["name"])
        self.assertTrue(schema_format["strict"])
        schema = schema_format["schema"]
        self.assertEqual(["processing_steps"], schema["required"])
        self.assertEqual({"$ref": "#/$defs/step"}, schema["properties"]["processing_steps"]["items"])
        self.assertEqual({"$ref": "#/$defs/step"}, schema["$defs"]["loop_step"]["properties"]["steps"]["items"])
        self.assertEqual({"$ref": "#/$defs/step"}, schema["$defs"]["if_step"]["properties"]["then"]["items"])
        self.assertEqual({"$ref": "#/$defs/step"}, schema["$defs"]["if_step"]["properties"]["else"]["items"])
        self.assertEqual(
            ["left", "operator", "right"],
            schema["$defs"]["condition"]["required"],
        )

    def test_processing_plan_response_format_rejects_extra_step_properties(self):
        schema_format = processing_plan_response_format()
        schema = schema_format["schema"]

        self.assertFalse(schema["additionalProperties"])
        for definition_name in (
            "parameter_mapping",
            "loop_step",
            "read_step",
            "if_step",
            "move_step",
            "calculate_step",
            "derive_step",
            "transform_step",
            "aggregate_step",
            "count_step",
            "average_step",
            "percentage_step",
            "clear_step",
            "append_step",
            "call_function_step",
            "call_static_method_step",
            "call_method_step",
            "concatenate_step",
            "sort_step",
            "delete_step",
        ):
            self.assertFalse(schema["$defs"][definition_name]["additionalProperties"], definition_name)
            self.assertIn("properties", schema["$defs"][definition_name], definition_name)
        self.assertEqual("array", schema["$defs"]["parameter_mappings"]["type"])
        self.assertEqual(
            {"$ref": "#/$defs/parameter_mapping"},
            schema["$defs"]["parameter_mappings"]["items"],
        )

    def test_processing_plan_normalization_accepts_schema_parameter_arrays(self):
        plan = {
            "processing_steps": [
                {
                    "operation": "CALL_FUNCTION",
                    "name": "Z_LOOKUP",
                    "input_parameters": [{"parameter": "IV_KEY", "value": "st_hdr-KEY"}],
                    "output_parameters": [{"parameter": "EV_TEXT", "value": "w_output-MSG"}],
                }
            ]
        }

        normalized = normalize_processing_plan_with_diagnostics(
            plan,
            base_prompt=(
                "SAP callable signature catalogue:\n"
                "- Z_LOOKUP: IV_KEY [IMPORTING ZSRC-KEY], EV_TEXT [EXPORTING ZMSG-TEXT]\n"
                "Shared generation contract:\n"
                "Exact callable identities: Z_LOOKUP\n"
                "Exact form names: processing_form\n"
                "Exact internal tables: none\n"
                "Exact work areas: st_hdr\n"
                "Exact output work area: w_output\n"
                "Exact output table: t_output\n"
                "Exact required global variables: none\n"
            ),
            declaration_requirements=json.dumps(
                {"output_structure_fields": [{"name": "MSG", "type_or_like": "TYPE ZMSG-TEXT"}]}
            ),
            callable_metadata={
                "Z_LOOKUP": {
                    "parameters": {
                        "IV_KEY": {"direction": "IMPORTING", "abap_type": "ZSRC", "field": "KEY"},
                        "EV_TEXT": {"direction": "EXPORTING", "abap_type": "ZMSG", "field": "TEXT"},
                    }
                }
            },
        )

        call = normalized["plan"]["processing_steps"][0]
        self.assertEqual({"IV_KEY": "st_hdr-key"}, call["input_parameters"])
        self.assertEqual({"EV_TEXT": "w_output-msg"}, call["output_parameters"])

    def test_processing_plan_extraction_retries_missing_processing_steps(self):
        prompts = []
        responses = [
            json.dumps({"wrong_key": []}),
            json.dumps({"processing_steps": []}),
        ]

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            return {"text": responses[len(prompts) - 1], "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "No explicit business processing.",
            generator,
            metadata_context="Shared generation contract:\nExact callable identities: none",
            declaration_requirements=json.dumps({"output_structure_fields": []}),
        )

        self.assertEqual(2, len(prompts))
        self.assertIn("missing processing_steps", prompts[1])
        self.assertEqual({"processing_steps": []}, result["plan"])

    def test_processing_plan_uses_only_processing_rules_section_as_source_text(self):
        captured_sources = []

        def generator(prompt_text, source_text):
            captured_sources.append(source_text)
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        source_text = (
            "# Functional Specification\n"
            "Intro text that should not be sent.\n\n"
            "## Processing Rules\n"
            "Loop over selected rows.\n"
            "### Nested Detail\n"
            "Map the status text.\n\n"
            "## ALV Output\n"
            "Display columns that should not be sent.\n"
        )

        result = extract_processing_plan(source_text, generator)

        expected = "Loop over selected rows.\n### Nested Detail\nMap the status text."
        self.assertEqual([expected], captured_sources)
        self.assertEqual(expected, result["source_text"])
        self.assertEqual(source_text, result["original_source_text"])

    def test_processing_rules_section_falls_back_to_full_spec_when_absent(self):
        source_text = "# Functional Specification\nNo dedicated processing section."

        self.assertEqual(source_text, extract_processing_rules_section(source_text))

    def test_processing_plan_llm_receives_minimal_validated_processing_contract(self):
        declaration_requirements = json.dumps(
            {"output_structure_fields": [{"name": "MSG", "type_or_like": "TYPE ZMSG-TEXT"}]}
        )
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- ZSRC: KEY [CHAR], UNUSED_SRC [CHAR]\n"
            "- ZMSG: TEXT [CHAR], UNUSED_MSG [CHAR]\n"
            "- ZEXTRA: SHADOW [CHAR]\n"
            "SAP callable signature catalogue:\n"
            "- Z_LOOKUP: IV_KEY [IMPORTING ZSRC-KEY], EV_TEXT [EXPORTING ZMSG-TEXT]\n"
            "- Z_EXTRA_CALL: IV_SHADOW [IMPORTING ZEXTRA-SHADOW]\n"
            "Shared generation contract:\n"
            "Exact FORM names: read_data, process_data, display_alv\n"
            "Exact internal-table names: t_src, t_extra\n"
            "Exact work-area names: st_src, st_extra\n"
            "Exact output structure fields: MSG, EXTRA_PRESENTATION_FIELD\n"
            "Exact callable identities: Z_LOOKUP\n"
            "- ZSRC: structure st_src, table t_src, work area st_src\n"
            "- ZEXTRA: structure st_extra, table t_extra, work area st_extra\n"
        )
        callable_metadata = {
            "callable_signatures": {
                "Z_LOOKUP": {
                    "parameters": {
                        "IV_KEY": {"direction": "IMPORTING", "abap_type": "ZSRC", "field": "KEY"},
                        "EV_TEXT": {"direction": "EXPORTING", "abap_type": "ZMSG", "field": "TEXT"},
                    }
                },
                "Z_EXTRA_CALL": {"parameters": {"IV_SHADOW": {"direction": "IMPORTING", "abap_type": "ZEXTRA", "field": "SHADOW"}}},
            }
        }
        prompts = []
        plan = {"processing_steps": []}

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            return {"text": json.dumps(plan), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "Call Z_LOOKUP with IV_KEY from ZSRC-KEY and EV_TEXT to output MESSAGE.",
            generator,
            metadata_context=metadata_context,
            callable_metadata=callable_metadata,
            declaration_requirements=declaration_requirements,
        )

        self.assertEqual(1, len(prompts))
        prompt = prompts[0]
        self.assertIn("Processing contract supplied by validated application artefacts", prompt)
        self.assertIn('"ZSRC"', prompt)
        self.assertIn('"KEY"', prompt)
        self.assertIn('"ZMSG"', prompt)
        self.assertIn('"TEXT"', prompt)
        self.assertIn('"Z_LOOKUP"', prompt)
        self.assertIn('"MSG"', prompt)
        self.assertNotIn("ZEXTRA", prompt)
        self.assertNotIn("Z_EXTRA_CALL", prompt)
        self.assertNotIn("display_alv", prompt)
        self.assertNotIn("EXTRA_PRESENTATION_FIELD", prompt)
        diagnostics = result["processing_contract_diagnostics"]
        self.assertEqual([], diagnostics["validation_errors"])
        self.assertNotIn("ZEXTRA", diagnostics["filtered_metadata_supplied"])
        self.assertNotIn("Z_EXTRA_CALL", diagnostics["filtered_callable_metadata_supplied"])

    def test_processing_contract_does_not_require_unused_prompt_callable_metadata(self):
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM [CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc\n"
            "Exact work-area names: st_edidc\n"
            "Exact callable identities: Z_UNUSED\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
        )
        prompts = []

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "Move EDIDC-DOCNUM to output field DOCNUM.",
            generator,
            metadata_context=metadata_context,
            callable_metadata={"callable_signatures": {}},
            declaration_requirements=json.dumps(
                {"output_structure_fields": [{"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"}]}
            ),
        )

        self.assertEqual(1, len(prompts))
        self.assertEqual([], result["processing_contract_diagnostics"]["validation_errors"])
        self.assertNotIn("Z_UNUSED", prompts[0])

    def test_processing_contract_requires_metadata_for_rule_referenced_callable(self):
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM [CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc\n"
            "Exact work-area names: st_edidc\n"
            "Exact callable identities: Z_MISSING\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
        )

        with self.assertRaises(ProcessingContractValidationError) as raised:
            extract_processing_plan(
                "Call Z_MISSING for each record.",
                lambda prompt_text, source_text: {"text": json.dumps({"processing_steps": []})},
                metadata_context=metadata_context,
                callable_metadata={"callable_signatures": {}},
                declaration_requirements=json.dumps({"output_structure_fields": []}),
            )

        errors = "\n".join(raised.exception.diagnostics["validation_errors"])
        self.assertIn("required callable Z_MISSING has no validated signature metadata", errors)

    def test_processing_contract_missing_field_error_does_not_include_unused_callable(self):
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- PA0016: PERNR [NUMC]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_pa0016\n"
            "Exact work-area names: st_pa0016\n"
            "Exact callable identities: O_SEND_REQUEST->SET_STATUS_ATTRIBUTES\n"
            "- PA0016: structure st_pa0016, table t_pa0016, work area st_pa0016\n"
        )

        with self.assertRaises(ProcessingContractValidationError) as raised:
            extract_processing_plan(
                "If PA0016-PROBATION_EMAIL_SENT = X continue to the next record.",
                lambda prompt_text, source_text: {"text": json.dumps({"processing_steps": []})},
                metadata_context=metadata_context,
                callable_metadata={"callable_signatures": {}},
                declaration_requirements=json.dumps({"output_structure_fields": []}),
            )

        errors = "\n".join(raised.exception.diagnostics["validation_errors"])
        self.assertIn("required DDIC field PA0016-PROBATION_EMAIL_SENT is missing", errors)
        self.assertNotIn("O_SEND_REQUEST->SET_STATUS_ATTRIBUTES", errors)

    def test_processing_contract_prefers_cached_object_method_identity(self):
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "Shared generation contract:\n"
            "Exact callable identities: "
            "CL_BCS=>CREATE_PERSISTENT, CL_BCS=>SEND, CL_BCS=>SET_DOCUMENT, "
            "CL_BCS=>ADD_RECIPIENT, CL_BCS=>SET_SENDER, SEND_REQUEST=>SEND, "
            "SEND_REQUEST=>SET_DOCUMENT, SEND_REQUEST=>ADD_RECIPIENT, SEND_REQUEST=>SET_SENDER\n"
        )
        callable_metadata = {
            "callable_signatures": {
                "CL_BCS=>CREATE_PERSISTENT": {
                    "returning": {"name": "RESULT", "direction": "RETURNING", "abap_type": "CL_BCS"}
                },
                "SEND_REQUEST=>SEND": {"parameters": {}},
                "SEND_REQUEST=>SET_DOCUMENT": {"parameters": {}},
                "SEND_REQUEST=>ADD_RECIPIENT": {"parameters": {}},
                "SEND_REQUEST=>SET_SENDER": {"parameters": {}},
            }
        }

        contract = build_processing_contract(
            metadata_context=metadata_context,
            callable_metadata=callable_metadata,
            processing_rules_text=(
                "- Send probation review reminder emails.\n"
                "Send Emails\n"
                "Description: Send Emails\n"
                "Call static method CL_BCS=>CREATE_PERSISTENT.\n"
                "Stores the result returned by SEND_REQUEST->SEND.\n"
                "Call instance method SEND_REQUEST->SET_DOCUMENT.\n"
                "Call instance method SEND_REQUEST->ADD_RECIPIENT.\n"
                "Call instance method SEND_REQUEST->SET_SENDER.\n"
                "Call instance method SEND_REQUEST->SEND."
            ),
        )

        self.assertIn("SEND_REQUEST=>SEND", contract["final_processing_contract"]["callables"])
        self.assertIn("SEND_REQUEST=>SET_DOCUMENT", contract["final_processing_contract"]["callables"])
        self.assertIn("SEND_REQUEST=>ADD_RECIPIENT", contract["final_processing_contract"]["callables"])
        self.assertIn("SEND_REQUEST=>SET_SENDER", contract["final_processing_contract"]["callables"])
        self.assertNotIn("CL_BCS=>SEND", contract["final_processing_contract"]["callables"])
        self.assertNotIn("CL_BCS=>SET_DOCUMENT", contract["final_processing_contract"]["callables"])
        self.assertNotIn("CL_BCS=>ADD_RECIPIENT", contract["final_processing_contract"]["callables"])
        self.assertNotIn("CL_BCS=>SET_SENDER", contract["final_processing_contract"]["callables"])
        self.assertEqual([], contract["validation_errors"])

    def test_processing_contract_retains_rule_only_dependencies(self):
        declaration_requirements = json.dumps(
            {
                "parameters": [{"name": "p_include_fallback", "type": "c"}],
                "output_structure_fields": [
                    {"name": "MESSAGE", "type_or_like": "TYPE ZMSG-TEXT"},
                    {"name": "STATUS", "type_or_like": "TYPE ZHDR-STATUS"},
                ],
            }
        )
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHDR: DOCNUM [CHAR], STATUS [CHAR], UNUSED_HDR [CHAR]\n"
            "- ZDET: DOCNUM [CHAR], CODE [CHAR], DETAIL_TEXT [CHAR], FALLBACK_TEXT [CHAR], UNUSED_DET [CHAR]\n"
            "- ZMSG: TEXT [CHAR], CODE [CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_hdr, t_det\n"
            "Exact work-area names: st_hdr, st_det\n"
            "Exact callable identities: none\n"
            "- ZHDR: structure st_hdr, table t_hdr, work area st_hdr\n"
            "- ZDET: structure st_det, table t_det, work area st_det\n"
        )
        callable_metadata = {
            "callable_signatures": {
                "Z_LOOKUP": {
                    "parameters": {
                        "IV_CODE": {"direction": "IMPORTING", "abap_type": "ZDET", "field": "CODE"},
                        "EV_TEXT": {"direction": "EXPORTING", "abap_type": "ZMSG", "field": "TEXT"},
                    }
                }
            }
        }
        processing_rules = (
            "Read t_det where st_det-DOCNUM = st_hdr-DOCNUM.\n"
            "Fallback lookup uses st_det-FALLBACK_TEXT when p_include_fallback = 'X'.\n"
            "If st_hdr-STATUS is not initial, call Z_LOOKUP with IV_CODE from st_det-CODE and EV_TEXT to w_output-MESSAGE.\n"
            "Move st_det-DETAIL_TEXT to output field MESSAGE and st_hdr-STATUS to STATUS."
        )
        prompts = []

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            processing_rules,
            generator,
            metadata_context=metadata_context,
            callable_metadata=callable_metadata,
            declaration_requirements=declaration_requirements,
        )

        self.assertEqual(1, len(prompts))
        prompt = prompts[0]
        for required in (
            '"ZHDR"',
            '"ZDET"',
            '"ZMSG"',
            '"DOCNUM"',
            '"STATUS"',
            '"FALLBACK_TEXT"',
            '"CODE"',
            '"DETAIL_TEXT"',
            '"Z_LOOKUP"',
            '"IV_CODE"',
            '"EV_TEXT"',
        ):
            self.assertIn(required, prompt)
        self.assertNotIn("UNUSED_HDR", prompt)
        self.assertNotIn("UNUSED_DET", prompt)
        diagnostics = result["processing_contract_diagnostics"]
        self.assertEqual([], diagnostics["validation_errors"])
        discovered = diagnostics["discovered_dependencies"]
        self.assertEqual(["Z_LOOKUP"], discovered["callables"])
        self.assertEqual({"IV_CODE", "EV_TEXT"}, set(discovered["callable_parameters"]["Z_LOOKUP"]))
        self.assertIn("p_include_fallback", discovered["selection_parameters"])
        self.assertIn("FALLBACK_TEXT", discovered["ddic_fields"]["ZDET"])
        self.assertIn("DETAIL_TEXT", discovered["ddic_fields"]["ZDET"])
        self.assertTrue(diagnostics["llm_call_allowed"])

    def test_processing_contract_retains_ddic_object_used_only_in_read(self):
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHDR: DOCNUM [CHAR]\n"
            "- ZREAD: DOCNUM [CHAR], VALUE [CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_hdr, t_read\n"
            "Exact work-area names: st_hdr, st_read\n"
            "Exact callable identities: none\n"
            "- ZHDR: structure st_hdr, table t_hdr, work area st_hdr\n"
            "- ZREAD: structure st_read, table t_read, work area st_read\n"
        )
        prompts = []

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "Read t_read for the current header record.",
            generator,
            metadata_context=metadata_context,
            declaration_requirements=json.dumps({"output_structure_fields": []}),
        )

        self.assertEqual(1, len(prompts))
        self.assertIn('"ZREAD"', prompts[0])
        self.assertEqual([], result["processing_contract_diagnostics"]["validation_errors"])

    def test_processing_contract_retains_field_used_only_in_read_condition(self):
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- ZHDR: DOCNUM [CHAR]\n"
            "- ZREAD: DOCNUM [CHAR], MATCH_KEY [CHAR], UNUSED_FIELD [CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_hdr, t_read\n"
            "Exact work-area names: st_hdr, st_read\n"
            "Exact callable identities: none\n"
            "- ZHDR: structure st_hdr, table t_hdr, work area st_hdr\n"
            "- ZREAD: structure st_read, table t_read, work area st_read\n"
        )
        prompts = []

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "Read t_read where st_read-MATCH_KEY = st_hdr-DOCNUM.",
            generator,
            metadata_context=metadata_context,
            declaration_requirements=json.dumps({"output_structure_fields": []}),
        )

        self.assertEqual(1, len(prompts))
        self.assertIn('"MATCH_KEY"', prompts[0])
        self.assertNotIn("UNUSED_FIELD", prompts[0])
        discovered = result["processing_contract_diagnostics"]["discovered_dependencies"]
        self.assertIn("MATCH_KEY", discovered["ddic_fields"]["ZREAD"])

    def test_processing_contract_declares_resolved_variable_for_multi_source_value(self):
        declaration_requirements = json.dumps(
            {
                "output_structure_fields": [
                    {"name": "CUSTOMER_ID", "type_or_like": "TYPE ZOUT-CUSTOMER_ID"},
                ],
            }
        )
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- ZFIRST: DOCNUM [CHAR], IDENTIFIER [CHAR]\n"
            "- ZSECOND: DOCNUM [CHAR], IDENTIFIER [CHAR]\n"
            "- ZOUT: CUSTOMER_ID [CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_first, t_second\n"
            "Exact work-area names: st_first, st_second\n"
            "Exact callable identities: none\n"
            "- ZFIRST: structure st_first, table t_first, work area st_first\n"
            "- ZSECOND: structure st_second, table t_second, work area st_second\n"
        )
        processing_rules = (
            "Determine the identifier.\n"
            "If an identifier is found, use ZFIRST-IDENTIFIER.\n"
            "Otherwise use ZSECOND-IDENTIFIER as the fallback source.\n"
            "Populate CUSTOMER_ID from the resolved identifier."
        )
        prompts = []

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            processing_rules,
            generator,
            metadata_context=metadata_context,
            declaration_requirements=declaration_requirements,
        )

        self.assertEqual(1, len(prompts))
        self.assertIn('"processing_variables"', prompts[0])
        self.assertIn('"lv_resolved_identifier"', prompts[0])
        self.assertIn("instead of using DDIC work areas", prompts[0])
        variables = result["processing_contract_diagnostics"]["final_processing_contract"]["processing_variables"]
        self.assertEqual(1, len(variables))
        self.assertEqual("lv_resolved_identifier", variables[0]["name"])
        self.assertEqual(["ZFIRST-IDENTIFIER", "ZSECOND-IDENTIFIER"], variables[0]["source_fields"])
        self.assertEqual("DATA lv_resolved_identifier TYPE ZFIRST-IDENTIFIER.", variables[0]["declaration"])

    def test_processing_contract_uses_full_ddic_metadata_not_compact_prompt_catalogue(self):
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM [NUMC(16); key; IDoc number]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc\n"
            "Exact work-area names: st_edidc\n"
            "Exact callable identities: none\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
        )
        ddic_metadata = {
            "tables": {
                "EDIDC": {
                    "fields": {
                        "DOCNUM": {"name": "DOCNUM", "datatype": "NUMC", "length": 16, "key": True},
                        "STATUS": {"name": "STATUS", "datatype": "CHAR", "length": 2, "description": "IDoc Status"},
                    }
                }
            }
        }
        prompts = []

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "IDOC_STATUS = EDIDC-STATUS",
            generator,
            metadata_context=metadata_context,
            ddic_metadata=ddic_metadata,
            declaration_requirements=json.dumps({"output_structure_fields": []}),
        )

        self.assertEqual(1, len(prompts))
        self.assertIn('"STATUS"', prompts[0])
        diagnostics = result["processing_contract_diagnostics"]
        self.assertEqual([], diagnostics["validation_errors"])
        self.assertIn("STATUS", diagnostics["discovered_dependencies"]["ddic_fields"]["EDIDC"])
        self.assertEqual(["STATUS"], [field["name"] for field in diagnostics["filtered_metadata_supplied"]["EDIDC"]["fields"]])

    def test_processing_contract_retains_ddic_object_used_only_for_callable_input(self):
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- ZINPUT: CODE [CHAR], UNUSED_FIELD [CHAR]\n"
            "- ZMSG: TEXT [CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_input\n"
            "Exact work-area names: st_input\n"
            "Exact callable identities: Z_LOOKUP\n"
            "- ZINPUT: structure st_input, table t_input, work area st_input\n"
        )
        callable_metadata = {
            "callable_signatures": {
                "Z_LOOKUP": {
                    "parameters": {
                        "IV_CODE": {"direction": "IMPORTING", "abap_type": "ZMSG", "field": "TEXT"},
                    }
                }
            }
        }
        prompts = []

        def generator(prompt_text, source_text):
            prompts.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "Call Z_LOOKUP with IV_CODE = st_input-CODE.",
            generator,
            metadata_context=metadata_context,
            callable_metadata=callable_metadata,
            declaration_requirements=json.dumps({"output_structure_fields": []}),
        )

        self.assertEqual(1, len(prompts))
        self.assertIn('"ZINPUT"', prompts[0])
        self.assertIn('"CODE"', prompts[0])
        self.assertNotIn("UNUSED_FIELD", prompts[0])
        discovered = result["processing_contract_diagnostics"]["discovered_dependencies"]
        self.assertIn("ZINPUT", discovered["ddic_objects"])
        self.assertIn("CODE", discovered["ddic_fields"]["ZINPUT"])

    def test_processing_contract_includes_conditional_second_stage_callables_and_methods(self):
        source_text = "\n".join(
            [
                "Process every record in t_pa0000.",
                "If P_NOTIFY EQ 'X'.",
                "DATA lo_sender TYPE REF TO zcl_notify_sender.",
                "Call function module Z_RESOLVE_LEADER with PERNR = PA0000-PERNR.",
                "Call lo_sender->send with IV_PERNR = PA0000-PERNR and IV_SENDER = P_SENDER.",
                "Call static method zcl_notify_audit=>record with IV_PERNR = PA0000-PERNR.",
                "Append W_OUTPUT to T_OUTPUT.",
            ]
        )
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- PA0000: PERNR [NUMC]\n"
            "SAP callable signature catalogue:\n"
            "- Z_RESOLVE_LEADER: PERNR [IMPORTING TYPE PA0000-PERNR], LEADER [EXPORTING CHAR]\n"
            "- ZCL_NOTIFY_SENDER=>SEND: IV_PERNR [IMPORTING TYPE PA0000-PERNR], EV_STATUS [EXPORTING CHAR]\n"
            "- ZCL_NOTIFY_AUDIT=>RECORD: IV_PERNR [IMPORTING TYPE PA0000-PERNR], RV_STATUS [RETURNING CHAR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_pa0000\n"
            "Exact work-area names: st_pa0000\n"
            "Exact callable identities: Z_RESOLVE_LEADER, ZCL_NOTIFY_SENDER=>SEND, ZCL_NOTIFY_AUDIT=>RECORD\n"
            "- PA0000: structure st_pa0000, table t_pa0000, work area st_pa0000"
        )
        declaration_requirements = json.dumps(
            {
                "parameters": [
                    {"name": "P_NOTIFY"},
                    {"name": "P_SENDER", "type_or_like": "TYPE AD_SMTPADR"},
                ],
                "output_structure_fields": [
                    {"name": "PERNR", "type_or_like": "TYPE PA0000-PERNR"},
                    {"name": "LEADER"},
                    {"name": "STATUS"},
                ]
            }
        )
        callable_metadata = {
            "callable_signatures": {
                "Z_RESOLVE_LEADER": {
                    "parameters": {
                        "PERNR": {"direction": "IMPORTING", "abap_type": "PA0000", "field": "PERNR"},
                        "LEADER": {"direction": "EXPORTING", "abap_type": "CHAR"},
                    }
                },
                "ZCL_NOTIFY_SENDER=>SEND": {
                    "parameters": {
                        "IV_PERNR": {"direction": "IMPORTING", "abap_type": "PA0000", "field": "PERNR"},
                        "IV_SENDER": {"direction": "IMPORTING", "abap_type": "AD_SMTPADR"},
                        "EV_STATUS": {"direction": "EXPORTING", "abap_type": "CHAR"},
                    }
                },
                "ZCL_NOTIFY_AUDIT=>RECORD": {
                    "parameters": {
                        "IV_PERNR": {"direction": "IMPORTING", "abap_type": "PA0000", "field": "PERNR"},
                    },
                    "returning": {"name": "RV_STATUS", "direction": "RETURNING", "abap_type": "CHAR"},
                },
            }
        }
        prompts = []

        def generator(prompt_text, _source_text):
            prompts.append(prompt_text)
            self.assertIn('"selection_parameters": [', prompt_text)
            self.assertIn('"p_notify"', prompt_text)
            self.assertIn('"p_sender"', prompt_text)
            self.assertIn('"Z_RESOLVE_LEADER"', prompt_text)
            self.assertIn('"ZCL_NOTIFY_SENDER=>SEND"', prompt_text)
            self.assertIn('"ZCL_NOTIFY_AUDIT=>RECORD"', prompt_text)
            return {
                "text": json.dumps(
                    {
                        "processing_steps": [
                            {
                                "operation": "LOOP",
                                "source": "t_pa0000",
                                "into": "st_pa0000",
                                "steps": [
                                    {
                                        "operation": "IF",
                                        "conditions": [{"left": "p_notify", "operator": "=", "right": "'X'"}],
                                        "then": [
                                            {"operation": "CLEAR", "target": "w_output"},
                                            {
                                                "operation": "CALL_FUNCTION",
                                                "name": "Z_RESOLVE_LEADER",
                                                "input_parameters": {"PERNR": "st_pa0000-PERNR"},
                                                "output_parameters": {"LEADER": "w_output-LEADER"},
                                            },
                                            {
                                                "operation": "CALL_METHOD",
                                                "object": "lo_sender",
                                                "method": "send",
                                                "input_parameters": {"IV_PERNR": "st_pa0000-PERNR", "IV_SENDER": "p_sender"},
                                                "output_parameters": {"EV_STATUS": "w_output-STATUS"},
                                            },
                                            {
                                                "operation": "CALL_STATIC_METHOD",
                                                "class": "zcl_notify_audit",
                                                "method": "record",
                                                "input_parameters": {"IV_PERNR": "st_pa0000-PERNR"},
                                                "receiving_parameter": "w_output-STATUS",
                                            },
                                            {"operation": "MOVE", "source": "st_pa0000-PERNR", "target": "w_output-PERNR"},
                                            {"operation": "APPEND", "source": "w_output", "target": "t_output"},
                                        ],
                                        "else": [],
                                    }
                                ],
                            }
                        ]
                    }
                ),
                "model": "test-model",
                "usage": None,
            }

        result = extract_processing_plan(
            source_text,
            generator,
            metadata_context=metadata_context,
            callable_metadata=callable_metadata,
            declaration_requirements=declaration_requirements,
        )

        self.assertEqual(1, len(prompts))
        diagnostics = result["processing_contract_diagnostics"]
        self.assertEqual([], diagnostics["validation_errors"])
        self.assertEqual(["p_notify", "p_sender"], diagnostics["final_processing_contract"]["selection_parameters"])
        self.assertEqual(
            {"Z_RESOLVE_LEADER", "ZCL_NOTIFY_SENDER=>SEND", "ZCL_NOTIFY_AUDIT=>RECORD"},
            set(diagnostics["final_processing_contract"]["callables"]),
        )
        normalized_steps = result["plan"]["processing_steps"][0]["steps"][0]["then"]
        self.assertEqual(["CLEAR", "CALL_FUNCTION", "CALL_METHOD", "CALL_STATIC_METHOD", "MOVE", "APPEND"], [step["operation"] for step in normalized_steps])
        self.assertEqual({"object": "lo_sender", "method": "SEND"}, {key: normalized_steps[2][key] for key in ("object", "method")})
        self.assertEqual({"class": "ZCL_NOTIFY_AUDIT", "method": "RECORD"}, {key: normalized_steps[3][key] for key in ("class", "method")})

    def test_processing_contract_uses_only_typed_dependencies_end_to_end(self):
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- PA0000: PERNR [TYPE PA0000-PERNR]\n"
            "- PA0001: WERKS [TYPE PA0001-WERKS]\n"
            "SAP callable signature catalogue:\n"
            "- Z_SEND_MAIL: IV_PERNR [IMPORTING TYPE PA0000-PERNR]\n"
            "- ZCL_MAILER=>CREATE: IV_SENDER [IMPORTING TYPE AD_SMTPADR], RO_MAILER [RETURNING REF TO OBJECT]\n"
            "- ZCL_MAILER=>SEND: IV_PERNR [IMPORTING TYPE PA0000-PERNR]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_pa0000, t_pa0001\n"
            "Exact work-area names: st_pa0000, st_pa0001\n"
            "Exact callable identities: Z_SEND_MAIL, ZCL_MAILER=>CREATE, ZCL_MAILER=>SEND\n"
            "- PA0000: structure st_pa0000, table t_pa0000, work area st_pa0000\n"
            "- PA0001: structure st_pa0001, table t_pa0001, work area st_pa0001\n"
        )
        callable_metadata = {
            "callable_signatures": {
                "Z_SEND_MAIL": {"parameters": {"IV_PERNR": {"direction": "IMPORTING", "abap_type": "PA0000", "field": "PERNR"}}},
                "ZCL_MAILER=>CREATE": {
                    "parameters": {"IV_SENDER": {"direction": "IMPORTING", "abap_type": "AD_SMTPADR"}},
                    "returning": {"name": "RO_MAILER", "direction": "RETURNING", "abap_type": "REF TO OBJECT"},
                },
                "ZCL_MAILER=>SEND": {"parameters": {"IV_PERNR": {"direction": "IMPORTING", "abap_type": "PA0000", "field": "PERNR"}}},
            }
        }
        declaration_requirements = {
            "parameters": [
                {"name": "P_EMAIL", "radiobutton_group": "rad1"},
                {"name": "P_REPORT", "radiobutton_group": "rad1"},
                {"name": "P_SENDER", "type_or_like": "TYPE AD_SMTPADR"},
            ],
            "select_options": [
                {"name": "S_PERNR", "for_field": "PA0000-PERNR"},
                {"name": "S_WERKS", "for_field": "PA0001-WERKS"},
            ],
            "global_variables": [{"name": "lo_mailer", "declaration": "DATA lo_mailer TYPE REF TO zcl_mailer."}],
            "output_structure_fields": [{"name": "PERNR", "type_or_like": "TYPE PA0000-PERNR"}],
        }
        processing_rules = (
            "START PROCESSING RULES\n"
            "S_PERNR Type: Select-Option Reference Field: PA0000-PERNR\n"
            "S_WERKS PA0001-WERKS Personnel Area\n"
            "P_EMAIL Control Type: Radio Button\n"
            "P_REPORT Control Type: Radio Button\n"
            "P_SENDER Reference Type: AD_SMTPADR\n"
            "CALL_FUNCTION\n"
            "Loop t_pa0000 into st_pa0000 and call function module Z_SEND_MAIL with IV_PERNR from PA0000-PERNR.\n"
            "Call static method ZCL_MAILER=>CREATE with IV_SENDER from P_SENDER returning lo_mailer.\n"
            "Call instance method lo_mailer->send with IV_PERNR from st_pa0000-PERNR.\n"
            "Unknown PROSE WORDS RADIO SELECT CALL START PROCESSING should not become DDIC metadata.\n"
        )

        contract = build_processing_contract(
            metadata_context=metadata_context,
            callable_metadata=callable_metadata,
            declaration_requirements=declaration_requirements,
            processing_rules_text=processing_rules,
        )
        final_contract = contract["final_processing_contract"]

        self.assertEqual([], contract["validation_errors"])
        self.assertEqual({"PA0000", "PA0001"}, set(final_contract["ddic_objects"]))
        self.assertEqual(
            {"Z_SEND_MAIL", "ZCL_MAILER=>CREATE", "ZCL_MAILER=>SEND"},
            set(final_contract["callables"]),
        )
        dependency_keys = {(item.get("kind"), item.get("name"), item.get("parent")) for item in final_contract["dependencies"]}
        self.assertIn(("ddic_field", "PERNR", "PA0000"), dependency_keys)
        self.assertIn(("ddic_field", "WERKS", "PA0001"), dependency_keys)
        self.assertIn(("selection_parameter", "p_email", ""), dependency_keys)
        self.assertFalse({"SELECT", "RADIO", "CALL", "START", "PROCESSING"} & set(final_contract["ddic_objects"]))

    def test_processing_rule_discovery_classifies_abap_system_fields_separately(self):
        discovered = discover_processing_rule_dependencies(
            (
                "If SY-DATUM is greater than ZHDR-DATE.\n"
                "If SY-SUBRC = 0 use SY-TABIX and SY-UNAME."
            ),
            ddic_catalogue={"tables": {"ZHDR": {"fields": [{"name": "DATE"}]}}},
            object_contracts={"ZHDR": {"structure": "st_zhdr", "table": "t_zhdr", "work_area": "st_zhdr"}},
        )

        self.assertEqual(["sy-datum", "sy-subrc", "sy-tabix", "sy-uname"], discovered["system_fields"])
        self.assertNotIn("SY", discovered["ddic_objects"])
        self.assertNotIn("SY", discovered["ddic_fields"])
        self.assertIn("ZHDR", discovered["ddic_objects"])
        self.assertEqual(["DATE"], discovered["ddic_fields"]["ZHDR"])

    def test_processing_contract_allows_system_fields_without_sy_metadata(self):
        calls = []

        def generator(prompt_text, source_text):
            calls.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "If SY-DATUM is before today, check SY-SUBRC, SY-TABIX and SY-UNAME.",
            generator,
            metadata_context="Shared generation contract:\nExact callable identities: none",
            declaration_requirements=json.dumps({"output_structure_fields": []}),
        )

        self.assertEqual(1, len(calls))
        diagnostics = result["processing_contract_diagnostics"]
        self.assertEqual([], diagnostics["validation_errors"])
        self.assertEqual([], diagnostics["missing_dependencies"])
        self.assertEqual({}, diagnostics["filtered_metadata_supplied"])
        self.assertEqual(
            ["sy-datum", "sy-subrc", "sy-tabix", "sy-uname"],
            diagnostics["discovered_dependencies"]["system_fields"],
        )
        self.assertNotIn("SY", diagnostics["discovered_dependencies"]["ddic_objects"])
        self.assertEqual(
            ["sy-datum", "sy-subrc", "sy-tabix", "sy-uname"],
            diagnostics["final_processing_contract"]["system_fields"],
        )

    def test_processing_contract_validation_stops_before_llm_when_metadata_is_missing(self):
        declaration_requirements = json.dumps(
            {"output_structure_fields": [{"name": "MSG", "type_or_like": "TYPE ZMSG-TEXT"}]}
        )
        calls = []

        def generator(prompt_text, source_text):
            calls.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []})}

        with self.assertRaises(ProcessingContractValidationError) as raised:
            extract_processing_plan(
                "Move message text.",
                generator,
                metadata_context="Shared generation contract:\nExact callable identities: none",
                declaration_requirements=declaration_requirements,
            )

        self.assertEqual([], calls)
        errors = "\n".join(raised.exception.diagnostics["validation_errors"])
        self.assertIn("required DDIC object ZMSG is missing", errors)
        self.assertEqual("MSG", raised.exception.diagnostics["filtered_output_contract_supplied"][0]["name"])

    def test_processing_contract_missing_rule_dependency_stops_before_llm_with_rule_text(self):
        calls = []

        def generator(prompt_text, source_text):
            calls.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []})}

        with self.assertRaises(ProcessingContractValidationError) as raised:
            extract_processing_plan(
                "Fallback lookup reads ZMISSING-KEY before appending output.",
                generator,
                metadata_context=(
                    "SAP DDIC metadata catalogue:\n"
                    "- ZSRC: KEY [CHAR]\n"
                    "Shared generation contract:\n"
                    "Exact callable identities: none\n"
                ),
                declaration_requirements=json.dumps({"output_structure_fields": []}),
            )

        self.assertEqual([], calls)
        diagnostics = raised.exception.diagnostics
        self.assertFalse(diagnostics["llm_call_allowed"])
        self.assertEqual("Fallback lookup reads ZMISSING-KEY before appending output.", diagnostics["processing_rules_text"])
        self.assertTrue(
            any(
                item["kind"] == "ddic_object"
                and item["name"] == "ZMISSING"
                and item["processing_rule_text"] == "Fallback lookup reads ZMISSING-KEY before appending output."
                for item in diagnostics["missing_dependencies"]
            )
        )
        self.assertIn("ZMISSING", "\n".join(diagnostics["validation_errors"]))

    def test_processing_contract_unshared_rule_dependency_stops_before_llm(self):
        calls = []

        def generator(prompt_text, source_text):
            calls.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []})}

        with self.assertRaises(ProcessingContractValidationError) as raised:
            extract_processing_plan(
                "Read ZUNSHARED-KEY as a fallback lookup.",
                generator,
                metadata_context=(
                    "SAP DDIC metadata catalogue:\n"
                    "- ZKNOWN: KEY [CHAR]\n"
                    "- ZUNSHARED: KEY [CHAR]\n"
                    "Shared generation contract:\n"
                    "Exact internal-table names: t_known\n"
                    "Exact work-area names: st_known\n"
                    "Exact callable identities: none\n"
                    "- ZKNOWN: structure st_known, table t_known, work area st_known\n"
                ),
                declaration_requirements=json.dumps({"output_structure_fields": []}),
            )

        self.assertEqual([], calls)
        diagnostics = raised.exception.diagnostics
        self.assertFalse(diagnostics["llm_call_allowed"])
        self.assertTrue(
            any(
                item["kind"] == "ddic_object"
                and item["name"] == "ZUNSHARED"
                and item["processing_rule_text"] == "Read ZUNSHARED-KEY as a fallback lookup."
                for item in diagnostics["missing_dependencies"]
            )
        )

    def test_processing_contract_validation_reports_missing_callable_metadata(self):
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- ZSRC: KEY [CHAR]\n"
            "Shared generation contract:\n"
            "Exact callable identities: Z_LOOKUP\n"
            "- ZSRC: structure st_src, table t_src, work area st_src\n"
        )

        with self.assertRaises(ProcessingContractValidationError) as raised:
            extract_processing_plan(
                "Call lookup.",
                lambda prompt_text, source_text: {"text": json.dumps({"processing_steps": []})},
                metadata_context=metadata_context,
            )

        errors = "\n".join(raised.exception.diagnostics["validation_errors"])
        self.assertIn("required callable Z_LOOKUP has no validated signature metadata", errors)

    def test_processing_contract_does_not_require_ddic_metadata_for_callable_standalone_types(self):
        declaration_requirements = json.dumps(
            {"output_structure_fields": [{"name": "ERROR_MESSAGE", "type_or_like": "TYPE BAPIRET2"}]}
        )
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- ZSRC: KEY [CHAR]\n"
            "Shared generation contract:\n"
            "Exact callable identities: Z_LOOKUP\n"
            "- ZSRC: structure st_src, table t_src, work area st_src\n"
        )
        callable_metadata = {
            "callable_signatures": {
                "Z_LOOKUP": {
                    "parameters": {
                        "MESSAGE": {
                            "direction": "EXPORTING",
                            "abap_type": "BAPIRET2",
                            "field": "MESSAGE",
                            "required": True,
                        },
                        "LANGUAGE": {
                            "direction": "IMPORTING",
                            "abap_type": "BAPITGA",
                            "field": "LANGU",
                            "required": False,
                        },
                    }
                }
            }
        }
        calls = []

        def generator(prompt_text, source_text):
            calls.append(prompt_text)
            return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}

        result = extract_processing_plan(
            "Call lookup and output the returned message.",
            generator,
            metadata_context=metadata_context,
            callable_metadata=callable_metadata,
            declaration_requirements=declaration_requirements,
        )

        self.assertEqual(1, len(calls))
        diagnostics = result["processing_contract_diagnostics"]
        self.assertEqual([], diagnostics["validation_errors"])
        self.assertEqual({}, diagnostics["filtered_metadata_supplied"])
        self.assertIn("BAPIRET2", calls[0])
        self.assertIn("BAPITGA", calls[0])

    def test_processing_contract_validation_reports_inconsistent_output_type(self):
        declaration_requirements = json.dumps(
            {"output_structure_fields": [{"name": "MSG", "type_or_like": "TYPE ZMSG-MISSING"}]}
        )
        metadata_context = (
            "SAP DDIC metadata catalogue:\n"
            "- ZMSG: TEXT [CHAR]\n"
            "Shared generation contract:\n"
            "Exact callable identities: none\n"
        )

        with self.assertRaises(ProcessingContractValidationError) as raised:
            extract_processing_plan(
                "Move message text.",
                lambda prompt_text, source_text: {"text": json.dumps({"processing_steps": []})},
                metadata_context=metadata_context,
                declaration_requirements=declaration_requirements,
            )

        errors = "\n".join(raised.exception.diagnostics["validation_errors"])
        self.assertIn("required DDIC field ZMSG-MISSING is missing", errors)

    def test_processing_plan_extraction_raises_after_invalid_retry_attempts(self):
        declaration_requirements = json.dumps(
            {"output_structure_fields": [{"name": "MSG", "type_or_like": "TYPE ZMSG-TEXT"}]}
        )
        invalid_plan = {
            "processing_steps": [
                {"step": 1, "operation": "MOVE", "source": "lv_missing", "target": "w_output-UNKNOWN"}
            ]
        }

        def generator(prompt_text, source_text):
            return {"text": json.dumps(invalid_plan), "model": "test-model", "usage": None}

        with self.assertRaises(ProcessingPlanValidationError) as raised:
            extract_processing_plan(
                "Move text into output.",
                generator,
                metadata_context="SAP DDIC metadata catalogue:\n- ZMSG: TEXT [CHAR]\nShared generation contract:\nExact FORM names: process_data",
                declaration_requirements=declaration_requirements,
            )

        self.assertIsNotNone(raised.exception.diagnostics["plan"])
        self.assertIsNone(raised.exception.diagnostics["invalid_plan"])
        self.assertEqual(1, len(raised.exception.diagnostics["attempts"]))

    def test_nested_processing_plan_fields_reach_filtered_ddic_metadata(self):
        processing_plan = json.dumps(
            {
                "processing_steps": [
                    {
                        "step": 1,
                        "operation": "LOOP",
                        "source": "t_edidc",
                        "into": "st_edidc",
                        "steps": [
                            {
                                "step": 2,
                                "operation": "READ",
                                "source": "t_edids",
                                "into": "st_edids",
                                "conditions": [
                                    {"left": "st_edids-docnum", "operator": "=", "right": "st_edidc-docnum"}
                                ],
                            },
                            {"step": 3, "operation": "MOVE", "source": "st_edids-status", "target": "w_output-idoc_status"},
                            {
                                "step": 4,
                                "operation": "CALL_FUNCTION",
                                "name": "BAPI_MESSAGE_GETDETAIL",
                                "input_parameters": {"ID": "st_edids-stamid", "NUMBER": "st_edids-stamno"},
                                "output_parameters": {"RETURN": "w_output-error_message"},
                            },
                        ],
                    }
                ]
            },
            indent=2,
        )
        prompt = chunk_prompt_text(
            (
                "SAP DDIC metadata catalogue:\n"
                "- EDIDC: CREDAT [DATS], DOCNUM [NUMC], MESTYP [CHAR]\n"
                "- EDIDS: DOCNUM [NUMC], STATUS [CHAR], STAMID [CHAR], STAMNO [NUMC], STAPA1 [CHAR]\n"
                "SAP callable signature catalogue:\n"
                "- BAPI_MESSAGE_GETDETAIL: ID [IMPORTING CHAR], NUMBER [IMPORTING NUMC], RETURN [EXPORTING BAPIRET2], MESSAGE [EXPORTING CHAR]\n"
                "Shared generation contract:\n"
                "Exact internal-table names: t_edidc, t_edids\n"
                "Exact work-area names: st_edidc, st_edids\n"
                "Exact callable identities: BAPI_MESSAGE_GETDETAIL\n"
                "Exact FORM names: process_data\n"
                "- EDIDC: structure st_edidc, table t_edidc, work area st_edidc\n"
                "- EDIDS: structure st_edids, table t_edids, work area st_edids"
            ),
            {"name": "processing_form", "instruction": "Generate processing."},
            declaration_requirements=json.dumps({"output_structure_fields": [{"name": "IDOC_STATUS"}, {"name": "ERROR_MESSAGE"}]}),
            processing_plan=processing_plan,
        )

        self.assertIn('"steps": [', prompt)
        self.assertIn('"conditions": [', prompt)
        self.assertIn('"input_parameters": {', prompt)
        self.assertIn('"output_parameters": {', prompt)
        self.assertIn("- EDIDC: DOCNUM [NUMC]", prompt)
        self.assertIn("- EDIDS: DOCNUM [NUMC], STATUS [CHAR], STAMID [CHAR], STAMNO [NUMC]", prompt)
        self.assertNotIn("CREDAT", prompt)
        self.assertNotIn("MESTYP", prompt)
        self.assertNotIn("STAPA1", prompt)
        self.assertIn("Exact internal-table names: t_edidc, t_edids", prompt)
        self.assertIn("Exact work-area names: st_edidc, st_edids", prompt)
        self.assertIn("Exact output names: type ty_output", prompt)
        self.assertIn("- BAPI_MESSAGE_GETDETAIL: ID [IMPORTING CHAR], NUMBER [IMPORTING NUMC], RETURN [EXPORTING BAPIRET2]", prompt)
        self.assertNotIn("MESSAGE [EXPORTING CHAR]", prompt)

    def test_output_forms_prompt_uses_exact_global_output_table_name(self):
        declaration_requirements = json.dumps(
            {
                "report_name": "ztest",
                "parameters": [],
                "select_options": [],
                "output_structure_fields": [
                    {"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"}
                ],
            },
            indent=2,
        )
        prompt = chunk_prompt_text(
            (
                "SAP DDIC metadata catalogue:\n"
                "- EDIDC: DOCNUM\n"
                "Shared generation contract:\n"
                "Exact output structure fields: DOCNUM\n"
                "Exact FORM names: output_data, display_alv, write_csv"
            ),
            {"name": "output_forms", "instruction": "Generate output forms."},
            source_text="Display output DOCNUM in ALV and export output DOCNUM to CSV.",
            declaration_requirements=declaration_requirements,
        )

        expected_contract = (
            "Exact global output internal table: t_output. "
            "This global object already exists; use t_output directly. "
            "Do not invent alternative output table names such as gt_output, it_output, lt_output, or ct_output."
        )
        self.assertIn(expected_contract, prompt)
        self.assertIn("Exact output structure fields: DOCNUM", prompt)
        self.assertIn("Exact output FORM names: output_data, display_alv, write_csv", prompt)
        self.assertIn("Exact file-output global variable: w_filename.", prompt)
        self.assertIn("Treat w_filename as the dataset path only; do not use it as a CSV content buffer.", prompt)
        self.assertIn("Exact CSV line global variable: w_csv_line.", prompt)
        self.assertIn("TRANSFER literals or w_csv_line TO w_filename; never TRANSFER w_filename TO w_filename.", prompt)
        self.assertIn("Do not use the filename variable as a CSV header, row, or concatenation buffer.", prompt)

    def test_output_forms_prompt_omits_output_data_when_processing_plan_builds_output(self):
        declaration_requirements = json.dumps(
            {
                "report_name": "ztest",
                "output_structure_fields": [
                    {"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"}
                ],
            },
            indent=2,
        )
        processing_plan = json.dumps(
            {
                "processing_steps": [
                    {"operation": "CLEAR", "target": "w_output"},
                    {"operation": "MOVE", "source": "st_edidc-DOCNUM", "target": "w_output-DOCNUM"},
                    {"operation": "APPEND", "source": "w_output", "target": "t_output"},
                ]
            }
        )

        prompt = chunk_prompt_text(
            (
                "SAP DDIC metadata catalogue:\n"
                "- EDIDC: DOCNUM\n"
                "Shared generation contract:\n"
                "Exact output structure fields: DOCNUM\n"
                "Exact FORM names: output_data, display_alv"
            ),
            {"name": "output_forms", "instruction": "Generate output forms."},
            source_text="Display output DOCNUM in ALV.",
            declaration_requirements=declaration_requirements,
            processing_plan=processing_plan,
        )

        self.assertIn("Exact output FORM names: display_alv", prompt)
        self.assertNotIn("Exact output FORM names: output_data", prompt)
        self.assertIn("must not CLEAR, REFRESH, FREE, DELETE, or append extra rows to t_output", prompt)

    def test_alv_field_catalogue_globals_are_passed_to_declarations_and_output_forms(self):
        declaration_requirements = json.dumps(
            {
                "report_name": "ztest",
                "parameters": [],
                "select_options": [],
                "output_structure_fields": [
                    {"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"}
                ],
            },
            indent=2,
        )
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM\n"
            "Shared generation contract:\n"
            "Exact output structure fields: DOCNUM\n"
            "Exact FORM names: output_data, display_alv"
        )

        declarations_prompt = chunk_prompt_text(
            base_prompt,
            {"name": "declarations", "instruction": "Generate declarations."},
            source_text="Display output DOCNUM using ALV.",
            declaration_requirements=declaration_requirements,
        )
        output_prompt = chunk_prompt_text(
            base_prompt,
            {"name": "output_forms", "instruction": "Generate output forms."},
            source_text="Display output DOCNUM using ALV.",
            declaration_requirements=declaration_requirements,
        )

        self.assertIn("Exact ALV field-catalogue globals:", declarations_prompt)
        self.assertIn("- Declare global internal table t_fieldcat TYPE slis_t_fieldcat_alv.", declarations_prompt)
        self.assertIn("- Declare global work area w_fieldcat TYPE slis_fieldcat_alv.", declarations_prompt)
        self.assertIn("Exact ALV field-catalogue globals:", output_prompt)
        self.assertIn(
            "- Use existing global internal table t_fieldcat directly; it is already declared TYPE slis_t_fieldcat_alv.",
            output_prompt,
        )
        self.assertIn(
            "- Use existing global work area w_fieldcat directly; it is already declared TYPE slis_fieldcat_alv.",
            output_prompt,
        )
        self.assertIn(
            "- Do not create local field-catalogue DATA, TYPES, CONSTANTS, FIELD-SYMBOLS, RANGES, or STATICS declarations inside output FORM routines.",
            output_prompt,
        )
        self.assertIn(
            "- Do not invent alternative field-catalogue names such as lt_fieldcat, it_fieldcat, gt_fieldcat, ls_fieldcat, wa_fieldcat, or gs_fieldcat.",
            output_prompt,
        )
        self.assertNotIn("T_FIELDCATALOG", output_prompt)

    def test_output_forms_prompt_uses_declared_alv_output_fields_over_csv_config_fields(self):
        declaration_requirements = json.dumps(
            {
                "report_name": "ztest",
                "parameters": [],
                "select_options": [],
                "output_structure_fields": [
                    {"name": "MPE_ID", "type_or_like": "TYPE ZMD_MPE0001-IDENTIFIER"},
                    {"name": "IDOC_STATUS", "type_or_like": "TYPE EDIDS-STATUS"},
                    {"name": "ERROR_MESSAGE", "type_or_like": "TYPE BAPIRET2"},
                    {
                        "name": "IDOC_NUMBER",
                        "type_or_like": "TYPE EDIDC-DOCNUM",
                        "include_when": "p_idoc = 'X'",
                    },
                ],
            },
            indent=2,
        )
        prompt = chunk_prompt_text(
            (
                "SAP DDIC metadata catalogue:\n"
                "- ZMD_GEN0007: ZPATH, ZFILE\n"
                "- ZMD_MPE0001: IDENTIFIER\n"
                "- EDIDS: STATUS\n"
                "- EDIDC: DOCNUM\n"
                "Shared generation contract:\n"
                "Exact output structure fields: IDENTIFIER, ZPATH, ZFILE\n"
                "Exact FORM names: output_data, display_alv, write_csv"
            ),
            {"name": "output_forms", "instruction": "Generate output forms."},
            source_text=(
                "# ALV Output\n"
                "When p_alv is selected display:\n"
                "1. MPE_ID\n"
                "2. IDOC_STATUS\n"
                "3. ERROR_MESSAGE\n"
                "Include IDOC_NUMBER only when:\n"
                "p_idoc = 'X'\n\n"
                "# CSV Output\n"
                "When p_file is selected:\n"
                "* ZPATH as the output directory\n"
                "* ZFILE as the filename prefix\n"
            ),
            declaration_requirements=declaration_requirements,
        )

        self.assertIn(
            "Exact output structure fields: MPE_ID, IDOC_STATUS, ERROR_MESSAGE, IDOC_NUMBER",
            prompt,
        )
        self.assertIn("Conditional output structure fields:", prompt)
        self.assertIn("- IDOC_NUMBER: include only when p_idoc = 'X'", prompt)
        self.assertNotIn("Exact output structure fields: IDENTIFIER, ZPATH, ZFILE", prompt)

    def test_chunk_ddic_diagnostics_use_only_explicit_spec_fields(self):
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: CREDAT, DOCNUM, MESTYP\n"
            "- EDID4: DOCNUM, SDATA, SEGNAM\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc, t_edid4\n"
            "Exact work-area names: w_edidc, w_edid4\n"
            "Exact output structure fields: DOCNUM, MESTYP, SDATA, SEGNAM\n"
            "Exact FORM names: read_edidc, read_edid4, process_data, output_data\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area w_edidc\n"
            "- EDID4: structure st_edid4, table t_edid4, work area w_edid4"
        )
        responses = {
            "declarations": "REPORT ztest.",
            "database_read_forms": "FORM read_data.\nENDFORM.",
            "processing_form": "FORM process_data.\nENDFORM.",
            "output_forms": "FORM output_data.\nENDFORM.",
            "main_program_flow": "START-OF-SELECTION.",
        }

        def generator(prompt_text, _source_text):
            if "Extract declaration requirements" in prompt_text:
                return {
                    "text": json.dumps(
                        {
                            "report_name": "ztest",
                            "parameters": [],
                            "select_options": [{"name": "s_docnum", "field": "EDIDC-DOCNUM"}],
                        }
                    ),
                    "model": "test-model",
                    "usage": None,
                }
            if "Extract business-processing logic" in prompt_text:
                return {"text": json.dumps({"processing_steps": []}), "model": "test-model", "usage": None}
            chunk_name = next(name for name in responses if f"Chunk: {name}" in prompt_text)
            return {"text": responses[chunk_name], "model": "test-model", "usage": None}

        result = generate_chunked_abap_program(
            base_prompt,
            "Select EDIDC-DOCNUM. Export output SDATA to CSV.",
            abap_generator=generator,
        )
        chunks = {chunk["name"]: chunk for chunk in result["chunks"]}

        self.assertEqual(
            chunks["declarations"]["ddic_metadata_filter"]["fields_extracted_from_specification"],
            ["EDIDC-DOCNUM"],
        )
        self.assertEqual(
            chunks["declarations"]["ddic_metadata_filter"]["matched_sap_metadata_fields"],
            ["EDIDC-DOCNUM"],
        )
        self.assertIn("- EDIDC: DOCNUM", chunks["declarations"]["filtered_ddic_metadata"])
        self.assertNotIn("SDATA", chunks["declarations"]["filtered_ddic_metadata"])
        self.assertNotIn("Exact output structure fields:", chunks["declarations"]["declaration_naming_contract"])
        self.assertEqual(
            chunks["database_read_forms"]["ddic_metadata_filter"]["fields_extracted_from_specification"],
            ["EDIDC-DOCNUM"],
        )
        self.assertEqual(
            chunks["database_read_forms"]["ddic_metadata_filter"]["matched_sap_metadata_fields"],
            ["EDID4-DOCNUM", "EDIDC-DOCNUM"],
        )
        self.assertEqual(
            chunks["database_read_forms"]["ddic_metadata_filter"]["complete_row_type_objects"],
            ["EDID4", "EDIDC"],
        )
        self.assertIn("- EDIDC: DOCNUM", chunks["database_read_forms"]["filtered_ddic_metadata"])
        self.assertIn("- EDID4: DOCNUM", chunks["database_read_forms"]["filtered_ddic_metadata"])
        self.assertNotIn("MESTYP", chunks["database_read_forms"]["filtered_ddic_metadata"])
        self.assertNotIn("SEGNAM", chunks["output_forms"]["filtered_ddic_metadata"])

    def test_database_read_ddic_filter_preserves_required_verified_fields(self):
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM [NUMC(16); key; IDoc number], CREDAT [DATS(8); Created on], MESTYP [CHAR(30); Message Type], STATUS [CHAR(2); IDoc Status]\n"
            "- EDID4: DOCNUM [NUMC(16); key; IDoc number], SDATA [LCHR(1000); Application data]\n"
            "- EDIDS: DOCNUM [NUMC(16); key; IDoc number], STATUS [CHAR(2); IDoc Status], STATYP [CHAR(1); Msg.type (E,I,W,A,S)]\n"
            "- ZMD_MPE0001: IDENTIFIER [CHAR(32); key; Identifier], MATNR [CHAR(18); Article]\n"
            "- ZMD_MPE0006: IDENTIFIER [CHAR(32); key; Identifier], IDOC_STATUS [CHAR(2); IDoc Status]\n"
            "- ZMD_GEN0007: ZMDID [NUMC(10); key; Interface ID], ZPATH [CHAR(55); Path name]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc, t_edid4, t_edids, t_zmd_mpe0001, t_zmd_mpe0006, t_zmd_gen0007\n"
            "Exact work-area names: w_edidc, w_edid4, w_edids, w_zmd_mpe0001, w_zmd_mpe0006, w_zmd_gen0007\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area w_edidc\n"
            "- EDID4: structure st_edid4, table t_edid4, work area w_edid4\n"
            "- EDIDS: structure st_edids, table t_edids, work area w_edids\n"
            "- ZMD_MPE0001: structure st_zmd_mpe0001, table t_zmd_mpe0001, work area w_zmd_mpe0001\n"
            "- ZMD_MPE0006: structure st_zmd_mpe0006, table t_zmd_mpe0006, work area w_zmd_mpe0006\n"
            "- ZMD_GEN0007: structure st_zmd_gen0007, table t_zmd_gen0007, work area w_zmd_gen0007"
        )
        declaration_requirements = json.dumps(
            {
                "select_options": [
                    {"name": "s_id01", "for_field": "ZMD_MPE0001-IDENTIFIER"},
                    {"name": "s_id06", "for_field": "ZMD_MPE0006-IDENTIFIER"},
                    {"name": "s_credat", "for_field": "EDIDC-CREDAT"},
                    {"name": "s_mestyp", "for_field": "EDIDC-MESTYP"},
                    {"name": "s_status", "for_field": "EDIDC-STATUS"},
                ],
                "output_structure_fields": [
                    {"name": "IDOC_STATUS", "type_or_like": "TYPE ZMD_MPE0006-IDOC_STATUS"},
                    {"name": "IDOC_NUMBER", "type_or_like": "TYPE EDIDC-DOCNUM"},
                ],
            }
        )

        diagnostics = chunk_ddic_diagnostics(
            "database_read_forms",
            base_prompt,
            source_text=(
                "Read EDIDC records and dependent EDID4 and EDIDS records by DOCNUM. "
                "Read lookup ZMD_GEN0007 by ZMDID."
            ),
            declaration_requirements=declaration_requirements,
        )
        metadata = diagnostics["final_filtered_metadata"]

        self.assertIn("EDIDC: DOCNUM", metadata)
        self.assertIn("CREDAT", metadata)
        self.assertIn("DOCNUM", metadata)
        self.assertIn("MESTYP", metadata)
        self.assertIn("STATUS", metadata)
        self.assertIn("- EDID4: DOCNUM", metadata)
        self.assertIn("- EDIDS: DOCNUM", metadata)
        self.assertIn("STATUS [CHAR(2); IDoc Status]", metadata)
        self.assertIn("- ZMD_MPE0001: IDENTIFIER", metadata)
        self.assertIn("- ZMD_MPE0006: IDENTIFIER", metadata)
        self.assertIn("IDOC_STATUS", metadata)
        self.assertIn("- ZMD_GEN0007: ZMDID", metadata)
        self.assertNotIn("no fields selected", metadata)
        self.assertNotIn(" A", metadata)
        self.assertNotIn("S)]", metadata)
        self.assertEqual(diagnostics["rejected_fields"], [])
        self.assertIn("EDID4-DOCNUM", diagnostics["accepted_fields"])
        self.assertIn("EDIDS-DOCNUM", diagnostics["accepted_fields"])
        self.assertIn("EDIDS-STATUS", diagnostics["accepted_fields"])
        self.assertIn("ZMD_MPE0001-IDENTIFIER", diagnostics["accepted_fields"])
        self.assertIn("ZMD_MPE0006-IDENTIFIER", diagnostics["accepted_fields"])
        self.assertIn("ZMD_GEN0007-ZMDID", diagnostics["accepted_fields"])

    def test_database_read_requirements_preserve_database_object_section_fields(self):
        diagnostics = self.database_read_section_diagnostics(
            "# Data Extraction\n"
            "\n"
            "### EDIDS\n"
            "\n"
            "Use EDIDC-DOCNUM to retrieve status details.\n"
            "Join to EDIDS-DOCNUM = EDIDC-DOCNUM\n"
            "\n"
            "Fields:\n"
            "* DOCNUM\n"
            "* STATUS\n"
            "* STAMID\n"
            "* STAMNO\n"
            "* STAPA1\n"
            "* STAPA2\n"
            "* STAPA3\n"
            "* STAPA4\n"
            "\n"
            "Selection:\n"
            "* DOCNUM = EDIDC-DOCNUM\n"
            "\n"
            "### EDIDC\n"
            "Fields:\n"
            "* DOCNUM"
        )

        normalized = diagnostics["normalized_database_read_requirements"]
        for line in [
            "### EDIDS",
            "Use EDIDC-DOCNUM to retrieve status details.",
            "Join to EDIDS-DOCNUM = EDIDC-DOCNUM",
            "Fields:",
            "* DOCNUM",
            "* STATUS",
            "* STAMID",
            "* STAMNO",
            "* STAPA1",
            "* STAPA2",
            "* STAPA3",
            "* STAPA4",
            "Selection:",
            "* DOCNUM = EDIDC-DOCNUM",
        ]:
            self.assertIn(line, normalized)
        for field in [
            "EDIDS-DOCNUM",
            "EDIDS-STATUS",
            "EDIDS-STAMID",
            "EDIDS-STAMNO",
            "EDIDS-STAPA1",
            "EDIDS-STAPA2",
            "EDIDS-STAPA3",
            "EDIDS-STAPA4",
        ]:
            self.assertIn(field, diagnostics["fields_extracted_from_specification"])
            self.assertIn(field, diagnostics["matched_sap_metadata_fields"])

    def test_database_read_ddic_filter_preserves_read_fields_section(self):
        diagnostics = self.database_read_section_diagnostics(
            "EDIDS status records\n"
            "Join to EDIDS-DOCNUM = EDIDC-DOCNUM\n"
            "\n"
            "Read Fields\n"
            "DOCNUM\n"
            "STATUS\n"
            "STAMID\n"
            "STAMNO\n"
            "STAPA1\n"
            "STAPA2\n"
            "STAPA3\n"
            "STAPA4"
        )

        self.assert_database_read_section_fields(diagnostics)

    def test_database_read_ddic_filter_preserves_fields_required_section(self):
        diagnostics = self.database_read_section_diagnostics(
            "EDIDS status records\n"
            "Join to EDIDS-DOCNUM = EDIDC-DOCNUM\n"
            "\n"
            "Fields Required\n"
            "DOCNUM\n"
            "STATUS\n"
            "STAMID\n"
            "STAMNO\n"
            "STAPA1\n"
            "STAPA2\n"
            "STAPA3\n"
            "STAPA4"
        )

        self.assert_database_read_section_fields(diagnostics)

    def test_database_read_ddic_filter_preserves_unheaded_field_list_in_object_section(self):
        diagnostics = self.database_read_section_diagnostics(
            "Read EDIDS status records\n"
            "Join to EDIDS-DOCNUM = EDIDC-DOCNUM\n"
            "DOCNUM\n"
            "STATUS\n"
            "STAMID\n"
            "STAMNO\n"
            "STAPA1\n"
            "STAPA2\n"
            "STAPA3\n"
            "STAPA4"
        )

        self.assert_database_read_section_fields(diagnostics)

    def test_database_read_ddic_filter_stops_at_next_database_object_section(self):
        diagnostics = self.database_read_section_diagnostics(
            "Read EDIDS status records\n"
            "Join to EDIDS-DOCNUM = EDIDC-DOCNUM\n"
            "Fields Required\n"
            "DOCNUM\n"
            "STATUS\n"
            "STAMID\n"
            "\n"
            "Read EDIDC control records\n"
            "Fields Required\n"
            "DOCNUM\n"
            "CREDAT"
        )

        self.assertIn("Join to EDIDS-DOCNUM = EDIDC-DOCNUM", diagnostics["raw_extracted_database_read_requirements"])
        self.assertIn("EDIDS-DOCNUM", diagnostics["accepted_fields"])
        self.assertIn("EDIDS-STATUS", diagnostics["accepted_fields"])
        self.assertIn("EDIDS-STAMID", diagnostics["accepted_fields"])
        self.assertIn("EDIDC-DOCNUM", diagnostics["accepted_fields"])
        self.assertIn("EDIDC-CREDAT", diagnostics["accepted_fields"])
        self.assertNotIn("EDIDS-CREDAT", diagnostics["fields_extracted_from_specification"])
        self.assertIn("- EDIDS: DOCNUM", diagnostics["final_filtered_metadata"])
        self.assertIn("STATUS", diagnostics["final_filtered_metadata"])
        self.assertIn("STAMID", diagnostics["final_filtered_metadata"])
        self.assertIn("- EDIDC: DOCNUM", diagnostics["final_filtered_metadata"])
        self.assertIn("CREDAT", diagnostics["final_filtered_metadata"])

    def test_database_read_ddic_filter_rejects_unverified_unqualified_words(self):
        diagnostics = self.database_read_section_diagnostics(
            "Read EDIDS status records\n"
            "Join to EDIDS-DOCNUM = EDIDC-DOCNUM\n"
            "Retrieve Fields\n"
            "DOCNUM\n"
            "STATUS\n"
            "NOT_A_DDIC_FIELD"
        )

        self.assertIn("Join to EDIDS-DOCNUM = EDIDC-DOCNUM", diagnostics["raw_extracted_database_read_requirements"])
        self.assertIn("EDIDS-DOCNUM", diagnostics["accepted_fields"])
        self.assertIn("EDIDS-STATUS", diagnostics["accepted_fields"])
        self.assertNotIn("EDIDS-NOT_A_DDIC_FIELD", diagnostics["fields_extracted_from_specification"])
        self.assertNotIn("NOT_A_DDIC_FIELD", diagnostics["final_filtered_metadata"])
        self.assertEqual(diagnostics["rejected_fields"], [])

    def database_read_section_diagnostics(self, source_text):
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDC: DOCNUM [NUMC(16); key; IDoc number], CREDAT [DATS(8); Created on]\n"
            "- EDIDS: DOCNUM [NUMC(16); key; IDoc number], STATUS [CHAR(2); IDoc Status], STAMID [CHAR(20); Status message ID], STAMNO [CHAR(3); Status message number], STAPA1 [CHAR(50); Status value 1], STAPA2 [CHAR(50); Status value 2], STAPA3 [CHAR(50); Status value 3], STAPA4 [CHAR(50); Status value 4]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edidc, t_edids\n"
            "Exact work-area names: w_edidc, w_edids\n"
            "- EDIDC: structure st_edidc, table t_edidc, work area w_edidc\n"
            "- EDIDS: structure st_edids, table t_edids, work area w_edids"
        )
        return chunk_ddic_diagnostics(
            "database_read_forms",
            base_prompt,
            source_text=source_text,
        )

    def assert_database_read_section_fields(self, diagnostics):
        expected_fields = [
            "EDIDS-DOCNUM",
            "EDIDS-STATUS",
            "EDIDS-STAMID",
            "EDIDS-STAMNO",
            "EDIDS-STAPA1",
            "EDIDS-STAPA2",
            "EDIDS-STAPA3",
            "EDIDS-STAPA4",
        ]
        self.assertIn("Join to EDIDS-DOCNUM = EDIDC-DOCNUM", diagnostics["raw_extracted_database_read_requirements"])
        for field in expected_fields:
            self.assertIn(field, diagnostics["accepted_fields"])
            self.assertIn(field, diagnostics["matched_sap_metadata_fields"])
        metadata = diagnostics["final_filtered_metadata"]
        self.assertIn("- EDIDS: DOCNUM", metadata)
        for field_name in ["STATUS", "STAMID", "STAMNO", "STAPA1", "STAPA2", "STAPA3", "STAPA4"]:
            self.assertIn(field_name, metadata)

    def test_database_read_ddic_filter_rejects_malformed_fragments(self):
        base_prompt = (
            "SAP DDIC metadata catalogue:\n"
            "- EDIDS: DOCNUM [NUMC(16); key; IDoc number], STATUS [CHAR(2); IDoc Status], STATYP [CHAR(1); Msg.type (E,I,W,A,S)]\n"
            "Shared generation contract:\n"
            "Exact internal-table names: t_edids\n"
            "Exact work-area names: w_edids\n"
            "- EDIDS: structure st_edids, table t_edids, work area w_edids"
        )

        diagnostics = chunk_ddic_diagnostics(
            "database_read_forms",
            base_prompt,
            source_text="Read EDIDS-DOCNUM and EDIDS-STATUS. Ignore malformed values A and S)]. Read EDIDS-BOGUS.",
        )

        self.assertEqual(diagnostics["requested_fields_by_ddic_object"]["EDIDS"], ["DOCNUM", "STATUS", "BOGUS"])
        self.assertEqual(
            diagnostics["rejected_fields"],
            [
                {"reference": "EDIDS-BOGUS", "reason": "field was not returned by SAP metadata for this object"},
            ],
        )
        self.assertIn("- EDIDS: DOCNUM", diagnostics["final_filtered_metadata"])
        self.assertIn("STATUS [CHAR(2); IDoc Status]", diagnostics["final_filtered_metadata"])
        self.assertNotIn(" A", diagnostics["final_filtered_metadata"])
        self.assertNotIn("S)]", diagnostics["final_filtered_metadata"])

if __name__ == "__main__":
    unittest.main()
