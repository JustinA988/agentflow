import pytest

from agentflow import BUILTIN_AGENTS, build_cmd, detect_test_cmd, load_agents, parse_ref


def test_parse_ref():
    assert parse_ref("octo/cat#42") == ("octo/cat", 42)
    with pytest.raises(ValueError):
        parse_ref("octo/cat")


def test_build_cmd_injects_prompt_as_single_arg():
    cmd = build_cmd(BUILTIN_AGENTS["claude"], "fix it; rm -rf /")
    assert "fix it; rm -rf /" in cmd and cmd[0] == "claude"


def test_load_agents_from_config(tmp_path):
    cfg = tmp_path / "a.toml"
    cfg.write_text('[agents.local]\ncmd = ["claude", "-p", "{prompt}"]\nenv = { ANTHROPIC_BASE_URL = "http://localhost:11434" }\n')
    agents = load_agents(cfg)
    assert "claude" in agents and agents["local"]["env"]["ANTHROPIC_BASE_URL"].endswith("11434")
    cfg.write_text('[agents.bad]\ncmd = ["x"]\n')
    with pytest.raises(ValueError):
        load_agents(cfg)
    assert load_agents(tmp_path / "missing.toml") == BUILTIN_AGENTS


def test_detect_test_cmd(tmp_path):
    assert detect_test_cmd(tmp_path) is None
    (tmp_path / "test_x.py").write_text("")
    assert detect_test_cmd(tmp_path) == "python -m pytest"
    (tmp_path / "package.json").write_text("{}")
    assert detect_test_cmd(tmp_path) == "npm test"
