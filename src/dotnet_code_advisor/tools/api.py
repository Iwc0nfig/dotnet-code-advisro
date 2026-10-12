"""find_api_calls: which API endpoints does this code end up calling?

The outbound view of the legacy -> new API hop.  Starting from a page, file,
type or member, walk DOWN the compiler-resolved call graph (rrefs: calls,
constructors, property accessors, method groups) and collect every HTTP call
site reached, with the endpoint its URL matches (rapilinks).

What it is and is not:
* the walk is static: every branch counts, whether or not it runs for a given
  request, so it over-approximates reachability;
* calls through an interface continue into every implementation the index
  knows; a virtual member of the starting types' own base classes (or of the
  framework, e.g. Page.OnLoad) only into overrides within their own
  hierarchy, any other virtual member into all overrides (marked via_dispatch);
* delegates, events, reflection and anything outside C# (JavaScript) are not
  followed;
* the HTTP hop itself is a route match (route_exact / route_partial /
  route_ambiguous), never a compiler reference.
"""

from __future__ import annotations

from typing import Optional

from mcp.types import ToolAnnotations

from ..models import ApiCallHit, FindApiCallsOut, Loc
from . import rindex
from .common import Ctx

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
MARKUP_EXTS = (".aspx", ".ascx", ".master", ".asmx", ".ashx", ".cshtml")
# Refs that run code in the target: calls, constructors, method groups, and
# reads/writes (a property access runs its accessor).
EXEC_KINDS = ("call", "new", "base_ctor", "method_group", "attribute", "read", "write")
NODE_CAP = 20_000


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(
        name="find_api_calls",
        annotations=RO,
        description=(
            "Outbound: which HTTP endpoints (e.g. the new .NET 8 API) does this code end up "
            "calling? Give a page or other file (file only), a type or member (key, symbol, or "
            "file+line+col). Walks down the compiler-resolved call graph to 'depth' and returns "
            "every HTTP call reached, the endpoint its URL matches (route_exact / route_partial "
            "/ route_ambiguous -- a route match, not a compiler reference), and the call chain "
            "that leads there. Unmatched calls (third-party APIs, routes the API does not "
            "declare) are listed separately. Reachability is static: every branch counts."
        ),
    )
    def find_api_calls(
        file: Optional[str] = None,
        line: Optional[int] = None,
        col: Optional[int] = None,
        key: Optional[str] = None,
        symbol: Optional[str] = None,
        depth: int = 6,
        include_dispatch: bool = True,
        max: int = 200,
    ) -> FindApiCallsOut:
        depth = 1 if int(depth) < 1 else (12 if int(depth) > 12 else int(depth))
        limit = min(int(max), ctx.cfg.limits.max_results_hard)
        if not rindex.available(ctx):
            return FindApiCallsOut(
                status="unavailable", layer="roslyn-index", searched_scope="compiler index",
                caveats=["No compiler index has been built; outbound API calls need it "
                         "(scripts/build-workspace.py, then scripts/reindex.py)."])
        scope = rindex.scope(ctx)

        starts, start_desc, err = _start_keys(ctx, file, line, col, key, symbol)
        if err is not None:
            return FindApiCallsOut(status=err[0], layer="roslyn-index", searched_scope=scope,
                                   caveats=[err[1]])
        return outbound(ctx, starts, start_desc, depth, include_dispatch, limit, scope)


def _start_keys(ctx: Ctx, file, line, col, key, symbol):
    """(keys, description, error) -- error is (status, message)."""
    con = ctx.con
    if file and not line and not key and not symbol:
        p = ctx.resolve_path(file)
        if p is None:
            return [], "", ("no_results", f"No file matching '{file}' is indexed.")
        rel = ctx.rel(p)
        paths = [rel]
        if rel.lower().endswith(MARKUP_EXTS):
            # the page's code-behind, from the graph's page table
            row = con.execute(
                "SELECT cb.rel_path FROM pages pg JOIN files m ON m.file_id = pg.markup_file "
                "JOIN files cb ON cb.file_id = pg.codebehind_file WHERE m.rel_path = ?", (rel,)).fetchone()
            if row:
                paths.append(row[0])
        keys: set[str] = set()
        for pth in paths:
            keys.update(r[0] for r in con.execute(
                "SELECT DISTINCT key FROM rsymbols WHERE path = ? AND kind IN "
                "('method','constructor','property','indexer','event')", (pth,)))
            # members of ASP.NET's generated page class that run this markup's expressions
            keys.update(r[0] for r in con.execute(
                "SELECT DISTINCT enclosing_key FROM rrefs WHERE path = ? AND enclosing_key IS NOT NULL", (pth,)))
        if not keys:
            return [], "", ("no_results", f"No compiled members in {rel} (or its code-behind).")
        return sorted(keys), " + ".join(paths), None

    t = rindex.resolve(ctx, key=key, symbol=symbol, file=file, line=line, col=col)
    if t is None:
        return [], "", ("no_results", "Nothing in the compiler index at that position / name.")
    if len(t.keys) > 1:
        _, caveat = rindex.ambiguity(ctx, t)
        return [], "", ("partial", caveat)
    k = t.keys[0]
    members = [r[0] for r in con.execute(
        "SELECT DISTINCT key FROM rsymbols WHERE container_key = ? AND kind IN "
        "('method','constructor','property','indexer','event')", (k,))]
    return (sorted(members) if members else [k]), t.query, None


def _related(ctx: Ctx, types: set[str], down: bool) -> set[str]:
    """The given types plus, transitively, their subtypes (down=True: e.g. ASP.NET's
    generated page class) or their base classes (down=False)."""
    near, far = ("dst_key", "src_key") if down else ("src_key", "dst_key")
    out = set(types)
    frontier = sorted(types)
    while frontier:
        nxt = []
        for chunk in _chunks(frontier):
            for (k,) in ctx.con.execute(
                    f"SELECT {far} FROM redges WHERE edge_type = 'inherits' AND {near} IN "
                    f"({','.join('?' * len(chunk))})", chunk):
                if k not in out:
                    out.add(k)
                    nxt.append(k)
        frontier = sorted(nxt)
    return out


def _chunks(xs: list, n: int = 500):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def outbound(ctx: Ctx, starts: list[str], start_desc: str, depth: int, include_dispatch: bool,
             limit: int, scope: str) -> FindApiCallsOut:
    con = ctx.con
    parent: dict[str, Optional[str]] = {k: None for k in starts}
    dispatched: set[str] = set()
    # Virtual dispatch stays inside the starting types' own hierarchy: a page
    # calling its base class's virtual buildContent() runs THIS page's override,
    # not every other page's.  Interface calls may reach any implementation.
    start_types = {r[0] for chunk in _chunks(sorted(starts)) for r in con.execute(
        f"SELECT DISTINCT container_key FROM rsymbols WHERE key IN ({','.join('?' * len(chunk))}) "
        "AND container_key IS NOT NULL", chunk)}
    own_types = _related(ctx, start_types, down=True)
    ancestors = _related(ctx, start_types, down=False) - start_types
    frontier = sorted(starts)
    truncated = False
    marks = ",".join("?" * len(EXEC_KINDS))
    for _ in range(depth):
        nxt: list[str] = []
        for chunk in _chunks(frontier):
            rows = con.execute(
                f"SELECT DISTINCT enclosing_key, target_key FROM rrefs WHERE enclosing_key IN "
                f"({','.join('?' * len(chunk))}) AND candidate_reason IS NULL AND target_in_source = 1 "
                f"AND ref_kind IN ({marks}) ORDER BY enclosing_key, target_key",
                (*chunk, *EXEC_KINDS)).fetchall()
            for src, tgt in rows:
                # types and fields run no code of their own
                if "|T:" in tgt or "|F:" in tgt or tgt in parent:
                    continue
                parent[tgt] = src
                nxt.append(tgt)
        if include_dispatch and nxt:
            for chunk in _chunks(sorted(set(nxt))):
                for impl, dst, edge, container, dst_container in con.execute(
                        "SELECT e.src_key, e.dst_key, e.edge_type, s.container_key, "
                        "(SELECT d.container_key FROM rsymbols d WHERE d.key = e.dst_key LIMIT 1) "
                        "FROM redges e LEFT JOIN rsymbols s ON s.key = e.src_key "
                        f"WHERE e.dst_key IN ({','.join('?' * len(chunk))}) "
                        "AND e.edge_type IN ('implements_member','overrides') ORDER BY e.src_key", chunk):
                    # A virtual member of the start types' own base classes (or of
                    # the framework, e.g. Page.OnLoad) is called on THIS object:
                    # only overrides in its own hierarchy can run.  A virtual member
                    # of some other type may run any override.
                    on_this = dst.startswith("asm:") or dst_container in ancestors
                    if edge == "overrides" and on_this and container not in own_types:
                        continue
                    if impl not in parent:
                        parent[impl] = dst
                        dispatched.add(impl)
                        nxt.append(impl)
        frontier = sorted(set(nxt))
        if not frontier:
            break
        if len(parent) > NODE_CAP:
            truncated = True
            break

    def chain(k: str) -> list[str]:
        out, seen = [], set()
        while k is not None and k not in seen:
            seen.add(k)
            s = rindex.symbol(ctx, k, with_snippet=False)
            out.append((s.fqn if s else k) + (" [via dispatch]" if k in dispatched else ""))
            k = parent.get(k)
        return list(reversed(out))

    visited = sorted(parent)
    hits: list[ApiCallHit] = []
    unmatched: list[ApiCallHit] = []
    for chunk in _chunks(visited):
        for r in con.execute(
            "SELECT c.*, l.match, e.verb AS ep_verb, e.route, e.handler_key, e.side AS ep_side "
            "FROM rhttpcalls c LEFT JOIN rapilinks l ON l.call_id = c.call_id "
            "LEFT JOIN rendpoints e ON e.endpoint_id = l.endpoint_id "
            f"WHERE c.enclosing_key IN ({','.join('?' * len(chunk))}) ORDER BY c.path, c.line, c.col",
            chunk,
        ):
            site = rindex._loc(ctx, r["path"], r["line"], r["col"])
            via = chain(r["enclosing_key"])
            hit = ApiCallHit(
                verb=r["ep_verb"] or r["verb"], route=r["route"], url=r["url"],
                match=r["match"] or "unmatched",
                handler=rindex.symbol(ctx, r["handler_key"], with_snippet=False) if r["handler_key"] else None,
                endpoint_side=r["ep_side"], call_site=site, chain=via,
                via_dispatch=any(v.endswith("[via dispatch]") for v in via),
            )
            (hits if r["match"] else unmatched).append(hit)
    hits.sort(key=lambda h: (h.route or "", h.verb, h.call_site.rel_path, h.call_site.line_start))
    n_endpoints = len({(h.verb, h.route) for h in hits})

    caveats = [
        "Links from a call site to an endpoint are route matches (URL string vs route), not "
        "compiler references; report each with its 'match'.",
        "Reachability is static: every branch of every member walked counts, whether or not "
        "it runs for a given request. Delegates, events, reflection and JavaScript are not followed.",
    ]
    if include_dispatch and dispatched:
        caveats.append(f"{len(dispatched)} member(s) were reached only through an interface or "
                       "virtual call (chain entries marked [via dispatch]); which implementation "
                       "runs is decided at run time.")
    if truncated:
        caveats.append(f"The walk stopped at {NODE_CAP:,} members; the result is incomplete.")
    # Compile state of the projects where calls were found (and where the walk
    # started): a binding error there can hide a call.
    site_projects = {r[0] for r in con.execute(
        "SELECT DISTINCT project FROM rsymbols WHERE key IN (%s)" % ",".join("?" * min(len(starts), 900)),
        starts[:900])}
    site_projects |= {h.handler.project for h in hits if h.handler and h.handler.project}
    for h in hits + unmatched:
        row = con.execute("SELECT project FROM rhttpcalls WHERE path = ? AND line = ? LIMIT 1",
                          (h.call_site.rel_path, h.call_site.line_start)).fetchone()
        if row:
            site_projects.add(row[0])
    comp = rindex.compilation(ctx, site_projects)
    caveats += rindex.compilation_caveats(comp)
    return FindApiCallsOut(
        status="ok" if (hits or unmatched) else "no_results",
        layer="roslyn-index",
        searched_scope=f"{scope}; walked {len(visited):,} member(s) down from {start_desc}",
        start=start_desc, members_walked=len(visited), endpoints_reached=n_endpoints,
        calls=hits[:limit], unmatched_calls=unmatched[:limit],
        truncated=truncated or len(hits) > limit, total_available=len(hits),
        compilation=comp, caveats=caveats,
    )
