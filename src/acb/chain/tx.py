"""Sending transactions: nonces, fees, gas, receipts and revert decoding (spec 7.4).

One :class:`TxSender` per key per process. It is the only code that signs and broadcasts transactions;
the signer (P3), bureau (P4), autonomy operator (P5), simulator (P6), deploy and the scenario runner
all use it.

- :class:`NonceManager`: ``next = get_transaction_count(addr, "pending")`` on start, after any error,
  after a receipt timeout and after any ``evm_revert``; increments locally after each successful
  broadcast. On "nonce too low" / "replacement transaction underpriced" it resyncs and retries once.
  On Anvil it also compares with the node before each send, so an ``evm_revert`` (tests) is noticed
  without a timeout. :func:`resync_all` resyncs every manager in the process (CR-2).
- Fees (EIP-1559): ``priority = eth_maxPriorityFeePerGas`` (fallback 1 gwei on Anvil, 0.001 gwei
  elsewhere); ``maxFee = 2 * latest.baseFeePerGas + priority``.
- Gas: ``int(estimate * 1.25) + 10_000``; transactions **expected to revert** (stolen-key
  demonstrations) skip simulation and estimation and use 500,000.
- A simulation or estimation that reverts raises :class:`TxSimulationError` (nothing is sent). A mined
  revert is returned as a :class:`~acb.types.TxResult` with ``status == 0`` and ``revert_error``
  decoded by replaying the call with ``eth_call`` at ``receipt.blockNumber - 1``.
"""

from __future__ import annotations

import os
import threading
import time
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.exceptions import TimeExhausted

from acb.chain.abi import DecodedError, decode_exception, decode_log
from acb.types import TxResult

ANVIL_CHAIN_ID = 31337
GWEI = 10**9
PRIORITY_FALLBACK_ANVIL = 1 * GWEI
PRIORITY_FALLBACK_PUBLIC = GWEI // 1000  # 0.001 gwei
GAS_MULTIPLIER_NUM = 125
GAS_MULTIPLIER_DEN = 100
GAS_EXTRA = 10_000
EXPECTED_REVERT_GAS = 500_000
RECEIPT_TIMEOUT_ANVIL_S = 30.0
RECEIPT_TIMEOUT_PUBLIC_S = 180.0

_RETRYABLE_SEND = ("nonce too low", "replacement transaction underpriced", "nonce has already been used")
_ALREADY_KNOWN = ("already known", "known transaction", "already imported")

# Highest block of any receipt seen, per chain ("read your own writes" on a public RPC). A public RPC is
# served by several nodes that can lag each other by a block or two, so a read sent right after a receipt
# can reach a node that has not seen the receipt's block yet (and, for example, sees no code at a contract
# deployed one block earlier). Every receipt raises this mark; on public networks it is also shared with
# the run's other processes (signer, bureau, simulator, owner console) through a small file under
# data/chain-<id>/, so reads in any process target a block at least as new as the newest local write
# (see acb.chain.client.ReadYourWrites). Anvil mines instantly and never uses the file.
_HIGHEST_RECEIPT_BLOCK: dict[int, int] = {}
_HWM_DIR = Path(__file__).resolve().parents[3] / "data"


def _hwm_file(chain_id: int) -> Path:
    return _HWM_DIR / f"chain-{chain_id}" / "receipt_hwm"


def _read_hwm_file(chain_id: int) -> int:
    try:
        return int(_hwm_file(chain_id).read_text(encoding="ascii").strip() or -1)
    except (OSError, ValueError):
        return -1


def note_receipt_block(chain_id: int, block: int) -> None:
    """Raise the per-chain high-water mark of mined blocks seen in a receipt (shared on public networks)."""
    if block > _HIGHEST_RECEIPT_BLOCK.get(chain_id, -1):
        _HIGHEST_RECEIPT_BLOCK[chain_id] = block
    if chain_id == ANVIL_CHAIN_ID:
        return
    try:
        if block > _read_hwm_file(chain_id):
            path = _hwm_file(chain_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"receipt_hwm.{os.getpid()}.tmp")
            tmp.write_text(str(block), encoding="ascii")
            os.replace(tmp, path)
    except OSError:
        pass  # the in-process mark still covers this process's own writes


def highest_receipt_block(chain_id: int) -> int:
    """The highest receipt block seen on ``chain_id`` by this process or (public networks) any local one."""
    mine = _HIGHEST_RECEIPT_BLOCK.get(chain_id, -1)
    if chain_id == ANVIL_CHAIN_ID:
        return mine
    return max(mine, _read_hwm_file(chain_id))


class TxError(RuntimeError):
    """Base class of transaction errors."""


class TxSimulationError(TxError):
    """The call reverted in ``eth_call`` or ``eth_estimateGas``; nothing was broadcast."""

    def __init__(self, function: str, decoded: DecodedError):
        super().__init__(f"{function} would revert: {decoded.text}")
        self.function = function
        self.decoded = decoded
        self.revert_error = decoded.text


class TxSendError(TxError):
    """The node refused the raw transaction."""


class TxTimeout(TxError):
    """No receipt within the timeout. The nonce manager has resynced; the transaction may still mine."""

    def __init__(self, tx_hash: str, function: str, timeout: float):
        super().__init__(f"no receipt for {function} {tx_hash} after {timeout:.0f} s")
        self.tx_hash = tx_hash
        self.function = function


def to_hex(value: Any) -> str:
    """``Web3.to_hex`` (never ``HexBytes.hex()``, which has no ``0x`` in hexbytes 2.x)."""
    return Web3.to_hex(value)


# ---------------------------------------------------------------------------
# nonces
# ---------------------------------------------------------------------------

_MANAGERS: weakref.WeakValueDictionary[tuple[str, str], NonceManager] = weakref.WeakValueDictionary()
_MANAGERS_LOCK = threading.Lock()


def _endpoint_key(w3: Any) -> str:
    provider = getattr(w3, "provider", None)
    uri = getattr(provider, "endpoint_uri", None)
    return str(uri) if uri else f"provider-{id(provider)}"


class NonceManager:
    """Local nonce counter for one address on one RPC endpoint (thread-safe).

    Use :meth:`for_address` to share one manager per (endpoint, address) inside a process.
    """

    def __init__(self, w3: Any, address: str, *, verify_each: bool | None = None):
        self.w3 = w3
        self.address = Web3.to_checksum_address(address)
        self.lock = threading.RLock()
        self._next: int | None = None
        self._verify_each = verify_each

    @classmethod
    def for_address(cls, w3: Any, address: str) -> NonceManager:
        """The process-wide manager of ``address`` on ``w3``'s endpoint (created on first use)."""
        key = (_endpoint_key(w3), address.lower())
        with _MANAGERS_LOCK:
            manager = _MANAGERS.get(key)
            if manager is None:
                manager = cls(w3, address)
                _MANAGERS[key] = manager
            return manager

    @property
    def verify_each(self) -> bool:
        if self._verify_each is None:
            try:
                self._verify_each = int(self.w3.eth.chain_id) == ANVIL_CHAIN_ID
            except Exception:  # noqa: BLE001 - unknown node: be conservative later
                return False
        return self._verify_each

    def chain_next(self) -> int:
        """``eth_getTransactionCount(address, "pending")``."""
        return int(self.w3.eth.get_transaction_count(self.address, "pending"))

    def resync(self) -> int:
        """Reload the next nonce from the node and return it."""
        with self.lock:
            self._next = self.chain_next()
            return self._next

    def invalidate(self) -> None:
        """Forget the local counter; the next :meth:`reserve` resyncs."""
        with self.lock:
            self._next = None

    def peek(self) -> int | None:
        return self._next

    def reserve(self) -> int:
        """The nonce for the next broadcast (resyncs when unknown, and on Anvil when the node disagrees)."""
        with self.lock:
            if self._next is None:
                return self.resync()
            if self.verify_each:
                chain = self.chain_next()
                if chain != self._next:
                    self._next = chain
            return self._next

    def advance(self, used: int) -> None:
        """Record a successful broadcast with nonce ``used``."""
        with self.lock:
            self._next = used + 1


PROBE_TIMEOUT_S = 2.0


def _endpoint_alive(w3: Any) -> bool:
    """One quick ``eth_chainId`` to an HTTP endpoint, without the provider's retries and backoff."""
    uri = getattr(getattr(w3, "provider", None), "endpoint_uri", None)
    if not uri or not str(uri).startswith("http"):
        return True  # not an HTTP provider: resync itself decides
    try:
        requests.post(
            str(uri), json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=PROBE_TIMEOUT_S
        )
        return True
    except requests.RequestException:
        return False


def resync_all() -> int:
    """Resync every :class:`NonceManager` of this process (call after ``evm_revert``). Returns the count resynced.

    Each endpoint is probed once first: the managers of a node that has gone away (an Anvil chain a test
    stopped) are forgotten, so a long-lived process never waits on the RPC retries of a dead port again.
    """
    with _MANAGERS_LOCK:
        items = list(_MANAGERS.items())
    alive: dict[str, bool] = {}
    count = 0
    for key, manager in items:
        endpoint = key[0]
        if endpoint not in alive:
            alive[endpoint] = _endpoint_alive(manager.w3)
        if not alive[endpoint]:
            manager.invalidate()
            with _MANAGERS_LOCK:
                if _MANAGERS.get(key) is manager:
                    del _MANAGERS[key]
            continue
        try:
            manager.resync()
            count += 1
        except Exception:  # noqa: BLE001 - a node that went away: resync lazily later
            manager.invalidate()
    return count


# ---------------------------------------------------------------------------
# fees and gas
# ---------------------------------------------------------------------------


def fee_params(w3: Any, chain_id: int | None = None) -> dict[str, int]:
    """EIP-1559 fee fields (``maxFeePerGas``, ``maxPriorityFeePerGas``), or ``gasPrice`` on a legacy chain."""
    chain_id = int(w3.eth.chain_id) if chain_id is None else chain_id
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas")
    try:
        priority = int(w3.eth.max_priority_fee)
    except Exception:  # noqa: BLE001 - the method is optional on some nodes
        priority = PRIORITY_FALLBACK_ANVIL if chain_id == ANVIL_CHAIN_ID else PRIORITY_FALLBACK_PUBLIC
    if base_fee is None:
        return {"gasPrice": int(w3.eth.gas_price)}
    return {"maxFeePerGas": 2 * int(base_fee) + priority, "maxPriorityFeePerGas": priority}


def padded_gas(estimate: int) -> int:
    """``int(estimate * 1.25) + 10_000`` (integer arithmetic)."""
    return estimate * GAS_MULTIPLIER_NUM // GAS_MULTIPLIER_DEN + GAS_EXTRA


def _message(exc: BaseException) -> str:
    parts = [str(a) for a in getattr(exc, "args", ())]
    response = getattr(exc, "rpc_response", None)
    if isinstance(response, dict):
        parts.append(str(response.get("error", "")))
    return " ".join(parts).lower()


# ---------------------------------------------------------------------------
# sender
# ---------------------------------------------------------------------------


class TxSender:
    """Signs, simulates, sends and waits for transactions from one account.

    ``on_result`` (optional) is called with every :class:`TxResult` (the scenario runner records them).
    """

    def __init__(
        self,
        w3: Any,
        account: LocalAccount,
        *,
        receipt_timeout: float | None = None,
        on_result: Callable[[TxResult], None] | None = None,
    ):
        self.w3 = w3
        self.account = account
        self.checksum_address = account.address
        self.address = account.address.lower()
        self.nonces = NonceManager.for_address(w3, account.address)
        self._chain_id: int | None = None
        self._receipt_timeout = receipt_timeout
        self.on_result = on_result

    def __repr__(self) -> str:
        return f"TxSender({self.checksum_address})"

    @property
    def chain_id(self) -> int:
        if self._chain_id is None:
            self._chain_id = int(self.w3.eth.chain_id)
        return self._chain_id

    @property
    def receipt_timeout(self) -> float:
        if self._receipt_timeout is not None:
            return self._receipt_timeout
        return RECEIPT_TIMEOUT_ANVIL_S if self.chain_id == ANVIL_CHAIN_ID else RECEIPT_TIMEOUT_PUBLIC_S

    def resync(self) -> int:
        """Resync this account's nonce (after ``evm_revert`` or any doubt)."""
        return self.nonces.resync()

    # ---- simulation ----------------------------------------------------------------------------

    def _call_params(self, to: str | None, data: str, value: int) -> dict[str, Any]:
        params: dict[str, Any] = {"from": self.checksum_address, "data": data, "value": value}
        if to is not None:
            params["to"] = Web3.to_checksum_address(to)
        return params

    def simulate(self, fn: Any, *, value: int = 0, block: int | str = "latest") -> DecodedError | None:
        """``eth_call`` of a contract function from this account: None if it succeeds, else the decoded revert."""
        try:
            self.w3.eth.call(self._call_params(fn.address, fn._encode_transaction_data(), value), block)
        except Exception as exc:  # noqa: BLE001 - every revert form is decoded below
            return decode_exception(exc)
        return None

    # ---- sending -------------------------------------------------------------------------------

    def send(
        self,
        fn: Any,
        *,
        expect_revert: bool = False,
        simulate: bool = True,
        gas: int | None = None,
        value: int = 0,
    ) -> TxResult:
        """Send a contract call (a web3 ``ContractFunction``) and wait for its receipt.

        ``expect_revert=True`` (stolen-key demonstrations, 10.2) disables simulation and estimation and
        uses 500,000 gas; ``expected_status`` of the result is then 0.
        """
        return self._send(
            to=fn.address,
            data=fn._encode_transaction_data(),
            function=fn.fn_name,
            expect_revert=expect_revert,
            simulate=simulate,
            gas=gas,
            value=value,
        )

    def deploy(self, contract_name: str, *args: Any, gas: int | None = None) -> TxResult:
        """Deploy ``abi/<contract_name>.json`` with constructor ``args``.

        The result's ``to`` is the created contract address and ``function`` is ``"deploy"``.
        """
        from acb.chain import abi as abi_mod

        factory = self.w3.eth.contract(abi=abi_mod.abi_of(contract_name), bytecode=abi_mod.bytecode_of(contract_name))
        data = factory.constructor(*args).data_in_transaction
        return self._send(to=None, data=data, function="deploy", gas=gas)

    def send_eth(self, to: str, value: int) -> TxResult:
        """Plain ETH transfer (``function`` is ``"ethTransfer"``)."""
        if value <= 0:
            raise ValueError("value must be positive")
        return self._send(to=to, data="0x", function="ethTransfer", value=value, simulate=False, gas=21_000)

    def _send(
        self,
        *,
        to: str | None,
        data: str,
        function: str,
        expect_revert: bool = False,
        simulate: bool = True,
        gas: int | None = None,
        value: int = 0,
    ) -> TxResult:
        if expect_revert:
            simulate = False
            gas = gas or EXPECTED_REVERT_GAS
        call = self._call_params(to, data, value)
        if simulate:
            try:
                self.w3.eth.call(call, "latest")
            except Exception as exc:  # noqa: BLE001
                raise TxSimulationError(function, decode_exception(exc)) from None
        if gas is None:
            try:
                gas = padded_gas(int(self.w3.eth.estimate_gas(call)))
            except Exception as exc:  # noqa: BLE001
                raise TxSimulationError(function, decode_exception(exc)) from None

        tx_hash, nonce = self._broadcast(to, data, value, gas)
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=self.receipt_timeout, poll_latency=0.1 if self.chain_id == ANVIL_CHAIN_ID else 1.0
            )
        except TimeExhausted:
            self.nonces.invalidate()
            raise TxTimeout(tx_hash, function, self.receipt_timeout) from None
        except Exception:
            self.nonces.invalidate()  # "after any error": the next send resyncs
            raise

        status = int(receipt["status"])
        block = int(receipt["blockNumber"])
        note_receipt_block(self.chain_id, block)
        revert_error: str | None = None
        if status == 0:
            revert_error = self._replay_revert(call, gas, block, int(receipt["gasUsed"]))
        logs = []
        codec = self.w3.codec
        for log in receipt.get("logs", []):
            decoded = decode_log(log, codec)
            if decoded is not None:
                logs.append(decoded)
        target = receipt.get("contractAddress") if to is None else to
        result = TxResult(
            hash=tx_hash,
            status=status,
            block=block,
            gas_used=int(receipt["gasUsed"]),
            function=function,
            from_address=self.address,
            to=str(target or "0x" + "00" * 20).lower(),
            nonce=nonce,
            chain_id=self.chain_id,
            expected_status=0 if expect_revert else 1,
            revert_error=revert_error,
            logs=tuple(logs),
        )
        if self.on_result is not None:
            self.on_result(result)
        return result

    def _broadcast(self, to: str | None, data: str, value: int, gas: int) -> tuple[str, int]:
        """Sign and send with the next nonce; resync and retry once on a nonce error."""
        for attempt in (0, 1):
            with self.nonces.lock:
                nonce = self.nonces.reserve()
                tx: dict[str, Any] = {
                    "chainId": self.chain_id,
                    "nonce": nonce,
                    "gas": int(gas),
                    "value": int(value),
                    "data": data,
                    **fee_params(self.w3, self.chain_id),
                }
                if to is not None:
                    tx["to"] = Web3.to_checksum_address(to)
                signed = self.account.sign_transaction(tx)
                try:
                    sent = self._send_raw(signed.raw_transaction)
                except Exception as exc:  # noqa: BLE001
                    text = _message(exc)
                    if any(w in text for w in _ALREADY_KNOWN):
                        self.nonces.advance(nonce)
                        return to_hex(signed.hash), nonce
                    self.nonces.invalidate()
                    if attempt == 0 and any(w in text for w in _RETRYABLE_SEND):
                        self.nonces.resync()
                        continue
                    raise TxSendError(f"node refused the transaction: {exc}") from None
                self.nonces.advance(nonce)
                return to_hex(sent), nonce
        raise TxSendError("unreachable")  # pragma: no cover

    SEND_RETRIES = 3

    def _send_raw(self, raw: bytes) -> Any:
        """``eth_sendRawTransaction`` that survives a dropped connection.

        A public RPC can close an idle keep-alive connection, and the next request on it fails with a
        connection error before the node sees it. Sending the same signed transaction again is always
        safe: it has one nonce and one hash, so it can be mined at most once (a node that already has it
        answers "already known", which the caller treats as sent)."""
        delay = 0.5
        for attempt in range(self.SEND_RETRIES + 1):
            try:
                return self.w3.eth.send_raw_transaction(raw)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == self.SEND_RETRIES:
                    raise
                time.sleep(delay)
                delay *= 2
        raise TxSendError("unreachable")  # pragma: no cover

    def _replay_revert(self, call: dict[str, Any], gas: int, block: int, gas_used: int) -> str:
        """Decode why a mined transaction reverted by replaying it at ``block - 1``."""
        try:
            self.w3.eth.call({**call, "gas": gas}, max(block - 1, 0))
        except Exception as exc:  # noqa: BLE001
            return decode_exception(exc).text
        if gas_used >= gas:
            return "OutOfGas()"
        return "Reverted()"


__all__ = [
    "EXPECTED_REVERT_GAS",
    "NonceManager",
    "TxError",
    "TxResult",
    "TxSendError",
    "TxSender",
    "TxSimulationError",
    "TxTimeout",
    "fee_params",
    "padded_gas",
    "resync_all",
    "to_hex",
]
