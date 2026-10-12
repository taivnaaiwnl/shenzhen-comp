"""Evidence files: published invoices and reports (inputs), and evidence bundles (outputs) (spec 4.1, 4.8).

- Inputs are read from ``<EVIDENCE_BASE_URI>/invoices/<invoiceHash>.json`` and
  ``<EVIDENCE_BASE_URI>/reports/<feedbackHash>.json``. During a run the files are first written under
  ``$ACB_OUT_DIR`` and exported to ``evidence/<network>/`` later, so :class:`EvidenceStore` searches a
  list of sources (local folders or http(s) URLs). **Every file is checked against its hash**, so the
  order of the sources never changes a result.
- Bundles are written with ``canonical_bytes`` to ``$ACB_OUT_DIR/bundles/<agentId>-<asOfBlock>.json``;
  their URI is ``EVIDENCE_BASE_URI + "bundles/<agentId>-<asOfBlock>.json"``.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from acb.canonical import canonical_bytes, chash, invoice_hash, recover_eip191, write_canonical
from acb.config import REPO_ROOT
from acb.types import Bundle, SignedInvoice

MAX_FILE_BYTES = 1_048_576
FETCH_TIMEOUT_S = 10.0


REPOSITORY_URL = "https://github.com/taivnaaiwnl/shenzhen-comp"
PUBLIC_EVIDENCE_BASE = "https://raw.githubusercontent.com/taivnaaiwnl/shenzhen-comp/main/evidence/{network}/"
"""Where the exported evidence of a public network is served (``evidence/<network>/`` of the repository)."""


def default_base_uri(network: str, environ: Mapping[str, str] | None = None) -> str:
    """``EVIDENCE_BASE_URI`` (with a trailing slash). When it is not set: the repository's public raw URL
    of ``evidence/<network>/`` for a public network (the URI is written on-chain permanently), and the
    local folder ``evidence/<network>/`` on Anvil."""
    env = os.environ if environ is None else environ
    base = (env.get("EVIDENCE_BASE_URI") or "").strip()
    if not base:
        base = f"evidence/{network}/" if network == "anvil" else PUBLIC_EVIDENCE_BASE.format(network=network)
    return base if base.endswith("/") else base + "/"


def bundle_name(agent_id: int, as_of_block: int) -> str:
    return f"{int(agent_id)}-{int(as_of_block)}.json"


def bundle_uri(base_uri: str, agent_id: int, as_of_block: int) -> str:
    """``EVIDENCE_BASE_URI + "bundles/<agentId>-<asOfBlock>.json"`` (goes on-chain as ``evidenceURI``)."""
    base = base_uri if base_uri.endswith("/") else base_uri + "/"
    return base + "bundles/" + bundle_name(agent_id, as_of_block)


def write_bundle(bundle: Bundle, out_dir: str | os.PathLike[str]) -> tuple[Path, str]:
    """Write ``bundle`` canonically to ``<out_dir>/bundles/<agentId>-<asOfBlock>.json``; returns (path, hash)."""
    path = Path(out_dir) / "bundles" / bundle_name(int(bundle.subject["agentId"]), int(bundle.chain["asOfBlock"]))
    digest = write_canonical(path, bundle.as_canonical())
    return path, digest


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def read_source(location: str) -> bytes | None:
    """Bytes of a local file (relative paths: repository root) or an http(s) URL; None if unavailable."""
    try:
        if _is_url(location):
            req = urllib.request.Request(location, headers={"User-Agent": "acb-bureau/1"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:  # http(s) only (checked above)
                data = resp.read(MAX_FILE_BYTES + 1)
            return data if len(data) <= MAX_FILE_BYTES else None
        path = Path(location)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_bytes()
    except (OSError, ValueError):
        return None


def read_bundle_source(location: str, name: str, local_dirs: Iterable[str | os.PathLike[str]]) -> bytes | None:
    """Bytes of a published bundle at an on-chain ``evidenceURI`` (or a fallback location), safely.

    The URI comes from the chain, so it is not trusted as a path: its file name must be exactly ``name``
    (``<agentId>-<asOfBlock>.json``), and a local path must resolve inside one of ``local_dirs``.
    http(s) URLs are fetched as they are.
    """
    if location.replace("\\", "/").rsplit("/", 1)[-1] != name:
        return None
    if _is_url(location):
        return read_source(location)
    try:
        path = Path(location)
        if not path.is_absolute():
            path = REPO_ROOT / path
        path = path.resolve()
        roots = [Path(d if Path(d).is_absolute() else REPO_ROOT / d).resolve() for d in local_dirs]
    except (OSError, ValueError):
        return None
    if not any(path.is_relative_to(root) for root in roots):
        return None
    return read_source(str(path))


def _join(source: str, rel: str) -> str:
    if _is_url(source):
        return source.rstrip("/") + "/" + rel
    return str(Path(source) / rel)


def _json(data: bytes | None) -> Any:
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None


class EvidenceStore:
    """Hash-checked reader of published invoices and reports from several sources."""

    def __init__(self, sources: Iterable[str | os.PathLike[str]]):
        self.sources = tuple(str(s) for s in sources if str(s))
        self._cache: dict[tuple[str, str], Any] = {}

    @classmethod
    def for_network(cls, network: str, *, extra: Iterable[str] = ()) -> EvidenceStore:
        """``extra`` first, then ``$ACB_OUT_DIR`` (when set), then ``EVIDENCE_BASE_URI``."""
        sources = [*extra]
        out = os.environ.get("ACB_OUT_DIR")
        if out:
            sources.append(out)
        sources.append(default_base_uri(network))
        return cls(sources)

    def _find(self, kind: str, digest: str, check) -> Any:
        key = (kind, digest)
        if key in self._cache:
            return self._cache[key]
        found = None
        for source in self.sources:
            obj = _json(read_source(_join(source, f"{kind}/{digest}.json")))
            if obj is not None and check(obj):
                found = obj
                break
        if found is not None:  # a file published later is found on the next call
            self._cache[key] = found
        return found

    def invoice(self, digest: str) -> dict | None:
        """The signed wire invoice whose ``invoiceHash`` is ``digest`` (or None)."""

        def ok(obj: Any) -> bool:
            try:
                return isinstance(obj, dict) and invoice_hash(obj) == digest
            except TypeError:
                return False

        return self._find("invoices", digest.lower(), ok)

    def report(self, digest: str) -> dict | None:
        """The report payload whose ``chash`` is ``digest`` (``feedbackHash``) or None."""

        def ok(obj: Any) -> bool:
            try:
                return isinstance(obj, dict) and chash(obj) == digest
            except TypeError:
                return False

        return self._find("reports", digest.lower(), ok)


def check_invoice(
    raw: dict | None, *, invoice_hash_hex: str, wallet: str | None, vault: str, payee_agent_id: int, amount: int
) -> str:
    """Counted-order rule 3 (spec 4.1): ``""`` if the published invoice backs the order, else a note."""
    if raw is None:
        return "no_invoice"
    try:
        si = SignedInvoice.from_wire(raw)
    except (ValueError, TypeError):
        return "bad_invoice"
    inv = si.invoice
    if si.invoice_hash != invoice_hash_hex:
        return "bad_invoice"
    try:
        signer = recover_eip191(si.invoice_hash, si.signature)
    except (ValueError, TypeError):
        return "bad_invoice"
    if wallet is None or signer != wallet:
        return "bad_invoice"
    if inv.buyer_vault != vault or inv.issuer_agent_id != payee_agent_id or inv.amount != amount:
        return "bad_invoice"
    return ""


# ---------------------------------------------------------------------------
# overrides (spec 4.7): disclosed manual grades
# ---------------------------------------------------------------------------

MANUAL_OVERRIDE = 1104


def with_override(bundle: Bundle, grade: str, reason: str, approved_by: str) -> Bundle:
    """The bundle with an ``override`` block; the model part (score, grade, reasons) is unchanged."""
    return bundle.model_copy(update={"override": {"grade": grade, "reason": reason, "approvedBy": approved_by}})


def posted_values(bundle: Bundle) -> tuple[str, int, tuple[int, int, int, int]]:
    """``(grade, score, reasons)`` that go on-chain for ``bundle``: the model's, or the override's grade
    with reason 1104 first."""
    reasons = tuple(int(r) for r in bundle.reasons)
    if bundle.override:
        listed = [r for r in reasons if r and r != MANUAL_OVERRIDE][:3]
        padded = [MANUAL_OVERRIDE, *listed] + [0] * (3 - len(listed))
        return str(bundle.override["grade"]), bundle.score, tuple(padded)  # type: ignore[return-value]
    return bundle.grade, bundle.score, reasons + (0,) * (4 - len(reasons))  # type: ignore[return-value]


def bundle_bytes(bundle: Bundle) -> bytes:
    return canonical_bytes(bundle.as_canonical())


__all__ = [
    "MANUAL_OVERRIDE",
    "PUBLIC_EVIDENCE_BASE",
    "REPOSITORY_URL",
    "EvidenceStore",
    "bundle_bytes",
    "bundle_name",
    "bundle_uri",
    "check_invoice",
    "default_base_uri",
    "posted_values",
    "read_bundle_source",
    "read_source",
    "with_override",
    "write_bundle",
]
