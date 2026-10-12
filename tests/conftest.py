"""All shared fixtures use newly authored samples in temporary workspaces."""
from pathlib import Path
import shutil
import json
import pytest
import tomli_w
from mcp.server import MCPServer
from dotnet_code_advisor.config import load_config
from dotnet_code_advisor.index.runner import run
from dotnet_code_advisor.tools import register_all
from dotnet_code_advisor.tools.common import Ctx

@pytest.fixture(scope="session")
def sample_config(tmp_path_factory):
    base = tmp_path_factory.mktemp("fictional")
    samples = Path(__file__).resolve().parents[1] / "examples/workspaces"
    shutil.copytree(samples, base / "workspaces")
    config = base / "config.toml"
    config.write_text(tomli_w.dumps({"paths": {
        "workspaces": "workspaces", "net48_root": "workspaces/Legacy",
        "net8_root": "workspaces/Modern", "website_root": "workspaces/Legacy/Website",
        "net48_solution": "workspaces/Legacy/Legacy.sln",
        "net8_solution": "workspaces/Modern/Modern.sln", "index_dir": "index",
    }}), encoding="utf-8")
    cfg = load_config(config)
    cfg.ensure_index_dirs()
    # Small unit-test type table, not a substitute for production System.Web reflection.
    cfg.paths.aspnet_controls_json.write_text(json.dumps({"webcontrols": {
        name: "System.Web.UI.WebControls." + name
        for name in ("Content", "ContentPlaceHolder", "Button", "TextBox", "Label")
    }}), encoding="utf-8")
    run(cfg, roslyn=False)
    return cfg

@pytest.fixture(scope="session")
def sample_ctx(sample_config):
    ctx = Ctx.create(sample_config)
    ctx.pool.disabled = True
    yield ctx
    ctx.pool.stop_all()
    ctx.con.close()

@pytest.fixture(scope="session")
def sample_tools(sample_ctx):
    server = MCPServer("fictional-test")
    register_all(server, sample_ctx)
    def call(name, **kwargs):
        return server._tool_manager.get_tool(name).fn(**kwargs)
    call.mcp = server
    return call
