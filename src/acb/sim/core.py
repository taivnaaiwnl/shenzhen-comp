"""The counterparty simulator (process P6, spec 9): every simulated agent of the demo, each with its own key.

:class:`Simulator` holds the parties (personas from ``deployments/<network>.json`` with their keys,
plus the Anvil-only attack identities), the persisted state, the event bus and the chain access. It
implements the two public provider endpoints of 9.4:

- :meth:`Simulator.quote`: a signed ``acb-invoice/1`` for a quote request, at catalogue prices, with
  ``quoteRequestId``, ``buyerVault`` and ``capOf`` as received; the persona's script may put an
  injection template into the memo (Orion). Every issued invoice is published to
  ``$ACB_OUT_DIR/invoices/<invoiceHash>.json``.
- :meth:`Simulator.deliver`: checks on-chain that the order is PAID or ESCROWED in the buyer's vault
  with the persona as payee, then follows the persona's script. For an escrow the provider sends
  ``claimDelivered`` from its own key **before** it hands over the payload (spec 9.3); a late persona
  answers ``{"status": "pending"}`` until its scripted time after the deadline, a defaulting one
  forever.

Scripted market behaviours (warm-up, defaults, reports, spam, appeal, attacks) are in
:mod:`acb.sim.market` and :mod:`acb.sim.attacks` and run through :meth:`Simulator.run_action`.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from eth_account.signers.local import LocalAccount
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from pydantic.alias_generators import to_camel

from acb.canonical import canonical_bytes, write_canonical
from acb.canonical import order_id as compute_order_id
from acb.chain.contracts import OrderInfo
from acb.keys import KeyRing
from acb.logbus import EventBus
from acb.sim.invoices import (
    QUOTE_REQUEST_ID_RE,
    build_invoice,
    build_payload,
    build_receipt,
    invoice_id_for,
    invoice_items,
    payload_hash,
    publish_invoice,
    sign_wire_invoice,
)
from acb.sim.personas import Behaviour, PersonaSpec, SimConfig
from acb.sim.state import SimState
from acb.types import Address, AgentId, Deployments, Hex32, NetworkProfile, OrderState, TxResult

ANVIL_CHAIN_ID = 31337
MAX_BODY_BYTES = 16 * 1024
STALE_CLOCK_S = 5
"""On Anvil with automine, a block older than this (wall clock) is refreshed with ``evm_mine`` before
the chain clock is read for an invoice or a delivery, so deadlines are not issued in the past."""

_INVOICE_ID = r"^[A-Za-z0-9-]{1,40}$"


class SimError(Exception):
    """An error with an HTTP status and a JSON body (used by the service and the actions)."""

    def __init__(self, status: int, body: Mapping[str, Any] | str):
        self.status = status
        self.body = {"status": "error", "error": body} if isinstance(body, str) else dict(body)
        super().__init__(f"{status}: {self.body}")


# ---------------------------------------------------------------------------
# request bodies (camelCase on the wire, nothing extra)
# ---------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", alias_generator=to_camel, populate_by_name=True)


class QuoteBody(_Body):
    """``POST /p/{key}/quote`` (spec 9.4)."""

    quote_request_id: str = Field(pattern=r"^qr-[0-9a-f]{32}$")
    buyer_agent_id: AgentId
    buyer_vault: Address
    sku: str = Field(max_length=40)
    quantity: StrictInt = Field(ge=1, le=1000)
    cap_of: str = Field(default="", pattern=r"^$|^[A-Za-z0-9-]{1,40}$")


class DeliverBody(_Body):
    """``POST /p/{key}/deliver`` (spec 9.3): the order id, the invoice id and the payment tx."""

    order_id: Hex32
    invoice_id: str = Field(pattern=_INVOICE_ID)
    tx_hash: Hex32 | None = None


def parse_body(model: type[_Body], raw: Any) -> Any:
    """Validate a request body; any problem is a 422 with the field names (never the values)."""
    if not isinstance(raw, Mapping):
        raise SimError(422, "body must be a JSON object")
    try:
        return model.model_validate(dict(raw))
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "body" for e in exc.errors()})
        raise SimError(422, {"status": "invalid", "fields": fields}) from None


# ---------------------------------------------------------------------------
# parties and chain access
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Party:
    """An identity the simulator acts as: its agentId and the key that owns it and is its agentWallet."""

    key: str
    agent_id: int
    account: LocalAccount
    name: str = ""

    @property
    def address(self) -> str:
        return self.account.address.lower()


class ChainView(Protocol):
    """What the provider endpoints need from the chain (a fake implements it in unit tests)."""

    chain_id: int
    usdc: str

    def now(self) -> int: ...

    def buyer_agent_id(self, vault: str) -> int | None: ...

    def order(self, vault: str, order_id: str) -> OrderInfo: ...

    def claim(self, sim: Simulator, party: Party, vault: str, order_id: str, delivery_hash: str) -> TxResult: ...


class Web3ChainView:
    """:class:`ChainView` over web3 and the deployment's contracts."""

    def __init__(self, w3: Any, dep: Deployments):
        from acb.chain.contracts import bind

        self.w3 = w3
        self.dep = dep
        self.contracts = bind(w3, dep)
        self.chain_id = int(dep.chain_id)
        self.usdc = dep.contracts["USDC"].address

    def now(self) -> int:
        from acb.chain.clock import now

        latest = now(self.w3)
        if self.chain_id == ANVIL_CHAIN_ID and latest < time.time() - STALE_CLOCK_S:
            response = self.w3.provider.make_request("evm_mine", [])
            if "error" not in response:
                latest = now(self.w3)
        return latest

    def buyer_agent_id(self, vault: str) -> int | None:
        if not self.contracts.acb.is_vault(vault):
            return None
        return self.contracts.acb.vault_agent_id(vault)

    def order(self, vault: str, order_id: str) -> OrderInfo:
        return self.contracts.vault_at(vault).get_order(order_id)

    def claim(self, sim: Simulator, party: Party, vault: str, order_id: str, delivery_hash: str) -> TxResult:
        fn = self.contracts.vault_at(vault).claim_delivered(order_id, delivery_hash)
        return sim.sender(party.account).send(fn)


class InboxClient:
    """Pushes to the wallet agent's public inbox (``/inbox/invoice``, ``/inbox/message``, spec 8.8)."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        import httpx

        url = f"{self.base_url}{path}"
        try:
            response = httpx.post(url, json=dict(body), timeout=self.timeout)
        except httpx.HTTPError as exc:
            return {"url": url, "statusCode": None, "error": type(exc).__name__}
        try:
            parsed: Any = response.json()
        except ValueError:
            parsed = None
        return {"url": url, "statusCode": response.status_code, "body": parsed}


def default_inbox_url(environ: Mapping[str, str] | None = None) -> str:
    """``SIM_AGENT_URL`` or ``http://127.0.0.1:<AGENT_PORT or 8701>`` (always the literal 127.0.0.1)."""
    env = os.environ if environ is None else environ
    if env.get("SIM_AGENT_URL"):
        return env["SIM_AGENT_URL"]
    return f"http://127.0.0.1:{int(env.get('AGENT_PORT') or 8701)}"


# ---------------------------------------------------------------------------
# the simulator
# ---------------------------------------------------------------------------


@dataclass
class _Collector:
    txs: list[TxResult] = field(default_factory=list)


class Simulator:
    """All simulated agents of one network (see the module docstring)."""

    def __init__(
        self,
        net: NetworkProfile,
        dep: Deployments,
        keys: KeyRing,
        cfg: SimConfig | None = None,
        *,
        w3: Any | None = None,
        chain: ChainView | None = None,
        out_dir: str | os.PathLike[str] | None = None,
        bus: EventBus | None = None,
        inbox: InboxClient | Any | None = None,
        evidence_base: str | None = None,
    ):
        from acb import config

        self.net = net
        self.dep = dep
        self.keys = keys
        self.cfg = cfg or SimConfig.load()
        if w3 is None and chain is None:
            from acb.chain.client import make_web3

            w3 = make_web3(net)
        self.w3 = w3
        self.chain: ChainView = chain if chain is not None else Web3ChainView(w3, dep)
        self.out_dir = Path(out_dir) if out_dir is not None else config.out_dir()
        self.state = SimState(self.out_dir / "sim" / "state.json")
        explorer = next((e.get("tx") for e in net.explorers if e.get("tx")), None)
        self.bus = bus or EventBus("sim", chain_id=dep.chain_id, out_dir=self.out_dir, explorer_tx=explorer)
        self.inbox = inbox if inbox is not None else InboxClient(default_inbox_url())
        self.evidence_base = evidence_base or os.environ.get("EVIDENCE_BASE_URI") or f"evidence/{net.name}/"
        self._senders: dict[str, Any] = {}
        self._persona_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self.admin_lock = threading.Lock()
        self._local = threading.local()
        self.parties: dict[str, Party] = {}
        self.accounts: dict[str, LocalAccount] = {}
        self._load_parties()

    # ---- basics ---------------------------------------------------------------------------------

    @property
    def chain_id(self) -> int:
        return int(self.dep.chain_id)

    @property
    def usdc(self) -> str:
        return self.dep.contracts["USDC"].address

    @property
    def is_anvil(self) -> bool:
        return self.chain_id == ANVIL_CHAIN_ID

    def now(self) -> int:
        """The one clock: the latest block timestamp (refreshed on an idle automine Anvil)."""
        return self.chain.now()

    def _load_parties(self) -> None:
        for role in self.keys.roles():
            account = self.keys.account(role)
            self.accounts[account.address.lower()] = account
        for key, entry in self.dep.agents.items():
            if key == "wallet" or key not in self.keys:
                continue
            account = self.keys.account(key)
            self.parties[key] = Party(key, int(entry.agent_id), account, entry.name)
            if entry.wallet is not None and entry.wallet != account.address.lower():
                self.emit(
                    "error",
                    {"message": f"{key}: registered wallet differs from its key; its invoices will not verify"},
                )
        for label, ent in self.state.data["entities"].items():
            account = self.extra_account(int(ent["keyIndex"]))
            self.parties[label] = Party(label, int(ent["agentId"]), account, ent.get("name", label))

    def extra_account(self, index: int) -> LocalAccount:
        """An extra account derived from the public Anvil test mnemonic (attack identities, Anvil only)."""
        from eth_account import Account
        from eth_account.hdaccount import key_from_seed, seed_from_mnemonic

        from acb.keys import ANVIL_MNEMONIC

        if not self.is_anvil:
            raise SimError(409, "extra accounts exist only on Anvil")
        seed = seed_from_mnemonic(ANVIL_MNEMONIC, "")
        account = Account.from_key(key_from_seed(seed, f"m/44'/60'/0'/0/{int(index)}"))
        self.accounts[account.address.lower()] = account
        return account

    def party(self, key: str) -> Party:
        if key not in self.parties:
            raise SimError(404, {"status": "unknown_party", "party": key})
        return self.parties[key]

    def persona(self, key: str) -> PersonaSpec | None:
        return self.cfg.personas.get(key)

    def behaviour(self, key: str) -> Behaviour:
        spec = self.persona(key)
        return spec.behaviour if spec is not None else Behaviour()

    def persona_lock(self, key: str) -> threading.RLock:
        with self._locks_guard:
            return self._persona_locks.setdefault(key, threading.RLock())

    def account_for(self, address: str) -> LocalAccount:
        account = self.accounts.get(address.lower())
        if account is None:
            raise SimError(409, {"status": "no_key", "address": address.lower()})
        return account

    def catalogue(self, key: str) -> dict[str, tuple[int, str]]:
        """``sku -> (unit price, description)``: prices from the pinned directory (deployments), which
        is what invoice validation compares against (V04), descriptions from the personas file."""
        entry = self.dep.agents.get(key)
        spec = self.persona(key)
        prices = dict(entry.skus) if entry is not None and entry.skus else {}
        if not prices and spec is not None:
            prices = {s: v.unit_price for s, v in spec.skus.items()}
        out = {}
        for sku, price in prices.items():
            description = spec.skus[sku].description if spec is not None and sku in spec.skus else sku
            out[sku] = (int(price), description)
        return out

    def attacker_agent_id(self) -> int | None:
        entry = self.dep.agents.get("attacker")
        return int(entry.agent_id) if entry is not None else None

    # ---- events and transactions ------------------------------------------------------------------

    @property
    def step(self) -> str | None:
        return getattr(self._local, "step", None)

    def emit(self, type_: str, data: Mapping[str, Any], **kw: Any) -> None:
        kw.setdefault("step", self.step)
        try:
            self.bus.emit(type_, data, **kw)
        except Exception:  # noqa: BLE001, S110 - the event feed must never break a simulated agent
            pass

    def _on_tx(self, result: TxResult) -> None:
        collector: _Collector | None = getattr(self._local, "collector", None)
        if collector is not None:
            collector.txs.append(result)
        self.emit(
            "tx",
            {
                "function": result.function,
                "from": result.from_address,
                "to": result.to,
                "status": result.status,
                "expectedStatus": result.expected_status,
                "block": result.block,
                "gasUsed": result.gas_used,
                "revertError": result.revert_error,
                "actor": self.actor_of(result.from_address),
            },
            tx_hash=result.hash,
        )

    def actor_of(self, address: str) -> str:
        for key, party in self.parties.items():
            if party.address == address.lower():
                return key
        for role in self.keys.roles():
            if self.keys.address(role) == address.lower():
                return role
        return address.lower()

    def sender(self, account: LocalAccount) -> Any:
        """One ``TxSender`` per key (spec 7.4), reporting every result to the event feed."""
        from acb.chain.tx import TxSender

        key = account.address.lower()
        if key not in self._senders:
            if self.w3 is None:
                raise SimError(409, "no chain connection")
            self._senders[key] = TxSender(self.w3, account, on_result=self._on_tx)
        return self._senders[key]

    @contextmanager
    def acting(self, step: str | None) -> Any:
        """Tag events with ``step`` and collect every transaction sent in this thread."""
        previous_step = getattr(self._local, "step", None)
        previous = getattr(self._local, "collector", None)
        collector = _Collector()
        self._local.step = step or previous_step
        self._local.collector = collector
        try:
            yield collector
        finally:
            self._local.step = previous_step
            self._local.collector = previous

    # ---- invoices ---------------------------------------------------------------------------------

    def issue_invoice(
        self,
        party: Party,
        *,
        buyer_vault: str,
        buyer_agent_id: int,
        lines: list[tuple[str, str, int, int]],
        quote_request_id: str = "",
        cap_of: str = "",
        memo: str = "",
        delivery_s: int | None = None,
        validity_s: int | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Sign, publish and record one invoice from ``party``; returns ``(signed wire, invoiceHash)``.

        The invoice id ``inv-<key>-<NNNN>`` is never reused: ids already issued or already used by an
        order in the buyer's vault are skipped.
        """
        behaviour = self.behaviour(party.key)
        delivery_s = behaviour.delivery_s if delivery_s is None else delivery_s
        validity_s = behaviour.validity_s if validity_s is None else validity_s
        vault = buyer_vault.lower()
        with self.persona_lock(party.key):
            for _ in range(10_000):
                n = self.state.bump(party.key, "invoices")
                invoice_id = invoice_id_for(party.key, n)
                if f"{party.key}:{invoice_id}" in self.state.data["invoices"]:
                    continue
                oid = compute_order_id(self.chain_id, vault, party.agent_id, invoice_id)
                if self.chain.order(vault, oid).state != OrderState.NONE:
                    continue
                break
            else:  # pragma: no cover
                raise SimError(500, "no free invoice id")
            issued_at = self.now()
            wire = build_invoice(
                invoice_id=invoice_id,
                quote_request_id=quote_request_id,
                cap_of=cap_of,
                issuer_agent_id=party.agent_id,
                buyer_agent_id=buyer_agent_id,
                buyer_vault=vault,
                chain_id=self.chain_id,
                token=self.usdc,
                items=invoice_items(lines),
                issued_at=issued_at,
                expires_at=issued_at + validity_s,
                delivery_deadline=issued_at + delivery_s,
                memo=memo,
            )
            signed, invoice_hash = sign_wire_invoice(party.account, wire)
            publish_invoice(self.out_dir, signed)
            with self.state.edit() as data:
                data["invoices"][f"{party.key}:{invoice_id}"] = {
                    "hash": invoice_hash,
                    "orderId": oid,
                    "buyerVault": vault,
                    "buyerAgentId": int(buyer_agent_id),
                    "amount": int(signed["amount"]),
                    "deliveryDeadline": int(signed["deliveryDeadline"]),
                    "expiresAt": int(signed["expiresAt"]),
                    "quoteRequestId": quote_request_id,
                    "capOf": cap_of,
                    **dict(extra or {}),
                }
        self.emit(
            "info",
            {
                "message": "invoice issued",
                "issuer": party.key,
                "invoiceId": invoice_id,
                "invoiceHash": invoice_hash,
                "orderId": oid,
                "buyerVault": vault,
                "amount": signed["amount"],
                "solicited": bool(quote_request_id),
                **({"template": extra["template"]} if extra and extra.get("template") else {}),
            },
        )
        return signed, invoice_hash

    def load_invoice(self, invoice_hash: str) -> dict[str, Any]:
        """A published invoice by hash (from ``$ACB_OUT_DIR/invoices``)."""
        if not re.fullmatch(r"0x[0-9a-f]{64}", invoice_hash or ""):
            raise SimError(422, "bad invoice hash")
        path = self.out_dir / "invoices" / f"{invoice_hash}.json"
        if not path.is_file():
            raise SimError(404, {"status": "unknown_invoice", "invoiceHash": invoice_hash})
        return json.loads(path.read_bytes())

    def invoice_record(self, key: str, invoice_id: str) -> dict[str, Any] | None:
        with self.state.lock:
            rec = self.state.data["invoices"].get(f"{key}:{invoice_id}")
            return dict(rec) if rec is not None else None

    # ---- provider endpoint: quote -------------------------------------------------------------------

    def quote(self, key: str, raw: Any) -> dict[str, Any]:
        """``POST /p/{key}/quote``: a signed invoice answering one quote request (spec 9.2, 9.4)."""
        spec = self.persona(key)
        if spec is None or key not in self.parties or not self.catalogue(key):
            raise SimError(404, {"status": "unknown_provider", "provider": key})
        body: QuoteBody = parse_body(QuoteBody, raw)
        catalogue = self.catalogue(key)
        if body.sku not in catalogue:
            raise SimError(404, {"status": "unknown_sku", "sku": body.sku})
        if not QUOTE_REQUEST_ID_RE.fullmatch(body.quote_request_id):  # pragma: no cover - pattern above
            raise SimError(422, "bad quoteRequestId")
        registered = self.chain.buyer_agent_id(body.buyer_vault)
        if registered is None or registered != body.buyer_agent_id:
            raise SimError(
                422, {"status": "unknown_buyer", "detail": "buyerVault is not a registered vault of buyerAgentId"}
            )
        if body.cap_of:
            previous = self.invoice_record(key, body.cap_of)
            if previous is None or previous["buyerVault"] != body.buyer_vault:
                raise SimError(
                    422, {"status": "unknown_cap_of", "detail": "capOf is not an invoice of ours to this buyer"}
                )
        party = self.parties[key]
        unit, description = catalogue[body.sku]
        with self.persona_lock(key):
            override = self.state.override(key)
            quote_no = self.state.bump(key, "quotes")
            orders_before = self.state.counter(key, "orders")
            template = spec.behaviour.injection_for(quote_no, orders_before, body.sku, override)  # type: ignore[arg-type]
            memo = spec.memo
            if template is not None:
                try:
                    memo = self.cfg.template(template, attacker_id=self.attacker_agent_id())
                except ValueError:
                    raise SimError(
                        500, {"status": "no_attacker", "detail": "the deployment has no attacker agent"}
                    ) from None
            signed, _ = self.issue_invoice(
                party,
                buyer_vault=body.buyer_vault,
                buyer_agent_id=body.buyer_agent_id,
                lines=[(body.sku, description, unit, body.quantity)],
                quote_request_id=body.quote_request_id,
                cap_of=body.cap_of,
                memo=memo,
                extra={"quoteNo": quote_no, "template": template},
            )
        return signed

    # ---- provider endpoint: deliver -----------------------------------------------------------------

    def delivery_path(self, order_id: str) -> Path:
        return self.out_dir / "sim" / "deliveries" / f"{order_id.lower()}.json"

    def ready_at(self, order_id: str) -> int | None:
        """When a late order is handed over (chain clock), or None if it is not a late order."""
        with self.state.lock:
            rec = self.state.data["orders"].get(order_id.lower())
            return int(rec["readyAt"]) if rec and rec.get("readyAt") is not None else None

    def order_mode(self, order_id: str) -> str | None:
        with self.state.lock:
            rec = self.state.data["orders"].get(order_id.lower())
            return rec.get("mode") if rec else None

    def deliver(self, key: str, raw: Any) -> dict[str, Any]:
        """``POST /p/{key}/deliver``: claim (escrow) then the signed receipt and payload, or pending."""
        if key not in self.parties:
            raise SimError(404, {"status": "unknown_provider", "provider": key})
        body: DeliverBody = parse_body(DeliverBody, raw)
        party = self.parties[key]
        record = self.invoice_record(key, body.invoice_id)
        if record is None:
            raise SimError(404, {"status": "unknown_invoice", "invoiceId": body.invoice_id})
        vault = record["buyerVault"]
        expected = compute_order_id(self.chain_id, vault, party.agent_id, body.invoice_id)
        if expected != body.order_id:
            raise SimError(422, {"status": "order_mismatch", "detail": "orderId does not belong to this invoice"})
        with self.persona_lock(key):
            stored = self._stored_delivery(body.order_id)
            if stored is not None:
                return stored
            order = self.chain.order(vault, body.order_id)
            if order.state not in (OrderState.PAID, OrderState.ESCROWED):
                raise SimError(409, {"status": "not_paid", "orderState": order.state.name})
            if (
                order.payee_agent_id != party.agent_id
                or order.payee != party.address
                or order.amount != int(record["amount"])
            ):
                raise SimError(409, {"status": "order_mismatch", "detail": "payee or amount differs from the invoice"})
            rec = self._order_record(key, body, record, order)
            mode = rec["mode"]
            if mode == "never":
                return {"status": "pending"}
            if mode == "late" and self.now() < int(rec["readyAt"]):
                return {"status": "pending"}
            return self._hand_over(party, body, record, order)

    def _order_record(self, key: str, body: DeliverBody, record: Mapping[str, Any], order: OrderInfo) -> dict[str, Any]:
        """The persisted record of an order; the first sight of a paid order assigns its number."""
        with self.state.edit() as data:
            rec = data["orders"].get(body.order_id)
            if rec is not None:
                return dict(rec)
            n = int(data["counters"].get(key, {}).get("orders", 0)) + 1
            data["counters"].setdefault(key, {})["orders"] = n
            override = data["overrides"].get(key)
            mode = self.behaviour(key).delivery_mode(n, override)
            rec = {
                "persona": key,
                "n": n,
                "invoiceId": body.invoice_id,
                "vault": record["buyerVault"],
                "kind": "escrow" if order.state == OrderState.ESCROWED else "pay",
                "mode": mode,
                "readyAt": None,
                "delivered": False,
            }
            if mode == "late":
                rec["readyAt"] = max(int(record["deliveryDeadline"]), order.deadline) + self.behaviour(key).late_after_s
            data["orders"][body.order_id] = rec
        self.emit(
            "info",
            {
                "message": "order seen",
                "provider": key,
                "orderNumber": n,
                "orderId": body.order_id,
                "kind": rec["kind"],
                "script": mode,
                **({"payTx": body.tx_hash} if body.tx_hash else {}),
            },
        )
        return dict(rec)

    def _stored_delivery(self, order_id: str) -> dict[str, Any] | None:
        path = self.delivery_path(order_id)
        if not path.is_file():
            return None
        return json.loads(path.read_bytes())

    def _hand_over(
        self, party: Party, body: DeliverBody, record: Mapping[str, Any], order: OrderInfo
    ) -> dict[str, Any]:
        from acb.chain.tx import TxError

        invoice = self.load_invoice(record["hash"])
        payload = build_payload(invoice, body.order_id)
        digest = payload_hash(payload)
        claim_tx: str | None = None
        if order.state == OrderState.ESCROWED and order.claimed_at == 0:
            # claim first, then hand over the data: the buyer cannot take the data and refund (9.3)
            try:
                result = self.chain.claim(self, party, record["buyerVault"], body.order_id, digest)
            except TxError as exc:
                raise SimError(502, {"status": "claim_failed", "error": str(exc)}) from None
            if result.status != 1:
                raise SimError(502, {"status": "claim_failed", "error": result.revert_error or "reverted"})
            claim_tx = result.hash
        delivered_at = self.now()
        receipt = build_receipt(
            party.account,
            order_id=body.order_id,
            invoice_id=body.invoice_id,
            issuer_agent_id=party.agent_id,
            payload_hash_hex=digest,
            delivered_at=delivered_at,
        )
        answer = {"status": "delivered", "receipt": receipt, "payload": payload, "claimTx": claim_tx}
        write_canonical(self.delivery_path(body.order_id), answer)
        with self.state.edit() as data:
            rec = data["orders"][body.order_id]
            rec.update({"delivered": True, "deliveredAt": delivered_at, "claimTx": claim_tx, "payloadHash": digest})
        self.emit(
            "info",
            {
                "message": "delivered",
                "provider": party.key,
                "orderId": body.order_id,
                "invoiceId": body.invoice_id,
                "payloadHash": digest,
                "items": len(payload["data"]),
                "claimTx": claim_tx,
            },
            tx_hash=claim_tx,
        )
        return answer

    # ---- admin actions --------------------------------------------------------------------------------

    def run_action(self, action: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """``POST /admin/run {action, args}``: one scripted behaviour (serialised; see :mod:`acb.sim.actions`)."""
        from acb.chain.tx import TxError
        from acb.sim.actions import ACTIONS, ANVIL_ONLY

        args = dict(args or {})
        if action not in ACTIONS:
            raise SimError(404, {"status": "unknown_action", "action": action, "known": sorted(ACTIONS)})
        if action in ANVIL_ONLY and not self.is_anvil:
            raise SimError(409, {"status": "anvil_only", "action": action})
        step = args.pop("step", None)
        if step is not None and not (isinstance(step, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,16}", step)):
            raise SimError(422, "step must be a short identifier such as S4")
        fn: Callable[[Simulator, dict[str, Any]], dict[str, Any]] = ACTIONS[action]
        with self.admin_lock, self.acting(step) as collector:
            self.emit("info", {"message": f"action {action} started", "action": action})
            try:
                result = fn(self, args)
            except SimError:
                raise
            except (KeyError, ValueError, TypeError) as exc:
                self.emit("error", {"message": f"action {action} failed: {exc}", "action": action})
                raise SimError(400, {"status": "failed", "action": action, "error": str(exc)}) from None
            except TxError as exc:
                self.emit("error", {"message": f"action {action}: transaction failed: {exc}", "action": action})
                raise SimError(502, {"status": "tx_failed", "action": action, "error": str(exc)}) from None
            except TimeoutError as exc:
                raise SimError(504, {"status": "timeout", "action": action, "error": str(exc)}) from None
            self.emit("info", {"message": f"action {action} done", "action": action, "txCount": len(collector.txs)})
        return {
            "status": "done",
            "action": action,
            "result": json.loads(canonical_bytes(result)) if result else {},
            "txs": [t.to_wire() for t in collector.txs],
        }


__all__ = [
    "MAX_BODY_BYTES",
    "ChainView",
    "DeliverBody",
    "InboxClient",
    "Party",
    "QuoteBody",
    "SimError",
    "Simulator",
    "Web3ChainView",
    "default_inbox_url",
    "parse_body",
]
