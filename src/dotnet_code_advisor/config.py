"""Portable configuration and explicit source boundaries."""
from __future__ import annotations
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_CONFIG_NAME = "config.toml"

@dataclass(frozen=True)
class Paths:
    workspaces: Path
    index_dir: Path
    net48_solution: Path | None = None
    net8_solution: Path | None = None
    website_root: Path | None = None
    roslyn_host_net48: Path | None = None
    roslyn_host_net8: Path | None = None
    netfx_ref_assemblies: Path | None = None
    markup_codegen: Path | None = None
    net48_root: Path | None = None
    net8_root: Path | None = None

    @property
    def roots(self) -> dict[str, Path]:
        return {side: root for side, root in
                (("net48", self.net48_root), ("net8", self.net8_root)) if root is not None}

    @property
    def db_path(self) -> Path:
        return self.index_dir / "graph.db"

    @property
    def build_dir(self) -> Path:
        return self.index_dir / "build"

    @property
    def markup_dir(self) -> Path:
        return self.build_dir / "markup"

    @property
    def build_manifest(self) -> Path:
        return self.build_dir / "manifest.json"

    @property
    def designer_dir(self) -> Path:
        return self.index_dir / "designer"

    @property
    def log_dir(self) -> Path:
        return self.index_dir / "logs"

    @property
    def aspnet_controls_json(self) -> Path:
        return self.index_dir / "aspnet_controls.json"

@dataclass(frozen=True)
class Limits:
    roslyn_load_timeout_s: int = 900
    roslyn_query_timeout_s: int = 45
    max_results_default: int = 50
    max_results_hard: int = 500
    read_file_max_lines: int = 400

@dataclass(frozen=True)
class IndexOpts:
    max_file_bytes: int = 2_000_000
    exclude_dirs: tuple[str, ...] = (
        ".git", ".vs", ".svn", ".index", ".venv", "obj", "bin", "node_modules",
        "packages", "TestResults", "PrecompiledWeb", "_ReSharper.Caches",
        ".nuget", "BuildProcessTemplates", "App_Data", "__pycache__",
    )
    exclude_suffixes: tuple[str, ...] = ("-release",)
    vendor_js_pattern: str = (
        r"(\.min\.js$)|(^jquery)|(^bootstrap)|(^ag-grid)|(^daypilot)|(^ckeditor)"
        r"|(^moment)|(^pdf\.)|(^xlsx)|(^telerik)|(^angular)|(^react)|(^vue)"
        r"|(^lodash)|(^underscore)|(^d3[.-])|(^chart)|(^hammer)|(^cropper)"
        r"|(^dropzone)|(^autonumeric)|(^imask)|(^html2canvas)|(^viewer\.js$)"
    )

@dataclass(frozen=True)
class BuildOpts:
    configuration: str = "Debug"
    msbuild: str = ""
    parallel: int = 0
    timeout_s: int = 1800
    target_framework_root: str = ""
    properties: dict[str, str] = field(default_factory=lambda: {
        "RestorePackagesConfig": "true", "RestoreIgnoreFailedSources": "true",
    })

@dataclass(frozen=True)
class Config:
    paths: Paths
    limits: Limits
    index: IndexOpts
    build: BuildOpts = field(default_factory=BuildOpts)
    source: Path = field(default_factory=lambda: Path(DEFAULT_CONFIG_NAME))

    def rel(self, p: Path | str) -> str:
        pp = Path(str(p).replace("\\", "/"))
        try:
            return pp.resolve().relative_to(self.paths.workspaces).as_posix()
        except ValueError:
            return pp.as_posix()

    def abs_of(self, rel_path: str) -> str:
        return (self.paths.workspaces / rel_path).as_posix()

    def synth_abs(self, name: str) -> str:
        return (self.paths.designer_dir / name).as_posix()

    def abs_posix(self, p: Path | str) -> str:
        return PurePosixPath(str(Path(str(p).replace("\\", "/")))).as_posix()

    def side_of(self, path: Path | str) -> str:
        pp = Path(str(path).replace("\\", "/")).resolve()
        for side, root in self.paths.roots.items():
            if pp.is_relative_to(root.resolve()):
                return side
        raise ValueError("Path is outside the configured source roots")

    def contains_source(self, path: Path | str) -> bool:
        try:
            self.side_of(path)
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    def ensure_index_dirs(self) -> None:
        for directory in (self.paths.index_dir, self.paths.designer_dir,
                          self.paths.log_dir, self.paths.build_dir):
            directory.mkdir(parents=True, exist_ok=True)

def find_config(start: Path | None = None) -> Path:
    # Do not search outside the public project for another deployment's config.
    for directory in ((start or Path.cwd()).resolve(), Path(__file__).resolve().parents[2]):
        candidate = directory / DEFAULT_CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("config.toml not found. Copy config.example.toml or pass --config.")

def load_config(path: str | Path | None = None) -> Config:
    explicit = path or os.environ.get("DOTNET_ADVISOR_CONFIG", "").strip()
    cfg_path = (Path(explicit) if explicit else find_config()).resolve()
    with cfg_path.open("rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)
    p = raw.get("paths", {})
    if any(not p.get(key) for key in ("workspaces", "index_dir")):
        raise ValueError("[paths] workspaces and index_dir must be nonempty")

    def resolve(value) -> Path | None:
        if value is None or not str(value).strip():
            return None
        candidate = Path(str(value).replace("\\", "/"))
        return (candidate if candidate.is_absolute() else cfg_path.parent / candidate).resolve()

    paths = Paths(**{key: resolve(p.get(key)) for key in Paths.__dataclass_fields__})
    if not paths.roots:
        raise ValueError("Configure at least one of net48_root or net8_root")
    roots = list(paths.roots.values())
    if len(roots) == 2 and (roots[0].is_relative_to(roots[1]) or roots[1].is_relative_to(roots[0])):
        raise ValueError("Source roots must not overlap")
    for side, root in paths.roots.items():
        if not root.is_relative_to(paths.workspaces):
            raise ValueError(f"{side}_root must be under workspaces")
        if paths.index_dir.is_relative_to(root) or root.is_relative_to(paths.index_dir):
            raise ValueError("index_dir must be separate from source roots")
    if paths.website_root is not None and (paths.net48_root is None or
            not paths.website_root.is_relative_to(paths.net48_root)):
        raise ValueError("website_root must be under net48_root")
    for side in ("net48", "net8"):
        solution = getattr(paths, f"{side}_solution")
        if solution is not None:
            root = paths.roots.get(side)
            if root is None or not solution.is_relative_to(root):
                raise ValueError(f"{side}_solution must be under {side}_root")
            if not solution.is_file():
                raise FileNotFoundError(f"Solution does not exist: {solution}")
    for key, value in (("workspaces", paths.workspaces), *paths.roots.items(),
                       ("website_root", paths.website_root)):
        if value is not None and not value.is_dir():
            raise NotADirectoryError(f"{key} is not a directory: {value}")
    opts_raw = dict(raw.get("index", {}))
    for key in ("exclude_dirs", "exclude_suffixes"):
        if key in opts_raw:
            if not isinstance(opts_raw[key], list) or not all(isinstance(x, str) for x in opts_raw[key]):
                raise ValueError(f"{key} must be an array of strings")
            opts_raw[key] = tuple(opts_raw[key])
    opts = IndexOpts(**opts_raw)
    re.compile(opts.vendor_js_pattern)
    if opts.max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be positive")
    limits = Limits(**raw.get("limits", {}))
    if any(getattr(limits, key) <= 0 for key in Limits.__dataclass_fields__):
        raise ValueError("All limits must be positive")
    return Config(paths, limits, opts, _build_opts(raw.get("build", {}), cfg_path.parent,
                                                paths.index_dir), cfg_path)

def _build_opts(raw: dict[str, Any], cfg_dir: Path, index_dir: Path) -> BuildOpts:
    raw = dict(raw)
    props = dict(BuildOpts().properties)
    props.update({key: str(value) for key, value in raw.pop("properties", {}).items()})
    tfr = str(raw.pop("target_framework_root", "") or "").strip()
    root = Path(tfr) if tfr else index_dir / "build/refasm"
    if not root.is_absolute():
        root = cfg_dir / root
    if raw.get("msbuild"):
        exe = Path(raw["msbuild"])
        raw["msbuild"] = str((exe if exe.is_absolute() else cfg_dir / exe).resolve())
    return BuildOpts(properties=props, target_framework_root=root.resolve().as_posix(), **raw)
