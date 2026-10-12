"""Fill the .NET Framework reference-assembly root the build and the Roslyn load use.

Projects may target .NET Framework 4.5 - 4.7.1, and no Visual Studio component
installs those reference assemblies any more.  Without them MSBuild resolves no
framework references and the project compiles against nothing (CS0518 on every
type), in the real build and in the index alike.

So the build step keeps one folder, in the Microsoft.NETFramework.ReferenceAssemblies
layout (<root>/.NETFramework/v4.x/), holding every version the workspace targets:

* 4.5 - 4.7.1 from Microsoft's NuGet packages, downloaded from nuget.org only
  after ``dotnet nuget verify`` confirms Microsoft's author signature;
* 4.7.2 - 4.8.1 copied from the targeting packs Visual Studio installs.

Only the build step's own folder (``<index_dir>/build/refasm``) is filled; a
target_framework_root the user configured elsewhere is theirs and left alone.
A version already present is never fetched again.
"""

from __future__ import annotations

import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from ..config import Config

Progress = Callable[[str], None]

PACKAGE_VERSION = "1.0.3"
FROM_NUGET = {
    "v4.5": "net45", "v4.5.2": "net452", "v4.6": "net46",
    "v4.6.1": "net461", "v4.6.2": "net462", "v4.7.1": "net471",
}
FROM_TARGETING_PACKS = ("v4.7.2", "v4.8", "v4.8.1")
INSTALLED_ROOT = Path("C:/Program Files (x86)/Reference Assemblies/Microsoft/Framework/.NETFramework")
_URL = "https://api.nuget.org/v3-flatcontainer/{id}/{v}/{id}.{v}.nupkg"


def managed_root(cfg: Config) -> Path:
    return cfg.paths.build_dir / "refasm"


def is_managed(cfg: Config) -> bool:
    return Path(cfg.build.target_framework_root).resolve() == managed_root(cfg).resolve()


def present(root: Path, version: str) -> bool:
    return (root / ".NETFramework" / version / "mscorlib.dll").is_file()


def ensure(cfg: Config, versions: set[str], progress: Progress = print) -> dict:
    """Make every version in *versions* present under the managed root.

    Returns {"root", "added": [...], "failed": {version: reason}}.  Failures are
    reported, not raised: the build still runs, and the projects that target a
    missing version are listed in the manifest as they were before.
    """
    root = managed_root(cfg)
    report: dict = {"root": root.as_posix(), "added": [], "failed": {}}
    todo = sorted(v for v in versions if not present(root, v))
    if not todo:
        return report
    (root / ".NETFramework").mkdir(parents=True, exist_ok=True)
    for v in todo:
        try:
            if v in FROM_NUGET:
                _from_nuget(cfg, root, v, progress)
            elif v in FROM_TARGETING_PACKS:
                _from_targeting_pack(root, v, progress)
            else:
                raise RuntimeError("no known source for this version")
            if not present(root, v):
                raise RuntimeError("mscorlib.dll still missing after install")
            report["added"].append(v)
        except Exception as e:  # recorded; one version must not stop the others
            report["failed"][v] = f"{type(e).__name__}: {e}"
            progress(f"  reference assemblies {v}: FAILED {e}")
    return report


def _from_targeting_pack(root: Path, v: str, progress: Progress) -> None:
    src = INSTALLED_ROOT / v
    if not (src / "mscorlib.dll").is_file():
        raise FileNotFoundError(
            f"{src} not installed; add the .NET Framework {v[1:]} targeting pack "
            "(scripts/provision-vm.ps1)")
    progress(f"  reference assemblies {v}: copying {src}")
    shutil.copytree(src, root / ".NETFramework" / v, dirs_exist_ok=True)


def _from_nuget(cfg: Config, root: Path, v: str, progress: Progress) -> None:
    pkg_id = f"microsoft.netframework.referenceassemblies.{FROM_NUGET[v]}"
    url = _URL.format(id=pkg_id, v=PACKAGE_VERSION)
    tmp = cfg.paths.build_dir / "refasm-download"
    tmp.mkdir(parents=True, exist_ok=True)
    nupkg = tmp / f"{pkg_id}.{PACKAGE_VERSION}.nupkg"
    progress(f"  reference assemblies {v}: downloading {url}")
    with urllib.request.urlopen(url, timeout=300) as r, open(nupkg, "wb") as f:
        shutil.copyfileobj(r, f)
    try:
        _verify_microsoft_signature(nupkg)
        prefix = "build/.NETFramework/"
        with zipfile.ZipFile(nupkg) as z:
            for e in z.infolist():
                if not e.filename.startswith(prefix) or e.is_dir():
                    continue
                rel = Path(e.filename[len("build/"):])
                if ".." in rel.parts:
                    continue
                dst = root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                with z.open(e) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
    finally:
        nupkg.unlink(missing_ok=True)


def _verify_microsoft_signature(nupkg: Path) -> None:
    """Refuse a package whose author signature is not Microsoft's."""
    proc = subprocess.run(["dotnet", "nuget", "verify", "--all", str(nupkg)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)
    out = proc.stdout + proc.stderr
    if proc.returncode != 0 or "CN=Microsoft Corporation" not in out:
        raise RuntimeError(f"dotnet nuget verify did not confirm a Microsoft signature "
                           f"(exit {proc.returncode}); not extracted")
