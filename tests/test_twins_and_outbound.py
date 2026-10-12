"""DTO twins (index/twins.py) and the outbound API walk (tools/api.py), on a synthetic index."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from dotnet_code_advisor import db as dbm
from dotnet_code_advisor.index import api_links, twins
from dotnet_code_advisor.tools import api

L = "Legacy/Modern/Modern.csproj"
N = "Modern/Demo.Next/Demo.Next.API/Demo.Next.API.csproj"


# ------------------------------------------------------------------ shape

def test_type_normalisation():
    assert twins.norm_type("List<AuditRecord?>?") == "[auditrecord]"
    assert twins.norm_type("AuditRecord[]") == "[auditrecord]"
    assert twins.norm_type("IEnumerable<Demo.Next.API.AuditRecord>") == "[auditrecord]"
    assert twins.norm_type("int?") == twins.norm_type("Nullable<int>") == "int"
    assert twins.norm_type("DateTime") != twins.norm_type("DateTimeOffset")


def test_shape_comparison():
    legacy = {"id": ("Id", "int"), "key": ("Key", "string"), "when": ("When", "DateTime")}
    new = {"id": ("ID", "long"), "key": ("key", "string?"), "user": ("User", "string")}
    sh = twins.compare(legacy, new)
    assert sh.common == ["Id", "Key"]
    assert sh.only_legacy == ["When"] and sh.only_new == ["User"]
    assert sh.type_differences == ["Id: int vs long"]
    assert sh.score == 0.5 and not sh.identical
    assert twins.compare({"a": ("A", "int")}, {"a": ("a", "int?")}).identical


# ------------------------------------------------------------------ synthetic index

def _sym(con, key, kind, name, fqn, path, line, side, container=None, typ=None, gen=0, json_name=None):
    con.execute(
        "INSERT INTO rsymbols(key,doc_id,project,side,kind,name,container_key,fqn,signature,accessibility,"
        "modifiers,path,line,col,end_line,is_generated,start_line,type,json_name) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, key.split("|", 1)[1], key.split("|", 1)[0], side, kind, name, container, fqn, fqn, "public", "",
         path, line, 5, line + 3, gen, line, typ, json_name))


@pytest.fixture()
def ctx(tmp_path):
    con = sqlite3.connect(tmp_path / "t.db")
    con.row_factory = sqlite3.Row
    con.executescript(dbm.SCHEMA_CORE)
    con.executescript(dbm.SCHEMA_ROSLYN)
    # legacy: page code-behind -> AuditSupport.GetAuditLog -> IAuditClient.Get (interface)
    #         AuditClass.Get implements it and posts AuditSearchRequest to /api/v{}/records/search
    page = "Legacy/App#website|M:ui_audit.Page_Load"
    support = f"{L}|M:Demo.AuditSupport.GetAuditLog"
    iface = f"{L}|M:Demo.Next.API.IAuditClient.Get"
    impl = f"{L}|M:Demo.Next.API.AuditClass.Get"
    other = f"{L}|M:Demo.Next.API.Teamwork.Tickets"
    _sym(con, page, "method", "Page_Load", "ui_audit.Page_Load", "Legacy/App/ui/audit.aspx.cs", 10, "net48")
    _sym(con, support, "method", "GetAuditLog", "Demo.AuditSupport.GetAuditLog", "Legacy/Modern/AuditSupport.cs", 5, "net48")
    _sym(con, iface, "method", "Get", "Demo.Next.API.IAuditClient.Get", "Legacy/Modern/NextApiClient/IAudit.cs", 3, "net48")
    _sym(con, impl, "method", "Get", "Demo.Next.API.AuditClass.Get", "Legacy/Modern/NextApiClient/Audit.cs", 95, "net48")
    _sym(con, other, "method", "Tickets", "Demo.Next.API.Teamwork.Tickets", "Legacy/Modern/Support.cs", 20, "net48")
    handler = f"{N}|M:Demo.Next.API.Endpoints.AuditEndpoint.SearchAuditEntriesAsync"
    _sym(con, handler, "method", "SearchAuditEntriesAsync", "Demo.Next.API.Endpoints.AuditEndpoint.SearchAuditEntriesAsync",
         "Modern/Demo.Next/Demo.Next.API/Endpoints/AuditEndpoint.cs", 46, "net8")
    # DTOs: legacy request vs API request, differently named; plus a same-name pair
    lreq, nreq = f"{L}|T:Demo.Next.API.AuditSearchByKeyRequest", f"{N}|T:Demo.Next.API.Model.SearchAuditRequest"
    _sym(con, lreq, "class", "AuditSearchByKeyRequest", "Demo.Next.API.AuditSearchByKeyRequest", "Legacy/Modern/NextApiClient/Audit.cs", 20, "net48")
    _sym(con, nreq, "record", "SearchAuditRequest", "Demo.Next.API.Model.SearchAuditRequest", "Modern/Demo.Next/Model/Audit.cs", 8, "net8")
    for k, (n, t) in {"Key": ("Key", "string"), "Take": ("Take", "int")}.items():
        _sym(con, f"{lreq.replace('T:', 'P:')}.{k}", "property", n, n, "x.cs", 21, "net48", container=lreq, typ=t)
    for k, (n, t) in {"Key": ("key", "string?"), "Take": ("Take", "long"), "Skip": ("Skip", "int")}.items():
        _sym(con, f"{nreq.replace('T:', 'P:')}.{k}", "property", n, n, "y.cs", 9, "net8", container=nreq, typ=t)
    lrec, nrec = f"{L}|T:Demo.Next.API.AuditRecord", f"{N}|T:Demo.Next.API.Model.AuditRecord"
    _sym(con, lrec, "class", "AuditRecord", "Demo.Next.API.AuditRecord", "a.cs", 1, "net48")
    _sym(con, nrec, "class", "AuditRecord", "Demo.Next.API.Model.AuditRecord", "b.cs", 1, "net8")
    _sym(con, f"{L}|P:Demo.Next.API.AuditRecord.Id", "property", "Id", "Id", "a.cs", 2, "net48", container=lrec, typ="int")
    _sym(con, f"{N}|P:Demo.Next.API.Model.AuditRecord.Id", "property", "Id", "Id", "b.cs", 2, "net8", container=nrec, typ="int")
    # member-less same-name pair: not a twin
    _sym(con, f"{L}|T:Program", "class", "Program", "Program", "p.cs", 1, "net48")
    _sym(con, f"{N}|T:Program", "class", "Program", "Program", "q.cs", 1, "net8")

    refs = [(page, support), (support, iface), (page, other)]
    for i, (src, tgt) in enumerate(refs):
        con.execute("INSERT INTO rrefs VALUES (?,?,?,?,?,1,'x','call',?,NULL,?,?,0)",
                    ("f.cs", 100 + i, 1, tgt, tgt.split("|")[1], src, src.split("|")[0], "net48"))
    con.execute("INSERT INTO redges VALUES (?,?,?,?)", (impl, "implements_member", iface, "net48"))
    con.execute(
        "INSERT INTO rendpoints(project,side,verb,route,prefix_complete,handler_key,handler_kind,style,path,line,col,"
        "body_type_key) VALUES (?,?,?,?,1,?,?,?,?,?,?,?)",
        (N, "net8", "POST", "/api/v{version:apiVersion}/records/search", handler, "method", "minimal",
         "Modern/Demo.Next/Demo.Next.API/Endpoints/AuditEndpoint.cs", 21, 9, nreq))
    call = ("INSERT INTO rhttpcalls(project,side,path,line,col,verb,url,enclosing_key,callee_key,is_generated,url_line,"
            "url_col,url_end_line,url_end_col,body_type_key) VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)")
    con.execute(call, (L, "net48", "Legacy/Modern/NextApiClient/Audit.cs", 100, 23, "POST",
                       "{}/api/v{}/records/search", impl, None, 100, 60, 100, 120, lreq))
    con.execute(call, (L, "net48", "Legacy/Modern/Support.cs", 22, 9, "GET",
                       "inboxes/{}/tickets/active.json", other, None, 22, 30, 22, 60, None))
    api_links.link(con)
    con.executescript(dbm.SCHEMA_ROSLYN_INDEXES)
    cfg = SimpleNamespace(abs_of=lambda rel: f"C:/ws/{rel}", limits=SimpleNamespace(max_results_hard=500))
    return SimpleNamespace(con=con, cfg=cfg, rel=lambda p: str(p).replace("\\", "/"),
                           resolve_path=lambda f: Path(f), keys=SimpleNamespace(page=page, impl=impl,
                           handler=handler, lreq=lreq, nreq=nreq, lrec=lrec, nrec=nrec))


def test_twins_use_the_api_contract_and_the_name(ctx):
    st = twins.compute(ctx.con)
    rows = {(r["legacy_key"], r["new_key"]): r for r in ctx.con.execute("SELECT * FROM rtwins")}
    k = ctx.keys
    # differently named, but the two bodies of one linked call
    r = rows[(k.lreq, k.nreq)]
    assert r["evidence"] == "api_contract"
    assert json.loads(r["routes"]) == ["POST /api/v{version:apiVersion}/records/search"]
    assert json.loads(r["only_new"]) == ["Skip"] and json.loads(r["type_differences"]) == ["Take: int vs long"]
    # same name, same shape
    assert rows[(k.lrec, k.nrec)]["evidence"] == "same_name" and rows[(k.lrec, k.nrec)]["identical"] == 1
    # Program/Program has no members: not reported
    assert not any("Program" in a for a, _ in rows)
    assert st["api_contract"] == 1 and st["pairs"] == 2


def test_inherited_members_and_wire_names_count(ctx):
    k = ctx.keys
    con = ctx.con
    # new-side DTO whose shape is inherited: Derived : Base { } with Base.Key
    base, derived = f"{N}|T:Model.Base", f"{N}|T:Model.AuditRecordEx"
    legacy = f"{L}|T:Demo.Next.API.AuditRecordEx"
    _sym(con, base, "class", "Base", "Model.Base", "c.cs", 1, "net8")
    _sym(con, derived, "class", "AuditRecordEx", "Model.AuditRecordEx", "c.cs", 5, "net8")
    _sym(con, f"{N}|P:Model.Base.Key", "property", "Key", "Key", "c.cs", 2, "net8", container=base, typ="string")
    con.execute("INSERT INTO redges VALUES (?,?,?,?)", (derived, "inherits", base, "net8"))
    _sym(con, legacy, "class", "AuditRecordEx", "Demo.Next.API.AuditRecordEx", "d.cs", 1, "net48")
    # renamed on the wire: C# name KeyValue, JSON name "key"
    _sym(con, f"{L}|P:Demo.Next.API.AuditRecordEx.KeyValue", "property", "KeyValue", "KeyValue", "d.cs", 2,
         "net48", container=legacy, typ="string", json_name="key")
    twins.compute(con)
    r = con.execute("SELECT * FROM rtwins WHERE legacy_key = ?", (legacy,)).fetchone()
    assert r["identical"] == 1 and r["common"] == 1


def test_nested_contract_types_are_twins_and_not_differences(ctx):
    con, k = ctx.con, ctx.keys
    # the request bodies carry a nested type with different names on each side
    lnest, nnest = f"{L}|T:Demo.Next.API.NextAPIClient_AuditData", f"{N}|T:Demo.Next.API.Model.AuditPayload"
    _sym(con, lnest, "class", "NextAPIClient_AuditData", "Demo.Next.API.NextAPIClient_AuditData", "n.cs", 1, "net48")
    _sym(con, nnest, "class", "AuditPayload", "Demo.Next.API.Model.AuditPayload", "m.cs", 1, "net8")
    _sym(con, f"{L}|P:X.Data", "property", "Data", "Data", "x.cs", 30, "net48", container=k.lreq, typ="NextAPIClient_AuditData")
    _sym(con, f"{N}|P:Y.Data", "property", "Data", "Data", "y.cs", 30, "net8", container=k.nreq, typ="AuditPayload")
    _sym(con, f"{L}|P:N.Who", "property", "Who", "Who", "n.cs", 2, "net48", container=lnest, typ="string")
    _sym(con, f"{N}|P:M.Who", "property", "Who", "Who", "m.cs", 2, "net8", container=nnest, typ="string")
    twins.compute(con)
    nested_pair = con.execute("SELECT * FROM rtwins WHERE legacy_key = ? AND new_key = ?", (lnest, nnest)).fetchone()
    assert nested_pair["evidence"] == "nested_contract" and nested_pair["identical"] == 1
    body = con.execute("SELECT * FROM rtwins WHERE legacy_key = ?", (k.lreq,)).fetchone()
    assert json.loads(body["nested"]) == ["Data: NextAPIClient_AuditData ~ AuditPayload"]
    assert json.loads(body["type_differences"]) == ["Take: int vs long"]


def test_builtin_names_never_pair_and_enums_compare_by_member_name(ctx):
    con, k = ctx.con, ctx.keys
    _sym(con, f"{L}|T:Weird.String", "class", "String", "Weird.String", "w.cs", 1, "net48")
    _sym(con, f"{N}|T:Model.SourceType", "enum", "SourceType", "Model.SourceType", "e.cs", 1, "net8")
    _sym(con, f"{L}|P:X.Src", "property", "Src", "Src", "x.cs", 40, "net48", container=k.lreq, typ="string")
    _sym(con, f"{N}|P:Y.Src", "property", "Src", "Src", "y.cs", 40, "net8", container=k.nreq, typ="SourceType")
    le, ne = f"{L}|T:Model.Status", f"{N}|T:Model.Status"
    _sym(con, le, "enum", "Status", "Model.Status", "s.cs", 1, "net48")
    _sym(con, ne, "enum", "Status", "Model.Status", "t.cs", 1, "net8")
    for key, side, c in ((f"{L}|F:Model.Status.Open", "net48", le), (f"{N}|F:Model.Status.Open", "net8", ne)):
        con.execute(
            "INSERT INTO rsymbols(key,doc_id,project,side,kind,name,container_key,fqn,signature,accessibility,"
            "modifiers,path,line,col,end_line,is_generated,start_line,type,json_name) "
            "VALUES (?,?,?,?,'field','Open',?,?,?,'public','static',?,1,1,1,0,1,'Status',NULL)",
            (key, key.split("|")[1], key.split("|")[0], side, c, "Model.Status.Open", "Open", "s.cs"))
    twins.compute(con)
    assert con.execute("SELECT COUNT(*) FROM rtwins WHERE legacy_key = ?", (f"{L}|T:Weird.String",)).fetchone()[0] == 0
    body = con.execute("SELECT * FROM rtwins WHERE legacy_key = ?", (k.lreq,)).fetchone()
    assert "Src: string vs SourceType" in json.loads(body["type_differences"])
    status = con.execute("SELECT * FROM rtwins WHERE legacy_key = ?", (le,)).fetchone()
    assert status["identical"] == 1 and status["common"] == 1


def test_outbound_walk_reaches_the_endpoint_through_the_interface(ctx):
    out = api.outbound(ctx, [ctx.keys.page], "Legacy/App/ui/audit.aspx", depth=6,
                       include_dispatch=True, limit=50, scope="test")
    assert out.status == "ok" and out.endpoints_reached == 1
    (hit,) = out.calls
    assert hit.match == "route_exact" and hit.route.endswith("/records/search")
    assert hit.handler.key == ctx.keys.handler
    assert hit.chain[0] == "ui_audit.Page_Load"
    assert hit.chain[-1] == "Demo.Next.API.AuditClass.Get [via dispatch]" and hit.via_dispatch
    # the third-party call is reached too, but unmatched
    assert [u.url for u in out.unmatched_calls] == ["inboxes/{}/tickets/active.json"]
    assert any("route matches" in c for c in out.caveats)


def test_outbound_walk_without_dispatch_stops_at_the_interface(ctx):
    out = api.outbound(ctx, [ctx.keys.page], "p", depth=6, include_dispatch=False, limit=50, scope="t")
    assert out.calls == []
    assert len(out.unmatched_calls) == 1


def test_outbound_walk_respects_depth(ctx):
    out = api.outbound(ctx, [ctx.keys.page], "p", depth=1, include_dispatch=True, limit=50, scope="t")
    assert out.calls == []  # Page_Load -> GetAuditLog only
