"""get_control_binding and get_page_assets.

These are the tools nothing off-the-shelf provides for this codebase: the
markup tag -> .NET type -> code-behind field -> handler chain, for both Website
Projects (fields generated at runtime) and Web Application Projects (real
.designer.cs on disk).
"""

from __future__ import annotations

from typing import Optional

from mcp.types import ToolAnnotations

from ..models import (
    Asset,
    ControlBinding,
    GetControlBindingOut,
    GetPageAssetsOut,
    Loc,
    SymbolRef,
)
from . import common as c
from .common import Ctx

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(
        name="get_control_binding",
        annotations=RO,
        description=(
            "Resolve an ASP.NET WebForms control: markup tag -> .NET type -> the "
            "code-behind field and members that use it -> its server-side and "
            "client-side event handlers. Works for both Website Projects (App/, "
            "CodeFile=, fields generated at runtime) and Web Application Projects "
            "(Demo.DevOps/, CodeBehind=, real .designer.cs). For Website Projects the "
            "field declaration is SYNTHESISED -- it exists only in this index, never on "
            "disk -- and is marked is_synthetic: true. Cite the markup line, not the "
            "synthetic file."
        ),
    )
    def get_control_binding(
        markup_file: str,
        control_id: Optional[str] = None,
        include_handlers: bool = True,
        max: int = 200,
    ) -> GetControlBindingOut:
        p = ctx.resolve_path(markup_file)
        if p is None:
            return GetControlBindingOut(
                status="no_results",
                layer="graph",
                searched_scope=ctx.graph_scope(),
                caveats=[f"No markup file matching '{markup_file}' is indexed."],
            )
        frow = ctx.file_row(p.as_posix())
        if frow is None:
            return GetControlBindingOut(
                status="no_results",
                layer="graph",
                searched_scope=ctx.graph_scope(),
                caveats=[f"'{ctx.rel(p)}' exists on disk but is not in the index."],
            )
        page = ctx.con.execute(
            "SELECT * FROM pages WHERE markup_file=?", (frow["file_id"],)
        ).fetchone()
        if page is None:
            return GetControlBindingOut(
                status="no_results",
                layer="graph",
                searched_scope=f"{ctx.rel(p)} (indexed, no page directive found)",
                caveats=[
                    f"'{ctx.rel(p)}' carries no Page/Control/Master directive, so it has "
                    "no code-behind binding."
                ],
            )

        params: list = [frow["file_id"]]
        where = "markup_file = ?"
        if control_id:
            where += " AND ctl_id = ?"
            params.append(control_id)
        rows = ctx.con.execute(
            f"SELECT * FROM controls WHERE {where} ORDER BY line LIMIT ?",
            params + [min(int(max), ctx.cfg.limits.max_results_hard)],
        ).fetchall()

        cb_row = (
            ctx.con.execute(
                "SELECT * FROM files WHERE file_id=?", (page["codebehind_file"],)
            ).fetchone()
            if page["codebehind_file"]
            else None
        )
        master_row = (
            ctx.con.execute(
                "SELECT * FROM files WHERE file_id=?", (page["master_file"],)
            ).fetchone()
            if page["master_file"]
            else None
        )

        bindings: list[ControlBinding] = []
        templated = 0
        for r in rows:
            b = _binding(ctx, r, page, cb_row, p, include_handlers)
            if b.in_data_bound_template:
                templated += 1
            bindings.append(b)

        caveats: list[str] = []
        if not page["is_wap"]:
            caveats.append(
                "This is a Website Project page: the control fields are SYNTHESISED by "
                "this tool because ASP.NET generates them at runtime. They are marked "
                "is_synthetic and do not exist in any file on disk. Cite the markup line."
            )
        else:
            caveats.append(
                "This is a Web Application Project page: the real *.designer.cs on disk "
                "was used, so codebehind_field.is_synthetic is false."
            )
        if templated:
            caveats.append(
                f"{templated} of these controls sit inside a data-bound template "
                "(ItemTemplate etc.). ASP.NET does NOT generate a page-level field for "
                "those at runtime; this index synthesises one anyway."
            )
        inexact = [
            h.name
            for b in bindings
            for h in b.client_handlers
            if h.confidence != "exact_fqn"
        ]
        if inexact:
            caveats.append(
                f"{len(inexact)} client handler(s) were matched by function name only, "
                "not by full path -- typically a cross-frame call such as "
                "'parent.demo.closeSidePane', whose parent page cannot be determined "
                "from this file. Their 'confidence' is 'unique_name' or 'ambiguous'; "
                "report them as a probable match, not a certain one."
            )
        unres = sum(len(b.unresolved_handlers) for b in bindings)
        if unres:
            caveats.append(
                f"{unres} client handler(s) are inline JavaScript rather than a function "
                "call. Their text is kept verbatim in unresolved_handlers and was not "
                "turned into a guessed function name."
            )

        page_sym = ctx.con.execute(
            f"{c.SYMBOL_SELECT} WHERE s.file_id=? AND s.kind IN "
            "('aspx_page','ascx_control','master_page') LIMIT 1",
            (frow["file_id"],),
        ).fetchone()

        return GetControlBindingOut(
            status="ok" if bindings else "no_results",
            layer="graph",
            searched_scope=(
                f"{ctx.rel(p)}: {len(rows)} control(s) from the markup index; "
                f"{ctx.graph_scope()}"
            ),
            page=c.symbol_from_row(ctx, page_sym, with_snippet=False) if page_sym else None,
            inherits=page["inherits"],
            codebehind=(
                Loc(
                    file=ctx.cfg.abs_of(cb_row["rel_path"]), rel_path=cb_row["rel_path"],
                    line_start=1, line_end=cb_row["line_count"],
                    snippet=f"{cb_row['line_count']} lines",
                )
                if cb_row
                else None
            ),
            master_page=(
                Loc(
                    file=ctx.cfg.abs_of(master_row["rel_path"]),
                    rel_path=master_row["rel_path"],
                    line_start=1, line_end=1, snippet=page["master_page"] or "",
                )
                if master_row
                else None
            ),
            is_web_application_project=bool(page["is_wap"]),
            controls=bindings,
            total_available=len(rows),
            caveats=caveats,
        )

    @mcp.tool(
        name="get_page_assets",
        annotations=RO,
        description=(
            "Everything a WebForms page pulls in: its master page, code-behind, "
            "registered user controls, script and stylesheet includes, imported "
            "namespaces, and its server and client event handlers. Script targets built "
            "at runtime from variables are reported with confidence 'heuristic' and no "
            "resolved target rather than a guess."
        ),
    )
    def get_page_assets(markup_file: str) -> GetPageAssetsOut:
        p = ctx.resolve_path(markup_file)
        if p is None:
            return GetPageAssetsOut(
                status="no_results", layer="graph",
                searched_scope=ctx.graph_scope(),
                caveats=[f"No markup file matching '{markup_file}' is indexed."],
            )
        frow = ctx.file_row(p.as_posix())
        if frow is None:
            return GetPageAssetsOut(
                status="no_results", layer="graph",
                searched_scope=ctx.graph_scope(),
                caveats=[f"'{ctx.rel(p)}' is not in the index."],
            )
        fid = frow["file_id"]
        page = ctx.con.execute("SELECT * FROM pages WHERE markup_file=?", (fid,)).fetchone()

        edges = ctx.con.execute(
            "SELECT * FROM edges WHERE src_kind='file' AND src_id=? ORDER BY edge_type, line",
            (fid,),
        ).fetchall()

        scripts, styles, imports, user_controls = [], [], [], []
        server_h, client_h = [], []
        heuristic = 0
        for e in edges:
            if e["edge_type"] == "includes_script":
                scripts.append(_asset(e, "script"))
                heuristic += int(e["confidence"] == "heuristic")
            elif e["edge_type"] == "includes_style":
                styles.append(_asset(e, "style"))
            elif e["edge_type"] == "uses_namespace":
                imports.append(e["dst_name"] or e["evidence"])
            elif e["edge_type"] == "registers_control":
                tgt = ctx.con.execute(
                    "SELECT rel_path FROM files WHERE rel_path LIKE ?",
                    (f"%{(e['dst_name'] or '').lstrip('~/')}",),
                ).fetchone()
                if tgt:
                    user_controls.append(
                        Loc(file=ctx.cfg.abs_of(tgt["rel_path"]), rel_path=tgt["rel_path"],
                            line_start=1, line_end=1, snippet=e["evidence"])
                    )
            elif e["edge_type"] == "binds_handler_server" and e["dst_name"]:
                s = _handler_symbol(ctx, e["dst_name"], page, frow, e)
                if s:
                    server_h.append(s)
            elif e["edge_type"] == "binds_handler_client" and e["dst_name"]:
                s = _js_symbol(ctx, e["dst_name"], e, frow)
                if s:
                    client_h.append(s)

        master_row = (
            ctx.con.execute("SELECT * FROM files WHERE file_id=?", (page["master_file"],)).fetchone()
            if page and page["master_file"] else None
        )
        cb_row = (
            ctx.con.execute("SELECT * FROM files WHERE file_id=?", (page["codebehind_file"],)).fetchone()
            if page and page["codebehind_file"] else None
        )
        page_sym = ctx.con.execute(
            f"{c.SYMBOL_SELECT} WHERE s.file_id=? AND s.kind IN "
            "('aspx_page','ascx_control','master_page') LIMIT 1", (fid,),
        ).fetchone()

        caveats = []
        if heuristic:
            caveats.append(
                f"{heuristic} script include(s) build their URL at runtime from "
                "variables. They are listed with confidence 'heuristic' and no resolved "
                "target: the actual file cannot be determined from the source."
            )
        return GetPageAssetsOut(
            status="ok",
            layer="graph",
            searched_scope=f"{ctx.rel(p)}; {ctx.graph_scope()}",
            page=c.symbol_from_row(ctx, page_sym, with_snippet=False) if page_sym else None,
            master_page=(
                Loc(file=ctx.cfg.abs_of(master_row["rel_path"]),
                    rel_path=master_row["rel_path"],
                    line_start=1, line_end=1, snippet=page["master_page"] or "")
                if master_row else None
            ),
            codebehind=(
                Loc(file=ctx.cfg.abs_of(cb_row["rel_path"]), rel_path=cb_row["rel_path"],
                    line_start=1, line_end=cb_row["line_count"], snippet="")
                if cb_row else None
            ),
            user_controls=user_controls,
            scripts=scripts,
            styles=styles,
            imports=imports,
            server_handlers=server_h,
            client_handlers=client_h,
            caveats=caveats,
        )


# --- helpers ---------------------------------------------------------------


def _asset(e, kind: str) -> Asset:
    return Asset(
        kind=kind,
        raw=e["evidence"],
        resolved_rel=e["dst_name"],
        confidence=e["confidence"],
        line=e["line"] or 0,
    )


def _binding(ctx: Ctx, r, page, cb_row, markup_path, include_handlers: bool) -> ControlBinding:
    markup_loc = Loc(
        file=markup_path.as_posix(),
        rel_path=ctx.rel(markup_path),
        line_start=r["line"],
        line_end=r["line"],
        col_start=r["col"],
        snippet=c.snippet_for(markup_path.as_posix(), r["line"], ctx.cfg),
    )

    field_sym = None
    if r["synth_field_sym"]:
        row = ctx.con.execute(
            f"{c.SYMBOL_SELECT} WHERE s.symbol_id=?", (r["synth_field_sym"],)
        ).fetchone()
        if row:
            field_sym = c.symbol_from_row(ctx, row, with_snippet=False)
            field_sym.loc = markup_loc
    elif page["is_wap"] and cb_row and r["ctl_id"]:
        row = ctx.con.execute(
            f"{c.SYMBOL_SELECT} WHERE s.name=? AND s.kind='field' AND f.rel_path LIKE ? LIMIT 1",
            (r["ctl_id"], f"%{markup_path.name}.designer.cs"),
        ).fetchone()
        if row:
            field_sym = c.symbol_from_row(ctx, row)

    type_decl = None
    if r["resolved_type"]:
        simple = r["resolved_type"].rsplit(".", 1)[-1]
        row = ctx.con.execute(
            f"{c.SYMBOL_SELECT} WHERE s.name=? AND s.kind IN ('class','interface') "
            "ORDER BY s.is_generated LIMIT 1",
            (simple,),
        ).fetchone()
        if row:
            type_decl = c.symbol_from_row(ctx, row, with_snippet=False)

    uses: list[Loc] = []
    if r["ctl_id"] and page["codebehind_file"]:
        for u in ctx.con.execute(
            "SELECT r.line, r.col, f.rel_path FROM refs r "
            "JOIN files f ON f.file_id=r.file_id "
            "WHERE r.file_id=? AND r.name=? ORDER BY r.line LIMIT 50",
            (page["codebehind_file"], r["ctl_id"]),
        ):
            uses.append(
                Loc(file=ctx.cfg.abs_of(u["rel_path"]), rel_path=u["rel_path"],
                    line_start=u["line"], line_end=u["line"], col_start=u["col"],
                    snippet=c.snippet_for(ctx.cfg.abs_of(u["rel_path"]), u["line"], ctx.cfg))
            )

    server_h: list[SymbolRef] = []
    client_h: list[SymbolRef] = []
    unresolved: list[str] = []
    if include_handlers:
        for e in ctx.con.execute(
            "SELECT * FROM edges WHERE src_kind='file' AND src_id=? AND line=? "
            "AND edge_type IN ('binds_handler_server','binds_handler_client')",
            (r["markup_file"], r["line"]),
        ):
            if e["edge_type"] == "binds_handler_server":
                s = _handler_symbol(ctx, e["dst_name"], page, None, e)
                if s:
                    server_h.append(s)
                else:
                    unresolved.append(e["evidence"])
            else:
                if e["dst_name"]:
                    s = _js_symbol(ctx, e["dst_name"], e, None)
                    if s:
                        client_h.append(s)
                    else:
                        unresolved.append(e["evidence"])
                else:
                    unresolved.append(e["evidence"])

    return ControlBinding(
        control_id=r["ctl_id"],
        tag=r["control_ref"],
        resolved_type=r["resolved_type"],
        type_resolution=r["resolution"],
        in_data_bound_template=bool(r["in_template"]),
        markup_loc=markup_loc,
        type_decl=type_decl,
        codebehind_field=field_sym,
        codebehind_uses=uses,
        server_handlers=server_h,
        client_handlers=client_h,
        unresolved_handlers=unresolved,
    )


def _handler_symbol(ctx: Ctx, name: Optional[str], page, frow, e) -> Optional[SymbolRef]:
    if not name:
        return None
    cb = page["codebehind_file"] if page is not None else None
    if cb:
        row = ctx.con.execute(
            f"{c.SYMBOL_SELECT} WHERE s.file_id=? AND s.name=? AND s.kind='method' LIMIT 1",
            (cb, name),
        ).fetchone()
        if row:
            return c.symbol_from_row(ctx, row, with_snippet=False)
    row = ctx.con.execute(
        f"{c.SYMBOL_SELECT} WHERE s.name=? AND s.kind='method' LIMIT 2", (name,)
    ).fetchone()
    return c.symbol_from_row(ctx, row, with_snippet=False) if row else None


def _js_symbol(ctx: Ctx, dotted: str, e, frow) -> Optional[SymbolRef]:
    """Resolve an OnClient* target to an indexed JavaScript function.

    An exact fqn match is exact.  Falling back to the last segment is not: it is
    how ``parent.demo.closeSidePane`` (a cross-frame call, whose parent page is
    not knowable from this file) reaches ``demo.closeSidePane``.  Those results
    are returned with confidence 'unique_name', or 'ambiguous' when several
    functions share the name, so the answer is never reported as certain.
    """
    row = ctx.con.execute(
        f"{c.SYMBOL_SELECT} WHERE s.fqn=? AND s.kind IN ('js_function','js_method') LIMIT 1",
        (dotted,),
    ).fetchone()
    if row is not None:
        sym = c.symbol_from_row(ctx, row, with_snippet=False)
        sym.confidence = "exact_fqn"
        return sym

    simple = dotted.rsplit(".", 1)[-1]
    rows = ctx.con.execute(
        f"{c.SYMBOL_SELECT} WHERE s.name=? AND s.kind IN ('js_function','js_method') LIMIT 5",
        (simple,),
    ).fetchall()
    if not rows:
        return None
    sym = c.symbol_from_row(ctx, rows[0], with_snippet=False)
    sym.confidence = "unique_name" if len(rows) == 1 else "ambiguous"
    return sym
