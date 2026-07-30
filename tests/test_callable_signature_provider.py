import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

import requests
from requests.auth import HTTPBasicAuth

from services.callable_signature_provider import (
    LocalCallableSignatureCache,
    ModeAwareCallableSignatureProvider,
    NoOpCallableSignatureProvider,
    SapCallableSignatureProvider,
    get_configured_callable_signature_provider,
    map_method_direction,
)


class CallableSignatureProviderTest(unittest.TestCase):
    def test_noop_provider_returns_no_metadata(self):
        self.assertEqual(NoOpCallableSignatureProvider().get_signatures(["Z_TEST"]), {})

    def test_sap_provider_reuses_old_function_and_method_contract(self):
        session = RecordingSession(
            {
                "https://sap.example.test/function": function_response(),
                "https://sap.example.test/method": method_response(),
            }
        )
        provider = SapCallableSignatureProvider(
            function_url="https://sap.example.test/function",
            method_url="https://sap.example.test/method",
            user="user",
            password="pass",
            timeout=9,
            verify=False,
            session_factory=lambda: session,
        )

        metadata = provider.get_signatures(["z_test_function", "zcl_test=>execute"])

        self.assertEqual(set(metadata["callable_signatures"]), {"Z_TEST_FUNCTION", "ZCL_TEST=>EXECUTE"})
        self.assertEqual(
            metadata["callable_signatures"]["Z_TEST_FUNCTION"]["parameters"]["IV_INPUT"]["direction"],
            "IMPORTING",
        )
        self.assertEqual(
            metadata["callable_signatures"]["ZCL_TEST=>EXECUTE"]["returning"]["direction"],
            "RETURNING",
        )
        self.assertEqual(
            metadata["callable_signatures"]["ZCL_TEST=>EXECUTE"]["parameters"]["IV_INPUT"]["direction"],
            "IMPORTING",
        )
        function_call = session.calls[0]
        method_call = session.calls[1]
        self.assertEqual(function_call["url"], "https://sap.example.test/function")
        self.assertIn("<urn:ZRFC_GET_FUNCTION_INTERFACE>", function_call["data"])
        self.assertIn("<FUNCNAME>Z_TEST_FUNCTION</FUNCNAME>", function_call["data"])
        self.assertEqual(
            function_call["headers"]["SOAPAction"],
            "urn:sap-com:document:sap:rfc:functions:ZRFC_GET_FUNCTION_INTERFACE:ZRFC_GET_FUNCTION_INTERFACERequest",
        )
        self.assertEqual(method_call["url"], "https://sap.example.test/method")
        self.assertIn("<urn:ZCLASS_SIGNATURE>", method_call["data"])
        self.assertIn("<CLASS>ZCL_TEST</CLASS>", method_call["data"])
        self.assertIn("<METHOD>EXECUTE</METHOD>", method_call["data"])
        self.assertEqual(
            method_call["headers"]["SOAPAction"],
            "urn:sap-com:document:sap:rfc:functions:ZCLASS_SIGNATURE:ZCLASS_SIGNATURERequest",
        )
        self.assertEqual(function_call["timeout"], 9)
        self.assertFalse(function_call["verify"])
        self.assertIsInstance(function_call["auth"], HTTPBasicAuth)
        self.assertFalse(session.trust_env)

    def test_unresolved_signature_is_recorded_without_guessing_parameters(self):
        provider = SapCallableSignatureProvider(
            function_url=None,
            method_url=None,
        )

        metadata = provider.get_signatures(["Z_MISSING_FUNCTION", "ZCL_MISSING=>RUN"])

        self.assertEqual(metadata["callable_signatures"], {})
        unresolved = {item["identity"] for item in metadata["_diagnostics"]["unresolved"]}
        self.assertEqual(unresolved, {"Z_MISSING_FUNCTION", "ZCL_MISSING=>RUN"})

    def test_configured_provider_defaults_to_noop(self):
        provider = get_configured_callable_signature_provider({})

        self.assertIsInstance(provider, NoOpCallableSignatureProvider)

    def test_cache_first_reads_cached_function_and_fetches_missing_method(self):
        temp_path = test_temp_path()
        try:
            cache = LocalCallableSignatureCache(temp_path / "callables")
            cache.save_signature("Z_TEST_FUNCTION", cached_signature())
            session = RecordingSession({"https://sap.example.test/method": method_response()})
            provider = ModeAwareCallableSignatureProvider(
                cache,
                SapCallableSignatureProvider(method_url="https://sap.example.test/method", session_factory=lambda: session),
                mode="cache_first",
            )

            metadata = provider.get_signatures(["Z_TEST_FUNCTION", "ZCL_TEST=>EXECUTE"])

            self.assertEqual(set(metadata["callable_signatures"]), {"Z_TEST_FUNCTION", "ZCL_TEST=>EXECUTE"})
            self.assertEqual([call["url"] for call in session.calls], ["https://sap.example.test/method"])
            self.assertTrue((temp_path / "callables" / "functions" / "Z_TEST_FUNCTION.json").exists())
            self.assertTrue((temp_path / "callables" / "methods" / "ZCL_TEST%3D%3EEXECUTE.json").exists())
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_sap_first_falls_back_to_cached_callable_when_sap_returns_no_signature(self):
        temp_path = test_temp_path()
        try:
            cache = LocalCallableSignatureCache(temp_path / "callables")
            cache.save_signature("Z_TEST_FUNCTION", cached_signature())
            provider = ModeAwareCallableSignatureProvider(
                cache,
                SapCallableSignatureProvider(function_url=None),
                mode="sap_first",
            )

            metadata = provider.get_signatures(["Z_TEST_FUNCTION"])

            self.assertEqual(metadata["callable_signatures"]["Z_TEST_FUNCTION"], cached_signature())
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_cache_only_does_not_call_sap_or_rewrite_callable_cache(self):
        temp_path = test_temp_path()
        try:
            cache = LocalCallableSignatureCache(temp_path / "callables")
            cache.save_signature("Z_TEST_FUNCTION", cached_signature())
            cache_path = temp_path / "callables" / "functions" / "Z_TEST_FUNCTION.json"
            original = cache_path.read_text(encoding="utf-8")
            session = RecordingSession({"https://sap.example.test/function": function_response()})
            provider = ModeAwareCallableSignatureProvider(
                cache,
                SapCallableSignatureProvider(function_url="https://sap.example.test/function", session_factory=lambda: session),
                mode="cache_only",
            )

            metadata = provider.get_signatures(["Z_TEST_FUNCTION", "Z_MISSING"])

            self.assertEqual(set(metadata["callable_signatures"]), {"Z_TEST_FUNCTION"})
            self.assertEqual(session.calls, [])
            self.assertEqual(cache_path.read_text(encoding="utf-8"), original)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_sap_only_ignores_cache_and_saves_successful_callable_signature(self):
        temp_path = test_temp_path()
        try:
            cache = LocalCallableSignatureCache(temp_path / "callables")
            cache.save_signature("Z_TEST_FUNCTION", cached_signature())
            session = RecordingSession({"https://sap.example.test/function": function_response()})
            provider = ModeAwareCallableSignatureProvider(
                cache,
                SapCallableSignatureProvider(function_url="https://sap.example.test/function", session_factory=lambda: session),
                mode="sap_only",
            )

            metadata = provider.get_signatures(["Z_TEST_FUNCTION"])

            self.assertEqual([call["url"] for call in session.calls], ["https://sap.example.test/function"])
            self.assertIn("IV_INPUT", metadata["callable_signatures"]["Z_TEST_FUNCTION"]["parameters"])
            saved = json.loads((temp_path / "callables" / "functions" / "Z_TEST_FUNCTION.json").read_text(encoding="utf-8"))
            self.assertIn("IV_INPUT", saved["parameters"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_configured_provider_uses_mode_aware_callable_cache_when_urls_are_configured(self):
        provider = get_configured_callable_signature_provider(
            {
                "SAP_FUNCTION_SIGNATURE_URL": "https://sap.example.test/function",
                "SAP_METHOD_SIGNATURE_URL": "https://sap.example.test/method",
                "SAP_CALLABLE_CACHE_DIR": "cache/callables",
                "SAP_CALLABLE_METADATA_MODE": "sap_first",
            }
        )

        self.assertIsInstance(provider, ModeAwareCallableSignatureProvider)
        self.assertEqual(provider.mode, "sap_first")
        self.assertIsInstance(provider.sap_provider, SapCallableSignatureProvider)

    def test_method_direction_codes_match_sap_raw_direction_values(self):
        self.assertEqual(map_method_direction("0"), "IMPORTING")
        self.assertEqual(map_method_direction("1"), "EXPORTING")
        self.assertEqual(map_method_direction("2"), "CHANGING")
        self.assertEqual(map_method_direction("3"), "RETURNING")

    def test_cached_method_signature_is_normalized_from_raw_direction(self):
        temp_path = test_temp_path()
        try:
            cache = LocalCallableSignatureCache(temp_path / "callables")
            path = temp_path / "callables" / "methods" / "ZCL_TEST%3D%3EEXECUTE.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "class": "ZCL_TEST",
                        "method": "EXECUTE",
                        "parameters": {
                            "IV_INPUT": {"direction": "UNRESOLVED", "rawDirection": "0"},
                            "EV_OUTPUT": {"direction": "IMPORTING", "rawDirection": "1"},
                            "CV_VALUE": {"direction": "EXPORTING", "rawDirection": "2"},
                            "RESULT": {"direction": "CHANGING", "rawDirection": "3"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            signature = cache.load_signature("ZCL_TEST=>EXECUTE")

            self.assertEqual(signature["parameters"]["IV_INPUT"]["direction"], "IMPORTING")
            self.assertEqual(signature["parameters"]["EV_OUTPUT"]["direction"], "EXPORTING")
            self.assertEqual(signature["parameters"]["CV_VALUE"]["direction"], "CHANGING")
            self.assertNotIn("RESULT", signature["parameters"])
            self.assertEqual(signature["returning"]["name"], "RESULT")
            self.assertEqual(signature["returning"]["direction"], "RETURNING")
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)


class RecordingSession:
    def __init__(self, responses):
        self.responses = responses
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
        if url not in self.responses:
            raise requests.exceptions.RequestException(f"No response for {url}")
        return RecordingResponse(self.responses[url])


class RecordingResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def function_response():
    return """
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <ZRFC_GET_FUNCTION_INTERFACEResponse>
      <PARAMS>
        <ITEM>
          <PARAMETER>IV_INPUT</PARAMETER>
          <PARAMCLASS>I</PARAMCLASS>
          <TABNAME>STRING</TABNAME>
          <FIELDNAME></FIELDNAME>
          <OPTIONAL></OPTIONAL>
        </ITEM>
        <ITEM>
          <PARAMETER>EV_OUTPUT</PARAMETER>
          <PARAMCLASS>E</PARAMCLASS>
          <TABNAME>CHAR10</TABNAME>
          <FIELDNAME></FIELDNAME>
          <OPTIONAL>X</OPTIONAL>
        </ITEM>
      </PARAMS>
    </ZRFC_GET_FUNCTION_INTERFACEResponse>
  </soapenv:Body>
</soapenv:Envelope>
"""


def method_response():
    return """
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <ZCLASS_SIGNATUREResponse>
      <T_ZSEOSUBCODF>
        <ITEM>
          <SCONAME>IV_INPUT</SCONAME>
          <PARDECLTYP>0</PARDECLTYP>
          <TYPE>STRING</TYPE>
          <PAROPTIONL></PAROPTIONL>
          <EXCDECLTYP>0</EXCDECLTYP>
        </ITEM>
        <ITEM>
          <SCONAME>RV_RESULT</SCONAME>
          <PARDECLTYP>3</PARDECLTYP>
          <TYPE>STRING</TYPE>
          <PAROPTIONL></PAROPTIONL>
          <EXCDECLTYP>0</EXCDECLTYP>
        </ITEM>
      </T_ZSEOSUBCODF>
    </ZCLASS_SIGNATUREResponse>
  </soapenv:Body>
</soapenv:Envelope>
"""


def cached_signature():
    return {
        "parameters": {
            "CACHED_INPUT": {
                "direction": "IMPORTING",
                "abap_type": "CHAR10",
                "required": False,
            }
        }
    }


def test_temp_path():
    return Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"


if __name__ == "__main__":
    unittest.main()
