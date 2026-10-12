"""find_symbol, go_to_definition, get_type_info.  """

from __future__ import annotations

import re
import sqlite3
from typing import Literal, Optional

from mcp.types import ToolAnnotations

from .. import db as dbm
from ..models import (
    FindSymbolOut,
    GoToDefinitionOut,
    Loc,
    SymbolRef,
    TypeInfoOut,
)
from . import common as c
from . import rindex
from .common import Ctx

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

KIND_RANK = {
    "class": 0, "interface": 1, "struct": 2, "record": 3, "enum": 4, "delegate": 5,
    "aspx_page": 6, "ascx_control": 6, "master_page": 6,
    "method": 7, "constructor": 8, "property": 9, "event": 10, "field": 11,
    "indexer": 12, "js_class": 13, "js_function": 14, "js_method": 14, "js_var": 15,
    "control_instance": 16, "namespace": 17, "handler": 18,
}

COLLISION_CAVEAT = (
    "The query matched declarations in both configured roots. "
    "Results are tagged with side; do not merge unrelated symbols."
)



def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(
        name="find_symbol",
        annotations=RO,
        description=(
            "Find declarations by name across configured roots. Returns the file and line "
            "range of each declaration so you can read the real source with "
            "read_file_range. This returns WHERE things are declared, not what they do "
            "-- always read the source before describing behaviour."
        ),
    )
    def find_symbol(
        query: str,
        kinds: Optional[list[str]] = None,
        side: Literal["net48", "net8", "both"] = "both",
        exact: bool = False,
        include_generated: bool = False,
        max: int = 50,
    ) -> FindSymbolOut:
        """Search declarations by simple name, FQN suffix, or full-text phrase."""
        limit = min(int(max), ctx.cfg.limits.max_results_hard)
        params: list = []
        where: list[str] = []

        if exact:
            where.append("s.name = ?")
            params.append(query)
        else:
            where.append("(s.name LIKE ? ESCAPE '\\' OR s.fqn LIKE ? ESCAPE '\\')")
            esc = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params += [f"%{esc}%", f"%{esc}"]
        if side != "both":
            where.append("s.side = ?")
            params.append(side)
        if kinds:
            where.append(f"s.kind IN ({','.join('?' * len(kinds))})")
            params += list(kinds)
        if not include_generated:
            where.append("s.is_generated = 0")

        # Full-text over declaration headers and doc comments, when available.
        # This is what makes a descriptive query ("credit card gateway") work at
        # all; the LIKE pass below still runs, so a substring that FTS tokenises
        # away is never lost.
        fts_rows: list = []
        if not exact and dbm.fts_enabled(ctx.con):
            match = _fts_query(query)
            if match:
                fts_where = list(where[1:])  # drop the name/fqn LIKE clause
                fts_params = list(params[2:])
                fsql = (
                    "SELECT s.*, f.rel_path FROM symbols_fts fts "
                    "JOIN symbols s ON s.symbol_id = fts.rowid "
                    "JOIN files f ON f.file_id = s.file_id "
                    "WHERE symbols_fts MATCH ?"
                    + ("" if not fts_where else " AND " + " AND ".join(fts_where))
                    + " LIMIT ?"
                )
                try:
                    fts_rows = ctx.con.execute(
                        fsql, [match] + fts_params + [limit * 4 + 50]
                    ).fetchall()
                except sqlite3.OperationalError:
                    fts_rows = []  # malformed MATCH; the LIKE pass still answers

        sql = f"{c.SYMBOL_SELECT} WHERE {' AND '.join(where)}"
        if side == "both":
            # Query each side separately.  A single capped query can return a
            # page that happens to be entirely one side, which would report a
            # symbol that exists on both as if it existed only on one -- the
            # precise confusion the net48/net8 split exists to prevent.
            cap = limit * 4 + 50
            rows = (
                ctx.con.execute(sql + " AND s.side='net48' LIMIT ?", params + [cap]).fetchall()
                + ctx.con.execute(sql + " AND s.side='net8' LIMIT ?", params + [cap]).fetchall()
            )
        else:
            rows = ctx.con.execute(sql + " LIMIT ?", params + [limit * 6 + 50]).fetchall()

        if fts_rows:
            seen_ids = {r["symbol_id"] for r in rows}
            rows = list(rows) + [r for r in fts_rows if r["symbol_id"] not in seen_ids]

        ql = query.lower()

        def rank(r):
            return (
                0 if r["name"].lower() == ql else 1,
                0 if not r["is_generated"] else 1,
                0 if not r["is_synthetic"] else 1,
                KIND_RANK.get(r["kind"], 50),
                r["rel_path"],
                r["symbol_id"],
            )

        rows.sort(key=rank)
        total = len(rows)
        sides_present = {r["side"] for r in rows}

        # When a name exists on BOTH sides of the migration, truncation must not
        # hide one of them: a page of results that happens to be all net8 reads
        # as "this only exists in .NET 8", which is exactly the wrong conclusion.
        # Interleave the two sides so both are visible within the limit.
        if side == "both" and sides_present == {"net48", "net8"}:
            a = [r for r in rows if r["side"] == "net48"]
            b = [r for r in rows if r["side"] == "net8"]
            merged = [x for pair in zip(a, b) for x in pair]
            merged += a[len(b):] if len(a) > len(b) else b[len(a):]
            page = merged[:limit]
        else:
            page = rows[:limit]

        hits = [c.symbol_from_row(ctx, r) for r in page]

        caveats: list[str] = []
        # Judged on every match, not just the page returned.
        if side == "both" and sides_present == {"net48", "net8"}:
            n48 = sum(1 for r in rows if r["side"] == "net48")
            caveats.append(
                COLLISION_CAVEAT
                + f" ({n48} match(es) on net48, {total - n48} on net8.)"
            )
        if any(h.is_synthetic for h in hits):
            caveats.append(
                "Some results are synthesised control fields (is_synthetic). They exist "
                "only in this index; cite the markup line, not a file on disk."
            )
        if not ctx.con.execute("SELECT 1 FROM symbols LIMIT 1").fetchone():
            caveats.append("The index is empty. Run scripts/reindex.py.")

        return FindSymbolOut(
            status="ok" if hits else "no_results",
            layer="graph",
            searched_scope=ctx.graph_scope(),
            query=query,
            symbols=hits,
            truncated=total > limit,
            total_available=total,
            caveats=caveats,
        )

    @mcp.tool(
        name="go_to_definition",
        annotations=RO,
        description=(
            "The declaration site(s) of a symbol. Give either a position "
            "(file + line + col) for a compiler-exact answer, or a name/FQN for a "
            "structural lookup. A partial class legitimately has several declarations; "
            "all are returned."
        ),
    )
    def go_to_definition(
        symbol: Optional[str] = None,
        file: Optional[str] = None,
        line: Optional[int] = None,
        col: Optional[int] = None,
        side: Literal["net48", "net8", "both"] = "both",
        key: Optional[str] = None,
    ) -> GoToDefinitionOut:
        t = rindex.resolve(ctx, key=key, symbol=symbol, file=file, line=line, col=col, side=side)
        if t is not None:
            return _rindex_definition(ctx, t)
        if file and line:
            p = ctx.resolve_path(file)
            if p is None:
                return GoToDefinitionOut(
                    status="no_results",
                    layer="filesystem",
                    searched_scope=ctx.graph_scope(),
                    caveats=[f"No file matching '{file}' is indexed."],
                )
            rside = ctx.cfg.side_of(p)
            res, unavailable = c.try_roslyn(
                ctx, rside, "go_to_definition",
                {"file": p.as_posix(), "line": int(line), "col": int(col or 1)},
            )
            if unavailable:
                return GoToDefinitionOut(
                    status="unavailable",
                    layer="roslyn",
                    searched_scope=ctx.roslyn_scope(rside),
                    caveats=[unavailable, "No results were produced; this is not 'not found'."],
                )
            defs = []
            for d in res.get("definitions", []):
                loc, synth = c.translate_location(
                    ctx, d["file"], d["line_start"], d.get("snippet") or ""
                )
                loc.line_end = d.get("line_end", loc.line_start)
                defs.append(loc)
            target = _symbol_from_roslyn(ctx, res.get("target"), rside)
            caveats = []
            if res.get("in_metadata"):
                caveats.append(
                    "This symbol is declared in metadata (a referenced assembly), not in "
                    "source in this repository, so it has no source location."
                )
            return GoToDefinitionOut(
                status="ok" if defs else "no_results",
                layer="roslyn",
                searched_scope=ctx.roslyn_scope(rside),
                target=target,
                definitions=defs,
                partial_declarations=len(defs),
                caveats=caveats,
            )

        if not symbol:
            return GoToDefinitionOut(
                status="no_results",
                layer="graph",
                searched_scope=ctx.graph_scope(),
                caveats=["Provide either 'symbol', or 'file' + 'line' (+ 'col')."],
            )

        params: list = [symbol, symbol]
        where = "(s.fqn = ? OR s.name = ?)"
        if side != "both":
            where += " AND s.side = ?"
            params.append(side)
        rows = ctx.con.execute(
            f"{c.SYMBOL_SELECT} WHERE {where} ORDER BY s.is_generated, s.symbol_id LIMIT 50",
            params,
        ).fetchall()
        hits = [c.symbol_from_row(ctx, r) for r in rows]
        return GoToDefinitionOut(
            status="ok" if hits else "no_results",
            layer="graph",
            searched_scope=ctx.graph_scope(),
            target=hits[0] if hits else None,
            definitions=[h.loc for h in hits],
            partial_declarations=len(hits),
            caveats=(
                [
                    "Structural (name-based) match, not a compiler answer. Pass "
                    "file+line+col for a compiler-exact result."
                ]
                if hits
                else []
            )
            + (
                [COLLISION_CAVEAT]
                if len({h.side for h in hits}) > 1
                else []
            ),
        )

    @mcp.tool(
        name="get_type_info",
        annotations=RO,
        description=(
            "Members, base type, interfaces and accessibility of the type at a position. "
            "Compiler-exact: requires a file position inside a loaded project."
        ),
    )
    def get_type_info(file: str, line: int, col: int = 1) -> TypeInfoOut:
        p = ctx.resolve_path(file)
        if p is None:
            return TypeInfoOut(
                status="no_results",
                layer="filesystem",
                searched_scope=ctx.graph_scope(),
                caveats=[f"No file matching '{file}' is indexed."],
            )
        rside = ctx.cfg.side_of(p)
        res, unavailable = c.try_roslyn(
            ctx, rside, "type_info",
            {"file": p.as_posix(), "line": int(line), "col": int(col)},
        )
        if unavailable:
            return TypeInfoOut(
                status="unavailable",
                layer="roslyn",
                searched_scope=ctx.roslyn_scope(rside),
                caveats=[unavailable, "No results were produced; this is not 'not found'."],
            )
        return TypeInfoOut(
            status="ok",
            layer="roslyn",
            searched_scope=ctx.roslyn_scope(rside),
            target=_symbol_from_roslyn(ctx, res.get("target"), rside),
            type_kind=res.get("type_kind"),
            base_type=_symbol_from_roslyn(ctx, res.get("base_type"), rside),
            interfaces=[
                s for s in (_symbol_from_roslyn(ctx, i, rside) for i in res.get("interfaces", []))
                if s
            ],
            members=[
                s for s in (_symbol_from_roslyn(ctx, m, rside) for m in res.get("members", []))
                if s
            ],
            is_partial=bool(res.get("is_partial")),
            declarations=int(res.get("declarations", 0)),
        )


_FTS_TOKENS = re.compile(r"[A-Za-z0-9_]+")


def _fts_query(query: str) -> Optional[str]:
    """Turn a user query into an FTS5 MATCH expression.

    A single token becomes a prefix match (``"order"*``) so ``ProcessOrder``
    still hits; several tokens become a phrase, which is what makes a dotted
    name like ``Modern`` match a container. Returns None when the query has
    no indexable token, in which case only the LIKE pass runs.
    """
    toks = _FTS_TOKENS.findall(query)
    if not toks:
        return None
    if len(toks) == 1:
        return f'"{toks[0]}"*'
    return '"' + " ".join(toks) + '"'


def _symbol_from_roslyn(ctx: Ctx, dto: Optional[dict], side: str) -> Optional[SymbolRef]:
    if not dto:
        return None
    loc_dto = dto.get("loc")
    if loc_dto and loc_dto.get("file"):
        loc, synthetic = c.translate_location(
            ctx, loc_dto["file"], loc_dto.get("line_start", 1), loc_dto.get("snippet") or ""
        )
        loc.line_end = loc_dto.get("line_end", loc.line_start)
        loc.col_start = loc_dto.get("col_start", 1)
    else:
        loc, synthetic = (
            Loc(
                file="(metadata)",
                rel_path="(declared in a referenced assembly, not in this repository)",
                line_start=0,
                line_end=0,
            ),
            False,
        )
    return SymbolRef(
        name=dto.get("name", ""),
        kind=dto.get("kind", "unknown"),
        fqn=dto.get("fqn"),
        container=dto.get("container"),
        signature=dto.get("signature"),
        side=side,  # type: ignore[arg-type]
        loc=loc,
        is_synthetic=synthetic,
        confidence="exact_fqn",
    )


def _rindex_definition(ctx: Ctx, t: "rindex.Target") -> GoToDefinitionOut:
    scope = rindex.scope(ctx)
    if len(t.keys) > 1:
        syms, caveat = rindex.ambiguity(ctx, t)
        if t.how == "http_route":
            caveat = ("The URL at this position matches more than one endpoint route "
                      "(route_ambiguous / route_partial). " + caveat)
        return GoToDefinitionOut(status="partial", layer=rindex.LAYER, searched_scope=scope,
                                 definitions=[s_.loc for s_ in syms], caveats=[caveat])
    key = t.keys[0]
    rows = rindex.declarations(ctx, key)
    if not rows:
        note = (rindex.metadata_symbol_note(key) if key.startswith("asm:")
                else f"No declaration with key {key} in the compiler index.")
        return GoToDefinitionOut(status="ok" if key.startswith("asm:") else "no_results",
                                 layer=rindex.LAYER, searched_scope=scope, caveats=[note])
    syms = [rindex.symbol_from_rrow(ctx, r) for r in rows]
    comp = rindex.compilation(ctx, {syms[0].project})
    caveats = rindex.compilation_caveats(comp)
    if t.how == "http_route":
        caveats.append(
            f"{t.query} is inside the URL of an HTTP call. The definition given is the endpoint "
            "handler whose route that URL matches -- a route match across the HTTP hop, not a "
            "compiler reference.")
    if any(s_.is_generated for s_ in syms):
        caveats.append(
            "Some declarations are in generated code (ASP.NET page code or a source "
            "generator), not in a file in the workspace; loc.file points at the generated file.")
    return GoToDefinitionOut(status="ok", layer=rindex.LAYER, searched_scope=scope,
                             target=syms[0], definitions=[s_.loc for s_ in syms],
                             partial_declarations=len(syms), compilation=comp, caveats=caveats)
