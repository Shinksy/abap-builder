import unittest
from pathlib import Path

from services.modifier_guardrails import accept_modified_source, build_identifier_provenance
from services.validator import validate_abap


class ModifierGuardrailsTest(unittest.TestCase):
    def test_selection_screen_is_restored_for_unrelated_modification(self):
        original = sample_program()
        proposed = original.replace("w_total = w_total + 1.", "w_total = w_total + 2.").replace(
            "            p_alv   AS RADIOBUTTON GROUP rg DEFAULT 'X',",
            "            p_alv   AS RADIOBUTTON GROUP rg out 'X' DEFAULT 'X',",
        )

        result = accept_modified_source(original, proposed, approved_ranges=[(14, 14)])

        self.assertTrue(result["accepted"])
        self.assertIn(selection_screen(), result["final_source"])
        self.assertIn("w_total = w_total + 2.", result["final_source"])
        self.assertNotIn("out 'X'", result["final_source"])

    def test_corrupted_selection_screen_only_is_rejected_safely(self):
        original = sample_program()
        proposed = original.replace(
            "            p_alv   AS RADIOBUTTON GROUP rg DEFAULT 'X',",
            "            p_alv   AS RADIOBUTTON GROUP rg out 'X' DEFAULT 'X',",
        )

        result = accept_modified_source(original, proposed, approved_ranges=[(14, 14)])

        self.assertFalse(result["accepted"])
        self.assertEqual(result["final_source"], original)
        self.assertFalse(result["full_regeneration_used"])

    def test_declaration_corruption_is_restored_from_original(self):
        original = sample_program()
        proposed = original.replace(
            "DATA t_items TYPE STANDARD TABLE OF ty_item.",
            "DATA t_items TYPE STANDARD TABLE OF ty_item,",
        ).replace("w_total = w_total + 1.", "w_total = w_total + 2.")

        result = accept_modified_source(original, proposed, approved_ranges=[(14, 14)])

        self.assertTrue(result["accepted"])
        self.assertIn("DATA t_items TYPE STANDARD TABLE OF ty_item.", result["final_source"])
        self.assertNotIn("DATA t_items TYPE STANDARD TABLE OF ty_item,", result["final_source"])
        self.assertIn("w_total = w_total + 2.", result["final_source"])

    def test_control_flow_substitution_is_restored(self):
        original = sample_program()
        proposed = original.replace("  LEAVE LIST-PROCESSING.", "  LEAVE REPORT.").replace(
            "w_total = w_total + 1.",
            "w_total = w_total + 2.",
        )

        result = accept_modified_source(original, proposed, approved_ranges=[(14, 14)])

        self.assertTrue(result["accepted"])
        self.assertIn("  LEAVE LIST-PROCESSING.", result["final_source"])
        self.assertNotIn("  LEAVE REPORT.", result["final_source"])
        self.assertIn("ABAP_UNAPPROVED_CONTROL_FLOW_CHANGE", [issue["rule_id"] for issue in result["issues"]])

    def test_list_processing_leave_report_is_repaired_without_surrounding_changes(self):
        original = "\n".join(
            [
                "REPORT ztest.",
                "AT USER-COMMAND.",
                "  WRITE: / 'Back'.",
                "  LEAVE REPORT.",
                "  WRITE: / 'After'.",
            ]
        )

        result = accept_modified_source(original, original, approved_ranges=[])

        self.assertTrue(result["accepted"])
        self.assertEqual(
            result["final_source"],
            "\n".join(
                [
                    "REPORT ztest.",
                    "AT USER-COMMAND.",
                    "  WRITE: / 'Back'.",
                    "  LEAVE LIST-PROCESSING.",
                    "  WRITE: / 'After'.",
                ]
            ),
        )
        self.assertFalse(result["full_regeneration_used"])

    def test_approved_form_edit_changes_only_that_form(self):
        original = sample_program()
        proposed = original.replace("w_total = w_total + 1.", "w_total = w_total + 2.").replace(
            "FORM unrelated.",
            "FORM unrelated.\n  WRITE: / 'changed'.",
        )

        result = accept_modified_source(original, proposed, approved_ranges=[(14, 14)])

        self.assertTrue(result["accepted"])
        self.assertIn(selection_screen(), result["final_source"])
        self.assertIn("w_total = w_total + 2.", result["final_source"])
        self.assertIn("FORM unrelated.\nENDFORM.", result["final_source"])
        self.assertNotIn("WRITE: / 'changed'.", result["final_source"])

    def test_existing_ddic_reference_remains_unchanged_for_unrelated_modification(self):
        original = ddic_program()
        proposed = original.replace("w_total = w_total + 1.", "w_total = w_total + 2.")

        result = accept_modified_source(original, proposed, approved_ranges=[(5, 5)])

        self.assertTrue(result["accepted"])
        self.assertIn("DATA w_created_on TYPE edidc-credat.", result["final_source"])
        self.assertIn("w_total = w_total + 2.", result["final_source"])

    def test_near_match_ddic_mutation_is_restored_and_reported(self):
        original = ddic_program()
        proposed = original.replace("edidc-credat", "edidc-credatt").replace(
            "w_total = w_total + 1.",
            "w_total = w_total + 2.",
        )

        result = accept_modified_source(original, proposed, approved_ranges=[(5, 5)])

        self.assertTrue(result["accepted"])
        self.assertIn("DATA w_created_on TYPE edidc-credat.", result["final_source"])
        self.assertNotIn("edidc-credatt", result["final_source"])
        issue = [item for item in result["issues"] if item["rule_id"] == "ABAP_UNVERIFIED_DDIC_IDENTIFIER"][0]
        self.assertEqual(issue["proposed_identifier"], "EDIDC-CREDATT")
        self.assertEqual(issue["closest_identifier"], "EDIDC-CREDAT")
        self.assertEqual(issue["closest_source"], "existing-source")
        self.assertTrue(issue["restored"])
        self.assertFalse(result["full_regeneration_used"])

    def test_validator_reports_unverified_ddic_identifier_with_closest_source(self):
        provenance = build_identifier_provenance(original_source="DATA w_created_on TYPE edidc-credat.")

        issues = validate_abap("DATA w_created_on TYPE edidc-credatt.", identifier_provenance=provenance)

        issue = [item for item in issues if item["rule_id"] == "ABAP_UNVERIFIED_DDIC_IDENTIFIER"][0]
        self.assertEqual(issue["proposed_identifier"], "EDIDC-CREDATT")
        self.assertEqual(issue["closest_identifier"], "EDIDC-CREDAT")
        self.assertEqual(issue["closest_source"], "existing-source")

    def test_ddic_identifier_from_specification_is_allowed(self):
        original = "REPORT ztest."
        proposed = "\n".join(["REPORT ztest.", "DATA w_created_on TYPE edidc-credat."])

        result = accept_modified_source(
            original,
            proposed,
            approved_ranges=[(1, 1)],
            functional_specification="Use field EDIDC-CREDAT for the creation date.",
        )

        self.assertTrue(result["accepted"])
        self.assertFalse([item for item in result["issues"] if item["rule_id"] == "ABAP_UNVERIFIED_DDIC_IDENTIFIER"])

    def test_ddic_identifier_from_sap_metadata_is_allowed(self):
        original = "REPORT ztest."
        proposed = "\n".join(["REPORT ztest.", "DATA w_message_type TYPE edidc-mestyp."])

        result = accept_modified_source(
            original,
            proposed,
            approved_ranges=[(1, 1)],
            sap_metadata={"tables": {"EDIDC": {"fields": ["MESTYP"]}}},
        )

        self.assertTrue(result["accepted"])
        self.assertFalse([item for item in result["issues"] if item["rule_id"] == "ABAP_UNVERIFIED_DDIC_IDENTIFIER"])

    def test_ddic_metadata_provider_allows_verified_existing_program_field(self):
        original = "REPORT ztest."
        proposed = "\n".join(["REPORT ztest.", "DATA w_message_type TYPE edidc-mestyp."])
        provider = StaticDdicProvider({"tables": {"EDIDC": {"fields": {"MESTYP": {"name": "MESTYP"}}}}})

        result = accept_modified_source(
            original,
            proposed,
            approved_ranges=[(1, 1)],
            functional_specification="Use table EDIDC.",
            ddic_metadata_provider=provider,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(provider.requested, ["EDIDC"])
        self.assertFalse([item for item in result["issues"] if item["rule_id"] == "ABAP_UNVERIFIED_DDIC_IDENTIFIER"])

    def test_ddic_metadata_provider_rejects_guessed_existing_program_field(self):
        original = "REPORT ztest."
        proposed = "\n".join(["REPORT ztest.", "DATA w_message_type TYPE edidc-mestyq."])
        provider = StaticDdicProvider({"tables": {"EDIDC": {"fields": {"MESTYP": {"name": "MESTYP"}}}}})

        result = accept_modified_source(
            original,
            proposed,
            approved_ranges=[(1, 1)],
            functional_specification="Use table EDIDC.",
            ddic_metadata_provider=provider,
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["final_source"], original)
        issue = [item for item in result["issues"] if item["rule_id"] == "ABAP_UNVERIFIED_DDIC_IDENTIFIER"][0]
        self.assertEqual(issue["proposed_identifier"], "EDIDC-MESTYQ")
        self.assertEqual(issue["closest_identifier"], "EDIDC-MESTYP")
        self.assertEqual(issue["closest_source"], "sap-metadata")
        self.assertFalse(result["full_regeneration_used"])

    def test_new_unapproved_ddic_reference_is_rejected(self):
        original = "REPORT ztest."
        proposed = "\n".join(["REPORT ztest.", "DATA w_created_on TYPE edidc-credatt."])

        result = accept_modified_source(original, proposed, approved_ranges=[(1, 1)])

        self.assertFalse(result["accepted"])
        self.assertEqual(result["final_source"], original)
        self.assertIn("ABAP_UNVERIFIED_DDIC_IDENTIFIER", [item["rule_id"] for item in result["issues"]])

    def test_new_local_variable_is_allowed_with_approved_change(self):
        original = "REPORT ztest."
        proposed = "\n".join(["REPORT ztest.", "DATA w_new_counter TYPE i."])

        result = accept_modified_source(original, proposed, approved_ranges=[(1, 1)])

        self.assertTrue(result["accepted"])
        self.assertIn("DATA w_new_counter TYPE i.", result["final_source"])

    def test_modify_prompt_contains_ddic_provenance_constraints(self):
        prompt = Path("prompts/modify_existing_abap.txt").read_text(encoding="utf-8")

        self.assertIn("Do not invent SAP table fields", prompt)
        self.assertIn("A similar-looking field name is not an acceptable substitute", prompt)
        self.assertIn("return an unresolved metadata requirement rather than guessing", prompt)


def selection_screen():
    return "\n".join(
        [
            "PARAMETERS: p_zmdid TYPE zmdid,",
            "            p_idoc  AS CHECKBOX DEFAULT '',",
            "            p_alv   AS RADIOBUTTON GROUP rg DEFAULT 'X',",
            "            p_file  AS RADIOBUTTON GROUP rg.",
        ]
    )


def sample_program():
    return "\n".join(
        [
            "REPORT ztest.",
            "TYPES: BEGIN OF ty_item,",
            "  value TYPE i,",
            "END OF ty_item.",
            "DATA t_items TYPE STANDARD TABLE OF ty_item.",
            selection_screen(),
            "AT USER-COMMAND.",
            "  LEAVE LIST-PROCESSING.",
            "FORM change_target.",
            "  DATA w_total TYPE i.",
            "  w_total = w_total + 1.",
            "ENDFORM.",
            "FORM unrelated.",
            "ENDFORM.",
        ]
    )


def ddic_program():
    return "\n".join(
        [
            "REPORT ztest.",
            "DATA w_created_on TYPE edidc-credat.",
            "FORM change_target.",
            "  DATA w_total TYPE i.",
            "  w_total = w_total + 1.",
            "ENDFORM.",
        ]
    )


class StaticDdicProvider:
    def __init__(self, metadata):
        self.metadata = metadata
        self.requested = None

    def get_tables(self, table_names):
        self.requested = list(table_names)
        return self.metadata


if __name__ == "__main__":
    unittest.main()
