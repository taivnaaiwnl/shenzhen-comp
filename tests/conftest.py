"""Shared pytest fixtures (spec 12.2).

- ``anvil`` (session): a fresh Anvil on a free port (``--accounts 30 --chain-id 31337``, automine),
  yields ``(rpc_url, w3)`` and stops it at the end. Chain tests are skipped when Anvil is missing.
- ``anvil_profile`` (session): the ``anvil`` :class:`~acb.types.NetworkProfile` pointing at that node.
- ``deployed`` (session): ``acb.chain.deploy.deploy_all(anvil_profile, KeyRing.anvil())`` (imported
  lazily; skipped until WP3 lands), yields ``Deployments``. Deployment files go to a temp folder
  (``ACB_DEPLOYMENTS_DIR``), never to the repository.
- ``fresh_deployed`` (function): ``evm_snapshot`` / ``evm_revert`` around a test.
- ``warp`` (function): ``warp(seconds)`` = ``evm_increaseTime`` + ``evm_mine``.
- ``out_dir`` (function): sets ``ACB_OUT_DIR`` to the test's ``tmp_path``.

Tests marked ``llm`` are skipped unless an LLM key is configured (``LLM_API_KEY``, or
``OPENAI_API_KEY`` in the environment or in ``.env``); the ``llm_env`` fixture exports only the
``LLM_*`` settings for such a test. The test process never loads the whole ``.env``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

ANVIL_START_TIMEOUT_S = 30.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def start_anvil(*extra_args: str) -> tuple[subprocess.Popen, str]:
    """Start Anvil on a free port; returns the process and its RPC URL. Skips if Anvil is missing."""
    from web3 import Web3

    from acb.config import foundry_tool

    exe = foundry_tool("anvil")
    if exe is None:
        pytest.skip("anvil not found: install Foundry or set ACB_FOUNDRY_BIN")
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    last_error = "unknown"
    for _ in range(3):  # a free port can be taken between probe and bind: retry
        port = _free_port()
        argv = [
            str(exe),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--accounts",
            "30",
            "--chain-id",
            "31337",
            "--silent",
            *extra_args,
        ]
        with tempfile.TemporaryFile() as err:  # anvil keeps its own handle
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, **flags)
            url = f"http://127.0.0.1:{port}"
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 5}))
            deadline = time.monotonic() + ANVIL_START_TIMEOUT_S
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    if w3.eth.chain_id == 31337:
                        time.sleep(0.3)
                        if proc.poll() is None:  # still ours: a node that lost the port race has exited
                            return proc, url
                        break
                except Exception:  # noqa: BLE001 - not listening yet
                    time.sleep(0.1)
            _stop(proc)
            err.seek(0)
            last_error = err.read().decode(errors="replace")[-500:] or "timeout"
    pytest.fail(f"anvil did not start: {last_error}")


@pytest.fixture(scope="session")
def anvil() -> Iterator[tuple[str, object]]:
    """``(rpc_url, w3)`` of a session-wide Anvil node (automine)."""
    from web3 import Web3

    proc, url = start_anvil()
    previous = os.environ.get("ANVIL_RPC_URL")
    os.environ["ANVIL_RPC_URL"] = url
    try:
        yield url, Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
    finally:
        if previous is None:
            os.environ.pop("ANVIL_RPC_URL", None)
        else:
            os.environ["ANVIL_RPC_URL"] = previous
        _stop(proc)


@pytest.fixture(scope="session")
def anvil_profile(anvil):
    """The ``anvil`` network profile pointing at the session's node."""
    from acb.types import NetworkProfile

    url, _ = anvil
    return NetworkProfile.load("anvil", {"ANVIL_RPC_URL": url})


@pytest.fixture(scope="session")
def deployed(anvil_profile, tmp_path_factory):
    """``Deployments`` of a full local deployment (WP3's ``deploy_all``); skipped until WP3 lands."""
    import inspect

    try:
        from acb.chain.deploy import deploy_all
    except ImportError as exc:
        pytest.skip(f"acb.chain.deploy not built yet (WP3): {exc}")
    from acb.keys import KeyRing

    deployments_dir = tmp_path_factory.mktemp("deployments")
    previous = os.environ.get("ACB_DEPLOYMENTS_DIR")
    os.environ["ACB_DEPLOYMENTS_DIR"] = str(deployments_dir)
    try:
        kwargs = {"fresh": True} if "fresh" in inspect.signature(deploy_all).parameters else {}
        result = deploy_all(anvil_profile, KeyRing.anvil(), **kwargs)
        yield result[0] if isinstance(result, tuple) else result
    finally:
        if previous is None:
            os.environ.pop("ACB_DEPLOYMENTS_DIR", None)
        else:
            os.environ["ACB_DEPLOYMENTS_DIR"] = previous


def _rpc(w3, method: str, params: list):
    response = w3.provider.make_request(method, params)
    if "error" in response:
        raise RuntimeError(f"{method} failed: {response['error']}")
    return response["result"]


@pytest.fixture
def fresh_deployed(anvil, deployed):
    """``deployed`` with the chain state reverted after the test (``evm_snapshot`` / ``evm_revert``).

    A ``TxSender`` created in the test must resync its nonce after the revert (WP3). The files in
    ``ACB_DEPLOYMENTS_DIR`` are restored too: a test that adds a vault or agent (``deploy_extra_vault``)
    must not leave an entry for a contract that the revert removed from the chain.
    """
    _, w3 = anvil
    folder = Path(os.environ["ACB_DEPLOYMENTS_DIR"])
    saved = {path: path.read_bytes() for path in folder.glob("*.json")}
    snapshot = _rpc(w3, "evm_snapshot", [])
    try:
        yield deployed
    finally:
        _rpc(w3, "evm_revert", [snapshot])
        for path in folder.glob("*.json"):
            if path not in saved:
                path.unlink()
        for path, content in saved.items():
            if not path.is_file() or path.read_bytes() != content:
                path.write_bytes(content)


@pytest.fixture
def warp(anvil) -> Callable[[int], int]:
    """``warp(seconds)``: advance the chain clock and mine one block; returns the new block timestamp."""
    _, w3 = anvil

    def _warp(seconds: int) -> int:
        _rpc(w3, "evm_increaseTime", [int(seconds)])
        _rpc(w3, "evm_mine", [])
        return w3.eth.get_block("latest")["timestamp"]

    return _warp


@pytest.fixture
def out_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Runtime output of the test goes to ``tmp_path`` (``ACB_OUT_DIR``)."""
    monkeypatch.setenv("ACB_OUT_DIR", str(tmp_path))
    return tmp_path


def _llm_settings():
    from acb.config import DEFAULT_ENV_FILE, llm_settings

    try:
        return llm_settings(env_file=DEFAULT_ENV_FILE)
    except (ValueError, TypeError):
        return None


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch):
    """Exports the LLM settings (only ``LLM_*``) for a test marked ``llm``; returns them."""
    settings = _llm_settings()
    if settings is None or not settings.has_key:
        pytest.skip("no LLM key configured")
    monkeypatch.setenv("LLM_API_KEY", settings.api_key)
    monkeypatch.setenv("LLM_BASE_URL", settings.base_url)
    monkeypatch.setenv("LLM_MODEL", settings.model)
    return settings


def pytest_collection_modifyitems(config, items):
    if not any(item.get_closest_marker("llm") for item in items):
        return
    settings = _llm_settings()
    if settings is not None and settings.has_key:
        return
    skip = pytest.mark.skip(reason="llm test: no LLM_API_KEY / OPENAI_API_KEY configured")
    for item in items:
        if item.get_closest_marker("llm"):
            item.add_marker(skip)
