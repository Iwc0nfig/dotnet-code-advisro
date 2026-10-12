from pathlib import Path
import os
import pytest
import tomli_w
from dotnet_code_advisor.config import load_config, find_config
from dotnet_code_advisor.inventory import walk_files
from dotnet_code_advisor.build.discover import discover

def make_config(tmp_path, **updates):
    ws = tmp_path / "workspaces"
    (ws / "old-any-name").mkdir(parents=True, exist_ok=True)
    (ws / "new-any-name").mkdir(parents=True, exist_ok=True)
    paths = {"workspaces": "workspaces", "net48_root": "workspaces/old-any-name",
             "net8_root": "workspaces/new-any-name", "index_dir": "index"}
    paths.update(updates)
    file = tmp_path / "custom.toml"
    file.write_text(tomli_w.dumps({"paths": paths}), encoding="utf-8")
    return file

def test_paths_are_relative_to_config_and_side_uses_roots(tmp_path, monkeypatch, capsys):
    file = make_config(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    cfg = load_config(file)
    assert cfg.paths.workspaces == tmp_path / "workspaces"
    assert cfg.side_of(cfg.paths.net48_root / "Modern.cs") == "net48"
    assert cfg.side_of(cfg.paths.net8_root / "Legacy.cs") == "net8"
    assert capsys.readouterr().out == ""

@pytest.mark.parametrize("side", ["net48", "net8"])
def test_each_side_can_be_used_alone_without_solutions_or_website(tmp_path, side):
    other = "net8" if side == "net48" else "net48"
    cfg = load_config(make_config(tmp_path, **{other + "_root": ""}))
    assert set(cfg.paths.roots) == {side}
    assert cfg.paths.website_root is None
    assert getattr(cfg.paths, side + "_solution") is None
    (cfg.paths.roots[side] / "Demo.cs").write_text("class Demo {}")
    assert len(list(walk_files(cfg))) == 1

@pytest.mark.parametrize("updates,match", [
    ({"net48_root": "", "net8_root": ""}, "at least one"),
    ({"net8_root": "workspaces/old-any-name"}, "overlap"),
    ({"net8_root": "workspaces"}, "overlap"),
    ({"net8_root": "../outside"}, "under workspaces"),
    ({"net8_root": "workspaces/missing"}, "not a directory"),
    ({"index_dir": "workspaces/new-any-name/index"}, "separate"),
    ({"index_dir": "."}, "separate"),
    ({"website_root": "workspaces/new-any-name"}, "under net48_root"),
    ({"net8_solution": "workspaces/new-any-name/missing.sln"}, "does not exist"),
])
def test_invalid_roots_fail_loudly(tmp_path, updates, match):
    with pytest.raises((ValueError, NotADirectoryError, FileNotFoundError), match=match):
        load_config(make_config(tmp_path, **updates))

def test_explicit_config_overrides_environment(tmp_path, monkeypatch):
    file = make_config(tmp_path)
    monkeypatch.setenv("DOTNET_ADVISOR_CONFIG", str(tmp_path / "missing.toml"))
    assert load_config(file).source == file
    monkeypatch.setenv("DOTNET_ADVISOR_CONFIG", str(file))
    assert load_config().source == file

def test_config_search_does_not_walk_into_parent_deployment(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("[paths]")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(FileNotFoundError):
        find_config()

def test_configured_exclusions_apply_to_inventory_and_discovery(tmp_path):
    file = make_config(tmp_path)
    with file.open("a", encoding="utf-8") as handle:
        handle.write('\n[index]\nexclude_dirs = ["private"]\nexclude_suffixes = ["-copy"]\n')
    cfg = load_config(file)
    for name in ("visible", "private", "shadow-copy"):
        directory = cfg.paths.net8_root / name
        directory.mkdir()
        (directory / "Demo.cs").write_text("class Demo {}")
        (directory / "Demo.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"/>')
    assert len(list(walk_files(cfg))) == 1
    assert len(discover(cfg).projects) == 1
