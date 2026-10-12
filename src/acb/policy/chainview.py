"""Chain reads behind the policy contexts (not pure: this is the only module of ``acb.policy`` with I/O).

:class:`ChainView` reads, at one block (``"latest"`` resolved to a number once), everything the
contexts need: the vault state and tier config, the mandate and its pinned wallets, the payee's
effective grade and live wallet, its unsecured exposure and payment history from the vault's logs,
and the order state. The signer (pass 2) uses it on fresh state; the agent (pass 1) may use it too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from acb.chain.contracts import ZERO_ADDRESS, ChainContracts, b32
from acb.policy.context import build_payee_view, display_name, payment_history, unsecured_exposure
from acb.policy.mandate import pinned_wallet
from acb.types import Deployments, Grade, MandateView, OrderState, Outcome, PayeeView, VaultState

SETTLED_OUTCOMES = frozenset({int(Outcome.ON_TIME), int(Outcome.LATE)})
"""Delivery outcomes that end an order's unsecured exposure (FAILED does not)."""


@dataclass(frozen=True)
class PayeeLogs:
    """Exposure and history of one payee in one vault."""

    unsecured_exposure: int
    payment_history: tuple[int, ...]


class ChainView:
    """Typed reads for one vault of one deployment, at a fixed block."""

    def __init__(self, contracts: ChainContracts, dep: Deployments, vault_name: str = "wallet"):
        self.c = contracts
        self.dep = dep
        self.vault = contracts.vault(vault_name)
        self.w3 = contracts.w3
        self.start_block = int(dep.start_block)

    def block(self) -> int:
        """The latest block number (read once per request, then used for every read)."""
        return int(self.w3.eth.block_number)

    def now(self, block: int) -> int:
        """Timestamp of ``block`` (the one clock: latest block time, spec 0.1)."""
        return int(self.w3.eth.get_block(block)["timestamp"])

    def vault_state(self, block: int) -> VaultState:
        return self.vault.state(block=block)

    def mandate(self, mandate_id: str, block: int) -> MandateView | None:
        return self.vault.mandate_view(mandate_id, from_block=self.start_block, block=block)

    def active_mandates(self, now: int, block: int) -> list[MandateView]:
        """Every mandate registered in the vault that is active and unexpired at ``now``."""
        out: list[MandateView] = []
        seen: set[str] = set()
        for log in self.vault.logs("MandateRegistered", self.start_block, block):
            mid = "0x" + bytes(log["args"]["mandateId"]).hex()
            if mid in seen:
                continue
            seen.add(mid)
            view = self.mandate(mid, block)
            if view is not None and view.active and now <= view.valid_until:
                out.append(view)
        return out

    def live_wallet(self, agent_id: int, block: int) -> str | None:
        wallet = self.c.identity.get_agent_wallet(int(agent_id), block=block)
        return None if not wallet or wallet == ZERO_ADDRESS else wallet.lower()

    def effective_grade(self, agent_id: int, max_grade_age: int, block: int) -> Grade:
        return self.c.acb.effective_grade(int(agent_id), int(max_grade_age), block=block)

    def payee_logs(self, agent_id: int, block: int) -> PayeeLogs:
        """``Paid`` orders of this vault to the payee and the ones settled by a delivery.

        Only an ON_TIME or LATE ``DeliveryRecorded`` settles an order. A FAILED outcome is a realised loss,
        so it stays in the unsecured exposure (C14) until the bureau's grade reflects it.
        """
        paid = [
            {"orderId": "0x" + bytes(log["args"]["orderId"]).hex(), "amount": int(log["args"]["amount"])}
            for log in self.vault.logs("Paid", self.start_block, block, payeeAgentId=int(agent_id))
        ]
        delivered = [
            "0x" + bytes(log["args"]["orderId"]).hex()
            for log in self.vault.logs("DeliveryRecorded", self.start_block, block, payeeAgentId=int(agent_id))
            if int(log["args"]["outcome"]) in SETTLED_OUTCOMES
        ]
        return PayeeLogs(unsecured_exposure(paid, delivered), payment_history(paid))

    def payee_view(self, agent_id: int, *, max_grade_age: int, mandate: MandateView | None, block: int) -> PayeeView:
        record = self.c.acb.get_record(int(agent_id), block=block)
        logs = self.payee_logs(agent_id, block)
        return build_payee_view(
            agent_id=agent_id,
            grade=self.effective_grade(agent_id, max_grade_age, block),
            live_wallet=self.live_wallet(agent_id, block),
            pinned_wallet=pinned_wallet(mandate, agent_id),
            name=display_name(self.dep, agent_id),
            score=record.score if record.grade != Grade.NONE else None,
            posted_at=record.posted_at if record.grade != Grade.NONE else None,
            unsecured_exposure=logs.unsecured_exposure,
            payment_history=logs.payment_history,
        )

    def order_state(self, order_id: str, block: int) -> OrderState:
        return self.vault.get_order(order_id, block=block).state

    def order(self, order_id: str, block: int) -> Any:
        return self.vault.get_order(order_id, block=block)

    def approval(self, order_id: str, block: int) -> Any:
        return self.vault.get_approval(order_id, block=block)

    def mandate_payee(self, mandate_id: str, agent_id: int, block: int) -> str | None:
        wallet = self.vault.mandate_payee(mandate_id, int(agent_id), block=block)
        return None if not wallet or wallet == ZERO_ADDRESS else wallet.lower()

    def decision_records(self, order_id: str, block: int) -> list[dict[str, Any]]:
        """``DecisionRecorded`` logs of this vault for one order."""
        out = []
        for log in self.vault.logs("DecisionRecorded", self.start_block, block, orderId=b32(order_id)):
            args = log["args"]
            out.append(
                {
                    "decision": int(args["decision"]),
                    "payeeAgentId": int(args["payeeAgentId"]),
                    "invoiceHash": "0x" + bytes(args["invoiceHash"]).hex(),
                    "decisionHash": "0x" + bytes(args["decisionHash"]).hex(),
                    "block": int(log["blockNumber"]),
                }
            )
        return out

    def usdc_blacklisted(self, address: str | None, block: int) -> bool:
        """Circle USDC blacklist (C11 on Base Sepolia); False when the token has no blacklist."""
        if not address:
            return False
        return bool(self.c.usdc.is_blacklisted(address, block=block))


__all__ = ["SETTLED_OUTCOMES", "ChainView", "PayeeLogs"]
