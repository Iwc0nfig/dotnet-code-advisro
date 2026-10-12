"""Shared plumbing for the tool layer.

Two things here matter more than the rest:

* :func:`translate_location` maps a hit inside a synthesised designer partial
  back to the markup line that produced it.  Those files exist only in
  ``.index/`` -- telling a user to open one would be telling them to open a file
  that does not exist.
* :func:`scope_of` builds the ``searched_scope`` string.  When a tool finds
  nothing, that string is the difference between a grounded "not found" and an
  invitation to guess.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from ..config import Config
from .. import db as dbm
from ..models import Loc, SymbolRef
from ..roslyn.client import RoslynPool, RoslynError, RoslynUnavailable

KNOWN_LIMITATIONS = [
    "No source-control history is queried: design intent and change history are not verified.",
    "Graph control fields are synthesized from markup. Cite markup, not generated files.",
    "Template controls have synthesized graph fields but no runtime page-level field.",
    "JavaScript, reflection, SQL and configuration dispatch are not compiler-exact references.",
    "HTML, CSS, SQL, configuration and Delphi files are text-searchable, without a symbol graph.",
    "Server-comment tags and directives are excluded because they do not run.",
    "The structural C# parser may miss declarations using contextual keywords as identifiers.",
]
LIVE_ROSLYN_ONLY_LIMITATION = (
    "No persisted compiler index has been built. Live hosts cover only configured solutions; "
    "projects absent from those solutions need the workspace build and persisted compiler index."
)
NOT_INDEXED = [
    "HTML, CSS, configuration, SQL and Delphi have no symbol graph; use search_text.",
    "Configured excluded directories, suffixes, vendor JavaScript and oversized files.",
]


@dataclass
class Ctx:
    cfg: Config
    pool: RoslynPool
    _local: threading.local = field(default_factory=threading.local, repr=False)

    @classmethod
    def create(cls, cfg: Config) -> "Ctx":
        if not cfg.paths.db_path.is_file():
            raise FileNotFoundError(
                f"{cfg.paths.db_path} not found. Run: python scripts/reindex.py"
            )
        return cls(cfg=cfg, pool=RoslynPool(cfg))

    @property
    def con(self) -> sqlite3.Connection:
        """A read-only connection owned by the calling thread.

        The MCP server dispatches synchronous tool functions onto a worker
        thread pool (``anyio.to_thread.run_sync``), and a sqlite3 connection may
        only be used from the thread that created it.  One connection per thread
        keeps that legal without relaxing ``check_same_thread``, and read-only
        connections cannot contend with each other.
        """
        con = getattr(self._local, "con", None)
        if con is None:
            con = dbm.connect(self.cfg.paths.db_path, read_only=True)
            self._local.con = con
        return con

    # -- scope strings ----------------------------------------------------

    def graph_scope(self) -> str:
        m = dbm.all_meta(self.con)
        return (
            f"structural graph index: {m.get('files_indexed', '?')} files / "
            f"{m.get('lines_indexed', '?')} lines across "
            f"{', '.join(self.cfg.paths.roots)}, built {m.get('built_at', 'unknown')}"
        )

    def roslyn_scope(self, side: str) -> str:
        host = self.pool.get(side)
        st = host.state
        sln = Path(st.solution).name if st.solution else (
            (getattr(self.cfg.paths, f"{side}_solution").name
             if getattr(self.cfg.paths, f"{side}_solution") else "no solution configured")
        )
        extra = ""
        if side == "net48" and self.cfg.paths.website_root is not None:
            extra = f" + PrimaryWebsite.Website synthetic project ({st.website_documents} documents)"
        return (
            f"Roslyn {side}: {sln} ({st.projects_loaded}/{st.projects_total} projects "
            f"loaded, {st.failure_count} workspace failures){extra}"
        )

    def both_scopes(self, sides: list[str]) -> str:
        return "; ".join([self.graph_scope()] + [self.roslyn_scope(s) for s in sides])

    # -- file helpers ------------------------------------------------------

    def rel(self, path: str | Path) -> str:
        return self.cfg.rel(path)

    def file_row(self, path: str) -> Optional[sqlite3.Row]:
        """The ``files`` row for an absolute or workspace-relative path.

        The index is keyed on the workspace-relative path, so anything absolute is
        reduced to that first.  There is deliberately no case-insensitive fallback:
        on a case-sensitive filesystem ``lower(rel_path)=lower(?)`` can match a
        *different* file than the one asked for, and answering confidently about
        the wrong file is the failure this whole design exists to prevent.
        """
        p = str(path).replace("\\", "/")
        rel = self.cfg.rel(p) if Path(p).is_absolute() else p.lstrip("/")
        return self.con.execute(
            "SELECT * FROM files WHERE rel_path=?", (rel,)
        ).fetchone()

    def resolve_path(self, given: str) -> Optional[Path]:
        """Resolve a unique file within configured roots; never open an outside path."""
        g = str(given).replace("\\", "/").strip()
        path = Path(g)
        if path.is_absolute():
            return path.resolve() if self.cfg.contains_source(path) and path.is_file() else None
        candidates = [self.cfg.paths.workspaces / g]
        if self.cfg.paths.website_root is not None:
            candidates.append(self.cfg.paths.website_root / g)
        matches = {p.resolve() for p in candidates if self.cfg.contains_source(p) and p.is_file()}
        if len(matches) == 1:
            return matches.pop()
        if matches or ".." in path.parts:
            return None
        # Suffix lookup must be unique and does not treat input as an SQL wildcard.
        rows = self.con.execute("SELECT rel_path FROM files WHERE rel_path=? OR substr(rel_path, -?)=?",
                                (g, len(g) + 1, "/" + g)).fetchall()
        matches = {Path(self.cfg.abs_of(row["rel_path"])).resolve() for row in rows
                   if self.cfg.contains_source(self.cfg.abs_of(row["rel_path"]))
                   and Path(self.cfg.abs_of(row["rel_path"])).is_file()}
        return matches.pop() if len(matches) == 1 else None


# --- synthetic designer translation ----------------------------------------


SYNTH_SUFFIX = ".synth.designer.cs"


def is_synthetic_path(cfg: Config, path: str) -> bool:
    """Whether *path* names a synthesised designer partial.

    Accepts either the absolute path (what a Roslyn location carries) or the bare
    basename the index stores in ``pages.synth_path``.
    """
    p = str(path).replace("\\", "/")
    if p.startswith(cfg.paths.designer_dir.as_posix()):
        return True
    return "/" not in p and p.endswith(SYNTH_SUFFIX)


def synth_name(path: str) -> str:
    """The stored ``pages.synth_path`` key for a synthetic file path."""
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


def translate_location(ctx: Ctx, file: str, line: int, snippet: str = "") -> tuple[Loc, bool]:
    """Return a citable location, plus whether it came from a synthetic file.

    A hit inside ``.index/designer/*.synth.designer.cs`` is rewritten to the
    markup tag that produced the field, because the synthetic file does not
    exist on disk and must never be handed to a user as somewhere to look.
    """
    fpath = str(file).replace("\\", "/")
    if not is_synthetic_path(ctx.cfg, fpath):
        return (
            Loc(
                file=fpath,
                rel_path=ctx.rel(fpath),
                line_start=line,
                line_end=line,
                snippet=snippet,
            ),
            False,
        )

    row = ctx.con.execute(
        "SELECT f.rel_path FROM pages p JOIN files f ON f.file_id=p.markup_file "
        "WHERE p.synth_path=?",
        (synth_name(fpath),),
    ).fetchone()
    if row is None:
        return (
            Loc(
                file=fpath,
                rel_path="(synthesised, not on disk)",
                line_start=line,
                line_end=line,
                snippet=snippet,
            ),
            True,
        )

    ctl_line = _synth_line_to_markup_line(ctx, fpath, snippet)
    return (
        Loc(
            file=ctx.cfg.abs_of(row["rel_path"]),
            rel_path=row["rel_path"],
            line_start=ctl_line or 1,
            line_end=ctl_line or 1,
            snippet=snippet,
        ),
        True,
    )


def _synth_line_to_markup_line(ctx: Ctx, synth_path: str, snippet: str) -> Optional[int]:
    """The emitted field carries its markup line in the doc comment; fall back to
    a lookup by field name."""
    name = snippet.strip().rstrip(";").split(" ")[-1] if snippet else ""
    if not name:
        return None
    row = ctx.con.execute(
        "SELECT c.line FROM controls c JOIN pages p ON p.markup_file=c.markup_file "
        "WHERE p.synth_path=? AND c.ctl_id=? ORDER BY c.line LIMIT 1",
        (synth_name(synth_path), name.lstrip("@")),
    ).fetchone()
    return row["line"] if row else None


# --- reading source ---------------------------------------------------------


@lru_cache(maxsize=256)
def _read_lines(path: str) -> list[str]:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    return data.decode("utf-8-sig", errors="replace").splitlines()


def snippet_for(path: str, line: int, cfg: Config | None = None) -> str:
    if cfg is not None and (not hasattr(cfg, "contains_source") or not cfg.contains_source(path)):
        return ""
    lines = _read_lines(path)
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()[:400]
    return ""


def span_text(path: str, start: int, end: int, limit: int = 400) -> str:
    lines = _read_lines(path)
    if not lines:
        return ""
    s = max(1, start)
    e = min(len(lines), max(start, end))
    return "\n".join(lines[s - 1 : e])[: limit * 20]


# --- symbol rows ------------------------------------------------------------


def symbol_from_row(ctx: Ctx, row: sqlite3.Row, *, with_snippet: bool = True) -> SymbolRef:
    path = ctx.cfg.abs_of(row["rel_path"])
    synthetic = bool(row["is_synthetic"])
    snippet = row["signature"] or ""
    if with_snippet and not synthetic:
        snippet = snippet_for(path, row["line_start"], ctx.cfg) or snippet
    return SymbolRef(
        name=row["name"],
        kind=row["kind"],
        fqn=row["fqn"],
        container=row["container"],
        signature=row["signature"],
        side=row["side"],
        loc=Loc(
            file=path,
            rel_path=row["rel_path"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            col_start=row["col_start"],
            snippet=snippet,
        ),
        is_generated=bool(row["is_generated"]),
        is_synthetic=synthetic,
    )


SYMBOL_SELECT = (
    "SELECT s.*, f.rel_path FROM symbols s JOIN files f ON f.file_id = s.file_id"
)


# --- Roslyn access ----------------------------------------------------------


def try_roslyn(ctx: Ctx, side: str, method: str, params: dict[str, Any]) -> tuple[Any, Optional[str]]:
    """Call a host.  Returns ``(result, unavailable_reason)``.

    Never raises for an unavailable host: the caller turns the reason into
    ``status: 'unavailable'`` with no results, rather than silently falling back
    to the graph and presenting a weaker answer as if it were a compiler answer.
    """
    host = ctx.pool.get(side)
    try:
        if host.proc is None or host.proc.poll() is not None:
            if not host.available():
                # Not configured, or never built: a stated absence, not a crash.
                return None, host.unavailable_reason()
            return None, (
                f"Roslyn {side} host is not running. Call index_status for details."
            )
        return host.call(method, params), None
    except RoslynUnavailable as exc:
        return None, str(exc)
    except RoslynError as exc:
        if exc.code in ("not_loaded", "timeout"):
            return None, str(exc)
        raise
