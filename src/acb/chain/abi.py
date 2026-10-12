"""Contract artefacts from ``abi/*.json`` and revert/log decoding (spec 3.7, 7.4, 15.3).

Each artefact is ``{"contractName", "abi", "bytecode", "deployedBytecode", "codehash"}`` (WP2's
``scripts/export_abi.py``). This module:

- loads artefacts by contract name (:func:`load`, with the aliases of :data:`ALIASES`),
- builds the **error selector table** from every ``error`` entry of every ABI, plus the standard
  ``Error(string)`` and ``Panic(uint256)``, and decodes revert data with it (web3 7 raises
  ``ContractCustomError`` with only the raw data),
- builds an event table (topic0 -> ABI) used to decode receipt logs of any of our contracts,
- computes ``abi_sha`` for ``deployments/<network>.json`` (13.5).

Hex output always goes through ``Web3.to_hex`` (hexbytes 2.x ``.hex()`` has no ``0x``, spec 0.4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from eth_abi import decode as abi_decode
from eth_utils import keccak
from web3 import Web3

from acb.canonical import chash
from acb.config import REPO_ROOT

ABI_DIR = REPO_ROOT / "abi"

ALIASES: dict[str, str] = {
    "IdentityRegistry": "IdentityRegistryLite",
    "ReputationRegistry": "ReputationRegistryLite",
    "USDC": "MockUSDC",
    "ERC20": "MockUSDC",
    "ACB": "AgentCreditBureau",
}
"""Logical names -> artefact files. The Lite registries share the official signatures (3.4), and the
MockUSDC ABI is a plain ERC-20 subset, so one ABI serves both networks."""

ERROR_STRING_SELECTOR = "0x08c379a0"  # Error(string)
PANIC_SELECTOR = "0x4e487b71"  # Panic(uint256)

PANIC_CODES: dict[int, str] = {
    0x00: "generic",
    0x01: "assert",
    0x11: "arithmetic overflow or underflow",
    0x12: "division by zero",
    0x21: "invalid enum value",
    0x22: "bad storage byte array",
    0x31: "pop on empty array",
    0x32: "array index out of bounds",
    0x41: "out of memory",
    0x51: "zero-initialised function pointer",
}


class AbiNotFound(FileNotFoundError):
    """The requested ``abi/<name>.json`` does not exist."""


def artefact_names() -> tuple[str, ...]:
    """Names of every contract artefact in ``abi/`` (files with an ``abi`` key)."""
    names = []
    for path in sorted(ABI_DIR.glob("*.json")):
        data = _read(path)
        if isinstance(data, dict) and "abi" in data:
            names.append(path.stem)
    return tuple(names)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load(contract_name: str) -> dict[str, Any]:
    """The artefact of ``contract_name`` (aliases allowed, see :data:`ALIASES`). Cached."""
    return _load_file(ALIASES.get(contract_name, contract_name))


@cache
def _load_file(name: str) -> dict[str, Any]:
    path = ABI_DIR / f"{name}.json"
    if not path.is_file():
        raise AbiNotFound(f"abi/{name}.json not found (WP2 exports it with scripts/export_abi.py)")
    data = _read(path)
    if not isinstance(data, dict) or "abi" not in data:
        raise AbiNotFound(f"abi/{name}.json is not a contract artefact")
    return data


def abi_of(contract_name: str) -> list[dict[str, Any]]:
    """The ABI list of a contract."""
    return load(contract_name)["abi"]


def bytecode_of(contract_name: str) -> str:
    """Creation bytecode (``0x`` hex)."""
    code = load(contract_name).get("bytecode") or ""
    if not code or code == "0x":
        raise AbiNotFound(f"abi/{contract_name}.json has no bytecode")
    return code


def codehash_of(contract_name: str) -> str:
    """keccak256 of the runtime bytecode as exported (the ``EXTCODEHASH`` of every instance)."""
    value = load(contract_name).get("codehash")
    if value:
        return str(value).lower()
    runtime = load(contract_name)["deployedBytecode"]
    return Web3.to_hex(keccak(hexstr=runtime))


def abi_sha() -> str:
    """``chash`` of ``{file name: parsed JSON}`` over every ``abi/*.json`` (spec 13.5 ``abi.sha``).

    Parsing first makes the value independent of line endings and formatting.
    """
    files = {path.name: _read(path) for path in sorted(ABI_DIR.glob("*.json"))}
    return chash(files)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def _type_string(param: dict[str, Any]) -> str:
    """Canonical ABI type of a parameter (tuples expanded)."""
    typ = param["type"]
    if typ.startswith("tuple"):
        inner = ",".join(_type_string(c) for c in param.get("components", []))
        return f"({inner}){typ[5:]}"
    return typ


def signature(entry: dict[str, Any]) -> str:
    """``Name(type1,type2)`` of an ABI error, event or function entry."""
    return f"{entry['name']}({','.join(_type_string(p) for p in entry.get('inputs', []))})"


def selector(entry_or_signature: dict[str, Any] | str) -> str:
    """4-byte selector (``0x`` + 8 hex) of an ABI entry or signature string."""
    sig = entry_or_signature if isinstance(entry_or_signature, str) else signature(entry_or_signature)
    return Web3.to_hex(keccak(text=sig)[:4])


@dataclass(frozen=True)
class ErrorSpec:
    """One custom error of the selector table."""

    name: str
    signature: str
    selector: str
    types: tuple[str, ...]
    arg_names: tuple[str, ...]
    contracts: tuple[str, ...]


@dataclass(frozen=True)
class DecodedError:
    """A decoded revert: ``name``, JSON-safe ``args`` and ``text``, the ``revert_error`` string.

    ``text`` is ``Name(arg, ...)``: integers in decimal, addresses and bytes as lowercase ``0x`` hex,
    strings JSON-quoted, e.g. ``GradeNotAllowed(7, 2, 48)`` or ``Error("Self-feedback not allowed")``.
    """

    name: str
    args: tuple[Any, ...]
    selector: str | None
    raw: str
    text: str


def _arg_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return Web3.to_hex(bytes(value))
    if isinstance(value, str):
        if value.startswith("0x") and len(value) == 42:
            return value.lower()
        return json.dumps(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_arg_text(v) for v in value) + "]"
    return str(value)


def _decoded(name: str, values: tuple[Any, ...], sel: str | None, raw: str) -> DecodedError:
    text = f"{name}({', '.join(_arg_text(v) for v in values)})"
    return DecodedError(name, tuple(json_safe(v) for v in values), sel, raw, text)


@lru_cache(maxsize=1)
def error_table() -> dict[str, ErrorSpec]:
    """Selector -> :class:`ErrorSpec` for every ``error`` entry in ``abi/*.json`` (plus Error, Panic).

    The same error declared in several contracts (``ZeroAddress()``) maps to one entry that lists every
    contract. A selector clash between different signatures raises ``ValueError``.
    """
    table: dict[str, ErrorSpec] = {
        ERROR_STRING_SELECTOR: ErrorSpec(
            "Error", "Error(string)", ERROR_STRING_SELECTOR, ("string",), ("message",), ()
        ),
        PANIC_SELECTOR: ErrorSpec("Panic", "Panic(uint256)", PANIC_SELECTOR, ("uint256",), ("code",), ()),
    }
    for name in artefact_names():
        for entry in abi_of(name):
            if entry.get("type") != "error":
                continue
            sig = signature(entry)
            sel = selector(sig)
            types = tuple(_type_string(p) for p in entry.get("inputs", []))
            arg_names = tuple(p.get("name", "") for p in entry.get("inputs", []))
            known = table.get(sel)
            if known is not None:
                if known.signature != sig:
                    raise ValueError(f"error selector clash {sel}: {known.signature} vs {sig}")
                if name not in known.contracts:
                    table[sel] = ErrorSpec(known.name, sig, sel, types, arg_names, (*known.contracts, name))
                continue
            table[sel] = ErrorSpec(entry["name"], sig, sel, types, arg_names, (name,))
    return table


def json_safe(value: Any) -> Any:
    """Convert decoded ABI values to JSON-safe values.

    Bytes become ``0x`` hex, addresses lowercase, tuples lists, and integers beyond 2**53 decimal
    strings (canonical JSON forbids them as numbers, spec 0.1).
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if abs(value) <= 2**53 else str(value)
    if isinstance(value, bytes | bytearray):
        return Web3.to_hex(bytes(value))
    if isinstance(value, str):
        if value.startswith("0x") and len(value) == 42:
            return value.lower()
        return value
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if hasattr(value, "items"):  # AttributeDict
        return {str(k): json_safe(v) for k, v in value.items()}
    return str(value)


def _normalise_data(data: Any) -> str | None:
    if data is None:
        return None
    if isinstance(data, bytes | bytearray):
        return Web3.to_hex(bytes(data))
    if isinstance(data, str):
        text = data.strip()
        if text.startswith(("0x", "0X")):
            return "0x" + text[2:].lower()
        try:
            int(text, 16)
        except ValueError:
            return None
        return "0x" + text.lower()
    return None


def decode_revert(data: Any) -> DecodedError:
    """Decode revert data (``0x`` hex or bytes) with the selector table.

    Empty data gives ``Reverted()``; an unknown selector gives ``UnknownError(0x<selector>)``.
    """
    raw = _normalise_data(data) or "0x"
    if len(raw) < 10:
        return _decoded("Reverted", (), None, raw)
    sel = raw[:10]
    spec = error_table().get(sel)
    if spec is None:
        return DecodedError("UnknownError", (sel,), sel, raw, f"UnknownError({sel})")
    try:
        values = tuple(abi_decode(list(spec.types), bytes.fromhex(raw[10:]))) if spec.types else ()
    except Exception:  # noqa: BLE001 - malformed payload: still report the name
        return _decoded(spec.name, (), sel, raw)
    return _decoded(spec.name, values, sel, raw)


def revert_data_from_exception(exc: BaseException) -> str | None:
    """Revert data carried by a web3 exception (``ContractCustomError``, ``ContractLogicError``, RPC error)."""
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        data = data.get("data")
    found = _normalise_data(data)
    if found:
        return found
    response = getattr(exc, "rpc_response", None)
    if isinstance(response, dict):
        err = response.get("error") or {}
        inner = err.get("data") if isinstance(err, dict) else None
        if isinstance(inner, dict):
            inner = inner.get("data")
        found = _normalise_data(inner)
        if found:
            return found
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, dict):
            found = _normalise_data(arg.get("data"))
            if found:
                return found
        if isinstance(arg, str) and arg.startswith("0x") and len(arg) >= 10:
            found = _normalise_data(arg)
            if found:
                return found
    return None


def decode_exception(exc: BaseException) -> DecodedError:
    """Decode the revert behind a web3 exception; a plain message becomes ``Error("<message>")``."""
    data = revert_data_from_exception(exc)
    if data and len(data) >= 10:
        return decode_revert(data)
    message = getattr(exc, "message", None) or (exc.args[0] if exc.args else "") or type(exc).__name__
    if isinstance(message, dict):
        message = message.get("message") or json.dumps(json_safe(message), sort_keys=True)
    message = str(message)
    prefix = "execution reverted: "
    if message.strip() in ("execution reverted", "execution reverted:"):
        return _decoded("Reverted", (), None, data or "0x")
    if message.startswith(prefix):
        return _decoded("Error", (message[len(prefix) :],), ERROR_STRING_SELECTOR, data or "0x")
    return _decoded("Error", (message,), None, data or "0x")


def error_name(revert_error: str | None) -> str | None:
    """The bare name of a ``revert_error`` string (``"GradeNotAllowed(7, 2, 48)"`` -> ``"GradeNotAllowed"``)."""
    if not revert_error:
        return None
    return revert_error.split("(", 1)[0]


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventSpec:
    name: str
    signature: str
    topic0: str
    abi: dict[str, Any]
    indexed_count: int
    contracts: tuple[str, ...]


@lru_cache(maxsize=1)
def event_table() -> dict[str, tuple[EventSpec, ...]]:
    """topic0 -> candidate event ABIs (ERC-20 and ERC-721 ``Transfer`` share a topic0 but differ in
    the number of indexed parameters, which picks the right one when decoding)."""
    table: dict[str, list[EventSpec]] = {}
    for name in artefact_names():
        for entry in abi_of(name):
            if entry.get("type") != "event" or entry.get("anonymous"):
                continue
            sig = signature(entry)
            topic0 = Web3.to_hex(keccak(text=sig))
            indexed = sum(1 for p in entry.get("inputs", []) if p.get("indexed"))
            candidates = table.setdefault(topic0, [])
            for i, known in enumerate(candidates):
                if known.indexed_count == indexed and known.signature == sig:
                    candidates[i] = EventSpec(known.name, sig, topic0, known.abi, indexed, (*known.contracts, name))
                    break
            else:
                candidates.append(EventSpec(entry["name"], sig, topic0, entry, indexed, (name,)))
    return {k: tuple(v) for k, v in table.items()}


def _hex_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.lower() if value.startswith("0x") else "0x" + value.lower()
    return Web3.to_hex(value)


def decode_log(log: Any, codec: Any = None) -> dict[str, Any] | None:
    """Decode one receipt log with our ABIs; ``None`` if no ABI matches.

    Returns ``{"address", "event", "args", "logIndex", "blockNumber", "transactionHash"}`` with
    JSON-safe values.
    """
    from web3._utils.events import get_event_data

    topics = log.get("topics") or []
    if not topics:
        return None
    topic0 = Web3.to_hex(topics[0]) if not isinstance(topics[0], str) else topics[0].lower()
    candidates = event_table().get(topic0, ())
    if codec is None:
        codec = Web3().codec
    for spec in candidates:
        if spec.indexed_count != len(topics) - 1:
            continue
        try:
            data = get_event_data(codec, spec.abi, log)
        except Exception:  # noqa: BLE001, S112 - try the next candidate
            continue
        tx_hash = log.get("transactionHash")
        return {
            "address": str(log.get("address", "")).lower(),
            "event": spec.name,
            "args": json_safe(dict(data["args"])),
            "logIndex": int(log.get("logIndex", 0) or 0),
            "blockNumber": int(log.get("blockNumber", 0) or 0),
            "transactionHash": _hex_or_none(tx_hash),
        }
    return None


__all__ = [
    "ABI_DIR",
    "ALIASES",
    "ERROR_STRING_SELECTOR",
    "PANIC_CODES",
    "PANIC_SELECTOR",
    "AbiNotFound",
    "DecodedError",
    "ErrorSpec",
    "EventSpec",
    "abi_of",
    "abi_sha",
    "artefact_names",
    "bytecode_of",
    "codehash_of",
    "decode_exception",
    "decode_log",
    "decode_revert",
    "error_name",
    "error_table",
    "event_table",
    "json_safe",
    "load",
    "revert_data_from_exception",
    "selector",
    "signature",
]
