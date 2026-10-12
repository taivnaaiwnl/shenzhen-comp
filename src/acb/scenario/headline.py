"""``HEADLINE.md``: about 25 curated transaction hashes, one per judge-facing claim (spec 10.4).

The claims are listed in ``config/scenario.yaml`` (``headline``) as ``{step, function, claim}`` with
optional ``agent`` (the subject of a ``GradePosted``), ``nth`` (1-based among the matches) and ``status``.
:func:`mark_headline` flags the matching transaction records; :func:`render` writes the Markdown table,
marking each row that was signed by the wallet agent's own key.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _matches(run: Any, rec: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    if rec["step"] != spec["step"] or rec["function"] != spec["function"]:
        return False
    if "status" in spec and int(rec["status"]) != int(spec["status"]):
        return False
    if "status" not in spec and rec["status"] != rec["expectedStatus"]:
        return False
    agent = spec.get("agent")
    if agent:
        aid = str(run.env.dep.agents[agent].agent_id)
        ok = any(
            lg["event"] == "GradePosted"
            and str(lg["args"]["agentId"]) == aid
            and int(lg["args"]["grade"]) != int(lg["args"]["previousGrade"])
            for lg in rec["_events"]
        )
        if not ok:
            return False
    return True


def mark_headline(
    run: Any, records: Sequence[dict[str, Any]], specs: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Flag the records of the headline claims; returns ``[{claim, record}]`` (record None if missing)."""
    rows = []
    for spec in specs:
        matches = [r for r in records if _matches(run, r, spec)]
        nth = int(spec.get("nth", 1))
        rec = matches[nth - 1] if len(matches) >= nth else None
        if rec is not None:
            rec["headline"] = True
        rows.append({"claim": spec["claim"], "step": spec["step"], "record": rec})
    return rows


def render(evidence: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    """The Markdown text of ``HEADLINE.md``."""
    network = evidence["network"]
    contracts = evidence.get("contracts") or {}
    lines = [
        f"# Agent Credit Bureau - headline transactions ({network})",
        "",
        (
            f"Run `{evidence['runId']}` on chain {evidence['chainId']}, {evidence.get('startedAt')} to "
            f"{evidence.get('finishedAt')} (git {str(evidence.get('gitCommit') or 'unknown')[:12]})."
        ),
        "Each row is one judge-facing claim of the recorded run (spec 10.2) and the transaction that proves it.",
        '"Agent key" marks transactions signed by the wallet agent\'s own key (the signer process); the others are',
        "signed by the owner, the bureau, the autonomy operator, a provider or, for the stolen-key rows, by the runner",
        "holding a copy of the agent key outside the signer to show that the vault reverts the call.",
        "",
        "| # | Claim | Step | Function | Status | Agent key | Reasons | Transaction |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, row in enumerate(rows, start=1):
        rec = row.get("record")
        if rec is None:
            lines.append(f"| {i} | {row['claim']} | {row['step']} | - | missing | - | - | - |")
            continue
        explorer = rec.get("explorer") or {}
        link = next(iter(explorer.values()), None)
        tx = f"[`{rec['txHash']}`]({link})" if link else f"`{rec['txHash']}`"
        reasons = ", ".join(str(c) for c in rec.get("reasonCodes") or []) or "-"
        status = (
            "success" if rec["status"] == 1 else "reverted (expected)" if rec["expectedStatus"] == 0 else "reverted"
        )
        agent_key = "yes" if rec.get("signedByAgentKey") else "no"
        lines.append(
            f"| {i} | {row['claim']} | {rec['step']} | `{rec['function']}` | {status} | {agent_key} | {reasons} | {tx} |"
        )
    lines += [
        "",
        "## Contracts",
        "",
        "| Contract | Address |",
        "|---|---|",
    ]
    for name, address in contracts.items():
        if isinstance(address, dict):
            for vault, addr in address.items():
                lines.append(f"| TierVault ({vault}) | `{addr}` |")
        else:
            lines.append(f"| {name} | `{address}` |")
    lines += [
        "",
        "## Verify",
        "",
        "```",
        f"python scripts/verify_evidence.py evidence/{network}/evidence.json"
        + (" --receipts-only" if network == "anvil" else ""),
        "```",
        "",
        (
            f"All {len(evidence.get('transactions') or [])} transactions of the run are in `evidence.json` and "
            "`evidence.csv`; the bootstrap and simulator transactions are listed there as supporting evidence."
        ),
        "",
    ]
    return "\n".join(lines)


__all__ = ["mark_headline", "render"]
