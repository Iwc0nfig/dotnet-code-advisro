"""Opt-in assertions against the disposable sample compiler validation output."""
import os
import pytest
from dotnet_code_advisor.config import load_config
from dotnet_code_advisor.tools.common import Ctx
from dotnet_code_advisor.tools import register_all
from mcp.server import MCPServer

@pytest.mark.compiler
@pytest.mark.skipif(os.environ.get("DOTNET_ADVISOR_ROSLYN") != "1",
                    reason="Set DOTNET_ADVISOR_ROSLYN=1 and DOTNET_ADVISOR_CONFIG after sample compiler validation")
def test_sample_compiler_coverage_http_and_twins():
    cfg = load_config()
    ctx = Ctx.create(cfg)
    ctx.pool.disabled = True
    server = MCPServer("compiler-test")
    register_all(server, ctx)
    try:
        projects = list(ctx.con.execute("SELECT project,clean FROM rprojects"))
        assert len(projects) == 3
        assert all(row["clean"] for row in projects)
        assert ctx.con.execute("SELECT COUNT(*) FROM rapilinks WHERE match='route_exact'").fetchone()[0] == 1
        assert ctx.con.execute("SELECT COUNT(*) FROM rtwins").fetchone()[0] >= 1
        get = server._tool_manager.get_tool("go_to_definition").fn
        out = get(symbol="Demo.Shared.OrderRequest", side="net48")
        assert out.status == "ok" and out.layer == "roslyn-index"
        refs = server._tool_manager.get_tool("find_references").fn(key=out.target.key)
        assert refs.layer == "roslyn-index"
        assert refs.status == "ok"
    finally:
        ctx.pool.stop_all()
        ctx.con.close()
