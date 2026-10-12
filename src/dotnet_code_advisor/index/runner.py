"""The index pass.

Every pass is a full rebuild.  The previous ``graph.db`` (and its -wal/-shm
sidecars) is deleted from disk first rather than emptied with ``DELETE FROM``:
clearing a half-gigabyte database row by row costs more than building a new one.
Nothing else under ``paths.index_dir`` is touched -- ``aspnet_controls.json`` is
an input produced by a separate script, and the designer files are rewritten in
place.

The pass is deliberately two-phase over the source:

  phase 1  declarations, markup, pages, controls  -> symbols / pages / controls
  phase 2  use sites                              -> refs, filtered by the set
                                                     of names phase 1 declared

Phase 2 needs the global name set, which does not exist until phase 1 finishes.
Without that filter every local variable and framework identifier in a large workspace
would become a row that can never resolve to anything in this repository.
"""

from __future__ import annotations

import os
import platform
import stat
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..config import Config
from .. import db as dbm
from .. import inventory as inv
from . import designer as dz
from . import fsmap
from . import markup as mk
from . import resolve as rz
from . import ts_csharp as cs
from . import ts_javascript as js
from .extract import EdgeRow, FileExtract, RefRow, SymbolRow, collapse

Progress = Callable[[str], None]


def _noop(msg: str) -> None:
    pass


SYMBOL_COLS = (
    "file_id, side, kind, name, container, fqn, signature, arity, modifiers, "
    "line_start, line_end, col_start, byte_start, byte_end, is_generated, "
    "is_synthetic, doc_comment"
)
SYMBOL_SQL = f"INSERT INTO symbols ({SYMBOL_COLS}) VALUES ({','.join('?' * 17)})"

REF_SQL = (
    "INSERT INTO refs (file_id, side, name, receiver, ref_kind, line, col, "
    "byte_start, enclosing_sym, resolved_sym, resolution, candidates) "
    "VALUES (?,?,?,?,?,?,?,?,?,NULL,'unresolved',0)"
)

EDGE_SQL = (
    "INSERT INTO edges (src_kind, src_id, dst_kind, dst_id, dst_name, edge_type, "
    "line, evidence, confidence) VALUES (?,?,?,?,?,?,?,?,?)"
)


@dataclass
class PassStats:
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    lines_indexed: int = 0
    symbols: int = 0
    refs: int = 0
    edges: int = 0
    controls: int = 0
    pages: int = 0
    parse_partial: list[str] = field(default_factory=list)
    designer_files: int = 0
    designer_fields: int = 0
    designer_collisions: list[str] = field(default_factory=list)
    designer_template_fields: int = 0
    designer_skipped_wap: int = 0
    elapsed_s: float = 0.0

    def as_meta(self) -> dict[str, str]:
        return {
            "files_indexed": str(self.files_indexed),
            "files_skipped": repr(dict(self.files_skipped)),
            "lines_indexed": str(self.lines_indexed),
            "symbols": str(self.symbols),
            "refs": str(self.refs),
            "edges": str(self.edges),
            "controls": str(self.controls),
            "pages": str(self.pages),
            "parse_partial_count": str(len(self.parse_partial)),
            "designer_files": str(self.designer_files),
            "designer_fields": str(self.designer_fields),
            "designer_collisions": str(len(self.designer_collisions)),
            "designer_template_fields": str(self.designer_template_fields),
            "designer_skipped_wap": str(self.designer_skipped_wap),
        }


class Indexer:
    def __init__(self, cfg: Config, con, *, progress: Progress = _noop) -> None:
        self.cfg = cfg
        self.con = con
        self.progress = progress
        # One case map for the whole pass: the markup context, the designer
        # check and the Web.config lookup all resolve paths written in markup
        # against what is actually on disk, and share the directory listings.
        self.casemap = fsmap.CaseMap()
        self.ctx = mk.MarkupContext(cfg, self.casemap)
        self.stats = PassStats()
        self.file_ids: dict[str, int] = {}
        # markup path -> extract, kept between phases for the designer pass
        self._markup: dict[str, mk.MarkupExtract] = {}
        # markup file_id -> (code-behind path, master path), wired up in
        # link_files() once every file has an id.  Per-instance, not a class
        # attribute: a shared mutable default would leak between index passes.
        self._pending_links: list[tuple[int, Optional[str], Optional[str]]] = []

    # -- phase 0: inventory ----------------------------------------------

    def inventory(self) -> list[inv.FileRecord]:
        t0 = time.time()
        records: list[inv.FileRecord] = []
        for p in inv.walk_files(self.cfg):
            self.stats.files_seen += 1
            try:
                st = p.stat()
            except OSError:
                continue
            rec = inv.describe(p, self.cfg, stat_result=st)
            self._insert_file(rec)
            records.append(rec)
        self.con.commit()
        self.progress(
            f"inventory: {self.stats.files_seen} seen, {len(records)} to index  "
            f"[{time.time() - t0:.1f}s]"
        )
        return records

    def _insert_file(self, rec: inv.FileRecord) -> int:
        fid = self.con.execute(
            "INSERT INTO files (rel_path, side, ext, lang, size_bytes, mtime_ns, "
            "content_hash, line_count, is_generated, indexed, skip_reason, indexed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.rel_path, rec.side, rec.ext, rec.lang, rec.size_bytes, rec.mtime_ns,
                rec.content_hash, rec.line_count, int(rec.is_generated), int(rec.indexed),
                rec.skip_reason, int(time.time()),
            ),
        ).lastrowid
        self.file_ids[rec.rel_path] = fid
        if rec.indexed:
            self.stats.files_indexed += 1
            self.stats.lines_indexed += rec.line_count
        else:
            self.stats.files_skipped[rec.skip_reason or "?"] += 1
        return fid

    # -- phase 1: declarations -------------------------------------------

    def declarations(self, records: list[inv.FileRecord]) -> None:
        t0 = time.time()
        for i, rec in enumerate(records):
            if not rec.indexed:
                continue
            if i and i % 500 == 0:
                self.progress(f"  declarations {i}/{len(records)}")
            try:
                src = inv.read_source(rec.path)
            except OSError:
                continue
            fid = self.file_ids[rec.rel_path]
            if rec.lang == "csharp":
                fx = cs.extract_symbols(src, is_generated=rec.is_generated)
                self._store(fid, rec, fx)
            elif rec.lang == "javascript":
                fx = js.extract_symbols(src)
                self._store(fid, rec, fx)
            elif rec.lang == "markup":
                self._markup_declarations(fid, rec, src)
        self.con.commit()
        self.progress(
            f"declarations: {self.stats.symbols:,} symbols  [{time.time() - t0:.1f}s]"
        )

    def _store(self, fid: int, rec: inv.FileRecord, fx: FileExtract) -> list[int]:
        if fx.parse_status != "ok":
            self.con.execute(
                "UPDATE files SET parse_status=? WHERE file_id=?", (fx.parse_status, fid)
            )
            self.stats.parse_partial.append(rec.rel_path)
        ids: list[int] = []
        for s in fx.symbols:
            cur = self.con.execute(
                SYMBOL_SQL,
                (
                    fid, rec.side, s.kind, s.name, s.container, s.fqn, s.signature,
                    s.arity, s.modifiers, s.line_start, s.line_end, s.col_start,
                    s.byte_start, s.byte_end, int(s.is_generated), int(s.is_synthetic),
                    s.doc_comment,
                ),
            )
            ids.append(cur.lastrowid)
        self.stats.symbols += len(ids)
        for e in fx.edges:
            src_id = ids[e.src_sym] if e.src_sym is not None else fid
            src_kind = "symbol" if e.src_sym is not None else "file"
            self.con.execute(
                EDGE_SQL,
                (src_kind, src_id, e.dst_kind, None, e.dst_name, e.edge_type,
                 e.line, e.evidence, e.confidence),
            )
            self.stats.edges += 1
        return ids

    # -- markup ------------------------------------------------------------

    def _markup_declarations(self, fid: int, rec: inv.FileRecord, src: bytes) -> None:
        ex = mk.scan(src, rec.path, self.ctx)
        self._markup[rec.rel_path] = ex
        if ex.parse_status != "ok":
            self.con.execute(
                "UPDATE files SET parse_status=? WHERE file_id=?", ("partial", fid)
            )
            self.stats.parse_partial.append(rec.rel_path)

        kind = {
            ".aspx": "aspx_page", ".ascx": "ascx_control", ".master": "master_page",
        }.get(rec.ext, "handler")
        page = ex.page
        page_name = (page.inherits if page and page.inherits else rec.path.stem)
        page_sym = self.con.execute(
            SYMBOL_SQL,
            (fid, rec.side, kind, page_name, None, page_name,
             page.directive_text if page else "", None, None,
             page.directive_line if page else 1,
             page.directive_line if page else 1, 1, 0, len(src), 0, 0, None),
        ).lastrowid
        self.stats.symbols += 1

        # inline <script> blocks -- mandatory, not optional
        for blk in ex.inline_scripts:
            jfx = js.extract_symbols(
                mk.neutralise_server_tags(blk.body),
                line_offset=blk.line - 1,
                byte_offset=blk.byte_start,
            )
            for s in jfx.symbols:
                self.con.execute(
                    SYMBOL_SQL,
                    (fid, rec.side, s.kind, s.name, s.container, s.fqn, s.signature,
                     None, None, s.line_start, s.line_end, s.col_start,
                     s.byte_start, s.byte_end, 0, 0, None),
                )
                self.stats.symbols += 1

        if page is None:
            return

        # controls
        for c in ex.controls:
            self.con.execute(
                "INSERT INTO controls (markup_file, codebehind_file, control_ref, "
                "tag_prefix, tag_name, ctl_id, resolved_type, resolution, line, col, "
                "used_in_codebehind, in_template, synth_field_sym) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,0,?,NULL)",
                (fid, None, c.control_ref, c.tag_prefix, c.tag_name, c.ctl_id,
                 c.resolved_type, c.resolution, c.line, c.col, int(c.in_template)),
            )
            self.stats.controls += 1
            if c.resolved_type:
                self.con.execute(
                    EDGE_SQL,
                    ("file", fid, "symbol", None, c.resolved_type.rsplit(".", 1)[-1],
                     "renders_control", c.line,
                     collapse(f"<{c.control_ref} ID=\"{c.ctl_id or ''}\">", 200), "exact"),
                )
                self.stats.edges += 1
            for attr, val in c.server_handlers:
                self.con.execute(
                    EDGE_SQL,
                    ("file", fid, "symbol", None, val, "binds_handler_server", c.line,
                     collapse(f'{attr}="{val}" on <{c.control_ref}>', 200), "exact"),
                )
                self.stats.edges += 1
            for attr, val in c.client_handlers:
                target = mk.js_call_target(val)
                self.con.execute(
                    EDGE_SQL,
                    ("file", fid, "symbol", None, target, "binds_handler_client", c.line,
                     collapse(f'{attr}="{val}"', 200),
                     "exact" if target else "heuristic"),
                )
                self.stats.edges += 1

        for s in ex.scripts:
            self.con.execute(
                EDGE_SQL,
                ("file", fid, "file", None, s.resolved_rel, "includes_script", s.line,
                 collapse(s.raw or "(empty src)", 200), s.confidence),
            )
            self.stats.edges += 1
        for s in ex.styles:
            self.con.execute(
                EDGE_SQL,
                ("file", fid, "file", None, s.resolved_rel, "includes_style", s.line,
                 collapse(s.raw or "(empty href)", 200), s.confidence),
            )
            self.stats.edges += 1
        for ns, line in page.imports:
            self.con.execute(
                EDGE_SQL,
                ("file", fid, "symbol", None, ns, "uses_namespace", line,
                 f'<%@ Import Namespace="{ns}" %>', "exact"),
            )
            self.stats.edges += 1
        for rd in page.registers:
            if rd.src:
                self.con.execute(
                    EDGE_SQL,
                    ("file", fid, "file", None, rd.src, "registers_control", rd.line,
                     collapse(f'<%@ Register Src="{rd.src}" TagPrefix="{rd.tag_prefix}" %>', 200),
                     "exact"),
                )
                self.stats.edges += 1

        # pages row
        code_abs = None
        if page.code_file:
            cand = self.ctx.virtual_to_path(page.code_file, rec.path)
            if cand is not None and cand.is_file():
                code_abs = self.cfg.rel(cand)
        master_abs = None
        if page.master_page:
            cand = self.ctx.virtual_to_path(page.master_page, rec.path)
            if cand is not None and cand.is_file():
                master_abs = self.cfg.rel(cand)
                self.con.execute(
                    EDGE_SQL,
                    ("file", fid, "file", None, master_abs, "master_of", page.directive_line,
                     f'MasterPageFile="{page.master_page}"', "exact"),
                )
                self.stats.edges += 1

        synth, real = dz.should_synthesise(page, rec.path, self.casemap)
        self.con.execute(
            "INSERT INTO pages (markup_file, directive, inherits, code_file, code_attr, "
            "codebehind_file, master_page, master_file, master_type, language, "
            "auto_event_wireup, is_wap, designer_file, synth_path) "
            "VALUES (?,?,?,?,?,NULL,?,NULL,?,?,?,?,?,NULL)",
            (fid, page.directive, page.inherits, page.code_file, page.code_attr,
             page.master_page, page.master_type, page.language,
             page.auto_event_wireup, int(real is not None),
             self.cfg.rel(real) if real else None),
        )
        self.stats.pages += 1
        self._pending_links.append((fid, code_abs, master_abs))

    def link_files(self) -> None:
        """Second-pass wiring of markup -> code-behind / master file ids."""
        for fid, code_abs, master_abs in self._pending_links:
            cb = self.file_ids.get(code_abs) if code_abs else None
            if cb is None and code_abs:
                row = self.con.execute(
                    "SELECT file_id FROM files WHERE rel_path=?", (code_abs,)
                ).fetchone()
                cb = row[0] if row else None
            mf = self.file_ids.get(master_abs) if master_abs else None
            if mf is None and master_abs:
                row = self.con.execute(
                    "SELECT file_id FROM files WHERE rel_path=?", (master_abs,)
                ).fetchone()
                mf = row[0] if row else None
            self.con.execute(
                "UPDATE pages SET codebehind_file=?, master_file=? WHERE markup_file=?",
                (cb, mf, fid),
            )
            self.con.execute(
                "UPDATE controls SET codebehind_file=? WHERE markup_file=?", (cb, fid)
            )
            if cb is not None:
                self.con.execute(
                    EDGE_SQL,
                    ("file", cb, "file", fid, None, "codebehind_of", None,
                     "CodeFile= + Inherits= both verified", "exact"),
                )
                self.stats.edges += 1
        self.con.commit()

    # -- phase 2: refs -----------------------------------------------------

    def references(self, records: list[inv.FileRecord]) -> None:
        t0 = time.time()
        names = {
            r[0]
            for r in self.con.execute("SELECT DISTINCT name FROM symbols")
        }
        names |= {
            r[0]
            for r in self.con.execute(
                "SELECT DISTINCT ctl_id FROM controls WHERE ctl_id IS NOT NULL"
            )
        }
        keep = frozenset(names)
        self.progress(f"  ref name-set: {len(keep):,} distinct names")

        for i, rec in enumerate(records):
            if not rec.indexed or rec.lang not in ("csharp", "javascript", "markup"):
                continue
            if i and i % 500 == 0:
                self.progress(f"  references {i}/{len(records)}")
            fid = self.file_ids.get(rec.rel_path)
            if fid is None:
                continue
            syms = self._file_symbols(fid)
            try:
                src = inv.read_source(rec.path)
            except OSError:
                continue
            if rec.lang == "csharp":
                rows = cs.extract_refs(src, [s for _, s in syms], keep)
            elif rec.lang == "javascript":
                rows = js.extract_refs(src, [s for _, s in syms], keep)
            else:
                rows = self._markup_refs(rec, src, syms, keep)
            self._store_refs(fid, rec.side, rows, [sid for sid, _ in syms])
        self.con.commit()
        self.progress(f"references: {self.stats.refs:,} refs  [{time.time() - t0:.1f}s]")

    def _markup_refs(self, rec, src, syms, keep) -> list[RefRow]:
        """Expression regions and inline scripts inside markup.

        ``<%# Eval("X") %>`` and ``<%= Helpers.ResolvePublicUrl(...) %>`` are
        real code; dropping them would hide behavior from the graph.  Fragments are parsed with the C# parser directly so byte
        offsets stay exact -- no wrapping, no offset arithmetic to get wrong.
        """
        ex = self._markup.get(rec.rel_path)
        if ex is None:
            ex = mk.scan(src, rec.path, self.ctx)
        rows: list[RefRow] = []
        sym_rows = [s for _, s in syms]
        for r in ex.expressions:
            if not r.body.strip():
                continue
            body = r.body
            base = r.body_start
            line0 = r.line - 1
            for rr in cs.extract_refs(body, [], keep):
                rr.line += line0
                rr.byte_start += base
                rr.enclosing = None
                rows.append(rr)
        for blk in ex.inline_scripts:
            rows.extend(
                js.extract_refs(
                    mk.neutralise_server_tags(blk.body), sym_rows, keep,
                    line_offset=blk.line - 1, byte_offset=blk.byte_start,
                )
            )
        return rows

    def _file_symbols(self, fid: int) -> list[tuple[int, SymbolRow]]:
        out: list[tuple[int, SymbolRow]] = []
        for sid, kind, name, bs, be, ls, le, cstart in self.con.execute(
            "SELECT symbol_id, kind, name, byte_start, byte_end, line_start, line_end, "
            "col_start FROM symbols WHERE file_id=? ORDER BY byte_start", (fid,)
        ):
            out.append(
                (sid, SymbolRow(kind=kind, name=name, line_start=ls, line_end=le,
                                col_start=cstart, byte_start=bs, byte_end=be))
            )
        return out

    def _store_refs(self, fid: int, side: str, rows: list[RefRow], sym_ids: list[int]) -> None:
        batch = []
        for r in rows:
            encl = sym_ids[r.enclosing] if r.enclosing is not None and r.enclosing < len(sym_ids) else None
            batch.append(
                (fid, side, r.name, r.receiver, r.ref_kind, r.line, r.col,
                 r.byte_start, encl)
            )
        if batch:
            self.con.executemany(REF_SQL, batch)
            self.stats.refs += len(batch)

    # -- phase 3: designer synthesis --------------------------------------

    def synthesise_designers(self) -> None:
        t0 = time.time()
        out_dir = self.cfg.paths.designer_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        for row in self.con.execute(
            "SELECT p.page_id, p.markup_file, p.inherits, p.codebehind_file, p.is_wap, "
            "f.rel_path FROM pages p JOIN files f ON f.file_id = p.markup_file"
        ).fetchall():
            page_id, mfid, inherits, cb_fid, is_wap, mrel = row
            if is_wap or not inherits:
                self.stats.designer_skipped_wap += int(bool(is_wap))
                continue
            ex = self._markup.get(mrel)
            if ex is None or ex.page is None:
                continue
            members, ns = self._codebehind_members(cb_fid, inherits)
            res = dz.emit(
                ex.page, mrel, ex.controls, members,
                namespace=ns, index_dir=self.cfg.paths.index_dir,
            )
            res.path.write_text(res.text, encoding="utf-8")
            self.stats.designer_files += 1
            self.stats.designer_fields += res.field_count
            self.stats.designer_template_fields += res.in_template_fields
            for c in res.collisions:
                self.stats.designer_collisions.append(f"{mrel}: {c}")
            self.con.execute(
                "UPDATE pages SET synth_path=? WHERE page_id=?",
                (res.path.name, page_id),
            )
            # Synthetic control fields become real symbol rows, flagged.
            for cid in res.emitted_ids:
                ctl = self.con.execute(
                    "SELECT control_id, resolved_type, line, col FROM controls "
                    "WHERE markup_file=? AND ctl_id=? ORDER BY line LIMIT 1",
                    (mfid, cid),
                ).fetchone()
                if ctl is None:
                    continue
                sid = self.con.execute(
                    SYMBOL_SQL,
                    (mfid, self.cfg.side_of(Path(self.cfg.abs_of(mrel))),
                     "control_instance", cid,
                     inherits, f"{inherits}.{cid}",
                     f"protected global::{ctl[1]} {cid};", None, "protected",
                     ctl[2], ctl[2], ctl[3], 0, 0, 0, 1,
                     "Synthesised from markup; ASP.NET generates it at runtime."),
                ).lastrowid
                self.stats.symbols += 1
                self.con.execute(
                    "UPDATE controls SET synth_field_sym=? WHERE control_id=?",
                    (sid, ctl[0]),
                )
        self.con.commit()
        self.progress(
            f"designers: {self.stats.designer_files} files, "
            f"{self.stats.designer_fields:,} fields, "
            f"{len(self.stats.designer_collisions)} CS0102 collisions avoided "
            f"[{time.time() - t0:.1f}s]"
        )

    def _codebehind_members(self, cb_fid: Optional[int], inherits: str):
        if cb_fid is None:
            return set(), None
        rows = self.con.execute(
            "SELECT name, kind, container FROM symbols WHERE file_id=? "
            "AND kind IN ('method','property','field','event','class')", (cb_fid,)
        ).fetchall()
        members = {n for n, k, c in rows if k != "class" and (c or "") == inherits}
        ns = None
        for n, k, c in rows:
            if k == "class" and n == inherits and c:
                ns = c
                break
        return members, ns

    # -- mark which control IDs the code-behind actually touches -----------

    def mark_control_usage(self) -> None:
        self.con.execute(
            "UPDATE controls SET used_in_codebehind = 1 "
            "WHERE codebehind_file IS NOT NULL AND ctl_id IS NOT NULL AND EXISTS ("
            "  SELECT 1 FROM refs r WHERE r.file_id = controls.codebehind_file "
            "  AND r.name = controls.ctl_id)"
        )
        self.con.commit()


def _delete_db(cfg: Config, progress: Progress) -> None:
    """Remove graph.db and its sidecars so the pass starts from an empty file."""
    db = cfg.paths.db_path
    for p in (db, *(db.with_name(db.name + s) for s in ("-wal", "-shm", "-journal"))):
        if not p.exists():
            continue
        try:
            p.unlink()
        except PermissionError:
            # A read-only attribute blocks unlink on Windows; clear it and retry.
            try:
                os.chmod(p, stat.S_IWRITE)
                p.unlink()
            except OSError as e:
                raise RuntimeError(
                    f"cannot delete {p}: {e}. It is probably held open by the MCP "
                    "server or the Roslyn hosts -- stop them and re-run."
                ) from e
        progress(f"removed {p}")


def run(cfg: Config, *, progress: Progress = _noop, roslyn: bool = True) -> PassStats:
    t0 = time.time()
    _delete_db(cfg, progress)
    cfg.ensure_index_dirs()
    con = dbm.connect(cfg.paths.db_path)
    dbm.init_schema(con)

    ix = Indexer(cfg, con, progress=progress)
    records = ix.inventory()
    ix.declarations(records)
    ix.link_files()
    ix.references(records)
    ix.synthesise_designers()
    ix.mark_control_usage()

    progress("resolving...")
    hist = rz.resolve_all(con)
    inh = rz.materialise_inheritance(con)
    progress(f"resolution: {hist}")
    progress(f"inheritance: {inh}")

    # The compiler index (plan Phase 2/3).  It needs the build step's manifest
    # and generated page code; roslyn_index.run records "skipped" with the
    # reason when they are absent, and index_status reports it.
    if roslyn:
        from . import roslyn_index
        roslyn_index.run(cfg, con, progress=progress)
    else:
        dbm.set_meta(con, "roslyn_index", "skipped: --no-roslyn")

    ix.stats.elapsed_s = time.time() - t0
    for k, v in ix.stats.as_meta().items():
        dbm.set_meta(con, k, v)
    dbm.set_meta(con, "built_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    # The index stores workspace-relative paths, so it is portable -- but only to a
    # machine whose config points at the same tree.  Recording the root it was built
    # against lets index_status report a mismatch instead of silently answering from
    # an index that describes a different checkout.
    dbm.set_meta(con, "workspaces_root", cfg.paths.workspaces.as_posix())
    dbm.set_meta(con, "built_on", f"{platform.system()} {platform.release()}")
    # Path references whose case in markup differs from the case on disk.  ASP.NET
    # ignores the difference and so does NTFS; a case-sensitive filesystem does not,
    # so this is the count of links that exist only because of the case map.
    dbm.set_meta(con, "case_resolved_count", ix.casemap.case_resolved)
    dbm.set_meta(con, "elapsed_s", f"{ix.stats.elapsed_s:.1f}")
    dbm.set_meta(con, "refs_by_resolution", repr(hist))
    dbm.set_meta(con, "parse_partial_files", repr(ix.stats.parse_partial[:200]))
    con.commit()
    con.execute("PRAGMA optimize")
    # Fold the write-ahead log back into graph.db.  The index is built on one
    # machine and copied to another; a database whose recent writes still live in
    # a -wal sidecar is only complete if that sidecar is copied too, and at
    # exactly the same moment.  Checkpointing here makes the single file
    # self-contained, so `rsync .index/` is enough.
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return ix.stats


def run_roslyn_only(cfg: Config, *, progress: Progress = _noop) -> dict:
    """Rebuild only the compiler index, on top of an existing, complete graph.db.

    For iterating on the build step or the host without repeating the graph
    pass.  The graph must have finished (it records built_at last); the r*
    tables are emptied and refilled.
    """
    from . import roslyn_index

    con = dbm.connect(cfg.paths.db_path)
    if dbm.get_meta(con, "built_at") is None:
        con.close()
        raise RuntimeError(f"{cfg.paths.db_path} has no completed graph pass; run a full reindex")
    # Recreated, not emptied: the r* shape may have changed since the graph pass,
    # and the graph tables do not depend on it.
    for t in ("rprojects", "rdiagnostics", "rfiles", "rsymbols", "rrefs", "redges", "rsubstitutions",
              "rendpoints", "rhttpcalls", "rapilinks", "rtwins"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(dbm.SCHEMA_ROSLYN)
    dbm.set_meta(con, "schema_fingerprint", dbm.schema_fingerprint())
    con.commit()
    out = roslyn_index.run(cfg, con, progress=progress)
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return out
