from io import BytesIO
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from app import create_app


class MetadataCacheUploadTest(unittest.TestCase):
    def test_home_includes_mandatory_metadata_type_radios(self):
        app = create_app({"TESTING": True})
        client = app.test_client()

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="metadata_type"', response.data)
        self.assertIn(b'value="table"', response.data)
        self.assertIn(b'value="function"', response.data)
        self.assertIn(b'value="method"', response.data)
        self.assertIn(b"required", response.data)

    def test_upload_table_metadata_uses_json_name_and_selected_table_folder(self):
        temp_path = test_temp_path()
        temp_path.mkdir()
        try:
            client = metadata_upload_client(temp_path)

            response = upload_metadata(
                client,
                "table",
                "ignored.json",
                {"name": "ztable", "fields": {"FIELD1": {"name": "FIELD1"}}},
            )

            self.assertEqual(response.status_code, 200)
            saved = json.loads((temp_path / "cache" / "ddic" / "ZTABLE.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["name"], "ZTABLE")
            self.assertEqual(saved["fields"], {"FIELD1": {"name": "FIELD1"}})
            self.assertIn(b"ZTABLE", response.data)
            self.assertIn(b"cache\\ddic\\ZTABLE.json", response.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_upload_function_metadata_ignores_filename_and_uses_json_name(self):
        temp_path = test_temp_path()
        temp_path.mkdir()
        try:
            client = metadata_upload_client(temp_path)

            response = upload_metadata(
                client,
                "function",
                "totally_different.json",
                {"name": "z_function_upload", "parameters": {"IV_INPUT": {"direction": "IMPORTING"}}},
            )

            self.assertEqual(response.status_code, 200)
            saved = json.loads((temp_path / "cache" / "callables" / "functions" / "Z_FUNCTION_UPLOAD.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["name"], "Z_FUNCTION_UPLOAD")
            self.assertEqual(saved["parameters"], {"IV_INPUT": {"direction": "IMPORTING"}})
            self.assertFalse((temp_path / "cache" / "callables" / "functions" / "TOTALLY_DIFFERENT.json").exists())
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_upload_method_metadata_uses_class_method_logical_name(self):
        temp_path = test_temp_path()
        temp_path.mkdir()
        try:
            client = metadata_upload_client(temp_path)

            response = upload_metadata(
                client,
                "method",
                "ignored.json",
                {
                    "class": "zcl_sender",
                    "method": "send",
                    "parameters": {"IV_TEXT": {"direction": "IMPORTING"}},
                    "returning": {"name": "RESULT", "direction": "RETURNING"},
                },
            )

            self.assertEqual(response.status_code, 200)
            saved = json.loads((temp_path / "cache" / "callables" / "methods" / "ZCL_SENDER%3D%3ESEND.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["class"], "ZCL_SENDER")
            self.assertEqual(saved["method"], "SEND")
            self.assertNotIn("name", saved)
            self.assertEqual(saved["parameters"], {"IV_TEXT": {"direction": "IMPORTING"}})
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_upload_rejects_invalid_json_and_missing_selected_type_properties(self):
        temp_path = test_temp_path()
        temp_path.mkdir()
        try:
            client = metadata_upload_client(temp_path)

            invalid = client.post(
                "/metadata-cache/upload",
                data={
                    "metadata_type": "function",
                    "metadata_file": (BytesIO(b"{"), "ignored.json"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(invalid.status_code, 400)
            self.assertIn(b"not valid JSON", invalid.data)

            missing = upload_metadata(client, "function", "ignored.json", {"parameters": {}})
            self.assertEqual(missing.status_code, 400)
            self.assertIn(b"missing required property: name", missing.data)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_upload_asks_before_overwriting_existing_cache_file(self):
        temp_path = test_temp_path()
        temp_path.mkdir()
        try:
            client = metadata_upload_client(temp_path)
            destination = temp_path / "cache" / "callables" / "functions" / "Z_EXISTING.json"
            destination.parent.mkdir(parents=True)
            destination.write_text(json.dumps({"name": "Z_EXISTING", "parameters": {"OLD": {}}}), encoding="utf-8")

            response = upload_metadata(
                client,
                "function",
                "ignored.json",
                {"name": "z_existing", "parameters": {"NEW": {}}},
            )

            self.assertEqual(response.status_code, 409)
            self.assertIn(b"already exists", response.data)
            self.assertIn(b"Overwrite", response.data)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["parameters"], {"OLD": {}})

            confirmed = client.post(
                "/metadata-cache/upload",
                data={
                    "metadata_type": "function",
                    "metadata_payload": json.dumps({"name": "z_existing", "parameters": {"NEW": {}}}),
                    "confirm_overwrite": "1",
                },
            )

            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["parameters"], {"NEW": {}})
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)


def metadata_upload_client(temp_path):
    app = create_app(
        {
            "TESTING": True,
            "SAP_METADATA_CACHE_DIR": str(temp_path / "cache"),
            "SAP_DDIC_CACHE_DIR": str(temp_path / "cache" / "ddic"),
            "SAP_CALLABLE_CACHE_DIR": str(temp_path / "cache" / "callables"),
        }
    )
    return app.test_client()


def upload_metadata(client, metadata_type, filename, payload):
    return client.post(
        "/metadata-cache/upload",
        data={
            "metadata_type": metadata_type,
            "metadata_file": (BytesIO(json.dumps(payload).encode("utf-8")), filename),
        },
        content_type="multipart/form-data",
    )


def test_temp_path():
    return Path(__file__).resolve().parents[1] / f".test_tmp_{uuid4().hex}"


if __name__ == "__main__":
    unittest.main()
