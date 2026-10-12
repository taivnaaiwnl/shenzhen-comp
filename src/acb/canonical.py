"""Canonical JSON, keccak helpers, order ids, EIP-191 signing and atomic file writes (spec 0.1, 6.6, 9.2).

Every hash of a JSON object in this project is ``chash(obj) = keccak256(canonical_bytes(obj))`` and
every hashed file is written with :func:`atomic_write_bytes` (``write_bytes`` of the canonical bytes
through a temp file and ``os.replace``). Nothing here reads a clock, the network or the environment.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from web3 import Web3

MAX_JSON_INT = 2**53
"""Integers with an absolute value above this MUST be strings in hashed JSON (spec 0.1)."""


# ---------------------------------------------------------------------------
# canonical JSON
# ---------------------------------------------------------------------------


def _check(obj: Any, path: str) -> None:
    """Reject every value canonical JSON does not allow (floats first of all)."""
    if obj is None or isinstance(obj, (bool, str)):
        return
    if isinstance(obj, int):
        if abs(obj) > MAX_JSON_INT:
            raise TypeError(f"{path}: integer {obj} is above 2**53; write it as a decimal string")
        return
    if isinstance(obj, float):
        raise TypeError(f"{path}: floats are not allowed in canonical JSON")
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"{path}: object keys must be strings, got {type(key).__name__}")
            _check(value, f"{path}.{key}")
        return
    if isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            _check(value, f"{path}[{i}]")
        return
    raise TypeError(f"{path}: {type(obj).__name__} is not allowed in canonical JSON")


def canonical_bytes(obj: Any) -> bytes:
    """Canonical JSON bytes: sorted keys, no spaces, ASCII only, no NaN, UTF-8.

    Allowed: ``dict`` (string keys), ``list`` (tuples are written as lists), ``str``, ``int``
    (``|n| <= 2**53``), ``bool`` and ``None``. Raises ``TypeError`` on floats and anything else.
    """
    _check(obj, "$")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def keccak_hex(data: bytes) -> str:
    """keccak256 of ``data`` as lowercase ``0x`` + 64 hex."""
    return Web3.to_hex(Web3.keccak(data))


def chash(obj: Any) -> str:
    """keccak256 of :func:`canonical_bytes` as lowercase ``0x`` + 64 hex."""
    return keccak_hex(canonical_bytes(obj))


# ---------------------------------------------------------------------------
# identifiers
# ---------------------------------------------------------------------------


def _hex32_bytes(value: str | bytes) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    else:
        if not (isinstance(value, str) and value[:2] in ("0x", "0X")):
            raise ValueError("expected 0x-prefixed hex")
        data = bytes.fromhex(value[2:])
    if len(data) != 32:
        raise ValueError(f"expected 32 bytes, got {len(data)}")
    return data


def order_id(chain_id: int, vault: str, issuer_agent_id: int, invoice_id: str) -> str:
    """``keccak256(abi.encode(uint256 chainId, address vault, uint256 issuerAgentId, bytes32 keccak256(bytes(invoiceId))))``.

    The amount is deliberately not part of the id (spec 6.6).
    """
    invoice_key = Web3.keccak(text=invoice_id)
    encoded = abi_encode(
        ["uint256", "address", "uint256", "bytes32"],
        [int(chain_id), Web3.to_checksum_address(vault), int(issuer_agent_id), invoice_key],
    )
    return keccak_hex(encoded)


def invoice_hash(invoice: Mapping[str, Any] | Any) -> str:
    """``invoiceHash = chash(invoice without "signature")`` over the wire object of spec 9.2.

    Accepts the wire ``dict`` (as received or as signed) or an object with ``wire_dict()``
    (``acb.types.Invoice`` / ``SignedInvoice``).
    """
    if hasattr(invoice, "wire_dict"):
        invoice = invoice.wire_dict()
    if not isinstance(invoice, Mapping):
        raise TypeError("invoice must be a mapping or have wire_dict()")
    return chash({k: v for k, v in invoice.items() if k != "signature"})


def short_string_bytes32(text: str) -> bytes:
    """A bytes32 short string (model ids such as ``acb-sc-1.0.0``): ASCII, left-aligned, zero-padded."""
    raw = text.encode("ascii")
    if len(raw) > 32:
        raise ValueError("short string longer than 32 bytes")
    return raw.ljust(32, b"\x00")


def short_string_from_bytes32(value: bytes | str) -> str:
    """Inverse of :func:`short_string_bytes32`."""
    return _hex32_bytes(value).rstrip(b"\x00").decode("ascii")


# ---------------------------------------------------------------------------
# EIP-191 signatures over 32-byte digests
# ---------------------------------------------------------------------------


def _account(key: str | bytes | LocalAccount) -> LocalAccount:
    if isinstance(key, LocalAccount):
        return key
    return Account.from_key(key)


def sign_digest_eip191(key: str | bytes | LocalAccount, digest: str | bytes) -> str:
    """EIP-191 ``personal_sign`` of a 32-byte digest (``encode_defunct(primitive=bytes32)``); 0x hex signature."""
    message = encode_defunct(primitive=_hex32_bytes(digest))
    signed = _account(key).sign_message(message)
    return Web3.to_hex(signed.signature)


SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
"""Order of the secp256k1 group; a signature's ``s`` must be at most ``N // 2`` (EIP-2)."""


def recover_eip191(digest: str | bytes, signature: str | bytes) -> str:
    """Address (lowercase) that signed ``digest`` with :func:`sign_digest_eip191`.

    Like OpenZeppelin ``ECDSA.recover``: the signature must be 65 bytes ``r || s || v`` with
    ``v`` in {27, 28} and a low ``s`` (``s <= N/2``), so one signer has exactly one valid signature
    per digest (no malleable twin). Raises ``ValueError`` otherwise.
    """
    message = encode_defunct(primitive=_hex32_bytes(digest))
    sig = (
        bytes(signature)
        if isinstance(signature, (bytes, bytearray))
        else bytes.fromhex(signature[2:] if signature[:2] in ("0x", "0X") else signature)
    )
    if len(sig) != 65:
        raise ValueError(f"signature must be 65 bytes, got {len(sig)}")
    s = int.from_bytes(sig[32:64], "big")
    if s == 0 or s > SECP256K1_N // 2:
        raise ValueError("signature s value is not in the lower half order (malleable)")
    if sig[64] not in (27, 28):
        raise ValueError("signature v must be 27 or 28")
    return Account.recover_message(message, signature=sig).lower()


def sign_invoice(key: str | bytes | LocalAccount, invoice: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of the wire invoice with ``signature`` = EIP-191 signature of its ``invoiceHash``."""
    body = {k: v for k, v in invoice.items() if k != "signature"}
    return {**body, "signature": sign_digest_eip191(key, invoice_hash(body))}


def recover_invoice_signer(invoice: Mapping[str, Any]) -> str:
    """Address (lowercase) that signed a wire invoice; raises ``KeyError`` without ``signature``."""
    return recover_eip191(invoice_hash(invoice), invoice["signature"])


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> Path:
    """Write ``data`` to ``path`` atomically: temp file in the same directory, fsync, ``os.replace``.

    Parent directories are created. On Windows ``os.replace`` can fail briefly while another process
    has the target open; it is retried for up to about one second.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp_name, target)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def write_canonical(path: str | os.PathLike[str], obj: Any) -> str:
    """Write ``canonical_bytes(obj)`` atomically to ``path`` and return its ``chash``."""
    data = canonical_bytes(obj)
    atomic_write_bytes(path, data)
    return keccak_hex(data)


__all__ = [
    "MAX_JSON_INT",
    "SECP256K1_N",
    "atomic_write_bytes",
    "canonical_bytes",
    "chash",
    "invoice_hash",
    "keccak_hex",
    "order_id",
    "recover_eip191",
    "recover_invoice_signer",
    "short_string_bytes32",
    "short_string_from_bytes32",
    "sign_digest_eip191",
    "sign_invoice",
    "write_canonical",
]
