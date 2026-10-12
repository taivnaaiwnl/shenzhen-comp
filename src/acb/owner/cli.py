"""``acb owner ...`` (spec 11.3): the owner console server and the owner's command-line fallback.

=======================================================  =====================================================
Command                                                  What it does
=======================================================  =====================================================
``acb owner serve``                                      the web console P1 on 127.0.0.1:``OWNER_PORT`` (8700)
``acb owner status``                                     wallet, mandates and grades from the chain
``acb owner freeze``                                     ``freeze(3202)`` with the owner key
``acb owner unfreeze --to 0 [--rotate-to <address>]``    ``unfreeze(toTier, 3300, newAgent)``
``acb owner approve <orderId> [--invoice FILE]``         verifies the signed invoice, then ``ownerApprove``
``acb owner revoke <mandateId>``                         ``revokeMandate``
``acb owner confirm <draftId>``                          rebuilds the agent's draft from chain reads, then
                                                         ``registerMandate`` (the CLI twin of the web card)
``acb owner bad-data <orderId>``                         the owner signs a BAD_DATA report
=======================================================  =====================================================

The CLI uses the same verification code as the web console (:class:`acb.owner.actions.OwnerConsole`
and :mod:`acb.owner.chain_views`): it prints the screen rebuilt from the chain and signs only after the
owner types the 6-character code of exactly those values (``--yes`` skips the typing, for the scenario
runner; the verification still runs). One-shot commands load ``.env`` (spec 2.1: ``owner <cmd>``);
``serve`` never does.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from typing import Any


def _text(value: Any) -> str:
    """Terminal-safe text: rich markup escaped, control characters removed."""
    from rich.markup import escape

    text = "" if value is None else str(value)
    return escape("".join(ch for ch in text if ord(ch) >= 0x20 and ord(ch) != 0x7F))


def _console() -> Any:
    from rich.console import Console

    return Console(highlight=False, soft_wrap=True)


def _network(args: argparse.Namespace) -> str:
    from acb import config

    return args.network or config.network_name()


def _load_env(args: argparse.Namespace) -> None:
    from acb import config

    config.load_env_file(args.env_file)


def _build_console(network: str, *, signing: str = "server", merger: Any = None, with_key: bool = True) -> Any:
    from acb.logbus import EventBus
    from acb.owner import chain_views as cv
    from acb.owner.actions import OwnerConsole, owner_account_for
    from acb.owner.agent_client import AgentClient

    account = None
    if signing == "server":
        try:
            account = owner_account_for(network)
        except (KeyError, ValueError):
            if with_key:
                raise
    chain_id = None
    try:
        from acb.types import NetworkProfile

        chain_id = NetworkProfile.load(network).chain_id
    except (KeyError, ValueError):
        chain_id = None
    bus = EventBus("owner", chain_id=chain_id)
    return OwnerConsole(
        chain_factory=lambda: cv.OwnerChain.load(network),
        agent=AgentClient.from_env(),
        bus=bus,
        owner_account=account,
        signing=signing,
        merger=merger,
    )


def _confirm(console: Any, code: str, yes: bool, ask: Callable[[str], str] | None = None) -> bool:
    if yes:
        return True
    console.print(f"Type [bold]{code}[/bold] to sign exactly this with the owner key; anything else cancels.")
    try:
        typed = (ask or input)("owner (confirm)> ")
    except (EOFError, KeyboardInterrupt):
        typed = ""
    if (typed or "").strip().upper() != code:
        console.print("cancelled: nothing was signed")
        return False
    return True


def _usdc(units: Any) -> str:
    from acb.owner.chain_views import usdc_text

    return usdc_text(int(units))


def _age(seconds: Any) -> str:
    if seconds is None:
        return "unknown"
    s = int(seconds)
    if s < 3600:
        return f"{max(0, s) // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def _print_result(console: Any, result: dict[str, Any]) -> None:
    for key in ("status", "mandateId", "orderId", "toTier", "rotatedTo", "txHash", "explorer"):
        if result.get(key) is not None:
            console.print(f"  {key}: {_text(result[key])}")
    agent = result.get("agent")
    if isinstance(agent, dict):
        if agent.get("notified"):
            console.print(f"  agent: {_text(agent.get('answer'))}")
        else:
            console.print(f"  [red]agent not notified: {_text(agent.get('error'))}[/red]")


def render_mandate(console: Any, card: dict[str, Any]) -> None:
    """The chain-rebuilt mandate screen (names from the pinned directory, the rest from the chain)."""
    v = card["values"]
    console.print("[bold]Mandate, verified from the chain (not from the agent):[/bold]")
    for row in card["payees"]:
        look = f"  [red]looks like {_text(row['lookalikeOf'])}[/red]" if row.get("lookalikeOf") else ""
        console.print(
            f"  #{_text(row['agentId'])} {_text(row['name'])}  grade {_text(row['grade'])}"
            f"{'' if row.get('score') is None else ' (' + str(row['score']) + ')'}  "
            f"wallet {_text(row['liveWallet'])}  registered {_age(row.get('ageS'))} ago{look}"
        )
    console.print(
        f"  per payment {_usdc(v['maxPerTx'])} USDC, total {_usdc(v['maxTotal'])} USDC, valid until "
        f"{v['validUntil']} (chain time, {card['validHours']} h), recurring {v['recurring']}"
    )
    console.print(f"  purpose (hashed on-chain): {_text(card['purpose'])}")
    console.print(f"  mandateId {v['mandateId']}")
    for w in card.get("warnings", []):
        console.print(f"  [yellow]warning: {_text(w)}[/yellow]")
    for p in card.get("problems", []):
        console.print(f"  [red]blocked: {_text(p)}[/red]")


def render_approval(console: Any, card: dict[str, Any]) -> None:
    """The verified approval screen (the invoice signature was checked by this process)."""
    v, inv, payee = card["values"], card["invoice"], card["payee"]
    console.print("[bold]Approval, verified from the signed invoice and the chain (not from the agent):[/bold]")
    look = f"  [red]looks like {_text(payee['lookalikeOf'])}[/red]" if payee.get("lookalikeOf") else ""
    console.print(
        f"  order {v['orderId']}: #{_text(payee['agentId'])} {_text(payee['name'])} grade {_text(payee['grade'])} "
        f"wallet {_text(v['expectedPayee'])}{look}"
    )
    console.print(f"  invoice {_text(inv['invoiceId'])} signed by {_text(inv['signer'])} (signature verified)")
    for item in inv["items"]:
        console.print(
            f"    {_text(item['sku'])} x{int(item['quantity'])} at {_usdc(item['unitPrice'])} USDC "
            f"({_text(item['description'])})"
        )
    console.print(f"  memo (untrusted): {_text(inv['memo'])}")
    console.print(
        f"  amount {_usdc(v['amount'])} USDC, "
        f"{'escrow until ' + str(v['escrowDeadline']) if v['asEscrow'] else 'direct pay'}, "
        f"approval valid until {v['expiry']} (chain time)"
    )
    for w in card.get("warnings", []):
        console.print(f"  [yellow]warning: {_text(w)}[/yellow]")


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def _run(args: argparse.Namespace, body: Callable[[Any, Any], int]) -> int:
    from acb.owner.actions import ActionError
    from acb.owner.chain_views import VerificationError

    _load_env(args)
    out = _console()
    try:
        owner = _build_console(_network(args), with_key=getattr(args, "needs_key", True))
        return body(owner, out)
    except (ActionError, VerificationError) as exc:
        message = exc.message if isinstance(exc, ActionError) else str(exc)
        out.print(f"[red]refused: {_text(message)}[/red]")
        return 1
    except KeyError as exc:
        out.print(f"[red]not configured: {_text(exc)}[/red]")
        return 2


def cmd_status(args: argparse.Namespace) -> int:
    args.needs_key = False

    def body(owner: Any, out: Any) -> int:
        from acb.owner.chain_views import PanelReader

        reader = PanelReader(owner.chain)
        w = reader.wallet()
        tier = "FROZEN" if w["frozen"] else str(w["tier"])
        out.print(f"[bold]Wallet vault {w['vault']}[/bold] (agent #{w['agentId']}) on {owner.chain.net.name}")
        out.print(
            f"  tier {tier} (max auto {w['maxAutoTier']}), balance {_usdc(w['balance'])}, reserve "
            f"{_usdc(w['reserveFloor'])}, escrow {_usdc(w['escrowLocked'])}, free {_usdc(w['freeBalance'])} USDC"
        )
        lim = w["limits"]
        out.print(
            f"  per tx {_usdc(lim['perTx'])}, window {_usdc(w['windowSpent'])}/{_usdc(lim['windowCap'])} USDC, "
            f"{w['windowCount']}/{lim['maxTx']} tx; clean {w['cleanSinceChange']}"
            f"{'' if w['cleanNeeded'] is None else '/' + str(w['cleanNeeded'])}, violation points "
            f"{w['violationPoints']}"
        )
        for m in w["mandates"]:
            state = "active" if m["active"] else ("revoked" if m["revoked"] else "expired")
            names = ", ".join(_text(p["name"]) for p in m["payees"])
            out.print(
                f"  mandate {m['mandateId']} {state}: {names}; {_usdc(m['spent'])}/{_usdc(m['maxTotal'])} USDC "
                f"spent, per tx {_usdc(m['maxPerTx'])}"
            )
        out.print("[bold]Grades[/bold]")
        for g in reader.grades():
            reasons = ", ".join(str(r["code"]) for r in g.get("reasons", []))
            out.print(
                f"  #{g['agentId']} {_text(g['name'])}: {g['grade']} (effective {g['effectiveGrade']})"
                f"{'' if g.get('score') is None else ' score ' + str(g['score'])}"
                f"{' reasons ' + reasons if reasons else ''}"
            )
        check = owner.owner_check()
        if check["address"] is None:
            out.print("no owner key is configured in this environment (read-only status)")
        else:
            verdict = "owns" if check["isVaultOwner"] else "does NOT own"
            out.print(f"owner key {check['address']} {verdict} the vault")
        return 0

    return _run(args, body)


def cmd_freeze(args: argparse.Namespace) -> int:
    def body(owner: Any, out: Any) -> int:
        from acb.owner.chain_views import signing_code

        vault = owner.chain.vault.address
        out.print(f"Freeze the wallet vault {vault} (reason 3202: frozen by the owner).")
        code = signing_code({"kind": "freeze", "values": {"vault": vault}})
        if not _confirm(out, code, args.yes):
            return 1
        _print_result(out, owner.freeze())
        return 0

    return _run(args, body)


def cmd_unfreeze(args: argparse.Namespace) -> int:
    def body(owner: Any, out: Any) -> int:
        from acb.owner.chain_views import signing_code

        vault = owner.chain.vault.address
        rotate = (args.rotate_to or "").lower() or None
        out.print(
            f"Unfreeze the wallet vault {vault} to tier {args.to} (reason 3300)"
            + (f", rotating the agent key to {_text(rotate)}" if rotate else ", keeping the agent key")
        )
        code = signing_code({"kind": "unfreeze", "values": {"vault": vault, "toTier": args.to, "rotateTo": rotate}})
        if not _confirm(out, code, args.yes):
            return 1
        _print_result(out, owner.unfreeze(int(args.to), rotate))
        return 0

    return _run(args, body)


def cmd_revoke(args: argparse.Namespace) -> int:
    def body(owner: Any, out: Any) -> int:
        from acb.owner.chain_views import is_hex32, signing_code

        if not is_hex32(args.mandate_id):
            out.print("[red]the mandate id must be 0x + 64 hex[/red]")
            return 2
        mid = args.mandate_id.lower()
        out.print(f"Revoke mandate {mid} in the wallet vault {owner.chain.vault.address}.")
        if not _confirm(out, signing_code({"kind": "revoke", "values": {"mandateId": mid}}), args.yes):
            return 1
        _print_result(out, owner.revoke_mandate(mid))
        return 0

    return _run(args, body)


def _read_invoice(path: str) -> Any:
    import json
    from pathlib import Path

    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def cmd_approve(args: argparse.Namespace) -> int:
    def body(owner: Any, out: Any) -> int:
        raw = _read_invoice(args.invoice) if args.invoice else None
        card = owner.prepare_approval(args.order_id, raw)
        render_approval(out, card)
        if not _confirm(out, card["code"], args.yes):
            owner.discard(card["prepareId"])
            return 1
        _print_result(out, owner.approve(args.order_id, card["prepareId"], notify=not args.no_notify))
        return 0

    return _run(args, body)


def cmd_confirm(args: argparse.Namespace) -> int:
    def body(owner: Any, out: Any) -> int:
        card = owner.prepare_mandate(args.draft_id)
        render_mandate(out, card)
        if not card["confirmable"]:
            out.print("[red]this mandate cannot be registered[/red]")
            return 1
        if not _confirm(out, card["code"], args.yes):
            return 1
        _print_result(out, owner.confirm_mandate(args.draft_id, card["prepareId"], notify=not args.no_notify))
        return 0

    return _run(args, body)


def cmd_bad_data(args: argparse.Namespace) -> int:
    def body(owner: Any, out: Any) -> int:
        from acb.owner.chain_views import is_hex32, signing_code

        if not is_hex32(args.order_id):
            out.print("[red]the order id must be 0x + 64 hex[/red]")
            return 2
        oid = args.order_id.lower()
        out.print(f"Sign a BAD_DATA report about the payee of order {oid} with the owner key.")
        if not _confirm(out, signing_code({"kind": "bad-data", "values": {"orderId": oid}}), args.yes):
            return 1
        result = owner.bad_data(oid)
        _print_result(out, result)
        return 0

    return _run(args, body)


def cmd_serve(args: argparse.Namespace) -> int:
    """``acb owner serve``: never loads ``.env``; reads the launcher's allow-listed environment only."""
    from acb import config
    from acb.owner.app import create_app, serve
    from acb.owner.feeds import BusFeed, FeedMerger, FileFeed, default_feeds
    from acb.owner.security import ConfigError, check_config

    password = os.environ.get("OWNER_UI_PASSWORD", "")
    secret = os.environ.get("SESSION_SECRET", "")
    try:
        check_config(password, secret)
    except ConfigError as exc:
        sys.stderr.write(f"acb owner serve: {exc}\n")
        return 78
    network = _network(args)
    for name in ("AGENT_TOKEN", "EVENTS_TOKEN"):
        if not os.environ.get(name):
            sys.stderr.write(f"acb owner serve: warning: {name} is not set (chat or event feeds will be offline)\n")
    feeds: list[Any] = [] if args.no_feeds else default_feeds(out_dir=config.out_dir())
    for path in args.replay or []:
        feeds.append(FileFeed("replay", path))
    merger = FeedMerger(feeds)
    try:
        owner = _build_console(network, signing=args.signing, merger=merger)
    except (KeyError, ValueError) as exc:
        sys.stderr.write(f"acb owner serve: owner key not available: {exc}\n")
        return 78
    merger.feeds.append(BusFeed(owner.bus))
    merger.start()
    port = int(args.port or os.environ.get("OWNER_PORT") or 8700)
    app = create_app(owner, password=password, session_secret=secret, merger=merger)
    print(f"owner console on http://127.0.0.1:{port} ({network}, signing: {args.signing})", flush=True)
    try:
        serve(app, port=port)
    finally:
        merger.stop()
    return 0


# ---------------------------------------------------------------------------
# mounting (spec 15.3)
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    from acb.config import DEFAULT_ENV_FILE

    owner = subparsers.add_parser("owner", help="owner console P1 (web) and the owner's command-line fallback")
    sub = owner.add_subparsers(dest="owner_command", metavar="<owner command>")

    def common(p: argparse.ArgumentParser, *, env: bool = True) -> None:
        p.add_argument("--network", default=None, help="anvil | base_sepolia (default: NETWORK or anvil)")
        if env:
            p.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="env file to load (default: .env)")

    p = sub.add_parser("serve", help="run the owner console on 127.0.0.1 (OWNER_PORT, default 8700)")
    common(p, env=False)
    p.add_argument("--port", type=int, default=None)
    p.add_argument(
        "--signing",
        choices=("server", "browser"),
        default=os.environ.get("OWNER_SIGNING") or "server",
        help="server: P1 signs with the owner key; browser: MetaMask signs (stretch goal 11.4)",
    )
    p.add_argument("--replay", action="append", help="also show the events of this JSONL file (repeatable)")
    p.add_argument("--no-feeds", action="store_true", help="do not poll the services' event feeds")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("status", help="wallet, mandates and grades from the chain")
    common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("freeze", help="freeze the wallet (3202)")
    common(p)
    p.add_argument("--yes", action="store_true", help="do not ask for the confirmation code")
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser("unfreeze", help="unfreeze to tier 0 or 1 (3300), optionally rotating the agent key")
    common(p)
    p.add_argument("--to", type=int, choices=(0, 1), required=True)
    p.add_argument("--rotate-to", default=None, help="new agent signer address (key rotation in the same tx)")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_unfreeze)

    p = sub.add_parser("approve", help="verify an approval request's signed invoice, then ownerApprove")
    common(p)
    p.add_argument("order_id")
    p.add_argument("--invoice", default=None, help="signed invoice JSON file (default: fetched from the agent)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--no-notify", action="store_true", help="do not ask the agent to execute afterwards")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("revoke", help="revokeMandate")
    common(p)
    p.add_argument("mandate_id")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("confirm", help="rebuild the agent's mandate draft from chain reads and register it")
    common(p)
    p.add_argument("draft_id")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--no-notify", action="store_true", help="do not tell the agent (POST /mandates/activated)")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("bad-data", help="owner-signed BAD_DATA report about an order's payee")
    common(p)
    p.add_argument("order_id")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_bad_data)

    def show_help(_args: argparse.Namespace) -> int:
        owner.print_help()
        return 2

    owner.set_defaults(func=show_help)


__all__ = ["register", "render_approval", "render_mandate"]
