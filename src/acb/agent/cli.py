"""``acb agent serve`` and ``acb chat`` (spec 8.8, 11.3, 15.3 CLI mounting).

``acb agent serve`` starts process P2 on 127.0.0.1. It never calls ``load_env_file()``: it reads the
allow-listed environment the launcher gives it (``config/processes.yaml``), refuses to start while any
``*_PRIVATE_KEY`` / ``*_MNEMONIC`` variable is visible (exit 78) and refuses to run without
``AGENT_TOKEN`` and ``EVENTS_TOKEN``.

``acb chat`` is the owner's terminal chat through the agent API, with rich-rendered cards. A mandate
draft or an approval card shows a 4-character code; typing it selects the card. The CLI then acts as
the owner (:mod:`acb.agent.owner_actions`): it re-reads every value from the chain (payee names from the
pinned directory, live wallets, grades, registration ages, lookalike warnings; for an approval it
verifies the invoice signature, buyer fields, expiry and the vault's NEEDS_OWNER_APPROVAL record and
recomputes the order id and amount), prints that verified screen with a second code computed from
exactly the values to be signed, and sends ``registerMandate`` / ``ownerApprove`` with the owner key only
when the owner types that second code (TB7). Approval requests are polled from ``GET /state`` (with
``EVENTS_TOKEN``) and shown as approval cards. As a one-shot owner command it may load ``.env`` (spec 2.1).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

ANVIL_CHAIN_ID = 31337
PUBLIC_PROVIDER_TIMEOUT_S = 60.0
"""Provider HTTP timeout on a public network (config/agent.yaml's provider_timeout_s is the Anvil value)."""

# ---------------------------------------------------------------------------
# acb agent serve
# ---------------------------------------------------------------------------


def deployment_fingerprint(w3: Any, dep: Any) -> str:
    """Identity of one deployment on one chain instance (chain id, vault, start block and its hash)."""
    from web3 import Web3

    from acb.canonical import chash

    block = w3.eth.get_block(int(dep.start_block))
    return chash(
        {
            "chainId": int(dep.chain_id),
            "vault": dep.vaults["wallet"].address,
            "startBlock": int(dep.start_block),
            "startBlockHash": Web3.to_hex(block["hash"]),
        }
    )


def build_agent(network: str, *, environ: dict[str, str] | None = None) -> Any:
    """The :class:`~acb.agent.wallet.WalletAgent` of ``acb agent serve`` (HTTP clients, chain, store, LLM)."""
    from acb import config
    from acb.agent.chain import Web3AgentChain
    from acb.agent.config import AgentConfig
    from acb.agent.llm import build_llm
    from acb.agent.quotes import HttpProviderClient
    from acb.agent.signer_client import HttpSignerClient
    from acb.agent.store import STATE_DIR_ENV, AgentStore, prepare_state_dir
    from acb.agent.wallet import WalletAgent
    from acb.chain.client import make_web3
    from acb.chain.deployments import load_deployments
    from acb.logbus import EventBus
    from acb.policy import PolicyConfig
    from acb.types import NetworkProfile

    env = os.environ if environ is None else environ
    net = NetworkProfile.load(network)
    w3 = make_web3(net)
    dep = load_deployments(network)
    chain = Web3AgentChain(w3, dep)
    cfg = AgentConfig.load()
    # ACB_AGENT_STATE_DIR is a test hook (not in the launcher's allow-list): default data/<network>/agent/
    base = env.get(STATE_DIR_ENV) or config.data_dir(network, "agent")
    store = AgentStore(prepare_state_dir(base, deployment_fingerprint(w3, dep)))
    settings = config.llm_settings(dict(env))
    llm, info = build_llm(
        mode=settings.effective_mode,
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout_s=settings.timeout_s,
        tool_mode=settings.tool_mode,
        extra_body=settings.extra_body,
        mock_script=settings.mock_script,
        persona=settings.mock_persona if settings.mock_persona in ("careful", "naive") else "careful",
    )
    explorer = net.explorers[0].get("tx") if net.explorers else None
    # a provider answers a quote or a delivery after its own chain reads; on a public network (2 s blocks,
    # a load-balanced RPC) that takes tens of seconds where Anvil answers at once
    provider_timeout = cfg.http.provider_timeout_s
    if int(net.chain_id) != ANVIL_CHAIN_ID:
        provider_timeout = max(provider_timeout, PUBLIC_PROVIDER_TIMEOUT_S)
    return WalletAgent(
        dep=dep,
        chain=chain,
        signer=HttpSignerClient(timeout_s=cfg.http.signer_timeout_s),
        provider=HttpProviderClient(timeout_s=provider_timeout),
        llm=llm,
        llm_info=info,
        store=store,
        cfg=cfg,
        pcfg=PolicyConfig.load(),
        bus=EventBus("agent", chain_id=net.chain_id, explorer_tx=explorer),
        network=network,
        explorer_tx=explorer,
    )


def _serve(args: argparse.Namespace) -> int:
    from acb import config
    from acb.agent.llm import set_exit_on_keys
    from acb.agent.service import LOOPBACK, serve
    from acb.keys import assert_no_private_keys

    assert_no_private_keys()  # exits 78 while any key or mnemonic is visible (spec 2.1, 7.6)
    if args.host != LOOPBACK:
        sys.stderr.write(f"acb agent: refusing to bind to {args.host!r}; the agent listens on {LOOPBACK} only\n")
        return 2
    missing = [n for n in ("AGENT_TOKEN", "EVENTS_TOKEN") if not os.environ.get(n)]
    if missing:
        sys.stderr.write(f"acb agent: {', '.join(missing)} not set; refusing to start without authentication\n")
        return 2
    network = args.network or config.network_name()
    set_exit_on_keys(True)  # from now on a key that appears in the environment ends the process (exit 78)
    try:
        agent = build_agent(network)
    except (FileNotFoundError, ValueError, TypeError, KeyError) as exc:
        sys.stderr.write(f"acb agent: cannot start: {type(exc).__name__}: {str(exc)[:300]}\n")
        return 2
    port = int(args.port or os.environ.get("AGENT_PORT") or 8701)
    sys.stderr.write(
        f"acb agent: vault {agent.vault_address} on {network}, LLM {agent.llm_info.get('mode')} "
        f"{agent.llm_info.get('model')}, {LOOPBACK}:{port}\n"
    )
    serve(agent, host=LOOPBACK, port=port)
    return 0


# ---------------------------------------------------------------------------
# acb chat
# ---------------------------------------------------------------------------


def card_code(card: dict[str, Any]) -> str:
    """The 4-character confirmation code of a card (first hex characters of its hash)."""
    from acb.canonical import chash

    return chash(card)[2:6].upper()


def render_card(console: Any, card: dict[str, Any]) -> str | None:
    """Print one card with rich; returns its confirmation code for drafts and approvals."""
    from rich.table import Table

    kind = card.get("type")
    if kind == "clarification":
        console.print(f"[bold]Question ({card.get('field')}):[/bold] {_safe(card.get('question'))}")
        for i, opt in enumerate(card.get("options") or [], 1):
            console.print(f"  {i}. {_safe(opt)}")
        if card.get("candidates"):
            console.print(_candidate_table(card["candidates"]))
        return None
    if kind == "mandate_draft":
        code = card_code(card)
        console.print(f"[bold]Mandate draft {card.get('draftId')}[/bold]: {_safe(card.get('purpose'))}")
        console.print(_candidate_table(card.get("payees") or []))
        console.print(
            f"max per payment {_usdc(card.get('maxPerTx'))} USDC, total {_usdc(card.get('maxTotal'))} USDC, "
            f"valid {card.get('validHours')} h, recurring {card.get('recurring')}"
        )
        if card.get("diff"):
            console.print(f"changed: {_safe(card['diff'])}")
        console.print(
            f"Type [bold]{code}[/bold] to check this draft on the chain; you confirm the verified values next."
        )
        return code
    if kind == "decision":
        payee = card.get("payee") or {}
        console.print(
            f"[bold]{card.get('outcome')}[/bold] {_safe(payee.get('name'))} #{payee.get('agentId')} "
            f"{_usdc(card.get('amount'))} USDC {'(off-chain)' if card.get('offchain') else ''}"
        )
        for text in card.get("reasonTexts") or []:
            console.print(f"  {_safe(text)}")
        if card.get("txHash"):
            console.print(f"  tx {card['txHash']}")
        return None
    if kind == "approval":
        code = card_code(card)
        inv = (card.get("invoice") or {}).get("invoice") or {}
        console.print(
            f"[bold]Approval requested[/bold] for order {_safe(card.get('orderId'))}: invoice "
            f"{_safe(inv.get('invoiceId'))} from #{_safe(inv.get('issuerAgentId'))}, {_usdc(inv.get('amount'))} USDC, "
            f"reasons {_safe(card.get('reasons'))}"
        )
        console.print(f"Type [bold]{code}[/bold] to verify it on the chain; you confirm the verified payment next.")
        return code
    if kind == "status":
        vault = card.get("vault") or {}
        table = Table(title="Wallet")
        for col in ("tier", "frozen", "balance", "reserve", "escrow", "window"):
            table.add_column(col)
        cfg = vault.get("config") or {}
        table.add_row(
            str(vault.get("tier")),
            str(vault.get("frozen")),
            _usdc(vault.get("balance")),
            _usdc(vault.get("reserveFloor")),
            _usdc(vault.get("escrowLocked")),
            f"{_usdc(vault.get('windowSpent'))} / {_usdc(cfg.get('windowCap'))}",
        )
        console.print(table)
        return None
    console.print(_safe(card))
    return None


def _candidate_table(candidates: list[dict[str, Any]]) -> Any:
    from rich.table import Table

    table = Table()
    for col in ("agentId", "name", "grade", "wallet", "registered", "warning"):
        table.add_column(col)
    for c in candidates:
        grade = c.get("grade")
        table.add_row(
            str(c.get("agentId")),
            _safe(c.get("name")),
            str(grade),
            str(c.get("liveWallet") or "-"),
            str(c.get("registeredAt") or "-"),
            _safe(f"looks like {c['lookalikeOf']}" if c.get("lookalikeOf") else ""),
        )
    return table


def _safe(value: Any) -> str:
    """Text for the terminal: rich markup escaped, control characters removed."""
    from rich.markup import escape

    text = str(value if value is not None else "")
    text = "".join(ch for ch in text if ch == "\n" or (ord(ch) >= 0x20 and ord(ch) != 0x7F))
    return escape(text)


def _usdc(units: Any) -> str:
    from acb.logbus import format_usdc

    try:
        return format_usdc(int(units))
    except (TypeError, ValueError):
        return "?"


def _chat(args: argparse.Namespace) -> int:
    import httpx
    from rich.console import Console

    from acb import config

    config.load_env_file(args.env_file)
    console = Console()
    port = int(os.environ.get("AGENT_PORT") or 8701)
    base = (args.url or f"http://127.0.0.1:{port}").rstrip("/")
    token = os.environ.get("AGENT_TOKEN", "")
    if not token:
        console.print("AGENT_TOKEN is not set")
        return 2
    headers = {"Authorization": f"Bearer {token}"}
    events_token = os.environ.get("EVENTS_TOKEN", "")
    pending: dict[str, dict[str, Any]] = {}
    shown_approvals: set[str] = set()
    messages = [args.message] if args.message else None

    def ask(prompt: str) -> str:
        return input(prompt) if messages is None else ""

    def poll_approvals() -> None:
        for card in approval_cards(base, events_token, shown_approvals):
            code = render_card(console, card)
            if code:
                pending[code] = card

    poll_approvals()
    while True:
        if messages is not None:
            if not messages:
                return 0
            line = messages.pop(0)
        else:
            try:
                line = input("owner> ").strip()
            except (EOFError, KeyboardInterrupt):
                return 0
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            return 0
        if line.upper() in pending:
            card = pending.pop(line.upper())
            try:
                from acb.agent.owner_actions import confirm_card

                confirm_card(card, base_url=base, agent_headers=headers, network=args.network, console=console, ask=ask)
            except Exception as exc:  # noqa: BLE001 - show the problem, keep the chat alive
                console.print(f"[red]not executed:[/red] {_safe(exc)}")
            continue
        try:
            response = httpx.post(f"{base}/chat", json={"message": line}, headers=headers, timeout=180.0)
        except httpx.HTTPError as exc:
            console.print(f"[red]agent unreachable:[/red] {type(exc).__name__}")
            continue
        if response.status_code != 200:
            console.print(f"[red]{response.status_code}[/red] {_safe(response.text[:300])}")
            continue
        data = response.json()
        llm = data.get("llm") or {}
        banner = " [yellow]AI offline: rule-based mode[/yellow]" if llm.get("fallback") else ""
        console.print(
            f"[bold]agent[/bold] ({_safe(llm.get('mode'))} {_safe(llm.get('model'))}){banner}: {_safe(data.get('reply'))}"
        )
        for card in data.get("cards") or []:
            code = render_card(console, card)
            if code:
                pending[code] = card
        poll_approvals()


def approval_cards(base: str, events_token: str, seen: set[str]) -> list[dict[str, Any]]:
    """Approval requests not shown yet, from ``GET /state`` (``EVENTS_TOKEN``), as approval cards.

    The card only selects the order: every value is verified on the chain before the owner signs.
    """
    import httpx

    if not events_token:
        return []
    try:
        response = httpx.get(f"{base}/state", headers={"Authorization": f"Bearer {events_token}"}, timeout=30.0)
        data = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    for item in (data.get("approvals") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict) or not isinstance(item.get("orderId"), str) or item["orderId"] in seen:
            continue
        seen.add(item["orderId"])
        out.append(
            {
                "type": "approval",
                "orderId": item["orderId"],
                "invoice": item.get("invoice"),
                "reasons": item.get("reasons"),
            }
        )
    return out


def register(subparsers: argparse._SubParsersAction) -> None:
    """Mount ``acb agent serve`` and ``acb chat``."""
    parser = subparsers.add_parser("agent", help="wallet agent service P2 (holds no private keys)")
    sub = parser.add_subparsers(dest="agent_command", metavar="<agent command>")
    serve_p = sub.add_parser("serve", help="run the wallet agent on 127.0.0.1 (default port AGENT_PORT or 8701)")
    serve_p.add_argument("--network", default=None, help="anvil or base_sepolia (default: NETWORK or anvil)")
    serve_p.add_argument("--port", type=int, default=None)
    serve_p.add_argument("--host", default="127.0.0.1", help="must be 127.0.0.1")
    serve_p.set_defaults(func=_serve)
    parser.set_defaults(func=lambda _args: (parser.print_help(), 2)[1])

    chat_p = subparsers.add_parser("chat", help="terminal owner chat with the wallet agent (confirm cards by code)")
    chat_p.add_argument("--url", default=None, help="agent URL (default http://127.0.0.1:<AGENT_PORT>)")
    chat_p.add_argument("--network", default=None, help="network for owner confirmations (default: NETWORK)")
    chat_p.add_argument("--env-file", default=None, help="env file to load (default: the repository .env)")
    chat_p.add_argument("--message", default=None, help="send one message and exit")
    chat_p.set_defaults(func=_chat)


__all__ = ["approval_cards", "build_agent", "card_code", "deployment_fingerprint", "register", "render_card"]
