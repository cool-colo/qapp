"""Select the paired source worktrees for the pipe/proxy deployment."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def configure_runtime_paths(project_root: Path) -> None:
    parent = project_root.parent
    nautilus = Path(os.environ.get("NAUTILUS_TRADER_PATH", parent / "nautilus_trader")).resolve()
    client = Path(os.environ.get("BIGQMT_CLIENT_PATH", parent / "xtquant_big_convert")).resolve()
    if not (nautilus / "nautilus_trader" / "__init__.py").is_file():
        raise RuntimeError(f"Nautilus worktree is missing: {nautilus}")
    if not (client / "src" / "bigqmt_signal_trader" / "xtquant_compat.py").is_file():
        raise RuntimeError(f"BigQMT client worktree is missing: {client}")
    os.environ["NAUTILUS_TRADER_PATH"] = str(nautilus)
    os.environ["BIGQMT_CLIENT_PATH"] = str(client)
    for directory in (project_root, client / "src", nautilus):
        value = str(directory)
        if value in sys.path:
            sys.path.remove(value)
        sys.path.insert(0, value)
