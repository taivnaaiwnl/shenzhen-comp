"""One running system for the scenario runner and the suites (spec 2.1, 10.1, 13.4).

An :class:`Env` owns, for one run:

- the chain: on Anvil a fresh node started with ``--block-time 1 --accounts 30 --chain-id 31337`` on a
  free port (stopped at the end); on Base Sepolia the public RPC of ``.env``;
- the key ring (Anvil: the public test mnemonic; Base Sepolia: ``<ROLE>_PRIVATE_KEY`` from ``.env``);
- the service tokens (Anvil: random values made for this run; Base Sepolia: from ``.env``);
- the six services P1-P6, started by :class:`acb.launcher.Launcher` with their allow-listed environments
  (each one sees only its own key and tokens);
- HTTP helpers for the service APIs and an in-process owner console (the runner is P7 and drives the
  owner role with the owner key, spec 2.1).

Runtime output goes to ``ACB_OUT_DIR`` (default ``runs/<run_id>/out/<network>/``), never to
``evidence/``.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from acb import config

SERVICE_ORDER: tuple[str, ...] = ("signer", "bureau", "autonomy", "sim", "agent", "owner")
"""Start order: the signer first (the agent and the autonomy operator call it), the owner console last."""

ROLE_PORT_VAR = {
    "owner": "OWNER_PORT",
    "agent": "AGENT_PORT",
    "signer": "SIGNER_PORT",
    "bureau": "BUREAU_PORT",
    "sim": "SIM_PORT",
    "autonomy": "AUTONOMY_PORT",
}
DEFAULT_PORTS = {"owner": 8700, "agent": 8701, "signer": 8702, "bureau": 8703, "sim": 8704, "autonomy": 8705}
LOOPBACK = "127.0.0.1"
PUBLIC_EVIDENCE_BASE = "https://raw.githubusercontent.com/taivnaaiwnl/shenzhen-comp/main/evidence/{network}/"
SCENARIO_MOCK_SCRIPT = config.REPO_ROOT / "config" / "mock_llm" / "scenario.yaml"
TOKEN_VARS = (
    "SIGNER_TOKEN",
    "AGENT_TOKEN",
    "AGENT_ADMIN_TOKEN",
    "BUREAU_TOKEN",
    "AUTONOMY_TOKEN",
    "SIM_TOKEN",
    "EVENTS_TOKEN",
    "OWNER_UI_PASSWORD",
    "SESSION_SECRET",
)


class EnvError(RuntimeError):
    """The system could not be brought up or a service call failed."""


def new_run_id() -> str:
    """``r-YYYYMMDD-HHMMSS`` (UTC); seconds so that two runs in a row never share a folder."""
    return time.strftime("r-%Y%m%d-%H%M%S", time.gmtime())


def _free_port(taken: set[int]) -> int:
    from acb.chain.anvil import free_port

    for _ in range(50):
        port = free_port()
        if port not in taken:
            taken.add(port)
            return port
    raise EnvError("no free port found")


def _is_secret_var(name: str) -> bool:
    upper = name.upper()
    return upper.endswith(("_PRIVATE_KEY", "_MNEMONIC"))


@dataclass
class ServiceCall:
    """One HTTP answer of a service (status and parsed JSON)."""

    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class Env:
    """A running system: chain, deployment, services and clients (see the module docstring)."""

    network: str
    run_id: str
    out_dir: Path
    llm_mode: str = "mock"
    block_time: int = 1
    fixed_ports: bool = False
    with_owner_ui: bool = True
    anvil: Any = None
    rpc_url: str | None = None
    source_env: dict[str, str] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)
    net: Any = None
    w3: Any = None
    keys: Any = None
    dep: Any = None
    launcher: Any = None
    _senders: dict[str, Any] = field(default_factory=dict)
    _owner: Any = None
    _saved_env: dict[str, str | None] = field(default_factory=dict)
    _locked: bool = False

    # ------------------------------------------------------------------ construction

    @classmethod
    def create(
        cls,
        network: str,
        *,
        run_id: str | None = None,
        out_dir: str | os.PathLike[str] | None = None,
        llm_mode: str = "mock",
        fixed_ports: bool = False,
        with_owner_ui: bool = True,
        block_time: int = 1,
        environ: Mapping[str, str] | None = None,
    ) -> Env:
        """Prepare (not start) an environment. ``environ`` defaults to ``os.environ`` (``.env`` already
        loaded by the CLI where the network needs it)."""
        if network not in ("anvil", "base_sepolia"):
            raise EnvError(f"unknown network {network!r}")
        if llm_mode not in ("mock", "openai", "auto"):
            raise EnvError("llm mode must be mock, openai or auto")
        rid = run_id or new_run_id()
        out = Path(out_dir) if out_dir else config.REPO_ROOT / "runs" / rid / "out" / network
        out.mkdir(parents=True, exist_ok=True)
        env = cls(
            network=network,
            run_id=rid,
            out_dir=out,
            llm_mode=llm_mode,
            fixed_ports=fixed_ports,
            with_owner_ui=with_owner_ui,
            block_time=block_time,
        )
        env._prepare_source_env(dict(os.environ if environ is None else environ))
        return env

    def _prepare_source_env(self, source: dict[str, str]) -> None:
        """The environment the launcher filters for each child, plus this process's own settings."""
        if self.network == "anvil":
            # Anvil keys come from the public test mnemonic only: a testnet key in .env must never be
            # handed to a service of a local run (it would sign for the wrong account).
            source = {k: v for k, v in source.items() if not _is_secret_var(k)}
            for name in TOKEN_VARS:
                source[name] = secrets.token_hex(24)
        else:
            missing = [name for name in TOKEN_VARS if not source.get(name)]
            if missing:
                raise EnvError(f"{', '.join(missing)} not set in .env: run `acb keys init --network base_sepolia`")
        self.tokens = {name: source[name] for name in TOKEN_VARS}
        taken: set[int] = set()
        for role, var in ROLE_PORT_VAR.items():
            if self.fixed_ports:
                port = int(source.get(var) or DEFAULT_PORTS[role])
            else:
                port = _free_port(taken)
            self.ports[role] = port
            source[var] = str(port)
        source["NETWORK"] = self.network
        source["ACB_RUN_ID"] = self.run_id
        source["ACB_OUT_DIR"] = str(self.out_dir)
        base = (source.get("EVIDENCE_BASE_URI") or "").strip()
        if self.network == "anvil":
            base = "evidence/anvil/"
        elif not base.startswith(("http://", "https://")):
            # the URI goes on-chain permanently: never a relative path on a public network
            base = PUBLIC_EVIDENCE_BASE.format(network=self.network)
        source["EVIDENCE_BASE_URI"] = base
        source["LLM_MODE"] = self.llm_mode
        source["MOCK_LLM_SCRIPT"] = str(SCENARIO_MOCK_SCRIPT)
        source["MOCK_LLM_PERSONA"] = "careful"
        source["ACB_ALLOW_LLM_SWITCH"] = "1"
        self.source_env = source

    # ------------------------------------------------------------------ chain

    def start_chain(self) -> None:
        """Anvil: start a fresh node on a free port. Base Sepolia: connect to the configured RPC."""
        from acb.chain.anvil import start_anvil

        self.lock()
        if self.network == "anvil":
            self.anvil = start_anvil(block_time=self.block_time, accounts=30, chain_id=31337)
            self.rpc_url = self.anvil.url
            self.source_env["ANVIL_RPC_URL"] = self.rpc_url
        self.connect()

    def attach_chain(self, rpc_url: str) -> None:
        """Re-use an existing chain (``--from`` on a kept Anvil node)."""
        self.lock()
        self.rpc_url = rpc_url
        if self.network == "anvil":
            self.source_env["ANVIL_RPC_URL"] = rpc_url
        self.connect()

    def connect(self) -> None:
        from acb.chain.client import make_web3
        from acb.config import load_network
        from acb.keys import TESTNET_ROLES, KeyRing, env_var

        self._export_process_env()
        self.net = load_network(self.network, self.source_env)
        self.rpc_url = self.rpc_url or self.net.rpc_url
        self.w3 = make_web3(self.net)
        if int(self.w3.eth.chain_id) != int(self.net.chain_id):
            raise EnvError(f"RPC chain id {self.w3.eth.chain_id} is not {self.net.chain_id}")
        if self.network == "anvil":
            self.keys = KeyRing.anvil()
        else:
            roles = [r for r in TESTNET_ROLES if self.source_env.get(env_var(r))]
            self.keys = KeyRing.from_env(roles, self.source_env)

    def _export_process_env(self) -> None:
        """Settings this process itself needs (in-process owner console, bureau reads): never secrets."""
        for name in (
            "NETWORK",
            "ACB_RUN_ID",
            "ACB_OUT_DIR",
            "EVIDENCE_BASE_URI",
            "ANVIL_RPC_URL",
            *ROLE_PORT_VAR.values(),
            "AGENT_TOKEN",
            "EVENTS_TOKEN",
        ):
            if self.source_env.get(name):
                self._saved_env.setdefault(name, os.environ.get(name))
                os.environ[name] = self.source_env[name]
        # The services read deployments/<network>.json of the repository (ACB_DEPLOYMENTS_DIR is not in
        # their allow-list), so this process must write and read the same file.
        if "ACB_DEPLOYMENTS_DIR" in os.environ:
            self._saved_env.setdefault("ACB_DEPLOYMENTS_DIR", os.environ.get("ACB_DEPLOYMENTS_DIR"))
            os.environ.pop("ACB_DEPLOYMENTS_DIR", None)

    def restore_process_env(self) -> None:
        """Undo :meth:`_export_process_env` (tests run several environments in one process)."""
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._saved_env.clear()

    def load_deployments(self) -> Any:
        from acb.chain.deployments import load_deployments

        self.dep = load_deployments(self.network)
        return self.dep

    # ------------------------------------------------------------------ transactions of the runner

    def sender(self, role: str, account: Any = None) -> Any:
        """A cached :class:`~acb.chain.tx.TxSender` for a key-ring role (or an explicit account)."""
        from acb.chain.tx import TxSender

        key = role if account is None else f"{role}:{account.address.lower()}"
        if key not in self._senders:
            acct = account if account is not None else self.keys.account(role)
            self._senders[key] = TxSender(self.w3, acct)
        return self._senders[key]

    def contracts(self) -> Any:
        from acb.chain.contracts import bind

        return bind(self.w3, self.dep)

    def now(self) -> int:
        from acb.chain.clock import now

        return now(self.w3)

    def warp(self, seconds: int) -> int:
        """Anvil: move the chain clock forward. Base Sepolia: wait in real time."""
        from acb.chain.clock import advance_to, warp

        if self.network == "anvil":
            return warp(self.w3, int(seconds))
        return advance_to(self.w3, self.now() + int(seconds), timeout_s=float(seconds) + 600.0)

    def advance_to(self, timestamp: int) -> int:
        from acb.chain.clock import advance_to

        return advance_to(self.w3, int(timestamp), timeout_s=3600.0)

    # ------------------------------------------------------------------ services

    def make_launcher(self) -> Any:
        from acb.launcher import Launcher

        if self.launcher is None:
            self.launcher = Launcher(
                self.network, run_id=self.run_id, out_dir=str(self.out_dir), source_env=self.source_env
            )
        return self.launcher

    def roles(self) -> tuple[str, ...]:
        return tuple(r for r in SERVICE_ORDER if r != "owner" or self.with_owner_ui)

    def sync_deployments_file(self) -> None:
        """The services read ``deployments/<network>.json`` of the repository when they start: make sure it
        is this run's deployment (another local run may have written it meanwhile)."""
        from acb.chain.deployments import save_deployments, try_load_deployments

        if self.dep is None:
            return
        current = try_load_deployments(self.network)
        if current is None or current.to_json_dict() != self.dep.to_json_dict():
            save_deployments(self.dep)

    def start_services(self, roles: Iterable[str] | None = None, *, timeout: float = 60.0) -> None:
        self.sync_deployments_file()
        launcher = self.make_launcher()
        for role in roles or self.roles():
            launcher.start(role)
            self.wait_healthy(role, timeout=timeout)

    def restart_service(self, role: str, overrides: Mapping[str, str] | None = None, *, timeout: float = 60.0) -> None:
        self.sync_deployments_file()
        launcher = self.make_launcher()
        launcher.restart(role, overrides)
        self.wait_healthy(role, timeout=timeout)

    def stop_service(self, role: str) -> str:
        return self.make_launcher().stop(role)

    def start_service(self, role: str, overrides: Mapping[str, str] | None = None, *, timeout: float = 60.0) -> None:
        self.sync_deployments_file()
        self.make_launcher().start(role, overrides)
        self.wait_healthy(role, timeout=timeout)

    def wait_healthy(self, role: str, *, timeout: float = 60.0) -> dict[str, Any]:
        """Wait until ``GET /health`` of ``role`` answers (or the child exits)."""
        deadline = time.monotonic() + timeout
        child = self.make_launcher().children.get(role)
        last = "no answer"
        while time.monotonic() < deadline:
            if child is not None and child.proc is not None and child.proc.poll() is not None:
                raise EnvError(f"{role} exited with code {child.proc.returncode}: {self.service_log_tail(role)}")
            try:
                r = httpx.get(f"http://{LOOPBACK}:{self.ports[role]}/health", timeout=2.0)
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}"
            except (httpx.HTTPError, ValueError) as exc:
                last = type(exc).__name__
            time.sleep(0.3)
        raise EnvError(f"{role} did not become healthy ({last}): {self.service_log_tail(role)}")

    def service_log_tail(self, role: str, lines: int = 15) -> str:
        path = self.out_dir / f"{role}.out.txt"
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return " | ".join(text[-lines:])[-1500:]

    def url(self, role: str, path: str) -> str:
        return f"http://{LOOPBACK}:{self.ports[role]}{path}"

    def call(
        self,
        role: str,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: Any = None,
        timeout: float = 120.0,
    ) -> ServiceCall:
        """One request to a service; ``token`` is the name of a token variable (e.g. ``AGENT_TOKEN``)."""
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {self.tokens[token]}"
        try:
            r = httpx.request(method, self.url(role, path), json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise EnvError(f"{role} {method} {path}: {type(exc).__name__}: {exc}") from None
        try:
            parsed = r.json()
        except ValueError:
            parsed = r.text
        return ServiceCall(r.status_code, parsed)

    def must(self, role: str, method: str, path: str, **kw: Any) -> Any:
        """Like :meth:`call` but raises on a non-2xx answer."""
        answer = self.call(role, method, path, **kw)
        if not answer.ok:
            raise EnvError(f"{role} {method} {path} -> HTTP {answer.status}: {json.dumps(answer.body)[:600]}")
        return answer.body

    # ---- convenience calls --------------------------------------------------------------------

    def sim(self, action: str, args: Mapping[str, Any] | None = None, *, step: str | None = None) -> dict[str, Any]:
        """``POST /admin/run`` of the simulator (synchronous; warm-up and rounds take a while)."""
        payload = dict(args or {})
        if step:
            payload["step"] = step
        return self.must(
            "sim", "POST", "/admin/run", token="SIM_TOKEN", body={"action": action, "args": payload}, timeout=900.0
        )

    def rescore(self, rounds: int = 1) -> dict[str, Any]:
        return self.must("bureau", "POST", "/rescore", token="BUREAU_TOKEN", body={"rounds": rounds}, timeout=900.0)

    def evaluate(self) -> dict[str, Any]:
        return self.must("autonomy", "POST", "/evaluate", token="AUTONOMY_TOKEN", body=None, timeout=300.0)

    def agent_state(self) -> dict[str, Any]:
        return self.must("agent", "GET", "/state", token="EVENTS_TOKEN")

    def purchase(self, mandate_id: str, items: list[dict[str, Any]]) -> ServiceCall:
        return self.call(
            "agent", "POST", f"/purchase/{mandate_id}", token="AGENT_TOKEN", body={"items": items}, timeout=600.0
        )

    def chat(self, message: str) -> dict[str, Any]:
        return self.must("agent", "POST", "/chat", token="AGENT_TOKEN", body={"message": message}, timeout=180.0)

    def switch_llm(self, **body: Any) -> dict[str, Any]:
        return self.must("agent", "POST", "/admin/llm", token="AGENT_ADMIN_TOKEN", body=body, timeout=60.0)

    def deliveries_check(self) -> dict[str, Any]:
        return self.must("agent", "POST", "/deliveries/check", token="AGENT_TOKEN", body=None, timeout=300.0)

    # ------------------------------------------------------------------ the owner role (in process)

    def owner(self) -> Any:
        """An in-process owner console (same verification code as P1) with the owner key."""
        if self._owner is None:
            from acb.logbus import EventBus
            from acb.owner import chain_views as cv
            from acb.owner.actions import OwnerConsole
            from acb.owner.agent_client import AgentClient

            agent = AgentClient(
                f"http://{LOOPBACK}:{self.ports['agent']}",
                agent_token=self.tokens["AGENT_TOKEN"],
                events_token=self.tokens["EVENTS_TOKEN"],
                timeout=300.0,
            )
            bus = EventBus("runner", run_id=self.run_id, chain_id=int(self.net.chain_id), out_dir=self.out_dir)
            network = self.network
            w3 = self.w3
            self._owner = OwnerConsole(
                chain_factory=lambda: cv.OwnerChain.load(network, dep=self.dep, w3=w3),
                agent=agent,
                bus=bus,
                owner_account=self.keys.account("owner"),
            )
        return self._owner

    def reset_owner(self) -> None:
        """Forget the cached owner console (after a redeploy or a key rotation)."""
        self._owner = None

    # ------------------------------------------------------------------ shutdown

    def stop(self, *, keep_chain: bool = False) -> None:
        if self.launcher is not None:
            try:
                self.launcher.stop_all()
            except Exception:  # noqa: BLE001, S110 - best effort at shutdown
                pass
        if self.anvil is not None and not keep_chain:
            try:
                self.anvil.stop()
            except Exception:  # noqa: BLE001, S110 - best effort at shutdown
                pass
        self.restore_process_env()
        self.unlock()

    # ------------------------------------------------------------------ one local run at a time

    def lock_path(self) -> Path:
        return config.data_dir(self.network) / "run.lock"

    def lock(self) -> None:
        """Refuse to run next to another run of the same network on this machine.

        The services keep their state in ``data/<network>/agent|signer/`` and read
        ``deployments/<network>.json`` of the repository (their environment allow-list has no override),
        so two local runs would overwrite each other's state.
        """
        from acb.launcher import _pid_alive, process_start_token

        path = self.lock_path()
        if path.is_file():
            try:
                held = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                held = {}
            pid = int(held.get("pid") or 0)
            if pid and pid != os.getpid() and _pid_alive(pid) and process_start_token(pid) == held.get("token"):
                raise EnvError(
                    f"another {self.network} run ({held.get('runId')}, pid {pid}) is active on this machine; "
                    "local runs share data/ and deployments/ and must run one at a time"
                )
        body = {"pid": os.getpid(), "token": process_start_token(os.getpid()), "runId": self.run_id}
        path.write_text(json.dumps(body), encoding="utf-8")
        self._locked = True

    def unlock(self) -> None:
        if not self._locked:
            return
        path = self.lock_path()
        try:
            held = json.loads(path.read_text(encoding="utf-8"))
            if int(held.get("pid") or 0) == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass
        self._locked = False


__all__ = [
    "DEFAULT_PORTS",
    "LOOPBACK",
    "PUBLIC_EVIDENCE_BASE",
    "ROLE_PORT_VAR",
    "SERVICE_ORDER",
    "Env",
    "EnvError",
    "ServiceCall",
    "new_run_id",
]
