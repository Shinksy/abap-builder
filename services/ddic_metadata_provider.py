from collections import OrderedDict
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth


class DdicMetadataError(RuntimeError):
    pass


class DdicMetadataTimeoutError(DdicMetadataError):
    pass


class DdicMetadataProvider:
    def get_tables(self, table_names, progress_callback=None):
        raise NotImplementedError


class NoOpDdicMetadataProvider(DdicMetadataProvider):
    def get_tables(self, table_names, progress_callback=None):
        return {
            "tables": {},
            "_diagnostics": [
                {"object": normalize_table_name(name), "event": "provider-unavailable"}
                for name in dedupe_table_names(table_names)
            ],
        }


class LocalDdicMetadataCache(DdicMetadataProvider):
    def __init__(self, cache_dir, legacy_path=None):
        self.cache_dir = Path(cache_dir)
        self.legacy_path = Path(legacy_path) if legacy_path else None

    def get_tables(self, table_names, progress_callback=None):
        names = dedupe_table_names(table_names)
        if names and progress_callback:
            progress_callback("Loading DDIC metadata from cache")
        result = {"tables": {}}
        diagnostics = []
        for name in names:
            cached = self.load_table(name)
            if cached is None:
                diagnostics.append({"object": name, "event": "cache-miss"})
                continue
            result["tables"][name] = deepcopy(cached)
            diagnostics.append({"object": name, "event": "cache-hit"})
        if names and progress_callback:
            progress_callback("Loaded DDIC metadata from cache")
        if diagnostics:
            result["_diagnostics"] = diagnostics
        return result

    def save_tables(self, metadata):
        tables = normalized_cache_tables(metadata)
        if not tables:
            return
        for name, table_metadata in tables.items():
            self.save_table(name, table_metadata)

    def load(self):
        tables = {}
        if self.cache_dir.exists():
            for path in self.cache_dir.glob("*.json"):
                table_metadata = self.load_table_file(path)
                name = normalize_table_name((table_metadata or {}).get("name") or path.stem)
                if name:
                    tables[name] = table_metadata
        if not tables:
            tables.update(normalized_cache_tables(self.load_legacy_raw()))
        return {"tables": tables}

    def load_table(self, table_name):
        normalized_name = normalize_table_name(table_name)
        path = self.table_path(normalized_name)
        if path.exists():
            return self.load_table_file(path)
        return normalized_cache_tables(self.load_legacy_raw()).get(normalized_name)

    def save_table(self, table_name, table_metadata):
        normalized_name = normalize_table_name(table_name)
        if not normalized_name:
            return
        path = self.table_path(normalized_name)
        existing = self.load_table_file(path) if path.exists() else None
        if existing == table_metadata:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(table_metadata, indent=2), encoding="utf-8")

    def table_path(self, table_name):
        return self.cache_dir / f"{cache_file_stem(table_name)}.json"

    def load_table_file(self, path):
        try:
            loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def load_legacy_raw(self):
        if not self.legacy_path or not self.legacy_path.exists():
            return {"tables": {}}
        try:
            loaded = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"tables": {}}
        return loaded if isinstance(loaded, dict) else {"tables": {}}


class ModeAwareDdicMetadataProvider(DdicMetadataProvider):
    VALID_MODES = {"cache_first", "sap_first", "cache_only", "sap_only"}

    def __init__(self, cache_provider, sap_provider=None, mode="cache_first"):
        self.cache_provider = cache_provider
        self.sap_provider = sap_provider or NoOpDdicMetadataProvider()
        self.mode = normalize_metadata_mode(mode)

    def get_tables(self, table_names, progress_callback=None):
        names = dedupe_table_names(table_names)
        if not names:
            return {"tables": {}}
        if self.mode == "cache_only":
            return self.cache_provider.get_tables(names, progress_callback=progress_callback)
        if self.mode == "sap_only":
            return self.fetch_from_sap(names, progress_callback=progress_callback)
        if self.mode == "sap_first":
            return self.get_tables_sap_first(names, progress_callback=progress_callback)
        return self.get_tables_cache_first(names, progress_callback=progress_callback)

    def get_tables_cache_first(self, names, progress_callback=None):
        cached = self.cache_provider.get_tables(names, progress_callback=progress_callback)
        missing = missing_table_names(names, cached)
        if not missing:
            return cached
        fetched = self.fetch_from_sap(missing, progress_callback=progress_callback, suppress_errors=True)
        return merge_provider_metadata(cached, fetched)

    def get_tables_sap_first(self, names, progress_callback=None):
        fetched = self.fetch_from_sap(names, progress_callback=progress_callback, suppress_errors=True)
        missing = missing_table_names(names, fetched)
        if not missing:
            return fetched
        cached = self.cache_provider.get_tables(missing, progress_callback=progress_callback)
        return merge_provider_metadata(fetched, cached)

    def fetch_from_sap(self, names, progress_callback=None, suppress_errors=False):
        try:
            fetched = self.sap_provider.get_tables(names, progress_callback=progress_callback)
        except DdicMetadataError as exc:
            if not suppress_errors:
                raise
            return {
                "tables": {},
                "_diagnostics": [
                    {"object": name, "event": "sap-fetch-failed", "message": str(exc)}
                    for name in names
                ],
            }
        self.cache_provider.save_tables(fetched)
        return fetched if isinstance(fetched, dict) else {"tables": {}}


class SapDdicMetadataProvider(DdicMetadataProvider):
    SOAP_ACTION = (
        "urn:sap-com:document:sap:rfc:functions:"
        "DDIF_FIELDINFO_GET:DDIF_FIELDINFO_GETRequest"
    )

    def __init__(
        self,
        url,
        user=None,
        password=None,
        timeout=30,
        verify=True,
        sap_client="100",
        cache_max_tables=128,
        session_factory=None,
    ):
        self.url = url
        self.user = user
        self.password = password
        self.timeout = timeout
        self.verify = verify
        self.sap_client = sap_client
        self.cache_max_tables = max(0, int(cache_max_tables or 0))
        self.session_factory = session_factory or requests.Session
        self._cache = OrderedDict()

    def get_tables(self, table_names, progress_callback=None):
        tables = {}
        diagnostics = []
        names = dedupe_table_names(table_names)
        all_cached = names and all(self._cache_get(normalize_table_name(name)) is not None for name in names)
        if names and progress_callback:
            progress_callback("Loading DDIC metadata from cache" if all_cached else "Calling SAP to get DDIC metadata")
        for table_name in names:
            metadata, events = self.get_table_with_diagnostics(table_name)
            tables[table_name] = metadata
            diagnostics.extend(events)
        if names and progress_callback:
            progress_callback("Loaded DDIC metadata from cache" if all_cached else "Called SAP to get DDIC metadata")
        result = {"tables": tables}
        if diagnostics:
            result["_diagnostics"] = diagnostics
        return result

    def get_table(self, table_name):
        metadata, _events = self.get_table_with_diagnostics(table_name)
        return metadata

    def get_table_with_diagnostics(self, table_name, progress_callback=None):
        normalized_name = normalize_table_name(table_name)
        cached = self._cache_get(normalized_name)
        if cached is not None:
            return cached, [{"object": normalized_name, "event": "cache-hit"}]

        events = [{"object": normalized_name, "event": "cache-miss"}]
        try:
            metadata = self._fetch_table(normalized_name)
        except DdicMetadataError:
            raise
        events.append({"object": normalized_name, "event": "sap-fetch-attempted"})
        if not metadata.get("fields"):
            events.append({"object": normalized_name, "event": "object-not-found"})
        self._cache_put(normalized_name, metadata)
        return deepcopy(metadata), events

    def build_auth(self):
        if self.user and self.password:
            return HTTPBasicAuth(self.user, self.password)
        return None

    def build_request(self, table_name):
        return f"""
<soapenv:Envelope
 xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:urn="urn:sap-com:document:sap:rfc:functions">
   <soapenv:Header/>
   <soapenv:Body>
      <urn:ZDDIF_FIELDINFO_GET>
         <TABNAME>{normalize_table_name(table_name)}</TABNAME>
      </urn:ZDDIF_FIELDINFO_GET>
   </soapenv:Body>
</soapenv:Envelope>
"""

    def _fetch_table(self, table_name):
        if not self.url:
            raise DdicMetadataError("SAP DDIC metadata URL is not configured.")

        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.post(
                self.url,
                data=self.build_request(table_name),
                headers={
                    "Content-Type": "text/xml;charset=UTF-8",
                    "SOAPAction": self.SOAP_ACTION,
                    "sap-client": self.sap_client,
                },
                auth=self.build_auth(),
                timeout=self.timeout,
                verify=self.verify,
            )
        except requests.exceptions.Timeout as exc:
            raise DdicMetadataTimeoutError(f"SAP DDIC metadata lookup timed out for {table_name}.") from exc
        except requests.exceptions.RequestException as exc:
            raise DdicMetadataError(f"SAP DDIC metadata lookup failed for {table_name}: {exc}") from exc

        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise DdicMetadataError(f"SAP DDIC metadata lookup failed for {table_name}: {exc}") from exc

        return normalize_sap_ddic_response(response.text, table_name)

    def _cache_get(self, table_name):
        if table_name not in self._cache:
            return None
        self._cache.move_to_end(table_name)
        return deepcopy(self._cache[table_name])

    def _cache_put(self, table_name, metadata):
        if self.cache_max_tables <= 0:
            return
        self._cache[table_name] = deepcopy(metadata)
        self._cache.move_to_end(table_name)
        while len(self._cache) > self.cache_max_tables:
            self._cache.popitem(last=False)


def get_configured_ddic_metadata_provider(config):
    if not config.get("SAP_DDIC_METADATA_ENABLED"):
        return NoOpDdicMetadataProvider()
    cache_provider = LocalDdicMetadataCache(
        config.get("SAP_DDIC_CACHE_DIR") or Path("cache") / "ddic",
        legacy_path=config.get("SAP_DDIC_CACHE_PATH"),
    )
    sap_provider = NoOpDdicMetadataProvider()
    if config.get("SAP_API_BASE_URL"):
        sap_provider = SapDdicMetadataProvider(
            url=config.get("SAP_API_BASE_URL"),
            user=config.get("SAP_API_USER"),
            password=config.get("SAP_API_PASSWORD"),
            timeout=config.get("SAP_API_TIMEOUT", 30),
            verify=config.get("SAP_API_VERIFY", True),
            sap_client=config.get("SAP_API_CLIENT", "100"),
            cache_max_tables=config.get("SAP_DDIC_CACHE_MAX_TABLES", 128),
        )
    return ModeAwareDdicMetadataProvider(
        cache_provider,
        sap_provider=sap_provider,
        mode=config.get("SAP_DDIC_METADATA_MODE", config.get("SAP_METADATA_CACHE_MODE", "cache_first")),
    )


def normalize_sap_ddic_response(xml_text, table_name):
    root = ET.fromstring(xml_text)
    fields = OrderedDict()
    for item in root.findall(".//item"):
        field = normalize_sap_ddic_field(item)
        if field["name"]:
            fields[field["name"]] = field

    return {
        "name": normalize_table_name(table_name),
        "field_count": len(fields),
        "fields": dict(fields),
        "field_order": list(fields.keys()),
    }


def normalize_sap_ddic_field(item):
    name = child_text(item, "FIELDNAME").upper()
    return {
        "name": name,
        "rollname": child_text(item, "ROLLNAME").upper(),
        "datatype": child_text(item, "DATATYPE").upper(),
        "length": parse_int(child_text(item, "LENG")),
        "decimals": parse_int(child_text(item, "DECIMALS")),
        "description": child_text(item, "SCRTEXT_L"),
        "key": child_text(item, "KEYFLAG").upper() == "X",
    }


def child_text(node, tag_name):
    for child in list(node):
        if local_name(child.tag).upper() == tag_name.upper():
            return (child.text or "").strip()
    return ""


def local_name(tag):
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_table_name(table_name):
    return str(table_name or "").strip().upper()


def dedupe_table_names(table_names):
    seen = set()
    result = []
    for table_name in table_names or []:
        normalized = normalize_table_name(table_name)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def normalized_cache_tables(metadata):
    if not isinstance(metadata, dict):
        return {}
    tables = metadata.get("tables") if isinstance(metadata.get("tables"), dict) else metadata
    if not isinstance(tables, dict):
        return {}
    return {
        normalize_table_name(name): deepcopy(value)
        for name, value in tables.items()
        if normalize_table_name(name)
    }


def missing_table_names(names, metadata):
    tables = normalized_cache_tables(metadata)
    return [name for name in names if name not in tables]


def merge_provider_metadata(*metadata_items):
    result = {"tables": {}}
    diagnostics = []
    for metadata in metadata_items:
        if not isinstance(metadata, dict):
            continue
        result["tables"].update(normalized_cache_tables(metadata))
        diagnostics.extend(metadata.get("_diagnostics") or [])
    if diagnostics:
        result["_diagnostics"] = diagnostics
    return result


def normalize_metadata_mode(mode):
    normalized = str(mode or "cache_first").strip().lower()
    if normalized not in ModeAwareDdicMetadataProvider.VALID_MODES:
        return "cache_first"
    return normalized


def cache_file_stem(identity):
    return quote(str(identity or "").strip().upper(), safe="")
