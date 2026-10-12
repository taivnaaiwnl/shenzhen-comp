"""The signer's decision log ``data/<network>/signer/decisions.jsonl`` (spec 7.3 step 6, 7.5, 5.4 rule 2).

Every decision is appended **before** any transaction that carries its ``decisionHash`` is sent, so the
autonomy operator can match each ``Paid`` / ``EscrowOpened`` event to a signer decision
(``GET /v1/decisions/{hash}``). When the transaction is mined a second line adds its hash. The file is
append-only; the in-memory index is rebuilt from it at start.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from acb.logbus import utc_now_iso


class DecisionLog:
    """Append-only JSONL with an index by decision hash."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, Any]] = {}
        if self.path.is_file():
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._apply(json.loads(line))
                    except (ValueError, KeyError, TypeError):
                        continue

    def _apply(self, entry: dict[str, Any]) -> None:
        h = entry["decisionHash"]
        if entry.get("type") == "tx":
            if h in self._index:
                self._index[h]["txHash"] = entry["txHash"]
            return
        self._index.setdefault(h, {**entry, "txHash": None})

    def _write(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def append(
        self,
        *,
        decision_hash: str,
        request_id: str,
        kind: str,
        order_id: str | None,
        outcome: str,
        reasons: tuple[int, ...] | list[int],
    ) -> dict[str, Any]:
        """Record a decision (fsync'd before returning)."""
        entry = {
            "type": "decision",
            "loggedAt": utc_now_iso(),
            "decisionHash": decision_hash,
            "requestId": request_id,
            "kind": kind,
            "orderId": order_id,
            "outcome": outcome,
            "reasons": [int(r) for r in reasons],
        }
        with self._lock:
            self._write(entry)
            self._apply(entry)
        return entry

    def add_tx(self, decision_hash: str, tx_hash: str) -> None:
        """Attach the mined transaction to a logged decision."""
        entry = {"type": "tx", "loggedAt": utc_now_iso(), "decisionHash": decision_hash, "txHash": tx_hash}
        with self._lock:
            self._write(entry)
            self._apply(entry)

    def lookup(self, decision_hash: str) -> dict[str, Any] | None:
        """``{"found": true, "requestId", "kind", "orderId", "outcome", "txHash", "loggedAt"}`` or None (7.2).

        ``outcome`` (additive to 7.2) is the decision's outcome name: REFUSE and NEEDS_OWNER_APPROVAL
        decisions are logged too, and their hashes are public in ``DecisionRecorded``, so a consumer must
        accept an outflow only for an outflow outcome (PAY, PAY_CAPPED, ESCROW) with a matching txHash.
        """
        with self._lock:
            entry = self._index.get(decision_hash.lower())
            if entry is None:
                return None
            return {
                "found": True,
                "requestId": entry["requestId"],
                "kind": entry["kind"],
                "orderId": entry["orderId"],
                "outcome": entry.get("outcome"),
                "txHash": entry.get("txHash"),
                "loggedAt": entry["loggedAt"],
            }

    def __contains__(self, decision_hash: object) -> bool:
        with self._lock:
            return isinstance(decision_hash, str) and decision_hash.lower() in self._index


__all__ = ["DecisionLog"]
