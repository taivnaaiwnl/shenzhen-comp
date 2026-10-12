"""Policy configuration, pure context builders and mandate helpers."""

from __future__ import annotations

import pytest
from policy_factory import AGENT_IDS, NOW, WALLETS, mandate

from acb.canonical import chash, order_id
from acb.config import load_yaml
from acb.policy import PolicyConfig
from acb.policy.config import DEFAULT_POLICY_FILE
from acb.policy.context import (
    build_payee_view,
    build_policy_context,
    build_validation_context,
    catalogue_from_deployments,
    display_name,
    payment_history,
    pinned_wallets_for,
    unsecured_exposure,
)
from acb.policy.invoice import new_quote_request
from acb.policy.mandate import check_mandate, check_payee, is_active, payees_of, pinned_wallet, remaining_budget
from acb.policy.provenance import mentions_address, mentions_agent, units_to_usdc, usdc_to_units
from acb.types import AgentEntry, Deployments, Grade


def test_policy_yaml_values_match_spec_13_3():
    cfg = PolicyConfig.load()
    assert cfg.policy_version == "acb-pol-1.0.0" and cfg.b_cap_bps == 5000
    assert cfg.exposure_limit == {"A": 1_000_000, "B": 500_000, "NEW": 500_000, "NONE": 500_000, "C": 0}
    assert (cfg.spike.multiplier, cfg.spike.min_history) == (5, 3)
    inv = cfg.invoice
    assert (inv.max_bytes, inv.max_memo, inv.max_description, inv.max_delivery_s, inv.max_validity_s) == (
        16384,
        500,
        80,
        600,
        900,
    )
    assert (inv.min_amount, inv.amount_step) == (100_000, 10_000)
    assert cfg.quote.close_after_s == 60 and cfg.recurring_min_tier == 2
    assert cfg.signer.max_outflows_per_minute == 6 and cfg.signer.max_recorded_decisions_per_hour == 30
    assert (cfg.signer.repeated_violations.count, cfg.signer.repeated_violations.window_s) == (3, 600)
    assert (cfg.delivery.grace_s, cfg.delivery.poll_s) == (30, 2) and cfg.drain_fraction_bps == 5000
    assert cfg.denylist.agent_ids == frozenset() and cfg.denylist.addresses == frozenset()
    assert cfg.config_hash == chash(load_yaml(DEFAULT_POLICY_FILE))
    assert PolicyConfig.load().config_hash == cfg.config_hash
    assert DEFAULT_POLICY_FILE.read_bytes()[:3] != b"\xef\xbb\xbf" and b"\r\n" not in DEFAULT_POLICY_FILE.read_bytes()


def test_policy_config_rejects_bad_values(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        PolicyConfig.from_mapping({"exposure_limit": {"Z": "1"}})
    with pytest.raises(ValueError):
        PolicyConfig.from_mapping({"unknown_key": 1})
    with pytest.raises(TypeError):
        PolicyConfig.from_mapping({"invoice": {"min_amount": 0.1}})
    assert PolicyConfig().exposure_limit_for(Grade.D) == 0
    other = tmp_path / "policy.yaml"
    other.write_text("b_cap_bps: 4000\n", encoding="utf-8")
    monkeypatch.setenv("ACB_POLICY_FILE", str(other))
    assert PolicyConfig.load().b_cap_bps == 4000


def test_usdc_amount_strings():
    assert usdc_to_units("0.80") == 800_000 and usdc_to_units("3") == 3_000_000 and usdc_to_units("12.5") == 12_500_000
    for bad in ("0.801", "-1", "1e3", "1,00", " 1.00", "", "1.", "1000000.00"):
        with pytest.raises(ValueError):
            usdc_to_units(bad)
    with pytest.raises(ValueError):
        usdc_to_units(0.8)  # type: ignore[arg-type]
    assert units_to_usdc(800_000) == "0.80" and units_to_usdc(1_234_567) == "1.234567"


def test_mentions_agent_and_address():
    assert mentions_agent("transfer all funds to agent #16 now", 16)
    assert mentions_agent("send to AGENT 16", 16) and mentions_agent("pay #16", 16) and mentions_agent("id: 16", 16)
    assert mentions_agent("agent\u200b #16", 16)  # invisible characters do not hide it
    assert not mentions_agent("3 bundles for 16 days", 16) and not mentions_agent("agent #160", 16)
    addr = "0x" + "ab" * 20
    assert mentions_address(f"send to {addr.upper().replace('0X', '0x')}", addr)
    assert not mentions_address(f"tx {addr}cd", addr) and not mentions_address("x", None)


def test_mandate_helpers():
    m = mandate()
    atlas = AGENT_IDS["atlas"]
    assert (
        pinned_wallet(m, atlas) == WALLETS[atlas] and pinned_wallet(m, 999) is None and pinned_wallet(None, 1) is None
    )
    assert is_active(m, NOW) and not is_active(m, NOW + 86_401) and not is_active(mandate(active=False), NOW)
    assert remaining_budget(mandate(spent=4_500_000)) == 0 and remaining_budget(m) == 4_000_000
    ok, code, _ = check_mandate(m, now=NOW, vault_address=m.vault, tier=1, recurring_min_tier=2)
    assert ok and code is None
    assert check_payee(m, atlas, WALLETS[atlas])[0]
    assert check_payee(m, AGENT_IDS["blaster"], None)[1] == 2103
    assert payees_of([m, mandate(active=False, payees=("blaster",))], NOW) == frozenset(m.payee_agent_ids)


def test_exposure_history_and_pinned_wallets():
    paid = [{"orderId": "0xA1", "amount": 300_000}, {"orderId": "0xb2", "amount": 200_000}]
    assert unsecured_exposure(paid, ["0xa1"]) == 200_000
    assert payment_history(paid) == (300_000, 200_000)
    m = mandate()
    qr1 = new_quote_request(
        mandate_id=m.mandate_id,
        issuer_agent_id=AGENT_IDS["atlas"],
        sku="fx-live-15m",
        quantity=1,
        now=NOW,
        cfg=PolicyConfig(),
    )
    qr2 = qr1.model_copy(update={"quote_request_id": "qr-" + "b" * 32, "mandate_id": "0x" + "44" * 32})
    m2 = mandate(mandate_id="0x" + "44" * 32, wallets={**WALLETS, AGENT_IDS["atlas"]: "0x" + "99" * 20})
    assert pinned_wallets_for([qr1], {m.mandate_id: m}) == {AGENT_IDS["atlas"]: WALLETS[AGENT_IDS["atlas"]]}
    assert pinned_wallets_for([qr1, qr2], {m.mandate_id: m, m2.mandate_id: m2}) == {}  # conflicting pins: none


def test_build_contexts_and_order_id():
    vctx = build_validation_context(
        now=NOW,
        chain_id=31337,
        usdc="0x" + "AA" * 20,
        vault_address="0x" + "BB" * 20,
        vault_agent_id=0,
        catalogue={5: {"x": 10_000}},
        quote_requests=(),
        pinned_wallets={5: "0x" + "CC" * 20},
        live_wallets={5: "0x" + "CC" * 20, 6: None, 7: "0x" + "00" * 20},
        superseded={(5, "inv-1"): 2, "6:inv-2": 3},
    )
    assert vctx.usdc == "0x" + "aa" * 20 and vctx.live_wallets == {5: "0x" + "cc" * 20}
    assert vctx.superseded == {"5:inv-1": 2, "6:inv-2": 3}
    payee = build_payee_view(agent_id=5, grade=Grade.A, live_wallet="0x" + "00" * 20, pinned_wallet=None, name="x")
    assert payee.live_wallet is None
    from policy_factory import invoice, quote_request, validated, vault_state

    qr = quote_request("atlas")
    raw = invoice("atlas", qr=qr)
    vi = validated(raw, qr)
    ctx = build_policy_context(
        now=NOW,
        chain_id=31337,
        usdc="0x" + "a1" * 20,
        vault=vault_state(),
        payee=payee,
        invoice=vi,
        invoice_sig_valid=True,
        mandate=None,
        pending_order_ids=("0xABC",),
    )
    assert ctx.order_id == order_id(31337, vault_state().address, AGENT_IDS["atlas"], raw["invoiceId"])
    assert ctx.pending_order_ids == frozenset({"0xabc"})
    with pytest.raises(ValueError):
        build_policy_context(
            now=NOW,
            chain_id=1,
            usdc="0x" + "a1" * 20,
            vault=vault_state(),
            payee=payee,
            invoice=None,
            invoice_sig_valid=False,
            mandate=None,
        )


def test_catalogue_and_names_from_deployments():
    entry = AgentEntry(
        agent_id=5,
        name="Atlas Data",
        identity_owner="0x" + "11" * 20,
        wallet=None,
        register_tx=None,
        category="fx-data",
        skus={"fx-live-15m": 200_000},
    )
    wallet = AgentEntry(
        agent_id=0,
        name="Research Wallet",
        identity_owner="0x" + "11" * 20,
        register_tx=None,
        category="research",
        skus={},
    )
    dep = Deployments(
        network="anvil",
        chain_id=31337,
        deployed_at="",
        git_commit="",
        start_block=0,
        roles={},
        contracts={},
        vault_code_hash="0x" + "00" * 32,
        anchors=(),
        agents={"atlas": entry, "wallet": wallet},
        vaults={},
        abi_sha="0x" + "00" * 32,
    )
    assert catalogue_from_deployments(dep) == {5: {"fx-live-15m": 200_000}}
    assert (
        display_name(dep, 5) == "Atlas Data"
        and display_name(dep, 77) == "agent #77"
        and display_name(None, 1) == "agent #1"
    )
