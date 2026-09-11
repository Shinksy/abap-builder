import unittest

from services.callable_generator import callable_call_statements as generator_callable_call_statements
from services.declaration_generator import (
    data_declarations as generator_data_declarations,
    type_declarations as generator_type_declarations,
)
from services.field_catalog_generator import deterministic_alv_field_catalogue_entries
from services.generation_contract import (
    build_structured_generation_contract,
    callable_call_statements,
    data_declarations,
    database_read_statements,
    form_headers,
    function_module_call_statements,
    main_processing_flow,
    selection_screen_declarations,
    validate_selection_screen_generation_contract,
    type_declarations,
    validate_generation_contract,
)


class GenerationContractTest(unittest.TestCase):
    def metadata(self):
        return {
            "tables": {
                "EDIDC": {
                    "fields": {
                        "DOCNUM": {"datatype": "NUMC", "length": 16},
                        "CREDAT": {"datatype": "DATS", "length": 8},
                    }
                },
                "BAPIRET2": {"fields": {"MESSAGE": {"datatype": "CHAR", "length": 220}}},
            }
        }

    def callable_metadata(self):
        return {
            "callable_signatures": {
                "Z_IDOC_STATUS": {
                    "parameters": {
                        "IV_DOCNUM": {"direction": "IMPORTING", "abap_type": "EDIDC-DOCNUM", "required": True},
                        "EV_MESSAGE": {"direction": "EXPORTING", "abap_type": "BAPIRET2-MESSAGE", "required": False},
                    }
                },
                "ZCL_IDOC_HELPER=>NORMALIZE": {
                    "parameters": {
                        "IV_DOCNUM": {"direction": "IMPORTING", "abap_type": "EDIDC-DOCNUM", "required": True},
                    },
                    "returning": {"name": "RV_DOCNUM", "direction": "RETURNING", "abap_type": "EDIDC-DOCNUM"},
                },
            }
        }

    def requirements(self):
        return {
            "report_name": "z_idoc_report",
            "parameters": [{"name": "p_limit", "type_or_like": "TYPE i"}],
            "select_options": [{"name": "s_docnum", "for_field": "EDIDC-DOCNUM"}],
            "global_variables": [
                {"name": "gv_message", "declaration": "TYPE BAPIRET2-MESSAGE"},
                {"name": "gv_docnum", "declaration": "TYPE EDIDC-DOCNUM"},
            ],
            "output_structure_fields": [
                {"name": "DOCNUM", "type_or_like": "TYPE EDIDC-DOCNUM"},
                {"name": "MESSAGE", "type_or_like": "TYPE BAPIRET2-MESSAGE"},
            ],
        }

    def plan(self):
        return {
            "processing_steps": [
                {
                    "operation": "SELECT",
                    "table": "EDIDC",
                    "fields": ["DOCNUM", "CREDAT"],
                    "target_table": "t_edidc",
                    "work_area": "w_edidc",
                    "where": [{"field": "DOCNUM", "operator": "IN", "value": "s_docnum"}],
                },
                {"operation": "PERFORM", "name": "read_edidc"},
                {
                    "operation": "CALL_FUNCTION",
                    "name": "Z_IDOC_STATUS",
                    "input_parameters": {"IV_DOCNUM": "gv_docnum"},
                    "output_parameters": {"EV_MESSAGE": "gv_message"},
                },
                {
                    "operation": "CALL_STATIC_METHOD",
                    "class": "ZCL_IDOC_HELPER",
                    "method": "NORMALIZE",
                    "input_parameters": {"IV_DOCNUM": "gv_docnum"},
                    "returning_parameter": "gv_docnum",
                },
                {"operation": "APPEND", "source": "w_output", "target": "t_output"},
            ]
        }

    def contract(self):
        return build_structured_generation_contract(
            self.requirements(),
            self.plan(),
            ddic_metadata=self.metadata(),
            callable_metadata=self.callable_metadata(),
        )

    def test_contract_represents_generation_surfaces(self):
        contract = self.contract()

        self.assertEqual(contract["program_name"], "Z_IDOC_REPORT")
        self.assertEqual(contract["selection_screen"][0]["name"], "p_limit")
        self.assertEqual(contract["database_reads"][0]["table"], "EDIDC")
        self.assertEqual(contract["form_routines"], [{"name": "read_edidc"}])
        self.assertEqual(contract["perform_calls"], [{"name": "read_edidc"}])
        self.assertEqual(contract["function_module_calls"][0]["name"], "Z_IDOC_STATUS")
        self.assertEqual(contract["class_method_calls"][0]["name"], "ZCL_IDOC_HELPER=>NORMALIZE")
        self.assertEqual(contract["output_structures"][0]["name"], "ty_output")
        self.assertTrue(contract["validation"]["valid"])

    def test_responsibility_generators_preserve_contract_outputs(self):
        contract = self.contract()

        self.assertEqual(generator_type_declarations(contract, self.metadata()), type_declarations(contract, self.metadata()))
        self.assertEqual(generator_data_declarations(contract), data_declarations(contract))
        self.assertEqual(generator_callable_call_statements(contract, self.callable_metadata()), callable_call_statements(contract, self.callable_metadata()))

        entries = deterministic_alv_field_catalogue_entries(
            generation_contract=contract,
            ddic_metadata=self.metadata(),
        )

        self.assertEqual([entry["name"] for entry in entries], ["DOCNUM", "MESSAGE"])

    def test_output_contract_preserves_heading_and_conditional_visibility(self):
        requirements = self.requirements()
        requirements["output_structure_fields"] = [
            {
                "name": "DOCNUM",
                "type_or_like": "TYPE EDIDC-DOCNUM",
                "heading": "Document Number",
                "include_when": "p_idoc = 'X'",
            }
        ]

        contract = build_structured_generation_contract(
            requirements,
            self.plan(),
            ddic_metadata=self.metadata(),
            callable_metadata=self.callable_metadata(),
        )

        field = contract["output_structures"][0]["fields"][0]
        self.assertEqual(field["heading"], "Document Number")
        self.assertEqual(field["include_when"], "p_idoc = 'X'")

    def test_selection_screen_declarations_are_deterministic(self):
        self.assertEqual(
            selection_screen_declarations(self.contract()),
            "PARAMETERS p_limit TYPE i.\nSELECT-OPTIONS s_docnum FOR edidc-docnum.",
        )

    def test_selection_screen_checkboxes_and_radio_buttons_use_classical_syntax(self):
        contract = {
            "selection_screen": [
                {"kind": "PARAMETERS", "name": "p_file", "as_checkbox": True, "default": "'X'"},
                {"kind": "PARAMETERS", "name": "p_alv", "radiobutton_group": "rad1", "default": "'X'"},
            ]
        }

        self.assertEqual(
            selection_screen_declarations(contract),
            "PARAMETERS p_file AS CHECKBOX DEFAULT 'X'.\nPARAMETERS p_alv RADIOBUTTON GROUP rad1 DEFAULT 'X'.",
        )

    def test_selection_screen_bare_x_default_is_emitted_as_character_literal(self):
        contract = {
            "selection_screen": [
                {"kind": "PARAMETERS", "name": "p_alv", "radiobutton_group": "rad1", "default": "x"},
            ]
        }

        self.assertEqual(
            selection_screen_declarations(contract),
            "PARAMETERS p_alv RADIOBUTTON GROUP rad1 DEFAULT 'X'.",
        )

    def test_selection_screen_character_default_with_spaces_is_quoted(self):
        contract = {
            "selection_screen": [
                {"kind": "PARAMETERS", "name": "p_profil", "type_or_like": "TYPE string", "default": "bfg wk_1"},
            ]
        }

        self.assertEqual(
            selection_screen_declarations(contract),
            "PARAMETERS p_profil TYPE string DEFAULT 'BFG WK_1'.",
        )

    def test_selection_screen_validation_ignores_later_callable_validation(self):
        contract = self.contract()
        contract["function_module_calls"][0]["parameters"][0]["variable"] = "missing_textformat"

        full_validation = validate_generation_contract(contract, self.metadata(), self.callable_metadata())
        selection_validation = validate_selection_screen_generation_contract(contract, self.metadata())

        self.assertFalse(full_validation["valid"])
        self.assertTrue(selection_validation["valid"], selection_validation["errors"])

    def test_selection_screen_validation_blocks_unresolved_selection_ddic(self):
        contract = self.contract()
        contract["selection_screen"].append({"kind": "SELECT-OPTIONS", "name": "s_bad", "for_field": "EDIDC-NOPE"})

        validation = validate_selection_screen_generation_contract(contract, self.metadata())

        self.assertFalse(validation["valid"])
        self.assertIn("DDIC reference EDIDC-NOPE is unresolved", validation["errors"])

    def test_type_declarations_are_deterministic_from_known_fields(self):
        source = type_declarations(self.contract(), self.metadata())

        self.assertIn("TYPES: BEGIN OF ty_output,", source)
        self.assertIn("         docnum TYPE EDIDC-DOCNUM,", source)
        self.assertIn("TYPES: BEGIN OF ty_edidc,", source)
        self.assertIn("         credat TYPE EDIDC-CREDAT,", source)

    def test_contract_canonicalizes_ddic_field_references_from_unique_metadata_description(self):
        metadata = {
            "tables": {
                "ZABSENCE_LOG": {
                    "fields": {
                        "PERNR": {"datatype": "NUMC", "length": 8, "description": "Personnel Number"},
                        "WORKDATE": {"datatype": "DATS", "length": 8, "description": "Date"},
                    }
                }
            }
        }
        contract = build_structured_generation_contract(
            {
                "output_structure_fields": [
                    {"name": "DATE", "type_or_like": "TYPE ZABSENCE_LOG-DATE"},
                ]
            },
            {
                "processing_steps": [
                    {
                        "operation": "READ",
                        "source": "t_zabsence_log",
                        "into": "st_zabsence_log",
                        "conditions": [
                            {"left": "st_zabsence_log-date", "operator": "=", "right": "w_output-date"}
                        ],
                    }
                ]
            },
            ddic_metadata=metadata,
        )

        self.assertTrue(contract["validation"]["valid"], contract["validation"]["errors"])
        self.assertIn({"name": "ty_output", "object": "ZABSENCE_LOG", "field": "WORKDATE"}, contract["ddic_backed_types"])
        self.assertIn({"name": "ty_zabsence_log", "object": "ZABSENCE_LOG", "field": "WORKDATE"}, contract["ddic_backed_types"])
        self.assertIn("date TYPE ZABSENCE_LOG-WORKDATE", type_declarations(contract, metadata))

    def test_data_declarations_include_tables_work_areas_and_scalars(self):
        source = data_declarations(self.contract())

        self.assertIn("DATA t_edidc TYPE STANDARD TABLE OF ty_edidc.", source)
        self.assertIn("DATA w_edidc TYPE ty_edidc.", source)
        self.assertIn("DATA t_output TYPE STANDARD TABLE OF ty_output.", source)
        self.assertIn("DATA w_output TYPE ty_output.", source)
        self.assertIn("DATA gv_message TYPE BAPIRET2-MESSAGE.", source)

    def test_form_headers_performs_database_reads_and_main_flow_are_deterministic(self):
        contract = self.contract()

        self.assertEqual(form_headers(contract), "FORM read_edidc.\nENDFORM.")
        self.assertEqual(main_processing_flow(contract), "START-OF-SELECTION.\n  PERFORM read_edidc.")
        self.assertEqual(
            database_read_statements(contract),
            "SELECT docnum, credat\n  FROM edidc\n  INTO CORRESPONDING FIELDS OF TABLE t_edidc\n  WHERE docnum IN s_docnum.",
        )

    def test_callable_interfaces_are_deterministic_from_signature_metadata(self):
        contract = self.contract()

        self.assertEqual(
            function_module_call_statements(contract, self.callable_metadata()),
            "CALL FUNCTION 'Z_IDOC_STATUS'\n  EXPORTING\n    iv_docnum = gv_docnum\n  IMPORTING\n    ev_message = gv_message.",
        )
        self.assertIn(
            "CALL METHOD zcl_idoc_helper=>normalize\n  EXPORTING\n    iv_docnum = gv_docnum\n  RECEIVING\n    rv_docnum = gv_docnum.",
            contract["deterministic_abap"]["class_method_calls"],
        )

    def test_unresolved_ddic_reference_prevents_generation(self):
        contract = self.contract()
        contract["selection_screen"].append({"kind": "SELECT-OPTIONS", "name": "s_bad", "for_field": "EDIDC-NOPE"})

        validation = validate_generation_contract(contract, self.metadata(), self.callable_metadata())

        self.assertFalse(validation["valid"])
        self.assertIn("DDIC reference EDIDC-NOPE is unresolved", validation["errors"])

    def test_unknown_callable_parameter_prevents_generation(self):
        contract = self.contract()
        contract["function_module_calls"][0]["parameters"].append(
            {"parameter": "IV_UNKNOWN", "variable": "gv_docnum", "direction": "input"}
        )

        validation = validate_generation_contract(contract, self.metadata(), self.callable_metadata())

        self.assertFalse(validation["valid"])
        self.assertIn("callable parameter Z_IDOC_STATUS.IV_UNKNOWN does not exist", validation["errors"])

    def test_incompatible_callable_variable_prevents_generation(self):
        contract = self.contract()
        contract["function_module_calls"][0]["parameters"][0]["variable"] = "gv_message"

        validation = validate_generation_contract(contract, self.metadata(), self.callable_metadata())

        self.assertFalse(validation["valid"])
        self.assertIn(
            "callable variable gv_message is not type-compatible with Z_IDOC_STATUS.IV_DOCNUM: expected EDIDC-DOCNUM, found BAPIRET2-MESSAGE",
            validation["errors"],
        )

    def test_missing_form_definition_and_duplicate_declaration_prevent_generation(self):
        contract = self.contract()
        contract["perform_calls"].append({"name": "missing_form"})
        contract["scalar_variables"].append({"name": "gv_message", "type_or_like": "TYPE BAPIRET2-MESSAGE"})

        validation = validate_generation_contract(contract, self.metadata(), self.callable_metadata())

        self.assertFalse(validation["valid"])
        self.assertIn("FORM call missing_form has no corresponding FORM definition", validation["errors"])
        self.assertIn("duplicate declaration rejected: gv_message", validation["errors"])

    def test_full_data_output_globals_do_not_duplicate_contract_owned_output_declarations(self):
        contract = build_structured_generation_contract(
            {
                "report_name": "ztest",
                "global_variables": [
                    {"name": "t_output", "declaration": "DATA t_output TYPE STANDARD TABLE OF ty_output."},
                    {"name": "w_output", "declaration": "DATA w_output TYPE ty_output."},
                ],
                "output_structure_fields": [{"name": "MESSAGE", "type_or_like": "TYPE BAPIRET2-MESSAGE"}],
            },
            {"processing_steps": []},
            ddic_metadata=self.metadata(),
        )

        self.assertTrue(contract["validation"]["valid"])
        self.assertEqual(["t_output"], [item["name"] for item in contract["internal_tables"]])
        self.assertEqual(["w_output"], [item["name"] for item in contract["work_areas"]])
        self.assertFalse([item for item in contract["scalar_variables"] if item["name"] in {"t_output", "w_output"}])

    def test_incomplete_data_output_globals_do_not_duplicate_contract_owned_output_declarations(self):
        contract = build_structured_generation_contract(
            {
                "report_name": "ztest",
                "global_variables": [
                    {"name": "t_output", "declaration": "DATA t_output."},
                    {"name": "w_record", "declaration": "DATA w_record."},
                ],
                "output_structure_fields": [{"name": "MESSAGE", "type_or_like": "TYPE BAPIRET2-MESSAGE"}],
            },
            {"processing_steps": []},
            ddic_metadata=self.metadata(),
        )

        self.assertTrue(contract["validation"]["valid"], contract["validation"]["errors"])
        self.assertEqual(["t_output"], [item["name"] for item in contract["internal_tables"]])
        self.assertEqual(["w_output"], [item["name"] for item in contract["work_areas"]])
        self.assertFalse([item for item in contract["work_areas"] if item["name"] in {"t_output", "w_record"}])

    def test_callable_component_compatibility_accepts_verified_same_technical_shape(self):
        metadata = {
            "tables": {
                "EDIDS": {
                    "fields": {
                        "DOCNUM": {"datatype": "NUMC", "length": 16},
                        "STAMID": {"datatype": "CHAR", "length": 20},
                        "STAMNO": {"datatype": "NUMC", "length": 3},
                    }
                },
                "BAPIRET2": {
                    "fields": {
                        "ID": {"datatype": "CHAR", "length": 20},
                        "NUMBER": {"datatype": "NUMC", "length": 3},
                    }
                },
            }
        }
        callable_metadata = {
            "callable_signatures": {
                "BAPI_MESSAGE_GETDETAIL": {
                    "parameters": {
                        "ID": {"direction": "IMPORTING", "abap_type": "BAPIRET2", "field": "ID"},
                        "NUMBER": {"direction": "IMPORTING", "abap_type": "BAPIRET2", "field": "NUMBER"},
                    }
                }
            }
        }
        contract = build_structured_generation_contract(
            {"report_name": "ztest"},
            {
                "processing_steps": [
                    {
                        "operation": "READ",
                        "source": "t_edids",
                        "into": "st_edids",
                        "conditions": [{"left": "st_edids-DOCNUM", "operator": "=", "right": "st_edidc-DOCNUM"}],
                    },
                    {
                        "operation": "CALL_FUNCTION",
                        "name": "BAPI_MESSAGE_GETDETAIL",
                        "input_parameters": {
                            "ID": "st_edids-stamid",
                            "NUMBER": "st_edids-stamno",
                        },
                    },
                ]
            },
            ddic_metadata=metadata,
            callable_metadata=callable_metadata,
        )

        self.assertTrue(contract["validation"]["valid"], contract["validation"]["errors"])

    def test_table_read_contract_keeps_join_fields_on_their_own_ddic_objects(self):
        metadata = {
            "tables": {
                "EDIDC": {"fields": {"DOCNUM": {"datatype": "NUMC", "length": 16}}},
                "ZMD_MPE0001": {
                    "fields": {
                        "DOCNUM_IN": {"datatype": "NUMC", "length": 16},
                        "IDENTIFIER": {"datatype": "CHAR", "length": 30},
                    }
                },
                "ZMD_MPE0006": {
                    "fields": {
                        "DOCNUM_IN": {"datatype": "NUMC", "length": 16},
                        "IDENTIFIER": {"datatype": "CHAR", "length": 30},
                    }
                },
            }
        }
        contract = build_structured_generation_contract(
            {"report_name": "ztest"},
            {
                "processing_steps": [
                    {
                        "operation": "READ",
                        "source": "t_zmd_mpe0001",
                        "into": "st_zmd_mpe0001",
                        "conditions": [{"left": "st_zmd_mpe0001-DOCNUM_IN", "operator": "=", "right": "st_edidc-DOCNUM"}],
                    },
                    {
                        "operation": "READ",
                        "source": "t_zmd_mpe0006",
                        "into": "st_zmd_mpe0006",
                        "conditions": [{"left": "st_zmd_mpe0006-DOCNUM_IN", "operator": "=", "right": "st_edidc-DOCNUM"}],
                    },
                ]
            },
            ddic_metadata=metadata,
        )

        self.assertTrue(contract["validation"]["valid"], contract["validation"]["errors"])
        self.assertIn({"name": "ty_zmd_mpe0001", "object": "ZMD_MPE0001", "field": "DOCNUM_IN"}, contract["ddic_backed_types"])
        self.assertIn({"name": "ty_zmd_mpe0006", "object": "ZMD_MPE0006", "field": "DOCNUM_IN"}, contract["ddic_backed_types"])
        self.assertNotIn({"name": "ty_zmd_mpe0001", "object": "ZMD_MPE0001", "field": "DOCNUM"}, contract["ddic_backed_types"])
        self.assertNotIn({"name": "ty_zmd_mpe0006", "object": "ZMD_MPE0006", "field": "DOCNUM"}, contract["ddic_backed_types"])

    def test_callable_output_parameter_accepts_output_structure_component_type(self):
        contract = build_structured_generation_contract(
            {
                "report_name": "ztest",
                "output_structure_fields": [{"name": "ERROR_MESSAGE", "type_or_like": "TYPE BAPIRET2-MESSAGE"}],
            },
            {
                "processing_steps": [
                    {
                        "operation": "CALL_FUNCTION",
                        "name": "BAPI_MESSAGE_GETDETAIL",
                        "output_parameters": {"MESSAGE": "w_output-error_message"},
                    }
                ]
            },
            ddic_metadata=self.metadata(),
            callable_metadata={
                "callable_signatures": {
                    "BAPI_MESSAGE_GETDETAIL": {
                        "parameters": {
                            "MESSAGE": {"direction": "EXPORTING", "abap_type": "BAPIRET2", "field": "MESSAGE"}
                        }
                    }
                }
            },
        )

        self.assertTrue(contract["validation"]["valid"], contract["validation"]["errors"])

    def test_callable_signature_metadata_owns_missing_scalar_declarations(self):
        contract = build_structured_generation_contract(
            {"report_name": "ztest"},
            {
                "processing_steps": [
                    {
                        "operation": "CALL_FUNCTION",
                        "name": "Z_IDOC_STATUS",
                        "input_parameters": {"IV_DOCNUM": "gv_docnum"},
                        "output_parameters": {"EV_MESSAGE": "gv_message"},
                    }
                ]
            },
            ddic_metadata=self.metadata(),
            callable_metadata=self.callable_metadata(),
        )

        self.assertTrue(contract["validation"]["valid"], contract["validation"]["errors"])
        self.assertIn({"name": "gv_docnum", "type_or_like": "TYPE EDIDC-DOCNUM"}, contract["scalar_variables"])
        self.assertIn({"name": "gv_message", "type_or_like": "TYPE BAPIRET2-MESSAGE"}, contract["scalar_variables"])
        self.assertIn("DATA gv_docnum TYPE EDIDC-DOCNUM.", data_declarations(contract))

    def test_missing_ddic_metadata_prevents_contract_generation(self):
        contract = build_structured_generation_contract(
            {"report_name": "ztest", "select_options": [{"name": "s_docnum", "for_field": "EDIDC-DOCNUM"}]},
            {"processing_steps": []},
        )

        self.assertFalse(contract["validation"]["valid"])
        self.assertIn("DDIC reference EDIDC-DOCNUM is unresolved", contract["validation"]["errors"])

    def test_missing_callable_signature_prevents_contract_generation(self):
        contract = build_structured_generation_contract(
            {"report_name": "ztest", "global_variables": [{"name": "gv_docnum", "declaration": "TYPE EDIDC-DOCNUM"}]},
            {
                "processing_steps": [
                    {"operation": "CALL_FUNCTION", "name": "Z_UNKNOWN", "input_parameters": {"IV_DOCNUM": "gv_docnum"}}
                ]
            },
            ddic_metadata=self.metadata(),
            callable_metadata={"callable_signatures": {}},
        )

        self.assertFalse(contract["validation"]["valid"])
        self.assertIn("callable signature Z_UNKNOWN is unresolved", contract["validation"]["errors"])

    def test_function_module_call_supports_all_parameter_sections_from_metadata(self):
        callable_metadata = {
            "callable_signatures": {
                "Z_FULL_INTERFACE": {
                    "parameters": {
                        "IV_KEY": {"direction": "IMPORTING", "abap_type": "EDIDC", "field": "DOCNUM", "required": True},
                        "ES_RETURN": {"direction": "EXPORTING", "abap_type": "BAPIRET2", "required": False},
                        "CS_RETURN": {"direction": "CHANGING", "abap_type": "BAPIRET2", "required": False},
                        "TT_TEXT": {"direction": "TABLES", "abap_type": "BAPITGB", "required": False},
                    }
                }
            }
        }
        contract = build_structured_generation_contract(
            {"report_name": "ztest"},
            {
                "processing_steps": [
                    {
                        "operation": "CALL_FUNCTION",
                        "name": "Z_FULL_INTERFACE",
                        "input_parameters": {"IV_KEY": "gv_docnum"},
                        "output_parameters": {"ES_RETURN": "st_return"},
                        "changing_parameters": {"CS_RETURN": "st_change"},
                        "tables_parameters": {"TT_TEXT": "t_text"},
                    }
                ]
            },
            ddic_metadata=self.metadata(),
            callable_metadata=callable_metadata,
        )

        self.assertTrue(contract["validation"]["valid"], contract["validation"]["errors"])
        mappings = {item["parameter"]: item for item in contract["function_module_calls"][0]["parameters"]}
        self.assertTrue(mappings["IV_KEY"]["required"])
        self.assertFalse(mappings["ES_RETURN"]["required"])
        self.assertEqual("scalar", mappings["IV_KEY"]["parameter_kind"])
        self.assertEqual("structure", mappings["ES_RETURN"]["parameter_kind"])
        self.assertEqual("structure", mappings["CS_RETURN"]["parameter_kind"])
        self.assertEqual("table", mappings["TT_TEXT"]["parameter_kind"])
        self.assertIn("DATA gv_docnum TYPE EDIDC-DOCNUM.", data_declarations(contract))
        self.assertIn("DATA st_return TYPE BAPIRET2.", data_declarations(contract))
        self.assertIn("DATA st_change TYPE BAPIRET2.", data_declarations(contract))
        self.assertIn("DATA t_text TYPE STANDARD TABLE OF BAPITGB.", data_declarations(contract))
        self.assertEqual(
            function_module_call_statements(contract, callable_metadata),
            "\n".join(
                [
                    "CALL FUNCTION 'Z_FULL_INTERFACE'",
                    "  EXPORTING",
                    "    iv_key = gv_docnum",
                    "  IMPORTING",
                    "    es_return = st_return",
                    "  CHANGING",
                    "    cs_return = st_change",
                    "  TABLES",
                    "    tt_text = t_text.",
                ]
            ),
        )

    def test_bapi_message_getdetail_uses_selected_parameter_not_semantic_substitute(self):
        callable_metadata = {
            "callable_signatures": {
                "BAPI_MESSAGE_GETDETAIL": {
                    "parameters": {
                        "MESSAGE": {"direction": "EXPORTING", "abap_type": "BAPIRET2", "field": "MESSAGE", "required": True},
                        "RETURN": {"direction": "EXPORTING", "abap_type": "BAPIRET2", "field": "", "required": True},
                    }
                }
            }
        }
        contract = build_structured_generation_contract(
            {"report_name": "ztest", "global_variables": [{"name": "gv_message", "declaration": "TYPE BAPIRET2-MESSAGE"}]},
            {
                "processing_steps": [
                    {
                        "operation": "CALL_FUNCTION",
                        "name": "BAPI_MESSAGE_GETDETAIL",
                        "output_parameters": {"RETURN": "gv_message"},
                    }
                ]
            },
            ddic_metadata=self.metadata(),
            callable_metadata=callable_metadata,
        )

        self.assertFalse(contract["validation"]["valid"])
        self.assertIn(
            "callable variable gv_message is not type-compatible with BAPI_MESSAGE_GETDETAIL.RETURN: expected BAPIRET2, found BAPIRET2-MESSAGE",
            contract["validation"]["errors"],
        )
        self.assertIn("return = gv_message.", function_module_call_statements(contract, callable_metadata))
        self.assertNotIn("message = gv_message", function_module_call_statements(contract, callable_metadata))

    def test_static_and_instance_methods_generate_interfaces_and_object_reference(self):
        callable_metadata = {
            "callable_signatures": {
                "CL_BCS=>CREATE_PERSISTENT": {
                    "parameters": {},
                    "returning": {"name": "RESULT", "direction": "RETURNING", "abap_type": "CL_BCS", "required": True},
                },
                "CL_BCS=>SEND": {
                    "parameters": {
                        "I_WITH_ERROR_SCREEN": {"direction": "IMPORTING", "abap_type": "OS_BOOLEAN", "required": False},
                    },
                    "returning": {"name": "RESULT", "direction": "RETURNING", "abap_type": "OS_BOOLEAN", "required": True},
                },
            }
        }
        contract = build_structured_generation_contract(
            {"report_name": "ztest"},
            {
                "processing_steps": [
                    {
                        "operation": "CALL_STATIC_METHOD",
                        "class": "CL_BCS",
                        "method": "CREATE_PERSISTENT",
                        "receiving_parameter": "lo_send_request",
                    },
                    {
                        "operation": "CALL_METHOD",
                        "name": "CL_BCS=>SEND",
                        "object": "lo_send_request",
                        "input_parameters": {"I_WITH_ERROR_SCREEN": "gv_error_screen"},
                        "returning_parameter": "gv_sent",
                    },
                ]
            },
            callable_metadata=callable_metadata,
        )

        self.assertTrue(contract["validation"]["valid"], contract["validation"]["errors"])
        declarations = data_declarations(contract)
        self.assertIn("DATA lo_send_request TYPE REF TO CL_BCS.", declarations)
        self.assertIn("DATA gv_error_screen TYPE OS_BOOLEAN.", declarations)
        self.assertIn("DATA gv_sent TYPE OS_BOOLEAN.", declarations)
        self.assertEqual(
            callable_call_statements(contract, callable_metadata),
            [
                "CALL METHOD cl_bcs=>create_persistent\n  RECEIVING\n    result = lo_send_request.",
                "CALL METHOD lo_send_request->send\n  EXPORTING\n    i_with_error_screen = gv_error_screen\n  RECEIVING\n    result = gv_sent.",
            ],
        )

    def test_wrong_callable_direction_is_rejected(self):
        contract = build_structured_generation_contract(
            {"report_name": "ztest"},
            {
                "processing_steps": [
                    {"operation": "CALL_FUNCTION", "name": "Z_IDOC_STATUS", "input_parameters": {"EV_MESSAGE": "gv_message"}}
                ]
            },
            ddic_metadata=self.metadata(),
            callable_metadata=self.callable_metadata(),
        )

        self.assertFalse(contract["validation"]["valid"])
        self.assertIn(
            "callable parameter Z_IDOC_STATUS.EV_MESSAGE is mapped as input but metadata direction is EXPORTING",
            contract["validation"]["errors"],
        )

    def test_bapi_tables_parameters_do_not_reuse_output_record_contract(self):
        callable_metadata = {
            "callable_signatures": {
                "BAPI_CATIMESHEETMGR_INSERT": {
                    "parameters": {
                        "CATSRECORDS_IN": {
                            "direction": "TABLES",
                            "abap_type": "BAPICATS1",
                            "required": True,
                        },
                        "CATSRECORDS_OUT": {
                            "direction": "TABLES",
                            "abap_type": "BAPICATS2",
                            "required": False,
                        },
                        "TESTRUN": {
                            "direction": "IMPORTING",
                            "abap_type": "BAPICATS6",
                            "field": "TESTRUN",
                        },
                        "RETURN": {
                            "direction": "TABLES",
                            "abap_type": "BAPIRET2",
                            "required": True,
                        },
                    }
                }
            }
        }
        contract = build_structured_generation_contract(
            {
                "report_name": "ztest",
                "global_variables": [
                    {"name": "PERNR", "declaration": "DATA PERNR TYPE CATSDB-PERNR."},
                    {"name": "t_catsdb", "declaration": "DATA t_catsdb TYPE STANDARD TABLE OF ty_catsdb."},
                    {"name": "t_output", "declaration": "DATA t_output TYPE STANDARD TABLE OF ty_output."},
                    {"name": "w_output", "declaration": "DATA w_output TYPE ty_output."},
                ],
                "output_structure_fields": [
                    {"name": "File Row Number", "type_or_like": ""},
                    {"name": "PERNR", "type_or_like": "TYPE CATSDB-PERNR"},
                    {"name": "STATUS", "type_or_like": ""},
                    {"name": "SAP_MESSAGE", "type_or_like": ""},
                ],
            },
            {
                "processing_steps": [
                    {
                        "operation": "CALL_FUNCTION",
                        "name": "BAPI_CATIMESHEETMGR_INSERT",
                        "input_parameters": {
                            "CATSRECORDS_IN": "t_catsdb",
                            "CATSRECORDS_OUT": "t_catsdb",
                            "TESTRUN": "w_output-status",
                            "RETURN": "t_catsdb",
                        },
                    }
                ]
            },
            ddic_metadata={"tables": {}},
            callable_metadata=callable_metadata,
        )

        self.assertTrue(contract["validation"]["valid"], contract["validation"]["errors"])
        self.assertIn({"name": "t_catsrecords_in", "row_type": "BAPICATS1"}, contract["internal_tables"])
        self.assertIn({"name": "t_catsrecords_out", "row_type": "BAPICATS2"}, contract["internal_tables"])
        self.assertIn({"name": "t_return", "row_type": "BAPIRET2"}, contract["internal_tables"])
        self.assertIn({"name": "w_testrun", "type_or_like": "TYPE BAPICATS6-TESTRUN"}, contract["scalar_variables"])
        self.assertNotIn({"name": "t_output", "type_or_like": ""}, contract["scalar_variables"])
        self.assertNotIn({"name": "w_output", "type_or_like": ""}, contract["scalar_variables"])
        self.assertNotIn({"parameter": "CATSRECORDS_IN", "variable": "t_catsdb"}, contract["function_module_calls"][0]["parameters"])
        self.assertEqual(
            function_module_call_statements(contract, callable_metadata),
            "\n".join(
                [
                    "CALL FUNCTION 'BAPI_CATIMESHEETMGR_INSERT'",
                    "  EXPORTING",
                    "    testrun = w_testrun",
                    "  TABLES",
                    "    catsrecords_in = t_catsrecords_in",
                    "    catsrecords_out = t_catsrecords_out",
                    "    return = t_return.",
                ]
            ),
        )
        self.assertIn("file row number TYPE i,", type_declarations(contract))
        self.assertIn("pernr TYPE string,", type_declarations(contract))


if __name__ == "__main__":
    unittest.main()
