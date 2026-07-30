from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth


SAP_RFC_NAMESPACE = "urn:sap-com:document:sap:rfc:functions"
SOAP_NAMESPACE = "http://schemas.xmlsoap.org/soap/envelope/"


class CallableSignatureProvider:
    def get_signatures(self, callable_identities):
        raise NotImplementedError


class NoOpCallableSignatureProvider(CallableSignatureProvider):
    def get_signatures(self, callable_identities):
        return {}


class LocalCallableSignatureCache(CallableSignatureProvider):
    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)

    def get_signatures(self, callable_identities):
        signatures = {}
        diagnostics = {"cacheHits": [], "cacheMisses": []}
        for identity in dedupe_callable_identities(callable_identities):
            cached = self.load_signature(identity)
            if cached is None:
                diagnostics["cacheMisses"].append(identity)
                continue
            signatures[identity] = cached
            diagnostics["cacheHits"].append(identity)
        result = {"callable_signatures": signatures}
        if diagnostics["cacheHits"] or diagnostics["cacheMisses"]:
            result["_diagnostics"] = diagnostics
        return result

    def save_signatures(self, metadata):
        for identity, signature in normalize_provider_signatures(metadata).items():
            self.save_signature(identity, signature)

    def load_signature(self, identity):
        path = self.signature_path(identity)
        if not path.exists():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def save_signature(self, identity, signature):
        normalized_identity = normalize_callable_identity(identity)
        if not normalized_identity or not isinstance(signature, dict):
            return
        path = self.signature_path(normalized_identity)
        existing = self.load_signature(normalized_identity)
        if existing == signature:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(signature, indent=2), encoding="utf-8")

    def signature_path(self, identity):
        target = parse_callable_identity(identity)
        directory = "methods" if target["kind"] == "METHOD" else "functions"
        return self.cache_dir / directory / f"{cache_file_stem(target['identity'])}.json"


class ModeAwareCallableSignatureProvider(CallableSignatureProvider):
    VALID_MODES = {"cache_first", "sap_first", "cache_only", "sap_only"}

    def __init__(self, cache_provider, sap_provider=None, mode="cache_first"):
        self.cache_provider = cache_provider
        self.sap_provider = sap_provider or NoOpCallableSignatureProvider()
        self.mode = normalize_metadata_mode(mode)

    def get_signatures(self, callable_identities):
        identities = dedupe_callable_identities(callable_identities)
        if not identities:
            return {}
        if self.mode == "cache_only":
            return self.cache_provider.get_signatures(identities)
        if self.mode == "sap_only":
            return self.fetch_from_sap(identities)
        if self.mode == "sap_first":
            return self.get_signatures_sap_first(identities)
        return self.get_signatures_cache_first(identities)

    def get_signatures_cache_first(self, identities):
        cached = self.cache_provider.get_signatures(identities)
        missing = missing_callable_identities(identities, cached)
        if not missing:
            return cached
        fetched = self.fetch_from_sap(missing)
        return merge_callable_provider_metadata(cached, fetched)

    def get_signatures_sap_first(self, identities):
        fetched = self.fetch_from_sap(identities)
        missing = missing_callable_identities(identities, fetched)
        if not missing:
            return fetched
        cached = self.cache_provider.get_signatures(missing)
        return merge_callable_provider_metadata(fetched, cached)

    def fetch_from_sap(self, identities):
        fetched = self.sap_provider.get_signatures(identities)
        self.cache_provider.save_signatures(fetched)
        return fetched if isinstance(fetched, dict) else {}


class SapCallableSignatureProvider(CallableSignatureProvider):
    FUNCTION_SOAP_ACTION = (
        "urn:sap-com:document:sap:rfc:functions:"
        "ZRFC_GET_FUNCTION_INTERFACE:ZRFC_GET_FUNCTION_INTERFACERequest"
    )
    METHOD_SOAP_ACTION = (
        "urn:sap-com:document:sap:rfc:functions:"
        "ZCLASS_SIGNATURE:ZCLASS_SIGNATURERequest"
    )

    def __init__(
        self,
        function_url=None,
        method_url=None,
        user=None,
        password=None,
        timeout=30,
        verify=True,
        session_factory=None,
    ):
        self.function_url = function_url
        self.method_url = method_url
        self.user = user
        self.password = password
        self.timeout = timeout
        self.verify = verify
        self.session_factory = session_factory or requests.Session

    def get_signatures(self, callable_identities):
        diagnostics = {
            "functionSignatureUrlConfigured": bool(self.function_url),
            "methodSignatureUrlConfigured": bool(self.method_url),
            "functionTargetsDetected": [],
            "methodTargetsDetected": [],
            "functionTargetsCalled": [],
            "methodTargetsCalled": [],
            "signaturesReturned": 0,
            "unresolved": [],
            "errors": [],
        }
        signatures = {}
        for identity in dedupe_callable_identities(callable_identities):
            target = parse_callable_identity(identity)
            if target["kind"] == "FUNCTION":
                diagnostics["functionTargetsDetected"].append(target["identity"])
                signature = self._lookup_function(target["name"], diagnostics)
            elif target["kind"] == "METHOD":
                diagnostics["methodTargetsDetected"].append(target["identity"])
                signature = self._lookup_method(target["class"], target["method"], diagnostics)
            else:
                signature = None
                diagnostics["unresolved"].append({"identity": identity, "reason": "unsupported callable identity"})

            if signature:
                signatures[target["identity"]] = signature
            elif target["kind"] in {"FUNCTION", "METHOD"}:
                diagnostics["unresolved"].append({"identity": target["identity"], "reason": "signature not retrieved"})

        diagnostics["signaturesReturned"] = len(signatures)
        result = {"callable_signatures": signatures}
        if diagnostics["unresolved"] or diagnostics["errors"] or callable_identities:
            result["_diagnostics"] = diagnostics
        return result

    def build_auth(self):
        if self.user and self.password:
            return HTTPBasicAuth(self.user, self.password)
        return None

    def build_function_signature_request(self, function_name):
        ET.register_namespace("soapenv", SOAP_NAMESPACE)
        ET.register_namespace("urn", SAP_RFC_NAMESPACE)
        envelope = ET.Element(f"{{{SOAP_NAMESPACE}}}Envelope")
        ET.SubElement(envelope, f"{{{SOAP_NAMESPACE}}}Header")
        body = ET.SubElement(envelope, f"{{{SOAP_NAMESPACE}}}Body")
        function = ET.SubElement(body, f"{{{SAP_RFC_NAMESPACE}}}ZRFC_GET_FUNCTION_INTERFACE")
        ET.SubElement(function, "FUNCNAME").text = str(function_name or "").strip().upper()
        ET.SubElement(function, "LANGUAGE")
        ET.SubElement(function, "NONE_UNICODE_LENGTH")
        return ET.tostring(envelope, encoding="unicode", short_empty_elements=False)

    def build_method_signature_request(self, class_name, method_name):
        ET.register_namespace("soapenv", SOAP_NAMESPACE)
        ET.register_namespace("urn", SAP_RFC_NAMESPACE)
        envelope = ET.Element(f"{{{SOAP_NAMESPACE}}}Envelope")
        ET.SubElement(envelope, f"{{{SOAP_NAMESPACE}}}Header")
        body = ET.SubElement(envelope, f"{{{SOAP_NAMESPACE}}}Body")
        function = ET.SubElement(body, f"{{{SAP_RFC_NAMESPACE}}}ZCLASS_SIGNATURE")
        ET.SubElement(function, "CLASS").text = str(class_name or "").strip().upper()
        ET.SubElement(function, "METHOD").text = str(method_name or "").strip().upper()
        return ET.tostring(envelope, encoding="unicode", short_empty_elements=False)

    def _lookup_function(self, function_name, diagnostics):
        identity = str(function_name or "").strip().upper()
        if not self.function_url:
            diagnostics["errors"].append("SAP function signature lookup skipped: SAP_FUNCTION_SIGNATURE_URL not configured")
            return None
        diagnostics["functionTargetsCalled"].append(identity)
        try:
            response = self._post(self.function_url, self.build_function_signature_request(identity), self.FUNCTION_SOAP_ACTION)
            return parse_function_signature_response(response.text, identity)
        except (requests.RequestException, ET.ParseError) as exc:
            diagnostics["errors"].append(f"SAP function signature lookup unavailable for {identity}: {exc}")
            return None

    def _lookup_method(self, class_name, method_name, diagnostics):
        identity = f"{str(class_name or '').strip().upper()}=>{str(method_name or '').strip().upper()}"
        if not self.method_url:
            diagnostics["errors"].append("SAP method signature lookup skipped: SAP_METHOD_SIGNATURE_URL not configured")
            return None
        diagnostics["methodTargetsCalled"].append(identity)
        try:
            response = self._post(self.method_url, self.build_method_signature_request(class_name, method_name), self.METHOD_SOAP_ACTION)
            return parse_method_signature_response(response.text, class_name, method_name)
        except (requests.RequestException, ET.ParseError) as exc:
            diagnostics["errors"].append(f"SAP method signature lookup unavailable for {identity}: {exc}")
            return None

    def _post(self, url, request_xml, soap_action):
        session = self.session_factory()
        session.trust_env = False
        response = session.post(
            url,
            data=request_xml,
            headers={
                "Content-Type": "text/xml;charset=UTF-8",
                "SOAPAction": soap_action,
            },
            auth=self.build_auth(),
            timeout=self.timeout,
            verify=self.verify,
        )
        response.raise_for_status()
        return response


def get_configured_callable_signature_provider(config):
    has_sap_urls = bool(config.get("SAP_FUNCTION_SIGNATURE_URL") or config.get("SAP_METHOD_SIGNATURE_URL"))
    has_cache_config = bool(
        config.get("SAP_CALLABLE_CACHE_DIR")
        or config.get("SAP_CALLABLE_METADATA_MODE")
        or config.get("SAP_METADATA_CACHE_MODE")
    )
    if not has_sap_urls and not has_cache_config:
        return NoOpCallableSignatureProvider()
    sap_provider = NoOpCallableSignatureProvider()
    if has_sap_urls:
        sap_provider = SapCallableSignatureProvider(
            function_url=config.get("SAP_FUNCTION_SIGNATURE_URL"),
            method_url=config.get("SAP_METHOD_SIGNATURE_URL"),
            user=config.get("SAP_API_USER"),
            password=config.get("SAP_API_PASSWORD"),
            timeout=config.get("SAP_API_TIMEOUT", 30),
            verify=config.get("SAP_API_VERIFY", True),
        )
    return ModeAwareCallableSignatureProvider(
        LocalCallableSignatureCache(config.get("SAP_CALLABLE_CACHE_DIR") or Path("cache") / "callables"),
        sap_provider=sap_provider,
        mode=config.get("SAP_CALLABLE_METADATA_MODE", config.get("SAP_METADATA_CACHE_MODE", "cache_first")),
    )


def normalize_provider_signatures(signatures):
    if not signatures:
        return {}
    if isinstance(signatures, dict) and "callable_signatures" in signatures:
        signatures = signatures["callable_signatures"]
    if isinstance(signatures, dict) and "callables" in signatures:
        signatures = signatures["callables"]
    return signatures if isinstance(signatures, dict) else {}


def resolve_callable_metadata_for_identities(callable_identities, internal_metadata=None, signature_provider=None):
    if internal_metadata:
        return internal_metadata

    provider = signature_provider or NoOpCallableSignatureProvider()
    identities = list(callable_identities or [])
    if not identities:
        return {}
    provider_metadata = provider.get_signatures(identities)
    signatures = normalize_provider_signatures(provider_metadata)
    result = {}
    if not signatures:
        return provider_metadata if isinstance(provider_metadata, dict) else {}
    result["callable_signatures"] = signatures
    if isinstance(provider_metadata, dict):
        for key in ("_diagnostics", "unresolved", "technical_mapping", "callable_mappings"):
            if key in provider_metadata:
                result[key] = provider_metadata[key]
    return result


def merge_callable_metadata(*metadata_items):
    merged = {"callable_signatures": {}}
    diagnostics = []
    unresolved = []
    for metadata in metadata_items:
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                if key not in {"callable_signatures", "callables", "_diagnostics", "unresolved"} and key not in merged:
                    merged[key] = value
            if isinstance(metadata.get("_diagnostics"), dict):
                diagnostics.append(metadata["_diagnostics"])
            unresolved.extend(metadata.get("unresolved", []) or [])
            diagnostic_unresolved = (
                metadata.get("_diagnostics", {}).get("unresolved", [])
                if isinstance(metadata.get("_diagnostics"), dict)
                else []
            )
            for diagnostic in diagnostic_unresolved:
                unresolved.append(diagnostic)
        signatures = normalize_provider_signatures(metadata)
        for name, signature in signatures.items():
            merged["callable_signatures"][name] = signature
    if diagnostics:
        merged["_diagnostics"] = diagnostics
    if unresolved:
        merged["unresolved"] = unresolved
    return merged if (merged["callable_signatures"] or diagnostics or unresolved) else {}


def callable_identities_from_source(source, parser):
    return sorted({call["name"] for call in parser(source.splitlines())})


def resolve_callable_metadata(source, parser, internal_metadata=None, signature_provider=None):
    if internal_metadata:
        return internal_metadata

    provider = signature_provider or NoOpCallableSignatureProvider()
    identities = callable_identities_from_source(source, parser)
    # Callable metadata is intentionally internal: the current provider is a
    # no-op, and a future SAP API provider can populate signatures automatically.
    return resolve_callable_metadata_for_identities(identities, signature_provider=provider)


def dedupe_callable_identities(callable_identities):
    seen = set()
    result = []
    for identity in callable_identities or []:
        normalized = normalize_callable_identity(identity)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def normalize_callable_identity(identity):
    text = str(identity or "").strip().upper()
    return text.replace("~", "=>").replace("->", "=>")


def parse_callable_identity(identity):
    text = normalize_callable_identity(identity)
    if "=>" in text:
        class_name, method_name = text.split("=>", 1)
        if class_name and method_name:
            return {"kind": "METHOD", "class": class_name, "method": method_name, "identity": f"{class_name}=>{method_name}"}
    return {"kind": "FUNCTION", "name": text, "identity": text} if text else {"kind": "", "identity": ""}


def parse_function_signature_response(xml_text, function_name):
    root = ET.fromstring(xml_text or "")
    parameters = {}
    for item in iter_items(root, "PARAMS"):
        raw_direction = child_text(item, "PARAMCLASS").upper()
        name = child_text(item, "PARAMETER").upper()
        if not name or raw_direction == "X":
            continue
        parameters[name] = {
            "direction": map_function_direction(raw_direction),
            "rawDirection": raw_direction,
            "abap_type": child_text(item, "TABNAME") or child_text(item, "EXID"),
            "field": child_text(item, "FIELDNAME"),
            "required": child_text(item, "OPTIONAL").upper() != "X",
        }
    return {"parameters": parameters}


def parse_method_signature_response(xml_text, class_name, method_name):
    root = ET.fromstring(xml_text or "")
    parameters = {}
    returning = None
    for item in iter_items(root, "T_ZSEOSUBCODF"):
        name = child_text(item, "SCONAME").upper()
        if not name:
            continue
        parameter = {
            "direction": map_method_direction(child_text(item, "PARDECLTYP")),
            "rawDirection": child_text(item, "PARDECLTYP"),
            "abap_type": child_text(item, "TYPE") or child_text(item, "TABLEOF"),
            "required": child_text(item, "PAROPTIONL").upper() != "X",
        }
        if child_text(item, "EXCDECLTYP") not in {"", "0"}:
            continue
        if parameter["direction"] == "RETURNING":
            returning = {"name": name, **parameter}
        else:
            parameters[name] = parameter
    result = {
        "class": str(class_name or "").strip().upper(),
        "method": str(method_name or "").strip().upper(),
        "parameters": parameters,
    }
    if returning:
        result["returning"] = returning
    return result


def iter_items(root, table_name):
    for table_node in root.iter():
        if local_name(table_node.tag).upper() != table_name:
            continue
        for item in list(table_node):
            if local_name(item.tag).upper() == "ITEM":
                yield item


def child_text(node, tag_name):
    for child in list(node):
        if local_name(child.tag).upper() == tag_name.upper():
            return (child.text or "").strip()
    return ""


def local_name(tag):
    return tag.rsplit("}", 1)[1] if "}" in tag else tag


def map_function_direction(raw_direction):
    return {
        "I": "IMPORTING",
        "E": "EXPORTING",
        "C": "CHANGING",
        "T": "TABLES",
    }.get(str(raw_direction or "").upper(), "UNRESOLVED")


def map_method_direction(raw_direction):
    return {
        "1": "IMPORTING",
        "2": "EXPORTING",
        "3": "CHANGING",
        "4": "RETURNING",
    }.get(str(raw_direction or "").upper(), "UNRESOLVED")


def missing_callable_identities(identities, metadata):
    signatures = normalize_provider_signatures(metadata)
    normalized = {normalize_callable_identity(identity) for identity in signatures}
    return [identity for identity in identities if identity not in normalized]


def merge_callable_provider_metadata(*metadata_items):
    merged = {"callable_signatures": {}}
    diagnostics = []
    unresolved = []
    for metadata in metadata_items:
        if not isinstance(metadata, dict):
            continue
        for identity, signature in normalize_provider_signatures(metadata).items():
            merged["callable_signatures"][normalize_callable_identity(identity)] = deepcopy(signature)
        if "_diagnostics" in metadata:
            diagnostics.append(metadata["_diagnostics"])
        unresolved.extend(metadata.get("unresolved", []) or [])
    if diagnostics:
        merged["_diagnostics"] = diagnostics
    if unresolved:
        merged["unresolved"] = unresolved
    return merged if (merged["callable_signatures"] or diagnostics or unresolved) else {}


def normalize_metadata_mode(mode):
    normalized = str(mode or "cache_first").strip().lower()
    if normalized not in ModeAwareCallableSignatureProvider.VALID_MODES:
        return "cache_first"
    return normalized


def cache_file_stem(identity):
    return quote(normalize_callable_identity(identity), safe="")
