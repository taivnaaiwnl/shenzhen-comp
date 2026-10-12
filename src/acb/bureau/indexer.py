"""Bureau indexer (spec 4.11): an in-memory, append-only store of decoded logs.

- Logs are fetched with ``eth_getLogs`` in chunks of ``logs_chunk_blocks`` (200 on public Base Sepolia,
  5000 on Anvil) from the deployment's ``startBlock``, with 3 retries (0.5 s, 1 s, 2 s backoff), by
  :func:`acb.chain.client.get_raw_logs_chunked`; ``to_block`` is the requested as-of block.
- The ACB is ours, so its logs are read unfiltered. The vault set is discovered from
  ``VaultRegistered``; each vault's ``token()``, ``bureau()`` and ``identity()`` are re-read once and a
  vault that does not match the deployment is ignored (with its look-alike events).
- The ERC-8004 registries on Base Sepolia are global: they are only ever queried with the agentIds of
  the grading universe as indexed topics (``agentId``, ``tokenId``) and, for reports, with
  ``indexedTag1 == keccak("acb-report")``. They are never scanned unfiltered.
- The grading universe is every agentId in ``deployments/<network>.json`` plus every agentId that
  appears in a registered vault's events. A newly seen vault or agentId is back-filled from
  ``startBlock``.

Only logs and block headers are read (plus the three immutable vault getters), so the indexer works
without an archive node.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from web3 import Web3

from acb.chain import abi as abi_mod
from acb.chain.client import get_raw_logs_chunked
from acb.chain.contracts import VaultContract
from acb.types import Deployments

TOPIC_REGISTERED = Web3.to_hex(Web3.keccak(text="Registered(uint256,string,address)"))
TOPIC_METADATA_SET = Web3.to_hex(Web3.keccak(text="MetadataSet(uint256,string,string,bytes)"))
TOPIC_TRANSFER = Web3.to_hex(Web3.keccak(text="Transfer(address,address,uint256)"))
TOPIC_NEW_FEEDBACK = Web3.to_hex(
    Web3.keccak(text="NewFeedback(uint256,address,uint64,int128,uint8,string,string,string,string,string,bytes32)")
)
TOPIC_FEEDBACK_REVOKED = Web3.to_hex(Web3.keccak(text="FeedbackRevoked(uint256,address,uint64)"))
REPORT_TAG = "acb-report"
TOPIC_REPORT_TAG = Web3.to_hex(Web3.keccak(text=REPORT_TAG))

ID_TOPIC_BATCH = 50
"""At most this many agentIds per OR-list of topics in one query."""

PAYEE_EVENTS = ("Paid", "EscrowOpened", "DecisionRecorded", "OwnerApproved", "DeliveryClaimed")


@dataclass(frozen=True)
class Log:
    """One decoded log (JSON-safe ``args``: addresses lowercase, bytes as ``0x`` hex)."""

    address: str
    event: str
    args: dict
    block: int
    log_index: int
    tx: str

    @property
    def pos(self) -> tuple[int, int]:
        return (self.block, self.log_index)


def id_topic(agent_id: int) -> str:
    return "0x" + f"{int(agent_id):064x}"


def decode_raw(raw: Any, codec: Any) -> Log | None:
    """Decode one raw log with our ABIs (None if no ABI matches)."""
    d = abi_mod.decode_log(raw, codec)
    if d is None:
        return None
    return Log(
        address=d["address"],
        event=d["event"],
        args=d["args"],
        block=int(d["blockNumber"]),
        log_index=int(d["logIndex"]),
        tx=d["transactionHash"] or "",
    )


def _sorted(logs: Iterable[Log]) -> list[Log]:
    unique = {(lg.block, lg.log_index, lg.address): lg for lg in logs}
    return sorted(unique.values(), key=lambda lg: lg.pos)


class Indexer:
    """Decoded logs of one deployment up to :attr:`synced_to` (see the module docstring)."""

    def __init__(
        self,
        w3: Any,
        dep: Deployments,
        *,
        chunk_blocks: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.w3 = w3
        self.dep = dep
        self.acb = dep.contracts["AgentCreditBureau"].address
        self.identity = dep.contracts["IdentityRegistry"].address
        self.reputation = dep.contracts["ReputationRegistry"].address
        self.token = dep.contracts["USDC"].address
        self.start_block = int(dep.start_block)
        self.synced_to = self.start_block - 1
        self.chunk_blocks = chunk_blocks
        self._sleep = sleep
        self.acb_logs: list[Log] = []
        self.vault_logs: dict[str, list[Log]] = {}
        self.identity_logs: list[Log] = []
        self.reputation_logs: list[Log] = []
        self.vaults: dict[str, int] = {}
        """Registered, config-bound vaults -> agentId."""
        self.rejected_vaults: dict[str, str] = {}
        """Registered vaults whose configuration does not match the deployment -> reason."""
        self.universe: set[int] = {int(a.agent_id) for a in dep.agents.values()}
        self._fetched_ids: set[int] = set()
        self._blocks: dict[int, tuple[int, str]] = {}
        self.rpc_calls = 0

    # ---- low level ----------------------------------------------------------------------------

    def _fetch(self, address: str | list[str], topics: list | None, a: int, b: int) -> list[Log]:
        if b < a:
            return []
        raw = get_raw_logs_chunked(self.w3, address, topics, a, b, chunk_blocks=self.chunk_blocks, sleep=self._sleep)
        self.rpc_calls += 1
        codec = self.w3.codec
        out = []
        for item in raw:
            lg = decode_raw(item, codec)
            if lg is not None:
                out.append(lg)
        return out

    def block(self, number: int) -> tuple[int, str]:
        """``(timestamp, hash)`` of a block (cached)."""
        number = int(number)
        if number not in self._blocks:
            blk = self.w3.eth.get_block(number)
            self._blocks[number] = (int(blk["timestamp"]), Web3.to_hex(blk["hash"]))
        return self._blocks[number]

    def timestamp(self, number: int) -> int:
        return self.block(number)[0]

    def _vault_config_ok(self, vault: str) -> str:
        """``""`` if the vault's token, bureau and identity are the deployment's, else a reason."""
        try:
            v = VaultContract(self.w3, vault)
            if v.token() != self.token:
                return "token"
            if v.bureau() != self.acb:
                return "bureau"
            if v.identity() != self.identity:
                return "identity"
        except Exception as exc:  # noqa: BLE001 - not a vault at all
            return f"unreadable: {type(exc).__name__}"
        return ""

    def _fetch_registries(self, ids: Iterable[int], a: int, b: int) -> tuple[list[Log], list[Log]]:
        ident: list[Log] = []
        rep: list[Log] = []
        ordered = sorted(int(i) for i in ids)
        for k in range(0, len(ordered), ID_TOPIC_BATCH):
            batch = [id_topic(i) for i in ordered[k : k + ID_TOPIC_BATCH]]
            ident += self._fetch(self.identity, [[TOPIC_REGISTERED, TOPIC_METADATA_SET], batch], a, b)
            ident += self._fetch(self.identity, [TOPIC_TRANSFER, None, None, batch], a, b)
            rep += self._fetch(self.reputation, [TOPIC_NEW_FEEDBACK, batch, None, TOPIC_REPORT_TAG], a, b)
            rep += self._fetch(self.reputation, [TOPIC_FEEDBACK_REVOKED, batch], a, b)
        return ident, rep

    @staticmethod
    def _agent_ids_in(logs: Iterable[Log]) -> set[int]:
        ids: set[int] = set()
        for lg in logs:
            if lg.event in PAYEE_EVENTS and "payeeAgentId" in lg.args:
                ids.add(int(lg.args["payeeAgentId"]))
            elif lg.event == "MandateRegistered":
                ids.update(int(x) for x in lg.args.get("payeeAgentIds", []))
            elif lg.event == "VaultInitialized":
                ids.add(int(lg.args["agentId"]))
        return ids

    # ---- sync ---------------------------------------------------------------------------------

    def sync(self, to_block: int) -> None:
        """Fetch every relevant log up to ``to_block`` (inclusive). Idempotent; incremental."""
        to_block = int(to_block)
        if to_block <= self.synced_to:
            return
        a, b = self.synced_to + 1, to_block
        start = self.start_block

        # 1. our ACB (VaultRegistered, GradePosted, appeals ...)
        acb_new = self._fetch(self.acb, None, a, b)
        self.acb_logs = _sorted([*self.acb_logs, *acb_new])

        # 2. vaults discovered from VaultRegistered; config re-checked
        new_vaults: list[str] = []
        for lg in acb_new:
            if lg.event != "VaultRegistered":
                continue
            vault = str(lg.args["vault"]).lower()
            if vault in self.vaults or vault in self.rejected_vaults:
                continue
            reason = self._vault_config_ok(vault)
            if reason:
                self.rejected_vaults[vault] = reason
                continue
            self.vaults[vault] = int(lg.args["agentId"])
            new_vaults.append(vault)
        fresh: list[Log] = []
        if new_vaults and a > start:
            fresh += self._fetch(new_vaults, None, start, a - 1)
        if self.vaults:
            fresh += self._fetch(sorted(self.vaults), None, a, b)
        for lg in fresh:
            self.vault_logs.setdefault(lg.address, []).append(lg)
        for vault in list(self.vault_logs):
            self.vault_logs[vault] = _sorted(self.vault_logs[vault])

        # 3. grading universe and the (filtered) ERC-8004 registries
        self.universe |= set(self.vaults.values()) | self._agent_ids_in(fresh)
        new_ids = self.universe - self._fetched_ids
        ident: list[Log] = []
        rep: list[Log] = []
        if new_ids and a > start:
            i2, r2 = self._fetch_registries(new_ids, start, a - 1)
            ident += i2
            rep += r2
        i3, r3 = self._fetch_registries(self.universe, a, b)
        ident += i3
        rep += r3
        self._fetched_ids |= self.universe
        self.identity_logs = _sorted([*self.identity_logs, *ident])
        self.reputation_logs = _sorted([*self.reputation_logs, *rep])
        self.synced_to = b

    # ---- views as of a block -------------------------------------------------------------------

    def acb_events(self, name: str, as_of: int) -> list[Log]:
        return [lg for lg in self.acb_logs if lg.event == name and lg.block <= as_of]

    def registered_vaults(self, as_of: int) -> dict[str, int]:
        """Config-bound vaults whose ``VaultRegistered`` is at or before ``as_of`` -> agentId."""
        out: dict[str, int] = {}
        for lg in self.acb_events("VaultRegistered", as_of):
            vault = str(lg.args["vault"]).lower()
            if vault in self.vaults:
                out[vault] = self.vaults[vault]
        return out

    def vault_events(self, vault: str, as_of: int) -> list[Log]:
        return [lg for lg in self.vault_logs.get(vault.lower(), []) if lg.block <= as_of]

    def identity_events(self, as_of: int) -> list[Log]:
        return [lg for lg in self.identity_logs if lg.block <= as_of]

    def reputation_events(self, as_of: int) -> list[Log]:
        return [lg for lg in self.reputation_logs if lg.block <= as_of]

    def postings(self, as_of: int) -> dict[int, list[Log]]:
        """``GradePosted`` logs per agentId at or before ``as_of`` (chain order)."""
        out: dict[int, list[Log]] = {}
        for lg in self.acb_events("GradePosted", as_of):
            out.setdefault(int(lg.args["agentId"]), []).append(lg)
        return out


__all__ = [
    "REPORT_TAG",
    "TOPIC_FEEDBACK_REVOKED",
    "TOPIC_METADATA_SET",
    "TOPIC_NEW_FEEDBACK",
    "TOPIC_REGISTERED",
    "TOPIC_REPORT_TAG",
    "TOPIC_TRANSFER",
    "Indexer",
    "Log",
    "decode_raw",
    "id_topic",
]
