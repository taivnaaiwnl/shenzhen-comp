"""Owner confirmations of ``acb chat`` (spec 11.3: the terminal fallback of the owner console).

This module runs in the owner's own CLI process, never in the agent (P2): it loads the owner key with
:func:`acb.keys.load_role_key` (which refuses inside the agent process). Like the owner console it
rebuilds every confirmation from the chain and from signatures it verifies itself, never from text the
agent supplied, and signs exactly what it displays (TB7). Confirming is two steps:

1. the owner types the code of the agent's card (that only selects the card);
2. :func:`prepare_card` rebuilds the values from the chain, :func:`render_verified` prints them with a
   **new** code computed from exactly those values, and only when the owner types that second code does
   :func:`execute_prepared` send them. Anything else cancels; nothing is signed.

What is rebuilt:

- **mandate**: from the agent's draft only the agentIds, the numbers and the purpose are taken; each
  payee must be a provider of the pinned directory; its name comes from the pinned directory, its live
  ``getAgentWallet``, effective grade, registration time and lookalike warning are read again;
  ``registerMandate`` is sent with exactly those values and a random salt, then the agent is told
  (``/mandates/activated``), which verifies the mandate on-chain itself;
- **approval**: the issuer must be a provider of the pinned directory; the signed invoice is verified
  against the issuer's live wallet; its buyer fields, chain and token must be ours and it must be
  unexpired; the order id is recomputed; the vault must have recorded ``DecisionRecorded`` with
  NEEDS_OWNER_APPROVAL for that order, invoice hash, payee and amount, the order must not exist yet and
  no unused approval may exist. ``ownerApprove(orderId, issuerAgentId, liveWallet, amount, expiry,
  asEscrow, deadline)`` uses only those values (escrow for a payee graded C, NEW or NONE), then the agent
  is asked to execute it (``/approvals/{orderId}/executed-request``).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any

APPROVAL_TTL_S = 900
ESCROW_MIN_S = 300


def _owner_account(network: str) -> Any:
    from acb.keys import KeyRing, load_role_key

    try:
        return load_role_key("owner")
    except KeyError:
        if network == "anvil":
            return KeyRing.anvil().account("owner")
        raise


def _chain(network: str | None) -> tuple[Any, Any, Any, str]:
    from acb import config
    from acb.chain.client import make_web3
    from acb.chain.contracts import bind
    from acb.chain.deployments import load_deployments
    from acb.types import NetworkProfile

    name = network or config.network_name()
    net = NetworkProfile.load(name)
    w3 = make_web3(net)
    dep = load_deployments(name)
    return w3, dep, bind(w3, dep), name


def _registered_at(w3: Any, dep: Any, agent_id: int) -> int | None:
    """Block time of the agent's ERC-8004 registration (``registerTx`` of the deployment), or None."""
    for entry in dep.agents.values():
        if int(entry.agent_id) == int(agent_id) and entry.register_tx:
            try:
                receipt = w3.eth.get_transaction_receipt(entry.register_tx)
                return int(w3.eth.get_block(receipt["blockNumber"])["timestamp"])
            except Exception:  # noqa: BLE001 - unknown on this node: shown as unknown
                return None
    return None


def mandate_from_draft(card: dict[str, Any], *, w3: Any, dep: Any, c: Any) -> dict[str, Any]:
    """The values to register for a draft card, re-read from the chain (raises ``ValueError`` on a problem)."""
    from acb.agent.directory import Directory
    from acb.canonical import chash
    from acb.chain import clock
    from acb.chain.contracts import ZERO_ADDRESS
    from acb.types import Grade

    directory = Directory.from_deployments(dep)
    vault = c.vault("wallet")
    try:
        ids = [int(p["agentId"]) for p in card.get("payees") or []]
    except (KeyError, TypeError, ValueError):
        raise ValueError("the draft's payees are malformed") from None
    if not 1 <= len(ids) <= 6 or len(set(ids)) != len(ids):
        raise ValueError("a mandate needs 1 to 6 distinct payees")
    now = clock.now(w3)
    rows, wallets = [], []
    max_age = vault.max_grade_age()
    for aid in ids:
        entry = directory.get(aid)
        if entry is None or entry not in directory.providers:
            raise ValueError(f"agent #{aid} is not a provider of the pinned directory")
        wallet = c.identity.get_agent_wallet(aid).lower()
        if not wallet or wallet == ZERO_ADDRESS:
            raise ValueError(f"agent #{aid} has no agent wallet")
        grade = Grade(c.acb.effective_grade(aid, max_age))
        registered = _registered_at(w3, dep, aid)
        wallets.append(wallet)
        rows.append(
            {
                "agentId": aid,
                "name": entry.name,
                "wallet": wallet,
                "grade": grade.name,
                "registeredAt": registered,
                "ageS": (now - registered) if registered is not None else None,
                "lookalike": directory.lookalike_of(aid),
            }
        )
    try:
        per_tx, total = int(card["maxPerTx"]), int(card["maxTotal"])
        hours = int(card["validHours"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("the draft's numbers are malformed") from None
    if not 0 < per_tx <= total or not 1 <= hours <= 168:
        raise ValueError("draft numbers are out of range")
    valid_until = now + hours * 3600
    purpose = str(card.get("purpose", ""))[:300]
    purpose_hash = chash({"purpose": purpose})
    salt = "0x" + secrets.token_hex(32)
    mandate_id = chash(
        {
            "vault": vault.address,
            "payeeAgentIds": [str(i) for i in ids],
            "payeeWallets": wallets,
            "maxPerTx": str(per_tx),
            "maxTotal": str(total),
            "validUntil": valid_until,
            "recurring": bool(card.get("recurring")),
            "purposeHash": purpose_hash,
            "salt": salt,
        }
    )
    return {
        "mandateId": mandate_id,
        "ids": ids,
        "wallets": wallets,
        "rows": rows,
        "maxPerTx": per_tx,
        "maxTotal": total,
        "validUntil": valid_until,
        "recurring": bool(card.get("recurring")),
        "purpose": purpose,
        "purposeHash": purpose_hash,
    }


def _needs_owner_record(c: Any, dep: Any, oid: str, invoice_hash: str, issuer: int, amount: int) -> bool:
    """True when our vault recorded ``DecisionRecorded(NEEDS_OWNER_APPROVAL)`` for exactly this invoice."""
    from web3 import Web3

    from acb.types import Decision

    vault = c.vault("wallet")
    for log in vault.logs("DecisionRecorded", int(dep.start_block), "latest", orderId=oid):
        args = log["args"]
        if (
            Web3.to_hex(args["orderId"]).lower() == oid
            and int(args["decision"]) == int(Decision.NEEDS_OWNER_APPROVAL)
            and Web3.to_hex(args["invoiceHash"]).lower() == invoice_hash.lower()
            and int(args["payeeAgentId"]) == int(issuer)
            and int(args["amount"]) == int(amount)
        ):
            return True
    return False


def approval_from_card(card: dict[str, Any], *, w3: Any, dep: Any, c: Any) -> dict[str, Any]:
    """The values to approve for an approval card, verified against the chain (``ValueError`` otherwise)."""
    from acb.agent.directory import Directory
    from acb.canonical import order_id, recover_invoice_signer
    from acb.chain import clock
    from acb.chain.contracts import ZERO_ADDRESS
    from acb.types import Grade, OrderState, SignedInvoice

    try:
        si = SignedInvoice.model_validate(card["invoice"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("the card carries no valid signed invoice") from None
    inv = si.invoice
    vault = c.vault("wallet")
    issuer = int(inv.issuer_agent_id)
    directory = Directory.from_deployments(dep)
    entry = directory.get(issuer)
    if entry is None or entry not in directory.providers:
        raise ValueError(f"agent #{issuer} is not a provider of the pinned directory")
    if int(inv.chain_id) != int(dep.chain_id):
        raise ValueError("the invoice is for another chain")
    if inv.token.lower() != dep.contracts["USDC"].address.lower():
        raise ValueError("the invoice is not in our USDC")
    if int(inv.buyer_agent_id) != int(dep.vaults["wallet"].agent_id) or inv.buyer_vault.lower() != (
        vault.address.lower()
    ):
        raise ValueError("the invoice is not addressed to our wallet")
    live = c.identity.get_agent_wallet(issuer).lower()
    if not live or live == ZERO_ADDRESS or recover_invoice_signer(si.wire_dict()) != live:
        raise ValueError("the invoice is not signed by the issuer's live agent wallet")
    oid = order_id(int(dep.chain_id), vault.address, issuer, inv.invoice_id)
    if oid != str(card.get("orderId", "")).lower():
        raise ValueError("the order id on the card is not the invoice's order id")
    now = clock.now(w3)
    if now > int(inv.expires_at):
        raise ValueError("the invoice has expired; ask the provider for a new quote")
    if vault.get_order(oid).state != OrderState.NONE:
        raise ValueError("this order already exists on-chain")
    existing = vault.get_approval(oid)
    if existing.exists and not existing.used and now <= existing.expiry:
        raise ValueError("this order already has an unused owner approval")
    if not _needs_owner_record(c, dep, oid, si.invoice_hash, issuer, int(inv.amount)):
        raise ValueError("our vault has no NEEDS_OWNER_APPROVAL record for this invoice")
    grade = Grade(c.acb.effective_grade(issuer, vault.max_grade_age()))
    if grade == Grade.D:
        raise ValueError("the payee is graded D: the vault refuses any payment to it")
    as_escrow = grade in (Grade.C, Grade.NEW, Grade.NONE)
    deadline = max(int(inv.delivery_deadline), now + ESCROW_MIN_S) if as_escrow else 0
    return {
        "orderId": oid,
        "issuer": issuer,
        "name": entry.name,
        "lookalike": directory.lookalike_of(issuer),
        "invoiceId": inv.invoice_id,
        "invoiceHash": si.invoice_hash,
        "invoiceExpiresAt": int(inv.expires_at),
        "wallet": live,
        "amount": int(inv.amount),
        "expiry": now + APPROVAL_TTL_S,
        "asEscrow": as_escrow,
        "escrowDeadline": deadline,
        "grade": grade.name,
    }


def prepare_card(card: dict[str, Any], *, w3: Any, dep: Any, c: Any) -> dict[str, Any]:
    """``{"type", "values"}``: the chain-rebuilt values of a draft or approval card (``ValueError`` otherwise)."""
    kind = card.get("type")
    if kind == "mandate_draft":
        if not isinstance(card.get("draftId"), str):
            raise ValueError("the draft has no draftId")
        return {"type": kind, "draftId": card["draftId"], "values": mandate_from_draft(card, w3=w3, dep=dep, c=c)}
    if kind == "approval":
        return {"type": kind, "values": approval_from_card(card, w3=w3, dep=dep, c=c)}
    raise ValueError("only mandate drafts and approvals can be confirmed")


def signing_code(prepared: dict[str, Any]) -> str:
    """The second confirmation code: 6 hex characters of the hash of exactly the values to be signed."""
    from acb.canonical import chash

    v = prepared["values"]
    if prepared["type"] == "mandate_draft":
        signed = {
            "mandateId": v["mandateId"],
            "ids": [str(i) for i in v["ids"]],
            "wallets": v["wallets"],
            "maxPerTx": str(v["maxPerTx"]),
            "maxTotal": str(v["maxTotal"]),
            "validUntil": v["validUntil"],
            "recurring": v["recurring"],
            "purposeHash": v["purposeHash"],
        }
    else:
        signed = {
            "orderId": v["orderId"],
            "issuer": str(v["issuer"]),
            "wallet": v["wallet"],
            "amount": str(v["amount"]),
            "expiry": v["expiry"],
            "asEscrow": v["asEscrow"],
            "escrowDeadline": v["escrowDeadline"],
        }
    return chash(signed)[2:8].upper()


def _text(value: Any) -> str:
    """Terminal-safe text: rich markup escaped, control characters removed."""
    from rich.markup import escape

    text = str(value if value is not None else "")
    return escape("".join(ch for ch in text if ord(ch) >= 0x20 and ord(ch) != 0x7F))


def _age(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 3600:
        return f"{max(0, seconds) // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def render_verified(console: Any, prepared: dict[str, Any]) -> str:
    """Print the chain-rebuilt confirmation screen; returns the code the owner must type to sign it."""
    from acb.logbus import format_usdc

    v = prepared["values"]
    code = signing_code(prepared)
    console.print("[bold]Verified from the chain (not from the agent):[/bold]")
    if prepared["type"] == "mandate_draft":
        for row in v["rows"]:
            warn = f"  [red]WARNING: looks like {_text(row['lookalike'])}[/red]" if row["lookalike"] else ""
            console.print(
                f"  #{row['agentId']} {_text(row['name'])}  grade {row['grade']}  wallet {row['wallet']}  "
                f"registered {_age(row['ageS'])} ago{warn}"
            )
        console.print(
            f"  per payment {format_usdc(v['maxPerTx'])} USDC, total {format_usdc(v['maxTotal'])} USDC, "
            f"valid until {v['validUntil']} (chain time), recurring {v['recurring']}"
        )
        console.print(f"  purpose (hashed on-chain): {_text(v['purpose'])}")
        console.print(f"  mandateId {v['mandateId']}")
        console.print(
            f"Type [bold]{code}[/bold] to register exactly this mandate with the owner key; anything else cancels."
        )
    else:
        warn = f"  [red]WARNING: looks like {_text(v['lookalike'])}[/red]" if v["lookalike"] else ""
        console.print(
            f"  order {v['orderId']}: #{v['issuer']} {_text(v['name'])} grade {v['grade']} wallet {v['wallet']}{warn}"
        )
        console.print(
            f"  invoice {_text(v['invoiceId'])} (signature verified), {format_usdc(v['amount'])} USDC, "
            f"{'escrow until ' + str(v['escrowDeadline']) if v['asEscrow'] else 'direct pay'}, "
            f"approval valid until {v['expiry']} (chain time)"
        )
        console.print(
            f"Type [bold]{code}[/bold] to approve exactly this payment with the owner key; anything else cancels."
        )
    return code


def execute_prepared(
    prepared: dict[str, Any], *, sender: Any, c: Any, base_url: str, agent_headers: dict[str, str], console: Any
) -> dict[str, Any]:
    """Send exactly the prepared values with the owner key, then notify the agent; returns the agent's answer."""
    import httpx

    vault = c.vault("wallet")
    v = prepared["values"]
    if prepared["type"] == "mandate_draft":
        result = sender.send(
            vault.register_mandate(
                v["mandateId"],
                v["ids"],
                v["wallets"],
                v["maxPerTx"],
                v["maxTotal"],
                v["validUntil"],
                v["recurring"],
                v["purposeHash"],
            )
        )
        if result.status != 1:
            raise ValueError(f"registerMandate reverted: {result.revert_error}")
        console.print(f"registered mandate {v['mandateId']} tx {result.hash}")
        answer = httpx.post(
            f"{base_url}/mandates/activated",
            json={"mandateId": v["mandateId"], "txHash": result.hash, "draftId": prepared["draftId"]},
            headers=agent_headers,
            timeout=60.0,
        )
    else:
        result = sender.send(
            vault.owner_approve(
                v["orderId"], v["issuer"], v["wallet"], v["amount"], v["expiry"], v["asEscrow"], v["escrowDeadline"]
            )
        )
        if result.status != 1:
            raise ValueError(f"ownerApprove reverted: {result.revert_error}")
        console.print(f"approved tx {result.hash}")
        answer = httpx.post(
            f"{base_url}/approvals/{v['orderId']}/executed-request", headers=agent_headers, timeout=180.0
        )
    try:
        data = answer.json()
    except ValueError:
        data = {"status": "error", "httpStatus": answer.status_code}
    console.print(f"agent: {_text(data)}")
    return data if isinstance(data, dict) else {"answer": data}


def confirm_card(
    card: dict[str, Any],
    *,
    base_url: str,
    agent_headers: dict[str, str],
    network: str | None,
    console: Any,
    ask: Callable[[str], str] = input,
) -> bool:
    """Act as the owner for one card: rebuild it from the chain, show it, and sign only after the owner
    types the code of the rebuilt screen. Returns True when the transaction was sent."""
    from acb.chain.tx import TxSender

    w3, dep, c, name = _chain(network)
    prepared = prepare_card(card, w3=w3, dep=dep, c=c)
    code = render_verified(console, prepared)
    try:
        typed = ask("owner (confirm)> ")
    except (EOFError, KeyboardInterrupt):
        typed = ""
    if (typed or "").strip().upper() != code:
        console.print("cancelled: nothing was signed")
        return False
    sender = TxSender(w3, _owner_account(name))
    execute_prepared(prepared, sender=sender, c=c, base_url=base_url, agent_headers=agent_headers, console=console)
    return True


__all__ = [
    "approval_from_card",
    "confirm_card",
    "execute_prepared",
    "mandate_from_draft",
    "prepare_card",
    "render_verified",
    "signing_code",
]
