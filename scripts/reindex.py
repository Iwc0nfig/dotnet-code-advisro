"""Driver for an index pass (always a full rebuild)."""
import argparse, json, sys, time
from dotnet_code_advisor.config import load_config
from dotnet_code_advisor.db import connect, get_meta
from dotnet_code_advisor.index.runner import run

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=None,
        help="config.toml to use; defaults to $DOTNET_ADVISOR_CONFIG, then the one beside this tree",
    )
    ap.add_argument(
        "--no-roslyn",
        action="store_true",
        help="skip the compiler index (Roslyn --index pass); the graph layers only",
    )
    ap.add_argument(
        "--roslyn-only",
        action="store_true",
        help="rebuild only the compiler index on the existing graph.db",
    )
    ap.add_argument(
        "--allow-missing-controls",
        action="store_true",
        help="build even without .index/aspnet_controls.json (every asp: tag then gets the base Control type)",
    )
    a = ap.parse_args()
    cfg = load_config(a.config)
    # Without a reflected System.Web control table, asp: tags fall back to Control: a complete-looking index with
    # wrong types.  Refuse up front rather than after a 20-minute pass.
    if cfg.paths.website_root is not None and not a.roslyn_only and not a.allow_missing_controls and not cfg.paths.aspnet_controls_json.is_file():
        print(f"ERROR: {cfg.paths.aspnet_controls_json} not found.\n"
              "Run first:  powershell -ExecutionPolicy Bypass -File scripts\\gen_aspnet_controls.ps1\n"
              "(or pass --allow-missing-controls to build with every asp: tag untyped)",
              file=sys.stderr)
        return 2
    print(f"config        : {cfg.source}")
    print(f"workspaces    : {cfg.paths.workspaces}")
    print(f"index_dir     : {cfg.paths.index_dir}")
    t0 = time.time()
    if a.roslyn_only:
        from dotnet_code_advisor.index.runner import run_roslyn_only
        out = run_roslyn_only(cfg, progress=lambda m: print(m, flush=True))
        print(json.dumps(out, indent=2))
        print(f"elapsed       : {time.time()-t0:.1f}s")
        return 0
    st = run(cfg, progress=lambda m: print(m, flush=True), roslyn=not a.no_roslyn)
    print("-" * 60)
    print(f"files indexed : {st.files_indexed:,}   skipped: {dict(st.files_skipped)}")
    print(f"lines indexed : {st.lines_indexed:,}")
    print(f"symbols       : {st.symbols:,}")
    print(f"refs          : {st.refs:,}")
    print(f"edges         : {st.edges:,}")
    print(f"controls      : {st.controls:,}   pages: {st.pages:,}")
    print(f"designers     : {st.designer_files} files / {st.designer_fields:,} fields")
    print(f"  in-template : {st.designer_template_fields:,}   CS0102 avoided: {len(st.designer_collisions)}")
    print(f"parse partial : {len(st.parse_partial)}")
    con = connect(cfg.paths.db_path, read_only=True)
    print(f"case-resolved : {get_meta(con, 'case_resolved_count', '0')} path refs whose "
          "case in markup differs from the case on disk")
    con.close()
    print(f"elapsed       : {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
