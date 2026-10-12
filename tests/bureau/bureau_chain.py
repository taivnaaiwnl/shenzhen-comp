"""Anvil helpers for the bureau and autonomy tests: real orders between registered vaults.

``ChainMarket`` drives the deployed contracts with the Anvil key ring: owners register mandates,
providers sign and publish invoices (written to ``<out>/invoices/<invoiceHash>.json``, as the
simulator does), buyers open escrows or pay, providers claim, buyers release or record deliveries.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acb.canonical import chash, invoice_hash, keccak_hex, order_id, sign_invoice, write_canonical
from acb.chain.clock import now
from acb.chain.contracts import DecisionParams, OutflowParams, bind
from acb.chain.tx import TxSender
from acb.keys import KeyRing
from acb.types import Decision, Deployments, Outcome

VAULT_ROLES = {
    "beacon": {"owner": "beacon_owner", "agent": "beacon"},
    "wallet": {"owner": "owner", "agent": "agent_signer"},
    "echo1": {"owner": "echo1_vault_owner", "agent": "echo1"},
    "echo2": {"owner": "echo2_vault_owner", "agent": "echo2"},
    "echo3": {"owner": "echo3_vault_owner", "agent": "echo3"},
}


@dataclass
class Order:
    vault: str
    order_id: str
    payee: str
    amount: int
    invoice: dict
    invoice_hash: str
    decision_hash: str


class ChainMarket:
    def __init__(self, w3: Any, dep: Deployments, out_dir: Path, keys: KeyRing | None = None):
        self.w3 = w3
        self.dep = dep
        self.keys = keys or KeyRing.anvil()
        self.c = bind(w3, dep)
        self.out = Path(out_dir)
        self._senders: dict[str, TxSender] = {}
        self._n = 0
        self.chain_id = int(w3.eth.chain_id)

    def sender(self, role: str) -> TxSender:
        if role not in self._senders:
            self._senders[role] = TxSender(self.w3, self.keys.account(role))
        return self._senders[role]

    def send(self, role: str, fn: Any, **kw: Any):
        r = self.sender(role).send(fn, **kw)
        return r

    def agent_id(self, key: str) -> int:
        return self.dep.agents[key].agent_id

    def wallet(self, key: str) -> str:
        return self.c.identity.get_agent_wallet(self.agent_id(key))

    # ---- mandates ----
    def mandate(self, vault: str, payees: list[str], max_per_tx: int = 1_000_000, max_total: int = 20_000_000) -> str:
        mid = keccak_hex(secrets.token_bytes(32))
        ids = [self.agent_id(p) for p in payees]
        wallets = [self.wallet(p) for p in payees]
        valid_until = now(self.w3) + 6 * 86400
        r = self.send(
            VAULT_ROLES[vault]["owner"],
            self.c.vault(vault).register_mandate(
                mid, ids, wallets, max_per_tx, max_total, valid_until, False, "0x" + "00" * 32
            ),
        )
        assert r.status == 1, r.revert_error
        return mid

    # ---- invoices ----
    def invoice(
        self,
        issuer: str,
        vault: str,
        amount: int,
        *,
        publish: bool = True,
        memo: str = "Thanks.",
        qr: str | None = None,
    ) -> dict:
        self._n += 1
        t = now(self.w3)
        body = {
            "schema": "acb-invoice/1",
            "invoiceId": f"inv-{issuer}-{self._n:04d}-{secrets.token_hex(3)}",
            "quoteRequestId": qr if qr is not None else "qr-" + secrets.token_hex(16),
            "capOf": "",
            "issuerAgentId": str(self.agent_id(issuer)),
            "buyerAgentId": str(self.dep.vaults[vault].agent_id),
            "buyerVault": self.dep.vaults[vault].address,
            "chainId": self.chain_id,
            "token": self.dep.contracts["USDC"].address,
            "items": [{"sku": "fx-live-15m", "description": "FX data", "unitPrice": str(amount), "quantity": 1}],
            "amount": str(amount),
            "issuedAt": t,
            "expiresAt": t + 600,
            "deliveryDeadline": t + 300,
            "memo": memo,
        }
        signed = sign_invoice(self.keys.account(issuer), body)
        if publish:
            write_canonical(self.out / "invoices" / f"{invoice_hash(body)}.json", signed)
        return signed

    # ---- orders ----
    def _params(self, vault: str, payee: str, amount: int, mid: str, decision: int, reason: int, **kw: Any) -> Order:
        inv = self.invoice(payee, vault, amount, publish=kw.get("publish", True))
        ihash = invoice_hash(inv)
        oid = order_id(self.chain_id, self.dep.vaults[vault].address, self.agent_id(payee), inv["invoiceId"])
        dhash = chash({"decision": decision, "order": oid, "n": self._n})
        return Order(vault, oid, payee, amount, inv, ihash, dhash)

    def escrow(
        self, vault: str, payee: str, amount: int, mid: str, *, publish: bool = True, deadline_s: int = 300
    ) -> Order:
        o = self._params(vault, payee, amount, mid, int(Decision.ESCROW), 2004, publish=publish)
        p = OutflowParams(
            o.order_id,
            self.agent_id(payee),
            amount,
            now(self.w3) + deadline_s,
            mid,
            o.invoice_hash,
            o.decision_hash,
            int(Decision.ESCROW),
            2004,
        )
        r = self.send(VAULT_ROLES[vault]["agent"], self.c.vault(vault).escrow(p))
        assert r.status == 1, r.revert_error
        return o

    def claim(self, o: Order) -> None:
        r = self.send(o.payee, self.c.vault(o.vault).claim_delivered(o.order_id, keccak_hex(o.order_id.encode())))
        assert r.status == 1, r.revert_error

    def release(self, o: Order) -> None:
        r = self.send(VAULT_ROLES[o.vault]["agent"], self.c.vault(o.vault).release(o.order_id))
        assert r.status == 1, r.revert_error

    def refund(self, o: Order, reason: int = 2400) -> None:
        r = self.send(VAULT_ROLES[o.vault]["agent"], self.c.vault(o.vault).refund(o.order_id, reason))
        assert r.status == 1, r.revert_error

    def escrow_release(self, vault: str, payee: str, amount: int, mid: str, **kw: Any) -> Order:
        o = self.escrow(vault, payee, amount, mid, **kw)
        self.claim(o)
        self.release(o)
        return o

    def pay(
        self, vault: str, payee: str, amount: int, mid: str, *, publish: bool = True, sender_role: str | None = None
    ) -> Order:
        o = self._params(vault, payee, amount, mid, int(Decision.PAY), 2000, publish=publish)
        p = OutflowParams(
            o.order_id,
            self.agent_id(payee),
            amount,
            now(self.w3) + 300,
            mid,
            o.invoice_hash,
            o.decision_hash,
            int(Decision.PAY),
            2000,
        )
        r = self.send(sender_role or VAULT_ROLES[vault]["agent"], self.c.vault(vault).pay(p))
        assert r.status == 1, r.revert_error
        return o

    def deliver(self, o: Order, outcome: Outcome = Outcome.ON_TIME) -> None:
        r = self.send(
            VAULT_ROLES[o.vault]["agent"],
            self.c.vault(o.vault).record_delivery(o.order_id, int(outcome), keccak_hex(b"d" + o.order_id.encode())),
        )
        assert r.status == 1, r.revert_error

    def refuse(self, vault: str, issuer: str, invoice: dict) -> str:
        ihash = invoice_hash(invoice)
        oid = order_id(self.chain_id, self.dep.vaults[vault].address, self.agent_id(issuer), invoice["invoiceId"])
        p = DecisionParams(
            oid,
            self.agent_id(issuer),
            int(invoice["amount"]),
            int(Decision.REFUSE),
            2108,
            ihash,
            chash({"refuse": oid}),
        )
        r = self.send(VAULT_ROLES[vault]["agent"], self.c.vault(vault).record_decision(p))
        assert r.status == 1, r.revert_error
        return oid

    # ---- reports ----
    def report(self, vault: str, subject: str, kind: str, order_id_hex: str, payload: dict | None = None) -> str:
        from acb.bureau.inputs import report_endpoint

        reporter_id = self.dep.vaults[vault].agent_id
        payload = payload or {
            "schema": "acb-report/1",
            "kind": kind,
            "subjectAgentId": str(self.agent_id(subject)),
            "reporterAgentId": str(reporter_id),
            "orderId": order_id_hex,
            "evidence": {},
        }
        fh = chash(payload)
        write_canonical(self.out / "reports" / f"{fh}.json", payload)
        endpoint = report_endpoint(order_id_hex, reporter_id, self.dep.vaults[vault].address)
        r = self.send(
            VAULT_ROLES[vault]["agent"],
            self.c.reputation.give_feedback(
                self.agent_id(subject), -1, 0, "acb-report", kind, endpoint, f"reports/{fh}.json", fh
            ),
        )
        assert r.status == 1, r.revert_error
        return fh
