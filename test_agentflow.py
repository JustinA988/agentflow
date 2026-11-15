import pytest

from agentflow import build_cmd, parse_ref


def test_parse_ref():
    assert parse_ref("octo/cat#42") == ("octo/cat", 42)
    with pytest.raises(ValueError):
        parse_ref("octo/cat")


def test_build_cmd_injects_prompt_as_single_arg():
    cmd = build_cmd("claude", "fix it; rm -rf /")
    assert "fix it; rm -rf /" in cmd and cmd[0] == "claude"
