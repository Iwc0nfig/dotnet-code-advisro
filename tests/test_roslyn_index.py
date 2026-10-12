"""The compiler-index layer (tools/rindex.py, index/roslyn_index.py).

These run against a small synthetic database, so they need neither the
workspace nor a built index: they pin the query semantics -- position lookup,
name lookup, dispatch vs direct references, transitive implementations, cycle
marking, ambiguity reporting -- and the loader's column contract with the C#
host.  Optional compiler checks use the fictional sample projects.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from dotnet_code_advisor import db as dbm
from dotnet_code_advisor.index import roslyn_index as ri
from dotnet_code_advisor.tools import rindex

P1 = "Legacy/Modern/Modern.csproj"
P2 = "Legacy/App#website"
IFACE = f"{P1}|T:Modern.IRepo"
IFACE_GET = f"{P1}|M:Modern.IRepo.Get(System.Int32)"
BASE = f"{P1}|T:Modern.RepoBase"
REPO = f"{P1}|T:Modern.Repo"
REPO_GET = f"{P1}|M:Modern.Repo.Get(System.Int32)"
SUB = f"{P2}|T:App.SpecialRepo"
PAGE_LOAD = f"{P2}|M:ui_orders.Page_Load(System.Object,System.EventArgs)"
HELPER = f"{P2}|M:ui_orders.Helper"


def _sym(key, kind, name, fqn, path, line, col, side="net48", gen=0, container=None):
    project = key.split("|", 1)[0]
    return (key, key.split("|", 1)[1], project, side, kind, name, container, fqn, fqn,
            "public", "", path, line, col, line + 5, gen, line - 1, None, None)


@pytest.fixture()
def ctx(tmp_path):
    con = sqlite3.connect(tmp_path / "t.db")
    con.row_factory = sqlite3.Row
    con.executescript(dbm.SCHEMA_ROSLYN)
    con.executemany(
        "INSERT INTO rsymbols(key,doc_id,project,side,kind,name,container_key,fqn,signature,"
        "accessibility,modifiers,path,line,col,end_line,is_generated,start_line,type,json_name) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            _sym(IFACE, "interface", "IRepo", "Modern.IRepo", "Legacy/Modern/IRepo.cs", 3, 18),
            _sym(IFACE_GET, "method", "Get", "Modern.IRepo.Get", "Legacy/Modern/IRepo.cs", 5, 16),
            _sym(BASE, "class", "RepoBase", "Modern.RepoBase", "Legacy/Modern/Repo.cs", 3, 14),
            _sym(REPO, "class", "Repo", "Modern.Repo", "Legacy/Modern/Repo.cs", 10, 14),
            _sym(REPO_GET, "method", "Get", "Modern.Repo.Get", "Legacy/Modern/Repo.cs", 12, 23),
            _sym(SUB, "class", "SpecialRepo", "App.SpecialRepo", "Legacy/App/App_Code/Special.cs", 1, 14),
            _sym(PAGE_LOAD, "method", "Page_Load", "ui_orders.Page_Load", "Legacy/App/ui/orders.aspx.cs", 20, 20),
            _sym(HELPER, "method", "Helper", "ui_orders.Helper", "Legacy/App/ui/orders.aspx.cs", 40, 20),
            # same simple name in a second project: a different symbol to the compiler
            _sym("Legacy/Tools/X.csproj|M:Tools.Repo.Get", "method", "Get", "Tools.Repo.Get",
                 "Legacy/Tools/Repo.cs", 7, 21),
        ],
    )
    con.executemany(
        "INSERT INTO rrefs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # direct call to Repo.Get from code-behind
            ("Legacy/App/ui/orders.aspx.cs", 25, 30, REPO_GET, "M:Modern.Repo.Get(System.Int32)",
             1, "Modern", "call", PAGE_LOAD, None, P2, "net48", 0),
            # call through the interface: a dispatch site for Repo.Get
            ("Legacy/App/ui/orders.aspx.cs", 26, 14, IFACE_GET, "M:Modern.IRepo.Get(System.Int32)",
             1, "Modern", "call", PAGE_LOAD, None, P2, "net48", 0),
            # a <%= %> nugget in markup, resolved in ASP.NET's generated code
            ("Legacy/App/ui/orders.aspx", 53, 44, REPO_GET, "M:Modern.Repo.Get(System.Int32)",
             1, "Modern", "call", None, None, P2, "net48", 1),
            # overload resolution failed: candidate only
            ("Legacy/App/ui/orders.aspx.cs", 30, 9, REPO_GET, "M:Modern.Repo.Get(System.Int32)",
             1, "Modern", "call", PAGE_LOAD, "OverloadResolutionFailure", P2, "net48", 0),
            # mutual recursion Page_Load <-> Helper
            ("Legacy/App/ui/orders.aspx.cs", 22, 9, HELPER, "M:ui_orders.Helper",
             1, "App", "call", PAGE_LOAD, None, P2, "net48", 0),
            ("Legacy/App/ui/orders.aspx.cs", 42, 9, PAGE_LOAD, "M:ui_orders.Page_Load(System.Object,System.EventArgs)",
             1, "App", "call", HELPER, None, P2, "net48", 0),
        ],
    )
    con.executemany(
        "INSERT INTO redges VALUES (?,?,?,?)",
        [
            (REPO, "implements", IFACE, "net48"),
            (REPO_GET, "implements_member", IFACE_GET, "net48"),
            (REPO, "inherits", BASE, "net48"),
            (SUB, "inherits", REPO, "net48"),
        ],
    )
    con.executemany(
        "INSERT INTO rprojects(project,side,name,assembly,documents,generated_documents,"
        "metadata_refs,project_refs,errors,binding_errors,warnings,clean,failure,top_errors,"
        "load_failures,is_website) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (P1, "net48", "Modern", "Modern", 10, 0, 50, 0, 0, 0, 0, 1, None, "", "[]", 0),
            (P2, "net48", "App.Website", "App.Website", 20, 5, 137, 7, 3, 3, 0, 0, None, "CS0246:3", "[]", 1),
        ],
    )
    con.execute(
        "INSERT INTO rendpoints(project,side,verb,route,prefix_complete,handler_key,handler_kind,style,"
        "path,line,col) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (P1, "net8", "POST", "/api/v{version}/repo/get", 1, REPO_GET, "method", "minimal",
         "Modern/Api/Endpoints.cs", 9, 9))
    con.execute(
        "INSERT INTO rhttpcalls(project,side,path,line,col,verb,url,enclosing_key,callee_key,is_generated,"
        "url_line,url_col,url_end_line,url_end_col) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (P2, "net48", "Legacy/App/ui/orders.aspx.cs", 44, 13, "POST", "{}/api/v{}/repo/get", HELPER,
         None, 0, 44, 60, 44, 90))
    con.execute("INSERT INTO rapilinks VALUES (1, 1, 'route_exact')")
    con.executescript(dbm.SCHEMA_ROSLYN_INDEXES)
    cfg = SimpleNamespace(abs_of=lambda rel: f"C:/ws/{rel}")
    return SimpleNamespace(con=con, cfg=cfg, rel=lambda p: str(p),
                           resolve_path=lambda f: Path(f))


def test_position_on_a_declaration_name(ctx):
    keys, how = rindex.keys_at(ctx, "Legacy/Modern/Repo.cs", 12, 24)
    assert (keys, how) == ([REPO_GET], "declaration")


def test_position_on_an_attribute_line_means_the_declaration(ctx):
    # Repo.Get is declared on line 12 with its header (an attribute) on line 11.
    keys, how = rindex.keys_at(ctx, "Legacy/Modern/Repo.cs", 11, 5)
    assert (keys, how) == ([REPO_GET], "declaration")


def test_position_in_a_url_string_resolves_to_the_endpoint_handler(ctx):
    keys, how = rindex.keys_at(ctx, "Legacy/App/ui/orders.aspx.cs", 44, 70)
    assert (keys, how) == ([REPO_GET], "http_route")
    # outside the URL string on the same line: not an http_route answer
    _, how = rindex.keys_at(ctx, "Legacy/App/ui/orders.aspx.cs", 44, 20)
    assert how != "http_route"


def test_callers_cross_the_http_hop_and_say_so(ctx):
    tree = rindex.caller_tree(ctx, REPO_GET, depth=2, limit=10)
    via_http = [n for n in tree.callers if n.http]
    assert [n.symbol.key for n in via_http] == [HELPER]
    assert "route_exact" in via_http[0].http and "/api/v{version}/repo/get" in via_http[0].http
    # direct callers are still there, unmarked
    assert any(n.symbol.key == PAGE_LOAD and n.http is None for n in tree.callers)


def test_position_on_a_use_resolves_to_its_target(ctx):
    # col 30..32 is "Get" in the code-behind call
    keys, how = rindex.keys_at(ctx, "Legacy/App/ui/orders.aspx.cs", 25, 31)
    assert (keys, how) == ([REPO_GET], "reference")


def test_position_in_markup_resolves_through_generated_code(ctx):
    keys, how = rindex.keys_at(ctx, "Legacy/App/ui/orders.aspx", 53, 44)
    assert (keys, how) == ([REPO_GET], "reference")


def test_candidate_only_binding_is_not_a_position_match(ctx):
    keys, _ = rindex.keys_at(ctx, "Legacy/App/ui/orders.aspx.cs", 30, 10)
    assert keys == []


def test_same_simple_name_in_two_projects_is_ambiguous(ctx):
    keys = rindex.keys_by_name(ctx, "Get")
    assert len(keys) == 3  # IRepo.Get, Repo.Get, Tools.Repo.Get
    t = rindex.Target(keys, "name", "Get")
    syms, caveat = rindex.ambiguity(ctx, t)
    assert len(syms) == 3
    assert "3 distinct symbols" in caveat and "key=" in caveat and "Ask the user" in caveat


def test_qualified_name_narrows_to_one(ctx):
    assert rindex.keys_by_name(ctx, "Modern.Repo.Get") == [REPO_GET]
    # '_' must not act as a LIKE wildcard
    assert rindex.keys_by_name(ctx, "ui_orders.Helper") == [HELPER]
    assert rindex.keys_by_name(ctx, "uiXorders.Helper") == []


def test_exact_candidate_and_dispatch_are_separate(ctx):
    exact = rindex.refs(ctx, [REPO_GET], 100)
    assert [(r["path"], r["line"]) for r in exact] == [
        ("Legacy/App/ui/orders.aspx", 53),
        ("Legacy/App/ui/orders.aspx.cs", 25),
    ]
    cand = rindex.refs(ctx, [REPO_GET], 100, exact=False)
    assert [r["line"] for r in cand] == [30]
    assert rindex.dispatch_keys(ctx, [REPO_GET]) == [IFACE_GET]
    assert [r["line"] for r in rindex.refs(ctx, [IFACE_GET], 100)] == [26]


def test_markup_references_can_be_excluded(ctx):
    rows = rindex.refs(ctx, [REPO_GET], 100, include_generated=False)
    assert [r["line"] for r in rows] == [25]


def test_implementations_and_derived_are_transitive(ctx):
    impl, derived = rindex.implementations(ctx, [IFACE], 100)
    assert impl == [REPO]
    impl, derived = rindex.implementations(ctx, [BASE], 100)
    assert derived == [REPO, SUB]


def test_caller_tree_marks_cycles_instead_of_looping(ctx):
    tree = rindex.caller_tree(ctx, HELPER, depth=5, limit=10)
    assert tree.symbol.key == HELPER
    (page,) = tree.callers
    assert page.symbol.key == PAGE_LOAD
    (back,) = page.callers
    assert back.symbol.key == HELPER and back.cycle


def test_compilation_state_is_reported_for_unclean_projects(ctx):
    comp = rindex.compilation(ctx, {P1, P2})
    assert [(p.project, p.clean) for p in comp] == [(P2, False), (P1, True)] or \
           sorted((p.project, p.clean) for p in comp) == sorted([(P1, True), (P2, False)])
    cav = rindex.compilation_caveats(comp)
    assert len(cav) == 1 and P2 in cav[0] and "CS0246:3" in cav[0]


# ------------------------------------------------------------------ loader contract

def _write_side(d: Path, done_cols: dict, paths: list[str]) -> None:
    d.mkdir(parents=True)
    (d / "done.json").write_text(json.dumps({"columns": done_cols, "fallback_keys": 0}))
    (d / "projects.jsonl").write_text(json.dumps(
        [P1, "Modern", "Modern", 1, 0, 1, 0, 0, 0, 0, 1, None, "", "[]", 0]) + "\n")
    (d / "symbols.jsonl").write_text("".join(json.dumps(
        [REPO, "T:Modern.Repo", P1, "class", "Repo", "ns:Modern", "Modern.Repo",
         "Modern.Repo", "public", "", p, 10, 14, 20, 0, 9, None, None]) + "\n" for p in paths))
    (d / "refs.jsonl").write_text(json.dumps(
        [paths[0], 3, 5, REPO, "T:Modern.Repo", 1, "Modern", "type_use", None, None, P1, 0]) + "\n")
    for f in ("edges", "files", "diagnostics", "substitutions", "load_failures",
              "endpoints", "http_calls"):
        (d / f"{f}.jsonl").write_text("")


def _loader_env(tmp_path):
    con = sqlite3.connect(tmp_path / "l.db")
    con.row_factory = sqlite3.Row
    con.executescript(dbm.SCHEMA_CORE)
    con.executescript(dbm.SCHEMA_ROSLYN)
    con.execute("INSERT INTO files(rel_path, side, ext, lang, size_bytes, mtime_ns, "
                "content_hash, line_count, indexed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("Legacy/Modern/Repo.cs", "net48", ".cs", "csharp", 1, 1, "h", 1, 0))
    cfg = SimpleNamespace(paths=SimpleNamespace(build_dir=tmp_path / "build"))
    return con, cfg


def test_loader_rejects_a_column_mismatch(tmp_path):
    con, cfg = _loader_env(tmp_path)
    cols = {"symbols": ",".join(ri.SYMBOL_COLS), "refs": "path,line", "edges": ",".join(ri.EDGE_COLS),
            "files": ",".join(ri.FILE_COLS), "endpoints": ",".join(ri.ENDPOINT_COLS),
            "http_calls": ",".join(ri.CALL_COLS)}
    _write_side(ri.side_dir(cfg, "net48"), cols, ["Legacy/Modern/Repo.cs"])
    with pytest.raises(ri.RoslynIndexError, match="refs.jsonl columns"):
        ri.load(cfg, con, {"projects": []}, "net48", lambda m: None)


def test_loader_canonicalises_path_case_to_the_graph(tmp_path):
    con, cfg = _loader_env(tmp_path)
    cols = {"symbols": ",".join(ri.SYMBOL_COLS), "refs": ",".join(ri.REF_COLS),
            "edges": ",".join(ri.EDGE_COLS), "files": ",".join(ri.FILE_COLS),
            "endpoints": ",".join(ri.ENDPOINT_COLS), "http_calls": ",".join(ri.CALL_COLS)}
    # MSBuild spelled the folder LEGACY; the graph has the on-disk case.
    _write_side(ri.side_dir(cfg, "net48"), cols, ["LEGACY/Modern/repo.cs"])
    ri.load(cfg, con, {"projects": [{"rel": P1, "build": {"exit_code": 0, "errors": []}}]},
            "net48", lambda m: None)
    assert con.execute("SELECT path FROM rsymbols").fetchone()[0] == "Legacy/Modern/Repo.cs"
    assert con.execute("SELECT path FROM rrefs").fetchone()[0] == "Legacy/Modern/Repo.cs"
    assert tuple(con.execute("SELECT build_exit, side FROM rprojects").fetchone()) == (0, "net48")
