import unittest

from requests.auth import HTTPBasicAuth

from services.sap_syntax_check import SapSyntaxChecker, normalize_sap_syntax_response


class SapSyntaxCheckTest(unittest.TestCase):
    def test_builds_zgenerate_program_request(self):
        session = RecordingSession(passing_response())
        checker = SapSyntaxChecker(
            url="https://sap.example.test/syntax",
            user="user",
            password="pass",
            timeout=7,
            verify=False,
            session_factory=lambda: session,
        )

        result = checker.check("REPORT zsyntax.\nWRITE 'ok'.")

        self.assertEqual(result["status"], "passed")
        self.assertEqual(session.calls[0]["url"], "https://sap.example.test/syntax")
        self.assertIn("ZGENERATE_PROGRAM", session.calls[0]["data"])
        self.assertIn("<REPID>ZSYNTAX</REPID>", session.calls[0]["data"])
        self.assertIn("<item>WRITE 'ok'.</item>", session.calls[0]["data"])
        self.assertEqual(
            session.calls[0]["headers"]["SOAPAction"],
            "urn:sap-com:document:sap:rfc:functions:ZGENERATE_PROGRAM:ZGENERATE_PROGRAMRequest",
        )
        self.assertEqual(session.calls[0]["timeout"], 7)
        self.assertFalse(session.calls[0]["verify"])
        self.assertIsInstance(session.calls[0]["auth"], HTTPBasicAuth)
        self.assertFalse(session.trust_env)

    def test_normalizes_sap_syntax_error(self):
        result = normalize_sap_syntax_response(
            failing_response(),
            "REPORT zsyntax.\nWRITE missing.",
        )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["passed"])
        self.assertEqual(result["raw_response"], failing_response())
        self.assertEqual(
            result["errors"],
            [
                {
                    "line": 2,
                    "column": None,
                    "severity": "E",
                    "message": "Field MISSING is unknown.",
                    "word": "MISSING",
                    "source_line": "WRITE missing.",
                }
            ],
        )


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


class RecordingResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def passing_response():
    return """
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <ZGENERATE_PROGRAMResponse/>
  </soapenv:Body>
</soapenv:Envelope>
"""


def failing_response():
    return """
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <ZGENERATE_PROGRAMResponse>
      <LINE>2</LINE>
      <MESSAGE>Field MISSING is unknown.</MESSAGE>
      <WORD>MISSING</WORD>
    </ZGENERATE_PROGRAMResponse>
  </soapenv:Body>
</soapenv:Envelope>
"""


if __name__ == "__main__":
    unittest.main()
