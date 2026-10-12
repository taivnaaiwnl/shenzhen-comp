"""Wallet agent (process P2, package WP6, spec 8): the LLM-facing buyer that holds no private keys.

- :mod:`acb.agent.llm` - ``LLMClient`` protocol, ``OpenAICompatClient``, ``FallbackLLM`` (8.2)
- :mod:`acb.agent.mock_llm` - deterministic ``MockLLM`` with the ``careful`` and ``naive`` personas (8.7)
- :mod:`acb.agent.fallback_parser` - the rule-based parser behind ``FallbackLLM`` (8.6)
- :mod:`acb.agent.tools` - closed owner-turn and invoice-turn tool sets and their validators (8.3)
- :mod:`acb.agent.prompts` - system prompt ``acb-prompt-1.0.0`` and message builders (8.4)
- :mod:`acb.agent.quarantine` - DATA blocks for counterparty text (8.5)
- :mod:`acb.agent.redact` - exact-value, BIP-39 and key-prefix redactor (8.6)
- :mod:`acb.agent.directory` - pinned directory, payee resolution, lookalike warnings (8.3)
- :mod:`acb.agent.dialogue` - owner turns and the dialogue state machine (8.6)
- :mod:`acb.agent.runtime` - quotes, validation, invoice turns, execution, delivery, reports (2.4, 6, 9)
- :mod:`acb.agent.quotes`, :mod:`acb.agent.delivery_check`, :mod:`acb.agent.store`,
  :mod:`acb.agent.chain`, :mod:`acb.agent.signer_client` - the parts the runtime builds on
- :mod:`acb.agent.service`, :mod:`acb.agent.cli` - ``acb agent serve`` (8.8) and ``acb chat`` (11.3)
"""

from __future__ import annotations

from acb.agent.config import PROMPT_VERSION, AgentConfig

__all__ = ["PROMPT_VERSION", "AgentConfig"]
