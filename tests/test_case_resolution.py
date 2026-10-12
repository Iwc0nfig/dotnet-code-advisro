"""Portable source analysis and regression checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from dotnet_code_advisor.index import designer as dz
from dotnet_code_advisor.index.fsmap import CaseMap


def test_exact_match_costs_nothing_and_counts_nothing(tmp_path):
    (tmp_path / "Default.aspx.cs").write_text("// x", encoding="utf-8")
    cm = CaseMap()
    got = cm.resolve(tmp_path / "Default.aspx.cs")
    assert got == tmp_path / "Default.aspx.cs"
    assert cm.case_resolved == 0


def test_a_mis_cased_codefile_still_resolves(tmp_path):
    """sign-in/Default.aspx writes CodeFile="default.aspx.cs" for Default.aspx.cs."""
    real = tmp_path / "Default.aspx.cs"
    real.write_text("// code-behind", encoding="utf-8")
    cm = CaseMap()
    got = cm.resolve(tmp_path / "default.aspx.cs")
    assert got == real
    assert got.name == "Default.aspx.cs", "must return the real name, not the written one"
    assert cm.case_resolved == 1


def test_a_mis_cased_master_page_still_resolves(tmp_path):
    """Five dashboards write ~/system/templates/exContent.master for excontent.master."""
    templates = tmp_path / "system" / "templates"
    templates.mkdir(parents=True)
    real = templates / "excontent.master"
    real.write_text("<%@ Master %>", encoding="utf-8")
    cm = CaseMap()
    assert cm.resolve_file(templates / "exContent.master") == real


def test_a_genuinely_missing_file_is_still_missing(tmp_path):
    """Case-insensitivity must not turn a broken reference into a resolved one."""
    (tmp_path / "Other.aspx.cs").write_text("// x", encoding="utf-8")
    cm = CaseMap()
    assert cm.resolve(tmp_path / "Missing.aspx.cs") is None
    assert cm.resolve_file(tmp_path / "Missing.aspx.cs") is None
    assert cm.case_resolved == 0


def test_a_directory_is_not_mistaken_for_a_file(tmp_path):
    (tmp_path / "Bin").mkdir()
    cm = CaseMap()
    assert cm.resolve(tmp_path / "bin") == tmp_path / "Bin"
    assert cm.resolve_file(tmp_path / "bin") is None


def test_two_files_differing_only_in_case_are_not_conflated(tmp_path):
    """The expensive error: returning the wrong file rather than no file.

    On a case-insensitive filesystem only one of these can exist, and the test
    degenerates to the exact-match case -- which is correct there.  On Linux both
    exist and each must resolve to itself.
    """
    lower = tmp_path / "widget.ascx"
    lower.write_text("lower", encoding="utf-8")
    upper = tmp_path / "Widget.ascx"
    upper.write_text("upper", encoding="utf-8")
    if lower.read_text(encoding="utf-8") == upper.read_text(encoding="utf-8"):
        pytest.skip("case-insensitive filesystem: only one of the two files exists")
    cm = CaseMap()
    assert cm.resolve(lower).read_text(encoding="utf-8") == "lower"
    assert cm.resolve(upper).read_text(encoding="utf-8") == "upper"


def test_listings_are_cached_per_directory(tmp_path):
    (tmp_path / "A.cs").write_text("", encoding="utf-8")
    cm = CaseMap()
    assert cm.resolve(tmp_path / "a.cs") is not None
    # A file created after the listing was cached is not seen until invalidation:
    # the indexer takes one consistent view of the tree per pass.
    (tmp_path / "B.cs").write_text("", encoding="utf-8")
    assert cm.resolve(tmp_path / "b.cs") is not None, "exact-ish probe should still stat"
    cm.invalidate(tmp_path)
    assert cm.resolve(tmp_path / "b.cs") is not None


def test_designer_detection_accepts_either_suffix_casing(tmp_path):
    """A missed real designer is the expensive direction: it causes CS0102."""
    markup = tmp_path / "Page.aspx"
    markup.write_text("<%@ Page %>", encoding="utf-8")
    real = tmp_path / "Page.aspx.Designer.cs"
    real.write_text("// VS wrote a capital D", encoding="utf-8")
    assert dz.has_real_designer(markup, CaseMap()) == real


def test_designer_detection_reports_absence(tmp_path):
    markup = tmp_path / "Page.aspx"
    markup.write_text("<%@ Page %>", encoding="utf-8")
    assert dz.has_real_designer(markup, CaseMap()) is None
