"""Local Anvil node (process P0, spec 2.1, 10.1): start, wait, stop.

Used by ``acb chain up|down`` and by tools that need a private chain (the scenario runner starts its
own with ``block_time=1``). The node listens on the literal ``127.0.0.1``; :func:`free_port` picks an
unused port so parallel test runs never collide.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Self

from acb.config import foundry_tool

HOST = "127.0.0.1"
START_TIMEOUT_S = 30.0


def free_port() -> int:
    """A TCP port that is free on 127.0.0.1 right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def port_in_use(port: int) -> bool:
    """True if something accepts connections on 127.0.0.1:``port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) == 0


def anvil_argv(
    port: int, *, block_time: int | None = None, accounts: int = 30, chain_id: int = 31337, silent: bool = True
) -> list[str]:
    """Command line of an Anvil node as the spec runs it (``--accounts 30 --chain-id 31337``)."""
    exe = foundry_tool("anvil")
    if exe is None:
        raise FileNotFoundError("anvil not found: install Foundry or set ACB_FOUNDRY_BIN")
    argv = [str(exe), "--host", HOST, "--port", str(port), "--accounts", str(accounts), "--chain-id", str(chain_id)]
    if block_time:
        argv += ["--block-time", str(int(block_time))]
    if silent:
        argv.append("--silent")
    return argv


@dataclass
class AnvilProcess:
    """A running Anvil node started by :func:`start_anvil`."""

    proc: subprocess.Popen
    port: int

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    @property
    def pid(self) -> int:
        return int(self.proc.pid)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, timeout: float = 10.0) -> None:
        """Terminate the node and wait for it."""
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=timeout)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _chain_id(url: str) -> int | None:
    from web3 import Web3

    try:
        return int(Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 2})).eth.chain_id)
    except Exception:  # noqa: BLE001 - not listening yet
        return None


def start_anvil(
    *,
    port: int | None = None,
    block_time: int | None = None,
    accounts: int = 30,
    chain_id: int = 31337,
    detach: bool = False,
    timeout: float = START_TIMEOUT_S,
) -> AnvilProcess:
    """Start Anvil and wait until it answers ``eth_chainId``.

    ``port=None`` picks a free port (retried if another process takes it first). ``detach`` keeps the
    node running after this Python process exits (``acb chain up``).
    """
    flags: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    )
    attempts = 3 if port is None else 1
    last_error = "timeout"
    for _ in range(attempts):
        chosen = port if port is not None else free_port()
        if port is not None and port_in_use(chosen):
            raise RuntimeError(f"port {chosen} is already in use on {HOST}")
        argv = anvil_argv(chosen, block_time=block_time, accounts=accounts, chain_id=chain_id)
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL if detach else err,
                **flags,
            )
            node = AnvilProcess(proc, chosen)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and proc.poll() is None:
                if _chain_id(node.url) == chain_id:
                    time.sleep(0.2)
                    if proc.poll() is None:
                        return node
                    break
                time.sleep(0.1)
            node.stop()
            if not detach:
                err.seek(0)
                last_error = err.read().decode(errors="replace")[-500:] or "timeout"
    raise RuntimeError(f"anvil did not start: {last_error}")


__all__ = ["HOST", "AnvilProcess", "anvil_argv", "free_port", "port_in_use", "start_anvil"]
