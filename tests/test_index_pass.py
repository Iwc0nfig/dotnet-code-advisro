"""The index pass (always a full rebuild).

These tests build a small synthetic workspace and index it, rather than touching
the real one: the advisor must never write under paths.workspaces, and a test
that modified real source would violate the guarantee it is meant to protect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotnet_code_advisor.config import Config, IndexOpts, Limits, Paths
from dotnet_code_advisor.index.runner import run
from dotnet_code_advisor import db as dbm

PAGE = """\
<%@ Page Language="C#" AutoEventWireup="true" CodeFile="demo.aspx.cs" Inherits="demo_page" %>
<asp:Content ID="Body" runat="server">
  <asp:Button ID="btnSave" runat="server" OnClick="btnSave_Click" Text="Save" />
  <asp:TextBox ID="txtName" runat="server" />
</asp:Content>
"""

CODEBEHIND = """\
using System;
public partial class demo_page
{
    protected void Page_Load(object sender, EventArgs e)
    {
        txtName.Text = "hello";
    }
    protected void btnSave_Click(object sender, EventArgs e)
    {
        Helper.Save(txtName.Text);
    }
}
"""

HELPER = """\
public static class Helper
{
    public static void Save(string value) { }
}
"""


@pytest.fixture
def sandbox(tmp_path: Path) -> Config:
    ws = tmp_path / "Workspaces"
    app = ws / "Legacy" / "App"
    app.mkdir(parents=True)
    (app / "demo.aspx").write_text(PAGE, encoding="utf-8")
    (app / "demo.aspx.cs").write_text(CODEBEHIND, encoding="utf-8")
    (app / "App_Code").mkdir()
    (app / "App_Code" / "Helper.cs").write_text(HELPER, encoding="utf-8")
    (ws / "Modern").mkdir(parents=True)

    index = tmp_path / ".index"
    return Config(
        paths=Paths(
            workspaces=ws,
            net48_root=ws / "Legacy",
            net8_root=ws / "Modern",
            net48_solution=ws / "Legacy" / "Legacy.sln",
            net8_solution=ws / "Modern" / "Modern.sln",
            website_root=app,
            index_dir=index,
            roslyn_host_net48=tmp_path / "nope48.exe",
            roslyn_host_net8=tmp_path / "nope8.exe",
            netfx_ref_assemblies=tmp_path / "refs",
        ),
        limits=Limits(),
        index=IndexOpts(),
        source=tmp_path / "config.toml",
    )


def _con(cfg: Config):
    return dbm.connect(cfg.paths.db_path, read_only=True)


def test_full_pass_indexes_the_sandbox(sandbox):
    st = run(sandbox)
    assert st.files_indexed == 3
    assert st.pages == 1
    assert st.controls == 3  # Content, Button, TextBox
    assert st.designer_files == 1
    con = _con(sandbox)
    ids = {r[0] for r in con.execute("SELECT ctl_id FROM controls")}
    assert ids == {"Body", "btnSave", "txtName"}


def test_control_ids_used_in_codebehind_are_flagged(sandbox):
    run(sandbox)
    con = _con(sandbox)
    used = {
        r[0]
        for r in con.execute("SELECT ctl_id FROM controls WHERE used_in_codebehind=1")
    }
    assert "txtName" in used
    assert "btnSave" not in used  # declared in markup, never touched in code


def test_server_handler_edge_points_at_the_method(sandbox):
    run(sandbox)
    con = _con(sandbox)
    row = con.execute(
        "SELECT dst_name, evidence FROM edges WHERE edge_type='binds_handler_server'"
    ).fetchone()
    assert row["dst_name"] == "btnSave_Click"
    assert "OnClick" in row["evidence"] or "onclick" in row["evidence"]


def test_editing_markup_reindexes_and_resynthesises(sandbox):
    run(sandbox)
    con = _con(sandbox)
    before = {r[0] for r in con.execute("SELECT ctl_id FROM controls")}
    assert "txtEmail" not in before
    con.close()

    page = sandbox.paths.website_root / "demo.aspx"
    page.write_text(
        PAGE.replace(
            '<asp:TextBox ID="txtName" runat="server" />',
            '<asp:TextBox ID="txtName" runat="server" />\n'
            '  <asp:TextBox ID="txtEmail" runat="server" />',
        ),
        encoding="utf-8",
    )
    st = run(sandbox)
    assert st.files_indexed == 3  # every pass is a full rebuild

    con = _con(sandbox)
    after = {r[0] for r in con.execute("SELECT ctl_id FROM controls")}
    assert "txtEmail" in after

    synth = con.execute(
        "SELECT synth_path FROM pages WHERE synth_path IS NOT NULL"
    ).fetchone()["synth_path"]
    # pages.synth_path stores a basename; the directory is always designer_dir.
    assert "/" not in synth
    text = Path(sandbox.synth_abs(synth)).read_text(encoding="utf-8")
    assert "txtEmail" in text


def test_deleting_a_file_removes_its_rows(sandbox):
    run(sandbox)
    (sandbox.paths.website_root / "App_Code" / "Helper.cs").unlink()
    run(sandbox)
    con = _con(sandbox)
    assert con.execute(
        "SELECT COUNT(*) FROM files WHERE rel_path LIKE '%Helper.cs'"
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM symbols WHERE name='Save'"
    ).fetchone()[0] == 0


def test_nothing_is_written_under_the_workspace(sandbox):
    """The whole index pass must leave the source tree byte-identical."""
    ws = sandbox.paths.workspaces
    before = {
        p: (p.stat().st_size, p.read_bytes())
        for p in sorted(ws.rglob("*"))
        if p.is_file()
    }
    run(sandbox)
    after = {
        p: (p.stat().st_size, p.read_bytes())
        for p in sorted(ws.rglob("*"))
        if p.is_file()
    }
    assert set(before) == set(after), "files appeared or vanished under the workspace"
    for p in before:
        assert before[p] == after[p], f"{p} was modified"
    # And the synthesised partial went to .index/, not next to the markup.
    assert not list(ws.rglob("*.synth.designer.cs"))
    assert list(sandbox.paths.designer_dir.glob("*.synth.designer.cs"))


def test_rerun_deletes_the_old_db_but_keeps_other_index_files(sandbox):
    """graph.db and its sidecars are deleted, not emptied; nothing else is."""
    first = run(sandbox)
    index = sandbox.paths.index_dir
    controls = sandbox.paths.aspnet_controls_json
    controls.write_text(json.dumps({"webcontrols": {}}), encoding="utf-8")
    stale_wal = index / "graph.db-wal"
    stale_wal.write_bytes(b"not a real wal")

    msgs: list[str] = []
    second = run(sandbox, progress=msgs.append)

    assert any(m.startswith("removed") and m.endswith("graph.db") for m in msgs), msgs
    assert controls.is_file()
    assert list(sandbox.paths.designer_dir.glob("*.synth.designer.cs"))
    # Rebuilt from nothing: nothing carried over, nothing duplicated.
    assert (second.files_indexed, second.symbols, second.controls, second.refs) == (
        first.files_indexed, first.symbols, first.controls, first.refs,
    )
    con = _con(sandbox)
    assert con.execute("SELECT COUNT(*) FROM files").fetchone()[0] == first.files_indexed + sum(
        first.files_skipped.values()
    )
