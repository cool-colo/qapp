"""Server-side persistence for saved 对比 (comparison) presets.

A preset is a named list of series descriptors the frontend builds. Stored as a JSON
file next to the config so presets survive restarts and are shared across browsers.
This is a single-user local tool, so a plain file with a process-wide lock is enough.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_STORE_PATH = Path(__file__).resolve().parent / "compare_presets.json"
_LOCK = threading.Lock()


def _read() -> dict[str, Any]:
    if not _STORE_PATH.exists():
        return {}
    try:
        data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict[str, Any]) -> None:
    tmp = _STORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STORE_PATH)


def list_presets() -> list[dict[str, Any]]:
    """Return presets as [{name, series}], sorted by name."""
    with _LOCK:
        data = _read()
    return [{"name": name, "series": series} for name, series in sorted(data.items())]


def save_preset(name: str, series: list[dict[str, Any]]) -> None:
    name = name.strip()
    if not name:
        raise ValueError("preset name must not be empty")
    if not isinstance(series, list) or not series:
        raise ValueError("preset must contain at least one series")
    with _LOCK:
        data = _read()
        data[name] = series
        _write(data)


def delete_preset(name: str) -> bool:
    with _LOCK:
        data = _read()
        existed = name in data
        if existed:
            del data[name]
            _write(data)
    return existed
