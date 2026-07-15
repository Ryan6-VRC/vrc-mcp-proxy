from vrc_mcp_proxy import config


def test_all_enabled_by_default():
    cfg = config.load_config(env={})
    assert all(cfg.values())
    assert set(cfg) == set(config.BEHAVIORS)


def test_disable_list_parsing():
    cfg = config.load_config(env={"VRC_MCP_PROXY_DISABLE": "canary, read_console_strip"})
    assert cfg["canary"] is False
    assert cfg["read_console_strip"] is False
    assert cfg["allowlist"] is True


def test_unknown_names_ignored():
    cfg = config.load_config(env={"VRC_MCP_PROXY_DISABLE": "not_a_behavior"})
    assert all(cfg.values())
