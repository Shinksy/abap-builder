import unittest

from requests.auth import HTTPBasicAuth

from services.callable_signature_provider import (
    NoOpCallableSignatureProvider,
    SapCallableSignatureProvider,
    get_configured_callable_signature_provider,
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
          <PARDECLTYP>1</PARDECLTYP>
          <TYPE>STRING</TYPE>
          <PAROPTIONL></PAROPTIONL>
          <EXCDECLTYP>0</EXCDECLTYP>
        </ITEM>
        <ITEM>
          <SCONAME>RV_RESULT</SCONAME>
          <PARDECLTYP>4</PARDECLTYP>
          <TYPE>STRING</TYPE>
          <PAROPTIONL></PAROPTIONL>
          <EXCDECLTYP>0</EXCDECLTYP>
        </ITEM>
      </T_ZSEOSUBCODF>
    </ZCLASS_SIGNATUREResponse>
  </soapenv:Body>
</soapenv:Envelope>
"""


if __name__ == "__main__":
    unittest.main()
