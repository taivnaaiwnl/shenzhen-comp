"""Start and stop the services P1-P6 as child processes with scrubbed environments (spec 2.1, 13.4).

- Children run ``sys.executable -m acb <role> serve`` (not ``uv run``) from the repository root, on
  Windows with ``CREATE_NEW_PROCESS_GROUP``.
- Each child receives only the variables allowed for its role in ``config/processes.yaml`` plus the
  ``base`` list, matched case-insensitively (``*`` wildcards allowed), and ``PYTHONUTF8=1``,
  ``ACB_PROCESS_ROLE``, ``ACB_RUN_ID``, ``ACB_OUT_DIR`` and ``NETWORK``.
- Stopping sends ``CTRL_BREAK_EVENT`` (``SIGTERM`` elsewhere), waits 5 s, then terminates.
- ``acb up`` records the children in ``data/<network>/launcher.json`` so that ``acb down`` (another
  process) can stop them. Each child is recorded with its PID and a start token (the process creation
  time); ``acb down`` signals a PID only when the token still matches, so a PID reused after a crash
  or reboot is never killed.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from acb import config

IS_WINDOWS = os.name == "nt"
STOP_GRACE_S = 5.0

SERVICE_ROLES: tuple[str, ...] = ("signer", "bureau", "autonomy", "sim", "agent", "owner")
"""Start order of ``acb up`` (the owner console last, the signer first)."""

ROLE_PORT_VAR = {
    "owner": "OWNER_PORT",
    "agent": "AGENT_PORT",
    "signer": "SIGNER_PORT",
    "bureau": "BUREAU_PORT",
    "sim": "SIM_PORT",
    "autonomy": "AUTONOMY_PORT",
}
DEFAULT_PORTS = {"owner": 8700, "agent": 8701, "signer": 8702, "bureau": 8703, "sim": 8704, "autonomy": 8705}
HOST = "127.0.0.1"


# ---------------------------------------------------------------------------
# environment filtering
# ---------------------------------------------------------------------------


def allowed_patterns(role: str, processes: Mapping[str, list[str]] | None = None) -> list[str]:
    """``base`` plus the role's patterns from ``config/processes.yaml`` (upper-cased)."""
    procs = processes if processes is not None else config.processes_config()
    if role not in procs or role == "base":
        raise KeyError(f"unknown process role {role!r}")
    return [p.upper() for p in [*procs.get("base", []), *procs[role]]]


def is_allowed(name: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive match of a variable name against allow-list patterns (``*`` wildcards)."""
    upper = name.upper()
    return any(fnmatch.fnmatchcase(upper, p.upper()) for p in patterns)


def build_child_env(
    role: str,
    source: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
    run_id: str | None = None,
    out_dir: str | None = None,
    network: str | None = None,
    processes: Mapping[str, list[str]] | None = None,
) -> dict[str, str]:
    """The exact environment of a ``role`` child: allow-listed variables of ``source`` only.

    ``overrides`` (for example the next signer key in S18) must themselves be allowed for the role;
    a disallowed override raises ``ValueError``. The LLM fallback (``OPENAI_API_KEY`` ->
    ``LLM_API_KEY``) is applied to the source before filtering.
    """
    patterns = allowed_patterns(role, processes)
    src = dict(os.environ if source is None else source)
    config.apply_llm_env_defaults(src)
    for name, value in (overrides or {}).items():
        if not is_allowed(name, patterns):
            raise ValueError(f"{name} is not allowed for process {role!r}")
        src[name] = value
    env = {k: v for k, v in src.items() if is_allowed(k, patterns)}
    # Windows environment names are case-insensitive: keep one spelling per name.
    if IS_WINDOWS:
        seen: dict[str, str] = {}
        for k in list(env):
            if k.upper() in seen:
                del env[k]
            else:
                seen[k.upper()] = k
    if role == "agent":
        leaked = [k for k in env if k.upper().endswith(("_PRIVATE_KEY", "_MNEMONIC"))]
        if leaked:
            raise ValueError(f"the agent process must not receive key variables: {', '.join(sorted(leaked))}")
    env["PYTHONUTF8"] = "1"
    env["ACB_PROCESS_ROLE"] = role
    env["ACB_RUN_ID"] = run_id or src.get("ACB_RUN_ID") or config.run_id(src)
    env["NETWORK"] = network or src.get("NETWORK") or "anvil"
    resolved_out = out_dir or src.get("ACB_OUT_DIR") or str(config.out_dir(environ={**src, **env}, create=False))
    env["ACB_OUT_DIR"] = resolved_out
    return env


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------


@dataclass
class Child:
    role: str
    pid: int
    argv: list[str]
    port: int | None
    log: str
    started_at: float
    proc: subprocess.Popen | None = field(default=None, repr=False)
    start_token: str | None = None  # process_start_token(pid) right after the start


def service_argv(role: str) -> list[str]:
    """``[sys.executable, "-m", "acb", <role>, "serve"]``."""
    return [sys.executable, "-m", "acb", role, "serve"]


def spawn(
    argv: list[str], env: Mapping[str, str], *, log_path: str | os.PathLike[str] | None = None, cwd=None
) -> subprocess.Popen:
    """Start ``argv`` with exactly ``env``, in its own process group, output to ``log_path``."""
    kwargs: dict = {"env": dict(env), "cwd": str(cwd or config.REPO_ROOT), "stdin": subprocess.DEVNULL}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    if log_path is None:
        return subprocess.Popen(argv, **kwargs)
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as fh:  # the child keeps its own handle
        return subprocess.Popen(argv, **kwargs, stdout=fh, stderr=subprocess.STDOUT)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes

        synchronize, query = 0x00100000, 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize | query, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_start_token(pid: int) -> str | None:
    """An opaque token for the process that has ``pid`` now (its creation time), or ``None``.

    PIDs are reused, so ``acb down`` compares this token with the one recorded by ``acb up`` before it
    signals anything: a different token means another process now has that PID.
    """
    if pid <= 0:
        return None
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            created = times[0]
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    stat = Path(f"/proc/{pid}/stat")
    if stat.is_file():
        try:
            text = stat.read_text(encoding="ascii", errors="replace")
            return text[text.rindex(")") + 2 :].split()[19]  # field 22: starttime
        except (OSError, ValueError, IndexError):
            return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False)
    except OSError:
        return None
    return out.stdout.strip() or None


def _same_process(pid: int, token: str | None) -> bool:
    """True when ``pid`` is alive and is the process recorded with ``token``."""
    return token is not None and _pid_alive(pid) and process_start_token(pid) == token


def _wait_exit(pid: int, proc: subprocess.Popen | None, timeout: float) -> bool:
    if proc is not None:
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def stop_pid(pid: int, proc: subprocess.Popen | None = None, *, grace_s: float = STOP_GRACE_S) -> str:
    """Stop one child: ``CTRL_BREAK_EVENT`` (Windows) / ``SIGTERM``, wait ``grace_s``, then terminate.

    Returns ``"stopped"``, ``"terminated"`` or ``"not running"``.
    """
    if proc is not None and proc.poll() is not None:
        return "not running"
    if proc is None and not _pid_alive(pid):
        return "not running"
    try:
        if IS_WINDOWS:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    if _wait_exit(pid, proc, grace_s):
        return "stopped"
    if proc is not None:
        proc.kill()
        proc.wait(timeout=10)
    elif IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    return "terminated"


def wait_for_port(port: int, timeout: float = 20.0, host: str = HOST) -> bool:
    """True once ``host:port`` accepts TCP connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


class Launcher:
    """Programmatic launcher (used by ``acb up`` and the scenario runner).

    ``launcher.start("signer", overrides={"AGENT_SIGNER_PRIVATE_KEY": ...})`` starts one service with
    its filtered environment; ``restart`` and ``stop`` act on one role; ``stop_all`` on every child.
    """

    def __init__(
        self,
        network: str,
        *,
        run_id: str | None = None,
        out_dir: str | os.PathLike[str] | None = None,
        source_env: Mapping[str, str] | None = None,
        argv_for=service_argv,
    ):
        self.network = network
        self.source_env = dict(os.environ if source_env is None else source_env)
        self.run_id = run_id or config.run_id(self.source_env)
        base_out = out_dir or self.source_env.get("ACB_OUT_DIR")
        if not base_out:
            base_out = str(config.REPO_ROOT / "runs" / self.run_id / "out" / network)
        self.out_dir = Path(base_out)
        self.argv_for = argv_for
        self.children: dict[str, Child] = {}

    def env_for(self, role: str, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
        return build_child_env(
            role,
            self.source_env,
            overrides=overrides,
            run_id=self.run_id,
            out_dir=str(self.out_dir),
            network=self.network,
        )

    def port_of(self, role: str) -> int | None:
        var = ROLE_PORT_VAR.get(role)
        if var is None:
            return None
        value = self.source_env.get(var)
        return int(value) if value else DEFAULT_PORTS.get(role)

    def start(self, role: str, overrides: Mapping[str, str] | None = None) -> Child:
        if role in self.children and self.children[role].proc and self.children[role].proc.poll() is None:
            raise RuntimeError(f"{role} is already running")
        env = self.env_for(role, overrides)
        argv = self.argv_for(role)
        log = self.out_dir / f"{role}.out.txt"
        proc = spawn(argv, env, log_path=log)
        child = Child(
            role=role,
            pid=proc.pid,
            argv=argv,
            port=self.port_of(role),
            log=str(log),
            started_at=time.time(),
            proc=proc,
            start_token=process_start_token(proc.pid),
        )
        self.children[role] = child
        return child

    def stop(self, role: str) -> str:
        child = self.children.pop(role, None)
        if child is None:
            return "not running"
        return stop_pid(child.pid, child.proc)

    def restart(self, role: str, overrides: Mapping[str, str] | None = None) -> Child:
        self.stop(role)
        return self.start(role, overrides)

    def stop_all(self) -> dict[str, str]:
        return {role: self.stop(role) for role in reversed(list(self.children))}

    def state(self) -> dict:
        return {
            "network": self.network,
            "runId": self.run_id,
            "outDir": str(self.out_dir),
            "children": [
                {
                    "role": c.role,
                    "pid": c.pid,
                    "port": c.port,
                    "log": c.log,
                    "startedAt": int(c.started_at),
                    "startToken": c.start_token,
                    "argv": c.argv[2:],
                }
                for c in self.children.values()
            ],
        }

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.stop_all()


def state_file(network: str) -> Path:
    return config.data_dir(network) / "launcher.json"


# ---------------------------------------------------------------------------
# CLI: acb up | acb down
# ---------------------------------------------------------------------------


def _cmd_up(args: argparse.Namespace) -> int:
    from acb.canonical import atomic_write_bytes

    config.load_env_file()
    network = args.network or config.network_name()
    roles = [r.strip() for r in args.only.split(",")] if args.only else list(SERVICE_ROLES)
    unknown = [r for r in roles if r not in SERVICE_ROLES]
    if unknown:
        sys.stderr.write(f"acb up: unknown services {unknown}; choose from {', '.join(SERVICE_ROLES)}\n")
        return 2
    path = state_file(network)
    if path.is_file():
        previous = json.loads(path.read_text(encoding="utf-8"))
        alive = [c["role"] for c in previous.get("children", []) if _same_process(int(c["pid"]), c.get("startToken"))]
        if alive:
            sys.stderr.write(
                f"acb up: already running ({', '.join(alive)}); run `acb down --network {network}` first\n"
            )
            return 2
    launcher = Launcher(network, run_id=args.run_id)
    failed = False
    for role in roles:
        child = launcher.start(role)
        ok = child.port is None or wait_for_port(child.port, timeout=args.timeout)
        if child.proc is not None and child.proc.poll() is not None:
            ok = False
        print(f"  {role:<9} pid {child.pid:<6} {HOST}:{child.port}  {'up' if ok else 'NOT UP'}  log {child.log}")
        failed |= not ok
    atomic_write_bytes(path, (json.dumps(launcher.state(), indent=2) + "\n").encode("utf-8"))
    if failed:
        sys.stderr.write("acb up: some services did not come up; see their logs\n")
    if args.wait:
        print("running; press Ctrl+C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        for role, result in launcher.stop_all().items():
            print(f"  {role:<9} {result}")
        path.unlink(missing_ok=True)
    return 1 if failed else 0


def _cmd_down(args: argparse.Namespace) -> int:
    network = args.network or config.network_name()
    path = state_file(network)
    if not path.is_file():
        print(f"nothing to stop for {network}")
        return 0
    state = json.loads(path.read_text(encoding="utf-8"))
    for child in reversed(state.get("children", [])):
        pid = int(child["pid"])
        if not _pid_alive(pid):
            result = "not running"
        elif not _same_process(pid, child.get("startToken")):
            # the PID now belongs to another process (crash or reboot, then reuse): never signal it
            result = "skipped: pid now belongs to another process"
        else:
            result = stop_pid(pid, None, grace_s=args.grace)
        print(f"  {child['role']:<9} pid {child['pid']:<6} {result}")
    path.unlink(missing_ok=True)
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Mount ``acb up`` and ``acb down``."""
    p = subparsers.add_parser("up", help="start the services P1-P6 with scrubbed environments")
    p.add_argument("--network", default=None, help="anvil | base_sepolia (default: NETWORK or anvil)")
    p.add_argument("--only", default=None, help=f"comma-separated subset of {','.join(SERVICE_ROLES)}")
    p.add_argument("--run-id", default=None)
    p.add_argument("--timeout", type=float, default=20.0, help="seconds to wait for each port")
    p.add_argument("--wait", action="store_true", help="stay in the foreground and stop everything on Ctrl+C")
    p.set_defaults(func=_cmd_up)

    p = subparsers.add_parser("down", help="stop the services started by `acb up`")
    p.add_argument("--network", default=None)
    p.add_argument("--grace", type=float, default=STOP_GRACE_S, help="seconds between CTRL_BREAK and terminate")
    p.set_defaults(func=_cmd_down)


__all__ = [
    "SERVICE_ROLES",
    "Child",
    "Launcher",
    "allowed_patterns",
    "build_child_env",
    "is_allowed",
    "process_start_token",
    "service_argv",
    "spawn",
    "state_file",
    "stop_pid",
    "wait_for_port",
]
