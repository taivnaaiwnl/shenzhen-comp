"""Block explorer links per network (spec 10.3, 10.4, 13.2).

The explorers come from ``config/networks.yaml`` (Base Sepolia: basescan and blockscout; Anvil: none).
"""

from __future__ import annotations

from acb.types import NetworkProfile


def _pick(net: NetworkProfile, kind: str, name: str | None) -> str | None:
    for explorer in net.explorers:
        if name is not None and explorer.get("name") != name:
            continue
        template = explorer.get(kind)
        if template:
            return str(template)
    return None


def tx_url(net: NetworkProfile, tx_hash: str, explorer: str | None = None) -> str | None:
    """URL of a transaction on ``explorer`` (default: the first one), or None when there is none."""
    template = _pick(net, "tx", explorer)
    return template.format(hash=tx_hash) if template else None


def address_url(net: NetworkProfile, address: str, explorer: str | None = None) -> str | None:
    """URL of an address on ``explorer`` (default: the first one), or None when there is none."""
    template = _pick(net, "address", explorer)
    return template.format(addr=address) if template else None


def tx_urls(net: NetworkProfile, tx_hash: str) -> dict[str, str]:
    """``{explorer name: tx URL}`` for every explorer of the network (the ``explorer`` object of 10.4)."""
    out: dict[str, str] = {}
    for explorer in net.explorers:
        template = explorer.get("tx")
        if template and explorer.get("name"):
            out[str(explorer["name"])] = str(template).format(hash=tx_hash)
    return out


def address_urls(net: NetworkProfile, address: str) -> dict[str, str]:
    """``{explorer name: address URL}`` for every explorer of the network."""
    out: dict[str, str] = {}
    for explorer in net.explorers:
        template = explorer.get("address")
        if template and explorer.get("name"):
            out[str(explorer["name"])] = str(template).format(addr=address)
    return out


__all__ = ["address_url", "address_urls", "tx_url", "tx_urls"]
