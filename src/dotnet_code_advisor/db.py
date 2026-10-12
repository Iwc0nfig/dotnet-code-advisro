"""SQLite schema, connection handling and FTS5 probing.

The graph database is the only file this package writes outside ``.index/logs``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

USER_VERSION = 3

PRAGMAS = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;
"""

# --- core tables -----------------------------------------------------------

SCHEMA_CORE = """
CREATE TABLE IF NOT EXISTS files (
  file_id       INTEGER PRIMARY KEY,
  -- Relative to paths.workspaces, forward slashes, original case.  Nothing in
  -- this database stores an absolute path: the index is built on one machine and
  -- served from another, so an absolute path would pin it to the machine that
  -- produced it.  The tool layer reconstitutes one via Config.abs_of() at the
  -- moment it hands a location to a caller.  Enforced by tests/test_portable_index.py.
  rel_path      TEXT    NOT NULL UNIQUE,
  side          TEXT    NOT NULL CHECK (side IN ('net48','net8')),
  ext           TEXT    NOT NULL,
  lang          TEXT    NOT NULL CHECK (lang IN ('csharp','javascript','markup','razor','unknown')),
  size_bytes    INTEGER NOT NULL,
  mtime_ns      INTEGER NOT NULL,
  content_hash  TEXT    NOT NULL,
  line_count    INTEGER NOT NULL,
  is_generated  INTEGER NOT NULL DEFAULT 0,
  indexed       INTEGER NOT NULL DEFAULT 1,
  skip_reason   TEXT,
  parse_status  TEXT    NOT NULL DEFAULT 'ok'
                        CHECK (parse_status IN ('ok','error','partial')),
  indexed_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_files_side ON files(side);
CREATE INDEX IF NOT EXISTS ix_files_lang ON files(lang);
CREATE INDEX IF NOT EXISTS ix_files_hash ON files(content_hash);
-- rel_path is UNIQUE, which already provides the lookup index.

CREATE TABLE IF NOT EXISTS symbols (
  symbol_id     INTEGER PRIMARY KEY,
  file_id       INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  side          TEXT    NOT NULL,
  kind          TEXT    NOT NULL CHECK (kind IN (
                  'namespace','class','struct','interface','enum','record','delegate',
                  'method','constructor','property','field','event','indexer',
                  'js_function','js_class','js_method','js_var',
                  'aspx_page','ascx_control','master_page','control_instance','handler')),
  name          TEXT    NOT NULL,
  container     TEXT,
  fqn           TEXT,
  signature     TEXT,
  arity         INTEGER,
  modifiers     TEXT,
  line_start    INTEGER NOT NULL,
  line_end      INTEGER NOT NULL,
  col_start     INTEGER NOT NULL,
  byte_start    INTEGER NOT NULL,
  byte_end      INTEGER NOT NULL,
  is_generated  INTEGER NOT NULL DEFAULT 0,
  is_synthetic  INTEGER NOT NULL DEFAULT 0,
  doc_comment   TEXT
);
CREATE INDEX IF NOT EXISTS ix_sym_name      ON symbols(name);
CREATE INDEX IF NOT EXISTS ix_sym_name_kind ON symbols(name, kind);
CREATE INDEX IF NOT EXISTS ix_sym_fqn       ON symbols(fqn);
CREATE INDEX IF NOT EXISTS ix_sym_file      ON symbols(file_id);
CREATE INDEX IF NOT EXISTS ix_sym_container ON symbols(container);
CREATE INDEX IF NOT EXISTS ix_sym_side_name ON symbols(side, name);

CREATE TABLE IF NOT EXISTS refs (
  ref_id        INTEGER PRIMARY KEY,
  file_id       INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  side          TEXT    NOT NULL,
  name          TEXT    NOT NULL,
  receiver      TEXT,
  ref_kind      TEXT    NOT NULL CHECK (ref_kind IN (
                  'call','member_access','identifier','new','type_use',
                  'using','import','script_src','control_use','handler_bind')),
  line          INTEGER NOT NULL,
  col           INTEGER NOT NULL,
  byte_start    INTEGER NOT NULL,
  enclosing_sym INTEGER REFERENCES symbols(symbol_id) ON DELETE SET NULL,
  resolved_sym  INTEGER REFERENCES symbols(symbol_id) ON DELETE SET NULL,
  resolution    TEXT    NOT NULL DEFAULT 'unresolved' CHECK (resolution IN (
                  'exact_fqn','same_file','same_type','same_namespace',
                  'unique_name','markup_binding','unresolved','ambiguous')),
  candidates    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ref_name     ON refs(name);
CREATE INDEX IF NOT EXISTS ix_ref_resolved ON refs(resolved_sym);
CREATE INDEX IF NOT EXISTS ix_ref_file     ON refs(file_id);
CREATE INDEX IF NOT EXISTS ix_ref_encl     ON refs(enclosing_sym);
CREATE INDEX IF NOT EXISTS ix_ref_kind     ON refs(ref_kind);

CREATE TABLE IF NOT EXISTS edges (
  edge_id    INTEGER PRIMARY KEY,
  src_kind   TEXT NOT NULL CHECK (src_kind IN ('file','symbol')),
  src_id     INTEGER NOT NULL,
  dst_kind   TEXT NOT NULL CHECK (dst_kind IN ('file','symbol')),
  -- Nullable: 7.6 requires includes_script edges whose target is computed at
  -- runtime to be recorded with no destination rather than guessed.
  dst_id     INTEGER,
  -- The destination as written in source, kept so an edge can be resolved after
  -- all files are parsed.  'evidence' stays the verbatim snippet it is spec'd as.
  -- Where the destination is a file rather than a symbol (master_of), it is
  -- stored workspace-relative, like files.rel_path.
  dst_name   TEXT,
  edge_type  TEXT NOT NULL CHECK (edge_type IN (
               'inherits','implements','codebehind_of','master_of','registers_control',
               'renders_control','binds_handler_server','binds_handler_client',
               'includes_script','includes_style','uses_namespace','project_ref',
               'migration_twin')),
  line       INTEGER,
  evidence   TEXT NOT NULL,
  confidence TEXT NOT NULL DEFAULT 'exact' CHECK (confidence IN ('exact','heuristic'))
);
CREATE INDEX IF NOT EXISTS ix_edge_src  ON edges(src_kind, src_id, edge_type);
CREATE INDEX IF NOT EXISTS ix_edge_dst  ON edges(dst_kind, dst_id, edge_type);
CREATE INDEX IF NOT EXISTS ix_edge_type ON edges(edge_type);
CREATE INDEX IF NOT EXISTS ix_edge_dstname ON edges(dst_name);

CREATE TABLE IF NOT EXISTS controls (
  control_id    INTEGER PRIMARY KEY,
  markup_file   INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  codebehind_file INTEGER REFERENCES files(file_id) ON DELETE SET NULL,
  control_ref   TEXT    NOT NULL,
  tag_prefix    TEXT    NOT NULL,
  tag_name      TEXT    NOT NULL,
  ctl_id        TEXT,
  resolved_type TEXT,
  resolution    TEXT    NOT NULL CHECK (resolution IN (
                  'web_config','register_src','register_assembly','bcl_webcontrols',
                  'bcl_htmlcontrols','html_runat','fallback_control')),
  line          INTEGER NOT NULL,
  col           INTEGER NOT NULL,
  used_in_codebehind INTEGER NOT NULL DEFAULT 0,
  -- 1 = the control sits inside ItemTemplate/EditItemTemplate/etc.  ASP.NET
  -- does NOT generate a page-level field for these at runtime, but a field is
  -- synthesised anyway.  Recorded so the over-approximation is
  -- reportable rather than invisible.
  in_template   INTEGER NOT NULL DEFAULT 0,
  synth_field_sym    INTEGER REFERENCES symbols(symbol_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_ctl_id   ON controls(ctl_id);
CREATE INDEX IF NOT EXISTS ix_ctl_file ON controls(markup_file);
CREATE INDEX IF NOT EXISTS ix_ctl_type ON controls(resolved_type);

-- WebForms page-level facts, one row per markup file carrying a main directive.
CREATE TABLE IF NOT EXISTS pages (
  page_id         INTEGER PRIMARY KEY,
  markup_file     INTEGER NOT NULL UNIQUE REFERENCES files(file_id) ON DELETE CASCADE,
  directive       TEXT    NOT NULL,        -- page | control | master
  inherits        TEXT,
  code_file       TEXT,                    -- CodeFile= / CodeBehind= value, as written
  code_attr       TEXT,                    -- 'codefile' | 'codebehind' | NULL
  codebehind_file INTEGER REFERENCES files(file_id) ON DELETE SET NULL,
  master_page     TEXT,                    -- MasterPageFile= value, as written
  master_file     INTEGER REFERENCES files(file_id) ON DELETE SET NULL,
  master_type     TEXT,                    -- <%@ MasterType VirtualPath/TypeName %>
  language        TEXT,
  auto_event_wireup TEXT,
  is_wap          INTEGER NOT NULL DEFAULT 0,  -- real *.designer.cs exists on disk
  designer_file   TEXT,                    -- workspace-relative
  -- Basename only, e.g. 'e475c1d122ef__up.synth.designer.cs'.  The directory is
  -- always paths.designer_dir, which lives outside the workspace and differs per
  -- machine; Config.synth_abs() rebuilds the full path.
  synth_path      TEXT
);
CREATE INDEX IF NOT EXISTS ix_pages_inherits ON pages(inherits);

CREATE TABLE IF NOT EXISTS index_meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""

# --- compiler index (plan Phase 2/3) -------------------------------------
#
# Written by index/roslyn_index.py from the Roslyn host's --index pass.  Every
# row here was resolved by the C# compiler, not matched by name.  Symbol keys
# are "<declaring project>|<DocumentationCommentId>" for source symbols and
# "asm:<assembly>|<DocumentationCommentId>" for metadata; paths are
# workspace-relative like files.rel_path (generated code outside the workspace
# keeps its absolute path and is_generated = 1).

SCHEMA_ROSLYN = """
CREATE TABLE IF NOT EXISTS rprojects (
  project        TEXT PRIMARY KEY,   -- .csproj rel path, or '<site rel>#website'
  side           TEXT NOT NULL,
  name           TEXT NOT NULL,
  assembly       TEXT,
  documents      INTEGER NOT NULL,
  generated_documents INTEGER NOT NULL,
  metadata_refs  INTEGER NOT NULL,
  project_refs   INTEGER NOT NULL,
  errors         INTEGER NOT NULL,
  binding_errors INTEGER NOT NULL,
  warnings       INTEGER NOT NULL,
  clean          INTEGER NOT NULL,   -- 1 = no load failure and no binding error
  failure        TEXT,
  top_errors     TEXT,
  load_failures  TEXT,               -- JSON list
  is_website     INTEGER NOT NULL DEFAULT 0,
  build_exit     INTEGER,            -- from .index/build/manifest.json; NULL = not built
  build_errors   TEXT                -- JSON list, first 20
);

CREATE TABLE IF NOT EXISTS rdiagnostics (
  project   TEXT NOT NULL,
  code      TEXT NOT NULL,
  binding   INTEGER NOT NULL,
  path      TEXT,
  line      INTEGER,
  message   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rfiles (
  project        TEXT NOT NULL,
  path           TEXT NOT NULL,
  generated_kind TEXT NOT NULL       -- '' | aspnet | synthesised | source_generator
);

CREATE TABLE IF NOT EXISTS rsymbols (
  rsym_id       INTEGER PRIMARY KEY,
  key           TEXT NOT NULL,
  doc_id        TEXT NOT NULL,
  project       TEXT NOT NULL,
  side          TEXT NOT NULL,
  kind          TEXT NOT NULL,
  name          TEXT NOT NULL,
  container_key TEXT,
  fqn           TEXT,
  signature     TEXT,
  accessibility TEXT,
  modifiers     TEXT,
  path          TEXT NOT NULL,
  line          INTEGER NOT NULL,
  col           INTEGER NOT NULL,
  end_line      INTEGER NOT NULL,
  is_generated  INTEGER NOT NULL,
  start_line    INTEGER NOT NULL,     -- first line incl. attributes/modifiers
  type          TEXT,                 -- member type without namespaces (property/field/event; method: return)
  json_name     TEXT                  -- wire name from [JsonProperty]/[JsonPropertyName]/[DataMember], if renamed
);

CREATE TABLE IF NOT EXISTS rrefs (
  path             TEXT NOT NULL,
  line             INTEGER NOT NULL,
  col              INTEGER NOT NULL,
  target_key       TEXT NOT NULL,
  target_doc_id    TEXT,
  target_in_source INTEGER NOT NULL,
  target_assembly  TEXT,
  ref_kind         TEXT NOT NULL,     -- call | new | base_ctor | attribute | type_use | read | write | method_group
  enclosing_key    TEXT,
  candidate_reason TEXT,              -- NULL = exact; else Roslyn CandidateReason
  project          TEXT NOT NULL,
  side             TEXT NOT NULL,
  is_generated     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS redges (
  src_key   TEXT NOT NULL,
  edge_type TEXT NOT NULL CHECK (edge_type IN ('inherits','implements','implements_member','overrides')),
  dst_key   TEXT NOT NULL,
  side      TEXT NOT NULL
);

-- HTTP endpoints the compiler found: minimal-API Map* calls (group prefixes
-- resolved) and attribute-routed controller actions.
CREATE TABLE IF NOT EXISTS rendpoints (
  endpoint_id     INTEGER PRIMARY KEY,
  project         TEXT NOT NULL,
  side            TEXT NOT NULL,
  verb            TEXT NOT NULL,      -- GET/POST/... ; '*' = any
  route           TEXT NOT NULL,      -- full route, group prefixes included
  prefix_complete INTEGER NOT NULL,   -- 0 = a group prefix was built out of sight
  handler_key     TEXT,               -- rsymbols key of the handler method (or enclosing member for a lambda)
  handler_kind    TEXT NOT NULL,      -- method | lambda
  style           TEXT NOT NULL,      -- minimal | controller
  path            TEXT NOT NULL,
  line            INTEGER NOT NULL,
  col             INTEGER NOT NULL,
  body_type_key   TEXT                -- the handler's request-body type ([FromBody] or inferred)
);

-- Call sites that send an HTTP request with a (partly) literal URL.
CREATE TABLE IF NOT EXISTS rhttpcalls (
  call_id       INTEGER PRIMARY KEY,
  project       TEXT NOT NULL,
  side          TEXT NOT NULL,
  path          TEXT NOT NULL,
  line          INTEGER NOT NULL,
  col           INTEGER NOT NULL,
  verb          TEXT NOT NULL,        -- '?' = not a literal HttpMethod
  url           TEXT NOT NULL,        -- literal parts; '{}' = computed at run time
  enclosing_key TEXT,
  callee_key    TEXT,                 -- the method called, e.g. Common.getResponse
  is_generated  INTEGER NOT NULL,
  url_line      INTEGER NOT NULL,
  url_col       INTEGER NOT NULL,
  url_end_line  INTEGER NOT NULL,
  url_end_col   INTEGER NOT NULL,
  body_type_key TEXT                  -- the type serialized as the request body
);

-- Legacy (net48) type <-> new (net8) type correspondences (index/twins.py).
CREATE TABLE IF NOT EXISTS rtwins (
  legacy_key       TEXT NOT NULL,
  new_key          TEXT NOT NULL,
  evidence         TEXT NOT NULL,     -- api_contract | same_name | api_contract,same_name
  routes           TEXT NOT NULL,     -- JSON: endpoints the api_contract evidence came from
  score            REAL NOT NULL,     -- shared members / all members (0..1)
  identical        INTEGER NOT NULL,
  common           INTEGER NOT NULL,
  only_legacy      TEXT NOT NULL,     -- JSON member names
  only_new         TEXT NOT NULL,
  type_differences TEXT NOT NULL,     -- JSON "Name: legacyType vs newType"
  nested           TEXT NOT NULL      -- JSON "Name: legacyType ~ newType": different types that are themselves twins
);

-- Call site -> endpoint.  A ROUTE MATCH (index/api_links.py), not a compiler
-- reference: 'match' says how strong it is.
CREATE TABLE IF NOT EXISTS rapilinks (
  call_id     INTEGER NOT NULL,
  endpoint_id INTEGER NOT NULL,
  match       TEXT NOT NULL CHECK (match IN ('route_exact','route_ambiguous','route_partial'))
);

CREATE TABLE IF NOT EXISTS rsubstitutions (
  project   TEXT NOT NULL,
  reference TEXT NOT NULL,
  action    TEXT NOT NULL,            -- substituted | duplicate_removed | ambiguous | cycle | repaired_reference | repair_*
  target    TEXT,
  candidates INTEGER NOT NULL,
  side      TEXT NOT NULL
);
"""

# Created after the bulk load: building them row by row over large reference sets
# costs far more than one pass at the end.
SCHEMA_ROSLYN_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_rsym_key     ON rsymbols(key);
CREATE INDEX IF NOT EXISTS ix_rsym_doc     ON rsymbols(doc_id);
CREATE INDEX IF NOT EXISTS ix_rsym_name    ON rsymbols(name);
CREATE INDEX IF NOT EXISTS ix_rsym_pos     ON rsymbols(path, line);
CREATE INDEX IF NOT EXISTS ix_rsym_cont    ON rsymbols(container_key);
CREATE INDEX IF NOT EXISTS ix_rref_target  ON rrefs(target_key);
CREATE INDEX IF NOT EXISTS ix_rref_pos     ON rrefs(path, line, col);
CREATE INDEX IF NOT EXISTS ix_rref_encl    ON rrefs(enclosing_key);
CREATE INDEX IF NOT EXISTS ix_redge_src    ON redges(src_key, edge_type);
CREATE INDEX IF NOT EXISTS ix_redge_dst    ON redges(dst_key, edge_type);
CREATE INDEX IF NOT EXISTS ix_rfiles_path  ON rfiles(path);
CREATE INDEX IF NOT EXISTS ix_rdiag_path   ON rdiagnostics(path, line);
CREATE INDEX IF NOT EXISTS ix_rep_handler  ON rendpoints(handler_key);
CREATE INDEX IF NOT EXISTS ix_rcall_encl   ON rhttpcalls(enclosing_key);
CREATE INDEX IF NOT EXISTS ix_rcall_pos    ON rhttpcalls(path, url_line);
CREATE INDEX IF NOT EXISTS ix_rlink_call   ON rapilinks(call_id);
CREATE INDEX IF NOT EXISTS ix_rlink_ep     ON rapilinks(endpoint_id);
CREATE INDEX IF NOT EXISTS ix_rtwin_legacy ON rtwins(legacy_key);
CREATE INDEX IF NOT EXISTS ix_rtwin_new    ON rtwins(new_key);
"""

SCHEMA_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
  name, container, signature, doc_comment,
  content='symbols', content_rowid='symbol_id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
  INSERT INTO symbols_fts(rowid,name,container,signature,doc_comment)
  VALUES (new.symbol_id,new.name,new.container,new.signature,new.doc_comment);
END;
CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
  INSERT INTO symbols_fts(symbols_fts,rowid,name,container,signature,doc_comment)
  VALUES ('delete',old.symbol_id,old.name,old.container,old.signature,old.doc_comment);
END;
"""


def fts5_available(con: sqlite3.Connection) -> bool:
    """Probe, never assume.  """
    try:
        con.execute("CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5(x)")
        con.execute("DROP TABLE temp.__fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    db_path = Path(db_path)
    if read_only:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30.0)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db_path, timeout=60.0)
    con.row_factory = sqlite3.Row
    con.executescript(PRAGMAS)
    return con


def schema_fingerprint() -> str:
    import hashlib

    h = hashlib.blake2b(digest_size=8)
    h.update(SCHEMA_CORE.encode())
    h.update(SCHEMA_ROSLYN.encode())
    h.update(SCHEMA_ROSLYN_INDEXES.encode())
    h.update(SCHEMA_FTS.encode())
    h.update(str(USER_VERSION).encode())
    return h.hexdigest()


def _drop_all(con: sqlite3.Connection) -> None:
    names = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]
    con.execute("PRAGMA foreign_keys = OFF")
    for n in names:
        con.execute(f'DROP TABLE IF EXISTS "{n}"')
    con.execute("PRAGMA foreign_keys = ON")
    con.commit()


def init_schema(con: sqlite3.Connection) -> bool:
    """Create the schema.  Returns whether FTS5 is available and enabled.

    The index is entirely derived from the workspace, so a schema change simply
    drops and rebuilds rather than migrating.  Silently running against a stale
    shape is the failure worth avoiding here.
    """
    have = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='index_meta'"
    ).fetchone()
    if have:
        current = get_meta(con, "schema_fingerprint")
        if current != schema_fingerprint():
            _drop_all(con)
    con.executescript(SCHEMA_CORE)
    con.executescript(SCHEMA_ROSLYN)
    fts = fts5_available(con)
    if fts:
        con.executescript(SCHEMA_FTS)
    con.execute(f"PRAGMA user_version = {USER_VERSION}")
    set_meta(con, "fts5", "on" if fts else "off")
    set_meta(con, "schema_version", str(USER_VERSION))
    set_meta(con, "schema_fingerprint", schema_fingerprint())
    con.commit()
    return fts


def fts_enabled(con: sqlite3.Connection) -> bool:
    return get_meta(con, "fts5") == "on"


def set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "INSERT INTO index_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM index_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def all_meta(con: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM index_meta")}


def executemany(con: sqlite3.Connection, sql: str, rows: Iterable[tuple]) -> None:
    con.executemany(sql, rows)
