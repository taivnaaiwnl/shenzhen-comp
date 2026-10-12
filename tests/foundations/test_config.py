"""Configuration: YAML (BOM tolerant), env files (strict, Foundry compatible), LLM settings, paths."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from acb import config
from acb.config import (
    EnvSyntaxError,
    apply_llm_env_defaults,
    llm_settings,
    load_env_file,
    load_yaml,
    out_dir,
    parse_env_file,
    parse_env_text,
)

REPO = Path(__file__).resolve().parents[2]


def test_yaml_reading_tolerates_a_bom(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_bytes(b"\xef\xbb\xbfkey: value\nlist: [1, 2]\n")
    assert load_yaml(p) == {"key": "value", "list": [1, 2]}


def test_repository_yaml_loads():
    nets = load_yaml("config/networks.yaml")
    assert set(nets) == {"anvil", "base_sepolia"}
    procs = config.processes_config()
    assert set(procs) == {"base", "owner", "agent", "signer", "bureau", "autonomy", "sim"}
    assert "AGENT_SIGNER_PRIVATE_KEY" in procs["signer"]
    for role, names in procs.items():
        if role in ("signer", "bureau", "autonomy", "owner", "sim"):
            continue
        assert not any(n.upper().endswith(("_PRIVATE_KEY", "_MNEMONIC")) for n in names), role


def test_env_example_parses_strictly():
    values = parse_env_file(REPO / ".env.example", strict=True)
    assert values["NETWORK"] == "anvil"
    assert values["ACB_MNEMONIC"] == ""
    assert values["ACB_FOUNDRY_BIN"] == "" and values["ACB_OUT_DIR"] == "" and values["LLM_BASE_URL"] == ""
    assert values["LLM_EXTRA_BODY"] == "{}"
    for name in ("SIGNER_TOKEN", "EVENTS_TOKEN", "DEPLOYER_PRIVATE_KEY", "SIM_ATTACKER_PRIVATE_KEY", "LLM_API_KEY"):
        assert values[name] == ""
    for name in ("OWNER_PORT", "AGENT_PORT", "SIGNER_PORT", "BUREAU_PORT", "SIM_PORT", "AUTONOMY_PORT"):
        assert values[name].isdigit()


@pytest.mark.skipif(config.foundry_tool("forge") is None, reason="Foundry forge not installed")
def test_env_example_loads_in_foundry(tmp_path):
    """Foundry auto-loads .env and refuses unquoted values with spaces (spec 13.1)."""
    (tmp_path / "foundry.toml").write_text('[profile.default]\nsrc = "src"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / ".env").write_bytes((REPO / ".env.example").read_bytes())
    proc = subprocess.run(
        [str(config.foundry_tool("forge")), "config"], capture_output=True, text=True, check=False, cwd=tmp_path
    )
    assert proc.returncode == 0
    assert "Failed to load" not in proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "text",
    [
        "NAME=two words\n",
        "NAME=   # a note\n",
        "not a binding\n",
        "1BAD=x\n",
    ],
)
def test_strict_parse_rejects_foundry_breaking_lines(text):
    with pytest.raises(EnvSyntaxError):
        parse_env_text(text, strict=True)


def test_strict_parse_accepts_quoted_and_comments():
    values = parse_env_text("# c\n\nA=\"two words\"\nB=plain  # note\nC=\nD='x y'\n", strict=True)
    assert values == {"A": "two words", "B": "plain", "C": "", "D": "x y"}


def test_load_env_file_does_not_override_and_applies_llm_fallback(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ACB_TEST_A=from-file\nACB_TEST_B=file\nOPENAI_API_KEY=sk-test-123\n", encoding="utf-8")
    monkeypatch.setenv("ACB_TEST_B", "from-env")
    for name in ("ACB_TEST_A", "OPENAI_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    loaded = load_env_file(env)
    import os

    assert os.environ["ACB_TEST_A"] == "from-file" and os.environ["ACB_TEST_B"] == "from-env"
    assert set(loaded) == {"ACB_TEST_A", "OPENAI_API_KEY"}
    assert os.environ["LLM_API_KEY"] == "sk-test-123"
    assert os.environ["LLM_BASE_URL"] == config.OPENAI_BASE_URL
    assert os.environ["LLM_MODEL"] == config.DEFAULT_LLM_MODEL


def test_llm_fallback_rules():
    env = {"OPENAI_API_KEY": "sk-a"}
    apply_llm_env_defaults(env)
    assert env["LLM_API_KEY"] == "sk-a" and env["LLM_MODEL"] == "gpt-5.4-mini"
    deepseek = {"OPENAI_API_KEY": "sk-a", "LLM_BASE_URL": "https://api.deepseek.com"}
    apply_llm_env_defaults(deepseek)
    assert "LLM_API_KEY" not in deepseek, "the OpenAI key never goes to another provider"
    explicit = {"OPENAI_API_KEY": "sk-a", "LLM_API_KEY": "sk-b", "LLM_MODEL": "m"}
    apply_llm_env_defaults(explicit)
    assert explicit["LLM_API_KEY"] == "sk-b" and explicit["LLM_MODEL"] == "m"


def test_llm_settings():
    s = llm_settings({"OPENAI_API_KEY": "sk-secret-value", "LLM_EXTRA_BODY": '{"thinking": {"type": "disabled"}}'})
    assert s.has_key and s.effective_mode == "openai" and s.model == "gpt-5.4-mini"
    assert s.extra_body == {"thinking": {"type": "disabled"}}
    assert "sk-secret-value" not in repr(s)
    none = llm_settings({})
    assert not none.has_key and none.effective_mode == "mock" and none.timeout_s == 20
    assert llm_settings({"LLM_MODE": "mock", "LLM_API_KEY": "k"}).effective_mode == "mock"
    with pytest.raises(ValueError):
        llm_settings({"LLM_MODE": "other"})
    with pytest.raises(ValueError):
        llm_settings({"LLM_EXTRA_BODY": "{bad"})


def test_llm_settings_reads_only_llm_values_from_an_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text('OPENAI_API_KEY=sk-file\nACB_MNEMONIC="a b c"\nLLM_MODEL=custom\n', encoding="utf-8")
    s = llm_settings({}, env_file=env)
    assert s.api_key == "sk-file" and s.model == "custom"


def test_out_dir_and_data_dir(tmp_path):
    p = out_dir("bundles", environ={"ACB_OUT_DIR": str(tmp_path / "o")})
    assert p == tmp_path / "o" / "bundles" and p.is_dir()
    default = out_dir(environ={"ACB_RUN_ID": "r-test", "NETWORK": "anvil"}, create=False)
    assert default == REPO / "runs" / "r-test" / "out" / "anvil"
    assert config.data_dir("anvil", "agent", create=False) == REPO / "data" / "anvil" / "agent"
    with pytest.raises(ValueError):
        config.data_dir("../x")


def test_foundry_bin(tmp_path):
    assert config.foundry_bin({"ACB_FOUNDRY_BIN": str(tmp_path)}) == tmp_path
    assert config.foundry_bin({"USERPROFILE": "C:/Users/x"}) == Path("C:/Users/x/.foundry/bin")
    (tmp_path / "anvil.exe").write_bytes(b"")
    assert config.foundry_tool("anvil", {"ACB_FOUNDRY_BIN": str(tmp_path)}) == tmp_path / "anvil.exe"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com.evil.example/v1",
        "https://evil.example/api.openai.com/v1",
        "https://evil.example/v1?h=api.openai.com",
        "http://api.openai.com/v1",
        "https://user@api.openai.com:8443/v1",
    ],
)
def test_openai_key_goes_only_to_the_openai_host(url):
    env = {"OPENAI_API_KEY": "sk-a", "LLM_BASE_URL": url}
    apply_llm_env_defaults(env)
    assert "LLM_API_KEY" not in env
    assert llm_settings({"OPENAI_API_KEY": "sk-a", "LLM_BASE_URL": url}).api_key in (None, "")


def test_openai_key_fallback_for_the_openai_host():
    for url in ("", "https://api.openai.com/v1", "https://API.openai.com/v1/"):
        env = {"OPENAI_API_KEY": "sk-a", "LLM_BASE_URL": url}
        apply_llm_env_defaults(env)
        assert env["LLM_API_KEY"] == "sk-a", url
