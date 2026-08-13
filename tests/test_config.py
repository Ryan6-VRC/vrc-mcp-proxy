from vrc_mcp_proxy import config


def test_all_enabled_by_default():
    cfg = config.load_config(env={})
    assert all(cfg.values())
    assert set(cfg) == set(config.BEHAVIORS)


def test_disable_list_parsing():
    cfg = config.load_config(env={"VRC_MCP_PROXY_DISABLE": "canary, timeout_notes"})
    assert cfg["canary"] is False
    assert cfg["timeout_notes"] is False
    assert cfg["allowlist"] is True


def test_unknown_names_ignored():
    cfg = config.load_config(env={"VRC_MCP_PROXY_DISABLE": "not_a_behavior"})
    assert all(cfg.values())


def test_unknown_names_are_warned_not_swallowed():
    # A behavior rename turns an operator's existing setting into a no-op that reads
    # exactly like a working one — and the behavior they meant to disable comes back on.
    lines = []
    cfg = config.load_config(
        env={"VRC_MCP_PROXY_DISABLE": "manage_asset_truth_correction,canary"},
        log=lines.append)
    assert cfg["canary"] is False          # the valid half still applies
    assert len(lines) == 1
    assert "manage_asset_truth_correction" in lines[0]
    assert "canary" not in lines[0].split(":")[-1]  # only the unknown name is named


def test_no_warning_when_every_name_is_valid():
    lines = []
    config.load_config(env={"VRC_MCP_PROXY_DISABLE": "canary timeout_notes"},
                       log=lines.append)
    assert lines == []
