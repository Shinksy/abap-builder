import unittest

from services.sap_dependency_analysis import (
    analyze_sap_dependencies,
    normalize_dependency_analysis,
    validate_dependency_analysis_json,
)


def analysis_json(**overrides):
    payload = {
        "ddic_objects": [],
        "callables": [],
        "unresolved": [],
    }
    payload.update(overrides)
    return payload


def ddic_object(name):
    lower_name = name.lower()
    return {"name": name, "structure": f"st_{lower_name}", "table": f"t_{lower_name}"}


class SapDependencyIdentificationTest(unittest.TestCase):
    def test_external_tables_and_callables_are_normalized(self):
        analysis = normalize_dependency_analysis(
            {
                "ddic_objects": [{"name": "edidc", "kind": "table"}, {"name": "EDIDC", "kind": "table"}],
                "callables": ["reuse_alv_grid_display"],
                "classes": [{"name": "cl_bcs", "methods": ["create_persistent", "send"]}],
                "unresolved": [],
            }
        )

        self.assertEqual(analysis["ddic_objects"], [ddic_object("EDIDC")])
        self.assertEqual(
            analysis["callables"],
            ["REUSE_ALV_GRID_DISPLAY", "CL_BCS=>CREATE_PERSISTENT", "CL_BCS=>SEND"],
        )
        self.assertEqual(analysis["unresolved"], [])
        self.assertNotIn("declarations", analysis)
        self.assertNotIn("operations", analysis)
        self.assertNotIn("execution_order", analysis)
        self.assertNotIn("form_names", analysis)
        self.assertNotIn("local_identifiers", analysis)

    def test_filters_invalid_ddic_candidates_without_local_identifier_output(self):
        analysis = normalize_dependency_analysis(
            {
                "ddic_objects": [
                    ddic_object("EDIDC"),
                    {"name": "CL_TEST=>RUN", "structure": "st_cl_test=>run", "table": "t_cl_test=>run"},
                    {"name": "FOR", "structure": "st_for", "table": "t_for"},
                    {"name": "SY", "structure": "st_sy", "table": "t_sy"},
                ],
                "callables": ["BAPI_MESSAGE_GETDETAIL"],
                "local_identifiers": ["LT_OUTPUT"],
                "unresolved": [],
            }
        )

        self.assertEqual(analysis["ddic_objects"], [ddic_object("EDIDC")])
        self.assertEqual(analysis["callables"], ["BAPI_MESSAGE_GETDETAIL"])
        self.assertNotIn("local_identifiers", analysis)
        rejected = {(item["name"], item["category"], item["reason"]) for item in analysis["rejected_analysis_entries"]}
        self.assertIn(("CL_TEST=>RUN", "ddic_objects", "invalid DDIC table/structure/view identifier"), rejected)
        self.assertIn(("FOR", "ddic_objects", "ABAP keyword"), rejected)
        self.assertIn(("SY", "ddic_objects", "system field"), rejected)

    def test_unresolved_excludes_classified_ddic_objects_and_callables(self):
        analysis = normalize_dependency_analysis(
            {
                "ddic_objects": [ddic_object("EDIDC")],
                "callables": ["BAPI_MESSAGE_GETDETAIL"],
                "unresolved": [
                    "EDIDC",
                    {"name": "BAPI_MESSAGE_GETDETAIL", "reason": "not sure"},
                    {"name": "ZUNKNOWN_DEPENDENCY", "reason": "not in metadata"},
                ],
            }
        )

        self.assertEqual(analysis["ddic_objects"], [ddic_object("EDIDC")])
        self.assertEqual(analysis["callables"], ["BAPI_MESSAGE_GETDETAIL"])
        self.assertEqual(analysis["unresolved"], ["ZUNKNOWN_DEPENDENCY"])

    def test_validator_accepts_ddic_object_naming_convention(self):
        validate_dependency_analysis_json(
            {
                "ddic_objects": [ddic_object("EDIDC")],
                "callables": [],
                "unresolved": [],
            }
        )

    def test_validator_rejects_ddic_object_string_entries(self):
        with self.assertRaisesRegex(ValueError, "ddic_objects entries must be objects"):
            validate_dependency_analysis_json(
                {
                    "ddic_objects": ["EDIDC"],
                    "callables": [],
                    "unresolved": [],
                }
            )

    def test_validator_rejects_ddic_object_naming_mismatch(self):
        with self.assertRaisesRegex(ValueError, "structure must be st_edidc"):
            validate_dependency_analysis_json(
                {
                    "ddic_objects": [{"name": "EDIDC", "structure": "st_wrong", "table": "t_edidc"}],
                    "callables": [],
                    "unresolved": [],
                }
            )

    def test_successful_parseable_dependency_json_ignores_implementation_plan_fields(self):
        raw_response = json_dumps(
            {
                "ddic_objects": [ddic_object("EDIDC")],
                "callables": ["BAPI_MESSAGE_GETDETAIL"],
                "unresolved": [],
                "declarations": [{"name": "st_output", "kind": "structure"}],
                "operations": [{"operation": "append"}],
                "execution_order": [{"operation_ref": "append_output"}],
                "form_names": ["append_output"],
                "local_identifiers": ["LT_OUTPUT"],
            }
        )

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Use SAP table EDIDC and BAPI_MESSAGE_GETDETAIL.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["ddic_objects"], [ddic_object("EDIDC")])
        self.assertEqual(analysis["callables"], ["BAPI_MESSAGE_GETDETAIL"])
        self.assertEqual(analysis["unresolved"], [])
        self.assertNotIn("declarations", analysis)
        self.assertNotIn("operations", analysis)
        self.assertNotIn("execution_order", analysis)
        self.assertEqual(analysis["_diagnostics"]["raw_response"], raw_response)

    def test_invalid_json_falls_back_to_deterministic_extraction(self):
        def invalid_json(_prompt, _source):
            return {"text": "not json"}

        analysis = analyze_sap_dependencies("Use field EDIDC-CREDAT. TYPES st_output TYPE char10.", enabled=True, llm_analyzer=invalid_json)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid JSON")
        self.assertEqual(analysis["ddic_objects"], [ddic_object("EDIDC")])
        self.assertEqual(analysis["ddic_types"], [])
        self.assertEqual(analysis["callables"], [])
        self.assertEqual(analysis["unresolved"], [])
        self.assertNotIn("local_identifiers", analysis)

    def test_deterministic_fallback_uses_positive_typed_ddic_evidence_only(self):
        def invalid_json(_prompt, _source):
            return {"text": "not json"}

        specification = "\n".join(
            [
                "Selection Screen",
                "S_PERNR  Type: Select-Option  Reference Field: PA0000-PERNR",
                "S_WERKS  PA0001-WERKS  Personnel Area",
                "P_REPORT Control Type: Radio Button",
                "P_EMAIL Control Type: Radio Button",
                "P_SENDER Reference Type: AD_SMTPADR",
                "CALL_FUNCTION",
                "START PROCESSING RULES",
                "THIS UNKNOWN PROSE HAS UPPERCASE WORDS",
            ]
        )

        analysis = analyze_sap_dependencies(specification, enabled=True, llm_analyzer=invalid_json)
        names = [item["name"] for item in analysis["ddic_objects"]]
        dependency_keys = {(item["kind"], item["name"]) for item in analysis["dependencies"]}

        self.assertEqual(names, ["PA0000", "PA0001"])
        self.assertEqual(analysis["ddic_types"], ["AD_SMTPADR"])
        self.assertIn(("ddic_field", "PA0000-PERNR"), dependency_keys)
        self.assertIn(("ddic_field", "PA0001-WERKS"), dependency_keys)
        self.assertIn(("ddic_type", "AD_SMTPADR"), dependency_keys)
        self.assertNotIn("SELECT", names)
        self.assertNotIn("RADIO", names)
        self.assertNotIn("CALL", names)
        self.assertNotIn("START", names)
        self.assertNotIn("PROCESSING", names)

    def test_llm_vocabulary_ddic_objects_are_rejected_without_typed_spec_evidence(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        ddic_objects=[
                            ddic_object("SELECT"),
                            ddic_object("RADIO"),
                            ddic_object("PA0000"),
                            ddic_object("PA0001"),
                        ]
                    )
                )
            }

        specification = "\n".join(
            [
                "Type: Select-Option",
                "Control Type: Radio Button",
                "Reference Field: PA0000-PERNR",
                "Reference Field: PA0001-WERKS",
            ]
        )
        analysis = analyze_sap_dependencies(specification, enabled=True, llm_analyzer=analyzer)
        names = [item["name"] for item in analysis["ddic_objects"]]
        rejected = {item["name"]: item["reason"] for item in analysis["rejected_analysis_entries"]}

        self.assertEqual(names, ["PA0000", "PA0001"])
        self.assertIn("SELECT", rejected)
        self.assertIn("RADIO", rejected)
        self.assertEqual(rejected["RADIO"], "no explicit DDIC table, structure, or field evidence in specification")

    def test_llm_ddic_objects_are_kept_for_prose_table_evidence(self):
        specification = "\n".join(
            [
                "Personnel number selection based on PA0000.",
                "Payroll area selection based on PA0001.",
                "Read the current PA0001 record for each employee identified from PA0000.",
                "Read the current PA0002 record for each retained employee.",
            ]
        )

        analysis = normalize_dependency_analysis(
            analysis_json(
                ddic_objects=[
                    ddic_object("PA0000"),
                    ddic_object("PA0001"),
                    ddic_object("PA0002"),
                ]
            ),
            specification_text=specification,
        )

        self.assertEqual(
            analysis["ddic_objects"],
            [ddic_object("PA0000"), ddic_object("PA0001"), ddic_object("PA0002")],
        )
        self.assertEqual(analysis["rejected_analysis_entries"], [])

    def test_explicit_table_read_heading_keeps_llm_ddic_object(self):
        specification = "\n".join(
            [
                "## Table Reads",
                "### PA0000",
                "Read Fields:",
                "* PERNR",
                "### PA0002",
                "Read PA0002 as a separate dependent table read using the PA0000 employee numbers.",
                "Read Fields:",
                "* PERNR",
                "* VORNA",
                "* NACHN",
                "WHERE Conditions:",
                "* PERNR = PA0000-PERNR",
                "* BEGDA LE current_date",
                "* ENDDA GE current_date",
            ]
        )

        analysis = normalize_dependency_analysis(
            analysis_json(ddic_objects=[ddic_object("PA0000"), ddic_object("PA0002")]),
            specification,
        )

        self.assertEqual(analysis["ddic_objects"], [ddic_object("PA0000"), ddic_object("PA0002")])
        self.assertFalse([item for item in analysis["rejected_analysis_entries"] if item["name"] == "PA0002"])

    def test_missing_required_dependency_shape_falls_back(self):
        def analyzer(_prompt, _source):
            return {"text": json_dumps({"ddic_objects": []})}

        analysis = analyze_sap_dependencies("Use SAP table EDIDC.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("missing required properties", analysis["_diagnostics"]["error"])

    def test_disabled_analysis_records_disabled_fallback_reason(self):
        analysis = analyze_sap_dependencies("Use field EDIDC-CREDAT.", enabled=False)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "disabled")
        self.assertEqual(analysis["ddic_objects"], [ddic_object("EDIDC")])

    def test_dependency_analysis_prompt_is_dependency_only(self):
        captured = {}

        def analyzer(prompt, _source):
            captured["prompt"] = prompt
            return {"text": "{}"}

        analyze_sap_dependencies("Use SAP table EDIDC.", enabled=True, llm_analyzer=analyzer)

        self.assertIn('"ddic_objects": [', captured["prompt"])
        self.assertIn('"name": "EDIDC"', captured["prompt"])
        self.assertIn('"structure": "st_edidc"', captured["prompt"])
        self.assertIn('"table": "t_edidc"', captured["prompt"])
        self.assertIn('"callables": []', captured["prompt"])
        self.assertIn('"unresolved": []', captured["prompt"])
        self.assertIn("Do not design the implementation.", captured["prompt"])
        self.assertNotIn('"declarations": []', captured["prompt"])
        self.assertNotIn('"operations": []', captured["prompt"])
        self.assertNotIn('"execution_order": []', captured["prompt"])
        self.assertNotIn('"form_names": []', captured["prompt"])
        self.assertNotIn('"local_identifiers": []', captured["prompt"])
        self.assertNotIn("SELECT operation JSON shape", captured["prompt"])
        self.assertNotIn("LOOP operation JSON shape", captured["prompt"])


class LegacySapDependencyAnalysisPlanTests:
    def test_local_structure_is_classified_as_local_not_ddic(self):
        analysis = normalize_dependency_analysis(
            {
                "ddic_objects": [{"name": "ST_OUTPUT", "kind": "structure"}],
                "local_identifiers": ["ST_OUTPUT"],
            },
            "TYPES: BEGIN OF st_output, field TYPE char10, END OF st_output.",
        )

        self.assertEqual(analysis["ddic_objects"], [])
        self.assertIn("ST_OUTPUT", analysis["local_identifiers"])

    def test_external_table_and_callables_are_normalized(self):
        analysis = normalize_dependency_analysis(
            {
                "ddic_objects": [{"name": "edidc", "kind": "table"}, {"name": "EDIDC", "kind": "table"}],
                "callables": ["reuse_alv_grid_display"],
                "classes": [{"name": "cl_bcs", "methods": ["create_persistent", "send"]}],
            }
        )

        self.assertEqual(analysis["ddic_objects"], ["EDIDC"])
        self.assertEqual(
            analysis["callables"],
            ["REUSE_ALV_GRID_DISPLAY", "CL_BCS=>CREATE_PERSISTENT", "CL_BCS=>SEND"],
        )

    def test_analysis_keeps_valid_llm_ddic_candidates_and_filters_local_prose(self):
        analysis = normalize_dependency_analysis(
            {
                "ddic_objects": [
                    {"name": "ZDEMO_ID"},
                    {"name": "MESSAGE_TEXT"},
                    {"name": "EDIDC", "kind": "table"},
                    {"name": "/ABC/DEMO"},
                    {"name": "ST_CUSTOM"},
                    {"name": "LT_OUTPUT"},
                    {"name": "CL_TEST=>RUN"},
                    {"name": "FOR"},
                    {"name": "SY"},
                ],
                "callables": ["BAPI_MESSAGE_GETDETAIL"],
                "local_identifiers": ["OR", "FOR", "BASE", "ROWS", "COLUMN", "LT_OUTPUT"],
            },
            "SAP table EDIDC.",
        )

        self.assertEqual(analysis["ddic_objects"], ["ZDEMO_ID", "MESSAGE_TEXT", "EDIDC", "/ABC/DEMO", "ST_CUSTOM"])
        self.assertEqual(analysis["callables"], ["BAPI_MESSAGE_GETDETAIL"])
        self.assertEqual(analysis["local_identifiers"], ["LT_OUTPUT"])
        rejected = {(item["name"], item["category"], item["reason"]) for item in analysis["rejected_analysis_entries"]}
        self.assertIn(("LT_OUTPUT", "ddic_objects", "local identifier"), rejected)
        self.assertIn(("CL_TEST=>RUN", "ddic_objects", "invalid DDIC table/structure/view identifier"), rejected)
        self.assertIn(("FOR", "ddic_objects", "ABAP keyword"), rejected)
        self.assertIn(("SY", "ddic_objects", "system field"), rejected)
        self.assertIn(("OR", "local_identifiers", "ABAP keyword"), rejected)
        self.assertIn(("BASE", "local_identifiers", "not a declared or local-prefixed identifier"), rejected)

    def test_successful_llm_analysis_does_not_require_deterministic_evidence(self):
        analysis = normalize_dependency_analysis(
            {"ddic_objects": ["EDIDC", "ZDEMO_ID"]},
            "Use field EDIDC-DOCNUM. Use type ZDEMO_ID for the selection parameter.",
        )

        self.assertEqual(analysis["ddic_objects"], ["EDIDC", "ZDEMO_ID"])
        self.assertFalse([item for item in analysis["rejected_analysis_entries"] if item["name"] == "ZDEMO_ID"])

    def test_rejected_local_prose_does_not_block_successful_llm_ddic_candidate(self):
        analysis = normalize_dependency_analysis(
            {"ddic_objects": ["ANY_TABLE"], "local_identifiers": ["ANY_TABLE"]},
            "Create a report.",
        )

        self.assertEqual(analysis["ddic_objects"], ["ANY_TABLE"])
        self.assertEqual(analysis["local_identifiers"], [])
        rejected = [item for item in analysis["rejected_analysis_entries"] if item["name"] == "ANY_TABLE"]
        self.assertEqual(rejected[0]["category"], "local_identifiers")

    def test_invalid_json_falls_back_to_deterministic_extraction(self):
        def invalid_json(_prompt, _source):
            return {"text": "not json"}

        analysis = analyze_sap_dependencies(
            "Use field EDIDC-CREDAT. TYPES st_output TYPE char10.",
            enabled=True,
            llm_analyzer=invalid_json,
        )

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid JSON")
        self.assertEqual(analysis["ddic_objects"], ["EDIDC"])
        self.assertIn("ST_OUTPUT", analysis["local_identifiers"])

    def test_empty_response_records_empty_response_fallback_reason(self):
        def empty_response(_prompt, _source):
            return {"text": ""}

        analysis = analyze_sap_dependencies("Use field EDIDC-CREDAT.", enabled=True, llm_analyzer=empty_response)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "empty response")

    def test_analysis_failure_does_not_block_when_no_external_metadata_required(self):
        def failing_analyzer(_prompt, _source):
            raise RuntimeError("analysis unavailable")

        analysis = analyze_sap_dependencies(
            "Create a simple report. TYPES st_output TYPE char10.",
            enabled=True,
            llm_analyzer=failing_analyzer,
        )

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "LLM call failed")
        self.assertEqual(analysis["ddic_objects"], [])
        self.assertIn("ST_OUTPUT", analysis["local_identifiers"])

    def test_disabled_analysis_records_disabled_fallback_reason(self):
        analysis = analyze_sap_dependencies("Use field EDIDC-CREDAT.", enabled=False)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "disabled")

    def test_successful_empty_analysis_is_not_replaced_by_regex_extraction(self):
        def empty_analysis(_prompt, _source):
            return {"text": json_dumps(analysis_json())}

        analysis = analyze_sap_dependencies(
            "Use field EDIDC-CREDAT. TYPES st_output TYPE char10.",
            enabled=True,
            llm_analyzer=empty_analysis,
        )

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["ddic_objects"], [])
        self.assertEqual(analysis["local_identifiers"], [])

    def test_dependency_analysis_records_prompt_and_raw_response_for_diagnostics(self):
        raw_response = json_dumps(analysis_json(ddic_objects=[{"name": "EDIDC", "kind": "table"}]))

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Use SAP table EDIDC.", enabled=True, llm_analyzer=analyzer)

        self.assertIn("Return SAP dependency analysis as structured JSON only", analysis["_diagnostics"]["prompt"])
        self.assertIn('"operations": []', analysis["_diagnostics"]["prompt"])
        self.assertIn('"form_names": []', analysis["_diagnostics"]["prompt"])
        self.assertIn('"target_table"', analysis["_diagnostics"]["prompt"])
        self.assertIn('"database_field"', analysis["_diagnostics"]["prompt"])
        self.assertEqual(analysis["_diagnostics"]["input"][0]["content"], analysis["_diagnostics"]["prompt"])
        self.assertEqual(analysis["_diagnostics"]["input"][1]["content"], "Use SAP table EDIDC.")
        self.assertEqual(analysis["_diagnostics"]["raw_response"], raw_response)
        self.assertEqual(analysis["ddic_objects"], ["EDIDC"])

    def test_dependency_analysis_rejects_missing_required_properties(self):
        def analyzer(_prompt, _source):
            return {"text": '{"ddic_objects": []}'}

        analysis = analyze_sap_dependencies("Use SAP table EDIDC.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("missing required properties", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_rejects_unknown_ddic_reference(self):
        def analyzer(_prompt, _source):
            return {"text": json_dumps(analysis_json(operations=[select_operation(source_table="UNKNOWN_TABLE")]))}

        analysis = analyze_sap_dependencies("Use SAP table EDIDC.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"], [])
        self.assertIn("unknown DDIC object", analysis["_diagnostics"]["rejected_operations"][0]["reason"])

    def test_dependency_analysis_rejects_unknown_fields(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        ddic_objects=[{"name": "EDIDC", "kind": "table", "fields": ["DOCNUM"]}],
                        operations=[select_operation(fields=["MESTYP"])],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Use SAP table EDIDC.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"], [])
        self.assertIn("unknown field", analysis["_diagnostics"]["rejected_operations"][0]["reason"])

    def test_dependency_analysis_rejects_descriptive_select_properties(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        ddic_objects=[{"name": "EDIDC", "kind": "table", "fields": ["DOCNUM"]}],
                        operations=[{**select_operation(), "where": "selected EDIDC DOCNUMs"}],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Use SAP table EDIDC.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"], [])
        self.assertIn("unsupported descriptive properties: where", analysis["_diagnostics"]["rejected_operations"][0]["reason"])

    def test_dependency_analysis_rejects_invalid_operation_without_full_fallback(self):
        valid_select = select_operation(
            id="select_edidc",
            source_table="EDIDC",
            target_table="t_edidc",
            target_work_area="w_edidc",
            fields=["DOCNUM"],
        )
        invalid_append = {
            "operation": "append",
            "id": "append_missing_source",
            "form_name": "append_output",
            "target_table": "t_output",
            "access_method": "APPEND",
            "cardinality": "SINGLE",
        }
        raw_response = json_dumps(
            analysis_json(
                ddic_objects=[{"name": "EDIDC", "kind": "table", "fields": ["DOCNUM"]}],
                callables=["BAPI_MESSAGE_GETDETAIL"],
                declarations=[
                    {"name": "st_edidc", "kind": "structure", "source_table": "EDIDC", "components": [{"name": "DOCNUM", "abap_type": "EDIDC-DOCNUM"}]},
                    {"name": "t_edidc", "kind": "internal_table", "line_type": "st_edidc"},
                    {"name": "w_edidc", "kind": "work_area", "abap_type": "st_edidc"},
                    {"name": "st_output", "kind": "structure", "components": [{"name": "DOCNUM", "abap_type": "EDIDC-DOCNUM"}]},
                    {"name": "t_output", "kind": "internal_table", "line_type": "st_output"},
                ],
                operations=[valid_select, invalid_append],
            )
        )

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Use SAP table EDIDC and BAPI_MESSAGE_GETDETAIL.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["callables"], ["BAPI_MESSAGE_GETDETAIL"])
        self.assertEqual(analysis["ddic_objects"], ["EDIDC"])
        self.assertEqual([operation["id"] for operation in analysis["operations"]], ["select_edidc"])
        rejected = analysis["_diagnostics"]["rejected_operations"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["operation"], invalid_append)
        self.assertIn("append operation is missing required properties: source", rejected[0]["reason"])

    def test_dependency_analysis_rejects_free_text_execution_steps(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        operations=[move_operation(id="move_output")],
                        execution_order=[
                            {
                                "order": 1,
                                "step": "Move data into output structure",
                                "operation_ref": "move_output",
                                "form_name": "display_data",
                            }
                        ],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create a report.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("unsupported descriptive properties", analysis["_diagnostics"]["error"])
        self.assertIn("step", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_accepts_structured_machine_readable_plan(self):
        raw_response = json_dumps(
            analysis_json(
                ddic_objects=[
                    {"name": "EDIDC", "kind": "table", "fields": ["DOCNUM"]},
                    {"name": "EDID4", "kind": "table", "fields": ["DOCNUM", "SDATA"]},
                ],
                declarations=[
                    {"name": "edidc", "kind": "tables_declaration", "source_table": "EDIDC"},
                    {
                        "name": "st_output",
                        "kind": "structure",
                        "components": [{"name": "docnum", "abap_type": "EDIDC-DOCNUM"}],
                    },
                    {"name": "st_edidc", "kind": "structure", "source_table": "EDIDC", "components": [{"name": "DOCNUM", "abap_type": "EDIDC-DOCNUM"}]},
                    {"name": "st_edid4", "kind": "structure", "source_table": "EDID4", "components": [{"name": "DOCNUM", "abap_type": "EDID4-DOCNUM"}]},
                    {"name": "t_edidc", "kind": "internal_table", "line_type": "st_edidc"},
                    {"name": "w_edidc", "kind": "work_area", "abap_type": "st_edidc"},
                    {"name": "t_edid4", "kind": "internal_table", "line_type": "st_edid4"},
                    {"name": "w_edid4", "kind": "work_area", "abap_type": "st_edid4"},
                    {"name": "w_gen0007", "kind": "work_area", "abap_type": "st_output"},
                ],
                operations=[
                    select_operation(id="select_edidc", source_table="EDIDC", target_table="t_edidc", target_work_area="w_edidc"),
                    select_operation(
                        id="select_edid4",
                        form_name="select_idoc_data",
                        source_table="EDID4",
                        target_table="t_edid4",
                        target_work_area="w_edid4",
                        fields=["DOCNUM", "SDATA"],
                        access_method="FOR_ALL_ENTRIES",
                        driving_table="t_edidc",
                        conditions=[
                            {
                                "database_field": "DOCNUM",
                                "operator": "=",
                                "value_type": "TABLE_FIELD",
                                "source_table": "t_edidc",
                                "source_field": "DOCNUM",
                            }
                        ],
                        initial_table_guard={"table": "t_edidc", "required": True},
                    ),
                    move_operation(id="move_payload", form_name="build_output", source="w_edid4-sdata", target="w_gen0007-payload"),
                ],
                form_names=["select_data", "select_idoc_data", "build_output"],
                execution_order=[
                    {"operation_ref": "select_edidc"},
                    {"operation_ref": "select_edid4"},
                    {"operation_ref": "move_payload"},
                ],
            )
        )

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Use SAP table EDIDC and EDID4.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["raw_response"], raw_response)
        self.assertEqual(analysis["ddic_objects"], ["EDIDC", "EDID4"])

    def test_dependency_analysis_accepts_supported_select_condition_types(self):
        raw_response = json_dumps(
            analysis_json(
                ddic_objects=[
                    {
                        "name": "ZMD_TABLE",
                        "kind": "table",
                        "fields": ["CREDAT", "DOCNUM", "ZMDID", "ZHOST"],
                    }
                ],
                declarations=[
                    {"name": "st_zmd_table", "kind": "structure", "source_table": "ZMD_TABLE", "components": [{"name": "DOCNUM", "abap_type": "ZMD_TABLE-DOCNUM"}]},
                    {"name": "st_edidc", "kind": "structure", "source_table": "EDIDC", "components": [{"name": "DOCNUM", "abap_type": "EDIDC-DOCNUM"}]},
                    {"name": "t_zmd_table", "kind": "internal_table", "line_type": "st_zmd_table"},
                    {"name": "w_zmd_table", "kind": "work_area", "abap_type": "st_zmd_table"},
                    {"name": "t_edidc", "kind": "internal_table", "line_type": "st_edidc"},
                    {"name": "s_credat", "kind": "select_option", "abap_type": "ZMD_TABLE-CREDAT"},
                    {"name": "p_zmdid", "kind": "parameter", "abap_type": "ZMD_TABLE-ZMDID"},
                ],
                operations=[
                    select_operation(
                        source_table="ZMD_TABLE",
                        target_table="t_zmd_table",
                        target_work_area="w_zmd_table",
                        fields=["CREDAT", "DOCNUM", "ZMDID", "ZHOST"],
                        conditions=[
                            {
                                "database_field": "CREDAT",
                                "operator": "IN",
                                "value_type": "SELECTION_OPTION",
                                "value": "s_credat",
                            },
                            {
                                "database_field": "DOCNUM",
                                "operator": "=",
                                "value_type": "TABLE_FIELD",
                                "source_table": "t_edidc",
                                "source_field": "DOCNUM",
                            },
                            {
                                "database_field": "ZMDID",
                                "operator": "=",
                                "value_type": "IDENTIFIER",
                                "value": "p_zmdid",
                            },
                            {
                                "database_field": "ZHOST",
                                "operator": "=",
                                "value_type": "CONSTANT",
                                "value": "ISAPDB",
                            },
                        ],
                    )
                ],
            )
        )

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Use SAP table ZMD_TABLE.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"][0]["conditions"][0]["value_type"], "SELECTION_OPTION")
        self.assertNotIn("source_field", analysis["operations"][0]["conditions"][0])

    def test_dependency_analysis_accepts_select_single_work_area_target(self):
        raw_response = json_dumps(
            analysis_json(
                ddic_objects=[{"name": "ZMD_GEN0007", "kind": "table", "fields": ["DOCNUM"]}],
                declarations=[
                    {"name": "st_gen0007", "kind": "structure", "source_table": "ZMD_GEN0007", "components": [{"name": "DOCNUM", "abap_type": "ZMD_GEN0007-DOCNUM"}]},
                    {"name": "w_gen0007", "kind": "work_area", "abap_type": "st_gen0007"},
                ],
                operations=[
                    select_operation(
                        id="select_gen0007",
                        source_table="ZMD_GEN0007",
                        target_table="w_gen0007",
                        target_work_area="w_gen0007",
                        fields=["DOCNUM"],
                        cardinality="SINGLE",
                        access_method="SELECT_SINGLE",
                    )
                ],
            )
        )

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Use SAP table ZMD_GEN0007 field DOCNUM.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"][0]["target_table"], "w_gen0007")
        self.assertEqual(analysis["operations"][0]["target_work_area"], "w_gen0007")

    def test_dependency_analysis_accepts_structured_read_operation(self):
        raw_response = json_dumps(
            analysis_json(
                declarations=[
                    {"name": "st_result", "kind": "structure", "source_table": "EDIDC", "components": [{"name": "DOCNUM", "abap_type": "EDIDC-DOCNUM"}]},
                    {"name": "t_result", "kind": "internal_table", "line_type": "st_result"},
                    {"name": "w_result", "kind": "work_area", "abap_type": "st_result"},
                    {"name": "w_source", "kind": "work_area", "abap_type": "st_result"},
                ],
                operations=[read_operation()],
                form_names=["read_existing_row"],
                execution_order=[{"operation_ref": "read_existing_row"}],
            )
        )

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Read an existing row from an internal table.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"][0]["operation"], "read")
        self.assertEqual(analysis["operations"][0]["key_fields"][0]["value"], "w_source-DOCNUM")

    def test_dependency_analysis_allows_select_without_target_work_area(self):
        operation = select_operation(
            source_table="EDIDC",
            target_table="t_edidc",
            fields=["DOCNUM"],
            access_method="SELECT",
            cardinality="MULTIPLE",
        )
        del operation["target_work_area"]
        raw_response = json_dumps(
            analysis_json(
                ddic_objects=[{"name": "EDIDC", "kind": "table", "fields": ["DOCNUM"]}],
                declarations=[
                    {"name": "st_edidc", "kind": "structure", "source_table": "EDIDC", "components": [{"name": "DOCNUM", "abap_type": "EDIDC-DOCNUM"}]},
                    {"name": "t_edidc", "kind": "internal_table", "line_type": "st_edidc"},
                ],
                operations=[operation],
            )
        )

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Use SAP table EDIDC field DOCNUM.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertNotIn("target_work_area", analysis["operations"][0])

    def test_dependency_analysis_allows_for_all_entries_null_target_work_area(self):
        raw_response = json_dumps(
            analysis_json(
                ddic_objects=[
                    {"name": "EDID4", "kind": "table", "fields": ["DOCNUM"]},
                ],
                declarations=[
                    {"name": "st_edidc", "kind": "structure", "source_table": "EDIDC", "components": [{"name": "DOCNUM", "abap_type": "EDIDC-DOCNUM"}]},
                    {"name": "st_edid4", "kind": "structure", "source_table": "EDID4", "components": [{"name": "DOCNUM", "abap_type": "EDID4-DOCNUM"}]},
                    {"name": "t_edidc", "kind": "internal_table", "line_type": "st_edidc"},
                    {"name": "t_edid4", "kind": "internal_table", "line_type": "st_edid4"},
                ],
                operations=[
                    select_operation(
                        id="select_edid4",
                        source_table="EDID4",
                        target_table="t_edid4",
                        target_work_area=None,
                        fields=["DOCNUM"],
                        access_method="FOR_ALL_ENTRIES",
                        driving_table="t_edidc",
                        conditions=[
                            {
                                "database_field": "DOCNUM",
                                "operator": "=",
                                "value_type": "TABLE_FIELD",
                                "source_table": "t_edidc",
                                "source_field": "DOCNUM",
                            }
                        ],
                        initial_table_guard={"table": "t_edidc", "required": True},
                    )
                ],
            )
        )

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Use SAP table EDID4 field DOCNUM.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertIsNone(analysis["operations"][0]["target_work_area"])

    def test_dependency_analysis_rejects_select_single_without_target_work_area(self):
        operation = select_operation(
            source_table="ZMD_GEN0007",
            target_table="w_gen0007",
            fields=["DOCNUM"],
            cardinality="SINGLE",
            access_method="SELECT_SINGLE",
        )
        del operation["target_work_area"]

        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        ddic_objects=[{"name": "ZMD_GEN0007", "kind": "table", "fields": ["DOCNUM"]}],
                        declarations=[
                            {"name": "st_gen0007", "kind": "structure", "source_table": "ZMD_GEN0007", "components": [{"name": "DOCNUM", "abap_type": "ZMD_GEN0007-DOCNUM"}]},
                            {"name": "w_gen0007", "kind": "work_area", "abap_type": "st_gen0007"},
                        ],
                        operations=[operation],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Use SAP table ZMD_GEN0007 field DOCNUM.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"], [])
        self.assertIn("invalid target_work_area", analysis["_diagnostics"]["rejected_operations"][0]["reason"])

    def test_dependency_analysis_rejects_select_single_internal_table_target(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        ddic_objects=[{"name": "ZMD_GEN0007", "kind": "table", "fields": ["DOCNUM"]}],
                        declarations=[
                            {"name": "st_gen0007", "kind": "structure", "source_table": "ZMD_GEN0007", "components": [{"name": "DOCNUM", "abap_type": "ZMD_GEN0007-DOCNUM"}]},
                            {"name": "t_gen0007", "kind": "internal_table", "line_type": "st_gen0007"},
                            {"name": "w_gen0007", "kind": "work_area", "abap_type": "st_gen0007"},
                        ],
                        operations=[
                            select_operation(
                                source_table="ZMD_GEN0007",
                                target_table="t_gen0007",
                                target_work_area="w_gen0007",
                                fields=["DOCNUM"],
                                cardinality="SINGLE",
                                access_method="SELECT_SINGLE",
                            )
                        ],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Use SAP table ZMD_GEN0007 field DOCNUM.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"], [])
        self.assertIn("SELECT_SINGLE target_table must be the declared target_work_area", analysis["_diagnostics"]["rejected_operations"][0]["reason"])

    def test_dependency_analysis_rejects_select_single_target_not_declared_as_work_area(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        ddic_objects=[{"name": "ZMD_GEN0007", "kind": "table", "fields": ["DOCNUM"]}],
                        declarations=[
                            {"name": "st_gen0007", "kind": "structure", "source_table": "ZMD_GEN0007", "components": [{"name": "DOCNUM", "abap_type": "ZMD_GEN0007-DOCNUM"}]},
                            {"name": "w_gen0007", "kind": "internal_table", "line_type": "st_gen0007"},
                        ],
                        operations=[
                            select_operation(
                                source_table="ZMD_GEN0007",
                                target_table="w_gen0007",
                                target_work_area="w_gen0007",
                                fields=["DOCNUM"],
                                cardinality="SINGLE",
                                access_method="SELECT_SINGLE",
                            )
                        ],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Use SAP table ZMD_GEN0007 field DOCNUM.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"], [])
        self.assertIn("SELECT_SINGLE target work area is not declared as a work_area", analysis["_diagnostics"]["rejected_operations"][0]["reason"])

    def test_dependency_analysis_keeps_table_field_condition_strict(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        ddic_objects=[{"name": "EDID4", "kind": "table", "fields": ["DOCNUM"]}],
                        declarations=[
                            {"name": "st_edid4", "kind": "structure", "source_table": "EDID4", "components": [{"name": "DOCNUM", "abap_type": "EDID4-DOCNUM"}]},
                            {"name": "t_edid4", "kind": "internal_table", "line_type": "st_edid4"},
                            {"name": "w_edid4", "kind": "work_area", "abap_type": "st_edid4"},
                        ],
                        operations=[
                            select_operation(
                                source_table="EDID4",
                                target_table="t_edid4",
                                target_work_area="w_edid4",
                                conditions=[
                                    {
                                        "database_field": "DOCNUM",
                                        "operator": "=",
                                        "value_type": "TABLE_FIELD",
                                        "value": "t_edidc-DOCNUM",
                                    }
                                ],
                            )
                        ],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Use SAP table EDID4.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"], [])
        self.assertIn("TABLE_FIELD select condition is missing required properties", analysis["_diagnostics"]["rejected_operations"][0]["reason"])

    def test_dependency_analysis_requires_initial_guard_for_for_all_entries(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        ddic_objects=[{"name": "EDID4", "kind": "table", "fields": ["DOCNUM"]}],
                        declarations=[
                            {"name": "st_edidc", "kind": "structure", "source_table": "EDIDC", "components": [{"name": "DOCNUM", "abap_type": "EDIDC-DOCNUM"}]},
                            {"name": "st_edid4", "kind": "structure", "source_table": "EDID4", "components": [{"name": "DOCNUM", "abap_type": "EDID4-DOCNUM"}]},
                            {"name": "t_edidc", "kind": "internal_table", "line_type": "st_edidc"},
                            {"name": "t_edid4", "kind": "internal_table", "line_type": "st_edid4"},
                        ],
                        operations=[
                            select_operation(
                                id="select_edid4",
                                source_table="EDID4",
                                target_table="t_edid4",
                                target_work_area="w_edid4",
                                fields=["DOCNUM"],
                                access_method="FOR_ALL_ENTRIES",
                                driving_table="t_edidc",
                                conditions=[
                                    {
                                        "database_field": "DOCNUM",
                                        "operator": "=",
                                        "value_type": "TABLE_FIELD",
                                        "source_table": "t_edidc",
                                        "source_field": "DOCNUM",
                                    }
                                ],
                            )
                        ],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Use SAP table EDID4.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"], [])
        self.assertIn("initial-table guard", analysis["_diagnostics"]["rejected_operations"][0]["reason"])

    def test_dependency_analysis_rejects_duplicate_declarations(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        declarations=[
                            {"name": "t_output", "kind": "internal_table"},
                            {"name": "t_output", "kind": "internal_table"},
                        ]
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create a report.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("duplicate declarations", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_rejects_internal_table_line_type_work_area(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        declarations=[
                            {"name": "st_result", "kind": "structure", "components": [{"name": "FIELD1", "abap_type": "CHAR10"}]},
                            {"name": "w_result", "kind": "work_area", "abap_type": "st_result"},
                            {"name": "t_result", "kind": "internal_table", "line_type": "w_result"},
                        ]
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create output processing.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("line_type references a work area", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_rejects_structure_name_without_st_prefix(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        declarations=[
                            {"name": "ty_result", "kind": "structure", "components": [{"name": "FIELD1", "abap_type": "CHAR10"}]},
                        ]
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create output processing.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("structure declaration name must begin with st_", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_rejects_anonymous_internal_table_line_type(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        declarations=[
                            {"name": "st_result", "kind": "structure", "components": [{"name": "FIELD1", "abap_type": "CHAR10"}]},
                            {"name": "t_result", "kind": "internal_table", "line_type": {"name": "st_result"}},
                        ]
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create output processing.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("line_type must be a string identifier", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_rejects_internal_table_line_type_without_declared_structure(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        declarations=[
                            {"name": "t_result", "kind": "internal_table", "line_type": "st_result"},
                        ]
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create output processing.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("line_type must reference a declared structure", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_rejects_work_area_abap_type_without_declared_structure(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        declarations=[
                            {"name": "w_result", "kind": "work_area", "abap_type": "st_result"},
                        ]
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create output processing.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("work area abap_type must reference a declared structure", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_rejects_work_area_with_duplicate_components(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        declarations=[
                            {"name": "st_result", "kind": "structure", "components": [{"name": "FIELD1", "abap_type": "CHAR10"}]},
                            {
                                "name": "w_result",
                                "kind": "work_area",
                                "abap_type": "st_result",
                                "components": [{"name": "FIELD1", "abap_type": "CHAR10"}],
                            },
                        ]
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create output processing.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("work area declaration must not duplicate structure components", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_accepts_loop_operation_schema(self):
        raw_response = json_dumps(
            analysis_json(
                declarations=[
                    {"name": "st_result", "kind": "structure", "components": [{"name": "FIELD1", "abap_type": "CHAR10"}]},
                    {"name": "t_result", "kind": "internal_table", "line_type": "st_result"},
                    {"name": "w_result", "kind": "work_area", "abap_type": "st_result"},
                ],
                operations=[
                    {
                        "id": "loop_result",
                        "form_name": "loop_result",
                        "operation": "loop",
                        "access_method": "LOOP",
                        "cardinality": "MULTIPLE",
                        "table": "t_result",
                        "target_work_area": "w_result",
                    }
                ],
            )
        )

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Loop over output rows.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"][0]["operation"], "loop")

    def test_dependency_analysis_rejects_loop_table_not_internal_table(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        declarations=[
                            {"name": "st_result", "kind": "structure", "components": [{"name": "FIELD1", "abap_type": "CHAR10"}]},
                            {"name": "w_result", "kind": "work_area", "abap_type": "st_result"},
                        ],
                        operations=[
                            {
                                "id": "loop_result",
                                "form_name": "loop_result",
                                "operation": "loop",
                                "access_method": "LOOP",
                                "cardinality": "MULTIPLE",
                                "table": "w_result",
                                "target_work_area": "w_result",
                            }
                        ],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Loop over output rows.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"], [])
        self.assertIn("loop table must reference a declared internal table", analysis["_diagnostics"]["rejected_operations"][0]["reason"])

    def test_dependency_analysis_rejects_loop_target_not_work_area(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        declarations=[
                            {"name": "st_result", "kind": "structure", "components": [{"name": "FIELD1", "abap_type": "CHAR10"}]},
                            {"name": "t_result", "kind": "internal_table", "line_type": "st_result"},
                        ],
                        operations=[
                            {
                                "id": "loop_result",
                                "form_name": "loop_result",
                                "operation": "loop",
                                "access_method": "LOOP",
                                "cardinality": "MULTIPLE",
                                "table": "t_result",
                                "target_work_area": "t_result",
                            }
                        ],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Loop over output rows.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["operations"], [])
        self.assertIn("loop target_work_area must reference a declared work area", analysis["_diagnostics"]["rejected_operations"][0]["reason"])

    def test_dependency_analysis_accepts_execution_order_operation_ref(self):
        raw_response = json_dumps(
            analysis_json(
                operations=[move_operation(id="move_output")],
                form_names=["display_data"],
                execution_order=[{"operation_ref": "move_output"}],
            )
        )

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Create a report.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["execution_order"], [{"operation_ref": "move_output"}])

    def test_dependency_analysis_accepts_execution_order_form_name(self):
        raw_response = json_dumps(
            analysis_json(
                operations=[move_operation(id="move_output")],
                form_names=["display_data"],
                execution_order=[{"form_name": "display_data"}],
            )
        )

        def analyzer(_prompt, _source):
            return {"text": raw_response}

        analysis = analyze_sap_dependencies("Create a report.", enabled=True, llm_analyzer=analyzer)

        self.assertFalse(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["execution_order"], [{"form_name": "display_data"}])

    def test_dependency_analysis_rejects_execution_order_missing_operation_ref_and_form_name(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        operations=[move_operation(id="move_output")],
                        form_names=["display_data"],
                        execution_order=[{}],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create a report.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("exactly one of operation_ref or form_name", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_rejects_execution_order_with_both_operation_ref_and_form_name(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        operations=[move_operation(id="move_output")],
                        form_names=["display_data"],
                        execution_order=[{"operation_ref": "move_output", "form_name": "display_data"}],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create a report.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("exactly one of operation_ref or form_name", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_rejects_unknown_execution_order_operation_ref(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        operations=[move_operation(id="move_output")],
                        form_names=["display_data"],
                        execution_order=[{"operation_ref": "missing_operation"}],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create a report.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("references unknown operation", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_rejects_unknown_execution_order_form_name(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        operations=[move_operation(id="move_output")],
                        form_names=["display_data"],
                        execution_order=[{"form_name": "missing_form"}],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create a report.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("references unknown FORM", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_rejects_deprecated_execution_order_operation_id_shape(self):
        def analyzer(_prompt, _source):
            return {
                "text": json_dumps(
                    analysis_json(
                        operations=[move_operation(id="move_output")],
                        form_names=["display_data"],
                        execution_order=[
                            {"operation": "move", "id": "move_output"},
                        ],
                    )
                )
            }

        analysis = analyze_sap_dependencies("Create a report.", enabled=True, llm_analyzer=analyzer)

        self.assertTrue(analysis["_diagnostics"]["used_fallback"])
        self.assertEqual(analysis["_diagnostics"]["fallback_reason"], "invalid dependency analysis JSON")
        self.assertIn("unsupported descriptive properties", analysis["_diagnostics"]["error"])

    def test_dependency_analysis_prompt_does_not_generate_abap(self):
        captured = {}

        def analyzer(prompt, _source):
            captured["prompt"] = prompt
            return {"text": "{}"}

        analyze_sap_dependencies("Use SAP table EDIDC.", enabled=True, llm_analyzer=analyzer)

        self.assertIn("Do not generate any ABAP.", captured["prompt"])
        self.assertIn('"declarations": []', captured["prompt"])
        self.assertIn('"execution_order": []', captured["prompt"])
        self.assertIn("Every SELECT operation must include id, form_name, source_table, target_table", captured["prompt"])
        self.assertIn("Do not use descriptive text", captured["prompt"])
        self.assertIn("execution_order must be generic and must reference previously defined operations or FORM names.", captured["prompt"])
        self.assertIn("Each execution_order entry must contain exactly one of operation_ref or form_name.", captured["prompt"])
        self.assertIn("operation_ref must match an id from the operations array.", captured["prompt"])
        self.assertIn("form_name must match a value from the form_names array.", captured["prompt"])
        self.assertIn("Do not use generic properties such as operation, operations, id, step or description in execution_order entries.", captured["prompt"])
        self.assertIn('"id": "read_primary_data"', captured["prompt"])
        self.assertIn('"id": "process_result"', captured["prompt"])
        self.assertIn('"form_names": [', captured["prompt"])
        self.assertIn('"operation_ref": "read_primary_data"', captured["prompt"])
        self.assertIn('"operation_ref": "process_result"', captured["prompt"])
        self.assertIn('"source_table": "ZMD_GEN0007"', captured["prompt"])
        self.assertIn('"target_table": "w_gen0007"', captured["prompt"])
        self.assertIn('"target_work_area": "w_gen0007"', captured["prompt"])
        self.assertIn('"cardinality": "SINGLE"', captured["prompt"])
        self.assertIn('"access_method": "SELECT_SINGLE"', captured["prompt"])
        self.assertIn("For SELECT and FOR_ALL_ENTRIES, target_work_area is optional and may be omitted or null.", captured["prompt"])
        self.assertIn("database_field values in conditions must exactly match field names written in the specification", captured["prompt"])
        self.assertIn('value_type "SELECTION_OPTION"', captured["prompt"])
        self.assertIn('value_type "TABLE_FIELD"', captured["prompt"])
        self.assertIn('value_type "IDENTIFIER"', captured["prompt"])
        self.assertIn('value_type "CONSTANT"', captured["prompt"])
        self.assertIn("Do not represent selection options using source_field LOW.", captured["prompt"])
        self.assertIn("Every CALL_FUNCTION operation must include callable and parameters.", captured["prompt"])
        self.assertIn("Do not leave exporting, importing, changing and tables all empty", captured["prompt"])
        self.assertIn("Put each parameter in the section supported by metadata", captured["prompt"])
        self.assertIn('"operation": "call_function"', captured["prompt"])
        self.assertIn('"access_method": "CALL_FUNCTION"', captured["prompt"])
        self.assertIn('"callable": "BAPI_MESSAGE_GETDETAIL"', captured["prompt"])
        self.assertIn('"parameters": {', captured["prompt"])
        self.assertIn('"exporting": []', captured["prompt"])
        self.assertIn('"importing": []', captured["prompt"])
        self.assertIn('"changing": []', captured["prompt"])
        self.assertIn('"tables": []', captured["prompt"])
        self.assertIn("Only include parameter names explicitly supported by the functional specification or callable metadata.", captured["prompt"])
        self.assertIn("Do not invent function-module parameters.", captured["prompt"])
        self.assertIn('Every structure declaration must use kind "structure"', captured["prompt"])
        self.assertIn("Structure declaration shape:", captured["prompt"])
        self.assertIn('"kind": "structure"', captured["prompt"])
        self.assertIn("line_type as a string identifier that references a declared st_* structure", captured["prompt"])
        self.assertIn("Never use a DDIC object, a work-area identifier, or an inline or anonymous object as line_type.", captured["prompt"])
        self.assertIn("Do not duplicate structure components inside the work_area declaration.", captured["prompt"])
        self.assertIn("When output processing is required, declare all three output artifacts separately", captured["prompt"])
        self.assertIn("Scalar CSV record variables", captured["prompt"])
        self.assertIn("Add explicit structured READ, LOOP, MOVE, APPEND, ALV and CSV operations", captured["prompt"])
        self.assertIn("Every READ operation must represent an explicit READ TABLE requirement", captured["prompt"])
        self.assertIn('"operation": "read"', captured["prompt"])
        self.assertIn('"access_method": "READ"', captured["prompt"])
        self.assertIn("Every LOOP operation must include id, form_name, operation, access_method, cardinality, table and target_work_area.", captured["prompt"])
        self.assertIn('"operation": "loop"', captured["prompt"])
        self.assertIn('"access_method": "LOOP"', captured["prompt"])
        self.assertIn("When an operation already exists for the intended processing step", captured["prompt"])
        self.assertIn("Do not represent the same operation only as form_name.", captured["prompt"])
        self.assertIn("Do not put data elements", captured["prompt"])
        self.assertNotIn("REPORT z", captured["prompt"].lower())


if __name__ == "__main__":
    unittest.main()


def json_dumps(payload):
    import json

    return json.dumps(payload)


def select_operation(**overrides):
    operation = {
        "operation": "select",
        "id": "select_edidc",
        "form_name": "select_data",
        "source_table": "EDIDC",
        "target_table": "t_edidc",
        "target_work_area": "w_edidc",
        "fields": ["DOCNUM"],
        "cardinality": "MULTIPLE",
        "access_method": "SELECT",
        "conditions": [],
    }
    operation.update(overrides)
    return operation


def move_operation(**overrides):
    operation = {
        "operation": "move",
        "id": "move_output",
        "form_name": "display_data",
        "source": "w_a",
        "target": "w_b",
        "source_table": "w_a",
        "target_work_area": "w_b",
        "fields": ["FIELD1"],
        "access_method": "MOVE",
        "cardinality": "SINGLE",
    }
    operation.update(overrides)
    return operation


def read_operation(**overrides):
    operation = {
        "operation": "read",
        "id": "read_existing_row",
        "form_name": "read_existing_row",
        "table": "t_result",
        "target_work_area": "w_result",
        "key_fields": [{"table_field": "DOCNUM", "value": "w_source-DOCNUM"}],
        "access_method": "READ",
        "cardinality": "SINGLE",
    }
    operation.update(overrides)
    return operation
