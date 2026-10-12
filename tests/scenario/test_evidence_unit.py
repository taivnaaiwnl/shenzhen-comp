"""Evidence files without a chain: schema validation, CSV, subtitles, export, headline, classification."""

from __future__ import annotations

import csv
import io
import json
import shutil
import uuid
from pathlib import Path

import pytest

from acb import config
from acb.scenario import evidence as ev
from acb.scenario.attacks import Obs, classify, totals
from acb.scenario.headline import render
from acb.scenario.runner import load_scenario_config, strictness

H = "0x" + "ab" * 32


def tx(seq: int, **kw):
    base = {
        "seq": seq,
        "step": "S6",
        "timestampUtc": "2026-10-17T09:12:03Z",
        "blockTimestamp": 1792222333,
        "actor": "agent-signer",
        "actorAddress": "0x" + "11" * 20,
        "signedByAgentKey": True,
        "action": "pay Atlas Data 0.80 USDC",
        "contract": "TierVault(wallet)",
        "to": "0x" + "22" * 20,
        "function": "pay",
        "decision": "PAY",
        "reasonCodes": [2000],
        "tierBefore": 1,
        "tierAfter": 1,
        "chainId": 31337,
        "block": 10 + seq,
        "txHash": "0x" + f"{seq:064x}",
        "status": 1,
        "expectedStatus": 1,
        "expectedEvents": ["Paid"],
        "gasUsed": 51234,
        "explorer": {},
        "receiptFile": f"receipts/0x{seq:064x}.json",
        "logLine": "line",
        "headline": False,
        "artefacts": [],
        "notes": "",
    }
    base.update(kw)
    return base


def evidence(transactions):
    return {
        "schema": "acb-run/1",
        "runId": "r-test",
        "network": "anvil",
        "chainId": 31337,
        "startedAt": "2026-10-17T09:00:00Z",
        "finishedAt": "2026-10-17T09:10:00Z",
        "gitCommit": "uncommitted",
        "contracts": {"AgentCreditBureau": "0x" + "33" * 20, "vaults": {"wallet": "0x" + "22" * 20}},
        "erc8004": {},
        "llm": {},
        "steps": [{"id": "S6", "title": "t", "status": "PASSED", "startedAt": "x", "txCount": 1, "notes": ""}],
        "transactions": transactions,
        "offchainDecisions": [],
        "gradeChanges": [],
    }


def test_valid_evidence_has_no_problems():
    assert ev.validate_evidence(evidence([tx(1), tx(2)])) == []


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (lambda e: e.update(schema="acb-run/0"), "schema"),
        (lambda e: e["transactions"][0].update(txHash=H.upper()), "txHash"),
        (lambda e: e["transactions"][0].update(status=2), "status"),
        (lambda e: e["transactions"][0].update(seq=5), "numbered"),
        (lambda e: e["transactions"][0].pop("expectedEvents"), "expectedEvents"),
        (lambda e: e["transactions"][0].update(block=True), "block"),
        (lambda e: e.pop("gradeChanges"), "gradeChanges"),
    ],
)
def test_invalid_evidence_is_reported(mutate, needle):
    e = evidence([tx(1)])
    mutate(e)
    problems = ev.validate_evidence(e)
    assert any(needle in p for p in problems), problems


def test_validation_checks_referenced_files(tmp_path):
    e = evidence([tx(1, artefacts=["decisions/x.json"])])
    problems = ev.validate_evidence(e, tmp_path)
    assert any("receipts/" in p for p in problems) and any("decisions/x.json" in p for p in problems)


def test_csv_has_the_spec_columns_and_one_row_per_transaction():
    text = ev.render_csv([tx(1, reasonCodes=[2105, 2108, 2301]), tx(2)])
    rows = list(csv.reader(io.StringIO(text)))
    assert tuple(rows[0]) == ev.CSV_COLUMNS
    assert len(rows) == 3
    assert rows[1][ev.CSV_COLUMNS.index("reason_codes")] == "2105;2108;2301"
    assert "\r" not in text


def test_srt_timing_and_text():
    text = ev.render_srt(
        [
            {"step": "S6", "start": 1.5, "end": 3.25, "text": "pays"},
            {"step": "S7", "start": 3.3, "end": 3.3, "text": "x"},
        ]
    )
    assert "1\n00:00:01,500 --> 00:00:03,250\nS6: pays" in text
    assert "00:00:03,300 --> 00:00:04,300" in text  # at least one second on screen


def test_headline_marks_agent_key_and_expected_reverts():
    rows = [
        {"claim": "pay", "step": "S6", "record": tx(1, explorer={"basescan": "https://e/tx/1"})},
        {"claim": "stolen", "step": "S13", "record": tx(2, status=0, expectedStatus=0, step="S13", reasonCodes=[])},
        {"claim": "missing", "step": "S20", "record": None},
    ]
    text = render(evidence([tx(1), tx(2)]), rows)
    assert "| 1 | pay | S6 | `pay` | success | yes | 2000 | [`0x" in text
    assert "reverted (expected)" in text
    assert "| 3 | missing | S20 | - | missing |" in text
    assert all(ord(c) < 128 for c in text)


@pytest.fixture
def staged_run():
    run_id = f"r-test-{uuid.uuid4().hex[:8]}"
    root = config.REPO_ROOT / "runs" / run_id
    folder = root / "out" / "anvil" / "evidence"
    (folder / "receipts").mkdir(parents=True)
    t = tx(1)
    (folder / t["receiptFile"]).write_text("{}", encoding="utf-8")
    (folder / "evidence.json").write_text(json.dumps(evidence([t])), encoding="utf-8")
    (folder / "HEADLINE.md").write_text("# h\n", encoding="utf-8")
    yield run_id, folder
    shutil.rmtree(root, ignore_errors=True)


def test_export_copies_only_curated_files(staged_run, tmp_path):
    run_id, _folder = staged_run
    dest = tmp_path / "evidence" / "anvil"
    (dest / "screenshots").mkdir(parents=True)
    (dest / "screenshots" / "keep.png").write_bytes(b"png")
    (dest / "stale.txt").write_text("old", encoding="utf-8")
    result = ev.export(run_id, "anvil", dest=dest)
    names = sorted(p.name for p in dest.iterdir())
    assert names == ["HEADLINE.md", "evidence.json", "receipts", "screenshots"]
    assert (dest / "screenshots" / "keep.png").is_file()
    assert result["transactions"] == 1


def test_export_refuses_non_curated_staging(staged_run, tmp_path):
    run_id, folder = staged_run
    (folder / "signer.jsonl").write_text("{}", encoding="utf-8")
    with pytest.raises(ev.EvidenceError, match="non-curated"):
        ev.export(run_id, "anvil", dest=tmp_path / "out")


def test_export_refuses_invalid_evidence(staged_run, tmp_path):
    run_id, folder = staged_run
    bad = evidence([tx(1)])
    bad["schema"] = "nope"
    (folder / "evidence.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ev.EvidenceError, match="invalid"):
        ev.export(run_id, "anvil", dest=tmp_path / "out")


# ---------------------------------------------------------------------------
# attack classification (12.3): from observations only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields,category",
    [
        ({}, "Blocked"),
        ({"frozen": True}, "Contained"),
        ({"escalated": True}, "Escalated"),
        ({"bounded": {"withinBound": True}}, "Bounded"),
        ({"bounded": {"withinBound": False}}, "Failed"),
        ({"goal": True, "frozen": True}, "Failed"),
        ({"funds_lost": 1}, "Failed"),
        ({"secret_leaked": True, "escalated": True}, "Failed"),
        ({"error": "boom"}, "Error"),
    ],
)
def test_classify(fields, category):
    o = Obs("A00")
    for k, v in fields.items():
        setattr(o, k, v)
    assert classify(o) == category


def test_totals_count_categories_and_losses():
    t = totals(
        [
            {"actual": "Blocked", "fundsLost": "0"},
            {"actual": "Failed", "fundsLost": "5"},
            {"actual": "Error", "fundsLost": "0"},
        ]
    )
    assert t["blocked"] == 1 and t["failed"] == 1 and t["errors"] == 1 and t["fundsLostOutsideIntent"] == "5"


def test_strictness_order():
    order = ["PAY", "PAY_CAPPED", "ESCROW", "NEEDS_OWNER_APPROVAL", "REFUSE"]
    assert [strictness(o) for o in order] == sorted(strictness(o) for o in order)


def test_scenario_config_covers_every_step_attack_and_task():
    cfg = load_scenario_config()
    assert [s["id"] for s in cfg["steps"]] == [f"S{i}" for i in range(22)]
    assert [a["id"] for a in cfg["attacks"]] == [f"A{i:02d}" for i in range(1, 64)]
    assert [b["id"] for b in cfg["benign"]] == [f"B{i:02d}" for i in range(1, 15)]
    expected = {}
    for a in cfg["attacks"]:
        expected[a["expected"]] = expected.get(a["expected"], 0) + 1
    assert expected == {"Blocked": 54, "Contained": 4, "Bounded": 2, "Escalated": 3}
    assert 20 <= len(cfg["headline"]) <= 30


def test_curated_names_are_the_spec_list():
    assert {
        "evidence.json",
        "evidence.csv",
        "HEADLINE.md",
        "run.txt",
        "narration.srt",
        "receipts",
        "bundles",
    } <= ev.CURATED
    assert not any(name.endswith((".log", ".jsonl")) for name in ev.CURATED)


def test_evidence_folder_is_marked_binary_for_git():
    text = (config.REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "evidence/** -text" in text


def test_staging_path():
    assert ev.staging_folder("r-x", "anvil") == config.REPO_ROOT / "runs" / "r-x" / "out" / "anvil" / "evidence"
    assert isinstance(ev.staging_folder("r-x", "anvil"), Path)
