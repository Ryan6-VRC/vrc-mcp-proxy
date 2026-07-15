from vrc_mcp_proxy import canary


def test_baseline_loads_allowlisted_only():
    schemas = canary.load_baseline_schemas()
    assert "execute_code" in schemas
    assert "generate_image" not in schemas  # hidden by allowlist
    assert schemas["execute_code"]["type"] == "object"


def test_matching_schema_is_no_drift():
    schemas = canary.load_baseline_schemas()
    tools = [{"name": "execute_code", "inputSchema": schemas["execute_code"]}]
    logs = []
    drifted = canary.validate_listing(tools, schemas, log=logs.append)
    assert drifted == set()
    assert logs == []


def test_changed_schema_is_drift_and_logs():
    schemas = canary.load_baseline_schemas()
    mutated = dict(schemas["execute_code"])
    mutated = {**mutated, "properties": {"totally": {"type": "string"}}}
    tools = [{"name": "execute_code", "inputSchema": mutated}]
    logs = []
    drifted = canary.validate_listing(tools, schemas, log=logs.append)
    assert drifted == {"execute_code"}
    assert len(logs) == 1
    assert "CANARY-DRIFT" in logs[0]
    assert "execute_code" in logs[0]


def test_absent_tool_is_not_drift():
    # Staged list: allowlisted tools not yet registered must NOT count as drift.
    schemas = canary.load_baseline_schemas()
    logs = []
    drifted = canary.validate_listing([], schemas, log=logs.append)
    assert drifted == set()
    assert logs == []


def test_key_order_does_not_matter():
    a = {"type": "object", "properties": {"x": {"type": "string"}}}
    b = {"properties": {"x": {"type": "string"}}, "type": "object"}
    assert canary.schema_matches(a, b)
