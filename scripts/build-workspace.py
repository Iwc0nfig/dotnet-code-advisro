"""Build the workspace copy so the index sees complete inputs.  plan: Phase 1.

Run by the scheduled pipeline after the latest code is copied into the
workspace, and before scripts/reindex.py:

    get latest -> Workspace -> build-workspace.py -> reindex.py -> run

Restores and builds every project (writing only MSBuild's bin/ and obj/ inside
the workspace), asks ASP.NET for every page's generated code, and writes
.index/build/manifest.json.  A failed project build does not stop the pass: it
is recorded, and the index reports it as a project whose references may be
incomplete.
"""
import argparse
import sys
import time

from dotnet_code_advisor.config import load_config
from dotnet_code_advisor.build.runner import run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--only", default=None,
                    help="substring filter on workspace-relative project/site paths (for testing)")
    ap.add_argument("--skip-restore", action="store_true")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--skip-markup", action="store_true")
    ap.add_argument("--no-fetch-refasm", action="store_true",
                    help="do not download missing .NET Framework reference assemblies from nuget.org")
    a = ap.parse_args()
    cfg = load_config(a.config)
    print(f"config     : {cfg.source}")
    print(f"workspaces : {cfg.paths.workspaces}")
    print(f"build dir  : {cfg.paths.build_dir}")
    t0 = time.time()
    m = run(cfg, only=a.only, skip_restore=a.skip_restore, skip_build=a.skip_build,
            skip_markup=a.skip_markup, fetch_refasm=not a.no_fetch_refasm,
            progress=lambda s: print(s, flush=True))
    print("-" * 60)
    for k, v in m["summary"].items():
        print(f"{k:32}: {v}")
    for v, ps in m["missing_targeting_packs"].items():
        print(f"missing .NET Framework {v} reference assemblies: {len(ps)} project(s)")
    print(f"manifest   : {cfg.paths.build_manifest}")
    print(f"elapsed    : {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
