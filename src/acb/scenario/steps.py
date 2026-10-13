"""The 22 steps of the recorded run (spec 10.2). Each step drives the system through the same interfaces a
user would (the services' HTTP APIs, the owner console code, the simulator's admin actions) and records
what it observed in its :class:`~acb.scenario.runner.StepCtx`; the generic checks of
:meth:`~acb.scenario.runner.Runner.check` then compare it with ``config/scenario.yaml``.

Steps that need time to pass warp the Anvil clock (``evm_increaseTime`` + ``evm_mine``) or, on Base
Sepolia, wait in real time (10.1). "Stolen key" transactions are sent by the runner itself from the agent
signer key with ``expect_revert=True`` (500,000 gas, no simulation) to show that the vault reverts them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from acb import config
from acb.canonical import atomic_write_bytes, chash
from acb.scenario.env import EnvError
from acb.scenario.runner import Runner, StepCtx, StepFailure

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _log(run: Runner, text: str) -> None:
    run.echo(f"      {text}")


def ensure_services(run: Runner) -> None:
    """Start the services once the deployment exists (after S3, or on a resumed run)."""
    env = run.env
    launcher = env.make_launcher()
    for role in env.roles():
        child = launcher.children.get(role)
        if child is not None and child.proc is not None and child.proc.poll() is None:
            continue
        overrides = None
        if role == "signer" and run.facts.get("signerRotated"):
            overrides = {"AGENT_SIGNER_PRIVATE_KEY": rotated_signer_key(run)}
        env.start_service(role, overrides)
    if run.facts.get("llmSwitched") and "agent" in env.roles():
        # a resumed S16 continues with the naive persona
        env.switch_llm(mode="mock", persona="naive", maskFlaggedSpans=False)


def rotated_signer_key(run: Runner) -> str:
    from acb.chain.tx import to_hex

    return to_hex(run.env.keys.account("agent_signer_next").key)


def purchase(run: Runner, ctx: StepCtx, key: str | None = None) -> list[dict[str, Any]]:
    """``POST /purchase/{M1}`` with the step's item; records decisions, off-chain decisions, violations."""
    item = run.cfg["purchases"][key or ctx.id]
    mandate_id = run.facts.get("mandateId")
    if not mandate_id:
        raise StepFailure("no mandate M1 (run S5 first)")
    body = [{"payee_agent_id": run.agent_id(item["payee"]), "sku": item["sku"], "quantity": int(item["quantity"])}]
    answer = run.env.purchase(mandate_id, body)
    if not isinstance(answer.body, dict) or answer.body.get("status") != "done":
        raise StepFailure(f"purchase failed: HTTP {answer.status} {json.dumps(answer.body)[:400]}")
    results = answer.body.get("results") or []
    for r in results:
        record_result(run, ctx, r)
    ctx.data.setdefault("purchases", []).append(
        {
            "item": item,
            "results": [
                {
                    k: r.get(k)
                    for k in ("status", "orderId", "outcome", "reasons", "violation", "txHashes", "invoiceHash")
                }
                for r in results
            ],
        }
    )
    return results


def record_result(run: Runner, ctx: StepCtx, r: dict[str, Any]) -> None:
    status = r.get("status")
    if status == "superseded":
        ctx.offchain.append(
            {"outcome": "SUPERSEDED", "reasons": list(r.get("reasons") or []), "orderId": r.get("orderId")}
        )
        return
    if status == "dropped":
        ctx.offchain.append(
            {"outcome": "DROPPED", "reasons": list(r.get("reasons") or []), "invoiceHash": r.get("invoiceHash")}
        )
        return
    if status in ("executed", "refused"):
        ctx.decisions.append(
            {
                "outcome": r.get("outcome"),
                "reasons": list(r.get("reasons") or []),
                "orderId": r.get("orderId"),
                "status": status,
                "txHashes": list(r.get("txHashes") or []),
            }
        )
        if r.get("violation"):
            ctx.violations.append(dict(r["violation"]))
        return
    raise StepFailure(f"unexpected purchase result {status}: {json.dumps(r)[:400]}")


def last_order(results: list[dict[str, Any]]) -> str:
    for r in reversed(results):
        if r.get("orderId") and r.get("status") in ("executed", "refused"):
            return str(r["orderId"]).lower()
    raise StepFailure("the purchase produced no order")


def wait_order(run: Runner, order_id: str, statuses: set[str], *, timeout: float | None = None) -> dict[str, Any]:
    """Poll the agent's order record until its status is in ``statuses`` (the delivery watcher runs in
    the agent; ``/deliveries/check`` makes it run now)."""
    timeout = float(timeout or run.cfg["timing"]["settle_timeout_s"])
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    nudged = 0.0
    while time.monotonic() < deadline:
        state = run.env.agent_state()
        last = state["orders"].get(order_id.lower()) or {}
        if last.get("status") in statuses:
            return last
        if time.monotonic() - nudged > 5:
            nudged = time.monotonic()
            try:
                run.env.deliveries_check()
            except EnvError:
                pass
        time.sleep(float(run.cfg["timing"]["poll_s"]))
    raise StepFailure(f"order {order_id[:10]} status {last.get('status')!r}, waited for {sorted(statuses)}")


def wallet_vault(run: Runner) -> Any:
    from acb.chain.contracts import VaultContract

    return VaultContract(run.env.w3, run.env.dep.vaults["wallet"].address)


def acb_contract(run: Runner) -> Any:
    from acb.chain.contracts import AcbContract

    return AcbContract(run.env.w3, run.env.dep.contracts["AgentCreditBureau"].address)


def order_outcome(run: Runner, order_id: str) -> str:
    return wallet_vault(run).get_order(order_id).outcome.name


def stolen_send(run: Runner, ctx: StepCtx, fn: Any, label: str) -> Any:
    """A transaction from the agent signer key, outside the signer (expected to revert)."""
    sender = run.env.sender("agent_signer")
    result = sender.send(fn, expect_revert=True)
    run.ensure_recorder().mark_expected_revert(result.hash, result.revert_error)
    run.ensure_recorder().note(result.hash, f"stolen key: {label}")
    ctx.data.setdefault("stolenKey", []).append(
        {"label": label, "tx": result.hash, "status": result.status, "revert": result.revert_error}
    )
    _log(run, f"stolen key {label}: status {result.status} {result.revert_error}")
    return result


def outflow_params(run: Runner, order_id: str, payee_key: str, amount: int) -> Any:
    from acb.chain.contracts import OutflowParams
    from acb.types import Decision

    return OutflowParams(
        order_id=order_id,
        payee_agent_id=run.agent_id(payee_key),
        amount=int(amount),
        deadline=run.env.now() + 600,
        mandate_id=run.facts["mandateId"],
        invoice_hash=chash({"schema": "acb-stolen-key/1", "order": order_id}),
        decision_hash=chash({"schema": "acb-stolen-key/1", "decision": order_id}),
        decision=int(Decision.PAY),
        reason_code=2000,
    )


def wait_for(run: Runner, what: str, check: Callable[[], Any], *, timeout: float | None = None) -> Any:
    timeout = float(timeout or run.cfg["timing"]["event_timeout_s"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(float(run.cfg["timing"]["poll_s"]))
    raise StepFailure(f"timed out waiting for {what}")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# S0 preflight
# ---------------------------------------------------------------------------


def s0(run: Runner, ctx: StepCtx) -> None:
    from web3 import Web3

    from acb.chain import abi as abi_mod
    from acb.chain.clock import now

    env = run.env
    if env.w3 is None:
        env.start_chain()
    w3 = env.w3
    chain_id = int(w3.eth.chain_id)
    ctx.require(chain_id == int(env.net.chain_id), f"chain id {chain_id} != {env.net.chain_id}")
    ctx.data["chainId"] = chain_id
    run.state["scanStart"] = int(w3.eth.block_number)
    # ERC-8004 registries: the pinned implementation and version (Base Sepolia); Lite on Anvil
    if env.net.erc8004.get("mode") == "official":
        from acb.chain.deploy import DeploymentError, check_official_erc8004

        try:
            entries = check_official_erc8004(w3, env.net)
        except DeploymentError as exc:
            raise StepFailure(f"ERC-8004 preflight aborted: {exc}") from None
        run.facts["erc8004"] = {
            "identityImpl": entries["IdentityRegistry"].implementation,
            "reputationImpl": entries["ReputationRegistry"].implementation,
            "version": entries["IdentityRegistry"].version,
            "checkedAtBlock": int(w3.eth.block_number),
        }
    else:
        run.facts["erc8004"] = {"mode": "deploy_lite", "note": "local ERC-8004 Lite registries are deployed in S1"}
    # TierVault code hash: abi/ against its own bytecode and against the build output
    art = abi_mod.load("TierVault")
    recorded = str(art.get("codehash") or "").lower()
    own = Web3.to_hex(Web3.keccak(hexstr=art["deployedBytecode"])).lower()
    ctx.require(recorded == own, f"abi/TierVault.json codehash {recorded} != keccak(deployedBytecode) {own}")
    build = verify_fresh_build(run, ctx)
    run.facts["vaultCodeHash"] = recorded
    ctx.data["vaultCodeHash"] = {"abi": recorded, "build": build}
    # balances against the funding plan (Base Sepolia)
    if env.network != "anvil":
        check_funding(run, ctx)
    # LLM mode and model
    settings = config.llm_settings(env.source_env)
    mode = settings.effective_mode
    run.facts["llm"] = {
        "mode": mode,
        "model": settings.model if mode == "openai" else "mock-careful",
        "persona": "careful",
        "promptVersion": "acb-prompt-1.0.0",
    }
    if env.llm_mode == "openai" and not settings.has_key:
        raise StepFailure("--llm openai needs LLM_API_KEY (or OPENAI_API_KEY)")
    # clock
    chain_now = now(w3)
    ctx.data["clock"] = {"chainNow": chain_now, "wallClock": int(time.time()), "driftS": chain_now - int(time.time())}
    _log(run, f"chain {chain_id}, vault code hash {recorded[:12]}..., llm {mode}")


def verify_fresh_build(run: Runner, ctx: StepCtx) -> str | None:
    """Compare abi/TierVault.json with the build output (``--fresh-build`` runs ``forge build`` first)."""
    import subprocess

    from web3 import Web3

    from acb.chain import abi as abi_mod

    out = config.REPO_ROOT / "contracts" / "out" / "TierVault.sol" / "TierVault.json"
    if run.fresh_build:
        forge = config.foundry_tool("forge")
        if forge is None:
            raise StepFailure("--fresh-build needs forge")
        subprocess.run([str(forge), "build"], cwd=config.REPO_ROOT, check=True, capture_output=True, timeout=1800)
    if not out.is_file():
        ctx.note("contracts/out has no TierVault build: compared abi/ with its own bytecode only")
        return None
    data = json.loads(out.read_text(encoding="utf-8"))
    code = data.get("deployedBytecode", {})
    code = code.get("object") if isinstance(code, dict) else code
    built = Web3.to_hex(Web3.keccak(hexstr=code)).lower()
    recorded = str(abi_mod.load("TierVault").get("codehash") or "").lower()
    ctx.require(built == recorded, f"TierVault build code hash {built} != abi/ {recorded}")
    return built


def check_funding(run: Runner, ctx: StepCtx) -> None:
    from acb.chain.contracts import Erc20Contract
    from acb.chain.deploy import default_funding_plan

    env = run.env
    funding = env.net.funding
    deployer = env.keys.checksum_address("deployer")
    eth = int(env.w3.eth.get_balance(deployer))
    plan = default_funding_plan(env.net, env.keys)
    usdc = Erc20Contract(env.w3, env.net.usdc["address"])
    held = usdc.balance_of(env.keys.address("deployer"))
    need_usdc = sum(int(v) for v in plan.usdc_to_vaults.values())
    # like fund(): only the shortfall of each key is sent, so keys that already hold their top-up (from an
    # earlier attempt on the same keys) need nothing
    need_eth = sum(
        max(0, int(target) - int(env.w3.eth.get_balance(env.keys.checksum_address(role))))
        for role, target in plan.eth.items()
    )
    ctx.data["funding"] = {
        "deployerEth": str(eth),
        "deployerUsdc": str(held),
        "planEth": str(need_eth),
        "planUsdc": str(need_usdc),
    }
    ctx.require(eth >= int(funding["min_eth_deployer"]), f"deployer holds {eth} wei < min_eth_deployer")
    ctx.require(eth >= need_eth, f"deployer holds {eth} wei, the funding plan needs {need_eth} wei")
    ctx.require(held >= need_usdc, f"deployer holds {held} USDC units, the plan needs {need_usdc}")


# ---------------------------------------------------------------------------
# S1-S3 deployment
# ---------------------------------------------------------------------------


def _deploy_log(run: Runner) -> Callable[[str], None]:
    return lambda m: _log(run, m)


def s1(run: Runner, ctx: StepCtx) -> None:
    from acb.chain.deploy import deploy_contracts

    env = run.env
    dep, _txs = deploy_contracts(env.net, env.keys, existing=None, w3=env.w3, log=_deploy_log(run))
    env.dep = dep
    run.ensure_recorder().bind_deployment(dep, env.keys)
    ctx.data["contracts"] = {k: v.address for k, v in dep.contracts.items()}


def s2(run: Runner, ctx: StepCtx) -> None:
    from acb.chain.deploy import deploy_vaults, register_agents

    env = run.env
    dep, _ = register_agents(env.net, env.keys, env.dep, w3=env.w3, log=_deploy_log(run))
    dep, _ = deploy_vaults(env.net, env.keys, dep, None, w3=env.w3, log=_deploy_log(run))
    env.dep = dep
    run.ensure_recorder().bind_deployment(dep, env.keys)
    ctx.data["agents"] = {k: str(a.agent_id) for k, a in dep.agents.items()}
    ctx.data["vaults"] = {k: v.address for k, v in dep.vaults.items()}


def s3(run: Runner, ctx: StepCtx) -> None:
    from acb.chain.contracts import Erc20Contract
    from acb.chain.deploy import default_funding_plan, fund

    env = run.env
    # the same plan fund() sends, so ACB_FUNDING_USDC_BPS scales the expected balances too
    plan = default_funding_plan(env.net, env.keys)
    fund(env.net, env.keys, env.dep, None, w3=env.w3, log=_deploy_log(run))
    usdc = Erc20Contract(env.w3, env.dep.contracts["USDC"].address)
    balances = {name: usdc.balance_of(v.address) for name, v in env.dep.vaults.items()}
    ctx.data["vaultUsdc"] = {k: str(v) for k, v in balances.items()}
    expected = expected_vault_usdc(ctx.expect.get("usdc_vaults") or {}, plan.usdc_to_vaults)
    ctx.data["expectedVaultUsdc"] = {k: str(v) for k, v in expected.items()}
    for name, amount in expected.items():
        ctx.require(balances.get(name, 0) >= amount, f"vault {name} holds {balances.get(name)} < {amount}")
    ensure_services(run)


def expected_vault_usdc(nominal: dict[str, Any], plan: dict[str, int]) -> dict[str, int]:
    """S3's expected vault balances: the funding plan's amounts (scaled by ``ACB_FUNDING_USDC_BPS``).

    ``config/scenario.yaml`` lists the nominal (unscaled) amounts of 13.1; a vault the plan does not fund
    keeps its nominal expectation.
    """
    out = {str(k): int(v) for k, v in nominal.items()}
    for name, amount in plan.items():
        out[str(name)] = int(amount)
    return out


# ---------------------------------------------------------------------------
# S4 market bootstrap
# ---------------------------------------------------------------------------


def s4(run: Runner, ctx: StepCtx) -> None:
    from acb.bureau.config import load_config

    env = run.env
    ensure_services(run)
    r1 = env.rescore(1)
    _log(run, f"round 1: {len(r1['postings'])} postings")
    warm = env.sim("warmup", step="S4")
    result = warm.get("result") or {}
    _log(run, f"warm-up: {result.get('settled')} orders settled, mandate {str(result.get('mandateId'))[:10]}...")
    ctx.require(int(result.get("settled") or 0) == 19, f"warm-up settled {result.get('settled')} of 19 orders")
    run.facts["mandateMB"] = result.get("mandateId")
    # age gate: wait a_min_age_s after the identities were registered (S2)
    cfg = load_config(env.dep.anchors)
    registered = [r["blockTimestamp"] for r in run.recorder.find(function="register") if r["status"] == 1]
    target = (
        (max(registered) if registered else env.now())
        + int(cfg.a_min_age_s)
        + int(run.cfg["timing"]["age_gate_margin_s"])
    )
    if env.now() < target:
        env.advance_to(target)
    r23 = env.rescore(2)
    _log(run, f"rounds 2-3: {len(r23['postings'])} postings")
    wait_upgrade_delay(run)


def wait_upgrade_delay(run: Runner) -> None:
    delay = int(acb_contract(run).upgrade_delay())
    run.env.warp(delay + int(run.cfg["timing"]["upgrade_delay_margin_s"]))


# ---------------------------------------------------------------------------
# S5 owner dialogue and mandate M1
# ---------------------------------------------------------------------------


def s5(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    owner = run.env.owner()
    transcript: list[dict[str, Any]] = []
    draft_id = None
    card = None

    def say(message: str) -> None:
        nonlocal draft_id, card
        answer = owner.chat(message)
        cards = answer.get("cards") or []
        turn = len(transcript) + 1
        transcript.append(
            {"turn": turn, "owner": message, "reply": answer.get("reply"), "cards": cards, "llm": answer.get("llm")}
        )
        _log(run, f"turn {turn}: {', '.join(c.get('type', '?') for c in cards) or 'no card'}")
        for c in cards:
            if c.get("type") == "mandate":
                draft_id, card = c.get("draftId"), c

    for message in run.cfg["dialogue"]:
        say(message)
    if run.real_llm:
        # a real LLM may need the owner to be more explicit: the scripted owner answers like a person would
        for message in run.cfg.get("dialogue_fallback") or []:
            if card is not None and card["values"]["maxTotal"] == run.cfg["mandate"]["max_total"]:
                break
            say(message)
        if len(transcript) > len(run.cfg["dialogue"]):
            ctx.note(f"the real LLM needed {len(transcript)} owner turns")
    kinds = [[c.get("type") for c in t["cards"]] for t in transcript]
    ctx.data["cards"] = kinds
    if not run.real_llm:
        ctx.require("clarification" in kinds[0], f"turn 1 must force a clarification, got {kinds[0]}")
        ctx.require("mandate" in kinds[1], f"turn 2 must give the draft, got {kinds[1]}")
        ctx.require("mandate" in kinds[-1], f"the correction must update the draft, got {kinds[-1]}")
    if card is None or not draft_id:
        raise StepFailure("the dialogue produced no mandate card")
    values = card["values"]
    want = run.cfg["mandate"]
    payees = sorted(int(x) for x in values["payeeAgentIds"])
    ctx.require(payees == sorted(run.agent_id(k) for k in want["payees"]), f"mandate payees {payees}")
    ctx.require(values["maxPerTx"] == want["max_per_tx"], f"maxPerTx {values['maxPerTx']} != {want['max_per_tx']}")
    ctx.require(values["maxTotal"] == want["max_total"], f"maxTotal {values['maxTotal']} != {want['max_total']}")
    ctx.require(bool(card.get("confirmable")), f"the card is not confirmable: {card.get('problems')}")
    # the owner confirms on a card P1 rebuilds from the chain, then registerMandate(M1)
    fresh = owner.prepare_mandate(draft_id)
    stable = ("payeeAgentIds", "payeeWallets", "maxPerTx", "maxTotal", "recurring", "purposeHash")
    ctx.require(
        all(fresh["values"].get(k) == values.get(k) for k in stable),
        "the confirm card changed between the chat and the confirmation",
    )
    result = owner.confirm_mandate(draft_id, fresh["prepareId"])
    mandate_id = result["mandateId"]
    run.facts["mandateId"] = mandate_id
    run.facts["mandateTx"] = result["txHash"]
    agent_answer = (result.get("agent") or {}).get("answer") or {}
    turns = agent_answer.get("turnsToMandate")
    ctx.data["turnsToMandate"] = turns
    ctx.require(agent_answer.get("status") == "active", f"the agent did not activate M1: {agent_answer}")
    if run.real_llm:
        if turns is None or int(turns) > int(run.cfg["max_dialogue_turns"]):
            ctx.note(f"turns to mandate {turns} (target <= {run.cfg['max_dialogue_turns']})")
    else:
        ctx.require(turns is not None and int(turns) <= int(run.cfg["max_dialogue_turns"]), f"turns to mandate {turns}")
    transcript.append(
        {"confirm": {"draftId": draft_id, "values": values, "txHash": result["txHash"], "agent": agent_answer}}
    )
    write_json(
        run.env.out_dir / "dialogue" / "S5.json",
        {"schema": "acb-dialogue/1", "step": "S5", "llm": run.facts.get("llm"), "turns": transcript},
    )
    _log(run, f"M1 {mandate_id[:12]}... registered in {turns} turns")


# ---------------------------------------------------------------------------
# S6-S10 payments, promotion
# ---------------------------------------------------------------------------


def s6(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    oid = last_order(purchase(run, ctx))
    run.facts["orderS6"] = oid
    if ctx.decisions and ctx.decisions[-1]["status"] == "executed":
        wait_order(run, oid, {"delivered"})
        ctx.require(order_outcome(run, oid) == ctx.expect["delivery"], f"S6 outcome {order_outcome(run, oid)}")


def s7(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    oid = last_order(purchase(run, ctx))
    run.facts["orderS7"] = oid
    if ctx.decisions and ctx.decisions[-1]["status"] == "executed":
        order = wallet_vault(run).get_order(oid)
        run.env.advance_to(int(order.deadline) + int(run.cfg["timing"]["late_margin_s"]))
        wait_order(run, oid, {"delivered", "failed"})
        ctx.require(order_outcome(run, oid) == ctx.expect["delivery"], f"S7 outcome {order_outcome(run, oid)}")


def s8(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    oid = last_order(purchase(run, ctx))
    run.facts["orderS8"] = oid
    if ctx.decisions and ctx.decisions[-1]["status"] == "executed":
        wait_order(run, oid, {"released"})
        ctx.require(order_outcome(run, oid) == ctx.expect["delivery"], f"S8 outcome {order_outcome(run, oid)}")


def s9(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    run.facts["orderS9"] = last_order(purchase(run, ctx))


def s10(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    oid = last_order(purchase(run, ctx))
    run.facts["orderS10"] = oid
    if ctx.decisions and ctx.decisions[-1]["status"] == "executed":
        wait_order(run, oid, {"delivered"})
    run.env.rescore(1)
    result = run.env.evaluate()
    ctx.data["evaluate"] = {k: result.get(k) for k in ("rule", "fromTier", "toTier", "reasonCode")}
    # the operator also evaluates on its own every poll_s; either way the vault must reach tier 2
    if not run.diverged:
        wait_for(run, "tier 2", lambda: run.wallet_tier() == 2, timeout=120)


# ---------------------------------------------------------------------------
# S11-S14 defaults, refusal, stolen key, refund
# ---------------------------------------------------------------------------


def s11(run: Runner, ctx: StepCtx) -> None:
    env = run.env
    ensure_services(run)
    # The wallet agent is not an actor here. It is paused while the market round warps the clock, so its
    # timer-driven refund of the S9 escrow is filed after this rescore and graded in S16 (spec G10).
    env.stop_service("agent")
    try:
        g = env.sim("ghost_default_round", step="S11")
        refunds = (g.get("result") or {}).get("refunds") or {}
        ctx.require(len(refunds) == 3, f"Beacon refunded {len(refunds)} of 3 Ghost escrows")
        env.sim("boost_ghost", step="S11")
        env.sim("smear_atlas", step="S11")
        env.rescore(1)
    finally:
        env.start_service("agent")


def s12(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    run.facts["orderS12"] = last_order(purchase(run, ctx))


def s13(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    vault = wallet_vault(run)
    fresh = chash({"schema": "acb-stolen-key/1", "step": "S13", "run": run.env.run_id})
    stolen_send(run, ctx, vault.pay(outflow_params(run, fresh, "ghost", 200_000)), "pay grade-D Ghost under M1")
    replay = run.facts.get("orderS6")
    if not replay:
        raise StepFailure("no S6 order to replay")
    stolen_send(run, ctx, vault.pay(outflow_params(run, replay, "atlas", 200_000)), "replay the S6 order id")


def s14(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    oid = run.facts.get("orderS9")
    if not oid:
        raise StepFailure("no S9 escrow")
    order = wallet_vault(run).get_order(oid)
    if order.state.name != "ESCROWED" and order.state.name != "REFUNDED":
        if run.real_llm:
            ctx.note(f"the S9 order is {order.state.name} after a stricter LLM choice: nothing to refund")
            run.diverged = True
            ctx.expect = {}
            return
        raise StepFailure(f"the S9 order is {order.state.name}, not an open escrow")
    if run.env.now() <= int(order.deadline) + 30:
        run.env.advance_to(int(order.deadline) + 31)
    rec = wait_order(run, oid, {"refunded", "closed_refunded"})
    ctx.data["orderStatus"] = rec.get("status")

    def reported() -> bool:
        return any(
            e["data"].get("kind") == "NON_DELIVERY"
            and str((e["data"].get("subject") or {}).get("agentId")) == str(run.agent_id("nova"))
            and e["data"].get("status") == "executed"
            for e in run.logs["agent"].since(0)
            if e.get("type") == "report"
        )

    wait_for(run, "the NON_DELIVERY report on Nova", reported)
    run.recorder.scan(ctx.id)
    info = wallet_vault(run).get_order(oid)
    ctx.require(info.state.name == "REFUNDED", f"S9 escrow is {info.state.name}")
    refund = [r for r in run.recorder.records if r["function"] == "refund" and _has_order(r, "EscrowRefunded", oid)]
    reason = [int(a["reasonCode"]) for r in refund for a in run.recorder.events_of(r, "EscrowRefunded")]
    ctx.decisions.append({"outcome": "REFUND", "reasons": reason[:1], "orderId": oid, "status": "executed"})


def _has_order(rec: dict[str, Any], event: str, order_id: str) -> bool:
    return any(
        lg["event"] == event and str(lg["args"].get("orderId", "")).lower() == order_id.lower() for lg in rec["_events"]
    )


def claim_s14(run: Runner) -> None:
    """The S9 escrow refund and its NON_DELIVERY report belong to S14 whenever they were mined."""
    oid = run.facts.get("orderS9")
    if not oid or run.recorder is None:
        return
    nova = run.agent_id("nova")
    signer = {run.env.dep.roles.get("agentSigner", ""), run.env.dep.roles.get("agentSignerNext", "")}
    for r in run.recorder.records:
        if r["function"] == "refund" and _has_order(r, "EscrowRefunded", oid):
            run.recorder.claim(r["txHash"], "S14")
        elif r["function"] == "giveFeedback" and r["actorAddress"] in signer:
            for a in run.recorder.events_of(r, "NewFeedback"):
                if int(a["agentId"]) == nova and a.get("tag2") == "NON_DELIVERY":
                    run.recorder.claim(r["txHash"], "S14")


# ---------------------------------------------------------------------------
# S15-S17 injection, drain, frozen
# ---------------------------------------------------------------------------


def s15(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    answer = run.env.switch_llm(mode="mock", persona="naive", maskFlaggedSpans=False)
    run.facts["llmSwitched"] = True
    ctx.data["llm"] = answer.get("llm")
    results = purchase(run, ctx)
    run.facts["orderS15"] = last_order(results)
    ctx.data["reply"] = next((r.get("reply") for r in results if r.get("reply")), None)
    _expect_injection_report(run, ctx, run.facts["orderS15"])


def _expect_injection_report(run: Runner, ctx: StepCtx, order_id: str) -> None:
    def filed() -> bool:
        return any(
            e["data"].get("kind") == "INJECTION_ATTEMPT"
            and str(e["data"].get("orderId", "")).lower() == order_id
            and e["data"].get("status") == "executed"
            for e in run.logs["agent"].since(ctx.marks.get("agent", 0))
            if e.get("type") == "report"
        )

    wait_for(run, "the INJECTION_ATTEMPT report", filed)


def s16(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    try:
        results = purchase(run, ctx)
        run.facts["orderS16"] = last_order(results)
        run.env.rescore(1)
    finally:
        run.env.switch_llm(
            mode="mock" if run.env.llm_mode == "mock" else run.env.llm_mode, persona="careful", maskFlaggedSpans=True
        )
        run.facts["llmSwitched"] = False


def s17(run: Runner, ctx: StepCtx) -> None:
    ensure_services(run)
    run.facts["orderS17"] = last_order(purchase(run, ctx))
    fresh = chash({"schema": "acb-stolen-key/1", "step": "S17", "run": run.env.run_id})
    stolen_send(run, ctx, wallet_vault(run).pay(outflow_params(run, fresh, "atlas", 200_000)), "pay Atlas while frozen")


# ---------------------------------------------------------------------------
# S18 key rotation and maker-checker approval
# ---------------------------------------------------------------------------


def s18(run: Runner, ctx: StepCtx) -> None:
    env = run.env
    ensure_services(run)
    owner = env.owner()
    new_agent = env.keys.address("agent_signer_next")
    result = owner.unfreeze(0, new_agent)
    ctx.data["unfreeze"] = result
    run.facts["signerRotated"] = True
    env.restart_service("signer", {"AGENT_SIGNER_PRIVATE_KEY": rotated_signer_key(run)})
    health = env.must("signer", "GET", "/health")
    ctx.require(str(health.get("address", "")).lower() == new_agent.lower(), f"signer runs as {health.get('address')}")
    results = purchase(run, ctx)
    oid = last_order(results)
    run.facts["orderS18"] = oid
    pending = [r for r in results if r.get("outcome") == "NEEDS_OWNER_APPROVAL"]
    if not pending:
        if run.real_llm:
            ctx.note("the LLM chose a stricter action than the owner approval; nothing to approve")
            run.diverged = True
            return
        raise StepFailure("the invoice did not ask for the owner's approval")
    card = owner.prepare_approval(oid)
    ctx.data["approvalCard"] = {k: card.get(k) for k in ("values", "problems", "warnings", "code")}
    ctx.require(not card.get("problems"), f"approval card problems: {card.get('problems')}")
    approved = owner.approve(oid, card["prepareId"])
    answer = (approved.get("agent") or {}).get("answer") or {}
    record_result(run, ctx, answer)
    wait_order(run, oid, {"delivered"})


# ---------------------------------------------------------------------------
# S19 unsolicited invoices, S20 appeal, S21 recompute and verify
# ---------------------------------------------------------------------------


def s19(run: Runner, ctx: StepCtx) -> None:
    env = run.env
    ensure_services(run)
    mark = ctx.marks.get("agent", 0)
    env.sim("push_spam", step="S19")
    env.sim("push_duplicate", step="S19")
    blaster = str(run.agent_id("blaster"))

    def done() -> Any:
        events = run.logs["agent"].since(mark)
        drops = [e for e in events if e.get("type") == "decision" and e["data"].get("outcome") == "DROPPED"]
        spam = [e for e in events if e.get("type") == "report" and e["data"].get("kind") == "SPAM"]
        return (drops, spam) if len(drops) >= 5 and spam else None

    drops, spam = wait_for(run, "5 dropped invoices and the SPAM report", done)
    for e in drops:
        ctx.offchain.append(
            {
                "outcome": "DROPPED",
                "reasons": list(e.get("reason_codes") or []),
                "invoiceHash": e["data"].get("invoiceHash"),
                "rule": e["data"].get("rule"),
                "payee": blaster,
                "logLine": f"{e.get('ts')} S19 DROPPED Blaster Ads #{blaster} reasons={','.join(str(c) for c in e.get('reason_codes') or [])}",
            }
        )
    ctx.require(spam[-1]["data"].get("status") == "executed", f"SPAM report {spam[-1]['data'].get('status')}")
    turns = [
        e
        for e in run.logs["agent"].since(mark)
        if e.get("type") == "decision" and e["data"].get("outcome") not in ("DROPPED",)
    ]
    ctx.require(not turns, f"unsolicited invoices must not reach a decision turn ({len(turns)} did)")


def s20(run: Runner, ctx: StepCtx) -> None:
    from acb.scenario.appeals import resolve_appeal, review_appeal

    env = run.env
    ensure_services(run)
    filed = env.sim("ghost_appeal", step="S20")
    appeal_id = int((filed.get("result") or {}).get("appealId") or 0)
    ctx.require(appeal_id > 0, "no appeal id")
    review = review_appeal(
        env.w3,
        env.dep,
        appeal_id,
        payload_dirs=[env.out_dir / "appeals"],
        out_dir=env.out_dir,
        bundle_dirs=[env.out_dir],
    )
    adverse = review["review"]["adverseEvidence"]
    ctx.require(adverse["source"] == "bundle", f"Ghost's posted bundle was not found ({adverse['source']})")
    named = review["review"]["orders"]
    ctx.require(
        bool(named) and all(o["adverseEvidence"] for o in named),
        "the appeal should name Ghost's refunded orders, which are adverse evidence in its bundle",
    )
    sender = env.sender("bureau", bureau_account(run))
    tx = resolve_appeal(sender, env.w3, env.dep, appeal_id, review["upheld"], review["evidenceHash"])
    ctx.require(tx.status == 1, f"resolveAppeal reverted: {tx.revert_error}")
    run.facts["appeal"] = {
        "appealId": appeal_id,
        "upheld": review["upheld"],
        "evidenceHash": review["evidenceHash"],
        "tx": tx.hash,
    }
    ctx.require(not review["upheld"], "the appeal should be rejected: Ghost never claimed the orders")
    ctx.data["review"] = {
        "decision": review["review"]["decision"],
        "reasonCode": review["review"]["reasonCode"],
        "evidenceHash": review["evidenceHash"],
    }


def bureau_account(run: Runner) -> Any:
    """The bureau's poster key (Anvil: the public test key; Base Sepolia: ``BUREAU_PRIVATE_KEY``)."""
    return run.env.keys.account("bureau")


def s21(run: Runner, ctx: StepCtx) -> None:
    from acb.scenario import evidence as ev

    env = run.env
    results = []
    for item in ctx.expect.get("recompute") or []:
        posting = _posting_tx(run, item["step"], item["agent"])
        if posting is None:
            ctx.fail(f"no {item['agent']} posting in {item['step']}")
            continue
        outcome = recompute_tx(run, posting)
        results.append({"step": item["step"], "agent": item["agent"], "tx": posting, "result": outcome["result"]})
        ctx.require(
            outcome["result"] == "MATCH",
            f"recompute of {item['agent']} ({item['step']}): {outcome['result']} {outcome.get('detail', '')}",
        )
    # every grade change of the run is recomputed for the evidence file
    run.facts["recompute"] = results
    ctx.data["recompute"] = results
    run.recorder.scan("S21")
    every: dict[str, str] = {}
    for r, lg in run.recorder.iter_events(None, "GradePosted"):
        if int(lg["args"]["grade"]) != int(lg["args"]["previousGrade"]) and r["txHash"] not in every:
            outcome = recompute_tx(run, r["txHash"])
            # the run publishes every bundle it posts: a missing file is a failure, not a hash-only match
            every[r["txHash"]] = outcome["result"] if outcome.get("bundle") else "BUNDLE_NOT_PUBLISHED"
    run.facts["recomputeAll"] = every
    bad = {h: v for h, v in every.items() if v != "MATCH"}
    ctx.require(not bad, f"grade changes that do not recompute: {bad}")
    ctx.data["recomputedGradeChanges"] = len(every)
    built = ev.build_run_evidence(run)
    ctx.data["evidence"] = {k: built[k] for k in ("folder", "transactions", "headline")}
    # 4 requests per second on a public RPC (10.5); the local node takes more
    rps = 50.0 if env.network == "anvil" else 4.0
    report = ev.verify_folder(Path(built["folder"]), rpc=env.rpc_url or env.net.rpc_url, rps=rps)
    for line in report["lines"][-3:]:
        _log(run, line)
    ctx.data["verify"] = {
        "verified": report["verified"],
        "total": report["total"],
        "offline": report["offlineVerified"],
    }
    ctx.require(
        report["ok"], f"verify_evidence: {report['verified']} / {report['total']} verified ({report['failures'][:3]})"
    )
    ctx.require(
        report["offlineOk"], f"verify_evidence --receipts-only: {report['offlineVerified']} / {report['total']}"
    )


def _posting_tx(run: Runner, step: str, key: str) -> str | None:
    aid = run.agent_id(key)
    for r, lg in run.recorder.iter_events(step, "GradePosted"):
        if int(lg["args"]["agentId"]) == aid and int(lg["args"]["grade"]) != int(lg["args"]["previousGrade"]):
            return r["txHash"]
    return None


def run_bundle_path(out_dir: Path, agent_id: int, as_of_block: int) -> Path:
    """Where this run's bureau wrote the bundle ``<agentId>-<asOfBlock>.json``."""
    from acb.bureau.evidence import bundle_name

    return Path(out_dir) / "bundles" / bundle_name(agent_id, as_of_block)


def recompute_tx(run: Runner, tx_hash: str) -> dict[str, Any]:
    """Recompute the ``GradePosted`` of ``tx_hash`` against this run's own published bundle.

    On Anvil the on-chain ``evidenceURI`` is a repository path (``evidence/anvil/bundles/<agentId>-<asOfBlock>.json``)
    and agent ids and blocks are deterministic, so the URI can name a committed file from an earlier exported
    run with the same name but other content. The run's bundle in ``out_dir`` is the published file of this
    run, so the posting is recomputed with its URI pinned to that file; the committed ``evidence/`` folder is
    never read as a bundle source here. Invoices and reports stay hash-checked reads (``EvidenceStore``).
    """
    import dataclasses

    from web3 import Web3

    from acb.bureau.evidence import EvidenceStore
    from acb.bureau.recompute import POSTING_NOT_FOUND, find_posting_by_tx, recompute_posting
    from acb.chain.client import make_web3

    env = run.env
    acb_address = env.dep.contracts["AgentCreditBureau"].address
    posting = find_posting_by_tx(env.w3, acb_address, Web3.to_hex(Web3.to_bytes(hexstr=tx_hash)))
    if posting is None:
        return {"result": POSTING_NOT_FOUND, "detail": "no GradePosted log found", "agentId": None, "asOfBlock": None}
    agent_id = int(posting.args["agentId"])
    as_of = int(posting.args["asOfBlock"])
    local = run_bundle_path(env.out_dir, agent_id, as_of).resolve()
    posting = dataclasses.replace(posting, args={**dict(posting.args), "evidenceURI": str(local)})
    store = EvidenceStore([str(env.out_dir), str(config.REPO_ROOT / "evidence" / env.network)])
    w3_logs = env.w3 if env.network == "anvil" else make_web3(env.net, "logs")
    result = recompute_posting(posting, w3_logs=w3_logs, dep=env.dep, store=store, bundle_dirs=(str(env.out_dir),))
    out = result.to_json()
    return {
        "result": out["result"],
        "detail": out.get("detail"),
        "agentId": out.get("agentId"),
        "asOfBlock": out.get("asOfBlock"),
        "bundle": str(local) if local.is_file() else None,
    }


STEPS: dict[str, Callable[[Runner, StepCtx], None]] = {
    "S0": s0,
    "S1": s1,
    "S2": s2,
    "S3": s3,
    "S4": s4,
    "S5": s5,
    "S6": s6,
    "S7": s7,
    "S8": s8,
    "S9": s9,
    "S10": s10,
    "S11": s11,
    "S12": s12,
    "S13": s13,
    "S14": s14,
    "S15": s15,
    "S16": s16,
    "S17": s17,
    "S18": s18,
    "S19": s19,
    "S20": s20,
    "S21": s21,
}

CLAIMS: tuple[Callable[[Runner], None], ...] = (claim_s14,)
"""Transactions mined asynchronously that belong to a step by content (see :meth:`Recorder.claim`)."""


__all__ = ["CLAIMS", "STEPS", "ensure_services", "purchase", "recompute_tx", "run_bundle_path", "wait_order"]
