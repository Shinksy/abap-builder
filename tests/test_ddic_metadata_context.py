import unittest

from services.ddic_metadata_context import (
    append_ddic_catalogue,
    ddic_identifier_provenance,
    extract_ambiguous_standalone_type_like_names_from_source,
    extract_post_generation_ddic_names_from_source,
    extract_relevant_ddic_names,
    extract_relevant_ddic_names_from_source,
    extract_typed_ddic_dependencies,
    render_compact_ddic_catalogue,
    retrieve_missing_ddic_metadata,
)


class DdicMetadataContextTest(unittest.TestCase):
    def test_extracts_relevant_table_and_structure_names(self):
        text = "\n".join(
            [
                "SAP table EDIDC.",
                "Structure: EDI_DC40.",
                "SELECT * FROM edidc INTO TABLE t_edidc.",
                "DATA w_msg TYPE edidc-mestyp.",
                "DATA t_items TYPE STANDARD TABLE OF ty_item.",
            ]
        )

        self.assertEqual(extract_relevant_ddic_names(text), ["EDIDC", "EDI_DC40"])

    def test_standard_table_field_reference_extracts_object(self):
        self.assertEqual(extract_relevant_ddic_names("Use field MARA-MATNR."), ["MARA"])

    def test_customer_table_field_reference_extracts_object(self):
        self.assertEqual(extract_relevant_ddic_names("Use field ZSALES_HDR-VBELN."), ["ZSALES_HDR"])

    def test_namespaced_object_reference_extracts_object(self):
        self.assertEqual(extract_relevant_ddic_names("Use field /ACME/ORDER-ID."), ["/ACME/ORDER"])

    def test_sql_from_and_join_extract_objects(self):
        text = "SELECT * FROM VBAK INNER JOIN VBAP ON VBAP-VBELN = VBAK-VBELN."

        self.assertEqual(extract_relevant_ddic_names(text), ["VBAK", "VBAP"])

    def test_type_and_like_extract_qualified_objects(self):
        text = "\n".join(["DATA w_date TYPE BKPF-BUDAT.", "DATA w_doc LIKE BSEG-BELNR."])

        self.assertEqual(extract_relevant_ddic_names(text), ["BKPF", "BSEG"])

    def test_post_generation_ignores_standalone_type_like_references(self):
        text = "\n".join(
            [
                "DATA w_value TYPE zexternal_data.",
                "DATA w_other LIKE yexternal_data.",
                "DATA w_field TYPE zknown_table-field1.",
                "SELECT * FROM zknown_other INTO TABLE t_rows.",
            ]
        )

        self.assertEqual(extract_post_generation_ddic_names_from_source(text), ["ZKNOWN_TABLE", "ZKNOWN_OTHER"])
        self.assertEqual(
            extract_ambiguous_standalone_type_like_names_from_source(text),
            ["ZEXTERNAL_DATA", "YEXTERNAL_DATA"],
        )

    def test_read_update_insert_modify_delete_and_tables_extract_objects(self):
        text = "\n".join(
            [
                "READ TABLE MARA INTO w_mara.",
                "UPDATE VBAK SET ernam = sy-uname.",
                "INSERT zorder_log FROM w_log.",
                "MODIFY zorder_item FROM w_item.",
                "DELETE FROM zorder_old WHERE id = w_id.",
                "TABLES kna1.",
            ]
        )

        self.assertEqual(
            extract_relevant_ddic_names(text),
            ["MARA", "VBAK", "ZORDER_LOG", "ZORDER_ITEM", "ZORDER_OLD", "KNA1"],
        )

    def test_labelled_prose_references_extract_objects(self):
        text = "\n".join(["SAP table VBAK", "table VBAP", "structure BAPIRET2", "view /ACME/V_ORD"])

        self.assertEqual(extract_relevant_ddic_names(text), ["VBAK", "VBAP", "BAPIRET2", "/ACME/V_ORD"])

    def test_table_read_section_headings_extract_requested_tables(self):
        text = "\n".join(
            [
                "## Table Reads",
                "### PA0000",
                "Read Fields:",
                "* PERNR",
                "### PA0002",
                "Read PA0002 as a separate dependent table read.",
                "Read Fields:",
                "* PERNR",
                "* VORNA",
                "* NACHN",
                "## Processing Rules",
                "### NOT_A_TABLE",
            ]
        )

        dependencies = extract_typed_ddic_dependencies(text)
        dependency_keys = {(item["kind"], item["name"], item["source"]) for item in dependencies}

        self.assertIn(("ddic_table", "PA0000", "table_read_heading"), dependency_keys)
        self.assertIn(("ddic_table", "PA0002", "table_read_heading"), dependency_keys)
        self.assertEqual(extract_relevant_ddic_names(text), ["PA0000", "PA0002"])

    def test_prose_table_references_extract_requested_tables(self):
        text = "\n".join(
            [
                "Personnel number selection based on PA0000.",
                "Payroll area selection based on PA0001.",
                "Read the current PA0001 record for each employee.",
                "Read the current PA0002 record for each retained employee.",
                "Only PA0002 records valid on the current date are considered.",
            ]
        )

        dependencies = extract_typed_ddic_dependencies(text)
        dependency_keys = {(item["kind"], item["name"], item["source"]) for item in dependencies}

        self.assertIn(("ddic_table", "PA0000", "prose_table_reference"), dependency_keys)
        self.assertIn(("ddic_table", "PA0001", "prose_table_reference"), dependency_keys)
        self.assertIn(("ddic_table", "PA0002", "prose_table_reference"), dependency_keys)
        self.assertEqual(extract_relevant_ddic_names(text), ["PA0000", "PA0001", "PA0002"])

    def test_prose_table_references_are_generic_but_not_business_nouns(self):
        text = "\n".join(
            [
                "Read employee records for the report.",
                "Read ZHR_PAYROLL records for selected employees.",
                "Department selection based on /ACME/ORG_UNIT.",
            ]
        )

        self.assertEqual(extract_relevant_ddic_names(text), ["ZHR_PAYROLL", "/ACME/ORG_UNIT"])

    def test_markdown_bullets_preserve_ddic_evidence_in_specification_mode(self):
        text = "\n".join(["* s_matnr FOR MARA-MATNR", "* s_kunnr FOR KNA1-KUNNR"])

        self.assertEqual(extract_relevant_ddic_names(text), ["MARA", "KNA1"])

    def test_abap_comment_lines_are_stripped_in_source_mode(self):
        text = "\n".join(["* SELECT * FROM MARA INTO TABLE t_mara.", "SELECT * FROM KNA1 INTO TABLE t_kna1."])

        self.assertEqual(extract_relevant_ddic_names_from_source(text), ["KNA1"])

    def test_hyphenated_prose_is_not_qualified_ddic_evidence(self):
        text = "selection-options range-table before-processing end-of-page"

        self.assertEqual(extract_relevant_ddic_names(text), [])

    def test_abap_keywords_are_rejected(self):
        text = "\n".join(["SAP table SELECT", "READ TABLE TABLE", "FROM WHERE", "Use field DATA-TYPE."])

        self.assertEqual(extract_relevant_ddic_names(text), [])

    def test_deduplicates_in_stable_order(self):
        text = "\n".join(["SAP table MARA", "SELECT * FROM mara.", "JOIN VBAP ON VBAP-MATNR = MARA-MATNR", "field MARA-MTART"])

        self.assertEqual(extract_relevant_ddic_names(text), ["MARA", "VBAP"])

    def test_empty_or_prose_only_specification_extracts_nothing(self):
        self.assertEqual(extract_relevant_ddic_names("", "Generate a daily report for business users."), [])

    def test_uppercase_prose_words_are_not_ddic_objects(self):
        text = "SELECTION BEFORE DECLARATIONS CHECKING READS MUST REFERENCED"

        self.assertEqual(extract_relevant_ddic_names(text), [])

    def test_qualified_reference_extracts_table(self):
        self.assertEqual(extract_relevant_ddic_names("Use field EDIDC-CREDAT."), ["EDIDC"])

    def test_type_qualified_reference_extracts_table(self):
        self.assertEqual(extract_relevant_ddic_names("DATA w_created TYPE EDIDC-CREDAT."), ["EDIDC"])

    def test_select_from_extracts_table(self):
        self.assertEqual(extract_relevant_ddic_names("SELECT * FROM EDIDS INTO TABLE t_status."), ["EDIDS"])

    def test_labelled_sap_table_extracts_table(self):
        self.assertEqual(extract_relevant_ddic_names("SAP table EDID4 is required."), ["EDID4"])

    def test_named_table_label_extracts_custom_tables(self):
        text = "\n".join(["Table: ZMD_MPE0001", "Structure: ZMD_MPE0006", "SAP table ZMD_GEN0007"])

        self.assertEqual(extract_relevant_ddic_names(text), ["ZMD_MPE0001", "ZMD_MPE0006", "ZMD_GEN0007"])

    def test_typed_dependency_extraction_ignores_ui_vocabulary(self):
        text = "\n".join(
            [
                "START PROCESSING RULES",
                "Type: Select-Option",
                "Control Type: Radio Button",
                "CALL_FUNCTION",
                "Unknown PROSE WORDS SHOULD NOT BECOME TABLES",
                "Reference Field: PA0000-PERNR",
                "S_WERKS  PA0001-WERKS  Personnel Area",
                "Reference Type: AD_SMTPADR",
                "Data Type: FLAG",
            ]
        )

        dependencies = extract_typed_ddic_dependencies(text)
        dependency_keys = {(item["kind"], item["name"]) for item in dependencies}

        self.assertIn(("ddic_field", "PA0000-PERNR"), dependency_keys)
        self.assertIn(("ddic_field", "PA0001-WERKS"), dependency_keys)
        self.assertIn(("ddic_type", "AD_SMTPADR"), dependency_keys)
        self.assertIn(("ddic_type", "FLAG"), dependency_keys)
        self.assertEqual(extract_relevant_ddic_names(text), ["PA0000", "PA0001"])
        self.assertNotIn(("ddic_table", "SELECT"), dependency_keys)
        self.assertNotIn(("ddic_table", "RADIO"), dependency_keys)
        self.assertNotIn(("ddic_field", "SELECT-OPTION"), dependency_keys)
        self.assertNotIn(("ddic_table", "CALL"), dependency_keys)
        self.assertNotIn(("ddic_table", "START"), dependency_keys)
        self.assertNotIn(("ddic_table", "PROCESSING"), dependency_keys)

    def test_missing_metadata_retrieval_fetches_only_missing_objects(self):
        provider = RecordingProvider({"EDIDC": ddic_table("EDIDC", ["DOCNUM"]), "EDID4": ddic_table("EDID4", ["SDATA"])})

        metadata = retrieve_missing_ddic_metadata(
            {"tables": {"EDIDC": ddic_table("EDIDC", ["DOCNUM"])}},
            ["EDIDC", "EDID4"],
            provider,
        )

        self.assertEqual(provider.requests, [["EDID4"]])
        self.assertIn("EDIDC", metadata["tables"])
        self.assertIn("EDID4", metadata["tables"])

    def test_no_missing_metadata_retrieval_uses_existing_catalogue(self):
        provider = RecordingProvider({"EDIDC": ddic_table("EDIDC", ["DOCNUM"])})
        existing = {"tables": {"EDIDC": ddic_table("EDIDC", ["DOCNUM"])}}

        metadata = retrieve_missing_ddic_metadata(existing, ["EDIDC"], provider)

        self.assertEqual(provider.requests, [])
        self.assertEqual(metadata, existing)

    def test_compact_catalogue_contains_verified_fields_only(self):
        catalogue = render_compact_ddic_catalogue(ddic_metadata())

        self.assertIn("SAP DDIC metadata catalogue:", catalogue)
        self.assertIn("Use these table-field names exactly", catalogue)
        self.assertIn("EDIDC:", catalogue)
        self.assertIn("DOCNUM [NUMC(16); key; IDoc number]", catalogue)
        self.assertIn("MESTYP [CHAR(30); Message Type]", catalogue)

    def test_compact_catalogue_uses_provider_field_order_before_truncating(self):
        catalogue = render_compact_ddic_catalogue(
            {
                "tables": {
                    "ZREAD": {
                        "fields": {
                            "ALPHA": {"name": "ALPHA"},
                            "BETA": {"name": "BETA"},
                            "GAMMA": {"name": "GAMMA"},
                        },
                        "field_order": ["GAMMA", "ALPHA", "BETA"],
                    }
                }
            },
            max_fields_per_table=2,
        )

        self.assertIn("- ZREAD: GAMMA, ALPHA", catalogue)
        self.assertNotIn("BETA", catalogue)

    def test_append_catalogue_is_noop_without_metadata(self):
        self.assertEqual(append_ddic_catalogue("Generate ABAP.", {"tables": {}}), "Generate ABAP.")

    def test_identifier_provenance_uses_table_field_pairs(self):
        provenance = ddic_identifier_provenance(ddic_metadata())

        self.assertEqual(provenance["EDIDC-DOCNUM"]["source"], "sap-metadata")
        self.assertEqual(provenance["EDIDC-MESTYP"]["identifier"], "EDIDC-MESTYP")


def ddic_metadata():
    return {
        "tables": {
            "EDIDC": {
                "fields": {
                    "DOCNUM": {
                        "name": "DOCNUM",
                        "datatype": "NUMC",
                        "length": 16,
                        "decimals": 0,
                        "description": "IDoc number",
                        "key": True,
                    },
                    "MESTYP": {
                        "name": "MESTYP",
                        "datatype": "CHAR",
                        "length": 30,
                        "decimals": 0,
                        "description": "Message Type",
                        "key": False,
                    },
                }
            }
        }
    }


def ddic_table(table_name, fields):
    return {
        "name": table_name,
        "field_count": len(fields),
        "fields": {field: {"name": field} for field in fields},
        "field_order": fields,
    }


class RecordingProvider:
    def __init__(self, tables):
        self.tables = tables
        self.requests = []

    def get_tables(self, table_names):
        names = list(table_names)
        self.requests.append(names)
        return {"tables": {name: self.tables.get(name, ddic_table(name, [])) for name in names}}


if __name__ == "__main__":
    unittest.main()
