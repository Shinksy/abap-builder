import unittest
from datetime import date

from services.modification_history import apply_modification_history, detect_modification_convention


def sample_source(prefix="ATOS", marker_space=""):
    return "\n".join(
        [
            "REPORT zsample.",
            "************************************************************************",
            "*  Report      : ZSAMPLE                Author : Example User         *",
            "*                                                                      *",
            "*  Revision History                                                    *",
            "************************************************************************",
            f"*  Date        : 01.01.2026               Change Ref  : {prefix}001        *",
            "*                                                                      *",
            "*  Developer   : First Dev                Request No  : REQ1          *",
            "*                                                                      *",
            "*  Description : Initial change                                      *",
            "*                                                                      *",
            "************************************************************************",
            f"*  Date        : 02.01.2026               Change Ref  : {prefix}002        *",
            "*                                                                      *",
            "*  Developer   : Second Dev               Request No  : REQ2          *",
            "*                                                                      *",
            "*  Description : Follow-up change                                    *",
            "*                                                                      *",
            "************************************************************************",
            "DATA gv_old TYPE string.",
            f"WRITE gv_old.  \"{marker_space}{prefix}002",
            "FORM output.",
            "  WRITE gv_old.",
            "ENDFORM.",
        ]
    )


class ModificationHistoryTest(unittest.TestCase):
    def test_increments_existing_numeric_modification_id_and_preserves_prefix(self):
        convention = detect_modification_convention(sample_source(prefix="XYZ"))

        self.assertEqual(convention.identifier, "XYZ002")
        self.assertEqual(convention.next_identifier, "XYZ003")

    def test_preserves_header_layout_and_inserts_current_date_name_log_and_description(self):
        original = sample_source()
        final = original.replace("DATA gv_old TYPE string.", "DATA gv_old TYPE string.\nDATA gv_new TYPE string.")

        result = apply_modification_history(
            original,
            final,
            developer_name="Ada Developer",
            log_number="REQ900",
            description="Add new output support. Do not change anything else.",
            current_date=date(2026, 9, 11),
        )

        lines = result["source"].splitlines()
        self.assertIn("*  Date        : 11.09.2026               Change Ref  : ATOS003        *", lines)
        self.assertIn("*  Developer   : Ada Developer            Request No  : REQ900        *", lines)
        self.assertIn("*  Description : Add new output support.                             *", lines)
        self.assertEqual(lines[5], "************************************************************************")
        self.assertEqual(result["modification_id"], "ATOS003")

    def test_marks_changed_lines_and_not_unchanged_lines(self):
        original = sample_source()
        final = original.replace("DATA gv_old TYPE string.", "DATA gv_old TYPE string.\nDATA gv_new TYPE string.")

        result = apply_modification_history(original, final, current_date=date(2026, 9, 11))

        self.assertIn('DATA gv_new TYPE string.  "ATOS003', result["source"])
        self.assertIn('  WRITE gv_old.\nENDFORM.', result["source"])
        self.assertNotIn('  WRITE gv_old.  "ATOS003', result["source"])

    def test_does_not_mark_repeated_existing_boilerplate_near_inserted_lines(self):
        original = "\n".join(
            [
                sample_source(),
                "FORM define_field_catalog.",
                "  st_alv_fieldcat-fieldname = 'NACHN'.",
                "  st_alv_fieldcat-seltext_l = 'Surname'.",
                "  APPEND st_alv_fieldcat TO t_alv_fieldcat.",
                "  CLEAR st_alv_fieldcat.",
                "",
                "  st_alv_fieldcat-fieldname = 'DAT01'.",
                "  st_alv_fieldcat-seltext_l = 'Start Date'.",
                "  APPEND st_alv_fieldcat TO t_alv_fieldcat.",
                "  CLEAR st_alv_fieldcat.",
                "ENDFORM.",
            ]
        )
        final = original.replace(
            "  st_alv_fieldcat-seltext_l = 'Surname'.\n"
            "  APPEND st_alv_fieldcat TO t_alv_fieldcat.\n"
            "  CLEAR st_alv_fieldcat.\n",
            "  st_alv_fieldcat-seltext_l = 'Surname'.\n"
            "  APPEND st_alv_fieldcat TO t_alv_fieldcat.\n"
            "  CLEAR st_alv_fieldcat.\n"
            "\n"
            "  st_alv_fieldcat-fieldname = 'GBDAT'.\n"
            "  st_alv_fieldcat-seltext_l = 'Date of Birth'.\n"
            "  APPEND st_alv_fieldcat TO t_alv_fieldcat.\n"
            "  CLEAR st_alv_fieldcat.\n",
        )

        result = apply_modification_history(original, final, changed_source=final, current_date=date(2026, 9, 11))

        self.assertIn('  st_alv_fieldcat-fieldname = \'GBDAT\'.  "ATOS003', result["source"])
        self.assertIn('  st_alv_fieldcat-seltext_l = \'Date of Birth\'.  "ATOS003', result["source"])
        self.assertNotIn('  APPEND st_alv_fieldcat TO t_alv_fieldcat.  "ATOS003', result["source"])
        self.assertNotIn('  CLEAR st_alv_fieldcat.  "ATOS003', result["source"])

    def test_marker_source_excludes_later_fixer_rewrites(self):
        original = sample_source()
        approved = original.replace("DATA gv_old TYPE string.", "DATA gv_old TYPE string.\nDATA gv_new TYPE string.")
        final = approved.replace("  WRITE gv_old.", "  WRITE: / gv_old.")

        result = apply_modification_history(original, final, changed_source=approved, current_date=date(2026, 9, 11))

        self.assertIn('DATA gv_new TYPE string.  "ATOS003', result["source"])
        self.assertIn("  WRITE: / gv_old.", result["source"])
        self.assertNotIn('  WRITE: / gv_old.  "ATOS003', result["source"])

    def test_preserves_detected_marker_spacing_and_avoids_duplicate_markers(self):
        original = sample_source(prefix="ZX", marker_space=" ")
        final = original.replace(
            "DATA gv_old TYPE string.",
            'DATA gv_old TYPE string.\nDATA gv_new TYPE string.  " ZX003',
        )

        result = apply_modification_history(original, final, current_date=date(2026, 9, 11))

        self.assertIn('DATA gv_new TYPE string.  " ZX003', result["source"])
        self.assertEqual(result["source"].count('DATA gv_new TYPE string.  " ZX003'), 1)

    def test_missing_modification_history_returns_warning_without_source_change(self):
        original = "REPORT zplain.\nDATA gv_old TYPE string."
        final = "REPORT zplain.\nDATA gv_old TYPE string.\nDATA gv_new TYPE string."

        result = apply_modification_history(original, final, current_date=date(2026, 9, 11))

        self.assertEqual(result["source"], final)
        self.assertEqual(result["issues"][0]["rule_id"], "ENHANCEMENT_MODIFICATION_HISTORY_UNDETECTED")
        self.assertEqual(result["issues"][0]["severity"], "warning")

    def test_ambiguous_modification_id_format_returns_warning(self):
        original = "\n".join(
            [
                "REPORT zambiguous.",
                "*  Change History",
                "************************************************************************",
                "*  Date : 01.01.2026  Ref : AA001  Batch : BB001                      *",
                "************************************************************************",
                "*  Date : 02.01.2026  Ref : AA002  Batch : BB002                      *",
                "************************************************************************",
                "DATA gv_old TYPE string.",
            ]
        )
        final = original + "\nDATA gv_new TYPE string."

        result = apply_modification_history(original, final, current_date=date(2026, 9, 11))

        self.assertEqual(result["source"], final)
        self.assertEqual(result["issues"][0]["rule_id"], "ENHANCEMENT_MODIFICATION_HISTORY_UNDETECTED")


if __name__ == "__main__":
    unittest.main()
