from io import BytesIO
from pathlib import Path
import shutil
import time as real_time
import unittest
from uuid import uuid4
from unittest.mock import patch

from app import create_app
from services.progress import get_progress
from services.validator import validate_abap


class ValidatorTest(unittest.TestCase):
    def test_no_issues_for_classical_abap(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "DATA lv_text TYPE string.",
                "DATA table_items TYPE STANDARD TABLE OF mara.",
                "DATA item_row TYPE mara.",
                "READ TABLE table_items INTO item_row INDEX 1.",
                "PARAMETERS p_flag AS CHECKBOX.",
                "PARAMETERS p_alv RADIOBUTTON GROUP r1 DEFAULT 'X'.",
            ]
        )

        self.assertEqual(validate_abap(source), [])

    def test_data_value_declaration_does_not_trigger_constructor_expression(self):
        issues = validate_abap("DATA gv_name TYPE scrfname VALUE 'CONTAINER'.")

        self.assertFalse([issue for issue in issues if issue["rule_id"] == "CONSTRUCTOR_EXPRESSION"])

    def test_data_inline_detection(self):
        issues = validate_abap("DATA(lv_text) = 'x'.")

        self.assert_issue(issues, "INLINE_DATA", 1)

    def test_indented_asterisk_comment_is_reported(self):
        issues = validate_abap("     * build error message")

        self.assert_issue(issues, "INDENTED_ASTERISK_COMMENT", 1)
        matching = [issue for issue in issues if issue["rule_id"] == "INDENTED_ASTERISK_COMMENT"]
        self.assertIn("column 1", matching[0]["message"])

    def test_valid_asterisk_comments_and_non_comments_are_not_reported(self):
        source = "\n".join(
            [
                "* build error message",
                "w_total = w_count * w_price.",
                "w_text = '* build error message'.",
                '     " build error message',
            ]
        )

        issues = validate_abap(source)

        self.assertFalse([issue for issue in issues if issue["rule_id"] == "INDENTED_ASTERISK_COMMENT"])

    def test_parenthesised_data_type_uses_invalid_declaration_rule_id(self):
        issues = validate_abap("DATA(lv_error_msg) TYPE string.")

        self.assert_issue(issues, "INVALID_DATA_DECLARATION", 1)
        self.assertFalse([issue for issue in issues if issue["rule_id"] == "INLINE_DATA"])

    def test_classical_data_type_declaration_passes(self):
        self.assertEqual(validate_abap("DATA lv_error_msg TYPE string."), [])

    def test_at_data_inline_detection(self):
        issues = validate_abap("SELECT * FROM mara INTO @DATA(ls_mara).")

        self.assert_issue(issues, "INLINE_DATA_SELECT", 1)

    def test_split_into_data_detection(self):
        issues = validate_abap("SPLIT lv_text AT ',' INTO DATA(lv_a) DATA(lv_b).")

        self.assert_issue(issues, "INLINE_DATA_SPLIT", 1)

    def test_loop_into_data_detection(self):
        issues = validate_abap("LOOP AT lt_items INTO DATA(ls_item).")

        self.assert_issue(issues, "INLINE_DATA_LOOP", 1)

    def test_string_template_detection(self):
        issues = validate_abap("lv_text = |Hello { lv_name }|.")

        self.assert_issue(issues, "STRING_TEMPLATE", 1)

    def test_constructor_expression_detection(self):
        issues = validate_abap("lt_items = VALUE #( ).")

        self.assert_issue(issues, "CONSTRUCTOR_EXPRESSION", 1)

    def test_leave_list_page_is_invalid_for_classical_ecc_target(self):
        issues = validate_abap("LEAVE LIST-PAGE.")

        self.assert_issue(issues, "INVALID_LEAVE_LIST_PAGE", 1)
        matching = [issue for issue in issues if issue["rule_id"] == "INVALID_LEAVE_LIST_PAGE"]
        self.assertIn("classical SAP ECC target", matching[0]["message"])
        self.assertEqual(matching[0]["suggested_fix"], "LEAVE LIST-PROCESSING.")

    def test_leave_list_page_in_comments_and_strings_is_ignored(self):
        source = "\n".join(
            [
                '" LEAVE LIST-PAGE.',
                "DATA w_text TYPE string VALUE 'LEAVE LIST-PAGE.'.",
            ]
        )

        issues = validate_abap(source)

        self.assertFalse([issue for issue in issues if issue["rule_id"] == "INVALID_LEAVE_LIST_PAGE"])

    def test_leave_report_inside_user_command_is_reported(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "AT USER-COMMAND.",
                "  LEAVE REPORT.",
            ]
        )

        issue = self.assert_issue(validate_abap(source), "ABAP_LIST_PROCESSING_EXIT_MISMATCH", 3)
        self.assertIn("not valid", issue["message"])
        self.assertEqual(issue["suggested_fix"], "Use LEAVE LIST-PROCESSING.")

    def test_leave_report_inside_line_selection_is_reported(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "AT LINE-SELECTION.",
                "  LEAVE REPORT.",
            ]
        )

        self.assert_issue(validate_abap(source), "ABAP_LIST_PROCESSING_EXIT_MISMATCH", 3)

    def test_leave_report_outside_list_processing_event_is_reported(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "START-OF-SELECTION.",
                "  LEAVE REPORT.",
            ]
        )

        self.assert_issue(validate_abap(source), "ABAP_LIST_PROCESSING_EXIT_MISMATCH", 3)

    def test_invalid_checkbox_syntax(self):
        issues = validate_abap("PARAMETERS p_flag AS CHECKBOX TYPE c.")

        self.assert_issue(issues, "INVALID_CHECKBOX_SYNTAX", 1)

    def test_chained_parameters_checkbox_syntax_is_reported(self):
        source = "\n".join(
            [
                "PARAMETERS: p_zmdid TYPE zmdid,",
                "            p_idoc TYPE c LENGTH 1 AS CHECKBOX,",
                "            p_alv RADIOBUTTON GROUP rbg DEFAULT 'X',",
                "            p_file RADIOBUTTON GROUP rbg.",
            ]
        )

        issues = validate_abap(source)

        self.assert_issue(issues, "INVALID_CHECKBOX_SYNTAX", 2)

    def test_radio_button_out_token_is_invalid_parameter_declaration(self):
        source = "\n".join(
            [
                "PARAMETERS: p_zmdid TYPE zmdid,",
                "            p_alv AS RADIOBUTTON GROUP rg out 'X' DEFAULT 'X'.",
            ]
        )

        issue = self.assert_issue(validate_abap(source), "ABAP_INVALID_PARAMETER_DECLARATION", 2)
        self.assertEqual(issue["token"], "out")

    def test_broken_chained_declaration_trailing_comma_is_reported(self):
        source = "\n".join(
            [
                "DATA t_gen0007 TYPE STANDARD TABLE OF ty_gen0007 WITH EMPTY KEY,",
                "PARAMETERS: p_zmdid TYPE zmdid.",
            ]
        )

        self.assert_issue(validate_abap(source), "ABAP_BROKEN_CHAINED_DECLARATION", 1)

    def test_broken_chained_declaration_merged_with_next_statement_is_reported(self):
        source = "t_gen0007 TYPE STANDARD TABLE OF ty_gen0007 WITH EMPTY KEY, PARAMETERS: p_zmdid TYPE zmdid."

        self.assert_issue(validate_abap(source), "ABAP_BROKEN_CHAINED_DECLARATION", 1)

    def test_invalid_radio_button_syntax(self):
        issues = validate_abap("PARAMETERS p_alv AS RADIOBUTTON TYPE c.")

        self.assert_issue(issues, "INVALID_RADIOBUTTON_SYNTAX", 1)
        self.assert_issue(issues, "RADIOBUTTON_WITHOUT_GROUP", 1)

    def test_string_concatenation_detection(self):
        issues = validate_abap("gv_text = gv_a && gv_b.")

        self.assert_issue(issues, "STRING_CONCATENATION", 1)

    def test_multiple_concatenations_on_one_line_report_once(self):
        issues = [
            issue
            for issue in validate_abap("gv_text = gv_a && gv_b && gv_c.")
            if issue["rule_id"] == "STRING_CONCATENATION"
        ]

        self.assertEqual(len(issues), 1)

    def test_lvc_field_catalogue_with_reuse_alv(self):
        source = "\n".join(
            [
                "DATA fieldcat TYPE lvc_t_fcat.",
                "CALL FUNCTION 'REUSE_ALV_GRID_DISPLAY'",
                "  TABLES",
                "    t_fieldcat = fieldcat.",
            ]
        )

        self.assert_issue(validate_abap(source), "ALV_LVC_FIELDCAT_WITH_REUSE_ALV", 4)

    def test_local_structure_name_with_reuse_alv(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'REUSE_ALV_GRID_DISPLAY'",
                "  EXPORTING",
                "    i_structure_name = 'TY_OUTPUT'.",
            ]
        )

        self.assert_issue(validate_abap(source), "ALV_LOCAL_STRUCTURE_NAME", 3)

    def test_alv_request_with_write_and_no_alv_call_is_invalid(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "FORM display_output.",
                "  WRITE: / 'Output'.",
                "ENDFORM.",
            ]
        )

        self.assert_issue(validate_abap(source, alv_requested=True), "ALV_REQUESTED_WITH_CLASSICAL_LIST_OUTPUT", 3)

    def test_alv_request_with_real_alv_call_allows_write(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "WRITE sy-repid.",
                "CALL FUNCTION 'REUSE_ALV_GRID_DISPLAY'",
                "  TABLES",
                "    t_outtab = t_output.",
            ]
        )

        self.assertFalse([issue for issue in validate_abap(source, alv_requested=True) if issue["rule_id"] == "ALV_REQUESTED_WITH_CLASSICAL_LIST_OUTPUT"])

    def test_blank_select_option_guard(self):
        source = "\n".join(
            [
                "IF s_matnr[] IS NOT INITIAL.",
                "  SELECT * FROM mara INTO TABLE result_table WHERE matnr IN s_matnr.",
                "ENDIF.",
            ]
        )

        self.assert_issue(validate_abap(source), "BLANK_SELECT_OPTION_GUARD", 1)

    def test_binary_search_without_sort(self):
        issues = validate_abap("READ TABLE item_table INTO item_row WITH KEY matnr = gv_matnr BINARY SEARCH.")

        self.assert_issue(issues, "BINARY_SEARCH_WITHOUT_SORT", 1)

    def test_binary_search_with_sort_passes(self):
        source = "\n".join(
            [
                "SORT item_table BY matnr.",
                "READ TABLE item_table INTO item_row WITH KEY matnr = gv_matnr BINARY SEARCH.",
            ]
        )

        self.assertFalse([issue for issue in validate_abap(source) if issue["rule_id"] == "BINARY_SEARCH_WITHOUT_SORT"])

    def test_forbidden_prefixes_reported_once_per_declaration(self):
        source = "\n".join(
            [
                "DATA gt_items TYPE STANDARD TABLE OF mara.",
                "APPEND mara TO gt_items.",
                "DATA gt_items TYPE STANDARD TABLE OF mara.",
            ]
        )
        issues = [issue for issue in validate_abap(source) if issue["rule_id"] == "FORBIDDEN_NAMING_PREFIX"]

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["line_number"], 1)

    def test_classical_source_with_new_rule_scope_passes(self):
        source = "\n".join(
            [
                "REPORT ztest.",
                "DATA gv_name TYPE scrfname VALUE 'CONTAINER'.",
                "DATA fieldcat TYPE slis_t_fieldcat_alv.",
                "PARAMETERS p_flag AS CHECKBOX.",
                "PARAMETERS p_alv RADIOBUTTON GROUP r1 DEFAULT 'X'.",
                "SORT item_table BY matnr.",
                "READ TABLE item_table INTO item_row WITH KEY matnr = gv_matnr BINARY SEARCH.",
            ]
        )

        self.assertEqual(validate_abap(source), [])

    def test_table_expression_detection(self):
        issues = validate_abap("ls_item = lt_items[ id = lv_id ].")

        self.assert_issue(issues, "TABLE_EXPRESSION", 1)

    def test_executable_placeholder_detection(self):
        source = 'WHERE docnum = t_docnums[ 1 ] " placeholder to satisfy standard syntax'

        issues = validate_abap(source)

        self.assert_issue(issues, "EXECUTABLE_PLACEHOLDER", 1)

    def test_normal_explanatory_comments_are_not_placeholder_issues(self):
        source = "\n".join(
            [
                "* TODO: document the business fallback.",
                'DATA gv_placeholder_note TYPE string. " placeholder text shown in help',
            ]
        )

        self.assertFalse([issue for issue in validate_abap(source) if issue["rule_id"] == "EXECUTABLE_PLACEHOLDER"])

    def test_fake_database_condition_placeholder_detection(self):
        source = "SELECT * FROM edidc INTO TABLE t_edidc WHERE docnum = '0000000000000001'. \" fake database condition"

        self.assert_issue(validate_abap(source), "EXECUTABLE_PLACEHOLDER", 1)

    def test_one_issue_per_rule_per_source_line(self):
        issues = [
            issue
            for issue in validate_abap("st_a = t_items[ 1 ]. st_b = t_items[ 2 ].")
            if issue["rule_id"] == "TABLE_EXPRESSION"
        ]

        self.assertEqual(len(issues), 1)

    def test_data_referencing_later_local_type_is_reported(self):
        source = "\n".join(
            [
                "DATA st_edidc TYPE ty_edidc.",
                "TYPES: BEGIN OF ty_edidc,",
                "  docnum TYPE edidc-docnum,",
                "END OF ty_edidc.",
            ]
        )

        self.assert_issue(validate_abap(source), "TYPE_USED_BEFORE_DECLARATION", 1)

    def test_data_referencing_earlier_local_type_passes(self):
        source = "\n".join(
            [
                "TYPES: BEGIN OF ty_edidc,",
                "  docnum TYPE edidc-docnum,",
                "END OF ty_edidc.",
                "DATA st_edidc TYPE ty_edidc.",
            ]
        )

        self.assertFalse([issue for issue in validate_abap(source) if issue["rule_id"] == "TYPE_USED_BEFORE_DECLARATION"])

    def test_unknown_local_type_is_reported(self):
        issues = validate_abap("DATA st_edidc TYPE ty_missing.")

        self.assert_issue(issues, "UNKNOWN_LOCAL_TYPE", 1)

    def test_inline_call_parameter_is_reported(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'ANY_CALL'",
                "  IMPORTING",
                "    result = DATA(w_result).",
            ]
        )

        self.assert_issue(validate_abap(source), "INLINE_DATA_CALL_PARAMETER", 3)

    def test_callable_unknown_parameter_is_reported(self):
        source = "\n".join(["CALL FUNCTION 'Z_TEST_FUNCTION'", "  EXPORTING", "    EXTRA = value."])
        issues = validate_abap(source, callable_signatures=callable_signature())

        issue = self.assert_issue(issues, "CALLABLE_UNKNOWN_PARAMETER", 3)
        self.assertEqual(issue["callable_name"], "Z_TEST_FUNCTION")
        self.assertEqual(issue["parameter_name"], "EXTRA")
        self.assertEqual(issue["actual_section"], "EXPORTING")

    def test_callable_wrong_section_is_reported(self):
        source = "\n".join(["CALL FUNCTION 'Z_TEST_FUNCTION'", "  IMPORTING", "    ID = value."])
        issues = validate_abap(source, callable_signatures=callable_signature())

        issue = self.assert_issue(issues, "CALLABLE_PARAMETER_WRONG_SECTION", 3)
        self.assertEqual(issue["expected_section"], "EXPORTING")
        self.assertEqual(issue["actual_section"], "IMPORTING")

    def test_callable_required_parameter_missing_is_reported(self):
        source = "\n".join(["CALL FUNCTION 'Z_TEST_FUNCTION'", "  IMPORTING", "    MESSAGE = text."])
        issues = validate_abap(source, callable_signatures=callable_signature())

        issue = self.assert_issue(issues, "CALLABLE_REQUIRED_PARAMETER_MISSING", 1)
        self.assertEqual(issue["parameter_name"], "ID")
        self.assertEqual(issue["expected_section"], "EXPORTING")

    def test_callable_tables_parameter_is_reported_when_not_supported(self):
        source = "\n".join(["CALL FUNCTION 'Z_TEST_FUNCTION'", "  TABLES", "    MESSAGE = text."])
        issues = validate_abap(source, callable_signatures=callable_signature())

        issue = self.assert_issue(issues, "CALLABLE_UNSUPPORTED_SECTION", 3)
        self.assertEqual(issue["expected_section"], "IMPORTING")
        self.assertEqual(issue["actual_section"], "TABLES")

    def test_callable_returning_parameter_misuse_is_reported(self):
        source = "\n".join(["CALL FUNCTION 'Z_TEST_FUNCTION'", "  TABLES", "    RESULT = text."])
        issues = validate_abap(source, callable_signatures=callable_signature())

        issue = self.assert_issue(issues, "CALLABLE_RETURNING_MISUSED", 3)
        self.assertEqual(issue["expected_section"], "RETURNING")
        self.assertEqual(issue["actual_section"], "TABLES")

    def test_valid_callable_call_passes(self):
        source = "\n".join(
            [
                "CALL FUNCTION 'Z_TEST_FUNCTION'",
                "  EXPORTING",
                "    ID = source-id",
                "  IMPORTING",
                "    MESSAGE = text.",
            ]
        )

        issues = [issue for issue in validate_abap(source, callable_signatures=callable_signature()) if issue["rule_id"].startswith("CALLABLE_")]

        self.assertEqual(issues, [])

    def test_correct_line_numbers(self):
        issues = validate_abap("REPORT ztest.\n\nDATA(lv_text) = 'x'.")

        self.assert_issue(issues, "INLINE_DATA", 3)

    def test_multiple_issues_returned(self):
        issues = validate_abap("DATA(lv_text) = |x|.\nls_item = lt_items[ 1 ].")

        self.assertGreaterEqual(len(issues), 3)

    def test_ddic_provenance_ignores_local_qualified_identifiers(self):
        source = "\n".join(["st_edidc-docnum = edidc-docnum.", "IF sy-subrc = 0.", "ENDIF."])
        provenance = {"EDIDC-DOCNUM": {"identifier": "EDIDC-DOCNUM", "source": "sap-metadata"}}

        issues = [
            issue
            for issue in validate_abap(source, identifier_provenance=provenance)
            if issue["rule_id"] == "ABAP_UNVERIFIED_DDIC_IDENTIFIER"
        ]

        self.assertEqual(issues, [])

    def test_result_page_displays_issues_and_download_still_works(self):
        temp_path = Path(__file__).resolve().parents[1] / f".test_validator_{uuid4().hex}"
        temp_path.mkdir()
        try:
            uploads_folder = temp_path / "uploads"
            jobs_folder = temp_path / "jobs"
            prompt_path = temp_path / "create_abap.txt"
            prompt_path.write_text("Generate ABAP.", encoding="utf-8")
            generated_abap = "REPORT ztest.\nDATA(lv_text) = |x|."

            with patch(
                "services.create_abap.generate_abap",
                return_value={
                    "text": generated_abap,
                    "model": "test-model",
                    "usage": None,
                },
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

                result = client.get(f"/result/{job_id}")
                self.assertEqual(result.status_code, 200)
                self.assertIn(b'<details class="validation-panel">', result.data)
                self.assertNotIn(b'<details class="validation-panel" open', result.data)
                self.assertIn(b"Validation", result.data)
                self.assertIn(b"deterministic issues found", result.data)
                self.assertIn(b"INLINE_DATA", result.data)
                self.assertIn(b"STRING_TEMPLATE", result.data)
                self.assertIn(b"Line 2", result.data)

                download = client.get(f"/download/{job_id}")
                self.assertEqual(download.status_code, 200)
                downloaded_abap = download.get_data(as_text=True).replace("\r\n", "\n")
                download.close()
                self.assertEqual(downloaded_abap, generated_abap)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def assert_issue(self, issues, rule_id, line_number):
        matching = [
            issue
            for issue in issues
            if issue["rule_id"] == rule_id and issue["line_number"] == line_number
        ]
        self.assertTrue(matching, f"Expected {rule_id} on line {line_number}, got {issues}")
        issue = matching[0]
        self.assertEqual(issue["severity"], "error")
        self.assertIn("message", issue)
        self.assertIn("source_line", issue)
        self.assertIn("suggested_fix", issue)
        return issue


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
