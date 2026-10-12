"""Chain layer (WP3): the only code that sends transactions, plus the typed read layer.

Interfaces (spec 15.3): :func:`make_web3`, :func:`get_logs_chunked`, :func:`now`, :func:`warp`,
:class:`TxSender` / :class:`~acb.types.TxResult`, the typed wrappers in :mod:`acb.chain.contracts`,
:func:`load_deployments` and :func:`acb.chain.deploy.deploy_all` (imported lazily: ``from
acb.chain.deploy import deploy_all``).
"""

from __future__ import annotations

from acb.chain.client import get_logs_chunked, make_web3
from acb.chain.clock import advance_to, now, warp
from acb.chain.contracts import (
    AcbContract,
    ChainContracts,
    Erc20Contract,
    IdentityContract,
    ReputationContract,
    VaultContract,
    bind,
)
from acb.chain.deployments import load_deployments, save_deployments
from acb.chain.tx import NonceManager, TxSender, TxSimulationError, resync_all

__all__ = [
    "AcbContract",
    "ChainContracts",
    "Erc20Contract",
    "IdentityContract",
    "NonceManager",
    "ReputationContract",
    "TxSender",
    "TxSimulationError",
    "VaultContract",
    "advance_to",
    "bind",
    "get_logs_chunked",
    "load_deployments",
    "make_web3",
    "now",
    "resync_all",
    "save_deployments",
    "warp",
]
