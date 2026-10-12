"""The signer builds only the calls of the spec 7.3 whitelist (no chain needed).

1. Every builder of :class:`CallBuilder` produces a function in the whitelist, aimed at our vault or the
   reputation registry; anything else is refused at run time.
2. A walk over the signer's source finds no vault or registry function outside the whitelist, no raw
   ``.functions`` access, no raw transaction signing and exactly one place that sends.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from web3 import Web3

import acb.signer
from acb.chain.contracts import DecisionParams, OutflowParams, ReputationContract, VaultContract
from acb.signer.executor import WHITELIST, CallBuilder, NotWhitelisted

SPEC_WHITELIST = {
    ("TierVault", f)
    for f in (
        "pay",
        "escrow",
        "release",
        "refund",
        "executeApproved",
        "recordDecision",
        "recordDelivery",
        "recordViolation",
        "freeze",
    )
} | {("ReputationRegistry", "giveFeedback")}

SIGNER_DIR = Path(acb.signer.__file__).parent
H = "0x" + "11" * 32
VAULT = "0x" + "aa" * 20
REPUTATION = "0x" + "bb" * 20

# Everything a stolen or confused signer must never be able to build (snake_case wrappers and ABI names).
FORBIDDEN = {
    "set_tier",
    "setTier",
    "unfreeze",
    "withdraw",
    "set_agent",
    "setAgent",
    "register_mandate",
    "registerMandate",
    "revoke_mandate",
    "revokeMandate",
    "owner_approve",
    "ownerApprove",
    "revoke_approval",
    "revokeApproval",
    "set_tier_config",
    "setTierConfig",
    "set_reserve_floor",
    "set_min_clean_amount",
    "set_max_auto_tier",
    "setMaxAutoTier",
    "set_promotion_cooldown",
    "set_max_grade_age",
    "set_window_seconds",
    "set_dispute_window",
    "set_autonomy_operator",
    "transfer_ownership",
    "transferOwnership",
    "accept_ownership",
    "claim_delivered",
    "post_grade",
    "postGrade",
    "set_poster",
    "register_vault",
    "revoke_feedback",
    "append_response",
    "transfer",
    "transfer_from",
    "approve",
    "mint",
    "send_raw_transaction",
    "sign_transaction",
    "build_transaction",
    "transact",
    "functions",
    "fn",
}


def _builders() -> CallBuilder:
    w3 = Web3()
    return CallBuilder(VaultContract(w3, VAULT), ReputationContract(w3, REPUTATION))


def _sample_calls(calls: CallBuilder):
    outflow = OutflowParams(H, 5, 400_000, 1_800_000_000, H, H, H, 1, 2000)
    decision = DecisionParams(H, 5, 400_000, 4, 2100, H, H)
    return {
        "pay": calls.pay(outflow),
        "escrow": calls.escrow(outflow),
        "release": calls.release(H),
        "refund": calls.refund(H, 2401),
        "execute_approved": calls.execute_approved(H, H, H),
        "record_decision": calls.record_decision(decision),
        "record_delivery": calls.record_delivery(H, 1, H),
        "record_violation": calls.record_violation(4, 2, H),
        "freeze": calls.freeze(3203, H),
        "give_feedback": calls.give_feedback(5, "SPAM", "acb:v1", "uri", H),
    }


def test_whitelist_is_exactly_the_spec_list():
    assert set(WHITELIST) == SPEC_WHITELIST


def test_every_builder_produces_a_whitelisted_call_on_the_right_contract():
    calls = _builders()
    built = _sample_calls(calls)
    assert set(built) == set(CallBuilder.BUILDERS)
    public = {n for n, _ in inspect.getmembers(CallBuilder, inspect.isfunction) if not n.startswith("_")}
    assert public - {"check"} == set(CallBuilder.BUILDERS)  # no other builder exists
    for name, fn in built.items():
        contract = "ReputationRegistry" if fn.fn_name == "giveFeedback" else "TierVault"
        assert (contract, fn.fn_name) in WHITELIST, name
        target = REPUTATION if contract == "ReputationRegistry" else VAULT
        assert fn.address.lower() == target


def test_non_whitelisted_calls_are_refused_at_run_time():
    calls = _builders()
    with pytest.raises(NotWhitelisted):
        calls.check(calls.vault.set_tier(3, 3000, H), "TierVault")
    with pytest.raises(NotWhitelisted):
        calls.check(calls.vault.withdraw("0x" + "cc" * 20, 1), "TierVault")
    with pytest.raises(NotWhitelisted):
        calls.check(calls.vault.unfreeze(1, 3300, None), "TierVault")
    other = VaultContract(Web3(), "0x" + "dd" * 20)
    with pytest.raises(NotWhitelisted):  # a whitelisted name on another contract
        calls.check(other.pay(OutflowParams(H, 5, 1, 1, H, H, H, 1, 2000)), "TierVault")
    with pytest.raises(NotWhitelisted):
        calls.check(calls.reputation.revoke_feedback(5, 1), "ReputationRegistry")


def _modules():
    return sorted(SIGNER_DIR.glob("*.py"))


def test_walk_of_the_signer_source_finds_no_call_outside_the_whitelist():
    seen_attrs: dict[str, set[str]] = {}
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        seen_attrs[path.name] = attrs
        bad = attrs & FORBIDDEN
        assert not bad, f"{path.name} uses {sorted(bad)}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in {"setTier", "unfreeze", "withdraw", "setAgent", "registerMandate"}, path.name
    # the builders only touch whitelisted vault / reputation wrappers
    tree = ast.parse((SIGNER_DIR / "executor.py").read_text(encoding="utf-8"))
    builder = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "CallBuilder")
    wrapper_calls = set()
    for node in ast.walk(builder):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr in ("vault", "reputation")
        ):
            wrapper_calls.add(node.func.attr)
    assert wrapper_calls == {
        "pay",
        "escrow",
        "release",
        "refund",
        "execute_approved",
        "record_decision",
        "record_delivery",
        "record_violation",
        "freeze",
        "give_feedback",
    }


def test_only_one_place_sends_and_it_checks_the_whitelist():
    sends = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "send"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "sender"
                ):
                    sends.append((path.name, fn.name))
    assert sends == [("executor.py", "_send")]
    src = inspect.getsource(acb.signer.executor.Executor._send)
    assert "self.calls.check(" in src
    # every outflow builder use goes through the executor's call builder object
    exec_src = (SIGNER_DIR / "executor.py").read_text(encoding="utf-8")
    assert "self.vault.pay(" not in exec_src.split("class Executor")[1]
