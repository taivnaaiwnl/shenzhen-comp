"""Signer service P3 (WP5, spec 7): the only process that holds the wallet agent's key.

It re-validates every invoice and re-runs the policy on fresh chain state, classifies violations and
records them first, appends every decision to its log before any outflow, simulates before sending and
builds only the whitelisted vault and reputation calls. See :mod:`acb.signer.executor` and
:mod:`acb.signer.service`.
"""

from __future__ import annotations

from acb.signer.executor import WHITELIST, CallBuilder, Executor
from acb.signer.service import create_app, serve

__all__ = ["WHITELIST", "CallBuilder", "Executor", "create_app", "serve"]
