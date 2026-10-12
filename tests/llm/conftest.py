"""Dialogue evaluation with a real LLM (spec 8.9, marker ``llm``). Shares the fakes and the harness of
``tests/agent`` (``agent_helpers.py``, ``agent_eval.py``)."""

from __future__ import annotations

import sys
from pathlib import Path

AGENT_TESTS = Path(__file__).resolve().parents[1] / "agent"
if str(AGENT_TESTS) not in sys.path:
    sys.path.insert(0, str(AGENT_TESTS))
