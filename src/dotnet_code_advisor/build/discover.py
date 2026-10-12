"""Find every solution, project and Website Project in the workspace.  plan: Phase 1.

Discovery reads the files as text/XML; it never evaluates MSBuild.  What it
extracts is only what the build step needs to order and configure builds:

* for a project: assembly name, target framework(s), ProjectReferences, and
  HintPaths (a HintPath naming another project's output is a build-order edge
  that MSBuild does not know about);
* for a Website Project: its physical root, target framework and referenced
  projects, read from ITS OWN WebsiteProperties section.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..inventory import is_excluded_dir, walk_paths

WEBSITE_TYPE = "E24C65DC-7377-472B-9ABA-BC803B73C61A"
SOLUTION_FOLDER_TYPE = "2150E333-8FDC-42A3-9474-1A3956D46DE8"

_PROJECT_BLOCK = re.compile(
    r'^Project\("\{(?P<type>[^}]+)\}"\)\s*=\s*"(?P<name>[^"]*)"\s*,\s*"(?P<path>[^"]*)"\s*,'
    r'\s*"\{(?P<guid>[^}]+)\}"(?P<body>.*?)^EndProject\s*$',
    re.MULTILINE | re.DOTALL,
)
_WEB_PROP = re.compile(r'^\s*(?P<key>[\w.]+)\s*=\s*"(?P<value>[^"]*)"', re.MULTILINE)


@dataclass
class ProjectInfo:
    path: Path
    rel: str
    side: str
    sdk_style: bool
    assembly_name: str
    target_frameworks: list[str]
    project_refs: list[str] = field(default_factory=list)   # workspace-relative
    hint_paths: list[str] = field(default_factory=list)     # absolute, posix
    solutions: list[str] = field(default_factory=list)      # workspace-relative
    # Every packages/ folder the project points into (HintPaths, <Import>s of
    # package build targets, EnsureNuGetPackageBuildImports conditions).  They
    # are not always the same folder in this workspace, and packages.config
    # restore fills exactly one per run, so the build restores into each.
    packages_dirs: list[str] = field(default_factory=list)
    parse_error: str | None = None


@dataclass
class WebsiteInfo:
    name: str
    solution: str            # workspace-relative .sln
    root: Path
    rel: str
    target_framework: str | None
    referenced_projects: list[str] = field(default_factory=list)  # workspace-relative


@dataclass
class SolutionInfo:
    path: Path
    rel: str
    projects: list[str] = field(default_factory=list)
    missing_projects: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)


@dataclass
class Discovery:
    solutions: list[SolutionInfo]
    projects: dict[str, ProjectInfo]      # keyed by workspace-relative path
    websites: list[WebsiteInfo]


def discover(cfg: Config) -> Discovery:
    slns: list[Path] = []
    projs: list[Path] = []
    for path in walk_paths(cfg):
        if path.suffix.lower() == ".sln":
            slns.append(path)
        elif path.suffix.lower() == ".csproj":
            projs.append(path)

    projects: dict[str, ProjectInfo] = {}
    for p in projs:
        info = _read_project(cfg, p)
        projects[info.rel] = info
    by_lower = {k.lower(): k for k in projects}

    solutions: list[SolutionInfo] = []
    websites: list[WebsiteInfo] = []
    for s in slns:
        sol, sites = _read_solution(cfg, s, by_lower)
        solutions.append(sol)
        websites.extend(sites)
        for rel in sol.projects:
            projects[rel].solutions.append(sol.rel)

    return Discovery(solutions=solutions, projects=projects, websites=websites)


# --------------------------------------------------------------- projects

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_project(cfg: Config, path: Path) -> ProjectInfo:
    rel = cfg.rel(path)
    info = ProjectInfo(
        path=path, rel=rel, side=cfg.side_of(path), sdk_style=False,
        assembly_name=path.stem, target_frameworks=[],
    )
    try:
        root = ET.fromstring(path.read_bytes())
    except (ET.ParseError, OSError) as e:
        info.parse_error = f"{type(e).__name__}: {e}"
        return info

    info.sdk_style = bool(root.get("Sdk")) or any(_local(el.tag) == "Sdk" for el in root)
    base = path.parent
    for el in root.iter():
        tag = _local(el.tag)
        text = (el.text or "").strip()
        if tag == "AssemblyName" and text and "$(" not in text:
            info.assembly_name = text
        elif tag in ("TargetFramework", "TargetFrameworks") and text:
            info.target_frameworks.extend(t for t in text.split(";") if t)
        elif tag == "TargetFrameworkVersion" and text:
            info.target_frameworks.append(text)
        elif tag == "ProjectReference" and el.get("Include"):
            ref = _resolve(base, el.get("Include"))
            info.project_refs.append(cfg.rel(ref))
        elif tag == "HintPath" and text and "$(" not in text:
            hp = _resolve(base, text)
            info.hint_paths.append(hp.as_posix())
            _add_packages_dir(info, hp)
        elif tag == "Import" and el.get("Project") and "$(" not in el.get("Project", ""):
            _add_packages_dir(info, _resolve(base, el.get("Project")))
        elif tag == "Error" and "packages" in (el.get("Condition") or "").lower():
            for m in _EXISTS.finditer(el.get("Condition") or ""):
                if "$(" not in m.group(1):
                    _add_packages_dir(info, _resolve(base, m.group(1)))
    info.target_frameworks = sorted(set(info.target_frameworks))
    return info


_EXISTS = re.compile(r"Exists\('([^']+)'\)", re.IGNORECASE)


def _add_packages_dir(info: ProjectInfo, p: Path) -> None:
    pk = _packages_dir_of(p)
    if pk and pk not in info.packages_dirs:
        info.packages_dirs.append(pk)


def _resolve(base: Path, include: str) -> Path:
    return Path(os.path.normpath(base / include.replace("\\", "/")))


def _packages_dir_of(hint: Path) -> str | None:
    """``.../packages/Foo.1.2/lib/...`` -> ``.../packages``: where packages.config restores."""
    parts = hint.parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() == "packages":
            return Path(*parts[: i + 1]).as_posix()
    return None


# --------------------------------------------------------------- solutions

def _read_solution(cfg: Config, path: Path, by_lower: dict[str, str]):
    sol = SolutionInfo(path=path, rel=cfg.rel(path))
    sites: list[WebsiteInfo] = []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return sol, sites

    guid_to_rel: dict[str, str] = {}
    blocks = list(_PROJECT_BLOCK.finditer(text))
    for m in blocks:
        ptype = m["type"].upper()
        if ptype in (SOLUTION_FOLDER_TYPE, WEBSITE_TYPE):
            continue
        rel_in_sln = m["path"]
        if not rel_in_sln.lower().endswith(".csproj"):
            continue
        rel = cfg.rel(_resolve(path.parent, rel_in_sln))
        key = by_lower.get(rel.lower())
        if key is None:
            sol.missing_projects.append(rel)
            continue
        sol.projects.append(key)
        guid_to_rel[m["guid"].upper()] = key

    for m in blocks:
        if m["type"].upper() != WEBSITE_TYPE:
            continue
        props = {k.group("key"): k.group("value") for k in _WEB_PROP.finditer(m["body"])}
        physical = props.get("Debug.AspNetCompiler.PhysicalPath") or m["path"]
        if physical.lower().startswith(("http://", "https://")):
            continue  # an IIS-hosted site: no physical root in the workspace
        root = _resolve(path.parent, physical.rstrip("\\/"))
        if not root.is_dir() or not cfg.contains_source(root):
            continue
        refs = []
        for entry in props.get("ProjectReferences", "").split(";"):
            g = entry.split("|", 1)[0].strip().strip("{}").upper()
            if g in guid_to_rel and guid_to_rel[g] not in refs:
                refs.append(guid_to_rel[g])
        tfm = props.get("TargetFrameworkMoniker")
        site = WebsiteInfo(
            name=m["name"], solution=sol.rel, root=root, rel=cfg.rel(root),
            target_framework=tfm.replace("%3D", "=").replace("%2C", ",") if tfm else None,
            referenced_projects=refs,
        )
        sites.append(site)
        sol.websites.append(site.rel)
    return sol, sites


# --------------------------------------------------------------- ordering

def assembly_producers(projects: dict[str, ProjectInfo]) -> dict[tuple[str, str], list[str]]:
    """(side, lower assembly name) -> projects producing it.  More than one = ambiguous."""
    out: dict[tuple[str, str], list[str]] = {}
    for rel, p in projects.items():
        out.setdefault((p.side, p.assembly_name.lower()), []).append(rel)
    return out


def build_levels(projects: dict[str, ProjectInfo]) -> tuple[list[list[str]], list[str]]:
    """Topological levels over ProjectReference + HintPath-to-our-own-output edges.

    Returns (levels, cycle_members).  Projects in a cycle are appended as a last
    level and reported, never dropped.
    """
    producers = assembly_producers(projects)
    deps: dict[str, set[str]] = {rel: set() for rel in projects}
    for rel, p in projects.items():
        for r in p.project_refs:
            if r in projects and r != rel:
                deps[rel].add(r)
        for hp in p.hint_paths:
            if "/packages/" in hp.lower():
                continue
            name = Path(hp).stem.lower()
            prod = producers.get((p.side, name), [])
            if len(prod) == 1 and prod[0] != rel:
                deps[rel].add(prod[0])

    levels: list[list[str]] = []
    done: set[str] = set()
    remaining = set(projects)
    while remaining:
        ready = sorted(r for r in remaining if deps[r] <= done)
        if not ready:
            break
        levels.append(ready)
        done.update(ready)
        remaining.difference_update(ready)
    cycle = sorted(remaining)
    if cycle:
        levels.append(cycle)
    return levels, cycle
