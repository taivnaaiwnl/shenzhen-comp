"""Sybil clusters (spec 4.1).

AgentIds are merged (union-find) when they share an identity owner address, share a vault owner
address, or one's identity owner is the other's vault owner. In other words: every agent is linked to
the set ``{identity owner} | {owners of its vaults}`` (as of ``asOfBlock``) and two agents that share
any address are one cluster. A cluster counts as one counterparty for diversity and as one payer for
failure caps. Funding-source clustering is planned (KR-4), not implemented.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


class UnionFind:
    """Union-find over hashable keys; the representative of a set is its smallest member."""

    def __init__(self) -> None:
        self._parent: dict = {}

    def add(self, x) -> None:
        self._parent.setdefault(x, x)

    def find(self, x):
        self.add(x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:  # path compression
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if _key(rb) < _key(ra):
            ra, rb = rb, ra
        self._parent[rb] = ra


def _key(x) -> tuple:
    # agent nodes ("a", id) sort before address nodes ("x", address); ids numerically
    return (x[0], x[1]) if isinstance(x, tuple) else (str(type(x)), x)


def build_clusters(addresses: Mapping[int, Iterable[str]]) -> dict[int, int]:
    """Map every agentId to its cluster id (the smallest agentId in its cluster).

    ``addresses`` maps an agentId to the addresses that control it (identity owner and vault owners).
    Empty or zero addresses are ignored.
    """
    uf = UnionFind()
    zero = "0x" + "00" * 20
    for agent_id, addrs in addresses.items():
        node = ("a", int(agent_id))
        uf.add(node)
        for addr in addrs:
            if not addr:
                continue
            a = str(addr).lower()
            if a == zero:
                continue
            uf.union(node, ("x", a))
    groups: dict[object, list[int]] = {}
    for agent_id in addresses:
        groups.setdefault(uf.find(("a", int(agent_id))), []).append(int(agent_id))
    out: dict[int, int] = {}
    for members in groups.values():
        rep = min(members)
        for m in members:
            out[m] = rep
    return out


def members(clusters: Mapping[int, int], cluster_id: int) -> list[int]:
    """Sorted agentIds of one cluster."""
    return sorted(a for a, c in clusters.items() if c == cluster_id)


__all__ = ["UnionFind", "build_clusters", "members"]
