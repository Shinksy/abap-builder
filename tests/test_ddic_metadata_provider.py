import unittest

import requests
from requests.auth import HTTPBasicAuth

from services.ddic_metadata_provider import (
    DdicMetadataError,
    DdicMetadataTimeoutError,
    NoOpDdicMetadataProvider,
    SapDdicMetadataProvider,
    get_configured_ddic_metadata_provider,
    normalize_sap_ddic_response,
)


class DdicMetadataProviderTest(unittest.TestCase):
    def test_noop_provider_returns_no_metadata(self):
        provider = NoOpDdicMetadataProvider()

        metadata = provider.get_tables(["EDIDC"])

        self.assertEqual(metadata["tables"], {})
        self.assertEqual(metadata["_diagnostics"], [{"object": "EDIDC", "event": "provider-unavailable"}])

    def test_normalizes_old_sap_ddic_response_shape(self):
        metadata = normalize_sap_ddic_response(sample_response(), "edidc")

        self.assertEqual(metadata["name"], "EDIDC")
        self.assertEqual(metadata["field_count"], 2)
        self.assertEqual(metadata["field_order"], ["DOCNUM", "MESTYP"])
        self.assertEqual(metadata["fields"]["DOCNUM"]["rollname"], "EDI_DOCNUM")
        self.assertEqual(metadata["fields"]["DOCNUM"]["datatype"], "NUMC")
        self.assertEqual(metadata["fields"]["DOCNUM"]["length"], 16)
        self.assertEqual(metadata["fields"]["DOCNUM"]["decimals"], 0)
        self.assertEqual(metadata["fields"]["DOCNUM"]["description"], "IDoc number")
        self.assertTrue(metadata["fields"]["DOCNUM"]["key"])
        self.assertFalse(metadata["fields"]["MESTYP"]["key"])

    def test_sap_provider_reuses_old_request_contract(self):
        session = RecordingSession(sample_response())
        provider = SapDdicMetadataProvider(
            url="https://sap.example.test/soap",
            user="user",
            password="pass",
            timeout=7,
            verify=False,
            sap_client="100",
            session_factory=lambda: session,
        )

        metadata = provider.get_tables(["edidc"])

        self.assertIn("EDIDC", metadata["tables"])
        self.assertIn({"object": "EDIDC", "event": "cache-miss"}, metadata["_diagnostics"])
        self.assertIn({"object": "EDIDC", "event": "sap-fetch-attempted"}, metadata["_diagnostics"])
        self.assertEqual(session.calls[0]["url"], "https://sap.example.test/soap")
        self.assertIn("<urn:ZDDIF_FIELDINFO_GET>", session.calls[0]["data"])
        self.assertIn("<TABNAME>EDIDC</TABNAME>", session.calls[0]["data"])
        self.assertEqual(
            session.calls[0]["headers"]["SOAPAction"],
            "urn:sap-com:document:sap:rfc:functions:DDIF_FIELDINFO_GET:DDIF_FIELDINFO_GETRequest",
        )
        self.assertEqual(session.calls[0]["headers"]["sap-client"], "100")
        self.assertEqual(session.calls[0]["timeout"], 7)
        self.assertFalse(session.calls[0]["verify"])
        self.assertIsInstance(session.calls[0]["auth"], HTTPBasicAuth)
        self.assertFalse(session.trust_env)

    def test_provider_dedupes_names_and_uses_bounded_cache(self):
        session = RecordingSession(sample_response())
        provider = SapDdicMetadataProvider(
            url="https://sap.example.test/soap",
            cache_max_tables=1,
            session_factory=lambda: session,
        )

        provider.get_tables(["edidc", "EDIDC"])
        cached = provider.get_tables(["edidc"])
        provider.get_tables(["mara"])
        provider.get_tables(["edidc"])

        posted_tabnames = [extract_tabname(call["data"]) for call in session.calls]
        self.assertEqual(posted_tabnames, ["EDIDC", "MARA", "EDIDC"])
        self.assertEqual(cached["_diagnostics"], [{"object": "EDIDC", "event": "cache-hit"}])

    def test_provider_reports_progress_for_fetch_and_cache(self):
        messages = []
        session = RecordingSession(sample_response())
        provider = SapDdicMetadataProvider(
            url="https://sap.example.test/soap",
            session_factory=lambda: session,
        )

        provider.get_tables(["edidc"], progress_callback=messages.append)
        provider.get_tables(["EDIDC"], progress_callback=messages.append)

        self.assertEqual(
            messages,
            [
                "Calling SAP to get DDIC metadata",
                "Called SAP to get DDIC metadata",
                "Loading DDIC metadata from cache",
                "Loaded DDIC metadata from cache",
            ],
        )

    def test_provider_reports_progress_for_not_found_and_failure(self):
        not_found_messages = []
        provider = SapDdicMetadataProvider(
            url="https://sap.example.test/soap",
            session_factory=lambda: RecordingSession(empty_response()),
        )

        provider.get_tables(["zmissing"], progress_callback=not_found_messages.append)

        self.assertEqual(
            not_found_messages,
            ["Calling SAP to get DDIC metadata", "Called SAP to get DDIC metadata"],
        )

        failure_messages = []
        failing_provider = SapDdicMetadataProvider(
            url="https://sap.example.test/soap",
            session_factory=lambda: TimeoutSession(),
        )
        with self.assertRaises(DdicMetadataError):
            failing_provider.get_tables(["edidc"], progress_callback=failure_messages.append)

        self.assertEqual(failure_messages, ["Calling SAP to get DDIC metadata"])

    def test_provider_wraps_request_timeout(self):
        provider = SapDdicMetadataProvider(
            url="https://sap.example.test/soap",
            timeout=1,
            session_factory=lambda: TimeoutSession(),
        )

        with self.assertRaisesRegex(DdicMetadataTimeoutError, "timed out"):
            provider.get_tables(["EDIDC"])

    def test_configured_provider_defaults_to_noop(self):
        provider = get_configured_ddic_metadata_provider({"SAP_DDIC_METADATA_ENABLED": False})

        self.assertIsInstance(provider, NoOpDdicMetadataProvider)

    def test_configured_provider_uses_sap_settings_when_enabled(self):
        provider = get_configured_ddic_metadata_provider(
            {
                "SAP_DDIC_METADATA_ENABLED": True,
                "SAP_API_BASE_URL": "https://sap.example.test/soap",
                "SAP_API_USER": "user",
                "SAP_API_PASSWORD": "pass",
                "SAP_API_TIMEOUT": 5,
                "SAP_API_VERIFY": False,
                "SAP_API_CLIENT": "200",
                "SAP_DDIC_CACHE_MAX_TABLES": 3,
            }
        )

        self.assertIsInstance(provider, SapDdicMetadataProvider)
        self.assertEqual(provider.url, "https://sap.example.test/soap")
        self.assertEqual(provider.user, "user")
        self.assertEqual(provider.password, "pass")
        self.assertEqual(provider.timeout, 5)
        self.assertFalse(provider.verify)
        self.assertEqual(provider.sap_client, "200")
        self.assertEqual(provider.cache_max_tables, 3)


class RecordingSession:
    def __init__(self, response_text):
        self.response_text = response_text
        self.calls = []
        self.trust_env = True

    def post(self, url, data, headers, auth, timeout, verify):
        self.calls.append(
            {
                "url": url,
                "data": data,
                "headers": headers,
                "auth": auth,
                "timeout": timeout,
                "verify": verify,
            }
        )
        return RecordingResponse(self.response_text)


class TimeoutSession:
    trust_env = True

    def post(self, *args, **kwargs):
        raise requests.exceptions.Timeout()


class RecordingResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def extract_tabname(request_xml):
    return request_xml.split("<TABNAME>", 1)[1].split("</TABNAME>", 1)[0]


def sample_response():
    return """
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <ZDDIF_FIELDINFO_GETResponse>
      <DFIES_TAB>
        <item>
          <FIELDNAME>DOCNUM</FIELDNAME>
          <ROLLNAME>EDI_DOCNUM</ROLLNAME>
          <DATATYPE>NUMC</DATATYPE>
          <LENG>16</LENG>
          <DECIMALS>0</DECIMALS>
          <SCRTEXT_L>IDoc number</SCRTEXT_L>
          <KEYFLAG>X</KEYFLAG>
        </item>
        <item>
          <FIELDNAME>MESTYP</FIELDNAME>
          <ROLLNAME>EDI_MESTYP</ROLLNAME>
          <DATATYPE>CHAR</DATATYPE>
          <LENG>30</LENG>
          <DECIMALS>0</DECIMALS>
          <SCRTEXT_L>Message Type</SCRTEXT_L>
          <KEYFLAG></KEYFLAG>
        </item>
      </DFIES_TAB>
    </ZDDIF_FIELDINFO_GETResponse>
  </soapenv:Body>
</soapenv:Envelope>
"""


def empty_response():
    return """
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <ZDDIF_FIELDINFO_GETResponse>
      <DFIES_TAB/>
    </ZDDIF_FIELDINFO_GETResponse>
  </soapenv:Body>
</soapenv:Envelope>
"""


if __name__ == "__main__":
    unittest.main()
