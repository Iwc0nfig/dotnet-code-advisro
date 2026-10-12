import builtins
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
import pytest
from mcp.server import MCPServer
from dotnet_code_advisor.__main__ import main
from dotnet_code_advisor.tools import EXPECTED_TOOLS, FORBIDDEN, list_tool_names
from dotnet_code_advisor.inventory import walk_paths

def test_tool_contract_and_annotations(sample_tools):
    assert set(list_tool_names(sample_tools.mcp)) == EXPECTED_TOOLS
    assert len(EXPECTED_TOOLS) == 15
    assert not any(word in name for name in EXPECTED_TOOLS for word in FORBIDDEN)
    for tool in sample_tools.mcp._tool_manager.list_tools():
        assert tool.annotations.read_only_hint is True
        assert tool.description
        assert tool.parameters["type"] == "object"

def test_shared_namespace_ambiguity_is_company_neutral(sample_tools):
    out = sample_tools("find_symbol", query="OrderRequest", exact=True)
    assert {s.side for s in out.symbols} == {"net48", "net8"}
    assert any("both configured roots" in caveat for caveat in out.caveats)
    assert out.layer == "graph"

def test_not_found_has_measured_scope(sample_tools):
    out = sample_tools("find_symbol", query="NoSuchFictionalSymbol")
    assert out.status == "no_results"
    assert "files" in out.searched_scope and "lines" in out.searched_scope

def test_webforms_controls_handlers_master_and_comments(sample_tools):
    out = sample_tools("get_control_binding", markup_file="Legacy/Website/Default.aspx")
    assert out.status == "ok"
    assert out.codebehind.rel_path.endswith("Default.aspx.cs")
    assert out.master_page.rel_path.endswith("Site.master")
    ids = {c.control_id for c in out.controls}
    assert "SearchButton" in ids and "HiddenComment" not in ids

def test_graph_twins_are_labelled_name_correspondence(sample_tools):
    out = sample_tools("find_migration_twin", symbol="OrderRequest", from_side="net48")
    assert out.status == "ok" and out.twins
    assert out.layer == "graph"
    assert any("Name correspondence only" in x for x in out.caveats)

def test_missing_compiler_is_unavailable_not_absence(sample_tools):
    out = sample_tools("go_to_definition", file="Legacy/Shared/OrderRequest.cs", line=3, col=18)
    assert out.status == "unavailable"
    assert out.definitions == []

def test_stdio_is_default_and_config_does_not_pollute_stdout(sample_config, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(MCPServer, "run", lambda self, *args, **kwargs: seen.append((args, kwargs)))
    assert main(["--config", str(sample_config.source), "--no-roslyn"]) == 0
    assert seen == [((), {})]
    assert capsys.readouterr().out == ""

def test_http_defaults_to_localhost_when_selected(sample_config, monkeypatch):
    seen = []
    monkeypatch.setattr(MCPServer, "run", lambda self, *args, **kwargs: seen.append((args, kwargs)))
    main(["--config", str(sample_config.source), "--no-roslyn", "--transport", "streamable-http"])
    assert seen[0][0] == ("streamable-http",)
    assert seen[0][1]["host"] == "127.0.0.1"

def test_absolute_traversal_and_prefix_sibling_reads_are_denied(sample_config, sample_tools, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private marker")
    sibling = sample_config.paths.workspaces / "LegacyExtra"
    sibling.mkdir(exist_ok=True)
    (sibling / "outside.txt").write_text("private marker")
    for path in (str(outside), "../outside.txt", str(sibling / "outside.txt")):
        assert sample_tools("read_file_range", file=path).status == "no_results"
    assert not sample_tools("search_text", pattern="private marker").hits

def test_symlink_escape_is_denied_for_reads_and_walks(sample_config, sample_tools, tmp_path):
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "leak.cs").write_text("class PrivateOutside {}")
    link = sample_config.paths.net8_root / "escape"
    junction = False
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"Symlink creation unsupported: {exc}")
        # Junctions exercise the same resolved-path boundary on Windows without
        # requiring Developer Mode or the symbolic-link privilege.
        result = subprocess.run([
            "powershell", "-NoProfile", "-Command",
            "New-Item -ItemType Junction -Path $env:DOTNET_ADVISOR_TEST_LINK -Value $env:DOTNET_ADVISOR_TEST_TARGET | Out-Null",
        ], env={**os.environ, "DOTNET_ADVISOR_TEST_LINK": str(link),
                "DOTNET_ADVISOR_TEST_TARGET": str(outside)}, capture_output=True)
        if result.returncode:
            pytest.skip("Neither symlink nor junction creation is supported")
        junction = True
    try:
        assert sample_tools("read_file_range", file=str(link / "leak.cs")).status == "no_results"
        assert link / "leak.cs" not in list(walk_paths(sample_config))
        assert not sample_tools("search_text", pattern="PrivateOutside").hits
    finally:
        if junction:
            os.rmdir(link)  # remove the junction itself, never the target tree
        else:
            link.unlink()

def test_all_query_tools_do_not_write_source(sample_tools, sample_config, monkeypatch):
    originals = {p: p.read_bytes() for p in walk_paths(sample_config)}
    original_builtin, original_io, original_os = builtins.open, io.open, os.open
    def under(path):
        return isinstance(path, (str, Path)) and sample_config.contains_source(path)
    def guarded(original):
        def open_file(path, mode="r", *args, **kwargs):
            if any(x in mode for x in "wax+") and under(path):
                raise AssertionError("query attempted source write")
            return original(path, mode, *args, **kwargs)
        return open_file
    def os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) and under(path):
            raise AssertionError("query attempted source write")
        return original_os(path, flags, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", guarded(original_builtin))
    monkeypatch.setattr(io, "open", guarded(original_io))
    monkeypatch.setattr(os, "open", os_open)
    calls = {
        "find_symbol": {"query": "OrderRequest"},
        "go_to_definition": {"symbol": "OrderRequest"},
        "find_references": {"symbol": "OrderRequest"},
        "find_callers": {"symbol": "Find"},
        "find_implementations": {"symbol": "OrderRequest"},
        "get_type_info": {"file": "Legacy/Shared/OrderRequest.cs", "line": 3, "col": 18},
        "get_dependency_graph": {"level": "type"},
        "find_dead_code": {"scope": "Legacy/Shared/OrderRequest.cs"},
        "read_file_range": {"file": "Legacy/Shared/OrderRequest.cs"},
        "search_text": {"pattern": "OrderRequest"},
        "get_control_binding": {"markup_file": "Legacy/Website/Default.aspx"},
        "get_page_assets": {"markup_file": "Legacy/Website/Default.aspx"},
        "find_migration_twin": {"symbol": "OrderRequest", "from_side": "net48"},
        "find_api_calls": {"file": "Legacy/Website/Default.aspx"},
        "index_status": {},
    }
    assert set(calls) == EXPECTED_TOOLS
    for name, kwargs in calls.items():
        sample_tools(name, **kwargs)
    assert all(p.read_bytes() == content for p, content in originals.items())
