"""read_file_range and search_text.

``read_file_range`` is the grounding primitive: the graph tells you WHERE code
is, this is how you find out what it says.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional

from mcp.types import ToolAnnotations

from ..inventory import INDEXED_EXTS, is_excluded_dir, is_vendor_js, walk_paths
from ..models import Loc, ReadFileOut, SearchHit, SearchTextOut
from . import common as c
from .common import Ctx

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

SEARCHABLE_EXTS = set(INDEXED_EXTS) | {
    ".html", ".htm", ".css", ".config", ".sql", ".pas", ".txt", ".md", ".json", ".xml",
    ".csproj", ".sln", ".master", ".resx",
}


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(
        name="read_file_range",
        annotations=RO,
        description=(
            "Read exact lines from a file, with line numbers. This is how you ground a "
            "description of behaviour: every other tool tells you where code is, this "
            "tells you what it says. Reads any text file in the workspace, including "
            "ones with no symbol graph (HTML, CSS, SQL, .config)."
        ),
    )
    def read_file_range(
        file: str, line_start: int = 1, line_end: Optional[int] = None
    ) -> ReadFileOut:
        # A synthesised designer partial is not a file; say so instead of
        # inventing a path the user could open.
        if c.is_synthetic_path(ctx.cfg, file):
            row = ctx.con.execute(
                "SELECT f.rel_path FROM pages p JOIN files f "
                "ON f.file_id=p.markup_file WHERE p.synth_path=?",
                (c.synth_name(file),),
            ).fetchone()
            where = (
                f" It was generated from {row['rel_path']}; read that markup file instead."
                if row
                else ""
            )
            return ReadFileOut(
                status="no_results",
                layer="filesystem",
                searched_scope="synthesised designer partials (in-memory only)",
                file=file,
                caveats=[
                    "That path is a designer partial this tool synthesises in memory "
                    "because ASP.NET generates it at runtime. It does not exist on disk "
                    "and must not be cited as a file." + where
                ],
            )

        p = ctx.resolve_path(file)
        if p is None:
            return ReadFileOut(
                status="no_results",
                layer="filesystem",
                searched_scope=f"filesystem under {ctx.cfg.paths.workspaces}",
                file=file,
                caveats=[
                    f"No file matching '{file}' exists under the workspace root "
                    f"{ctx.cfg.paths.workspaces}."
                ],
            )

        try:
            data = p.read_bytes()
        except OSError as exc:
            return ReadFileOut(
                status="no_results", layer="filesystem",
                searched_scope=str(p), file=str(p),
                caveats=[f"Could not read: {exc}"],
            )

        lines = data.decode("utf-8-sig", errors="replace").splitlines()
        total = len(lines)
        start = 1 if line_start < 1 else line_start
        cap = ctx.cfg.limits.read_file_max_lines
        end = total if line_end is None else min(int(line_end), total)
        if end < start:
            end = start
        truncated = (end - start + 1) > cap
        if truncated:
            end = start + cap - 1
        chunk = lines[start - 1 : end]
        numbered = [f"{start + i:>6}  {t}" for i, t in enumerate(chunk)]

        caveats = []
        if truncated:
            caveats.append(
                f"Showing lines {start}-{end} of {total}; the per-call cap is {cap} lines."
            )
        return ReadFileOut(
            status="ok",
            layer="filesystem",
            searched_scope=f"{ctx.rel(p)} ({total} lines)",
            file=p.as_posix(),
            rel_path=ctx.rel(p),
            line_start=start,
            line_end=end,
            total_lines=total,
            lines=numbered,
            text="\n".join(chunk),
            truncated=truncated,
            total_available=total,
            caveats=caveats,
        )

    @mcp.tool(
        name="search_text",
        annotations=RO,
        description=(
            "Literal or regex text search across the workspace, with the standard "
            "exclusions applied. This is a TEXT match and nothing more -- it proves the "
            "characters occur at that line, not that a symbol is used. Use it for files "
            "that have no symbol graph (HTML, CSS, SQL, .config) or as a fallback."
        ),
    )
    def search_text(
        pattern: str,
        regex: bool = False,
        case_sensitive: bool = False,
        path_glob: Optional[str] = None,
        extensions: Optional[list[str]] = None,
        side: Literal["net48", "net8", "both"] = "both",
        max: int = 100,
    ) -> SearchTextOut:
        limit = min(int(max), ctx.cfg.limits.max_results_hard)
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            rx = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as exc:
            return SearchTextOut(
                status="no_results", layer="filesystem",
                searched_scope="(pattern not compiled)", pattern=pattern,
                caveats=[f"Invalid regular expression: {exc}"],
            )

        exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in (extensions or [])}
        root = ctx.cfg.paths.workspaces
        glob_rx = re.compile(path_glob.replace(".", r"\.").replace("*", ".*"), re.IGNORECASE) if path_glob else None

        hits: list[SearchHit] = []
        files_searched = 0
        truncated = False

        import os

        for fp in walk_paths(ctx.cfg):
            ext = fp.suffix.lower()
            if exts:
                if ext not in exts:
                    continue
            elif ext not in SEARCHABLE_EXTS:
                continue
            if ext == ".js" and is_vendor_js(fp, ctx.cfg):
                continue
            rel = ctx.rel(fp)
            if side != "both" and ctx.cfg.side_of(fp) != side:
                continue
            if glob_rx and not glob_rx.search(rel):
                continue
            try:
                if fp.stat().st_size > ctx.cfg.index.max_file_bytes:
                    continue
                text = fp.read_bytes().decode("utf-8-sig", errors="replace")
            except OSError:
                continue
            files_searched += 1
            if not rx.search(text):
                continue
            for i, ln in enumerate(text.splitlines(), 1):
                m = rx.search(ln)
                if not m:
                    continue
                if len(hits) >= limit:
                    truncated = True
                    break
                hits.append(
                    SearchHit(
                        loc=Loc(
                            file=fp.as_posix(), rel_path=rel,
                            line_start=i, line_end=i,
                            col_start=m.start() + 1,
                            snippet=ln.strip()[:400],
                        ),
                        match=m.group(0)[:200],
                    )
                )
            if truncated:
                break

        by_file: dict[str, int] = {}
        for h in hits:
            by_file[h.loc.rel_path] = by_file.get(h.loc.rel_path, 0) + 1

        return SearchTextOut(
            status="ok" if hits else "no_results",
            layer="filesystem",
            searched_scope=(
                f"text search over {files_searched} files under {root} "
                f"(configured exclusions applied; max file bytes={ctx.cfg.index.max_file_bytes})"
            ),
            pattern=pattern,
            hits=hits,
            files_searched=files_searched,
            by_file=by_file,
            truncated=truncated,
            caveats=[
                "This is a text match. It does not prove the symbol is used here -- the "
                "characters may occur in a comment, a string, or an unrelated identifier."
            ],
        )
