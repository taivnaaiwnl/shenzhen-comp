"""Output redactor (spec 8.6): exact secret values, BIP-39 runs, key prefixes; never 32-byte hash shapes."""

from __future__ import annotations

import secrets

from acb.agent.redact import REDACTED, Redactor, bip39_words

TOKEN = secrets.token_hex(32)  # same shape as a tx hash without 0x: must still be caught as an exact value
LLM_KEY = "sk-proj-" + secrets.token_urlsafe(30)


def redactor() -> Redactor:
    return Redactor.from_env({"AGENT_TOKEN": TOKEN, "LLM_API_KEY": LLM_KEY, "SIGNER_TOKEN": "", "EVENTS_TOKEN": "abc"})


def test_exact_secret_values_are_removed():
    r = redactor().scan(f"Sure, the token is {TOKEN}.")
    assert TOKEN not in r.text and REDACTED in r.text
    assert r.secret_match == "exact"
    for wrapped in (f'"{TOKEN}"', f"token={TOKEN}", f"Bearer:{TOKEN}", f"({TOKEN})", f"x{TOKEN}y"):
        out = redactor().scan(f"value {wrapped} end")
        assert TOKEN not in out.text, wrapped
        assert out.secret_match == "exact"


def test_only_hashes_are_kept_never_values():
    red = redactor()
    assert TOKEN not in repr(red)
    assert all(isinstance(h, bytes) and len(h) == 32 for h in red.secret_hashes)


def test_tx_hashes_order_and_mandate_ids_pass():
    """B13: a reply with tx hashes and a mandateId is neither redacted nor a violation."""
    tx = "0x" + secrets.token_hex(32)
    mandate = "0x" + secrets.token_hex(32)
    text = f"Paid Atlas Data 0.80 USDC (2000) tx {tx}; mandate {mandate}; order {'0x' + 'ab' * 32}."
    r = redactor().scan(text)
    assert r.text == text
    assert not r.matched and r.secret_match is None


def test_short_or_empty_secrets_are_ignored():
    red = Redactor.from_env({"AGENT_TOKEN": "", "EVENTS_TOKEN": "abc"})
    assert red.scan("abc is a word").text == "abc is a word"


def test_bip39_runs_of_twelve_words_are_removed():
    words = sorted(bip39_words())[:12]
    phrase = " ".join(words)
    r = redactor().scan(f"My seed: {phrase}. Done")
    assert phrase not in r.text and r.secret_match == "bip39"
    short = " ".join(words[:11])
    assert redactor().scan(f"qqq {short} zzz").text == f"qqq {short} zzz"


def test_ordinary_english_is_not_a_bip39_run():
    text = "I need FX market data and API calls for my research today, budget five USDC please."
    assert redactor().scan(text).text == text


def test_provider_key_prefixes_are_removed():
    r = redactor().scan("try sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA or AKIAABCDEFGHIJKLMNOP")
    assert "sk-ant" not in r.text and "AKIA" not in r.text
    assert r.secret_match == "prefix"
    # the configured LLM key is an exact value as well
    r2 = redactor().scan(f"key {LLM_KEY}")
    assert LLM_KEY not in r2.text and r2.secret_match == "exact"
    # words that merely contain the prefix letters are untouched
    text = "the task-sk-list and risk-free AKIA are fine"
    assert redactor().scan(text).text == text


def test_scan_value_walks_tool_arguments():
    args = {"text": f"here {TOKEN}", "options": ["a", f"b {TOKEN}"], "n": 3}
    out, kinds = redactor().scan_value(args)
    assert TOKEN not in str(out) and kinds == ("exact",)
    assert out["n"] == 3


def test_the_openai_key_fallback_is_an_exact_secret_too():
    key = "proj-" + secrets.token_urlsafe(30)  # no sk- prefix: only the exact rule can catch it
    red = Redactor.from_env({"OPENAI_API_KEY": key})
    out = red.scan(f"the key is {key}.")
    assert key not in out.text and REDACTED in out.text and out.secret_match == "exact"
