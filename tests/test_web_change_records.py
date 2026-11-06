from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from web import change_records
from web import app as web_app
from web.app import ChangeRecordDelete


class ChangeRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "change_records.json"
        self.path_patch = patch.object(change_records, "_STORE_PATH", self.store_path)
        self.path_patch.start()

    def tearDown(self) -> None:
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_add_lists_newest_first_and_persists_next_to_selected_store(self) -> None:
        first = change_records.add_record("First", "Initial deployment")
        second = change_records.add_record("Second", "Parameter adjustment")

        records = change_records.list_records()

        self.assertEqual([second["id"], first["id"]], [record["id"] for record in records])
        persisted = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual([second["id"], first["id"]], [record["id"] for record in persisted])

    def test_update_keeps_original_order(self) -> None:
        first = change_records.add_record("First", "Initial deployment")
        second = change_records.add_record("Second", "Parameter adjustment")

        updated = change_records.update_record(first["id"], "First revised", "More details")

        self.assertIsNotNone(updated)
        records = change_records.list_records()
        self.assertEqual([second["id"], first["id"]], [record["id"] for record in records])
        self.assertEqual("First revised", records[1]["title"])
        self.assertNotEqual(records[1]["created_at"], records[1]["updated_at"])

    def test_delete_and_missing_ids(self) -> None:
        record = change_records.add_record("Change", "Details")

        self.assertFalse(change_records.delete_record("missing"))
        self.assertTrue(change_records.delete_record(record["id"]))
        self.assertEqual([], change_records.list_records())
        self.assertIsNone(change_records.update_record("missing", "Title", "Details"))

    def test_rejects_empty_or_oversized_content(self) -> None:
        with self.assertRaisesRegex(ValueError, "title must not be empty"):
            change_records.add_record(" ", "Details")
        with self.assertRaisesRegex(ValueError, "details must not be empty"):
            change_records.add_record("Title", " ")
        with self.assertRaisesRegex(ValueError, "title must be at most"):
            change_records.add_record("x" * 121, "Details")

    def test_delete_pin_must_be_configured_and_match(self) -> None:
        with patch.object(web_app, "_config", return_value=SimpleNamespace(control_pin=None)):
            with self.assertRaises(HTTPException) as missing_config:
                web_app._require_change_record_delete_pin("1234")
        self.assertEqual(403, missing_config.exception.status_code)

        with patch.object(web_app, "_config", return_value=SimpleNamespace(control_pin="9527")):
            with self.assertRaises(HTTPException) as wrong_pin:
                web_app._require_change_record_delete_pin("1234")
            web_app._require_change_record_delete_pin("9527")
        self.assertEqual(403, wrong_pin.exception.status_code)

    def test_delete_endpoint_checks_pin_before_removing_record(self) -> None:
        record = change_records.add_record("Protected", "Must keep history")
        with patch.object(web_app, "_config", return_value=SimpleNamespace(control_pin="9527")):
            with self.assertRaises(HTTPException):
                web_app.delete_change_record(record["id"], ChangeRecordDelete(pin="0000"))
            self.assertEqual(1, len(change_records.list_records()))
            self.assertEqual(
                {"ok": True},
                web_app.delete_change_record(record["id"], ChangeRecordDelete(pin="9527")),
            )
        self.assertEqual([], change_records.list_records())


if __name__ == "__main__":
    unittest.main()
