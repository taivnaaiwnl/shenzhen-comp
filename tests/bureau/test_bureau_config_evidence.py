"""Scorecard configuration hash and archive (spec 4.7, 13.3), model loading, evidence files and the
counted-order invoice check (spec 4.1), endpoints, overrides and small helpers."""

from __future__ import annotations

import json

import pytest

from acb.bureau.config import ConfigError, archive_config, config_hash, find_config, load_config, load_raw, parse_config
from acb.bureau.evidence import EvidenceStore, bundle_uri, check_invoice, default_base_uri, posted_values, with_override
from acb.bureau.inputs import parse_endpoint, report_endpoint
from acb.bureau.models import load_model
from acb.bureau.poster import psi_x10000
from acb.bureau.recompute import json_diff
from acb.canonical import canonical_bytes, chash, invoice_hash, sign_invoice, write_canonical
from acb.keys import KeyRing
from acb.types import Bundle

KEY = KeyRing.anvil().account("atlas")
VAULT = "0x" + "aa" * 20


def test_config_hash_binds_file_and_anchors():
    raw = load_raw()
    cfg = load_config(["7"])
    assert cfg.config_hash == chash({"scorecard": raw, "anchors": ["7"]}) == config_hash(raw, [7])
    assert load_config([8]).config_hash != cfg.config_hash
    assert cfg.model_id == "acb-sc-1.0.0" and cfg.detector_id == "acb-inj-1.0.0" and cfg.profile == "demo"
    assert cfg.a_min_age_s == 600 and cfg.decay_start_s == 2_592_000 and cfg.decay_h_s == 7_776_000
    prod = cfg.with_profile("production")
    assert prod.a_min_age_s == 2_592_000 and prod.config_hash != cfg.config_hash


def test_spec_numbers_in_the_config():
    cfg = load_config([7])
    assert dict(cfg.positive_quality) == {"anchor": 1000, "A": 1000, "B": 800, "NEW": 250, "C": 200, "NONE": 0, "D": 0}
    assert dict(cfg.adverse_weight) == {"anchor": 1000, "A": 1000, "B": 800, "NEW": 0, "C": 0, "NONE": 0, "D": 0}
    assert (cfg.min_order_amount, cfg.ref_amount, cfg.fail_cap_per_payer) == (100_000, 200_000, 3000)
    assert (cfg.verified_weight, cfg.spam_weight, cfg.spam_min_invoices, cfg.reporter_cap_milli) == (1000, 500, 3, 1000)
    assert (cfg.band_a, cfg.band_b, cfg.band_c, cfg.base, cfg.clamp_lo, cfg.clamp_hi) == (700, 640, 560, 600, 300, 850)
    assert cfg.repost_min_score_delta == 10 and cfg.mirror_enabled and cfg.mirror_revoke_previous


def test_floats_are_rejected():
    raw = load_raw()
    raw["base"] = 600.5
    with pytest.raises(ConfigError):
        parse_config(raw, [7])


def test_archive_and_find(tmp_path, monkeypatch):
    monkeypatch.setenv("ACB_SCORECARD_ARCHIVE_DIR", str(tmp_path))
    current = load_config([7])
    raw = load_raw()
    raw["base"] = 601
    old = parse_config(raw, [7])
    path = archive_config(old, tmp_path)
    assert path.name == f"{old.config_hash}.json" and path.read_bytes() == canonical_bytes(old.combined())
    assert find_config(current.config_hash, current) is current
    found = find_config(old.config_hash, current)
    assert found is not None and found.base == 601 and found.anchors == (7,)
    data = json.loads(path.read_text())
    data["scorecard"]["base"] = 650  # tampered archive: its hash no longer matches its name
    path.write_text(json.dumps(data))
    assert find_config(old.config_hash, current) is None
    assert find_config("0x" + "12" * 32, current) is None


def test_model_registry():
    assert load_model("acb-sc-1.0.0").MODEL_ID == "acb-sc-1.0.0"
    for bad in ("acb-sc-9.9.9", "something"):
        with pytest.raises(KeyError):
            load_model(bad)


def signed(amount: str = "200000", vault: str = VAULT, issuer: str = "5", key=KEY) -> dict:
    body = {
        "schema": "acb-invoice/1",
        "invoiceId": "inv-atlas-0001",
        "quoteRequestId": "qr-" + "11" * 16,
        "capOf": "",
        "issuerAgentId": issuer,
        "buyerAgentId": "11",
        "buyerVault": vault,
        "chainId": 31337,
        "token": "0x" + "cc" * 20,
        "items": [{"sku": "fx-live-15m", "description": "FX", "unitPrice": amount, "quantity": 1}],
        "amount": amount,
        "issuedAt": 1,
        "expiresAt": 2,
        "deliveryDeadline": 2,
        "memo": "",
    }
    return sign_invoice(key, body)


def test_check_invoice_rules():
    inv = signed()
    h = invoice_hash(inv)
    wallet = KEY.address.lower()
    ok = {"invoice_hash_hex": h, "wallet": wallet, "vault": VAULT, "payee_agent_id": 5, "amount": 200_000}
    assert check_invoice(inv, **ok) == ""
    assert check_invoice(None, **ok) == "no_invoice"
    assert check_invoice(inv, **{**ok, "wallet": "0x" + "01" * 20}) == "bad_invoice"  # not the payee's wallet
    assert check_invoice(inv, **{**ok, "wallet": None}) == "bad_invoice"
    assert check_invoice(inv, **{**ok, "vault": "0x" + "bb" * 20}) == "bad_invoice"
    assert check_invoice(inv, **{**ok, "payee_agent_id": 6}) == "bad_invoice"
    assert check_invoice(inv, **{**ok, "amount": 300_000}) == "bad_invoice"
    assert check_invoice(inv, **{**ok, "invoice_hash_hex": "0x" + "00" * 32}) == "bad_invoice"
    noncanonical = {**inv, "amount": 200000}
    assert check_invoice(noncanonical, **ok) == "bad_invoice"
    other = signed(key=KeyRing.anvil().account("ghost"))
    assert check_invoice(other, **{**ok, "invoice_hash_hex": invoice_hash(other)}) == "bad_invoice"


def test_evidence_store_checks_hashes(tmp_path):
    inv = signed()
    h = invoice_hash(inv)
    first, second = tmp_path / "a", tmp_path / "b"
    write_canonical(first / "invoices" / f"{h}.json", {**inv, "memo": "swapped"})  # wrong content under the name
    write_canonical(second / "invoices" / f"{h}.json", inv)
    store = EvidenceStore([str(first), str(second)])
    assert store.invoice(h) == inv
    report = {"schema": "acb-report/1", "kind": "SPAM"}
    rh = chash(report)
    write_canonical(second / "reports" / f"{rh}.json", report)
    assert store.report(rh) == report
    assert store.report("0x" + "34" * 32) is None
    assert EvidenceStore([str(tmp_path / "missing")]).invoice(h) is None


def test_endpoint_parsing():
    oid = "0x" + "ab" * 32
    ep = report_endpoint(oid.upper().replace("0X", "0x"), 12, "0x" + "CD" * 20)
    assert ep == f"acb:v1;order={oid};reporter=12;vault=0x{'cd' * 20}"
    assert parse_endpoint(ep) == (oid, 12, "0x" + "cd" * 20)
    for bad in ("", "acb:v1;order=0x12;reporter=1;vault=0x" + "cd" * 20, "acb:v2;" + ep[7:], ep + ";x=1", 5):
        assert parse_endpoint(bad) == (None, None, None)


def minimal_bundle() -> Bundle:
    return Bundle(
        model={},
        chain={},
        contracts={},
        subject={"agentId": "1"},
        orders=(),
        reports=(),
        features={},
        points={},
        score=700,
        grade="A",
        reasons=(1006, 1007, 0, 0),
        rules={},
    )


def test_override_disclosure():
    b = minimal_bundle()
    assert posted_values(b) == ("A", 700, (1006, 1007, 0, 0))
    o = with_override(b, "C", "documented exception", "bureau-ops")
    assert o.override == {"grade": "C", "reason": "documented exception", "approvedBy": "bureau-ops"}
    assert posted_values(o) == ("C", 700, (1104, 1006, 1007, 0))
    assert o.evidence_hash != b.evidence_hash


def test_json_diff_and_psi():
    assert json_diff({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 3]}) == [
        {"path": "$.b[1]", "published": 2, "computed": 3}
    ]
    assert json_diff({"a": 1}, {"a": 1}) == []
    assert psi_x10000({"A": 10, "B": 10}, {"A": 10, "B": 10}) == 0
    assert psi_x10000({"A": 10}, {"D": 10}) > 2500


def test_evidence_base_uri_defaults():
    assert default_base_uri("anvil", {}) == "evidence/anvil/"
    assert default_base_uri("base_sepolia", {}) == (
        "https://raw.githubusercontent.com/taivnaaiwnl/shenzhen-comp/main/evidence/base_sepolia/"
    )
    assert default_base_uri("anvil", {"EVIDENCE_BASE_URI": "x/y"}) == "x/y/"
    assert bundle_uri("x/y/", 5, 40) == "x/y/bundles/5-40.json"


def test_bundle_reads_from_on_chain_uris_are_confined(tmp_path, monkeypatch):
    """An ``evidenceURI`` comes from the chain: only ``<agentId>-<asOfBlock>.json`` inside the evidence or
    output folders is read, never another local file."""
    from acb.bureau.evidence import read_bundle_source
    from acb.bureau.recompute import load_published_bundle

    out = tmp_path / "out"
    write_canonical(out / "bundles" / "7-42.json", {"ok": 1})
    secret = tmp_path / "secret.json"
    secret.write_text('{"secret": 1}', encoding="utf-8")
    write_canonical(tmp_path / "elsewhere" / "7-42.json", {"no": 1})
    monkeypatch.setenv("ACB_OUT_DIR", str(out))
    assert read_bundle_source(str(out / "bundles" / "7-42.json"), "7-42.json", [out]) == canonical_bytes({"ok": 1})
    assert read_bundle_source(str(secret), "7-42.json", [out, tmp_path]) is None  # wrong file name
    assert read_bundle_source(str(tmp_path / "elsewhere" / "7-42.json"), "7-42.json", [out]) is None  # outside
    traversal = str(out / "bundles" / ".." / ".." / "elsewhere" / "7-42.json")
    assert read_bundle_source(traversal, "7-42.json", [out]) is None
    assert load_published_bundle(str(secret), 7, 42) is None
    assert load_published_bundle(str(tmp_path / "elsewhere" / "7-42.json"), 7, 42) is None
    assert load_published_bundle(str(out / "bundles" / "7-42.json"), 7, 42) == {"ok": 1}
    assert load_published_bundle("https://example.invalid/x/7-41.json", 7, 42, (str(out),)) == {"ok": 1}


def test_first_posting_needs_listing_or_evidence():
    """Anyone can name payees in recordDecision / ownerApprove: an unlisted agent without a counted order
    or a bound report is scored but never posted (it would drain the bureau key)."""
    from types import SimpleNamespace

    from acb.bureau.poster import SKIP_NO_EVIDENCE, Bureau, has_evidence

    def bundle(orders=(), reports=()):
        return SimpleNamespace(orders=tuple(orders), reports=tuple(reports))

    assert not has_evidence(bundle())
    assert not has_evidence(bundle([{"counted": False}], [{"bound": False}]))
    assert has_evidence(bundle([{"counted": True}]))
    assert has_evidence(bundle(reports=[{"bound": True}]))
    b = Bureau.__new__(Bureau)
    b.listed = frozenset({5})
    snap = SimpleNamespace(last_posting=lambda aid: None)
    assert b._changed(snap, 5, bundle()) == "first posting"  # in deployments/<network>.json
    assert b._changed(snap, 900, bundle()) == SKIP_NO_EVIDENCE
    assert b._changed(snap, 900, bundle([{"counted": True}])) == "first posting"


def test_public_network_rescore_archives_into_the_repository(tmp_path, monkeypatch):
    """Spec 4.7: on a public network the combined config also goes to config/scorecard/archive, so a judge
    can recompute an old posting from the repository after scorecard.yaml changes; Anvil does not."""
    from types import SimpleNamespace

    from acb.bureau import poster

    repo_archive = tmp_path / "repo-archive"
    monkeypatch.setattr(poster, "ARCHIVE_DIR", repo_archive)
    cfg = load_config([7])
    for chain_id, expected in ((31337, False), (84532, True)):
        b = poster.Bureau.__new__(poster.Bureau)
        b.cfg, b.archive_dir = cfg, None
        b.out_dir = tmp_path / f"out-{chain_id}"
        b.net = SimpleNamespace(chain_id=chain_id)
        b.archive()
        assert (b.out_dir / "scorecard" / "archive" / f"{cfg.config_hash}.json").is_file()
        assert (repo_archive / f"{cfg.config_hash}.json").is_file() == expected
