"""Policy engine (WP5, spec 6): invoice validation V01-V09, ``decide`` with checks C01-C18, the violation
classifier of 5.2 and pure context builders.

Interfaces (spec 15.3): :func:`validate_invoice`, :func:`decide`, :func:`classify`,
:meth:`PolicyConfig.load`. Everything here is pure except :mod:`acb.policy.chainview` (chain reads).
"""

from __future__ import annotations

from acb.policy.config import PolicyConfig
from acb.policy.context import (
    build_payee_view,
    build_policy_context,
    build_validation_context,
    catalogue_from_deployments,
)
from acb.policy.engine import OwnerApproval, decide, decision_core, decision_hash
from acb.policy.invoice import new_quote_request, superseded_key, validate_invoice
from acb.policy.severity import classify, classify_tool_call

POLICY_VERSION = "acb-pol-1.0.0"

__all__ = [
    "POLICY_VERSION",
    "OwnerApproval",
    "PolicyConfig",
    "build_payee_view",
    "build_policy_context",
    "build_validation_context",
    "catalogue_from_deployments",
    "classify",
    "classify_tool_call",
    "decide",
    "decision_core",
    "decision_hash",
    "new_quote_request",
    "superseded_key",
    "validate_invoice",
]
