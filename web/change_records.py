"""File-backed storage for manually recorded significant changes.

Records live beside ``compare_presets.json`` so they survive dashboard restarts and
are shared by every browser using the dashboard. The list is stored newest first;
editing a record does not change its original position.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_STORE_PATH = Path(__file__).resolve().parent / "change_records.json"
_LOCK = threading.Lock()
_MAX_TITLE_LENGTH = 120
_MAX_DETAILS_LENGTH = 5_000


def _read() -> list[dict[str, Any]]:
    if not _STORE_PATH.exists():
        return []
    try:
        data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [record for record in data if isinstance(record, dict)]


def _write(records: list[dict[str, Any]]) -> None:
    tmp = _STORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STORE_PATH)


def _validated_text(title: str, details: str) -> tuple[str, str]:
    title = title.strip()
    details = details.strip()
    if not title:
        raise ValueError("title must not be empty")
    if not details:
        raise ValueError("details must not be empty")
    if len(title) > _MAX_TITLE_LENGTH:
        raise ValueError(f"title must be at most {_MAX_TITLE_LENGTH} characters")
    if len(details) > _MAX_DETAILS_LENGTH:
        raise ValueError(f"details must be at most {_MAX_DETAILS_LENGTH} characters")
    return title, details


def list_records() -> list[dict[str, Any]]:
    """Return a detached snapshot of all records, newest first."""
    with _LOCK:
        records = _read()
    return [dict(record) for record in records]


def add_record(title: str, details: str) -> dict[str, Any]:
    title, details = _validated_text(title, details)
    now = datetime.now().astimezone().isoformat(timespec="microseconds")
    record = {
        "id": uuid4().hex,
        "title": title,
        "details": details,
        "created_at": now,
        "updated_at": now,
    }
    with _LOCK:
        records = _read()
        records.insert(0, record)
        _write(records)
    return dict(record)


def update_record(record_id: str, title: str, details: str) -> dict[str, Any] | None:
    title, details = _validated_text(title, details)
    with _LOCK:
        records = _read()
        for record in records:
            if record.get("id") != record_id:
                continue
            record["title"] = title
            record["details"] = details
            record["updated_at"] = datetime.now().astimezone().isoformat(timespec="microseconds")
            _write(records)
            return dict(record)
    return None


def delete_record(record_id: str) -> bool:
    with _LOCK:
        records = _read()
        kept = [record for record in records if record.get("id") != record_id]
        if len(kept) == len(records):
            return False
        _write(kept)
    return True
