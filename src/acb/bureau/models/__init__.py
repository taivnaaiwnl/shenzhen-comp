"""Scorecard model modules, one per model id (spec 4.7 change control).

``acb-sc-1.0.0`` lives in :mod:`acb.bureau.models.sc_1_0_0`. A formula change is a new module
``sc_X_Y_Z.py`` and a new model id; old modules stay so old postings remain recomputable.
"""

from __future__ import annotations

import importlib
import re
from types import ModuleType

_MODEL_RE = re.compile(r"^acb-sc-(\d+)\.(\d+)\.(\d+)$")


def module_name(model_id: str) -> str:
    """``acb-sc-1.0.0`` -> ``acb.bureau.models.sc_1_0_0``."""
    m = _MODEL_RE.fullmatch(model_id)
    if not m:
        raise KeyError(f"unknown model id {model_id!r}")
    return f"acb.bureau.models.sc_{m.group(1)}_{m.group(2)}_{m.group(3)}"


def load_model(model_id: str) -> ModuleType:
    """The module implementing ``model_id`` (it exposes ``score(inputs, cfg, facts)``)."""
    try:
        module = importlib.import_module(module_name(model_id))
    except ImportError as exc:
        raise KeyError(f"model {model_id!r} is not available") from exc
    if getattr(module, "MODEL_ID", None) != model_id:
        raise KeyError(f"model module for {model_id!r} reports {getattr(module, 'MODEL_ID', None)!r}")
    return module


__all__ = ["load_model", "module_name"]
