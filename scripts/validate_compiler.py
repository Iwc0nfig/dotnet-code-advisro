"""Build/index only fresh copies of fictional samples, after helper compilation."""
import argparse
import shutil
import tempfile
from pathlib import Path
import tomli_w
from dotnet_code_advisor.config import load_config
from dotnet_code_advisor.build.runner import run as build
from dotnet_code_advisor.index.runner import run as index
from dotnet_code_advisor.tools.common import Ctx
from dotnet_code_advisor.tools import register_all
from mcp.server import MCPServer

def check(base: Path, repo: Path):
    ws = base / "workspaces"
    if ws.exists():
        raise ValueError("Use a fresh work directory; existing workspaces are never overwritten")
    shutil.copytree(repo / "examples/workspaces", ws)
    shutil.copyfile(repo / "NuGet.Config", base / "NuGet.Config")
    paths = {
        "workspaces": str(ws), "net48_root": str(ws / "Legacy"),
        "net8_root": str(ws / "Modern"), "website_root": str(ws / "Legacy/Website"),
        "index_dir": str(base / "index"),
        "net48_solution": str(ws / "Legacy/Legacy.sln"),
        "net8_solution": str(ws / "Modern/Modern.sln"),
        "roslyn_host_net48": str(repo / "roslyn-host/bin/Release/net48/RoslynHost.exe"),
        "roslyn_host_net8": str(repo / "roslyn-host/bin/Release/net8.0/RoslynHost.exe"),
        "markup_codegen": str(repo / "markup-codegen/bin/Release/net48/MarkupCodegen.exe"),
    }
    for key in ("roslyn_host_net48", "roslyn_host_net8", "markup_codegen"):
        if not Path(paths[key]).is_file():
            raise FileNotFoundError(f"Build helper first: {key}")
    cfg_file = base / "config.toml"
    cfg_file.write_text(tomli_w.dumps({"paths": paths}), encoding="utf-8")
    cfg = load_config(cfg_file)
    manifest = build(cfg, progress=print)
    if any(not p.get("build") or p["build"]["exit_code"] != 0 for p in manifest["projects"]):
        raise RuntimeError("Sample build failed; inspect the work-directory manifest")
    index(cfg, progress=print, roslyn=True)
    ctx = Ctx.create(cfg)
    server = MCPServer("compiler-check")
    register_all(server, ctx)
    try:
        state = server._tool_manager.get_tool("index_status").fn()
        if not state.compiler_index.get("available"):
            raise RuntimeError("Compiler index unavailable; inspect helper and build diagnostics")
        # Require both sample projects to produce compiler declarations.
        sides = {r[0] for r in ctx.con.execute("SELECT DISTINCT side FROM rsymbols")}
        if sides != {"net48", "net8"}:
            raise RuntimeError(f"Incomplete compiler coverage: {sides}")
        endpoints = ctx.con.execute("SELECT COUNT(*) FROM rendpoints").fetchone()[0]
        links = ctx.con.execute("SELECT COUNT(*) FROM rapilinks").fetchone()[0]
        if endpoints < 1 or links < 1:
            raise RuntimeError("Sample endpoint or matching HTTP link missing")
        print(f"Compiler sample check passed: {endpoints} endpoint(s), {links} route link(s).")
    finally:
        ctx.pool.stop_all()
        ctx.con.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, help="fresh scratch directory, outside the repository")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if args.work_dir:
        base = args.work_dir.resolve()
        if base.is_relative_to(repo):
            raise ValueError("Work directory must be outside the repository")
        base.mkdir(parents=True, exist_ok=True)
        check(base, repo)
    else:
        with tempfile.TemporaryDirectory(prefix="dotnet-advisor-") as temporary:
            check(Path(temporary), repo)

if __name__ == "__main__":
    main()
