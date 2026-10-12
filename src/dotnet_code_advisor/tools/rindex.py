"""Queries over the persisted compiler index (db.SCHEMA_ROSLYN).  plan: Phase 3.

Every answer here was resolved by the C# compiler during the index pass, so it
is reported as layer "roslyn-index": compiler-exact, as of the build recorded in
index_meta.  It needs no live Roslyn host, so it answers identically on the
Linux deployment.

Nothing is merged silently:

* ``candidate_reason`` rows (the compiler found candidates but could not bind
  one -- overload failure, ambiguity, inaccessible) are returned apart from
  exact references;
* uses that reach a member only through the interface or base member it
  implements or overrides are "dispatch sites", apart from direct references;
* every answer carries the compile state of the projects involved, because a
  project with binding errors has uses the compiler could not resolve.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable, Optional

from .. import db as dbm
from ..models import CallNode, Loc, ProjectCompilation, SymbolRef
from . import common as c
from .common import Ctx

LAYER = "roslyn-index"
CALL_KINDS = ("call", "new", "base_ctor", "method_group", "attribute")


def available(ctx: Ctx) -> bool:
    try:
        return ctx.con.execute("SELECT 1 FROM rsymbols LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False


def scope(ctx: Ctx) -> str:
    con = ctx.con
    n_proj, n_clean = con.execute("SELECT COUNT(*), COALESCE(SUM(clean),0) FROM rprojects").fetchone()
    n_sym = con.execute("SELECT COUNT(*) FROM rsymbols").fetchone()[0]
    built = dbm.get_meta(con, "build_started_at", "unknown")
    sides = ", ".join(row[0] for row in con.execute("SELECT DISTINCT side FROM rprojects ORDER BY side"))
    return (
        f"compiler index (Roslyn, persisted): {n_proj} projects across {sides}, "
        f"{n_clean} of them compiled without binding errors; {n_sym:,} declarations; "
        f"workspace built {built}"
    )


# ------------------------------------------------------------------ rows -> models

def _loc(ctx: Ctx, path: str, line: int, col: int = 1, end: Optional[int] = None) -> Loc:
    generated = path.startswith("/") or (len(path) > 1 and path[1] == ":")
    abs_path = path if generated else ctx.cfg.abs_of(path)
    return Loc(file=abs_path, rel_path=path, line_start=line, line_end=end or line,
               col_start=col, snippet=c.snippet_for(abs_path, line, ctx.cfg))


def symbol_from_rrow(ctx: Ctx, r: sqlite3.Row, *, with_snippet: bool = True) -> SymbolRef:
    loc = _loc(ctx, r["path"], r["line"], r["col"], r["end_line"])
    if not with_snippet:
        loc.snippet = ""
    container = r["container_key"]
    if container and "|" in container:
        container = container.split("|", 1)[1][2:]  # "T:Ns.Type" -> "Ns.Type"
    elif container and container.startswith("ns:"):
        container = container[3:]
    return SymbolRef(
        name=r["name"], kind=r["kind"], fqn=r["fqn"], container=container,
        signature=r["signature"], side=r["side"], loc=loc,
        is_generated=bool(r["is_generated"]), confidence="exact_fqn",
        key=r["key"], project=r["project"],
    )


def declarations(ctx: Ctx, key: str) -> list[sqlite3.Row]:
    """All declaring rows of a symbol (a partial type has several), ordered."""
    return ctx.con.execute(
        "SELECT * FROM rsymbols WHERE key = ? ORDER BY is_generated, path, line", (key,)
    ).fetchall()


def symbol(ctx: Ctx, key: str, *, with_snippet: bool = True) -> Optional[SymbolRef]:
    rows = declarations(ctx, key)
    return symbol_from_rrow(ctx, rows[0], with_snippet=with_snippet) if rows else None


def metadata_symbol_note(key: str) -> str:
    asm, _, doc = key[4:].partition("|")
    return (f"{doc[2:]} is declared in the referenced assembly {asm} (metadata), not in "
            "source anywhere in the workspace; there is no file to open for it.")


# ------------------------------------------------------------------ lookup

def keys_by_name(ctx: Ctx, name: str, side: str = "both") -> list[str]:
    """Distinct symbol keys for a simple name ("Foo") or a qualified one ("Ns.Type.Foo").

    Generated declarations are included -- ASP.NET's control fields are real --
    but ordered after hand-written ones.
    """
    if "." in name:
        like = "%." + name.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        where, params = "(fqn = ? OR fqn LIKE ? ESCAPE '!')", [name, like]
    else:
        where, params = "name = ?", [name]
    if side != "both":
        where += " AND side = ?"
        params.append(side)
    rows = ctx.con.execute(
        f"SELECT key, MIN(is_generated) g FROM rsymbols WHERE {where} "
        "GROUP BY key ORDER BY g, key LIMIT 200", params).fetchall()
    return [r["key"] for r in rows]


def keys_at(ctx: Ctx, rel_path: str, line: int, col: Optional[int]) -> tuple[list[str], str]:
    """Symbol(s) at a file position: a declaration there, else what a reference there binds to.

    Returns (keys, how).  More than one key means the position was not specific
    enough (no column, several symbols on the line) -- the caller must say so.
    """
    con = ctx.con
    decls = con.execute(
        "SELECT key, name, col FROM rsymbols WHERE path = ? AND line = ? ORDER BY col",
        (rel_path, line)).fetchall()
    # A position in a declaration's header -- its attributes or modifiers, above
    # or before the name -- means that declaration (innermost first).
    header = con.execute(
        "SELECT key FROM rsymbols WHERE path = ? AND start_line <= ? AND line > ? "
        "ORDER BY start_line DESC, line LIMIT 1", (rel_path, line, line)).fetchall()
    if col:
        hit = [r["key"] for r in decls if r["col"] <= col < r["col"] + max(1, len(r["name"]))]
        if hit:
            return sorted(set(hit)), "declaration"
    refs = con.execute(
        "SELECT target_key, target_doc_id, col FROM rrefs WHERE path = ? AND line = ? "
        "AND candidate_reason IS NULL ORDER BY col", (rel_path, line)).fetchall()
    if col:
        hit = [r["target_key"] for r in refs
               if r["col"] <= col < r["col"] + max(1, len(_simple_name(r["target_doc_id"])))]
        if hit:
            return sorted(set(hit)), "reference"
    if header and not decls:
        return [header[0]["key"]], "declaration"
    # A position inside the URL string of an HTTP call: the endpoint(s) it hits.
    if col:
        hk = http_handlers_at(ctx, rel_path, line, col)
        if hk:
            return hk, "http_route"
    # No column, or the column fell between names: the line decides only if it
    # holds exactly one symbol.
    on_line = sorted({r["key"] for r in decls}) or sorted({r["target_key"] for r in refs})
    return on_line, ("declaration" if decls else "reference") if on_line else "none"


@dataclass
class Target:
    keys: list[str]
    how: str        # key | declaration | reference | name
    query: str


def resolve(ctx: Ctx, *, key: Optional[str] = None, symbol: Optional[str] = None,
            file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None,
            side: str = "both") -> Optional[Target]:
    """What the caller is asking about, in compiler-index keys.  None = the index cannot say.

    None (not an empty Target) lets the tool fall through to the live host or
    the graph -- e.g. for JavaScript, which the compiler index does not cover.
    """
    if not available(ctx):
        return None
    if key:
        return Target([key], "key", key)
    if file and line:
        p = ctx.resolve_path(file)
        if p is None:
            return None
        rel = ctx.rel(p)
        keys, how = keys_at(ctx, rel, int(line), int(col) if col else None)
        return Target(keys, how, f"{rel}:{line}" + (f":{col}" if col else "")) if keys else None
    if symbol:
        keys = keys_by_name(ctx, symbol, side)
        return Target(keys, "name", symbol) if keys else None
    return None


def ambiguity(ctx: Ctx, t: Target) -> tuple[list[SymbolRef], str]:
    """Candidates plus a caveat that lets the agent ask the user which one was meant."""
    syms: list[SymbolRef] = []
    parts: list[str] = []
    for k in t.keys[:20]:
        s = symbol(ctx, k, with_snippet=False)
        n = count_refs(ctx, [k])
        if s is not None:
            syms.append(s)
            parts.append(f"{s.signature or s.fqn} [{s.kind}, {s.project}] -- {n} reference(s), key={k}")
        else:
            parts.append(f"{k} -- {n} reference(s)")
    more = f" (first 20 of {len(t.keys)})" if len(t.keys) > 20 else ""
    return syms, (
        f"'{t.query}' matches {len(t.keys)} distinct symbols in the compiler index{more}: "
        + "; ".join(parts)
        + ". These are different symbols to the compiler (overloads, same-named types in "
        "different projects, ...). Ask the user which one they mean, or pass its 'key'."
    )


def side_dirty_caveat(ctx: Ctx, side: str) -> list[str]:
    n = ctx.con.execute(
        "SELECT COUNT(*) FROM rprojects WHERE clean = 0 AND side = ?", (side,)).fetchone()[0]
    if not n:
        return []
    return [f"{n} project(s) on the {side} side compiled with binding errors (index_status "
            "lists them). A use inside one of them that did not bind cannot appear here."]


def http_handlers_at(ctx: Ctx, rel_path: str, line: int, col: int) -> list[str]:
    try:
        rows = ctx.con.execute(
            "SELECT DISTINCT e.handler_key FROM rhttpcalls c "
            "JOIN rapilinks l ON l.call_id = c.call_id JOIN rendpoints e ON e.endpoint_id = l.endpoint_id "
            "WHERE c.path = ? AND c.url_line <= ? AND c.url_end_line >= ? "
            "AND (c.url_line < ? OR c.url_col <= ?) AND (c.url_end_line > ? OR c.url_end_col >= ?) "
            "AND e.handler_key IS NOT NULL ORDER BY e.handler_key",
            (rel_path, line, line, line, col, line, col)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows]


def http_callers(ctx: Ctx, key: str) -> list[sqlite3.Row]:
    """HTTP call sites whose URL matches a route this symbol handles."""
    try:
        return ctx.con.execute(
            "SELECT c.*, l.match, e.verb AS ep_verb, e.route FROM rendpoints e "
            "JOIN rapilinks l ON l.endpoint_id = e.endpoint_id "
            "JOIN rhttpcalls c ON c.call_id = l.call_id "
            "WHERE e.handler_key = ? ORDER BY c.path, c.line, c.col", (key,)).fetchall()
    except sqlite3.OperationalError:
        return []


def http_label(r: sqlite3.Row) -> str:
    return f"{r['ep_verb']} {r['route']} <- {r['url']} ({r['match']}) at {r['path']}:{r['line']}"


def _simple_name(doc_id: Optional[str]) -> str:
    if not doc_id:
        return ""
    body = doc_id[2:].split("(", 1)[0]
    name = body.rsplit(".", 1)[-1]
    name = name.split("`", 1)[0]
    return "this" if name == "Item" and doc_id.startswith("P:") and "(" in doc_id else name


# ------------------------------------------------------------------ queries

def _ref_where(keys: list[str], exact: bool) -> tuple[str, list]:
    marks = ",".join("?" * len(keys))
    return (f"target_key IN ({marks}) AND candidate_reason IS " + ("NULL" if exact else "NOT NULL"),
            list(keys))


def count_refs(ctx: Ctx, keys: list[str], exact: bool = True) -> int:
    if not keys:
        return 0
    where, params = _ref_where(keys, exact)
    return ctx.con.execute(f"SELECT COUNT(*) FROM rrefs WHERE {where}", params).fetchone()[0]


def refs(ctx: Ctx, keys: list[str], limit: int, *, exact: bool = True,
         include_generated: bool = True) -> list[sqlite3.Row]:
    if not keys:
        return []
    where, params = _ref_where(keys, exact)
    if not include_generated:
        where += " AND is_generated = 0"
    return ctx.con.execute(
        f"SELECT * FROM rrefs WHERE {where} ORDER BY path, line, col LIMIT ?", params + [limit]
    ).fetchall()


def ref_locs(ctx: Ctx, rows: Iterable[sqlite3.Row]) -> list[Loc]:
    out, seen = [], set()
    for r in rows:
        k = (r["path"], r["line"], r["col"])
        if k in seen:
            continue
        seen.add(k)
        out.append(_loc(ctx, r["path"], r["line"], r["col"]))
    return out


def dispatch_keys(ctx: Ctx, keys: list[str]) -> list[str]:
    """Interface/base members that ``keys`` implement or override, transitively."""
    seen: set[str] = set(keys)
    frontier = list(keys)
    out: list[str] = []
    while frontier:
        marks = ",".join("?" * len(frontier))
        rows = ctx.con.execute(
            f"SELECT DISTINCT dst_key FROM redges WHERE src_key IN ({marks}) "
            "AND edge_type IN ('implements_member','overrides')", frontier).fetchall()
        frontier = [r[0] for r in rows if r[0] not in seen]
        seen.update(frontier)
        out.extend(frontier)
    return sorted(out)


def implementations(ctx: Ctx, keys: list[str], limit: int) -> tuple[list[str], list[str]]:
    """(implementers / overriders, derived types), transitively for inheritance."""
    impl: list[str] = []
    derived: list[str] = []
    seen: set[str] = set(keys)
    frontier = list(keys)
    while frontier and len(impl) + len(derived) < limit:
        marks = ",".join("?" * len(frontier))
        rows = ctx.con.execute(
            f"SELECT src_key, edge_type FROM redges WHERE dst_key IN ({marks}) "
            "ORDER BY src_key", frontier).fetchall()
        nxt = []
        for r in rows:
            if r["src_key"] in seen:
                continue
            seen.add(r["src_key"])
            (derived if r["edge_type"] == "inherits" else impl).append(r["src_key"])
            nxt.append(r["src_key"])
        frontier = nxt
    return impl, derived


def caller_tree(ctx: Ctx, key: str, depth: int, limit: int,
                path: tuple[str, ...] = ()) -> Optional[CallNode]:
    sym = symbol(ctx, key, with_snippet=False)
    if sym is None:
        return None
    node = CallNode(symbol=sym)
    if key in path:
        node.cycle = True
        return node
    if depth <= 0:
        return node
    marks = ",".join("?" * len(CALL_KINDS))
    rows = ctx.con.execute(
        f"SELECT DISTINCT enclosing_key FROM rrefs WHERE target_key = ? AND candidate_reason IS NULL "
        f"AND ref_kind IN ({marks}) AND enclosing_key IS NOT NULL ORDER BY enclosing_key LIMIT ?",
        (key, *CALL_KINDS, limit)).fetchall()
    for r in rows:
        child = caller_tree(ctx, r[0], depth - 1, limit, path + (key,))
        if child is not None:
            node.callers.append(child)
    # Callers across an HTTP hop: code whose URL matches a route this handles.
    seen: set[str] = set()
    for c in http_callers(ctx, key):
        encl = c["enclosing_key"]
        if not encl or encl in seen or len(node.callers) >= limit:
            continue
        seen.add(encl)
        child = caller_tree(ctx, encl, depth - 1, limit, path + (key,))
        if child is not None:
            child.http = http_label(c)
            node.callers.append(child)
    return node


# ------------------------------------------------------------------ compile state

def compilation(ctx: Ctx, projects: Iterable[str]) -> list[ProjectCompilation]:
    ps = sorted({p for p in projects if p})
    if not ps:
        return []
    marks = ",".join("?" * len(ps))
    return [
        ProjectCompilation(
            project=r["project"], side=r["side"], clean=bool(r["clean"]),
            binding_errors=r["binding_errors"], errors=r["errors"],
            top_errors=r["top_errors"] or "", build_exit=r["build_exit"],
        )
        for r in ctx.con.execute(f"SELECT * FROM rprojects WHERE project IN ({marks}) ORDER BY project", ps)
    ]


def compilation_caveats(comp: list[ProjectCompilation]) -> list[str]:
    dirty = [p for p in comp if not p.clean]
    if not dirty:
        return []
    return [
        f"Project {p.project} compiled with {p.binding_errors} binding error(s) "
        f"({p.top_errors}). Uses of symbols the compiler could not resolve in that project "
        "are missing from this answer; everything that did bind is exact."
        for p in dirty
    ]


def projects_of(rows: Iterable[sqlite3.Row]) -> set[str]:
    return {r["project"] for r in rows}
