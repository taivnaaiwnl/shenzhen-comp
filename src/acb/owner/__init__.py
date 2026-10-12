"""Owner console (process P1, package WP7, spec 11): the human owner's view of the wallet.

P1 holds the owner key, ``OWNER_UI_PASSWORD``, ``SESSION_SECRET``, ``AGENT_TOKEN`` (chat and
notifications) and the read-only ``EVENTS_TOKEN``; it has no ``SIGNER_TOKEN``. It renders every
confirmation screen from the chain and from signatures it verifies itself, never from text supplied by
the wallet agent, and signs exactly what it displayed (trust boundary TB7, spec 2.2).

- :mod:`acb.owner.chain_views` - chain reads for the panels and the verification behind every confirm
  screen (mandate cards rebuilt from chain reads, approvals from a signed invoice verified here)
- :mod:`acb.owner.actions` - the owner transactions (``registerMandate``, ``revokeMandate``,
  ``ownerApprove``, ``freeze``, ``unfreeze`` with key rotation) and the agent notifications
- :mod:`acb.owner.agent_client` - the HTTP client of the wallet agent API (spec 8.8)
- :mod:`acb.owner.feeds` - the merged event stream of every service (SSE, spec 11.1)
- :mod:`acb.owner.security` - login sessions, CSRF tokens and the response headers (CSP)
- :mod:`acb.owner.app` - the FastAPI app ``acb owner serve`` (127.0.0.1:8700)
- :mod:`acb.owner.cli` - ``acb owner serve|status|freeze|unfreeze|approve|revoke|confirm|bad-data``
"""

from __future__ import annotations

__all__: list[str] = []
