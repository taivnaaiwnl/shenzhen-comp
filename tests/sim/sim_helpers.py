"""Test helpers of the simulator tests (WP8): a fake chain, a fake inbox, a fake deployment and an
independent implementation of the invoice rules V01-V09 of spec 6.2 (the stub of WP5's
``acb.policy.validate_invoice`` until it lands)."""

from __future__ import annotations

import dataclasses
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from acb.canonical import canonical_bytes, order_id, recover_invoice_signer
from acb.chain.contracts import ZERO_ADDRESS, ZERO_HASH, OrderInfo
from acb.chain.deploy import AGENT_ORDER, load_personas
from acb.keys import KeyRing
from acb.types import (
    AgentEntry,
    ContractEntry,
    Decision,
    Deployments,
    NetworkProfile,
    OrderState,
    Outcome,
    SignedInvoice,
    TxResult,
    VaultEntry,
)

CHAIN_ID = 31337
USDC = "0x" + "11" * 20
ACB = "0x" + "22" * 20
IDENTITY = "0x" + "33" * 20
REPUTATION = "0x" + "44" * 20
WALLET_VAULT = "0x" + "55" * 20
BEACON_VAULT = "0x" + "66" * 20
START = 1_800_000_000


def fake_deployments(keys: KeyRing | None = None) -> Deployments:
    """A deployment with the personas of ``config/personas.yaml``; agentIds 0.. in AGENT_ORDER."""
    keys = keys or KeyRing.anvil()
    personas = load_personas()
    agents = {}
    for i, key in enumerate(AGENT_ORDER):
        if key == "wallet":
            agents[key] = AgentEntry(
                agent_id=i,
                name="Research Wallet",
                identity_owner=keys.address("owner"),
                wallet=None,
                register_tx=None,
                category="research",
                skus={},
            )
            continue
        p = personas[key]
        agents[key] = AgentEntry(
            agent_id=i,
            name=p.name,
            identity_owner=keys.address(key),
            wallet=keys.address(key),
            register_tx=None,
            category=p.category,
            skus=dict(p.skus),
        )
    vaults = {
        "wallet": VaultEntry(
            address=WALLET_VAULT,
            agent_id=0,
            owner=keys.address("owner"),
            agent=keys.address("agent_signer"),
            autonomy=keys.address("autonomy"),
            profile="wallet",
            deploy_tx=None,
            register_vault_tx=None,
        ),
        "beacon": VaultEntry(
            address=BEACON_VAULT,
            agent_id=agents["beacon"].agent_id,
            owner=keys.address("beacon_owner"),
            agent=keys.address("beacon"),
            autonomy=keys.address("autonomy"),
            profile="market",
            deploy_tx=None,
            register_vault_tx=None,
        ),
    }
    return Deployments(
        network="anvil",
        chain_id=CHAIN_ID,
        deployed_at="2026-10-12T00:00:00Z",
        git_commit="test",
        start_block=0,
        roles={},
        contracts={
            "AgentCreditBureau": ContractEntry(address=ACB),
            "IdentityRegistry": ContractEntry(address=IDENTITY),
            "ReputationRegistry": ContractEntry(address=REPUTATION),
            "USDC": ContractEntry(address=USDC, extra={"mock": True, "decimals": 6}),
        },
        vault_code_hash=ZERO_HASH[:-1] + "1",
        anchors=(agents["beacon"].agent_id,),
        agents=agents,
        vaults=vaults,
        abi_sha=ZERO_HASH[:-1] + "2",
    )


EMPTY_ORDER = OrderInfo(
    state=OrderState.NONE,
    outcome=Outcome.NONE,
    decision=Decision.NONE,
    created_at=0,
    deadline=0,
    claimed_at=0,
    amount=0,
    payee_agent_id=0,
    payee=ZERO_ADDRESS,
    mandate_id=ZERO_HASH,
    delivery_hash=ZERO_HASH,
)


@dataclass
class FakeChain:
    """In-memory :class:`acb.sim.core.ChainView`: a clock, registered vaults, orders and claims."""

    chain_id: int = CHAIN_ID
    usdc: str = USDC
    clock: int = START
    vaults: dict[str, int] = field(default_factory=dict)
    orders: dict[tuple[str, str], OrderInfo] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)
    claim_status: int = 1

    def now(self) -> int:
        return self.clock

    def buyer_agent_id(self, vault: str) -> int | None:
        return self.vaults.get(vault.lower())

    def order(self, vault: str, oid: str) -> OrderInfo:
        return self.orders.get((vault.lower(), oid.lower()), EMPTY_ORDER)

    def open(
        self, vault: str, invoice: Mapping[str, Any], payee: str, *, escrow: bool, deadline: int | None = None
    ) -> str:
        """Record the order of a signed wire invoice as PAID or ESCROWED (what the buyer's vault does)."""
        oid = order_id(self.chain_id, vault, int(invoice["issuerAgentId"]), invoice["invoiceId"])
        self.orders[(vault.lower(), oid)] = OrderInfo(
            state=OrderState.ESCROWED if escrow else OrderState.PAID,
            outcome=Outcome.NONE,
            decision=Decision.ESCROW if escrow else Decision.PAY,
            created_at=self.clock,
            deadline=deadline if deadline is not None else int(invoice["deliveryDeadline"]),
            claimed_at=0,
            amount=int(invoice["amount"]),
            payee_agent_id=int(invoice["issuerAgentId"]),
            payee=payee.lower(),
            mandate_id="0x" + "77" * 32,
            delivery_hash=ZERO_HASH,
        )
        return oid

    def claim(self, sim, party, vault: str, oid: str, delivery_hash: str) -> TxResult:
        self.calls.append(("claim", vault.lower(), oid, delivery_hash, self.clock))
        tx_hash = "0x" + secrets.token_hex(32)
        if self.claim_status == 1:
            old = self.orders[(vault.lower(), oid)]
            self.orders[(vault.lower(), oid)] = dataclasses.replace(
                old, claimed_at=self.clock, delivery_hash=delivery_hash
            )
        return TxResult(
            hash=tx_hash,
            status=self.claim_status,
            block=1,
            gas_used=50_000,
            function="claimDelivered",
            from_address=party.address,
            to=vault.lower(),
            nonce=0,
            chain_id=self.chain_id,
            expected_status=1,
            revert_error=None if self.claim_status == 1 else "AlreadyClaimed(0x00)",
            logs=(),
        )


@dataclass
class FakeInbox:
    """Captures what the simulator pushes to the wallet agent's inbox; answers like the agent (8.5)."""

    posts: list[tuple[str, dict]] = field(default_factory=list)

    def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        self.posts.append((path, json.loads(json.dumps(dict(body)))))
        return {"url": f"http://127.0.0.1:8701{path}", "statusCode": 200, "body": {"status": "accepted"}}


def quote_body(sku: str, quantity: int, vault: str = WALLET_VAULT, buyer_agent_id: int = 0, cap_of: str = "") -> dict:
    return {
        "quoteRequestId": "qr-" + secrets.token_hex(16),
        "buyerAgentId": str(buyer_agent_id),
        "buyerVault": vault,
        "sku": sku,
        "quantity": quantity,
        "capOf": cap_of,
    }


# ---------------------------------------------------------------------------
# V01-V09 (spec 6.2), written from the spec, independent of the simulator code
# ---------------------------------------------------------------------------

TOP_KEYS = {
    "schema",
    "invoiceId",
    "quoteRequestId",
    "capOf",
    "issuerAgentId",
    "buyerAgentId",
    "buyerVault",
    "chainId",
    "token",
    "items",
    "amount",
    "issuedAt",
    "expiresAt",
    "deliveryDeadline",
    "memo",
    "signature",
}
ITEM_KEYS = {"sku", "description", "unitPrice", "quantity"}
PRINTABLE = re.compile(r"^[\x20-\x7e]*$")
DECIMAL = re.compile(r"^[0-9]{1,12}$")


@dataclass(frozen=True)
class QuoteReq:
    quote_request_id: str
    issuer_agent_id: int
    sku: str
    quantity: int
    cap_of: str = ""


def check_invoice(
    raw: Mapping[str, Any],
    *,
    now: int,
    chain_id: int,
    usdc: str,
    vault: str,
    vault_agent_id: int,
    catalogue: Mapping[int, Mapping[str, int]],
    quote_requests: list[QuoteReq],
    pinned_wallets: Mapping[int, str],
    live_wallets: Mapping[int, str],
    max_delivery_s: int = 600,
) -> str | None:
    """None if the invoice passes V01-V09, else the first failing rule and why."""
    if len(canonical_bytes(dict(raw))) > 16_384:
        return "V01 size"
    if raw.get("schema") != "acb-invoice/1" or set(raw) != TOP_KEYS:
        return "V01 schema/keys"
    items = raw["items"]
    if not isinstance(items, list) or not all(isinstance(i, dict) and set(i) == ITEM_KEYS for i in items):
        return "V01 items"
    for key in (
        "invoiceId",
        "quoteRequestId",
        "capOf",
        "issuerAgentId",
        "buyerAgentId",
        "buyerVault",
        "token",
        "amount",
    ):
        if not isinstance(raw[key], str) or not PRINTABLE.fullmatch(raw[key]):
            return f"V01 {key}"
    for item in items:
        if not isinstance(item["sku"], str) or not PRINTABLE.fullmatch(item["sku"]):
            return "V01 sku"
    try:
        SignedInvoice.from_wire(dict(raw))
    except ValueError as exc:
        return f"V01 canonical: {exc}"
    if not re.fullmatch(r"^[A-Za-z0-9-]{1,40}$", raw["invoiceId"]):
        return "V02 invoiceId"
    if raw["quoteRequestId"] and not re.fullmatch(r"^qr-[0-9a-f]{32}$", raw["quoteRequestId"]):
        return "V02 quoteRequestId"
    if raw["capOf"] and not re.fullmatch(r"^[A-Za-z0-9-]{1,40}$", raw["capOf"]):
        return "V02 capOf"
    if not 1 <= len(items) <= 6:
        return "V03 items"
    total = 0
    for item in items:
        if not DECIMAL.fullmatch(item["unitPrice"]) or int(item["unitPrice"]) % 10_000:
            return "V03 unitPrice"
        if (
            not isinstance(item["quantity"], int)
            or isinstance(item["quantity"], bool)
            or not 1 <= item["quantity"] <= 1000
        ):
            return "V03 quantity"
        total += int(item["unitPrice"]) * item["quantity"]
    if not DECIMAL.fullmatch(raw["amount"]) or int(raw["amount"]) % 10_000 or int(raw["amount"]) != total:
        return "V03 amount"
    if int(raw["amount"]) < 100_000:
        return "V03 min amount"
    issuer = int(raw["issuerAgentId"])
    cat = catalogue.get(issuer, {})
    for item in items:
        if item["sku"] not in cat or int(item["unitPrice"]) != cat[item["sku"]]:
            return "V04 catalogue"
        if len(item["description"]) > 80:
            return "V04 description"
    if len(raw["memo"]) > 500:
        return "V04 memo"
    if raw["chainId"] != chain_id or raw["token"] != usdc.lower():
        return "V05"
    issued, expires, deadline = raw["issuedAt"], raw["expiresAt"], raw["deliveryDeadline"]
    if issued > now + 30 or expires - issued > 900 or now > expires:
        return "V06 validity"
    if deadline < issued or deadline - issued > max_delivery_s:
        return "V06 deadline"
    if int(raw["buyerAgentId"]) != vault_agent_id or raw["buyerVault"] != vault.lower():
        return "V07"
    match = [
        q
        for q in quote_requests
        if q.quote_request_id == raw["quoteRequestId"]
        and q.issuer_agent_id == issuer
        and len(items) == 1
        and q.sku == items[0]["sku"]
        and q.quantity == items[0]["quantity"]
        and q.cap_of == raw["capOf"]
    ]
    if not match:
        return "V08 solicited"
    signer = recover_invoice_signer(raw)
    if signer != pinned_wallets.get(issuer, "").lower() or signer != live_wallets.get(issuer, "").lower():
        return "V09 signature"
    return None


def policy_validate(raw: Mapping[str, Any], **ctx: Any) -> Any:
    """Run WP5's ``acb.policy.validate_invoice`` with a matching context (only when it exists)."""
    import pytest

    policy = pytest.importorskip("acb.policy", reason="acb.policy (WP5) not built yet")
    from acb.types import QuoteRequest, ValidationContext

    qrs = tuple(
        QuoteRequest(
            quote_request_id=q.quote_request_id,
            mandate_id="0x" + "77" * 32,
            issuer_agent_id=q.issuer_agent_id,
            sku=q.sku,
            quantity=q.quantity,
            cap_of=q.cap_of,
            created_at=ctx["now"] - 5,
            closes_at=ctx["now"] + 1200,
            used_by=None,
        )
        for q in ctx["quote_requests"]
    )
    vctx = ValidationContext(
        now=ctx["now"],
        chain_id=ctx["chain_id"],
        usdc=ctx["usdc"],
        vault_address=ctx["vault"],
        vault_agent_id=ctx["vault_agent_id"],
        catalogue={k: dict(v) for k, v in ctx["catalogue"].items()},
        quote_requests=qrs,
        superseded={},
        pinned_wallets=dict(ctx["pinned_wallets"]),
        live_wallets=dict(ctx["live_wallets"]),
    )
    cfg = policy.PolicyConfig.load() if hasattr(policy, "PolicyConfig") else None
    return policy.validate_invoice(dict(raw), vctx, cfg)


# ---------------------------------------------------------------------------
# a simulator over the fake chain (imported by conftest.py and the tests; a test module must not import
# "conftest", whose module name clashes with the other test folders' conftest.py)
# ---------------------------------------------------------------------------


@dataclass
class FakeSim:
    sim: Any
    chain: FakeChain
    inbox: FakeInbox
    keys: KeyRing


def make_fake_sim(tmp_path, chain: FakeChain | None = None, network: str = "anvil") -> FakeSim:
    from acb.sim.core import Simulator

    keys = KeyRing.anvil()
    dep = fake_deployments(keys)
    chain = chain or FakeChain()
    chain.vaults.setdefault(WALLET_VAULT, 0)
    chain.vaults.setdefault(BEACON_VAULT, dep.agents["beacon"].agent_id)
    net = NetworkProfile.load(
        network, {"ANVIL_RPC_URL": "http://127.0.0.1:1", "BASE_SEPOLIA_RPC_URL": "http://127.0.0.1:1"}
    )
    inbox = FakeInbox()
    sim = Simulator(net, dep, keys, chain=chain, out_dir=tmp_path, inbox=inbox, evidence_base="evidence/anvil/")
    return FakeSim(sim, chain, inbox, keys)
