"""Anvil-only attack actions on the bureau (spec 9.4, 12.3 A36-A43, A59).

The harness of WP9 calls them through ``POST /admin/run`` and measures the effect on grades; each
action only produces the on-chain record an attacker could produce and returns what it did
(identities, vaults, order ids, transactions and expected reverts).

Attack identities and vault owners are **extra accounts** derived from the public Anvil test mnemonic
at ``attacks.key_index_base`` (100) and above, funded with ``anvil_setBalance``. The role map of 13.1
has too few sybil keys for rings with distinct owners (every vault owner must differ from every
identity owner, or the bureau merges the cluster). Attack vaults are TierVaults of the ``market``
profile built with WP3's :func:`acb.chain.deploy.vault_init`, registered with ``registerVault`` and
funded by minting MockUSDC (the deployer is the minter on Anvil). They are recorded in the simulator
state, not in ``deployments/anvil.json``.

- ``wash_round_same_owner`` (A36a): one round of a ring echo1 -> echo2 -> echo3 -> echo1 through three
  vaults that share one owner (``echo1_vault_owner``): self-dealing for the bureau.
- ``wash_round_distinct`` (A36b): the same ring through the Echo vaults of the deployment (distinct
  owners): circular volume.
- ``dust_ring`` (A42): five sybil payers with distinct owners each pay a sybil subject 0.10 per order.
- ``sybil_failed_orders`` (A39): two sybils buy from Atlas for real, Atlas delivers, the sybils record
  FAILED after the deadline.
- ``dust_smear`` (A40): a sybil escrows 0.01 to Atlas without an invoice and refunds it (2400) after the
  deadline; then it escrows 0.20 against a real Atlas invoice and tries to refund early
  (``EscrowNotExpired``) and after Atlas's claim (``DeliveryClaimed``).
- ``sybil_reports`` (A41, A38): sybils record a free ``recordDecision(REFUSE)`` and file
  INJECTION_ATTEMPT / SPAM reports without signed evidence against Atlas and the wallet;
  ``replay: true`` posts every report twice.
- ``fake_vault`` (A37, A43): ``registerVault`` of a contract with other bytecode (``BadVaultCode``) and of
  same-codehash TierVaults with a fake token, bureau or identity (``BadVaultConfig``).
- ``bust_out`` (A59): ``phase=build`` makes a provider build a record with Beacon (on-time orders under
  its own Beacon mandate); ``phase=default`` makes it never deliver again; ``phase=reset`` clears it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from eth_account.signers.local import LocalAccount

from acb.canonical import chash
from acb.chain.contracts import DecisionParams, OutflowParams
from acb.sim import market
from acb.sim.core import Party, SimError, Simulator
from acb.sim.market import Action, Buyer, Slot, contracts, drive_slots, ensure_mandate, file_report, send
from acb.types import Decision, Outcome

ETH_BALANCE = 10**20
DAY = 24 * 3600
WEEK = 7 * DAY


def _require_anvil(sim: Simulator) -> None:
    if not sim.is_anvil:
        raise SimError(409, {"status": "anvil_only"})


def _cfg(sim: Simulator, name: str) -> dict[str, Any]:
    return dict(sim.cfg.attacks.get(name) or {})


def _trade(sim: Simulator) -> tuple[str, int, str]:
    spec = sim.cfg.attacks.get("trade_sku") or {}
    return str(spec.get("sku", "research-note")), int(spec.get("unit_price", 100_000)), str(spec.get("description", ""))


def _round(sim: Simulator, action: str) -> int:
    return len(sim.state.data["rounds"].get(action, [])) + 1


def _record_round(sim: Simulator, action: str, order_ids: Iterable[str]) -> None:
    with sim.state.edit() as data:
        data["rounds"].setdefault(action, []).append(list(order_ids))


# ---------------------------------------------------------------------------
# attack identities and vaults
# ---------------------------------------------------------------------------


def new_key(sim: Simulator) -> tuple[int, LocalAccount]:
    """The next extra account (index ``key_index_base + n``), funded with ETH on Anvil."""
    _require_anvil(sim)
    base = int(sim.cfg.attacks.get("key_index_base", 100))
    index = base + sim.state.bump("_attack", "keys") - 1
    account = sim.extra_account(index)
    response = sim.w3.provider.make_request("anvil_setBalance", [account.address, hex(ETH_BALANCE)])
    if "error" in response:
        raise SimError(500, {"status": "anvil_setBalance failed"})
    return index, account


def ensure_entity(sim: Simulator, label: str, *, name: str | None = None) -> Party:
    """An attack identity registered on ERC-8004 from its own extra key (labelled as simulated)."""
    from acb.chain.deploy import Persona, agent_uri

    if label in sim.parties:
        return sim.parties[label]
    index, account = new_key(sim)
    persona = Persona(label, name or label, "research", {}, False, "Attack harness identity.")
    identity = contracts(sim).identity
    result = send(sim, account, identity.register(agent_uri(label, persona)))
    agent_id = identity.agent_id_from_result(result)
    with sim.state.edit() as data:
        data["entities"][label] = {
            "agentId": agent_id,
            "keyIndex": index,
            "address": account.address.lower(),
            "name": name or label,
            "registerTx": result.hash,
        }
    party = Party(label, agent_id, account, name or label)
    sim.parties[label] = party
    return party


def fund_vault(sim: Simulator, address: str, amount: int) -> str | None:
    """Mint MockUSDC so the vault holds at least ``amount`` (Anvil only)."""
    _require_anvil(sim)
    usdc = contracts(sim).usdc
    have = usdc.balance_of(address)
    if have >= amount:
        return None
    return send(sim, sim.keys.account("deployer"), usdc.mint(address, amount - have)).hash


def ensure_vault(
    sim: Simulator,
    name: str,
    *,
    identity: Party,
    agent: LocalAccount,
    owner: LocalAccount,
    registrant: LocalAccount | None = None,
    overrides: dict[str, Any] | None = None,
    register: bool = True,
    fund: bool = True,
) -> Buyer:
    """A ``market``-profile TierVault for ``identity`` (deploy, ``registerVault``, MockUSDC)."""
    from acb.chain.deploy import load_tiers, vault_init

    _require_anvil(sim)
    rec = sim.state.data["vaults"].get(name)
    if rec is None:
        tiers = load_tiers(sim.net.profile)
        init = vault_init(
            sim.dep,
            tiers.profiles["market"],
            owner=owner.address.lower(),
            agent=agent.address.lower(),
            autonomy_operator=sim.keys.address("autonomy"),
            agent_id=identity.agent_id,
        )
        if overrides:
            init = dataclasses.replace(init, **overrides)
        deployed = sim.sender(sim.keys.account("deployer")).deploy("TierVault", init.as_tuple())
        rec = {
            "address": deployed.to,
            "agentId": identity.agent_id,
            "identity": identity.key,
            "owner": owner.address.lower(),
            "agent": agent.address.lower(),
            "deployTx": deployed.hash,
            "registerTx": None,
        }
        with sim.state.edit() as data:
            data["vaults"][name] = rec
    if register and not rec.get("registerTx"):
        result = send(sim, registrant or agent, contracts(sim).acb.register_vault(rec["address"]))
        with sim.state.edit() as data:
            data["vaults"][name]["registerTx"] = result.hash
    if fund:
        fund_vault(sim, rec["address"], int(sim.cfg.attacks.get("vault_funding", 5_000_000)))
    return market.buyer(sim, name)


def sybil(sim: Simulator, label: str) -> tuple[Party, Buyer]:
    """A sybil with its own identity key (also the vault agent) and a distinct vault owner key."""
    party = ensure_entity(sim, label)
    rec = sim.state.data["vaults"].get(label)
    if rec is not None:
        return party, market.buyer(sim, label)
    _, owner = new_key(sim)
    return party, ensure_vault(sim, label, identity=party, agent=party.account, owner=owner)


def _mandate(sim: Simulator, b: Buyer, label: str, payees: list[Party], total: int) -> str:
    mandate_id, _ = ensure_mandate(
        sim,
        b,
        label,
        payees,
        max_per_tx=1_000_000,
        max_total=max(total, 1_000_000),
        valid_s=WEEK,
        purpose=f"attack harness: {label}",
        need=0,
    )
    return mandate_id


# ---------------------------------------------------------------------------
# wash rings (A36a, A36b)
# ---------------------------------------------------------------------------


ECHOES = ("echo1", "echo2", "echo3")


def _ring_round(sim: Simulator, action: str, vault_names: list[str], label: str) -> dict[str, Any]:
    sku, unit, description = _trade(sim)
    per_round = int(_cfg(sim, "wash").get("orders_per_round", 1))
    number = _round(sim, action)
    orders = []
    for i, name in enumerate(vault_names):
        b = market.buyer(sim, name)
        provider = sim.party(ECHOES[(i + 1) % len(ECHOES)])
        mandate_id = _mandate(sim, b, label, [provider], 20 * unit)
        slots = [
            Slot(f"{label}:{number}:{i}:{k}", provider.key, sku, 1, "escrow", unit, description)
            for k in range(1, per_round + 1)
        ]
        orders += drive_slots(sim, b, mandate_id, slots)
    _record_round(sim, action, [o["orderId"] for o in orders])
    return {"round": number, "vaults": vault_names, "orders": orders}


def wash_round_same_owner(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """A36a: ring of three vaults with one owner (echo1_vault_owner); one round per call."""
    _require_anvil(sim)
    owner = sim.keys.account("echo1_vault_owner")
    names = []
    for key in ECHOES:
        party = sim.party(key)
        name = f"washA-{key}"
        ensure_vault(sim, name, identity=party, agent=party.account, owner=owner)
        names.append(name)
    return _ring_round(sim, "wash_round_same_owner", names, "washA")


def wash_round_distinct(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """A36b: ring through the Echo vaults of the deployment (three distinct owners); one round per call."""
    _require_anvil(sim)
    for key in ECHOES:
        if key not in sim.dep.vaults:
            raise SimError(409, {"status": "missing_vault", "vault": key})
        fund_vault(sim, sim.dep.vaults[key].address, int(sim.cfg.attacks.get("vault_funding", 5_000_000)))
    return _ring_round(sim, "wash_round_distinct", list(ECHOES), "washB")


# ---------------------------------------------------------------------------
# dust self-trade (A42), sybil failures (A39), dust smear (A40)
# ---------------------------------------------------------------------------


def dust_ring(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """A42: ``payers`` sybils with distinct owners each pay the sybil subject ``orders`` x 0.10."""
    _require_anvil(sim)
    spec = _cfg(sim, "dust_ring")
    payers = int(args.get("payers", spec.get("payers", 5)))
    orders_per = int(args.get("orders", spec.get("orders_per_payer", 4)))
    kind = str(args.get("kind", spec.get("kind", "pay")))
    if kind not in ("pay", "escrow"):
        raise ValueError("kind must be pay or escrow")
    sku, unit, description = _trade(sim)
    subject = ensure_entity(sim, "dust-subject", name="Sybil X")
    number = _round(sim, "dust_ring")
    orders = []
    for p in range(1, payers + 1):
        _, b = sybil(sim, f"dust-p{p}")
        mandate_id = _mandate(sim, b, "dust", [subject], 40 * unit)
        slots = [
            Slot(f"dust:{number}:{p}:{k}", subject.key, sku, 1, kind, unit, description)
            for k in range(1, orders_per + 1)
        ]
        orders += drive_slots(sim, b, mandate_id, slots)
    _record_round(sim, "dust_ring", [o["orderId"] for o in orders])
    return {"subject": {"key": subject.key, "agentId": str(subject.agent_id)}, "orders": orders}


def sybil_failed_orders(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """A39: NEW sybils (distinct owners) pay Atlas, Atlas delivers, the sybils record FAILED after the deadline."""
    from acb.chain.clock import advance_to

    _require_anvil(sim)
    spec = _cfg(sim, "sybil_failed_orders")
    provider = sim.party(str(args.get("provider", spec.get("provider", "atlas"))))
    sku = str(args.get("sku", spec.get("sku", "fx-live-15m")))
    quantity = int(args.get("quantity", spec.get("quantity", 1)))
    count = int(args.get("sybils", spec.get("sybils", 2)))
    number = _round(sim, "sybil_failed_orders")
    tickets = []
    for k in range(1, count + 1):
        _, b = sybil(sim, f"failer-{k}")
        mandate_id = _mandate(sim, b, f"fail-{provider.key}", [provider], 1_000_000)
        slot = Slot(f"fail:{number}:{k}", provider.key, sku, quantity, "pay")
        ticket = market.open_slot(sim, b, mandate_id, slot)
        answer = market.ask_delivery(sim, ticket)
        tickets.append((b, ticket, answer.get("status")))
    deadline = max(sim.chain.order(b.address, t["orderId"]).deadline for b, t, _ in tickets)
    advance_to(sim.w3, deadline + 1, timeout_s=3600.0)
    out = []
    for b, ticket, delivered in tickets:
        vault = contracts(sim).vault_at(b.address)
        fn = vault.record_delivery(ticket["orderId"], int(Outcome.FAILED), chash({"failed": ticket["orderId"]}))
        result = send(sim, b.agent, fn)
        order = vault.get_order(ticket["orderId"])
        out.append(
            {
                "sybil": b.name,
                "orderId": ticket["orderId"],
                "providerDelivered": delivered == "delivered",
                "recordTx": result.hash,
                "outcome": order.outcome.name,
            }
        )
    _record_round(sim, "sybil_failed_orders", [o["orderId"] for o in out])
    return {"provider": provider.key, "orders": out}


def dust_smear(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """A40: a dust escrow refunded 2400 (not counted: dust, no invoice) and a 0.20 escrow that cannot be
    refunded early (``EscrowNotExpired``) nor after the provider's claim (``DeliveryClaimed``)."""
    from acb.chain.abi import error_name
    from acb.chain.clock import advance_to

    _require_anvil(sim)
    spec = _cfg(sim, "dust_smear")
    provider = sim.party(str(args.get("provider", spec.get("provider", "atlas"))))
    dust = int(args.get("dustAmount", spec.get("dust_amount", 10_000)))
    _, b = sybil(sim, "duster")
    mandate_id = _mandate(sim, b, f"dust-{provider.key}", [provider], 2_000_000)
    vault = contracts(sim).vault_at(b.address)
    number = _round(sim, "dust_smear")

    # 1. dust escrow with no invoice behind it; refunded 2400 after the deadline
    dust_order = chash({"schema": "acb-sim-dust/1", "round": number, "vault": b.address})
    now = sim.now()
    params = OutflowParams(
        order_id=dust_order,
        payee_agent_id=provider.agent_id,
        amount=dust,
        deadline=now + 60,
        mandate_id=mandate_id,
        invoice_hash=chash({"schema": "acb-sim-no-invoice/1", "orderId": dust_order}),
        decision_hash=chash({"schema": "acb-sim-decision/1", "orderId": dust_order}),
        decision=int(Decision.ESCROW),
        reason_code=2004,
    )
    opened = send(sim, b.agent, vault.escrow(params))
    early = send(sim, b.agent, vault.refund(dust_order, 2400), expect_revert=True)
    advance_to(sim.w3, now + 61, timeout_s=600.0)
    refunded = send(sim, b.agent, vault.refund(dust_order, 2400))

    # 2. a real 0.20 order: Atlas claims before it hands over the data
    slot = Slot(f"dustsmear:{number}", provider.key, str(spec.get("sku", "fx-live-15m")), int(spec.get("quantity", 1)))
    ticket = market.open_slot(sim, b, mandate_id, slot)
    early_real = send(sim, b.agent, vault.refund(ticket["orderId"], 2400), expect_revert=True)
    answer = market.ask_delivery(sim, ticket)
    order = vault.get_order(ticket["orderId"])
    advance_to(sim.w3, order.deadline + 1, timeout_s=3600.0)
    after_claim = send(sim, b.agent, vault.refund(ticket["orderId"], 2400), expect_revert=True)
    _record_round(sim, "dust_smear", [dust_order, ticket["orderId"]])
    return {
        "provider": provider.key,
        "dust": {
            "orderId": dust_order,
            "amount": str(dust),
            "openTx": opened.hash,
            "earlyRefund": {"tx": early.hash, "status": early.status, "error": error_name(early.revert_error)},
            "refundTx": refunded.hash,
            "outcome": vault.get_order(dust_order).outcome.name,
        },
        "real": {
            "orderId": ticket["orderId"],
            "earlyRefund": {
                "tx": early_real.hash,
                "status": early_real.status,
                "error": error_name(early_real.revert_error),
            },
            "delivery": answer.get("status"),
            "claimTx": answer.get("claimTx"),
            "refundAfterClaim": {
                "tx": after_claim.hash,
                "status": after_claim.status,
                "error": error_name(after_claim.revert_error),
            },
            "state": vault.get_order(ticket["orderId"]).state.name,
        },
    }


# ---------------------------------------------------------------------------
# fake accusations (A41, A38)
# ---------------------------------------------------------------------------


def _agent_id_of(sim: Simulator, key: str) -> int:
    if key in sim.parties:
        return sim.parties[key].agent_id
    if key in sim.dep.agents:
        return int(sim.dep.agents[key].agent_id)
    raise SimError(404, {"status": "unknown_party", "party": key})


def sybil_reports(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """A41 (and A38 with ``replay``): sybils back INJECTION_ATTEMPT / SPAM reports with a free
    ``recordDecision(REFUSE)`` and no signed evidence."""
    _require_anvil(sim)
    spec = _cfg(sim, "sybil_reports")
    subjects = [str(s) for s in args.get("subjects", spec.get("subjects", ["atlas", "wallet"]))]
    kinds = [str(k) for k in args.get("kinds", spec.get("kinds", ["INJECTION_ATTEMPT", "SPAM"]))]
    count = int(args.get("sybils", spec.get("sybils", 2)))
    replay = bool(args.get("replay", False))
    number = _round(sim, "sybil_reports")
    out = []
    for k in range(1, count + 1):
        party, b = sybil(sim, f"accuser-{k}")
        vault = contracts(sim).vault_at(b.address)
        for subject in subjects:
            subject_id = _agent_id_of(sim, subject)
            for kind in kinds:
                order_id = chash(
                    {
                        "schema": "acb-sim-accusation/1",
                        "round": number,
                        "by": party.key,
                        "subject": subject,
                        "kind": kind,
                    }
                )
                decision_tx = None
                if kind == "INJECTION_ATTEMPT":
                    params = DecisionParams(
                        order_id=order_id,
                        payee_agent_id=subject_id,
                        amount=200_000,
                        decision=int(Decision.REFUSE),
                        reason_code=2108,
                        invoice_hash=chash({"schema": "acb-sim-no-invoice/1", "orderId": order_id}),
                        decision_hash=chash({"schema": "acb-sim-decision/1", "orderId": order_id}),
                    )
                    decision_tx = send(sim, b.agent, vault.record_decision(params)).hash
                evidence = {"type": "none", "note": "no signed invoice"}
                txs = []
                for _ in range(2 if replay else 1):
                    tx, feedback_hash = file_report(
                        sim,
                        party,
                        subject_agent_id=subject_id,
                        kind=kind,
                        order_id=order_id,
                        vault=b.address,
                        evidence=evidence,
                    )
                    txs.append(tx.hash)
                out.append(
                    {
                        "reporter": party.key,
                        "subject": subject,
                        "kind": kind,
                        "orderId": order_id,
                        "decisionTx": decision_tx,
                        "reportTxs": txs,
                        "feedbackHash": feedback_hash,
                    }
                )
    return {"reports": out, "replay": replay}


# ---------------------------------------------------------------------------
# fake vaults (A37, A43)
# ---------------------------------------------------------------------------

FAKE_VARIANTS = ("bytecode", "token", "bureau", "identity")


def fake_vault(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """A37/A43: ``registerVault`` of a contract with other bytecode and of same-codehash vaults whose
    token, bureau or identity is fake. Every registration is sent and is expected to revert."""
    from acb.chain.abi import error_name

    _require_anvil(sim)
    wanted = args.get("variants", list(FAKE_VARIANTS))
    if isinstance(wanted, str):
        wanted = [wanted]
    unknown = set(wanted) - set(FAKE_VARIANTS)
    if unknown:
        raise ValueError(f"unknown variants {sorted(unknown)}")
    deployer = sim.sender(sim.keys.account("deployer"))
    faker = ensure_entity(sim, "faker", name="Fake Vault Owner")
    fake = deployer.deploy("MockUSDC", sim.keys.checksum_address("deployer"))
    out: dict[str, Any] = {"fakeContract": fake.to}
    acb = contracts(sim).acb
    for variant in wanted:
        if variant == "bytecode":
            address = fake.to
        else:
            field = {"token": "token", "bureau": "bureau", "identity": "identity"}[variant]
            _, owner = new_key(sim)
            b = ensure_vault(
                sim,
                f"fake-{variant}-{_round(sim, 'fake_vault')}",
                identity=faker,
                agent=faker.account,
                owner=owner,
                overrides={field: fake.to},
                register=False,
                fund=False,
            )
            address = b.address
        result = send(sim, faker.account, acb.register_vault(address), expect_revert=True)
        out[variant] = {
            "vault": address,
            "registerTx": result.hash,
            "status": result.status,
            "error": error_name(result.revert_error),
            "registered": acb.is_vault(address),
        }
    _record_round(sim, "fake_vault", [])
    return out


# ---------------------------------------------------------------------------
# bust-out (A59)
# ---------------------------------------------------------------------------


def bust_out(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """A59: ``build`` (record with Beacon, delivered on time), ``default`` (never delivers again) or
    ``reset`` (back to the persona's script)."""
    _require_anvil(sim)
    spec = _cfg(sim, "bust_out")
    persona = str(args.get("persona", spec.get("persona", "nova")))
    party = sim.party(persona)
    phase = str(args.get("phase", "build"))
    if phase == "default":
        with sim.state.edit() as data:
            data["overrides"][persona] = "default"
        return {"persona": persona, "override": "default"}
    if phase == "reset":
        with sim.state.edit() as data:
            data["overrides"].pop(persona, None)
        return {"persona": persona, "override": None}
    if phase != "build":
        raise ValueError("phase must be build, default or reset")
    with sim.state.edit() as data:
        data["overrides"][persona] = "honest"
    orders = int(args.get("orders", spec.get("orders", 5)))
    sku = str(args.get("sku") or next(iter(sim.catalogue(persona))))
    unit = sim.catalogue(persona)[sku][0]
    b = market.buyer(sim, sim.cfg.market.get("buyer", "beacon"))
    mandate_id = _mandate(sim, b, f"bust-{persona}", [party], orders * unit)
    number = _round(sim, "bust_out")
    slots = [Slot(f"bust:{persona}:{number}:{i}", persona, sku, 1) for i in range(1, orders + 1)]
    result = drive_slots(sim, b, mandate_id, slots)
    _record_round(sim, "bust_out", [o["orderId"] for o in result])
    return {"persona": persona, "override": "honest", "orders": result}


ATTACK_ACTIONS: dict[str, Action] = {
    "wash_round_same_owner": wash_round_same_owner,
    "wash_round_distinct": wash_round_distinct,
    "dust_ring": dust_ring,
    "sybil_failed_orders": sybil_failed_orders,
    "sybil_reports": sybil_reports,
    "fake_vault": fake_vault,
    "bust_out": bust_out,
    "dust_smear": dust_smear,
}
"""Anvil-only actions (spec 9.4 list, plus ``dust_smear`` for A40)."""


__all__ = [
    "ATTACK_ACTIONS",
    "FAKE_VARIANTS",
    "ensure_entity",
    "ensure_vault",
    "fund_vault",
    "new_key",
    "sybil",
]
