"""find_references, find_callers, find_implementations.

``ambiguous_sites`` is never merged into ``references``.  The agent is expected
to say "37 confirmed references, plus 4 sites where the name is ambiguous
between A and B" rather than presenting a name match as a resolved one.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal, Optional

from mcp.types import ToolAnnotations

from ..models import (
    CallNode,
    FindCallersOut,
    FindImplementationsOut,
    FindReferencesOut,
    Loc,
    SymbolRef,
)
from . import common as c
from . import rindex
from .common import Ctx
from .symbols import _symbol_from_roslyn

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

GRAPH_CAVEAT = (
    "Structural (name-based) match from the tree-sitter graph, NOT a compiler answer. "
    "Report it as a structural match and check 'confidence' on each result."
)


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(
        name="find_references",
        annotations=RO,
        description=(
            "Every use site of a symbol. Answers first from the persisted compiler index "
            "(layer 'roslyn-index': every C# project in the workspace plus ASP.NET page code, "
            "compiler-exact), given a 'key', a file position (file + line + col, also inside "
            ".aspx/.ascx/.cshtml), or a name. Falls back to a live Roslyn host, then to the "
            "structural graph for JavaScript and anything the compiler index does not cover. "
            "If layer is 'graph', the answer is name-based and 'ambiguous_sites' may be "
            "non-empty. 'dispatch_sites' and 'candidate_sites' are never references."
        ),
    )
    def find_references(
        symbol: Optional[str] = None,
        file: Optional[str] = None,
        line: Optional[int] = None,
        col: Optional[int] = None,
        side: Literal["net48", "net8", "both"] = "both",
        include_markup: bool = True,
        max: int = 100,
        key: Optional[str] = None,
    ) -> FindReferencesOut:
        limit = min(int(max), ctx.cfg.limits.max_results_hard)

        t = rindex.resolve(ctx, key=key, symbol=symbol, file=file, line=line, col=col, side=side)
        if t is not None:
            return _rindex_references(ctx, t, include_markup, limit)

        if file and line:
            p = ctx.resolve_path(file)
            if p is None:
                return FindReferencesOut(
                    status="no_results",
                    layer="filesystem",
                    searched_scope=ctx.graph_scope(),
                    caveats=[f"No file matching '{file}' is indexed."],
                )
            rside = ctx.cfg.side_of(p)
            res, unavailable = c.try_roslyn(
                ctx, rside, "find_references",
                {"file": p.as_posix(), "line": int(line), "col": int(col or 1), "max": limit},
            )
            if unavailable:
                return FindReferencesOut(
                    status="unavailable",
                    layer="roslyn",
                    searched_scope=ctx.roslyn_scope(rside),
                    caveats=[
                        unavailable,
                        "No results were produced. This is NOT the same as 'no references "
                        "exist'; the compiler layer could not answer.",
                    ],
                )
            locs: list[Loc] = []
            synth_hits = 0
            for r in res.get("references", []):
                loc, synth = c.translate_location(
                    ctx, r["file"], r.get("line_start", 1), r.get("snippet") or ""
                )
                loc.line_end = r.get("line_end", loc.line_start)
                synth_hits += int(synth)
                locs.append(loc)
            by_file = Counter(l.rel_path for l in locs)
            caveats = []
            if synth_hits:
                caveats.append(
                    f"{synth_hits} of these references are in synthesised designer "
                    "partials and have been rewritten to the markup line that declares "
                    "the control. Those .synth.designer.cs files do not exist on disk."
                )
            if res.get("truncated"):
                caveats.append(
                    f"Showing {len(locs)} of {res.get('total_available')} references."
                )
            return FindReferencesOut(
                status="ok" if locs else "no_results",
                layer="roslyn",
                searched_scope=ctx.roslyn_scope(rside),
                target=_symbol_from_roslyn(ctx, res.get("target"), rside),
                references=locs,
                by_file=dict(by_file),
                truncated=bool(res.get("truncated")),
                total_available=res.get("total_available"),
                caveats=caveats,
            )

        if not symbol:
            return FindReferencesOut(
                status="no_results",
                layer="graph",
                searched_scope=ctx.graph_scope(),
                caveats=["Provide either 'symbol', or 'file' + 'line' (+ 'col')."],
            )

        return _graph_references(ctx, symbol, side, include_markup, limit)

    @mcp.tool(
        name="find_callers",
        annotations=RO,
        description=(
            "Inbound call tree for a method, to a given depth. Compiler-exact when a file "
            "position is given. Nodes already on the current path are marked cycle:true "
            "instead of being expanded again."
        ),
    )
    def find_callers(
        file: Optional[str] = None,
        line: Optional[int] = None,
        col: Optional[int] = None,
        symbol: Optional[str] = None,
        depth: int = 2,
        max: int = 100,
        key: Optional[str] = None,
    ) -> FindCallersOut:
        # NB: the builtin max() is shadowed by the 'max' parameter here.
        depth = int(depth)
        depth = 1 if depth < 1 else (5 if depth > 5 else depth)
        limit = min(int(max), ctx.cfg.limits.max_results_hard)

        t = rindex.resolve(ctx, key=key, symbol=symbol, file=file, line=line, col=col)
        if t is not None:
            return _rindex_callers(ctx, t, depth, limit)

        if file and line:
            p = ctx.resolve_path(file)
            if p is None:
                return FindCallersOut(
                    status="no_results",
                    layer="filesystem",
                    searched_scope=ctx.graph_scope(),
                    caveats=[f"No file matching '{file}' is indexed."],
                    depth=depth,
                )
            rside = ctx.cfg.side_of(p)
            res, unavailable = c.try_roslyn(
                ctx, rside, "find_callers",
                {
                    "file": p.as_posix(), "line": int(line), "col": int(col or 1),
                    "depth": depth, "max": limit,
                },
            )
            if unavailable:
                return FindCallersOut(
                    status="unavailable",
                    layer="roslyn",
                    searched_scope=ctx.roslyn_scope(rside),
                    caveats=[unavailable, "No results were produced; this is not 'no callers'."],
                    depth=depth,
                )
            tree = _call_node(ctx, res, rside)
            return FindCallersOut(
                status="ok",
                layer="roslyn",
                searched_scope=ctx.roslyn_scope(rside),
                target=tree.symbol if tree else None,
                tree=tree,
                depth=depth,
            )

        if not symbol:
            return FindCallersOut(
                status="no_results",
                layer="graph",
                searched_scope=ctx.graph_scope(),
                caveats=["Provide either 'symbol', or 'file' + 'line' (+ 'col')."],
                depth=depth,
            )
        return _graph_callers(ctx, symbol, depth, limit)

    @mcp.tool(
        name="find_implementations",
        annotations=RO,
        description=(
            "Implementers of an interface or interface member, derived classes (transitively), "
            "and overrides. Compiler-exact. Give a 'key', a file position, or a name."
        ),
    )
    def find_implementations(
        file: Optional[str] = None,
        line: Optional[int] = None,
        col: int = 1,
        max: int = 100,
        key: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> FindImplementationsOut:
        limit = min(int(max), ctx.cfg.limits.max_results_hard)
        t = rindex.resolve(ctx, key=key, symbol=symbol, file=file, line=line, col=col)
        if t is not None:
            return _rindex_implementations(ctx, t, limit)
        if not (file and line):
            return FindImplementationsOut(
                status="no_results",
                layer="filesystem",
                searched_scope=ctx.graph_scope(),
                caveats=["Provide 'key', 'symbol', or 'file' + 'line' (+ 'col')."],
            )
        p = ctx.resolve_path(file)
        if p is None:
            return FindImplementationsOut(
                status="no_results",
                layer="filesystem",
                searched_scope=ctx.graph_scope(),
                caveats=[f"No file matching '{file}' is indexed."],
            )
        rside = ctx.cfg.side_of(p)
        res, unavailable = c.try_roslyn(
            ctx, rside, "find_implementations",
            {"file": p.as_posix(), "line": int(line), "col": int(col),
             "max": min(int(max), ctx.cfg.limits.max_results_hard)},
        )
        if unavailable:
            return FindImplementationsOut(
                status="unavailable",
                layer="roslyn",
                searched_scope=ctx.roslyn_scope(rside),
                caveats=[unavailable, "No results were produced; this is not 'none exist'."],
            )
        impls = [s for s in (_symbol_from_roslyn(ctx, i, rside) for i in res.get("implementations", [])) if s]
        derived = [s for s in (_symbol_from_roslyn(ctx, d, rside) for d in res.get("derived", [])) if s]
        return FindImplementationsOut(
            status="ok" if (impls or derived) else "no_results",
            layer="roslyn",
            searched_scope=ctx.roslyn_scope(rside),
            target=_symbol_from_roslyn(ctx, res.get("target"), rside),
            implementations=impls,
            derived=derived,
            truncated=bool(res.get("truncated")),
        )


# --- compiler index ---------------------------------------------------------


def _rindex_references(ctx: Ctx, t: "rindex.Target", include_markup: bool, limit: int) -> FindReferencesOut:
    scope = rindex.scope(ctx)
    if len(t.keys) > 1:
        syms, caveat = rindex.ambiguity(ctx, t)
        return FindReferencesOut(status="partial", layer=rindex.LAYER, searched_scope=scope,
                                 ambiguous_candidates=syms, caveats=[caveat])
    key = t.keys[0]
    target = rindex.symbol(ctx, key)
    caveats: list[str] = []
    if target is None and key.startswith("asm:"):
        caveats.append(rindex.metadata_symbol_note(key))
    if t.how == "reference" and target is not None:
        caveats.append(f"{t.query} is a use site; this answer is about the symbol it binds to, "
                       f"{target.fqn}.")
    if t.how == "http_route" and target is not None:
        caveats.append(f"{t.query} is inside the URL of an HTTP call; this answer is about the "
                       f"endpoint handler that URL's route matches, {target.fqn}. That link is a "
                       "route match, not a compiler reference.")

    rows = rindex.refs(ctx, [key], limit, include_generated=include_markup)
    total = rindex.count_refs(ctx, [key])
    locs = rindex.ref_locs(ctx, rows)
    cand = rindex.refs(ctx, [key], limit, exact=False)
    n_cand = rindex.count_refs(ctx, [key], exact=False)
    dkeys = rindex.dispatch_keys(ctx, [key])
    disp = rindex.refs(ctx, dkeys, limit) if dkeys else []
    n_disp = rindex.count_refs(ctx, dkeys) if dkeys else 0

    http = rindex.http_callers(ctx, key)
    if http:
        kinds = Counter(h["match"] for h in http)
        caveats.append(
            f"This symbol handles HTTP endpoint(s) ({', '.join(sorted({h['ep_verb'] + ' ' + h['route'] for h in http}))}). "
            f"{len(http)} call site(s) send a request whose URL matches the route "
            f"({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))}); they are in "
            "'http_call_sites', NOT in 'references': the link is a route match between a URL "
            "string and the route, not a compiler reference. route_partial / route_ambiguous "
            "mean the URL could also hit another endpoint.")

    projects = rindex.projects_of(rows) | ({target.project} if target and target.project else set())
    comp = rindex.compilation(ctx, projects)
    caveats += rindex.compilation_caveats(comp)
    if target is not None:
        caveats += rindex.side_dirty_caveat(ctx, target.side)
    if total > len(locs):
        caveats.append(f"Showing {len(locs)} of {total} references.")
    gen = sum(1 for r in rows if r["is_generated"])
    if gen:
        caveats.append(
            f"{gen} of the references shown are in markup or views (<%= %>, <%# %>, control "
            "declarations, .cshtml). The compiler resolved them in ASP.NET's / Razor's generated "
            "code; the location given is the markup line.")
    if n_disp:
        caveats.append(
            f"{n_disp} further use(s) call an interface or base member this symbol implements "
            f"or overrides; they are in 'dispatch_sites' ({len(disp)} shown), NOT in "
            "'references' -- which implementation runs is decided at run time.")
    if n_cand:
        caveats.append(
            f"{n_cand} site(s) where the compiler had this symbol only as a candidate and did "
            f"not bind it (see candidate_reason); in 'candidate_sites' ({len(cand)} shown).")
    return FindReferencesOut(
        status="ok" if (locs or disp or cand) else "no_results",
        layer=rindex.LAYER,
        searched_scope=scope,
        target=target,
        references=locs,
        by_file=dict(Counter(l.rel_path for l in locs)),
        truncated=total > len(locs),
        total_available=total,
        dispatch_sites=rindex.ref_locs(ctx, disp),
        http_call_sites=rindex.ref_locs(ctx, http[:limit]),
        candidate_sites=rindex.ref_locs(ctx, cand),
        compilation=comp,
        caveats=caveats,
    )


def _rindex_callers(ctx: Ctx, t: "rindex.Target", depth: int, limit: int) -> FindCallersOut:
    scope = rindex.scope(ctx)
    if len(t.keys) > 1:
        _, caveat = rindex.ambiguity(ctx, t)
        return FindCallersOut(status="partial", layer=rindex.LAYER, searched_scope=scope,
                              depth=depth, caveats=[caveat])
    key = t.keys[0]
    tree = rindex.caller_tree(ctx, key, depth, limit)
    if tree is None:
        return FindCallersOut(
            status="no_results", layer=rindex.LAYER, searched_scope=scope, depth=depth,
            caveats=[rindex.metadata_symbol_note(key) if key.startswith("asm:")
                     else f"No declaration with key {key} in the compiler index."])

    def walk(n: CallNode):
        yield n
        for ch in n.callers:
            yield from walk(ch)

    comp = rindex.compilation(ctx, {n.symbol.project for n in walk(tree)})
    caveats = rindex.compilation_caveats(comp) + rindex.side_dirty_caveat(ctx, tree.symbol.side)
    n_http = sum(1 for n in walk(tree) if n.http)
    if n_http:
        caveats.append(
            f"{n_http} caller(s) in this tree reach their parent over HTTP (field 'http'): "
            "a URL string matched to an endpoint route, not a compiler call. Say so when "
            "reporting them, with the match strength in the label.")
    dkeys = rindex.dispatch_keys(ctx, [key])
    if dkeys:
        n = sum(len(rindex.refs(ctx, [k], 10_000)) for k in dkeys)
        if n:
            caveats.append(
                f"{n} further call site(s) go through the interface/base member(s) this "
                f"implements or overrides ({', '.join(dkeys[:5])}). They are not in this tree; "
                "call find_callers with one of those keys to see them.")
    return FindCallersOut(status="ok" if tree.callers else "no_results", layer=rindex.LAYER,
                          searched_scope=scope, target=tree.symbol, tree=tree, depth=depth,
                          compilation=comp, caveats=caveats)


def _rindex_implementations(ctx: Ctx, t: "rindex.Target", limit: int) -> FindImplementationsOut:
    scope = rindex.scope(ctx)
    if len(t.keys) > 1:
        _, caveat = rindex.ambiguity(ctx, t)
        return FindImplementationsOut(status="partial", layer=rindex.LAYER, searched_scope=scope,
                                      caveats=[caveat])
    key = t.keys[0]
    impl_keys, derived_keys = rindex.implementations(ctx, [key], limit)
    impls = [s for s in (rindex.symbol(ctx, k, with_snippet=False) for k in impl_keys) if s]
    derived = [s for s in (rindex.symbol(ctx, k, with_snippet=False) for k in derived_keys) if s]
    target = rindex.symbol(ctx, key, with_snippet=False)
    comp = rindex.compilation(ctx, {s.project for s in impls + derived + ([target] if target else [])})
    caveats = rindex.compilation_caveats(comp)
    if target is None and key.startswith("asm:"):
        caveats.append(rindex.metadata_symbol_note(key)
                       + " Implementations and derived types in source are still listed.")
    return FindImplementationsOut(
        status="ok" if (impls or derived) else "no_results", layer=rindex.LAYER,
        searched_scope=scope, target=target, implementations=impls, derived=derived,
        truncated=len(impl_keys) + len(derived_keys) >= limit, compilation=comp, caveats=caveats)


# --- graph fallbacks --------------------------------------------------------


def _graph_references(ctx: Ctx, symbol: str, side: str, include_markup: bool, limit: int) -> FindReferencesOut:
    name = symbol.rsplit(".", 1)[-1]
    params: list = [name]
    where = "r.name = ?"
    if side != "both":
        where += " AND r.side = ?"
        params.append(side)
    if not include_markup:
        where += " AND f.lang != 'markup'"

    # Confirmed and ambiguous are fetched as SEPARATE queries with their own
    # counts.  A single ordered-and-capped query returns whichever bucket sorts
    # first and silently drops the other -- which would hide ambiguity, the one
    # thing this layer must never do.
    base = (
        "SELECT r.*, f.rel_path FROM refs r JOIN files f ON f.file_id = r.file_id "
        f"WHERE {where}"
    )
    count_sql = (
        "SELECT COUNT(*) FROM refs r JOIN files f ON f.file_id = r.file_id "
        f"WHERE {where} AND r.resolution {{op}} 'ambiguous'"
    )
    n_conf = ctx.con.execute(count_sql.format(op="!="), params).fetchone()[0]
    n_amb = ctx.con.execute(count_sql.format(op="="), params).fetchone()[0]

    def fetch(extra: str) -> list[Loc]:
        rows = ctx.con.execute(
            f"{base} AND {extra} ORDER BY f.rel_path, r.line LIMIT ?", params + [limit]
        ).fetchall()
        return [
            Loc(
                file=ctx.cfg.abs_of(r["rel_path"]), rel_path=r["rel_path"],
                line_start=r["line"], line_end=r["line"], col_start=r["col"],
                snippet=c.snippet_for(ctx.cfg.abs_of(r["rel_path"]), r["line"], ctx.cfg),
            )
            for r in rows
        ]

    confirmed = fetch("r.resolution != 'ambiguous'")
    ambiguous = fetch("r.resolution = 'ambiguous'")
    total = n_conf

    cand_rows = ctx.con.execute(
        f"{c.SYMBOL_SELECT} WHERE s.name = ? ORDER BY s.is_generated LIMIT 20", (name,)
    ).fetchall()
    candidates = [c.symbol_from_row(ctx, r, with_snippet=False) for r in cand_rows]

    caveats = [GRAPH_CAVEAT]
    if n_amb:
        caveats.append(
            f"{n_conf} confirmed reference(s), plus {n_amb} site(s) where the name "
            f"'{name}' is ambiguous between {len(candidates)} declarations that share it. "
            f"The ambiguous ones are listed separately in 'ambiguous_sites' "
            f"({len(ambiguous)} shown) and must be reported as ambiguous, not as "
            "references."
        )
    if len(candidates) > 1:
        caveats.append(
            f"'{name}' is declared in {len(candidates)} places. For a compiler-exact "
            "answer call find_references with file + line + col of the declaration."
        )

    return FindReferencesOut(
        status="ok" if (confirmed or ambiguous) else "no_results",
        layer="graph",
        searched_scope=ctx.graph_scope(),
        target=candidates[0] if candidates else None,
        references=confirmed,
        by_file=dict(Counter(l.rel_path for l in confirmed)),
        ambiguous_sites=ambiguous,
        ambiguous_candidates=candidates,
        truncated=total > limit,
        total_available=total,
        caveats=caveats,
    )


def _graph_callers(ctx: Ctx, symbol: str, depth: int, limit: int) -> FindCallersOut:
    name = symbol.rsplit(".", 1)[-1]
    # Separate queries per bucket, for the same reason as _graph_references:
    # a single capped query would return only whichever bucket sorts first.
    call_where = "r.name = ? AND r.ref_kind IN ('call','handler_bind')"
    rows = ctx.con.execute(
        "SELECT r.*, f.rel_path FROM refs r JOIN files f ON f.file_id = r.file_id "
        f"WHERE {call_where} AND r.resolution != 'ambiguous' "
        "ORDER BY f.rel_path, r.line LIMIT ?",
        (name, limit * 4 + 100),
    ).fetchall()
    amb_rows = ctx.con.execute(
        "SELECT r.*, f.rel_path FROM refs r JOIN files f ON f.file_id = r.file_id "
        f"WHERE {call_where} AND r.resolution = 'ambiguous' "
        "ORDER BY f.rel_path, r.line LIMIT ?",
        (name, limit),
    ).fetchall()
    n_amb = ctx.con.execute(
        f"SELECT COUNT(*) FROM refs r WHERE {call_where} AND r.resolution = 'ambiguous'",
        (name,),
    ).fetchone()[0]

    target_rows = ctx.con.execute(
        f"{c.SYMBOL_SELECT} WHERE s.name = ? LIMIT 20", (name,)
    ).fetchall()
    target = c.symbol_from_row(ctx, target_rows[0], with_snippet=False) if target_rows else None

    def as_loc(r) -> Loc:
        return Loc(
            file=ctx.cfg.abs_of(r["rel_path"]), rel_path=r["rel_path"],
            line_start=r["line"], line_end=r["line"], col_start=r["col"],
            snippet=c.snippet_for(ctx.cfg.abs_of(r["rel_path"]), r["line"], ctx.cfg),
        )

    ambiguous: list[Loc] = [as_loc(r) for r in amb_rows]

    callers: list[CallNode] = []
    seen: set[tuple[str, int]] = set()
    for r in rows:
        encl = r["enclosing_sym"]
        if encl is None:
            continue
        key = (r["rel_path"], encl)
        if key in seen:
            continue
        seen.add(key)
        erow = ctx.con.execute(
            f"{c.SYMBOL_SELECT} WHERE s.symbol_id = ?", (encl,)
        ).fetchone()
        if erow:
            callers.append(CallNode(symbol=c.symbol_from_row(ctx, erow, with_snippet=False)))
        if len(callers) >= limit:
            break

    caveats = [
        GRAPH_CAVEAT,
        "Graph-layer callers are one level deep only; 'depth' is honoured by the Roslyn layer.",
    ]
    if n_amb:
        caveats.append(
            f"{len(callers)} confirmed caller(s), plus {n_amb} call site(s) where the "
            f"name '{name}' is ambiguous between {len(target_rows)} declarations "
            f"({len(ambiguous)} shown in 'ambiguous_callers'). Report those separately."
        )
    tree = CallNode(symbol=target, callers=callers) if target else None
    return FindCallersOut(
        status="ok" if callers or ambiguous else "no_results",
        layer="graph",
        searched_scope=ctx.graph_scope(),
        target=target,
        tree=tree,
        ambiguous_callers=ambiguous[:limit],
        depth=depth,
        caveats=caveats,
    )


def _call_node(ctx: Ctx, dto: Optional[dict], side: str) -> Optional[CallNode]:
    if not dto:
        return None
    sym = _symbol_from_roslyn(ctx, dto.get("symbol"), side)
    if sym is None:
        return None
    return CallNode(
        symbol=sym,
        cycle=bool(dto.get("cycle")),
        callers=[n for n in (_call_node(ctx, x, side) for x in dto.get("callers", [])) if n],
    )
