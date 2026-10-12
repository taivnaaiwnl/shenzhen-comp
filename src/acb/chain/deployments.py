"""``deployments/<network>.json`` I/O (spec 13.5).

The file is written atomically (temp file + ``os.replace``) with LF endings and no BOM. Its folder is
``deployments/`` in the repository, or ``ACB_DEPLOYMENTS_DIR`` when set (tests and parallel runs,
changes.md CR-1). ``deployments/anvil.json`` is git-ignored; ``deployments/base_sepolia.json`` is
committed after the recorded run.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from acb.canonical import atomic_write_bytes
from acb.config import REPO_ROOT
from acb.types import Deployments

DEPLOYMENTS_DIR_ENV = "ACB_DEPLOYMENTS_DIR"
DEFAULT_DEPLOYMENTS_DIR = REPO_ROOT / "deployments"


def deployments_dir() -> Path:
    """``ACB_DEPLOYMENTS_DIR`` if set, else ``<repo>/deployments``."""
    override = os.environ.get(DEPLOYMENTS_DIR_ENV)
    return Path(override) if override else DEFAULT_DEPLOYMENTS_DIR


def deployments_path(network: str) -> Path:
    """Path of ``<deployments dir>/<network>.json``."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", network):
        raise ValueError("invalid network name")
    return deployments_dir() / f"{network}.json"


def load_deployments(network: str, path: str | os.PathLike[str] | None = None) -> Deployments:
    """Read and validate ``deployments/<network>.json``. Raises ``FileNotFoundError`` if missing."""
    target = Path(path) if path is not None else deployments_path(network)
    raw = json.loads(target.read_text(encoding="utf-8-sig"))
    dep = Deployments.from_json_dict(raw)
    if dep.network != network:
        raise ValueError(f"{target} is for network {dep.network!r}, not {network!r}")
    return dep


def try_load_deployments(network: str, path: str | os.PathLike[str] | None = None) -> Deployments | None:
    """Like :func:`load_deployments` but None when the file is missing or invalid."""
    try:
        return load_deployments(network, path)
    except (FileNotFoundError, ValueError):
        return None


def dumps_deployments(dep: Deployments) -> bytes:
    """The file bytes: the 13.5 layout, two-space indent, ASCII, LF, trailing newline."""
    text = json.dumps(dep.to_json_dict(), indent=2, ensure_ascii=True, allow_nan=False)
    return (text + "\n").encode("utf-8")


def save_deployments(dep: Deployments, path: str | os.PathLike[str] | None = None) -> Path:
    """Write ``dep`` atomically to ``deployments/<network>.json`` (or ``path``); returns the path."""
    target = Path(path) if path is not None else deployments_path(dep.network)
    return atomic_write_bytes(target, dumps_deployments(dep))


__all__ = [
    "DEFAULT_DEPLOYMENTS_DIR",
    "DEPLOYMENTS_DIR_ENV",
    "deployments_dir",
    "deployments_path",
    "dumps_deployments",
    "load_deployments",
    "save_deployments",
    "try_load_deployments",
]
