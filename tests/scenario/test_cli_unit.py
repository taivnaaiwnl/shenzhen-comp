"""Command-line guards and pure helpers of the runner (no chain)."""

from __future__ import annotations

import uuid

import pytest

from acb import config
from acb.cli import main
from acb.scenario.recorder import Recorder
from acb.scenario.resume import reset_run
from acb.scenario.runner import EventLog, StepCtx


def test_commands_are_mounted(capsys):
    with pytest.raises(SystemExit):  # argparse exits after printing the help
        main(["scenario", "run", "--help"])
    out = capsys.readouterr().out
    assert "--from" in out and "--run-id" in out and "--llm" in out


def test_resume_needs_an_existing_run(capsys):
    run_id = f"r-missing-{uuid.uuid4().hex[:6]}"
    assert main(["scenario", "run", "--network", "anvil", "--from", "S6", "--run-id", run_id]) == 2
    assert "needs an existing run" in capsys.readouterr().err
    assert not (config.REPO_ROOT / "runs" / run_id).exists()


def test_unknown_step_is_refused(capsys):
    assert main(["scenario", "run", "--network", "anvil", "--to", "S99"]) == 2


def test_attacks_refuse_a_public_network(capsys):
    assert main(["attacks", "run", "--network", "base_sepolia"]) == 2


def test_evidence_verify_on_anvil_needs_receipts_only_or_rpc(capsys, tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text("{}", encoding="utf-8")
    assert main(["evidence", "verify", str(path), "--network", "anvil"]) == 2


def test_reset_without_state_files_stops_nothing():
    assert reset_run(f"r-missing-{uuid.uuid4().hex[:6]}")[0].endswith("no state file")


def test_event_log_tails_complete_lines_only(tmp_path):
    path = tmp_path / "agent.jsonl"
    log = EventLog(path)
    assert log.poll() == []
    path.write_text('{"seq": 1}\n{"seq": 2', encoding="utf-8")
    assert [e["seq"] for e in log.poll()] == [1]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("}\n")
    assert [e["seq"] for e in log.poll()] == [2]
    assert log.mark() == 2 and log.since(1) == [{"seq": 2}]


def test_step_ctx_collects_failures_and_notes():
    ctx = StepCtx(id="S6", title="t", expect={})
    ctx.require(True, "never")
    ctx.require(False, "failed check")
    ctx.note("a note")
    assert ctx.failures == ["failed check"] and ctx.notes == ["a note"]


def _log(event: str, **args):
    return {"event": event, "args": args, "address": "0x" + "22" * 20}


@pytest.mark.parametrize(
    "function,events,signer,status,expected",
    [
        ("pay", [_log("Paid", decision=1, reasonCode=2000)], None, 1, ("PAY", [2000])),
        ("pay", [], {"outcome": "PAY_CAPPED", "reasons": [2002]}, 1, ("PAY_CAPPED", [2002])),
        ("recordDecision", [_log("DecisionRecorded", decision=4, reasonCode=2100)], None, 1, ("REFUSE", [2100])),
        ("refund", [_log("EscrowRefunded", reasonCode=2400)], None, 1, ("REFUND", [2400])),
        ("release", [_log("EscrowReleased")], None, 1, ("RELEASE", [2008])),
        ("recordDelivery", [_log("DeliveryRecorded", outcome=2)], None, 1, ("DELIVERY_LATE", [])),
        (
            "recordViolation",
            [
                _log("ViolationRecorded", kind=6, severity=3),
                _log("TierChanged", fromTier=2, toTier=255, reasonCode=3200),
            ],
            None,
            1,
            ("VIOLATION_INJECTION_FOLLOWED", [3200]),
        ),
        ("setTier", [_log("TierChanged", fromTier=1, toTier=2, reasonCode=3000)], None, 1, ("TIER_1_TO_2", [3000])),
        ("postGrade", [_log("GradePosted", grade=2, reasons=[1002, 1001, 0, 0])], None, 1, ("GRADE_D", [1002, 1001])),
        ("resolveAppeal", [_log("AppealResolved", upheld=False)], None, 1, ("APPEAL_REJECTED", [1103])),
        ("giveFeedback", [_log("NewFeedback", tag1="acb-report", tag2="SPAM")], None, 1, ("REPORT_SPAM", [])),
        ("pay", [], None, 0, (None, [])),
    ],
)
def test_recorder_decision_from_events(function, events, signer, status, expected):
    assert Recorder._decision(function, events, signer, status) == expected


def test_benign_suite_is_mounted(capsys):
    with pytest.raises(SystemExit):
        main(["attacks", "benign", "--help"])
    assert "--export" in capsys.readouterr().out
    assert main(["attacks", "benign", "--network", "base_sepolia"]) == 2


def test_s3_expects_the_scaled_funding_plan(monkeypatch):
    from acb.chain.deploy import default_funding_plan
    from acb.config import load_network
    from acb.scenario.runner import load_scenario_config
    from acb.scenario.steps import expected_vault_usdc

    nominal = next(s for s in load_scenario_config()["steps"] if s["id"] == "S3")["expect"]["usdc_vaults"]
    net = load_network("anvil")
    monkeypatch.delenv("ACB_FUNDING_USDC_BPS", raising=False)
    assert expected_vault_usdc(nominal, default_funding_plan(net).usdc_to_vaults) == {
        "wallet": 15_000_000,
        "beacon": 6_000_000,
    }
    monkeypatch.setenv("ACB_FUNDING_USDC_BPS", "9500")
    scaled = expected_vault_usdc(nominal, default_funding_plan(net).usdc_to_vaults)
    assert scaled == {"wallet": 14_250_000, "beacon": 5_700_000}
    assert sum(scaled.values()) <= 20_000_000  # the 20 test USDC of the Base Sepolia deployer


def test_benign_export_adds_the_totals_to_the_attack_suite(monkeypatch, tmp_path):
    import json

    from acb.scenario import benign

    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "attack_suite.json").write_text(json.dumps({"totals": {"failed": 0}}), encoding="utf-8")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    report = {
        "schema": "acb-benign/1",
        "runId": "r-test",
        "runAt": "2026-10-12T00:00:00Z",
        "results": [{"id": "B01", "falsePositive": False}, {"id": "B02", "falsePositive": True}],
        "totals": {"tasks": 2, "falsePositives": 1, "target": "<= 1"},
        "path": "x",
        "log": ["x"],
    }
    benign.export_benign(report)
    suite = json.loads((tmp_path / "evidence" / "attack_suite.json").read_text(encoding="utf-8"))
    assert suite["benign"]["falsePositives"] == 1 and suite["benign"]["falsePositiveIds"] == ["B02"]
    assert suite["benign"]["file"] == "benign_suite.json"
    exported = json.loads((tmp_path / "evidence" / "benign_suite.json").read_text(encoding="utf-8"))
    assert "path" not in exported and "log" not in exported
    assert benign.exported_benign_summary()["tasks"] == 2
