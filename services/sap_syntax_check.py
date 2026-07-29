import re
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth


SAP_RFC_NAMESPACE = "urn:sap-com:document:sap:rfc:functions"
SOAP_NAMESPACE = "http://schemas.xmlsoap.org/soap/envelope/"
DEFAULT_PROGRAM_NAME = "ZTMP_SYNTAX_CHECK"


class SapSyntaxChecker:
    SOAP_ACTION = (
        "urn:sap-com:document:sap:rfc:functions:"
        "ZGENERATE_PROGRAM:ZGENERATE_PROGRAMRequest"
    )

    def __init__(
        self,
        url=None,
        user=None,
        password=None,
        timeout=30,
        verify=True,
        session_factory=None,
    ):
        self.url = url
        self.user = user
        self.password = password
        self.timeout = timeout
        self.verify = verify
        self.session_factory = session_factory or requests.Session

    def check(self, source_code, program_name=None):
        if not self.url:
            return unavailable_result("SAP syntax check unavailable: SAP_SYNTAX_CHECK_URL is not configured")

        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.post(
                self.url,
                data=self.build_request(source_code, program_name=program_name),
                headers={
                    "Content-Type": "text/xml;charset=UTF-8",
                    "SOAPAction": self.SOAP_ACTION,
                },
                auth=self.build_auth(),
                timeout=self.timeout,
                verify=self.verify,
            )
            response.raise_for_status()
            return normalize_sap_syntax_response(response.text, source_code)
        except requests.exceptions.Timeout:
            return unavailable_result("SAP syntax check unavailable: request timed out")
        except requests.exceptions.RequestException as exc:
            return unavailable_result(f"SAP syntax check unavailable: {exc}")
        except ET.ParseError as exc:
            return unavailable_result(f"SAP syntax check unavailable: invalid SOAP response: {exc}")

    def build_auth(self):
        if self.user and self.password:
            return HTTPBasicAuth(self.user, self.password)
        return None

    def build_request(self, source_code, program_name=None):
        ET.register_namespace("soapenv", SOAP_NAMESPACE)
        ET.register_namespace("urn", SAP_RFC_NAMESPACE)
        envelope = ET.Element(f"{{{SOAP_NAMESPACE}}}Envelope")
        ET.SubElement(envelope, f"{{{SOAP_NAMESPACE}}}Header")
        body = ET.SubElement(envelope, f"{{{SOAP_NAMESPACE}}}Body")
        function = ET.SubElement(body, f"{{{SAP_RFC_NAMESPACE}}}ZGENERATE_PROGRAM")
        ET.SubElement(function, "REPID").text = derive_program_name(source_code, program_name)
        t_code = ET.SubElement(function, "T_CODE")
        for line in source_lines(source_code):
            ET.SubElement(t_code, "item").text = line
        return ET.tostring(envelope, encoding="unicode", short_empty_elements=False)


def get_configured_sap_syntax_checker(config):
    return SapSyntaxChecker(
        url=config.get("SAP_SYNTAX_CHECK_URL"),
        user=config.get("SAP_API_USER"),
        password=config.get("SAP_API_PASSWORD"),
        timeout=config.get("SAP_SYNTAX_CHECK_TIMEOUT_SECONDS", config.get("SAP_API_TIMEOUT", 30)),
        verify=config.get("SAP_API_VERIFY", True),
    )


def normalize_sap_syntax_response(xml_text, source_code):
    root = ET.fromstring(xml_text or "")
    fault = find_text(root, "faultstring") or find_text(root, "faultcode")
    if fault:
        result = unavailable_result(f"SAP syntax check unavailable: {fault}")
        result["raw_response"] = xml_text
        return result

    line_text = find_text(root, "LINE")
    message = find_text(root, "MESSAGE")
    word = find_text(root, "WORD")
    if not line_text and not message:
        return {
            "requested": True,
            "status": "passed",
            "passed": True,
            "errors": [],
            "raw_response": xml_text,
            "technical_message": "",
        }

    line_number = parse_int(line_text)
    lines = source_lines(source_code)
    source_line = lines[line_number - 1] if line_number and 1 <= line_number <= len(lines) else ""
    return {
        "requested": True,
        "status": "failed",
        "passed": False,
        "errors": [
            {
                "line": line_number,
                "column": None,
                "severity": "E",
                "message": message,
                "word": word,
                "source_line": source_line,
            }
        ],
        "raw_response": xml_text,
        "technical_message": "",
    }


def unavailable_result(message):
    return {
        "requested": True,
        "status": "unavailable",
        "passed": False,
        "errors": [],
        "raw_response": "",
        "technical_message": message,
    }


def derive_program_name(source_code, program_name=None):
    if program_name and str(program_name).strip():
        return str(program_name).strip().upper()
    for line in source_lines(source_code):
        match = re.match(r"^\s*(?:REPORT|PROGRAM)\s+([A-Z][A-Z0-9_]*)\b", line, flags=re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return DEFAULT_PROGRAM_NAME


def source_lines(source_code):
    return re.split(r"\r\n|\r|\n", source_code or "")


def find_text(node, tag_name):
    for element in node.iter():
        if local_name(element.tag).upper() == tag_name.upper():
            return (element.text or "").strip()
    return ""


def local_name(tag):
    return tag.rsplit("}", 1)[1] if "}" in tag else tag


def parse_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
