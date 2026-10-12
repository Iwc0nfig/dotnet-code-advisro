"""Restore + build every project in the workspace copy, then generate page code.  plan: Phase 1.

Why per project and not per solution: 210 of the 341 projects are in no .sln,
some are in several, and building a .sln that contains a Website Project runs
aspnet_compiler and writes PrecompiledWeb/ into the workspace.  So every
project is restored and built on its own, in dependency order (discover.build_levels),
with BuildProjectReferences=false -- its dependencies were built in an earlier
level.  That also gives an exact exit code and error list per project, which is
what the manifest is for.

Inside the workspace this writes only what a developer's Visual Studio build
writes: MSBuild's bin/ and obj/ output (some Debug OutputPaths point into
App/Bin), and each Website Project's Bin refreshed from its *.dll.refresh files
and referenced projects (prepare_website).  Logs, the manifest and generated
page code go under paths.build_dir.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Config
from . import refasm
from .discover import Discovery, ProjectInfo, WebsiteInfo, build_levels, discover

Progress = Callable[[str], None]
MANIFEST_SCHEMA = 1

# "  Demo.Vault -> C:\...\Demo.Vault.dll"  (minimal verbosity)
_OUTPUT_LINE = re.compile(r"^\s*\S[^>]*? -> (?P<path>[A-Za-z]:\\.+\.(?:dll|exe))\s*$", re.IGNORECASE)

# "file(12,5): error CS0246: message [C:\...\X.csproj]", and the code-less form
# NuGet and some tasks log: "NuGet.targets(178,5): error : No solution found."
_ERROR_LINE = re.compile(r"(?:^\s*|:\s)error(?: [A-Z]+\d+)?\s*:", re.IGNORECASE)

DEFAULT_REF_ROOT = "C:/Program Files (x86)/Reference Assemblies/Microsoft/Framework"


@dataclass
class StepResult:
    exit_code: int | None = None
    errors: list[str] = field(default_factory=list)
    error_count: int = 0
    elapsed_s: float = 0.0
    log: str | None = None
    retried: bool = False
    outputs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "exit_code": self.exit_code, "error_count": self.error_count,
            "errors": self.errors, "elapsed_s": round(self.elapsed_s, 1),
            "log": self.log, "retried": self.retried, "outputs": self.outputs,
        }


# What an MSBuild instance must have to build this workspace: the .NET SDK
# resolver (every SDK-style project) and the web-application targets (legacy
# web projects import Microsoft.WebApplication.targets).  Two installs of the
# same version can differ here -- Build Tools without the web workload has
# neither -- so the instance is chosen by capability, never by "latest".
#
# Globs, not fixed paths: the web targets sit under v<VisualStudioVersion>
# (v17.0 for VS 2022, v18.0 for VS 2026), and legacy projects import them
# through $(VSToolsPath), which MSBuild sets to the running instance's version.
_CAPABILITIES = {
    "dotnet_sdk_resolver": "MSBuild/Current/Bin/SdkResolvers/Microsoft.DotNet.MSBuildSdkResolver",
    "web_application_targets": "MSBuild/Microsoft/VisualStudio/v*/WebApplications/Microsoft.WebApplication.targets",
}


def msbuild_capabilities(install: Path) -> dict[str, bool]:
    return {k: any(install.glob(v)) for k, v in _CAPABILITIES.items()}


def find_msbuild(cfg: Config) -> tuple[Path, dict[str, bool]]:
    """Returns (MSBuild.exe, capabilities).  Raises if no instance exists at all."""
    if cfg.build.msbuild:
        p = Path(cfg.build.msbuild)
        if not p.is_file():
            raise FileNotFoundError(f"[build] msbuild = {p} does not exist")
        return p, msbuild_capabilities(p.parents[3])
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) \
        / "Microsoft Visual Studio/Installer/vswhere.exe"
    installs: list[Path] = []
    if vswhere.is_file():
        out = subprocess.run(
            [str(vswhere), "-all", "-products", "*", "-requires", "Microsoft.Component.MSBuild",
             "-property", "installationPath"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        installs = sorted(Path(l.strip()) for l in out.splitlines() if l.strip())
    candidates = [(i, msbuild_capabilities(i)) for i in installs
                  if (i / "MSBuild/Current/Bin/MSBuild.exe").is_file()]
    if not candidates:
        raise FileNotFoundError(
            "No Visual Studio MSBuild found. Install VS Build Tools (scripts/provision-vm.ps1) "
            "or set [build] msbuild in config.toml."
        )
    # Most capabilities wins; on a tie the first path in sorted order, so the
    # choice is the same on every run.
    best = max(sum(c.values()) for _, c in candidates)
    install, caps = next(c for c in candidates if sum(c[1].values()) == best)
    return install / "MSBuild/Current/Bin/MSBuild.exe", caps


def host_env(cfg: Config) -> dict[str, str]:
    """Environment for a Roslyn host: pins it to the MSBuild the workspace build used.

    The net48 host loads MSBuild in-process through MSBuildLocator, which on its
    own takes the highest VS version.  Two installs can tie, or differ in
    workloads, so the build's own choice (the manifest, else the same
    capability rule) is passed down as DOTNET_ADVISOR_MSBUILD_BIN.  An explicit
    [build] msbuild wins over both.
    """
    env = dict(os.environ)
    exe: Path | None = Path(cfg.build.msbuild) if cfg.build.msbuild else None
    try:
        m = json.loads(cfg.paths.build_manifest.read_text(encoding="utf-8"))
        if exe is None and m.get("msbuild") and Path(m["msbuild"]).is_file():
            exe = Path(m["msbuild"])
    except (OSError, ValueError):
        pass
    if exe is None:
        try:
            exe, _ = find_msbuild(cfg)
        except (OSError, subprocess.SubprocessError):
            return env
    env["DOTNET_ADVISOR_MSBUILD_BIN"] = str(exe.parent)
    return env


def ref_root(cfg: Config) -> Path:
    return Path(cfg.build.target_framework_root or DEFAULT_REF_ROOT)


def missing_targeting_packs(cfg: Config, d: Discovery) -> dict[str, list[str]]:
    """.NET Framework versions projects target whose reference assemblies are not present.

    Without them MSBuild resolves no framework references at all and the project
    compiles against nothing (CS0518 on every type) -- in a real build and in
    the Roslyn load alike.
    """
    root = ref_root(cfg) / ".NETFramework"
    missing: dict[str, list[str]] = {}
    for p in d.projects.values():
        for tfm in p.target_frameworks:
            v = _netfx_version(tfm)
            if v is None or v == "v3.5":  # 3.5 resolves from the v3.5/GAC layout
                continue
            d_ = root / v
            if not (d_.is_dir() and any(d_.glob("*.dll"))):
                missing.setdefault(v, []).append(p.rel)
    return {k: sorted(v) for k, v in sorted(missing.items())}


def _netfx_version(tfm: str) -> str | None:
    t = tfm.strip().lower()
    if t.startswith("v4") or t.startswith("v3"):
        return t
    m = re.fullmatch(r"net(4)(\d)(\d)?", t)
    if m:
        return f"v{m[1]}.{m[2]}" + (f".{m[3]}" if m[3] else "")
    return None


def _global_props(cfg: Config) -> list[str]:
    props = dict(cfg.build.properties)
    props["Configuration"] = cfg.build.configuration
    if cfg.build.target_framework_root:
        props["TargetFrameworkRootPath"] = cfg.build.target_framework_root.rstrip("/\\") + "\\"
    return [f"-p:{k}={v}" for k, v in sorted(props.items())]


def _solution_dir(packages_dir: str) -> str:
    """packages.config restore puts packages in $(SolutionDir)packages."""
    return str(Path(packages_dir).parent).rstrip("\\/") + "\\"


def _default_packages_dir(cfg: Config, p: ProjectInfo) -> str | None:
    """Where packages.config restore should go when no HintPath says so.

    NuGet refuses a packages.config restore without $(SolutionDir) ("No solution
    found"): a legacy project (with RestorePackagesConfig, even one that has no
    packages.config) or an SDK-style project that still has a packages.config.  Visual Studio would pass the solution's
    folder; a project in no solution gets the nearest packages/ folder above it.
    """
    if p.solutions:
        return ((cfg.paths.workspaces / p.solutions[0]).parent / "packages").as_posix()
    for d in p.path.parents:
        if (d / "packages").is_dir():
            return (d / "packages").as_posix()
        if d == cfg.paths.workspaces:
            break
    return None


def _run_restore(cfg: Config, msbuild: Path, p: ProjectInfo, log_dir: Path) -> StepResult:
    """Restore once per packages/ folder the project points into; the first failure wins."""
    uses_packages_config = not p.sdk_style or (p.path.parent / "packages.config").is_file()
    dirs = p.packages_dirs or ([_default_packages_dir(cfg, p)] if uses_packages_config else [None])
    result: StepResult | None = None
    for i, pk in enumerate(dirs):
        extra = [f"-p:SolutionDir={_solution_dir(pk)}"] if pk else []
        r = _run_msbuild(cfg, msbuild, p, "Restore", log_dir, extra, suffix=f"-{i}" if i else "")
        if result is None or (result.exit_code == 0 and r.exit_code != 0):
            result = r
        result.elapsed_s = max(result.elapsed_s, r.elapsed_s)
    assert result is not None
    return result


def _run_msbuild(cfg: Config, msbuild: Path, p: ProjectInfo, target: str,
                 log_dir: Path, extra: list[str], suffix: str = "") -> StepResult:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", p.rel)
    log = log_dir / f"{safe}.{target.lower()}{suffix}.log"
    args = [str(msbuild), str(p.path), f"-t:{target}", "-nologo", "-nr:false", "-m:1",
            "-v:m", f"-flp:logfile={log};verbosity=normal;encoding=utf-8",
            *_global_props(cfg), *extra]
    if target != "Restore" and p.packages_dirs:
        args.append(f"-p:SolutionDir={_solution_dir(p.packages_dirs[0])}")
    t0 = time.time()
    r = StepResult(log=log.as_posix())
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=cfg.build.timeout_s)
        r.exit_code = proc.returncode
        out = proc.stdout
    except subprocess.TimeoutExpired as e:
        r.exit_code = None
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        r.errors.append(f"timeout after {cfg.build.timeout_s}s")
    seen: set[str] = set()
    for line in out.splitlines():
        om = _OUTPUT_LINE.match(line)
        if om and om["path"] not in r.outputs:
            r.outputs.append(om["path"])
        if _ERROR_LINE.search(line):
            line = line.strip()
            if line in seen:
                continue
            seen.add(line)
            r.error_count += 1
            if len(r.errors) < 20:
                r.errors.append(line)
    r.elapsed_s = time.time() - t0
    return r


def _pool_size(cfg: Config) -> int:
    return cfg.build.parallel or max(1, (os.cpu_count() or 2))


def run(cfg: Config, *, only: str | None = None, skip_restore: bool = False,
        skip_build: bool = False, skip_markup: bool = False,
        fetch_refasm: bool = True, progress: Progress = print) -> dict:
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    t0 = time.time()
    cfg.ensure_index_dirs()
    log_dir = cfg.paths.build_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    d = discover(cfg)
    levels, cycle = build_levels(d.projects)
    selected = {rel for rel in d.projects if not only or only.lower() in rel.lower()}
    progress(f"discovered {len(d.solutions)} solutions, {len(d.projects)} projects, "
             f"{len({w.rel for w in d.websites})} websites; {len(levels)} build levels"
             + (f"; {len(selected)} selected by --only" if only else ""))

    missing = missing_targeting_packs(cfg, d)
    refasm_report = None
    if missing and fetch_refasm and refasm.is_managed(cfg):
        progress(f"reference assemblies: adding {', '.join(missing)} to {ref_root(cfg)}")
        refasm_report = refasm.ensure(cfg, set(missing), progress)
        missing = missing_targeting_packs(cfg, d)
    for v, ps in missing.items():
        progress(f"WARNING: .NET Framework {v} reference assemblies not found "
                 f"under {ref_root(cfg)}; {len(ps)} project(s) will not compile")

    msbuild, caps = find_msbuild(cfg) if not (skip_restore and skip_build) else (None, {})
    for k, ok in caps.items():
        if not ok:
            progress(f"WARNING: MSBuild {msbuild} lacks {k}; projects needing it will fail")
    restore: dict[str, StepResult] = {}
    build: dict[str, StepResult] = {}

    if msbuild and not skip_restore:
        progress(f"restore: {len(selected)} projects with {msbuild}")
        with ThreadPoolExecutor(_pool_size(cfg)) as ex:
            futs = {rel: ex.submit(_run_restore, cfg, msbuild, d.projects[rel], log_dir)
                    for rel in sorted(selected)}
            for i, (rel, f) in enumerate(futs.items(), 1):
                restore[rel] = f.result()
                if restore[rel].exit_code != 0:
                    progress(f"  restore FAILED {rel}: {restore[rel].errors[:1]}")
                if i % 25 == 0:
                    progress(f"  restore {i}/{len(futs)}")

    if msbuild and not skip_build:
        extra = ["-p:BuildProjectReferences=false"]
        for li, level in enumerate(levels, 1):
            todo = [rel for rel in level if rel in selected]
            if not todo:
                continue
            progress(f"build level {li}/{len(levels)}: {len(todo)} projects")
            with ThreadPoolExecutor(_pool_size(cfg)) as ex:
                futs = {rel: ex.submit(_run_msbuild, cfg, msbuild, d.projects[rel], "Build", log_dir, extra)
                        for rel in todo}
                for rel, f in futs.items():
                    build[rel] = f.result()
        # One sequential retry: parallel builds sharing an output folder can
        # collide on a file lock, and that is not a property of the code.
        failed = sorted(rel for rel, r in build.items() if r.exit_code != 0)
        if failed:
            progress(f"retrying {len(failed)} failed build(s) sequentially")
            for rel in failed:
                r = _run_msbuild(cfg, msbuild, d.projects[rel], "Build", log_dir, extra)
                r.retried = True
                build[rel] = r

    sites = _unique_sites(d.websites)
    prepared: dict[str, dict] = {}
    if msbuild and not skip_build:
        for w in sites:
            if not only or only.lower() in w.rel.lower():
                try:
                    prepared[w.rel] = prepare_website(cfg, msbuild, w, build, log_dir, progress)
                except Exception as e:  # recorded; one site must not stop the pass
                    prepared[w.rel] = {"error": f"{type(e).__name__}: {e}"}
    markup: dict[str, dict] = {}
    if not skip_markup:
        for w in sites:
            if not only or only.lower() in w.rel.lower():
                try:
                    markup[w.rel] = generate_markup(cfg, w, progress)
                except Exception as e:
                    markup[w.rel] = {"status": "failed", "reason": f"{type(e).__name__}: {e}"}

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "workspaces_root": cfg.paths.workspaces.as_posix(),
        "started_at": started,
        "elapsed_s": round(time.time() - t0, 1),
        "only": only,
        "msbuild": msbuild.as_posix() if msbuild else None,
        "msbuild_capabilities": caps,
        "configuration": cfg.build.configuration,
        "properties": dict(sorted(cfg.build.properties.items())),
        "target_framework_root": ref_root(cfg).as_posix(),
        "missing_targeting_packs": missing,
        "reference_assemblies": refasm_report,
        "levels": [len(level) for level in levels],
        "cycle": cycle,
        "solutions": [
            {"rel": s.rel, "projects": s.projects, "missing_projects": s.missing_projects,
             "websites": s.websites}
            for s in d.solutions
        ],
        "projects": [
            {
                "rel": p.rel, "side": p.side, "assembly_name": p.assembly_name,
                "target_frameworks": p.target_frameworks, "sdk_style": p.sdk_style,
                "solutions": p.solutions, "project_refs": p.project_refs,
                "parse_error": p.parse_error,
                "restore": restore[p.rel].as_dict() if p.rel in restore else None,
                "build": build[p.rel].as_dict() if p.rel in build else None,
            }
            for p in sorted(d.projects.values(), key=lambda x: x.rel)
        ],
        "websites": [
            {
                "name": w.name, "rel": w.rel, "solution": w.solution,
                "target_framework": w.target_framework,
                "referenced_projects": w.referenced_projects,
                "prepared": prepared.get(w.rel),
                "markup": markup.get(w.rel),
            }
            for w in sites
        ],
    }
    manifest["summary"] = _summary(manifest)
    tmp = cfg.paths.build_manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp.replace(cfg.paths.build_manifest)
    return manifest


def _unique_sites(sites: list[WebsiteInfo]) -> list[WebsiteInfo]:
    """A site listed in several solutions is one site; keep the entry with most references."""
    best: dict[str, WebsiteInfo] = {}
    for w in sites:
        cur = best.get(w.rel.lower())
        if cur is None or len(w.referenced_projects) > len(cur.referenced_projects):
            best[w.rel.lower()] = w
    return sorted(best.values(), key=lambda w: w.rel)


def prepare_website(cfg: Config, msbuild: Path, w: WebsiteInfo,
                    build: dict[str, StepResult], log_dir: Path, progress: Progress) -> dict:
    """Do what Visual Studio's website build does before compiling pages.

    A Website Project has no project file, so nothing in the per-project build
    touches its Bin.  VS would: restore the site's packages.config, copy every
    Bin/*.dll.refresh target into Bin, and copy the referenced projects' outputs
    into Bin.  Without that, Bin drifts from packages.config and Web.config's
    binding redirects, and ASP.NET cannot even start to generate page code.
    """
    progress(f"prepare website: {w.rel}")
    bin_dir = w.root / "Bin"
    result: dict = {"packages_restore": None, "refresh": {}, "project_outputs": {}}

    pc = w.root / "packages.config"
    refresh_files = sorted(bin_dir.glob("*.refresh")) if bin_dir.is_dir() else []
    pkg_dir = _packages_dir_from_refresh(w.root, refresh_files)
    if pc.is_file() and pkg_dir is not None:
        result["packages_restore"] = _restore_site_packages(cfg, msbuild, w, pc, pkg_dir, log_dir)

    skipped: list[str] = []
    failed: list[str] = []
    copied, same, missing = [], 0, []
    for rf in refresh_files:
        target = _read_refresh(w.root, rf)
        dst = bin_dir / rf.name[: -len(".refresh")]
        if target is None or not target.is_file():
            missing.append(rf.name)
            continue
        if _same_file(target, dst):
            same += 1
            continue
        _copy(target, dst, copied, skipped, failed)
    result["refresh"] = {"copied": copied, "unchanged": same, "missing_target": missing}

    out_copied, out_missing = [], []
    for rel in w.referenced_projects:
        r = build.get(rel)
        outs = [Path(o) for o in (r.outputs if r else []) if o.lower().endswith(".dll")]
        if not outs:
            out_missing.append(rel)
            continue
        for o in outs:
            for f in (o, o.with_suffix(".pdb")):
                dst = bin_dir / f.name
                if f.is_file() and not _same_file(f, dst):
                    _copy(f, dst, out_copied, skipped, failed)
    result["project_outputs"] = {"copied": sorted(set(out_copied)), "not_built": out_missing}
    result["skipped_read_only"] = sorted(set(skipped))
    result["copy_failed"] = sorted(set(failed))
    return result


def _copy(src: Path, dst: Path, copied: list[str], skipped: list[str], failed: list[str]) -> None:
    """Copy into a site's Bin -- except over a read-only file.

    Some source-control systems mark files read-only when they are not
    checked out.  Overwriting one would leave the workspace with a "writable
    file" conflict on the next source update, so a checked-in Bin is taken as the
    source of truth and the difference is recorded instead.
    """
    if dst.exists() and not os.access(dst, os.W_OK):
        skipped.append(dst.name)
        return
    try:
        shutil.copy2(src, dst)
        copied.append(dst.name)
    except OSError as e:
        failed.append(f"{dst.name}: {e.strerror or e}")


def _read_refresh(site: Path, rf: Path) -> Path | None:
    """A .refresh file holds one path, relative to the site root; VS writes some as UTF-16."""
    raw = rf.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    text = text.strip().strip("\x00")
    if not text:
        return None
    return Path(os.path.normpath(site / text.replace("\\", "/")))


def _packages_dir_from_refresh(site: Path, refresh_files: list[Path]) -> Path | None:
    for rf in refresh_files:
        t = _read_refresh(site, rf)
        if t is None:
            continue
        parts = t.parts
        for i in range(len(parts) - 1, -1, -1):
            if parts[i].lower() == "packages":
                return Path(*parts[: i + 1])
    return None


def _same_file(a: Path, b: Path) -> bool:
    if not b.is_file():
        return False
    if a.stat().st_size != b.stat().st_size:
        return False
    return a.read_bytes() == b.read_bytes()


_SITE_RESTORE_PROJ = r"""<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <!-- Generated by dotnet_code_advisor.build: lets MSBuild restore a Website
       Project's packages.config, which has no project file of its own. -->
  <Import Project="$(MSBuildExtensionsPath)\$(MSBuildToolsVersion)\Microsoft.Common.props" />
  <PropertyGroup>
    <OutputType>Library</OutputType>
    <TargetFrameworkVersion>v4.8</TargetFrameworkVersion>
  </PropertyGroup>
  <Import Project="$(MSBuildToolsPath)\Microsoft.CSharp.targets" />
</Project>
"""


def _restore_site_packages(cfg: Config, msbuild: Path, w: WebsiteInfo, pc: Path,
                           pkg_dir: Path, log_dir: Path) -> dict:
    work = cfg.paths.build_dir / "siterestore" / w.rel.replace("/", "__")
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pc, work / "packages.config")
    proj = work / "site.csproj"
    proj.write_text(_SITE_RESTORE_PROJ, encoding="utf-8")
    info = ProjectInfo(path=proj, rel=f"{w.rel}#packages.config", side="net48", sdk_style=False,
                       assembly_name="site", target_frameworks=["v4.8"],
                       packages_dirs=[pkg_dir.as_posix()])
    return _run_restore(cfg, msbuild, info, log_dir).as_dict()


def markup_out_dir(cfg: Config, site_rel: str) -> Path:
    return cfg.paths.markup_dir / site_rel.replace("/", "__")


def generate_markup(cfg: Config, w: WebsiteInfo, progress: Progress) -> dict:
    exe = cfg.paths.markup_codegen
    out = markup_out_dir(cfg, w.rel)
    if exe is None or not exe.is_file():
        return {"status": "unavailable",
                "reason": f"paths.markup_codegen not built ({exe}); markup falls back to "
                          "synthesised designer partials"}
    progress(f"markup codegen: {w.rel}")
    t0 = time.time()
    proc = subprocess.run([str(exe), "--site", str(w.root), "--out", str(out)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=cfg.build.timeout_s)
    summary_file = out / "codegen.json"
    if proc.returncode != 0 or not summary_file.is_file():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return {"status": "failed", "exit_code": proc.returncode, "reason": " | ".join(tail)}
    s = json.loads(summary_file.read_text(encoding="utf-8"))
    return {
        "status": "ok", "out": out.as_posix(), "elapsed_s": round(time.time() - t0, 1),
        "files_ok": s["files_ok"], "files_failed": s["files_failed"],
        "files_no_code": s["files_no_code"], "references": len(s["references"]),
        "compiler_options": s["compiler_options"],
    }


def _summary(m: dict) -> dict:
    ps = m["projects"]
    def count(step: str, ok: bool) -> int:
        return sum(1 for p in ps if p[step] is not None and ((p[step]["exit_code"] == 0) == ok))
    return {
        "projects": len(ps),
        "restore_ok": count("restore", True), "restore_failed": count("restore", False),
        "build_ok": count("build", True), "build_failed": count("build", False),
        "missing_targeting_pack_projects": sorted({p for v in m["missing_targeting_packs"].values() for p in v}).__len__(),
        "websites": len(m["websites"]),
        "markup_ok": sum(1 for w in m["websites"] if (w["markup"] or {}).get("status") == "ok"),
    }
