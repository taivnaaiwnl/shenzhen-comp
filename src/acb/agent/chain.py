"""Read-only chain access of the wallet agent (process P2 never sends a transaction).

:class:`AgentChain` is the protocol the agent codes against; :class:`Web3AgentChain` implements it over
web3 and the deployment's contracts (WP3) with WP5's :class:`~acb.policy.chainview.ChainView`. Unit
tests use a fake with the same methods (``tests/agent/agent_helpers.py``).

Every method takes an optional ``block``; the runtime reads ``block()`` once per invoice and passes it
to every read, so one decision sees one consistent state. Time is always the chain clock (spec 0.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from acb.types import Deployments, Grade, MandateView, OrderState, Outcome, PayeeView, VaultState


@dataclass(frozen=True)
class OrderView:
    """The fields of ``TierVault.Order`` the agent needs (3.2)."""

    state: OrderState
    outcome: Outcome
    deadline: int
    claimed_at: int
    amount: int
    payee_agent_id: int
    payee: str
    delivery_hash: str
    created_at: int = 0


@dataclass(frozen=True)
class ApprovalView:
    """``TierVault.Approval`` (3.2)."""

    exists: bool
    payee_agent_id: int
    expected_payee: str
    amount: int
    expiry: int
    escrow_deadline: int
    as_escrow: bool
    used: bool


@dataclass(frozen=True)
class GradeInfo:
    """Effective grade plus the posted record (score and reasons) of one agent."""

    grade: Grade
    score: int | None
    reasons: tuple[int, ...]
    posted_at: int | None


class AgentChain(Protocol):
    """Reads the agent needs (all read-only)."""

    chain_id: int
    vault_address: str
    vault_agent_id: int
    usdc: str

    def block(self) -> int: ...

    def now(self, block: int | None = None) -> int: ...

    def vault_state(self, block: int | None = None) -> VaultState: ...

    def mandate(self, mandate_id: str, block: int | None = None) -> MandateView | None: ...

    def active_mandates(self, block: int | None = None) -> list[MandateView]: ...

    def live_wallet(self, agent_id: int, block: int | None = None) -> str | None: ...

    def grade_info(self, agent_id: int, block: int | None = None) -> GradeInfo: ...

    def payee_view(self, agent_id: int, mandate: MandateView | None, block: int | None = None) -> PayeeView: ...

    def order(self, order_id: str, block: int | None = None) -> OrderView: ...

    def approval(self, order_id: str, block: int | None = None) -> ApprovalView: ...

    def registered_at(self, agent_id: int) -> int | None: ...

    def tier_changes(self, block: int | None = None) -> list[dict[str, Any]]: ...


class Web3AgentChain:
    """:class:`AgentChain` over a ``Web3`` and a :class:`~acb.types.Deployments` (vault ``wallet``)."""

    def __init__(self, w3: Any, dep: Deployments, vault_name: str = "wallet"):
        from acb.chain.contracts import bind
        from acb.policy.chainview import ChainView

        self.w3 = w3
        self.dep = dep
        self.contracts = bind(w3, dep)
        self.view = ChainView(self.contracts, dep, vault_name)
        self.vault = self.contracts.vault(vault_name)
        self.chain_id = int(dep.chain_id)
        self.vault_address = dep.vaults[vault_name].address.lower()
        self.vault_agent_id = int(dep.vaults[vault_name].agent_id)
        self.usdc = dep.contracts["USDC"].address.lower()
        self._registered: dict[int, int | None] = {}

    def _b(self, block: int | None) -> int:
        return self.view.block() if block is None else int(block)

    def block(self) -> int:
        return self.view.block()

    def now(self, block: int | None = None) -> int:
        return self.view.now(self._b(block))

    def vault_state(self, block: int | None = None) -> VaultState:
        return self.view.vault_state(self._b(block))

    def mandate(self, mandate_id: str, block: int | None = None) -> MandateView | None:
        return self.view.mandate(mandate_id, self._b(block))

    def active_mandates(self, block: int | None = None) -> list[MandateView]:
        b = self._b(block)
        return self.view.active_mandates(self.view.now(b), b)

    def live_wallet(self, agent_id: int, block: int | None = None) -> str | None:
        try:
            return self.view.live_wallet(int(agent_id), self._b(block))
        except Exception:  # noqa: BLE001 - an unknown agentId reverts in the identity registry
            return None

    def grade_info(self, agent_id: int, block: int | None = None) -> GradeInfo:
        b = self._b(block)
        max_age = self.vault.max_grade_age(block=b)
        grade = self.view.effective_grade(int(agent_id), max_age, b)
        record = self.contracts.acb.get_record(int(agent_id), block=b)
        posted = record.grade != Grade.NONE
        return GradeInfo(
            grade=grade,
            score=record.score if posted else None,
            reasons=record.reason_list if posted else (),
            posted_at=record.posted_at if posted else None,
        )

    def payee_view(self, agent_id: int, mandate: MandateView | None, block: int | None = None) -> PayeeView:
        b = self._b(block)
        max_age = self.vault.max_grade_age(block=b)
        view = self.view.payee_view(int(agent_id), max_grade_age=max_age, mandate=mandate, block=b)
        return view.model_copy(update={"registered_at": self.registered_at(int(agent_id))})

    def order(self, order_id: str, block: int | None = None) -> OrderView:
        info = self.view.order(order_id, self._b(block))
        return OrderView(
            state=OrderState(info.state),
            outcome=Outcome(info.outcome),
            deadline=int(info.deadline),
            claimed_at=int(info.claimed_at),
            amount=int(info.amount),
            payee_agent_id=int(info.payee_agent_id),
            payee=str(info.payee).lower(),
            delivery_hash=str(info.delivery_hash).lower(),
            created_at=int(info.created_at),
        )

    def approval(self, order_id: str, block: int | None = None) -> ApprovalView:
        info = self.view.approval(order_id, self._b(block))
        return ApprovalView(
            exists=bool(info.exists),
            payee_agent_id=int(info.payee_agent_id),
            expected_payee=str(info.expected_payee).lower(),
            amount=int(info.amount),
            expiry=int(info.expiry),
            escrow_deadline=int(info.escrow_deadline),
            as_escrow=bool(info.as_escrow),
            used=bool(info.used),
        )

    def registered_at(self, agent_id: int) -> int | None:
        """Block time of the agent's ERC-8004 registration (from ``registerTx`` in the deployment)."""
        aid = int(agent_id)
        if aid in self._registered:
            return self._registered[aid]
        found: int | None = None
        for entry in self.dep.agents.values():
            if int(entry.agent_id) == aid and entry.register_tx:
                try:
                    receipt = self.w3.eth.get_transaction_receipt(entry.register_tx)
                    found = int(self.w3.eth.get_block(receipt["blockNumber"])["timestamp"])
                except Exception:  # noqa: BLE001 - unknown on this node: age is shown as unknown
                    found = None
                break
        self._registered[aid] = found
        return found

    def tier_changes(self, block: int | None = None) -> list[dict[str, Any]]:
        """``TierChanged`` events of the vault, oldest first (for ``explain(tier)``)."""
        from web3 import Web3

        out = []
        for log in self.vault.logs("TierChanged", self.view.start_block, self._b(block)):
            args = log["args"]
            out.append(
                {
                    "fromTier": int(args["fromTier"]),
                    "toTier": int(args["toTier"]),
                    "reasonCode": int(args["reasonCode"]),
                    "txHash": Web3.to_hex(log["transactionHash"]),
                    "block": int(log["blockNumber"]),
                }
            )
        return out


__all__ = ["AgentChain", "ApprovalView", "GradeInfo", "OrderView", "Web3AgentChain"]
