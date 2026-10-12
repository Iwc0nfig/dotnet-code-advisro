"""The compiler index: run the Roslyn host's --index pass and load its output.  plan: Phase 2/3.

For each side this writes a solution containing EVERY project of that side
found by the build step (not just Legacy.sln / Modern.sln), describes the
Website Projects exactly as ASP.NET compiles them, runs
``RoslynHost --index <spec>`` and bulk-loads the JSONL it produces into the
r* tables (db.SCHEMA_ROSLYN).

Inputs come from the build step (.index/build/manifest.json and the generated
page code).  Without a manifest the pass is skipped and index_status says so:
indexing an unbuilt workspace is exactly the incompleteness this exists to
remove.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable, Iterator

from .. import db as dbm
from . import api_links, twins
from ..build import runner as build_runner
from ..config import Config
from ..inventory import is_excluded_dir

Progress = Callable[[str], None]

SYMBOL_COLS = ("key", "doc_id", "project", "kind", "name", "container_key", "fqn", "signature",
               "accessibility", "modifiers", "path", "line", "col", "end_line", "is_generated",
               "start_line", "type", "json_name")
REF_COLS = ("path", "line", "col", "target_key", "target_doc_id", "target_in_source",
            "target_assembly", "ref_kind", "enclosing_key", "candidate_reason", "project",
            "is_generated")
EDGE_COLS = ("src_key", "edge_type", "dst_key")
ENDPOINT_COLS = ("project", "verb", "route", "prefix_complete", "handler_key", "handler_kind",
                 "style", "path", "line", "col", "body_type_key")
CALL_COLS = ("path", "line", "col", "verb", "url", "enclosing_key", "callee_key", "project",
             "is_generated", "url_line", "url_col", "url_end_line", "url_end_col", "body_type_key")
FILE_COLS = ("project", "path", "generated_kind")
BATCH = 50_000


class RoslynIndexError(RuntimeError):
    pass


def run(cfg: Config, con: sqlite3.Connection, *, progress: Progress = print) -> dict:
    manifest_path = cfg.paths.build_manifest
    if not manifest_path.is_file():
        reason = (f"no build manifest at {manifest_path}; run scripts/build-workspace.py first. "
                  "The compiler index was not built.")
        dbm.set_meta(con, "roslyn_index", "skipped: " + reason)
        progress("roslyn index: " + reason)
        return {"status": "skipped", "reason": reason}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    out: dict = {"status": "ok", "sides": {}}
    for side in cfg.paths.roots:
        exe = cfg.paths.roslyn_host_net48 if side == "net48" else cfg.paths.roslyn_host_net8
        if exe is None or not exe.is_file():
            out["sides"][side] = {"status": "unavailable", "reason": f"host not built: {exe}"}
            continue
        spec_path = write_spec(cfg, con, manifest, side)
        t0 = time.time()
        _run_host(cfg, exe, spec_path, side, progress)
        counts = load(cfg, con, manifest, side, progress)
        counts["elapsed_s"] = round(time.time() - t0, 1)
        out["sides"][side] = {"status": "ok", **counts}
        con.commit()

    stats = api_links.link(con)
    out["api_links"] = vars(stats)
    progress(f"roslyn index: HTTP calls -> endpoints: {vars(stats)}")
    out["twins"] = twins.compute(con)
    progress(f"roslyn index: legacy <-> new type twins: {out['twins']}")

    moved = relocate_control_fields(cfg, con, manifest)
    progress(f"roslyn index: {moved} control-field declarations cited at their markup tag")
    out["control_fields_at_markup"] = moved

    progress("roslyn index: creating indexes")
    con.executescript(dbm.SCHEMA_ROSLYN_INDEXES)
    dbm.set_meta(con, "roslyn_index", json.dumps(out))
    dbm.set_meta(con, "build_started_at", manifest.get("started_at", ""))
    con.commit()
    return out


# ------------------------------------------------------------------ spec

def side_dir(cfg: Config, side: str) -> Path:
    return cfg.paths.build_dir / "roslyn" / side


def write_spec(cfg: Config, con: sqlite3.Connection, manifest: dict, side: str) -> Path:
    d = side_dir(cfg, side)
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.jsonl"):
        f.unlink()

    projects = sorted(p["rel"] for p in manifest["projects"]
                      if p["side"] == side and not p.get("parse_error"))
    sln = d / f"all-{side}.sln"
    _write_solution(cfg, sln, projects + reference_repairs(cfg, manifest, side)[1])

    props: dict[str, str] = {}
    if cfg.build.target_framework_root:
        props["TargetFrameworkRootPath"] = cfg.build.target_framework_root.rstrip("/\\") + "\\"

    repairs, borrowed = reference_repairs(cfg, manifest, side)

    websites = []
    if side == "net48":
        for w in manifest.get("websites", []):
            websites.append(_website_spec(cfg, con, w))

    spec = {
        "side": side,
        "solution": str(sln),
        "workspaces_root": str(cfg.paths.workspaces),
        "out_dir": str(d),
        "properties": props,
        "websites": websites,
        "markup_dirs": [str(cfg.paths.markup_dir)],
        "borrowed_projects": [cfg.abs_of(b) for b in borrowed],
        "reference_repairs": repairs,
    }
    spec_path = d / "spec.json"
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return spec_path


def reference_repairs(cfg: Config, manifest: dict, side: str) -> tuple[list[dict], list[str]]:
    """ProjectReferences whose path does not exist, resolved by file name.

    Several .csproj files here carry another machine's folder layout
    (..\\..\\Demo.NET\\Demo\\Demo.DocuSign\\..., C:/src/example...).  When
    exactly one project in the workspace has the missing file name, that is the
    target; otherwise nothing is repaired.  Targets on the other side are
    returned as "borrowed": the side's pass must load them to compile against
    them.  Returns (repairs for the spec, borrowed workspace-relative paths).
    """
    all_rels = [p["rel"] for p in manifest["projects"] if not p.get("parse_error")]
    by_name: dict[str, list[str]] = {}
    for rel in all_rels:
        by_name.setdefault(rel.rsplit("/", 1)[-1].lower(), []).append(rel)
    have = {r.lower() for r in all_rels}
    side_of = {p["rel"]: p["side"] for p in manifest["projects"]}
    repairs: list[dict] = []
    borrowed: set[str] = set()
    for p in manifest["projects"]:
        if p["side"] != side or p.get("parse_error"):
            continue
        for ref in p.get("project_refs", []):
            if ref.lower() in have:
                continue
            cands = by_name.get(ref.replace("\\", "/").rsplit("/", 1)[-1].lower(), [])
            if len(cands) != 1:
                continue
            target = cands[0]
            repairs.append({"project": cfg.abs_of(p["rel"]), "missing": ref,
                            "target": cfg.abs_of(target)})
            if side_of.get(target) != side:
                borrowed.add(target)
    return repairs, sorted(borrowed)


def _write_solution(cfg: Config, sln: Path, projects: list[str]) -> None:
    """A .sln listing every project, names made unique (MSBuild rejects duplicates).

    Project GUIDs are derived from the path, so the file is identical run to run.
    """
    lines = ["Microsoft Visual Studio Solution File, Format Version 12.00", "# Visual Studio Version 17"]
    seen: Counter[str] = Counter()
    for rel in projects:
        name = Path(rel).stem
        seen[name.lower()] += 1
        if seen[name.lower()] > 1:
            name = f"{name}__{seen[name.lower()]}"
        guid = str(uuid.uuid5(uuid.NAMESPACE_URL, rel.lower())).upper()
        path = cfg.abs_of(rel).replace("/", "\\")
        lines.append(f'Project("{{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}}") = "{name}", "{path}", "{{{guid}}}"')
        lines.append("EndProject")
    lines += ["Global", "EndGlobal"]
    sln.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig")


def _website_spec(cfg: Config, con: sqlite3.Connection, w: dict) -> dict:
    root = Path(cfg.abs_of(w["rel"]))
    markup = w.get("markup") or {}
    gen_dir = Path(markup["out"]) if markup.get("status") == "ok" else None

    generated: list[str] = []
    if gen_dir is not None:
        generated = sorted(str(p) for p in gen_dir.rglob("*.g.cs"))
    have_gen = {Path(g).relative_to(gen_dir).as_posix()[: -len(".g.cs")].lower()
                for g in generated} if gen_dir else set()

    # Pages ASP.NET could not generate keep their synthesised designer partial.
    fallback: list[str] = []
    prefix = w["rel"] + "/"
    for row in con.execute(
        "SELECT f.rel_path, p.synth_path FROM pages p JOIN files f ON f.file_id = p.markup_file "
        "WHERE p.synth_path IS NOT NULL AND substr(f.rel_path, 1, ?) = ?",
        (len(prefix), prefix),
    ):
        within = row[0][len(prefix):].lower()
        if within not in have_gen:
            synth = Path(cfg.synth_abs(row[1]))
            if synth.is_file():
                fallback.append(str(synth))

    references, lang, ref_source = _site_references(cfg, root, w, gen_dir)
    return {
        "name": w["name"],
        "rel": w["rel"],
        "root": str(root),
        "project_paths": [cfg.abs_of(p) for p in w.get("referenced_projects", [])],
        "source_files": _site_sources(cfg, con, w["rel"], root),
        "generated_files": generated,
        "fallback_designers": sorted(fallback),
        "references": references,
        "lang_version": lang,
        "preprocessor_symbols": [],
        # Not read by the host; kept in spec.json as a record of where the list came from.
        "references_source": ref_source,
    }


def _site_sources(cfg: Config, con: sqlite3.Connection, site_rel: str, root: Path) -> list[str]:
    """What ASP.NET compiles as C# source: App_Code, plus each page's CodeFile.

    Not every .cs under the site: a Website Project compiles only these, and a
    stray copy elsewhere would define its types twice.
    """
    out: set[str] = set()
    app_code = root / "App_Code"
    if app_code.is_dir():
        for dirpath, dirnames, filenames in os.walk(app_code):
            dirnames[:] = [x for x in dirnames if is_excluded_dir(x, cfg) is None and cfg.contains_source(Path(dirpath) / x)]
            out.update(str(Path(dirpath) / f) for f in filenames
                       if f.lower().endswith(".cs") and cfg.contains_source(Path(dirpath) / f))
    prefix = site_rel + "/"
    for (rel,) in con.execute(
        "SELECT cb.rel_path FROM pages p "
        "JOIN files m  ON m.file_id  = p.markup_file "
        "JOIN files cb ON cb.file_id = p.codebehind_file "
        "WHERE p.code_attr = 'codefile' AND substr(m.rel_path, 1, ?) = ?",
        (len(prefix), prefix),
    ):
        out.add(str(Path(cfg.abs_of(rel))))
    return sorted(out)


_FW_VERSION = re.compile(r"[\\/](v\d+(?:\.\d+)+)[\\/]", re.IGNORECASE)


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.lstrip("vV").split(".") if x.isdigit())


def _framework_dir(cfg: Config, wanted: str | None) -> Path | None:
    """The reference-assembly folder for a .NET Framework version: that version if
    present, else the nearest newer one (4.7 -> 4.7.1), else the newest available."""
    root = Path(cfg.build.target_framework_root or
                "C:/Program Files (x86)/Reference Assemblies/Microsoft/Framework") / ".NETFramework"
    have = sorted((d for d in root.glob("v4*") if (d / "mscorlib.dll").is_file()),
                  key=lambda d: _version_key(d.name))
    if not have:
        return None
    if wanted:
        w = _version_key(wanted)
        for d in have:
            if _version_key(d.name) >= w:
                return d
    return have[-1]


def _site_references(cfg: Config, root: Path, w: dict, gen_dir: Path | None):
    """What a site compiles against: ASP.NET's exact list when page codegen produced
    one, else Bin + framework.  Either way plus mscorlib and the facades, which csc
    and ASP.NET add implicitly and Roslyn has to be given explicitly -- without
    them every type fails with CS0518/CS0012."""
    refs: list[str] = []
    lang = "7.3"
    source = "fallback_bin_and_framework"
    fw_version: str | None = None
    if gen_dir is not None and (gen_dir / "codegen.json").is_file():
        cg = json.loads((gen_dir / "codegen.json").read_text(encoding="utf-8"))
        refs = sorted(r["path"] for r in cg["references"] if r.get("path"))
        m = re.search(r"/langversion:(\S+)", cg.get("compiler_options") or "")
        lang = m.group(1) if m else lang
        for r in cg["references"]:
            if r.get("kind") == "framework" and r.get("path"):
                vm = _FW_VERSION.search(r["path"])
                if vm:
                    fw_version = vm.group(1)
                    break
        if refs:
            source = "aspnet"
    if not fw_version:
        tfm = w.get("target_framework") or ""
        vm = re.search(r"v(\d+(?:\.\d+)+)", tfm)
        fw_version = "v" + vm.group(1) if vm else None
    fw = _framework_dir(cfg, fw_version)

    if not refs:
        refs = sorted(str(p) for p in (root / "Bin").glob("*.dll"))
        if fw is not None:
            refs += sorted(str(p) for p in fw.glob("*.dll"))
    if fw is not None:
        # Last, so a same-named assembly from ASP.NET's list or Bin wins (the host
        # keeps the first file of each name).
        refs.append(str(fw / "mscorlib.dll"))
        refs += sorted(str(p) for p in (fw / "Facades").glob("*.dll"))
        source += f"+mscorlib+facades({fw.name})"
    return refs, lang, source


def relocate_control_fields(cfg: Config, con: sqlite3.Connection, manifest: dict) -> int:
    """Cite a page's control fields at the markup tag that declares them.

    ASP.NET's generated code declares ``protected TextBox txtName;`` in a file
    under .index/build/markup/, which is ASP.NET's output and not a workspace
    file (AGENTS.md rule 7).  The graph already knows where each control's tag
    is (controls.line/col), so the declaration is moved there.  The row keeps
    is_generated = 1; only its location changes.
    """
    site_of_dir: dict[str, str] = {}
    for w in manifest.get("websites", []):
        m = w.get("markup") or {}
        if m.get("status") == "ok":
            site_of_dir[Path(m["out"]).as_posix().lower()] = w["rel"]

    tags: dict[tuple[str, str], tuple[int, int]] = {}
    for r in con.execute(
        "SELECT f.rel_path, c.ctl_id, MIN(c.line), MIN(c.col) FROM controls c "
        "JOIN files f ON f.file_id = c.markup_file WHERE c.ctl_id IS NOT NULL "
        "GROUP BY f.rel_path, c.ctl_id"
    ):
        tags[(r[0].lower(), r[1])] = (r[2], r[3])
    canon = {r[0].lower(): r[0] for r in con.execute("SELECT rel_path FROM files")}

    updates = []
    for rid, path, name in con.execute(
        "SELECT rsym_id, path, name FROM rsymbols WHERE is_generated = 1 AND kind = 'field' "
        "AND path LIKE '%.g.cs'"
    ).fetchall():
        posix = path.replace("\\", "/")
        for d, site in site_of_dir.items():
            if posix.lower().startswith(d + "/"):
                within = posix[len(d) + 1: -len(".g.cs")]
                markup = f"{site}/{within}".lower()
                hit = tags.get((markup, name))
                if hit:
                    updates.append((canon.get(markup, markup), hit[0], hit[1], rid))
                break
    con.executemany("UPDATE rsymbols SET path = ?, line = ?, col = ?, end_line = ?, start_line = ? "
                    "WHERE rsym_id = ?",
                    [(p_, l, c_, l, l, rid) for p_, l, c_, rid in updates])
    return len(updates)


# ------------------------------------------------------------------ host

def _run_host(cfg: Config, exe: Path, spec: Path, side: str, progress: Progress) -> None:
    log = cfg.paths.log_dir / f"roslyn-index-{side}.log"
    progress(f"roslyn index {side}: {exe} --index {spec}")
    with open(log, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen([str(exe), "--index", str(spec)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", env=build_runner.host_env(cfg))
        assert proc.stdout is not None
        for line in proc.stdout:
            lf.write(line)
            line = line.rstrip()
            if line.startswith("[index") or "!" in line[:12]:
                progress("  " + line)
        rc = proc.wait()
    done = side_dir(cfg, side) / "done.json"
    if rc != 0 or not done.is_file():
        raise RoslynIndexError(f"RoslynHost --index {side} failed (exit {rc}); see {log}")


# ------------------------------------------------------------------ load

def _rows(path: Path) -> Iterator[list]:
    if not path.is_file():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _batched(con: sqlite3.Connection, sql: str, rows: Iterator[tuple]) -> int:
    n = 0
    batch: list[tuple] = []
    for r in rows:
        batch.append(r)
        if len(batch) >= BATCH:
            con.executemany(sql, batch)
            n += len(batch)
            batch.clear()
    if batch:
        con.executemany(sql, batch)
        n += len(batch)
    return n


def load(cfg: Config, con: sqlite3.Connection, manifest: dict, side: str, progress: Progress) -> dict:
    d = side_dir(cfg, side)
    done = json.loads((d / "done.json").read_text(encoding="utf-8"))
    for name, cols in (("symbols", SYMBOL_COLS), ("refs", REF_COLS), ("edges", EDGE_COLS),
                       ("files", FILE_COLS), ("endpoints", ENDPOINT_COLS), ("http_calls", CALL_COLS)):
        declared = tuple(done["columns"][name].split(","))
        if declared != cols:
            raise RoslynIndexError(f"{name}.jsonl columns {declared} != expected {cols}; "
                                   "rebuild the host (scripts/build-hosts.ps1)")

    build = {p["rel"]: p.get("build") for p in manifest["projects"]}
    counts: dict[str, int] = {}

    # Paths as the graph spells them (on-disk case).  MSBuild reports paths as
    # the .sln/.csproj spell them, and those differ in case here and there; the
    # tools look positions up by exact path, so one spelling has to win.
    canon = {r[0].lower(): r[0] for r in con.execute("SELECT rel_path FROM files")}

    def cp(path):
        return canon.get(path.lower(), path) if path else path

    def projects() -> Iterator[tuple]:
        for r in _rows(d / "projects.jsonl"):
            (key, name, asm, docs, gen, mrefs, prefs, errs, berrs, warns, clean, failure,
             top, failures, is_site) = r
            b = build.get(key)
            yield (key, side, name, asm, docs, gen, mrefs, prefs, errs, berrs, warns, clean,
                   failure, top, failures, is_site,
                   b["exit_code"] if b else None, json.dumps(b["errors"]) if b else None)

    counts["projects"] = _batched(con, "INSERT OR REPLACE INTO rprojects VALUES (" + ",".join("?" * 18) + ")",
                                  projects())
    counts["diagnostics"] = _batched(
        con, "INSERT INTO rdiagnostics VALUES (?,?,?,?,?,?)",
        ((r[0], r[1], r[2], cp(r[3]), r[4], r[5]) for r in _rows(d / "diagnostics.jsonl")))
    counts["substitutions"] = _batched(
        con, "INSERT INTO rsubstitutions VALUES (?,?,?,?,?,?)",
        ((*r, side) for r in _rows(d / "substitutions.jsonl")))
    counts["files"] = _batched(con, "INSERT INTO rfiles VALUES (?,?,?)",
                               ((r[0], cp(r[1]), r[2]) for r in _rows(d / "files.jsonl")))
    progress(f"roslyn index {side}: loading symbols")
    counts["symbols"] = _batched(
        con, "INSERT INTO rsymbols(key,doc_id,project,side,kind,name,container_key,fqn,signature,"
             "accessibility,modifiers,path,line,col,end_line,is_generated,start_line,type,json_name) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ((r[0], r[1], r[2], side, *r[3:10], cp(r[10]), *r[11:]) for r in _rows(d / "symbols.jsonl")))
    progress(f"roslyn index {side}: loading references")
    counts["refs"] = _batched(
        con, "INSERT INTO rrefs(path,line,col,target_key,target_doc_id,target_in_source,"
             "target_assembly,ref_kind,enclosing_key,candidate_reason,project,side,is_generated) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ((cp(r[0]), *r[1:11], side, r[11]) for r in _rows(d / "refs.jsonl")))
    counts["edges"] = _batched(con, "INSERT INTO redges VALUES (?,?,?,?)",
                               ((*r, side) for r in _rows(d / "edges.jsonl")))
    counts["endpoints"] = _batched(
        con, "INSERT INTO rendpoints(project,side,verb,route,prefix_complete,handler_key,handler_kind,"
             "style,path,line,col,body_type_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ((r[0], side, r[1], r[2], r[3], r[4], r[5], r[6], cp(r[7]), r[8], r[9], r[10])
         for r in _rows(d / "endpoints.jsonl")))
    counts["http_calls"] = _batched(
        con, "INSERT INTO rhttpcalls(project,side,path,line,col,verb,url,enclosing_key,callee_key,"
             "is_generated,url_line,url_col,url_end_line,url_end_col,body_type_key) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ((r[7], side, cp(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], r[8], r[9], r[10], r[11], r[12], r[13])
         for r in _rows(d / "http_calls.jsonl")))
    counts["load_failures"] = sum(1 for r in _rows(d / "load_failures.jsonl") if r[0] == "failure")
    # The host writes in a fixed order (projects, then documents by path, then
    # syntax order), so identical inputs give byte-identical output.  Recording
    # the hash makes "two passes over an unchanged workspace agree" checkable.
    h = hashlib.blake2b(digest_size=16)
    for name in ("symbols", "refs", "edges", "files", "projects", "diagnostics", "substitutions",
                 "endpoints", "http_calls"):
        f = d / f"{name}.jsonl"
        if f.is_file():
            with open(f, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
    dbm.set_meta(con, f"roslyn_content_hash_{side}", h.hexdigest())
    counts["fallback_keys"] = done.get("fallback_keys", 0)
    progress(f"roslyn index {side}: {counts}")
    return counts
