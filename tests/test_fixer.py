from io import BytesIO
import json
from pathlib import Path
import shutil
import time as real_time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import create_app
from services.fixer import auto_fix_abap
from services.progress import get_progress


class FixerTest(unittest.TestCase):
    def test_identifier_rename_updates_declaration_and_references(self):
        source = "\n".join(
            [
                "DATA gt_items TYPE STANDARD TABLE OF mara.",
                "APPEND mara TO gt_items.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("DATA t_items TYPE STANDARD TABLE OF mara.", result["fixed_source"])
        self.assertIn("APPEND mara TO t_items.", result["fixed_source"])
        self.assertLess(result["final_issue_count"], result["original_issue_count"])

    def test_comments_and_strings_are_untouched(self):
        source = "\n".join(
            [
                "DATA gt_items TYPE STANDARD TABLE OF mara.",
                '" gt_items in a comment',
                "gv_text = 'gt_items in a string'.",
            ]
        )

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertIn('" gt_items in a comment', fixed)
        self.assertIn("'gt_items in a string'", fixed)
        self.assertIn("DATA t_items TYPE STANDARD TABLE OF mara.", fixed)

    def test_indented_asterisk_comment_is_fixed(self):
        result = auto_fix_abap("     * build error message")

        self.assertEqual(result["fixed_source"], "* build error message")
        self.assertEqual(result["final_issues"], [])

    def test_valid_column_one_asterisk_comment_is_unchanged(self):
        source = "* build error message"

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["final_issues"], [])

    def test_multiplication_is_unchanged_for_asterisk_comment_fix(self):
        source = "w_total = w_count * w_price."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["final_issues"], [])

    def test_string_content_is_unchanged_for_asterisk_comment_fix(self):
        source = "w_text = '* build error message'."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["final_issues"], [])

    def test_quote_comment_is_unchanged_for_asterisk_comment_fix(self):
        source = '     " build error message'

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["final_issues"], [])

    def test_final_validation_reaches_zero_for_indented_asterisk_comment(self):
        result = auto_fix_abap("     * build error message")

        self.assertEqual(result["original_issue_count"], 1)
        self.assertEqual(result["final_issue_count"], 0)
        self.assertEqual(result["final_issues"], [])

    def test_leave_list_page_is_fixed_for_classical_ecc_target(self):
        result = auto_fix_abap("LEAVE LIST-PAGE.")

        self.assertEqual(result["fixed_source"], "LEAVE LIST-PROCESSING.")
        self.assertEqual(result["final_issues"], [])

    def test_leave_list_page_indentation_and_trailing_comment_are_preserved(self):
        result = auto_fix_abap("  LEAVE LIST-PAGE. \" done")

        self.assertEqual(result["fixed_source"], "  LEAVE LIST-PROCESSING. \" done")
        self.assertEqual(result["final_issues"], [])

    def test_leave_list_page_comments_are_not_modified(self):
        source = '" LEAVE LIST-PAGE.'

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["final_issues"], [])

    def test_leave_list_page_string_literals_are_not_modified(self):
        source = "DATA w_text TYPE string VALUE 'LEAVE LIST-PAGE.'."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["final_issues"], [])

    def test_leave_list_processing_remains_unchanged(self):
        source = "LEAVE LIST-PROCESSING."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["final_issues"], [])

    def test_host_variable_escapes_are_removed_for_classical_open_sql(self):
        source = "\n".join(
            [
                "FORM read_zs505.",
                "  SELECT kunnr",
                "    FROM zs505",
                "    INTO TABLE @t_zs505",
                "    WHERE sptag BETWEEN @p_from_date AND @p_to_date.",
                "ENDFORM.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("    INTO TABLE t_zs505", result["fixed_source"])
        self.assertIn("    WHERE sptag BETWEEN p_from_date AND p_to_date.", result["fixed_source"])
        self.assertNotIn("@", result["fixed_source"])
        self.assertIn("HOST_VARIABLE_ESCAPE", result["diagnostics"]["changed_rules"])
        self.assertFalse([issue for issue in result["final_issues"] if issue["rule_id"] == "HOST_VARIABLE_ESCAPE"])

    def test_select_field_list_commas_are_removed_for_classical_open_sql(self):
        source = "\n".join(
            [
                "FORM read_zs505.",
                "  SELECT werks,",
                "         kunnr,",
                "         vbeln,",
                "         vrkme_01,",
                "         SUM( kzwi2 ) AS kzwi2,",
                "         SUM( wavwr ) AS wavwr",
                "    FROM zs505",
                "    INTO CORRESPONDING FIELDS OF TABLE t_zs505",
                "    WHERE sptag BETWEEN p_from_date AND p_to_date",
                "    GROUP BY werks,",
                "             kunnr,",
                "             vbeln,",
                "             vrkme_01.",
                "ENDFORM.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("  SELECT werks", result["fixed_source"])
        self.assertIn("         kunnr", result["fixed_source"])
        self.assertIn("         SUM( kzwi2 ) AS kzwi2", result["fixed_source"])
        self.assertIn("    GROUP BY werks", result["fixed_source"])
        self.assertIn("             kunnr", result["fixed_source"])
        self.assertNotIn("werks,", result["fixed_source"])
        self.assertNotIn("kunnr,", result["fixed_source"])
        self.assertNotIn("kzwi2,", result["fixed_source"])
        self.assertIn("SELECT_FIELD_LIST_COMMAS", result["diagnostics"]["changed_rules"])

    def test_endselect_after_select_into_table_is_removed(self):
        source = "\n".join(
            [
                "FORM read_edidc.",
                "  SELECT credat mestyp docnum",
                "    FROM edidc",
                "    INTO TABLE t_edidc",
                "    WHERE credat IN s_credat",
                "      AND mestyp IN s_mestyp.",
                "ENDSELECT.",
                "ENDFORM.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "FORM read_edidc.",
                    "  SELECT credat mestyp docnum",
                    "    FROM edidc",
                    "    INTO TABLE t_edidc",
                    "    WHERE credat IN s_credat",
                    "      AND mestyp IN s_mestyp.",
                    "ENDFORM.",
                ]
            ),
        )
        self.assertIn("INVALID_ENDSELECT_AFTER_INTO_TABLE", result["diagnostics"]["changed_rules"])

    def test_endselect_after_select_loop_is_preserved(self):
        source = "\n".join(
            [
                "SELECT credat mestyp docnum",
                "  FROM edidc",
                "  INTO st_edidc",
                "  WHERE credat IN s_credat.",
                "  APPEND st_edidc TO t_edidc.",
                "ENDSELECT.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertNotIn("INVALID_ENDSELECT_AFTER_INTO_TABLE", result["diagnostics"]["changed_rules"])

    def test_valid_select_clause_order_is_unchanged(self):
        source = "\n".join(
            [
                "SELECT docnum credat mestyp",
                "  FROM edidc",
                "  INTO TABLE t_edidc",
                "  FOR ALL ENTRIES IN t_keys",
                "  WHERE docnum = t_keys-docnum",
                "    AND credat IN s_credat.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertNotIn("SELECT_CLAUSE_ORDER", result["diagnostics"]["changed_rules"])

    def test_classic_multiline_select_into_before_from_is_unchanged(self):
        source = "\n".join(
            [
                "FORM read_customers .",
                "",
                "  SELECT knvp~kunn2",
                "         knb1~kunnr",
                "         knb1~zhomebran",
                "         kna1~zslsman1",
                "         kna1~sortl",
                "         kna1~name2",
                "         INTO TABLE t_customers",
                "         FROM knb1 AS knb1",
                "         INNER JOIN knvp AS knvp",
                "         ON knvp~kunnr   EQ knb1~kunnr",
                "         AND knvp~vkorg  EQ '1001'",
                "         AND knvp~vtweg  EQ '3'",
                "         AND knvp~parvw  EQ 'RG'",
                "         INNER JOIN kna1 AS kna1",
                "         ON kna1~kunnr EQ knb1~kunnr",
                "         WHERE knb1~kunnr     IN s_kunnr",
                "           AND knb1~zhomebran IN s_branch",
                "           AND knb1~zstatus   EQ 'C'.",
                "",
                "ENDFORM.                    \" READ_CUSTOMERS",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertNotIn("SELECT_CLAUSE_ORDER", result["diagnostics"]["changed_rules"])

    def test_misplaced_select_into_table_is_moved_before_where(self):
        source = "\n".join(
            [
                "SELECT docnum credat mestyp",
                "  FROM edidc",
                "  WHERE credat IN s_credat",
                "    AND mestyp IN s_mestyp",
                "  INTO TABLE t_edidc.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "SELECT docnum credat mestyp FROM edidc INTO TABLE t_edidc WHERE credat IN s_credat AND mestyp IN s_mestyp.",
        )
        self.assertEqual([fix["rule_id"] for fix in result["fixes"]], ["SELECT_CLAUSE_ORDER"])
        self.assertIn("statement starting on line 1", result["fixes"][0]["description"])

    def test_misplaced_select_into_preserves_for_all_entries_and_conditions(self):
        source = "\n".join(
            [
                "SELECT docnum status",
                "  FROM edids",
                "  FOR ALL ENTRIES IN t_edidc",
                "  WHERE docnum = t_edidc-docnum",
                "    AND status IN s_status",
                "  INTO st_edids.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "SELECT docnum status FROM edids INTO st_edids FOR ALL ENTRIES IN t_edidc WHERE docnum = t_edidc-docnum AND status IN s_status.",
        )
        self.assertEqual(result["diagnostics"]["changed_rules"], ["SELECT_CLAUSE_ORDER"])

    def test_misplaced_select_appending_table_is_moved_before_where(self):
        source = "\n".join(
            [
                "SELECT field_a field_b",
                "  FROM zsource",
                "  WHERE field_a = w_value",
                "  APPENDING TABLE t_result.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "SELECT field_a field_b FROM zsource APPENDING TABLE t_result WHERE field_a = w_value.",
        )
        self.assertEqual(result["diagnostics"]["changed_rules"], ["SELECT_CLAUSE_ORDER"])

    def test_leave_report_fix_is_global_and_targeted_to_statement_lines(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "START-OF-SELECTION.",
                "  LEAVE REPORT.",
                "AT USER-COMMAND.",
                "  WRITE: / 'Back'.",
                "  LEAVE REPORT. \" return to list",
                "  WRITE: / 'After'.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "REPORT ztest.",
                    "START-OF-SELECTION.",
                    "  LEAVE LIST-PROCESSING.",
                    "AT USER-COMMAND.",
                    "  WRITE: / 'Back'.",
                    "  LEAVE LIST-PROCESSING. \" return to list",
                    "  WRITE: / 'After'.",
                ]
            ),
        )
        self.assertIn(
            {"rule_id": "ABAP_LIST_PROCESSING_EXIT_MISMATCH", "description": "Replaced LEAVE REPORT with LEAVE LIST-PROCESSING on line 6."},
            result["fixes"],
        )
        self.assertEqual(result["final_issues"], [])

    def test_leave_report_outside_list_processing_event_is_changed(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "START-OF-SELECTION.",
                "  LEAVE REPORT.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "REPORT ztest.",
                    "START-OF-SELECTION.",
                    "  LEAVE LIST-PROCESSING.",
                ]
            ),
        )
        self.assertEqual(result["final_issues"], [])

    def test_final_validation_reaches_zero_for_leave_list_page(self):
        result = auto_fix_abap("LEAVE LIST-PAGE.")

        self.assertEqual(result["original_issue_count"], 1)
        self.assertEqual(result["final_issue_count"], 0)
        self.assertEqual(result["final_issues"], [])

    def test_component_names_are_untouched(self):
        source = "\n".join(
            [
                "DATA ls_item TYPE mara.",
                "ls_item-ls_field = 'X'.",
            ]
        )

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertIn("DATA st_item TYPE mara.", fixed)
        self.assertIn("st_item-ls_field = 'X'.", fixed)

    def test_simple_message_template_is_converted(self):
        source = "MESSAGE |Cannot open file { w_file } for output| TYPE 'E'."

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertIn("DATA lv_message_text TYPE string.", fixed)
        self.assertIn("CONCATENATE 'Cannot open file' w_file 'for output' INTO lv_message_text SEPARATED BY space.", fixed)
        self.assertIn("MESSAGE lv_message_text TYPE 'E'.", fixed)

    def test_complex_message_template_is_left_unchanged(self):
        source = "MESSAGE |Cannot open { w_file } for { w_mode }| TYPE 'E'."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertIn("Left complex MESSAGE string template unchanged", result["fixes"][0]["description"])

    def test_validator_reruns_and_issue_count_decreases(self):
        source = "DATA gt_items TYPE STANDARD TABLE OF mara."

        result = auto_fix_abap(source)

        self.assertEqual(result["original_issue_count"], 1)
        self.assertEqual(result["final_issue_count"], 0)
        self.assertEqual(result["final_issues"], [])

    def test_flow_saves_original_fixed_counts_and_downloads_fixed_abap(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_fixer_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            generated_abap = "DATA gt_items TYPE STANDARD TABLE OF mara.\nAPPEND mara TO gt_items."

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": generated_abap, "model": "test-model", "usage": None},
            ):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/upload",
                    data={"abap_file": (BytesIO(b"Create a test report."), "request.txt")},
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )
                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Complete")
                job_folder = jobs_folder / job_id

                self.assertEqual((job_folder / "original_generated.abap").read_text(encoding="utf-8"), generated_abap)
                self.assertIn("DATA t_items", (job_folder / "generated.abap").read_text(encoding="utf-8"))

                summary = json.loads((job_folder / "fix_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["original_issue_count"], 1)
                self.assertEqual(summary["final_issue_count"], 0)
                self.assertEqual(len(summary["fixes"]), 1)
                self.assertEqual(summary["diagnostics"]["source_before_fixer"], generated_abap)
                self.assertIn("DATA t_items TYPE STANDARD TABLE OF mara.", summary["diagnostics"]["source_after_fixer"])
                self.assertEqual(summary["diagnostics"]["changed_rules"], ["FORBIDDEN_NAMING_PREFIX"])

                result = client.get(f"/result/{job_id}")
                self.assertIn(b"Auto Fix", result.data)
                self.assertIn(b"Issue count before: 1", result.data)
                self.assertIn(b"Issue count after: 0", result.data)

                download = client.get(f"/download/{job_id}")
                downloaded = download.get_data(as_text=True).replace("\r\n", "\n")
                download.close()
                self.assertIn("DATA t_items TYPE STANDARD TABLE OF mara.", downloaded)
                self.assertNotIn("gt_items", downloaded)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_no_llm_or_sap_call_occurs_in_fixer(self):
        with patch("services.fixer.validate_abap", return_value=[]):
            result = auto_fix_abap("DATA gv_name TYPE string.")

        self.assertEqual(result["fixed_source"], "DATA gv_name TYPE string.")

    def test_loop_inline_declaration_fixed_when_row_type_known(self):
        source = "\n".join(
            [
                "DATA item_table TYPE STANDARD TABLE OF mara.",
                "LOOP AT item_table INTO DATA(item_row).",
                "ENDLOOP.",
            ]
        )

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertIn("DATA item_row TYPE mara.", fixed)
        self.assertIn("LOOP AT item_table INTO item_row.", fixed)
        self.assertNotIn("DATA(item_row)", fixed)

    def test_loop_inline_fix_applies_project_naming_convention(self):
        source = "\n".join(
            [
                "DATA lt_items TYPE STANDARD TABLE OF mara.",
                "LOOP AT lt_items INTO DATA(ls_item).",
                "  ls_item-matnr = 'A'.",
                "ENDLOOP.",
            ]
        )

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertIn("DATA t_items TYPE STANDARD TABLE OF mara.", fixed)
        self.assertIn("DATA st_item TYPE mara.", fixed)
        self.assertIn("LOOP AT t_items INTO st_item.", fixed)
        self.assertIn("st_item-matnr = 'A'.", fixed)
        self.assertNotIn("lt_items", fixed)
        self.assertNotIn("ls_item", fixed)

    def test_read_table_inline_declaration_fixed_when_row_type_known(self):
        source = "\n".join(
            [
                "DATA item_table TYPE STANDARD TABLE OF mara.",
                "READ TABLE item_table INTO DATA(item_row) INDEX 1.",
            ]
        )

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertIn("DATA item_row TYPE mara.", fixed)
        self.assertIn("READ TABLE item_table INTO item_row INDEX 1.", fixed)

    def test_direct_table_declaration_infers_loop_row_type(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "LOOP AT t_items INTO DATA(st_item).",
                "ENDLOOP.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("DATA st_item TYPE ty_item.", result["fixed_source"])
        self.assertIn("LOOP AT t_items INTO st_item.", result["fixed_source"])
        self.assertEqual(result["final_issues"], [])

    def test_perform_form_mapping_infers_row_type(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "PERFORM load_items USING t_items.",
                "FORM load_items USING p_items TYPE STANDARD TABLE.",
                "  LOOP AT p_items INTO DATA(st_item).",
                "ENDLOOP.",
                "ENDFORM.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("FORM load_items USING p_items TYPE STANDARD TABLE.", result["fixed_source"])
        self.assertIn("DATA st_item TYPE ty_item.", result["fixed_source"])
        self.assertIn("  LOOP AT p_items INTO st_item.", result["fixed_source"])
        self.assertEqual(result["final_issues"], [])

    def test_multiple_compatible_perform_calls_infer_row_type(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA t_first TYPE STANDARD TABLE OF ty_item.",
                "DATA t_second TYPE STANDARD TABLE OF ty_item.",
                "PERFORM load_items USING t_first.",
                "PERFORM load_items USING t_second.",
                "FORM load_items USING p_items TYPE STANDARD TABLE.",
                "  LOOP AT p_items INTO DATA(st_item).",
                "ENDLOOP.",
                "ENDFORM.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("DATA st_item TYPE ty_item.", result["fixed_source"])
        self.assertIn("  LOOP AT p_items INTO st_item.", result["fixed_source"])
        self.assertEqual(result["final_issues"], [])

    def test_incompatible_perform_calls_do_not_fix_inline_loop(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "TYPES ty_other TYPE makt.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "DATA t_other TYPE STANDARD TABLE OF ty_other.",
                "PERFORM load_items USING t_items.",
                "PERFORM load_items USING t_other.",
                "FORM load_items USING p_items TYPE STANDARD TABLE.",
                "  LOOP AT p_items INTO DATA(st_item).",
                "ENDLOOP.",
                "ENDFORM.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assert_issue_count(result, "INLINE_DATA_LOOP", 1)

    def test_read_table_inline_declaration_fixed_from_generic_row_type(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "READ TABLE t_items INTO DATA(st_item) INDEX 1.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("DATA st_item TYPE ty_item.", result["fixed_source"])
        self.assertIn("READ TABLE t_items INTO st_item INDEX 1.", result["fixed_source"])
        self.assertEqual(result["final_issues"], [])

    def test_inline_field_symbol_fixed_with_known_row_type(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "LOOP AT t_items ASSIGNING FIELD-SYMBOL(<st_item>).",
                "ENDLOOP.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("FIELD-SYMBOLS <st_item> TYPE ty_item.", result["fixed_source"])
        self.assertIn("LOOP AT t_items ASSIGNING <st_item>.", result["fixed_source"])
        self.assertEqual(result["final_issues"], [])

    def test_unknown_field_symbol_row_type_remains_unresolved(self):
        source = "LOOP AT t_items ASSIGNING FIELD-SYMBOL(<st_item>)."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assert_issue_count(result, "INLINE_FIELD_SYMBOL", 1)

    def test_no_duplicate_field_symbols_declaration(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "FIELD-SYMBOLS <st_item> TYPE ty_item.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "LOOP AT t_items ASSIGNING FIELD-SYMBOL(<st_item>).",
                "ENDLOOP.",
            ]
        )

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertEqual(fixed.count("FIELD-SYMBOLS <st_item> TYPE ty_item."), 1)
        self.assertIn("LOOP AT t_items ASSIGNING <st_item>.", fixed)

    def test_read_table_inline_field_symbol_fixed_with_known_row_type(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "READ TABLE t_items ASSIGNING FIELD-SYMBOL(<fs_item>) WITH KEY id = w_id.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("FIELD-SYMBOLS <fs_item> TYPE ty_item.", result["fixed_source"])
        self.assertIn("READ TABLE t_items ASSIGNING <fs_item> WITH KEY id = w_id.", result["fixed_source"])
        self.assertEqual(result["final_issues"], [])

    def test_multiline_read_table_inline_field_symbol_statement_is_preserved(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "READ TABLE t_items ASSIGNING FIELD-SYMBOL(<fs_item>)",
                "  WITH KEY id = w_id.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("FIELD-SYMBOLS <fs_item> TYPE ty_item.", result["fixed_source"])
        self.assertIn("READ TABLE t_items ASSIGNING <fs_item>", result["fixed_source"])
        self.assertIn("  WITH KEY id = w_id.", result["fixed_source"])
        self.assertEqual(result["final_issues"], [])

    def test_unknown_read_table_field_symbol_row_type_remains_unresolved(self):
        source = "READ TABLE t_items ASSIGNING FIELD-SYMBOL(<fs_item>) WITH KEY id = w_id."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assert_issue_count(result, "INLINE_FIELD_SYMBOL", 1)

    def test_existing_field_symbols_declaration_prevents_read_table_duplication(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "FIELD-SYMBOLS <fs_item> TYPE ty_item.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "READ TABLE t_items ASSIGNING FIELD-SYMBOL(<fs_item>) WITH KEY id = w_id.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"].count("FIELD-SYMBOLS <fs_item> TYPE ty_item."), 1)
        self.assertIn("READ TABLE t_items ASSIGNING <fs_item> WITH KEY id = w_id.", result["fixed_source"])
        self.assertEqual(result["final_issues"], [])

    def test_final_validation_reaches_zero_for_read_table_inline_field_symbol_example(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "READ TABLE t_items ASSIGNING FIELD-SYMBOL(<fs_item>)",
                "  WITH KEY id = w_id.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["original_issue_count"], 1)
        self.assertEqual(result["final_issue_count"], 0)
        self.assertEqual(result["final_issues"], [])

    def test_generic_form_table_parameter_row_type_inferred_from_perform(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "PERFORM render_items USING t_items.",
                "FORM render_items USING pt_output TYPE STANDARD TABLE.",
                "  LOOP AT pt_output INTO DATA(st_out).",
                "ENDLOOP.",
                "ENDFORM.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("DATA st_out TYPE ty_item.", result["fixed_source"])
        self.assertIn("  LOOP AT pt_output INTO st_out.", result["fixed_source"])
        self.assertEqual(result["final_issues"], [])

    def test_incompatible_perform_calls_do_not_infer_generic_table_parameter(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "TYPES ty_other TYPE makt.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "DATA t_other TYPE STANDARD TABLE OF ty_other.",
                "PERFORM render_items USING t_items.",
                "PERFORM render_items USING t_other.",
                "FORM render_items USING pt_output TYPE STANDARD TABLE.",
                "  LOOP AT pt_output INTO DATA(st_out).",
                "ENDLOOP.",
                "ENDFORM.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assert_issue_count(result, "INLINE_DATA_LOOP", 1)

    def test_known_signature_call_parameter_is_fixed(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'ANY_CALL'",
                "  IMPORTING",
                "    result = DATA(w_result).",
            ]
        )

        result = auto_fix_abap(source, callable_signatures={"ANY_CALL": {"result": "string"}})

        self.assertIn("DATA w_result TYPE string.", result["fixed_source"])
        self.assertIn("    result = w_result.", result["fixed_source"])
        self.assertEqual(result["final_issues"], [])

    def test_unknown_signature_call_parameter_is_left_unresolved(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'ANY_CALL'",
                "  IMPORTING",
                "    result = DATA(w_result).",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assert_issue_count(result, "INLINE_DATA_CALL_PARAMETER", 1)

    def test_complete_callable_mapping_allows_safe_correction(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'Z_TEST_FUNCTION'",
                "  TABLES",
                "    MESSAGE = old_target.",
            ]
        )

        result = auto_fix_abap(
            source,
            callable_signatures=callable_signature(),
            callable_mappings=callable_mapping(),
        )

        self.assertIn("  EXPORTING", result["fixed_source"])
        self.assertIn("    ID = source_structure-field1", result["fixed_source"])
        self.assertIn("  IMPORTING", result["fixed_source"])
        self.assertIn("    MESSAGE = target_variable.", result["fixed_source"])
        self.assertNotIn("TABLES", result["fixed_source"])
        self.assertEqual([issue for issue in result["final_issues"] if issue["rule_id"].startswith("CALLABLE_")], [])

    def test_incomplete_callable_mapping_remains_unresolved(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'Z_TEST_FUNCTION'",
                "  TABLES",
                "    MESSAGE = old_target.",
            ]
        )
        incomplete_mapping = {
            "callable": "Z_TEST_FUNCTION",
            "parameter_mappings": {"IMPORTING": {"MESSAGE": "target_variable"}},
        }

        result = auto_fix_abap(
            source,
            callable_signatures=callable_signature(),
            callable_mappings=incomplete_mapping,
        )

        self.assertEqual(result["fixed_source"], source)
        self.assertTrue([issue for issue in result["final_issues"] if issue["rule_id"].startswith("CALLABLE_")])

    def test_bapi_message_getdetail_tables_section_is_removed(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'BAPI_MESSAGE_GETDETAIL'",
                "  EXPORTING",
                "    id = lv_id",
                "  TABLES",
                "    text = lt_text",
                "  IMPORTING",
                "    message = lv_message.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "CALL FUNCTION 'BAPI_MESSAGE_GETDETAIL'",
                    "  EXPORTING",
                    "    id = lv_id",
                    "  IMPORTING",
                    "    message = lv_message.",
                ]
            ),
        )
        self.assertIn(
            "BAPI_MESSAGE_GETDETAIL_TABLES_SECTION_REMOVED",
            [fix["rule_id"] for fix in result["fixes"]],
        )

    def test_bapi_message_getdetail_tables_message_parameter_is_removed_by_section(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'BAPI_MESSAGE_GETDETAIL'",
                "  EXPORTING",
                "    id = lv_id",
                "  TABLES",
                "    message = lt_text",
                "  IMPORTING",
                "    message = lv_message.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "CALL FUNCTION 'BAPI_MESSAGE_GETDETAIL'",
                    "  EXPORTING",
                    "    id = lv_id",
                    "  IMPORTING",
                    "    message = lv_message.",
                ]
            ),
        )

    def test_bapi_message_getdetail_tables_removal_preserves_other_sections_and_other_calls(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'BAPI_MESSAGE_GETDETAIL'",
                "  EXPORTING",
                "    id = lv_id",
                "  CHANGING",
                "    message = lv_message",
                "  TABLES",
                "    text = lt_text",
                "  EXCEPTIONS",
                "    OTHERS = 1.",
                "CALL FUNCTION 'Z_OTHER_FUNCTION'",
                "  TABLES",
                "    text = lt_text.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "CALL FUNCTION 'BAPI_MESSAGE_GETDETAIL'",
                    "  EXPORTING",
                    "    id = lv_id",
                    "  CHANGING",
                    "    message = lv_message",
                    "  EXCEPTIONS",
                    "    OTHERS = 1.",
                    "CALL FUNCTION 'Z_OTHER_FUNCTION'",
                    "  TABLES",
                    "    text = lt_text.",
                ]
            ),
        )

    def test_inline_declaration_inserted_after_required_types(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "LOOP AT t_items INTO DATA(st_item).",
                "ENDLOOP.",
            ]
        )

        fixed_lines = auto_fix_abap(source)["fixed_source"].splitlines()

        self.assertLess(fixed_lines.index("TYPES ty_item TYPE mara."), fixed_lines.index("DATA st_item TYPE ty_item."))

    def test_unknown_row_type_is_left_unchanged(self):
        source = "LOOP AT item_table INTO DATA(item_row)."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["original_issue_count"], result["final_issue_count"])

    def test_local_ty_structure_name_removed_when_field_catalogue_is_explicit(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'REUSE_ALV_GRID_DISPLAY'",
                "  EXPORTING",
                "    i_structure_name = 'TY_OUTPUT'",
                "  TABLES",
                "    t_fieldcat = t_fieldcat.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertNotIn("i_structure_name", result["fixed_source"])
        self.assertIn("t_fieldcat = t_fieldcat.", result["fixed_source"])
        self.assertFalse([issue for issue in result["final_issues"] if issue["rule_id"] == "ALV_LOCAL_STRUCTURE_NAME"])

    def test_local_ty_structure_name_removed_when_it_fieldcat_is_supplied(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'REUSE_ALV_GRID_DISPLAY'",
                "  EXPORTING",
                "    i_structure_name = 'TY_OUTPUT'.",
                "  TABLES",
                "    it_fieldcat = t_fieldcat.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertNotIn("i_structure_name", result["fixed_source"])
        self.assertIn("it_fieldcat = t_fieldcat.", result["fixed_source"])
        self.assertFalse([issue for issue in result["final_issues"] if issue["rule_id"] == "ALV_LOCAL_STRUCTURE_NAME"])

    def test_local_ty_structure_name_removed_when_t_fieldcat_is_supplied_after_period(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'REUSE_ALV_GRID_DISPLAY'",
                "  EXPORTING",
                "    i_structure_name = 'TY_OUTPUT'.",
                "  TABLES",
                "    t_fieldcat = t_fieldcat.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertNotIn("i_structure_name", result["fixed_source"])
        self.assertIn("t_fieldcat = t_fieldcat.", result["fixed_source"])
        self.assertFalse([issue for issue in result["final_issues"] if issue["rule_id"] == "ALV_LOCAL_STRUCTURE_NAME"])

    def test_valid_ddic_structure_name_is_preserved(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'REUSE_ALV_GRID_DISPLAY'",
                "  EXPORTING",
                "    i_structure_name = 'MARA'",
                "  TABLES",
                "    t_fieldcat = t_fieldcat.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("i_structure_name = 'MARA'", result["fixed_source"])
        self.assertEqual(result["fixed_source"], source)

    def test_final_issue_count_reaches_zero_for_field_symbol_form_and_alv_example(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "PERFORM render_items USING t_items.",
                "FORM render_items USING pt_output TYPE STANDARD TABLE.",
                "  LOOP AT pt_output INTO DATA(st_out).",
                "  LOOP AT pt_output ASSIGNING FIELD-SYMBOL(<st_out>).",
                "  ENDLOOP.",
                "ENDLOOP.",
                "ENDFORM.",
                "CALL FUNCTION 'REUSE_ALV_GRID_DISPLAY'",
                "  EXPORTING",
                "    i_structure_name = 'TY_OUTPUT'",
                "  TABLES",
                "    it_fieldcat = t_fieldcat.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertGreater(result["original_issue_count"], 0)
        self.assertEqual(result["final_issue_count"], 0)
        self.assertEqual(result["final_issues"], [])

    def test_table_expression_index_remains_reported_and_unfixed(self):
        source = "st_docnum = t_docnums[ 1 ]."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assert_issue_count(result, "TABLE_EXPRESSION", 1)

    def test_checkbox_type_length_becomes_as_checkbox(self):
        source = "PARAMETERS: p_flag TYPE c LENGTH 1 AS CHECKBOX DEFAULT space."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertNotIn("INVALID_CHECKBOX_SYNTAX", result["diagnostics"]["changed_rules"])

    def test_checkbox_default_x_is_preserved(self):
        source = "PARAMETERS p_flag TYPE c LENGTH 1 AS CHECKBOX DEFAULT 'X'."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertNotIn("INVALID_CHECKBOX_SYNTAX", result["diagnostics"]["changed_rules"])

    def test_checkbox_default_space_is_removed(self):
        source = "PARAMETERS p_flag TYPE c LENGTH 1 AS CHECKBOX DEFAULT space."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertNotIn("INVALID_CHECKBOX_SYNTAX", result["diagnostics"]["changed_rules"])

    def test_chained_checkbox_item_is_not_modified(self):
        source = "\n".join(
            [
                "PARAMETERS: p_zmdid TYPE zmdid,",
                "            p_idoc TYPE c LENGTH 1 AS CHECKBOX,",
                "            p_alv RADIOBUTTON GROUP rbg DEFAULT 'X',",
                "            p_file RADIOBUTTON GROUP rbg.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertNotIn("INVALID_CHECKBOX_SYNTAX", result["diagnostics"]["changed_rules"])

    def test_chained_checkbox_default_space_is_not_modified(self):
        source = "\n".join(
            [
                "PARAMETERS: p_zmdid TYPE zmdid,",
                "            p_idoc TYPE c LENGTH 1 AS CHECKBOX DEFAULT space.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertNotIn("INVALID_CHECKBOX_SYNTAX", result["diagnostics"]["changed_rules"])

    def test_chained_checkbox_default_x_is_not_modified(self):
        source = "\n".join(
            [
                "PARAMETERS: p_zmdid TYPE zmdid,",
                "            p_idoc TYPE c LENGTH 1 AS CHECKBOX DEFAULT 'X'.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertNotIn("INVALID_CHECKBOX_SYNTAX", result["diagnostics"]["changed_rules"])

    def test_checkbox_without_type_is_unchanged(self):
        source = "PARAMETERS p_flag AS CHECKBOX."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["final_issues"], [])

    def test_non_checkbox_parameters_are_unchanged(self):
        source = "PARAMETERS p_date TYPE sy-datum."

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["final_issues"], [])

    def test_valid_chained_declaration_blocks_are_unchanged(self):
        source = "\n".join(
            [
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

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["diagnostics"]["source_before_fixer"], source)
        self.assertEqual(result["diagnostics"]["source_after_fixer"], source)
        self.assertEqual(result["diagnostics"]["changed_rules"], [])

    def test_valid_chained_declaration_statement_types_are_unchanged(self):
        source = "\n".join(
            [
                "TABLES: edidc,",
                "        edids.",
                "TYPES: ty_count TYPE i,",
                "       ty_message TYPE c LENGTH 40.",
                "DATA: count TYPE i,",
                "      message TYPE c LENGTH 40.",
                "CONSTANTS: c_active TYPE c VALUE 'X',",
                "           c_inactive TYPE c VALUE space.",
                "PARAMETERS: p_first TYPE c LENGTH 1,",
                "            p_second AS CHECKBOX.",
                "SELECT-OPTIONS: s_doc FOR edidc-docnum,",
                "                s_date FOR edidc-credat.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["diagnostics"]["source_before_fixer"], source)
        self.assertEqual(result["diagnostics"]["source_after_fixer"], source)
        self.assertEqual(result["diagnostics"]["changed_rules"], [])

    def test_missing_chained_select_options_delimiters_are_fixed(self):
        source = "\n".join(
            [
                "SELECT-OPTIONS: s_id01   FOR zmd_mpe0001-identifier",
                "                s_id06   FOR zmd_mpe0006-identifier",
                "                s_credat FOR edidc-credat",
                "                s_mestyp FOR edidc-mestyp",
                "                s_status FOR edidc-status.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "SELECT-OPTIONS: s_id01   FOR zmd_mpe0001-identifier,",
                    "                s_id06   FOR zmd_mpe0006-identifier,",
                    "                s_credat FOR edidc-credat,",
                    "                s_mestyp FOR edidc-mestyp,",
                    "                s_status FOR edidc-status.",
                ]
            ),
        )
        self.assertEqual(result["diagnostics"]["changed_rules"], ["CHAINED_SELECT_OPTIONS_DELIMITERS"])

    def test_missing_chained_select_options_delimiters_preserve_comments(self):
        source = "\n".join(
            [
                "SELECT-OPTIONS: s_doc FOR edidc-docnum \" document",
                "                s_date FOR edidc-credat. \" date",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "SELECT-OPTIONS: s_doc FOR edidc-docnum, \" document",
                    "                s_date FOR edidc-credat. \" date",
                ]
            ),
        )
        self.assertEqual(result["diagnostics"]["changed_rules"], ["CHAINED_SELECT_OPTIONS_DELIMITERS"])

    def test_invalid_select_options_for_field_is_fixed_generically(self):
        source = "\n".join(
            [
                "SELECT-OPTIONS s_id01 FOR FIELD zmd_mpe0001-identifier.",
                "SELECT-OPTIONS s_status FOR FIELD edidc-status.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "SELECT-OPTIONS s_id01 FOR zmd_mpe0001-identifier.",
                    "SELECT-OPTIONS s_status FOR edidc-status.",
                ]
            ),
        )
        self.assertEqual(result["diagnostics"]["changed_rules"], ["INVALID_SELECT_OPTIONS_FOR_FIELD"])

    def test_invalid_chained_select_options_for_field_is_fixed_generically(self):
        source = "\n".join(
            [
                "SELECT-OPTIONS: s_id01 FOR FIELD zmd_mpe0001-identifier,",
                "                s_id06 FOR FIELD zmd_mpe0006-identifier,",
                "                s_status FOR FIELD edidc-status.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "SELECT-OPTIONS: s_id01 FOR zmd_mpe0001-identifier,",
                    "                s_id06 FOR zmd_mpe0006-identifier,",
                    "                s_status FOR edidc-status.",
                ]
            ),
        )
        self.assertEqual(result["diagnostics"]["changed_rules"], ["INVALID_SELECT_OPTIONS_FOR_FIELD"])

    def test_select_options_for_field_fix_does_not_touch_other_statements(self):
        source = "\n".join(
            [
                "DATA text TYPE string.",
                "text = 'SELECT-OPTIONS s_doc FOR FIELD edidc-docnum.'.",
                "WRITE: / 'FOR FIELD edidc-docnum'.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertNotIn("INVALID_SELECT_OPTIONS_FOR_FIELD", result["diagnostics"]["changed_rules"])

    def test_global_data_declarations_are_grouped_by_prefix_generically(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "DATA t_first TYPE STANDARD TABLE OF ty_first.",
                "DATA w_first TYPE string.",
                "DATA t_second TYPE STANDARD TABLE OF ty_second.",
                "DATA st_first TYPE ty_first.",
                "DATA t_third TYPE STANDARD TABLE OF ty_third.",
                "DATA w_second TYPE ty_second.",
                "DATA st_second TYPE ty_second.",
                "DATA other_value TYPE string.",
                "PARAMETERS p_id TYPE c.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "REPORT ztest.",
                    "DATA t_first TYPE STANDARD TABLE OF ty_first.",
                    "DATA t_second TYPE STANDARD TABLE OF ty_second.",
                    "DATA t_third TYPE STANDARD TABLE OF ty_third.",
                    "DATA st_first TYPE ty_first.",
                    "DATA st_second TYPE ty_second.",
                    "DATA w_first TYPE string.",
                    "DATA w_second TYPE ty_second.",
                    "DATA other_value TYPE string.",
                    "PARAMETERS p_id TYPE c.",
                ]
            ),
        )
        self.assertIn("DATA_DECLARATION_PREFIX_ORDER", result["diagnostics"]["changed_rules"])

    def test_data_prefix_order_does_not_move_local_form_declarations(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "DATA w_global TYPE string.",
                "DATA t_global TYPE STANDARD TABLE OF ty_row.",
                "FORM do_work.",
                "  DATA w_local TYPE string.",
                "  DATA t_local TYPE STANDARD TABLE OF ty_row.",
                "ENDFORM.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn(
            "\n".join(
                [
                    "FORM do_work.",
                    "  DATA w_local TYPE string.",
                    "  DATA t_local TYPE STANDARD TABLE OF ty_row.",
                    "ENDFORM.",
                ]
            ),
            result["fixed_source"],
        )

    def test_data_prefix_order_leaves_mixed_chained_data_statement_unchanged(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "DATA: w_value TYPE string,",
                "      t_values TYPE STANDARD TABLE OF ty_value.",
                "PARAMETERS p_id TYPE c.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assertNotIn("DATA_DECLARATION_PREFIX_ORDER", result["diagnostics"]["changed_rules"])

    def test_checkbox_fix_is_disabled_for_selection_screen_declarations(self):
        source = "PARAMETERS: p_flag TYPE c LENGTH 1 AS CHECKBOX DEFAULT space."

        result = auto_fix_abap(source)

        self.assertEqual(result["original_issue_count"], 1)
        self.assertEqual(result["fixed_source"], source)
        self.assertEqual(result["final_issue_count"], 1)
        self.assert_issue_count(result, "INVALID_CHECKBOX_SYNTAX", 1)
        self.assertEqual(result["diagnostics"]["changed_rules"], [])

    def test_forbidden_prefix_renamer_does_not_touch_selection_screen_declarations(self):
        source = "\n".join(
            [
                "PARAMETERS: gt_flag AS CHECKBOX,",
                "            lt_mode TYPE c.",
                "SELECT-OPTIONS: gt_doc FOR edidc-docnum,",
                "                lt_date FOR edidc-credat.",
                "DATA gt_items TYPE STANDARD TABLE OF mara.",
                "APPEND mara TO gt_items.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertIn("PARAMETERS: gt_flag AS CHECKBOX,", result["fixed_source"])
        self.assertIn("            lt_mode TYPE c.", result["fixed_source"])
        self.assertIn("SELECT-OPTIONS: gt_doc FOR edidc-docnum,", result["fixed_source"])
        self.assertIn("                lt_date FOR edidc-credat.", result["fixed_source"])
        self.assertIn("DATA t_items TYPE STANDARD TABLE OF mara.", result["fixed_source"])
        self.assertIn("APPEND mara TO t_items.", result["fixed_source"])
        self.assertEqual(result["diagnostics"]["changed_rules"], ["FORBIDDEN_NAMING_PREFIX"])

    def test_multiple_early_declarations_are_moved_after_types(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "DATA st_edidc TYPE ty_edidc.",
                "DATA t_edidc TYPE STANDARD TABLE OF ty_edidc.",
                "TYPES: BEGIN OF ty_edidc,",
                "  docnum TYPE edidc-docnum,",
                "END OF ty_edidc.",
                "PARAMETERS p_doc TYPE edidc-docnum.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(
            result["fixed_source"],
            "\n".join(
                [
                    "REPORT ztest.",
                    "TYPES: BEGIN OF ty_edidc,",
                    "  docnum TYPE edidc-docnum,",
                    "END OF ty_edidc.",
                    "DATA t_edidc TYPE STANDARD TABLE OF ty_edidc.",
                    "DATA st_edidc TYPE ty_edidc.",
                    "PARAMETERS p_doc TYPE edidc-docnum.",
                ]
            ),
        )
        self.assertFalse([issue for issue in result["final_issues"] if issue["rule_id"] == "TYPE_USED_BEFORE_DECLARATION"])

    def test_chained_tables_continuations_do_not_block_declaration_reordering(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "DATA st_edidc TYPE ty_edidc.",
                "TABLES: edidc,",
                "        edids.",
                "PARAMETERS: p_doc TYPE edidc-docnum,",
                "            p_flag AS CHECKBOX.",
                "TYPES: BEGIN OF ty_edidc,",
                "  docnum TYPE edidc-docnum,",
                "END OF ty_edidc.",
            ]
        )

        result = auto_fix_abap(source)
        fixed_lines = result["fixed_source"].splitlines()

        self.assertLess(fixed_lines.index("END OF ty_edidc."), fixed_lines.index("DATA st_edidc TYPE ty_edidc."))
        self.assertFalse([issue for issue in result["final_issues"] if issue["rule_id"] == "TYPE_USED_BEFORE_DECLARATION"])

    def test_arbitrary_comma_line_still_blocks_declaration_reordering(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "DATA st_edidc TYPE ty_edidc.",
                "edids,",
                "TYPES ty_edidc TYPE edidc.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assert_issue_count(result, "TYPE_USED_BEFORE_DECLARATION", 1)

    def test_declaration_text_is_preserved_when_moved(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "  DATA st_edidc TYPE ty_edidc.",
                "TYPES ty_edidc TYPE edidc.",
            ]
        )

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertIn("  DATA st_edidc TYPE ty_edidc.", fixed)

    def test_executable_statements_are_not_moved_for_declaration_order_fix(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "DATA st_edidc TYPE ty_edidc.",
                "WRITE sy-repid.",
                "TYPES ty_edidc TYPE edidc.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assert_issue_count(result, "TYPE_USED_BEFORE_DECLARATION", 1)

    def test_declarations_inside_forms_are_not_moved_automatically(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "FORM build.",
                "  DATA st_edidc TYPE ty_edidc.",
                "ENDFORM.",
                "TYPES ty_edidc TYPE edidc.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["fixed_source"], source)
        self.assert_issue_count(result, "TYPE_USED_BEFORE_DECLARATION", 1)

    def test_final_validation_reaches_zero_for_later_ty_edidc_example(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "DATA st_edidc TYPE ty_edidc.",
                "TYPES: BEGIN OF ty_edidc,",
                "  docnum TYPE edidc-docnum,",
                "END OF ty_edidc.",
            ]
        )

        result = auto_fix_abap(source)

        self.assertEqual(result["original_issue_count"], 1)
        self.assertEqual(result["final_issue_count"], 0)
        self.assertEqual(result["final_issues"], [])

    def test_later_type_fix_preserves_chained_parameters(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "DATA st_edids TYPE ty_edids.",
                "DATA st_edidc_row TYPE ty_edidc.",
                "TABLES: edidc,",
                "        edids.",
                "PARAMETERS: p_zmdid TYPE zmdid,",
                "            p_idoc TYPE c LENGTH 1 AS CHECKBOX,",
                "            p_alv RADIOBUTTON GROUP rbg DEFAULT 'X',",
                "            p_file RADIOBUTTON GROUP rbg.",
                "TYPES: BEGIN OF ty_edidc,",
                "  docnum TYPE edidc-docnum,",
                "END OF ty_edidc.",
                "TYPES: BEGIN OF ty_edids,",
                "  docnum TYPE edids-docnum,",
                "END OF ty_edids.",
            ]
        )

        result = auto_fix_abap(source)
        fixed_lines = result["fixed_source"].splitlines()

        self.assertGreater(result["original_issue_count"], 0)
        self.assertEqual(result["final_issue_count"], 1)
        self.assert_issue_count(result, "INVALID_CHECKBOX_SYNTAX", 1)
        self.assertLess(fixed_lines.index("END OF ty_edidc."), fixed_lines.index("DATA st_edidc_row TYPE ty_edidc."))
        self.assertLess(fixed_lines.index("END OF ty_edids."), fixed_lines.index("DATA st_edids TYPE ty_edids."))
        self.assertIn("            p_idoc TYPE c LENGTH 1 AS CHECKBOX,", result["fixed_source"])
        self.assertIn("TYPE_USED_BEFORE_DECLARATION", result["diagnostics"]["changed_rules"])
        self.assertNotIn("INVALID_CHECKBOX_SYNTAX", result["diagnostics"]["changed_rules"])

    def test_inline_empty_string_assignment_becomes_explicit_string(self):
        source = "DATA(w_text) = ''."

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertIn("DATA w_text TYPE string.", fixed)
        self.assertIn("w_text = ''.", fixed)

    def test_parenthesised_data_type_declaration_is_fixed(self):
        source = "\n".join(
            [
                "  DATA(lv_error_msg) TYPE string.",
                "DATA(w_header_line) TYPE string.",
            ]
        )

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertIn("  DATA lv_error_msg TYPE string.", fixed)
        self.assertIn("DATA w_header_line TYPE string.", fixed)
        self.assertNotIn("DATA(lv_error_msg)", fixed)

    def test_inline_abap_false_assignment_becomes_abap_bool(self):
        source = "DATA(lv_found) = abap_false."

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertIn("DATA lv_found TYPE abap_bool.", fixed)
        self.assertIn("lv_found = abap_false.", fixed)

    def test_existing_declaration_prevents_duplicate_inline_assignment_declaration(self):
        source = "\n".join(
            [
                "DATA lv_found TYPE abap_bool.",
                "DATA(lv_found) = abap_false.",
            ]
        )

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertEqual(fixed.count("DATA lv_found TYPE abap_bool."), 1)
        self.assertIn("lv_found = abap_false.", fixed)

    def test_simple_concatenation_converted_to_concatenate(self):
        source = "w_filename = p_gen-zfile && '_' && sy-datum && '.csv'."

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertIn("CONCATENATE p_gen-zfile '_' sy-datum '.csv'", fixed)
        self.assertIn("  INTO w_filename.", fixed)

    def test_no_duplicate_declaration_when_variable_exists(self):
        source = "\n".join(
            [
                "DATA item_row TYPE mara.",
                "DATA item_table TYPE STANDARD TABLE OF mara.",
                "LOOP AT item_table INTO DATA(item_row).",
                "ENDLOOP.",
            ]
        )

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertEqual(fixed.count("DATA item_row TYPE mara."), 1)
        self.assertIn("LOOP AT item_table INTO item_row.", fixed)

    def test_no_duplicate_declaration_for_generic_inline_loop(self):
        source = "\n".join(
            [
                "TYPES ty_item TYPE mara.",
                "DATA st_item TYPE ty_item.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
                "LOOP AT t_items INTO DATA(st_item).",
                "ENDLOOP.",
            ]
        )

        fixed = auto_fix_abap(source)["fixed_source"]

        self.assertEqual(fixed.count("DATA st_item TYPE ty_item."), 1)
        self.assertIn("LOOP AT t_items INTO st_item.", fixed)

    def test_no_neutral_test_names_or_callable_specific_rules_in_services(self):
        root = Path(__file__).resolve().parents[1]
        service_text = "\n".join(
            [
                (root / "services" / "validator.py").read_text(encoding="utf-8"),
                (root / "services" / "fixer.py").read_text(encoding="utf-8"),
            ]
        )

        for token in ("t_items", "p_items", "ty_item", "st_item", "ANY_CALL", "Z_TEST_FUNCTION"):
            self.assertNotIn(token, service_text)

    def test_fixed_inline_code_remains_downloadable(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_fixer_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            generated_abap = "\n".join(
                [
                    "DATA item_table TYPE STANDARD TABLE OF mara.",
                    "LOOP AT item_table INTO DATA(item_row).",
                    "ENDLOOP.",
                ]
            )

            with patch(
                "services.create_abap.generate_abap",
                return_value={"text": generated_abap, "model": "test-model", "usage": None},
            ):
                app = create_app(
                    {
                        "TESTING": True,
                        "UPLOAD_FOLDER": str(uploads_folder),
                        "JOBS_FOLDER": str(jobs_folder),
                        "CREATE_ABAP_PROMPT": str(prompt_path),
                    }
                )
                client = app.test_client()
                upload = client.post(
                    "/upload",
                    data={"abap_file": (BytesIO(b"Create a test report."), "request.txt")},
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )
                job_id = upload.headers["Location"].rsplit("/", 1)[-1]
                wait_for_status(jobs_folder, job_id, "Complete")
                download = client.get(f"/download/{job_id}")
                downloaded = download.get_data(as_text=True).replace("\r\n", "\n")
                download.close()

                self.assertIn("DATA item_row TYPE mara.", downloaded)
                self.assertIn("LOOP AT item_table INTO item_row.", downloaded)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def assert_issue_count(self, result, rule_id, expected_count):
        issues = [issue for issue in result["final_issues"] if issue["rule_id"] == rule_id]
        self.assertEqual(len(issues), expected_count, result["final_issues"])


def callable_signature():
    return {
        "Z_TEST_FUNCTION": {
            "parameters": {
                "ID": {"direction": "EXPORTING", "abap_type": "char10", "required": True},
                "MESSAGE": {"direction": "IMPORTING", "abap_type": "string", "required": True},
                "OPTIONAL_TEXT": {"direction": "EXPORTING", "abap_type": "string", "required": False},
                "RESULT": {"direction": "RETURNING", "abap_type": "string", "required": False},
            }
        }
    }


def callable_mapping():
    return {
        "callable": "Z_TEST_FUNCTION",
        "parameter_mappings": {
            "EXPORTING": {"ID": "source_structure-field1"},
            "IMPORTING": {"MESSAGE": "target_variable"},
        },
    }


def wait_for_status(jobs_folder, job_id, expected_status, timeout=5):
    deadline = real_time.time() + timeout
    while real_time.time() < deadline:
        progress = get_progress(jobs_folder, job_id)
        if progress["status"] == expected_status:
            return progress
        real_time.sleep(0.02)
    return get_progress(jobs_folder, job_id)


if __name__ == "__main__":
    unittest.main()
