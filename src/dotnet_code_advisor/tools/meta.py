"""index_status and find_migration_twin.

``index_status`` exists because an agent that cannot tell "not found" from "not
indexed" will fill the gap from training data -- the exact failure this whole
server is built to prevent.
"""

from __future__ import annotations

import platform

import ast
from typing import Literal, Optional

from mcp.types import ToolAnnotations

from ..models import (
    FindMigrationTwinOut,
    IndexStatusOut,
    MigrationTwin,
    RoslynHostStatus,
    SymbolRef,
)
from .. import db as dbm
from . import common as c
from .common import Ctx

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

TWIN_CAVEAT = (
    "Name correspondence only. Neither Roslyn nor the graph can prove these are "
    "functionally equivalent."
)


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(
        name="find_migration_twin",
        annotations=RO,
        description=(
            "Given a symbol on one side of the 4.8 -> .NET 8 migration, find same-named "
            "or similarly-named declarations on the other side. This is a NAME-BASED "
            "correspondence, not proof that the two are equivalent: read both before "
            "drawing any conclusion. Every result carries match_kind so you can state "
            "how strong the correspondence is."
        ),
    )
    def find_migration_twin(
        symbol: Optional[str] = None,
        from_side: Optional[Literal["net48", "net8"]] = None,
        max: int = 20,
        key: Optional[str] = None,
    ) -> FindMigrationTwinOut:
        limit = min(int(max), 100)
        compiler = _compiler_twins(ctx, symbol, key, from_side, limit)
        if compiler is not None:
            return compiler
        if not symbol:
            return FindMigrationTwinOut(
                status="no_results", layer="graph", searched_scope=ctx.graph_scope(),
                caveats=["Provide 'symbol' (or 'key' of a type in the compiler index)."])
        simple = symbol.rsplit(".", 1)[-1]

        params: list = [symbol, simple]
        where = "(s.fqn = ? OR s.name = ?)"
        if from_side:
            where += " AND s.side = ?"
            params.append(from_side)
        src_rows = ctx.con.execute(
            f"{c.SYMBOL_SELECT} WHERE {where} ORDER BY s.is_generated LIMIT 20", params
        ).fetchall()

        if not src_rows:
            return FindMigrationTwinOut(
                status="no_results",
                layer="graph",
                searched_scope=ctx.graph_scope(),
                query=symbol,
                from_side=from_side,
                caveats=[
                    f"No declaration named '{symbol}' is indexed on "
                    f"{from_side or 'either side'}.",
                    TWIN_CAVEAT,
                ],
            )

        src = [c.symbol_from_row(ctx, r, with_snippet=False) for r in src_rows]
        origin = from_side or src[0].side
        other = "net8" if origin == "net48" else "net48"

        rows = ctx.con.execute(
            f"{c.SYMBOL_SELECT} WHERE s.side = ? AND s.name = ? "
            "ORDER BY s.is_generated, s.symbol_id LIMIT ?",
            (other, simple, limit * 3),
        ).fetchall()

        src_fqns = {r["fqn"] for r in src_rows if r["fqn"]}
        src_containers = {r["container"] for r in src_rows if r["container"]}
        src_kinds = {r["kind"] for r in src_rows}

        twins: list[MigrationTwin] = []
        for r in rows:
            if r["fqn"] and r["fqn"] in src_fqns:
                kind = "same_fqn"
            elif r["container"] and r["container"] in src_containers:
                kind = "same_container_and_name"
            elif r["kind"] not in src_kinds:
                kind = "same_name_different_kind"
            else:
                kind = "same_name_only"
            twins.append(
                MigrationTwin(symbol=c.symbol_from_row(ctx, r, with_snippet=False), match_kind=kind)
            )

        rank = {
            "same_fqn": 0, "same_container_and_name": 1,
            "same_name_only": 2, "same_name_different_kind": 3,
        }
        twins.sort(key=lambda t: rank[t.match_kind])

        return FindMigrationTwinOut(
            status="ok" if twins else "no_results",
            layer="graph",
            searched_scope=(
                f"{ctx.graph_scope()}; searched side '{other}' for declarations named "
                f"'{simple}'"
            ),
            query=symbol,
            from_side=origin,  # type: ignore[arg-type]
            source_symbols=src,
            twins=twins[:limit],
            truncated=len(twins) > limit,
            total_available=len(twins),
            caveats=[TWIN_CAVEAT],
        )

    @mcp.tool(
        name="index_status",
        annotations=RO,
        description=(
            "What is indexed, what is not, and why. Call this whenever a result is "
            "surprising or empty: it distinguishes 'this does not exist' from 'this was "
            "never indexed'. Quote known_limitations rather than speculating about a gap."
        ),
    )
    def index_status(project: Optional[str] = None) -> IndexStatusOut:
        m = dbm.all_meta(ctx.con)

        def count(table: str) -> int:
            try:
                return ctx.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                return 0

        skipped: dict[str, int] = {}
        for r in ctx.con.execute(
            "SELECT skip_reason, COUNT(*) FROM files WHERE indexed=0 GROUP BY 1"
        ):
            skipped[r[0] or "unknown"] = r[1]

        by_res: dict[str, int] = {
            r[0]: r[1]
            for r in ctx.con.execute("SELECT resolution, COUNT(*) FROM refs GROUP BY 1")
        }

        partial = [
            r[0]
            for r in ctx.con.execute(
                "SELECT rel_path FROM files WHERE parse_status != 'ok' ORDER BY rel_path LIMIT 60"
            )
        ]
        partial_total = ctx.con.execute(
            "SELECT COUNT(*) FROM files WHERE parse_status != 'ok'"
        ).fetchone()[0]

        roslyn: dict[str, RoslynHostStatus] = {}
        for side, st in ctx.pool.status().items():
            roslyn[side] = RoslynHostStatus(
                side=side, state=st.state,
                projects_loaded=st.projects_loaded, projects_total=st.projects_total,
                website_documents=st.website_documents, elapsed_ms=st.elapsed_ms,
                failure_count=st.failure_count, failures=st.failures[:20],
                solution=st.solution, error=st.error,
            )

        files_indexed = ctx.con.execute(
            "SELECT COUNT(*) FROM files WHERE indexed=1"
        ).fetchone()[0]
        lines_indexed = ctx.con.execute(
            "SELECT COALESCE(SUM(line_count),0) FROM files WHERE indexed=1"
        ).fetchone()[0]

        built_root = m.get("workspaces_root", "")
        configured_root = ctx.cfg.paths.workspaces.as_posix()
        deployment = {
            "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "python": platform.python_version(),
            "config": ctx.cfg.source.as_posix(),
            "workspaces_root_configured": configured_root,
            "workspaces_root_at_index_time": built_root or "(not recorded)",
            "index_built_on": m.get("built_on", "(not recorded)"),
        }

        caveats = []
        if built_root and built_root != configured_root:
            # The index stores workspace-relative paths, so it travels between
            # machines -- but only to a tree with the same shape.  Say so rather
            # than quietly resolving every citation against the wrong root.
            caveats.append(
                f"This index was built against {built_root} and is being served with "
                f"paths.workspaces = {configured_root}. That is expected when the index "
                "was produced on another machine; it is a problem if the two trees are "
                "not the same checkout, because every file path is reconstructed from "
                "the configured root."
            )
        down = [s for s, r in roslyn.items() if r.state not in ("ready",)]
        if down:
            caveats.append(
                "Roslyn host(s) not ready: " + ", ".join(
                    f"{s} ({roslyn[s].state})" for s in down
                )
                + ". Compiler-exact tools will return status 'unavailable' for those "
                "sides; the structural graph still answers, but only as a name-based match."
            )
        if files_indexed == 0:
            caveats.append("The index is empty. Run scripts/reindex.py.")
        if partial_total:
            caveats.append(
                f"{partial_total} file(s) parsed only partially; their symbols are "
                "incomplete in the structural graph. The first 60 are listed in "
                "parse_errors."
            )

        compiler, limitations, projects, diags = _compiler_index(ctx, m, project)
        if not compiler.get("available"):
            caveats.append("No compiler index: " + compiler.get("state", "not built")
                           + ". Compiler-exact answers need a live Roslyn host.")

        return IndexStatusOut(
            status="ok",
            layer="mixed",
            searched_scope=ctx.graph_scope(),
            built_at=m.get("built_at", "never"),
            elapsed_s=m.get("elapsed_s", ""),
            files_indexed=files_indexed,
            files_skipped=skipped,
            lines_indexed=lines_indexed,
            symbols=count("symbols"),
            refs=count("refs"),
            edges=count("edges"),
            controls=count("controls"),
            pages=count("pages"),
            refs_by_resolution=by_res,
            parse_errors=partial,
            parse_error_count=partial_total,
            fts5=dbm.fts_enabled(ctx.con),
            designer_files=_int(m.get("designer_files")),
            designer_fields=_int(m.get("designer_fields")),
            designer_collisions=_int(m.get("designer_collisions")),
            designer_template_fields=_int(m.get("designer_template_fields")),
            roslyn=roslyn,
            deployment=deployment,
            case_resolved_count=int(m.get("case_resolved_count", 0) or 0),
            not_indexed=c.NOT_INDEXED,
            known_limitations=c.KNOWN_LIMITATIONS + limitations,
            compiler_index=compiler,
            projects=projects,
            project_diagnostics=diags,
            caveats=caveats,
        )


def _compiler_twins(ctx: Ctx, symbol, key, from_side, limit) -> Optional[FindMigrationTwinOut]:
    """Type twins from the compiler index (index/twins.py), or None to fall back to names."""
    import json as _json
    from . import rindex
    from ..models import TwinShape

    if not rindex.available(ctx):
        return None
    try:
        ctx.con.execute("SELECT 1 FROM rtwins LIMIT 1")
    except Exception:
        return None
    t = rindex.resolve(ctx, key=key, symbol=symbol, side=from_side or "both")
    if t is None:
        return None
    keys = [k for k in t.keys if "|T:" in k]
    if not keys:
        return None  # members, not types: the name-based answer applies
    twins: list[MigrationTwin] = []
    sources: list[SymbolRef] = []
    for k in keys[:10]:
        src = rindex.symbol(ctx, k, with_snippet=False)
        if src is None:
            continue
        sources.append(src)
        col_self, col_other = ("legacy_key", "new_key") if src.side == "net48" else ("new_key", "legacy_key")
        for r in ctx.con.execute(
            f"SELECT * FROM rtwins WHERE {col_self} = ? ORDER BY "
            "(evidence LIKE '%api_contract%') DESC, identical DESC, score DESC, " + col_other, (k,)
        ):
            other = rindex.symbol(ctx, r[col_other], with_snippet=False)
            if other is None:
                continue
            legacy_side = src.side == "net48"
            only_l, only_n = _json.loads(r["only_legacy"]), _json.loads(r["only_new"])
            diffs = _json.loads(r["type_differences"])
            nested = _json.loads(r["nested"])
            if not legacy_side:
                diffs = [_swap(d) for d in diffs]
                nested = [_swap(d, " ~ ") for d in nested]
            ev = r["evidence"].split(",")
            kind = ("api_contract" if "api_contract" in ev
                    else "nested_contract" if "nested_contract" in ev
                    else "same_fqn" if other.fqn == src.fqn else "same_name_only")
            twins.append(MigrationTwin(
                symbol=other, match_kind=kind, evidence=ev, routes=_json.loads(r["routes"]),
                shape=TwinShape(score=r["score"], identical=bool(r["identical"]), common=r["common"],
                                only_in_source=only_l if legacy_side else only_n,
                                only_in_twin=only_n if legacy_side else only_l,
                                type_differences=diffs, nested=nested)))
    if not sources:
        return None
    caveats = [TWIN_CAVEAT,
               "Compiler-index twins: 'api_contract' means the two types are the request body "
               "at the two ends of one HTTP call whose URL matches the endpoint's route "
               "(route_exact) -- strong evidence they are the same wire contract; "
               "'nested_contract' means they are member types inside such a pair. 'same_name' "
               "alone is a name correspondence. 'shape' compares public properties/fields by "
               "name, case-insensitively, as JSON binding does."]
    if len(sources) > 1:
        caveats.append(f"'{t.query}' names {len(sources)} types; twins are listed for each "
                       "(see source_symbols). Pass 'key' for one.")
    return FindMigrationTwinOut(
        status="ok" if twins else "no_results",
        layer="roslyn-index",
        searched_scope=rindex.scope(ctx) + "; twins by api_contract and same_name evidence",
        query=t.query, from_side=sources[0].side, source_symbols=sources,
        twins=twins[:limit], truncated=len(twins) > limit, total_available=len(twins),
        caveats=caveats if twins else caveats + [
            "No twin with compiler evidence. The type may have been renamed and never sent "
            "over a linked call; try find_symbol on the other side."],
    )


def _swap(diff: str, sep: str = " vs ") -> str:
    """'Name: a vs b' -> 'Name: b vs a' (diffs are stored legacy-first)."""
    name, _, rest = diff.partition(": ")
    a, _, b = rest.partition(sep)
    return f"{name}: {b}{sep}{a}"


def _compiler_index(ctx: Ctx, m: dict, project: Optional[str]):
    """Summary of the persisted compiler index, plus the limitation text it implies."""
    from . import rindex
    from ..models import ProjectCompilation

    state = m.get("roslyn_index", "not built")
    if not rindex.available(ctx):
        return {"available": False, "state": state}, [c.LIVE_ROSLYN_ONLY_LIMITATION], [], []

    con = ctx.con
    sides = {}
    for r in con.execute(
        "SELECT side, COUNT(*) n, SUM(clean) clean, SUM(is_website) sites, SUM(binding_errors) be, "
        "SUM(CASE WHEN build_exit = 0 THEN 1 ELSE 0 END) built FROM rprojects GROUP BY side"
    ):
        sides[r["side"]] = {"projects": r["n"], "clean": r["clean"], "websites": r["sites"],
                            "binding_errors": r["be"], "built_ok": r["built"]}
    for r in con.execute("SELECT side, COUNT(*) FROM rsymbols GROUP BY side"):
        sides.setdefault(r[0], {})["symbols"] = r[1]
    for r in con.execute("SELECT side, COUNT(*) FROM rrefs GROUP BY side"):
        sides.setdefault(r[0], {})["references"] = r[1]
    unclean = [
        {"project": r["project"], "side": r["side"], "binding_errors": r["binding_errors"],
         "top_errors": r["top_errors"], "build_exit": r["build_exit"], "failure": r["failure"]}
        for r in con.execute("SELECT * FROM rprojects WHERE clean = 0 ORDER BY binding_errors DESC")
    ]
    subs = {r[0]: r[1] for r in con.execute("SELECT action, COUNT(*) FROM rsubstitutions GROUP BY 1")}
    gen = {r[0] or "source": r[1] for r in con.execute(
        "SELECT generated_kind, COUNT(*) FROM rfiles GROUP BY 1")}
    total = sum(s.get("projects", 0) for s in sides.values())
    n_unclean = len(unclean)
    try:
        twins = {r[0]: r[1] for r in con.execute(
            "SELECT CASE WHEN evidence LIKE '%api_contract%' THEN 'api_contract' "
            "WHEN evidence LIKE '%nested_contract%' THEN 'nested_contract' ELSE 'same_name_only' END, "
            "COUNT(*) FROM rtwins GROUP BY 1")}
    except Exception:
        twins = {}
    summary = {
        "available": True,
        "api_map": _api_map(con),
        "type_twins": twins,
        "workspace_built_at": m.get("build_started_at", ""),
        "sides": sides,
        "documents_by_kind": gen,
        "reference_substitutions": subs,
        "unclean_projects": unclean,
    }
    limitations = [
        f"The compiler index covers {total} projects -- every .csproj found on disk on both "
        "sides, plus the Website Projects compiled from ASP.NET's own generated page code "
        "(<%= %>, <%# %>, control fields, inline .ashx classes) and Razor views. "
        f"{n_unclean} of them compiled with binding errors (compiler_index.unclean_projects): "
        "a use inside one of those that did not bind is missing from compiler answers.",
        "Compiler answers are as of the last build + index pass (compiler_index."
        "workspace_built_at); files edited since then are not reflected until the next pass.",
        "Only the Debug configuration is compiled: code inside #if RELEASE (or other symbols "
        "not defined in Debug) is not in the compiler index.",
        "No compiler sees calls made by reflection, by URL from JavaScript (PageMethods, "
        ".ashx/.asmx endpoints), from SQL or Delphi, or through types named in .config files; "
        "those remain graph/text matches.",
    ]

    projects: list[ProjectCompilation] = []
    diags: list[dict] = []
    if project:
        like = "%" + project.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        rows = con.execute(
            "SELECT project FROM rprojects WHERE project LIKE ? ESCAPE '!' ORDER BY project LIMIT 50",
            (like,)).fetchall()
        projects = rindex.compilation(ctx, [r[0] for r in rows])
        if len(projects) == 1:
            diags = [dict(r) for r in con.execute(
                "SELECT code, binding, path, line, message FROM rdiagnostics WHERE project = ? "
                "ORDER BY binding DESC, path, line LIMIT 100", (projects[0].project,))]
    return summary, limitations, projects, diags


def _api_map(con) -> dict:
    """Legacy -> new API coverage: HTTP call sites matched to endpoint routes."""
    try:
        n_ep = con.execute("SELECT COUNT(*) FROM rendpoints").fetchone()[0]
    except Exception:
        return {}
    by_match = {r[0]: r[1] for r in con.execute(
        "SELECT match, COUNT(DISTINCT call_id) FROM rapilinks GROUP BY 1")}
    n_calls = con.execute("SELECT COUNT(*) FROM rhttpcalls").fetchone()[0]
    uncalled = [
        f"{r['verb']} {r['route']} -> {r['handler_key'] or '(unresolved handler)'}"
        for r in con.execute(
            "SELECT e.* FROM rendpoints e WHERE NOT EXISTS (SELECT 1 FROM rapilinks l "
            "WHERE l.endpoint_id = e.endpoint_id) ORDER BY e.route, e.verb LIMIT 60")
    ]
    n_uncalled = con.execute(
        "SELECT COUNT(*) FROM rendpoints e WHERE NOT EXISTS "
        "(SELECT 1 FROM rapilinks l WHERE l.endpoint_id = e.endpoint_id)").fetchone()[0]
    unmatched = [
        f"{r['verb']} {r['url']} at {r['path']}:{r['line']}"
        for r in con.execute(
            "SELECT c.* FROM rhttpcalls c WHERE NOT EXISTS (SELECT 1 FROM rapilinks l "
            "WHERE l.call_id = c.call_id) ORDER BY c.path, c.line LIMIT 40")
    ]
    return {
        "endpoints": n_ep,
        "endpoints_by_side": {r[0]: r[1] for r in con.execute(
            "SELECT side, COUNT(*) FROM rendpoints GROUP BY 1")},
        "http_call_sites": n_calls,
        "call_sites_linked": by_match,
        "call_sites_unmatched": n_calls - sum(by_match.values()),
        "endpoints_without_callers": n_uncalled,
        "endpoints_without_callers_sample": uncalled,
        "unmatched_call_sites_sample": unmatched,
        "note": "Links are route matches between a URL string and an endpoint route, not "
                "compiler references. route_exact = every literal segment agrees.",
    }


def _int(v: Optional[str]) -> int:
    try:
        return int(v) if v is not None else 0
    except ValueError:
        return 0
