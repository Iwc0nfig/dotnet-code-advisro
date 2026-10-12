"""Matching legacy HTTP call sites to new-API endpoints (index/api_links.py).

The shapes are the real ones: Legacy/Modern/NextApiClient/Audit.cs
calls Common.getResponse(client, HttpMethod.Post,
$"{client._callUrl}/api/v{client._version}/records/search", ...), and
Demo.Next.API/Endpoints/AuditEndpoint.cs maps
MapGroup("/api/v{version:apiVersion}/audit").MapPost("/searchByKey", ...).
"""

from __future__ import annotations

import sqlite3

import pytest

from dotnet_code_advisor import db as dbm
from dotnet_code_advisor.index import api_links as al

AUDIT_URL = "{}/api/v{}/records/search"
AUDIT_ROUTE = "/api/v{version:apiVersion}/records/search"


def test_segments_normalise_both_ends():
    assert al.url_segments(AUDIT_URL) == ["api", "v{}", "records", "search"]
    assert al.route_segments(AUDIT_ROUTE) == ["api", "v{}", "records", "search"]
    assert al.url_segments("https://api.example.com/api/v2/audit?x={}") == ["api", "v2", "audit"]
    assert al.route_segments("/orders/{id:int?}") == ["orders", "{}"]


def test_a_computed_query_suffix_is_not_part_of_the_path():
    # AI.cs: $"{client._callUrl}/api/v{client._version}/ml/chat/sessions{sb}", sb = "?leadEmail=..."
    assert al.url_segments("{}/api/v{}/ml/chat/sessions{}") == ["api", "v{}", "ml", "chat", "sessions"]
    assert al.match("GET", "{}/api/v{}/ml/chat/sessions{}", "GET",
                    "/api/v{version:apiVersion}/ml/chat/sessions") == "exact"
    # a hole that IS a whole segment stays a segment
    assert al.url_segments("{}/api/v{}/lookups/{}") == ["api", "v{}", "lookups", "{}"]


def test_the_real_audit_call_matches_exactly():
    assert al.match("POST", AUDIT_URL, "POST", AUDIT_ROUTE) == "exact"


def test_verb_must_agree():
    assert al.match("GET", AUDIT_URL, "POST", AUDIT_ROUTE) is None
    # A verb that is not a literal (HttpMethod passed in a variable) weakens the match.
    assert al.match("?", AUDIT_URL, "POST", AUDIT_ROUTE) == "partial"


def test_route_parameter_takes_a_literal_or_a_hole():
    assert al.match("GET", "{}/orders/42", "GET", "/orders/{id}") == "exact"
    assert al.match("GET", "{}/orders/{}", "GET", "/orders/{id}") == "exact"


def test_a_hole_on_a_literal_route_segment_is_only_partial():
    assert al.match("POST", "{}/api/v{}/records/{}", "POST", AUDIT_ROUTE) == "partial"


def test_different_literals_or_lengths_do_not_match():
    assert al.match("POST", "{}/api/v{}/audit/deleteByKey", "POST", AUDIT_ROUTE) is None
    assert al.match("POST", "{}/api/v{}/audit", "POST", AUDIT_ROUTE) is None


def test_an_all_hole_url_matches_nothing():
    assert al.match("GET", "{}/{}", "GET", "/{a}/{b}") is None


def _db():
    con = sqlite3.connect(":memory:")
    con.executescript(dbm.SCHEMA_ROSLYN)
    ep = "INSERT INTO rendpoints(project,side,verb,route,prefix_complete,handler_key,handler_kind,style,path,line,col) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    call = ("INSERT INTO rhttpcalls(project,side,path,line,col,verb,url,enclosing_key,callee_key,is_generated,"
            "url_line,url_col,url_end_line,url_end_col) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    con.executemany(ep, [
        ("api", "net8", "POST", AUDIT_ROUTE, 1, "api|M:Search", "method", "minimal", "a.cs", 21, 9),
        ("api", "net8", "POST", "/api/v{version:apiVersion}/audit/searchAll", 1, "api|M:SearchAll", "method", "minimal", "a.cs", 23, 9),
        ("api", "net8", "POST", "/api/v{version:apiVersion}/audit/{op}", 1, "api|M:Op", "method", "minimal", "a.cs", 25, 9),
        ("api", "net8", "GET", "/never/called", 1, "api|M:Never", "method", "minimal", "a.cs", 30, 9),
    ])
    con.executemany(call, [
        ("legacy", "net48", "Audit.cs", 100, 23, "POST", AUDIT_URL, "legacy|M:AuditClass.SearchByKey", "legacy|M:Common.getResponse", 0, 100, 60, 100, 120),
        ("legacy", "net48", "Audit.cs", 109, 23, "POST", "{}/api/v{}/audit/{}", "legacy|M:AuditClass.Dyn", None, 0, 109, 60, 109, 100),
        ("legacy", "net48", "Other.cs", 5, 1, "GET", "{}/unknown/thing", "legacy|M:X", None, 0, 5, 20, 5, 40),
    ])
    return con


def test_link_classifies_every_call_site():
    con = _db()
    st = al.link(con)
    links = con.execute("SELECT call_id, endpoint_id, match FROM rapilinks ORDER BY 1, 2").fetchall()
    # searchByKey matches the literal route and the {op} route; as in ASP.NET
    # routing, the literal wins.
    assert (1, 1, "route_exact") in links
    assert not any(c == 1 and e == 3 for c, e, _ in links)
    # a computed last segment is only a partial match for the literal routes,
    # but an exact one for the {op} parameter -- the only exact candidate.
    assert (2, 3, "route_exact") in links
    assert st.route_exact == 2 and st.route_ambiguous == 0 and st.unmatched == 1


def test_equally_specific_routes_stay_ambiguous():
    con = _db()
    con.execute("INSERT INTO rendpoints(project,side,verb,route,prefix_complete,handler_key,handler_kind,"
                "style,path,line,col) VALUES ('api2','net8','POST',?,1,'api2|M:Dup','method','minimal','b.cs',1,1)",
                (AUDIT_ROUTE,))
    st = al.link(con)
    assert st.route_ambiguous == 1


def test_link_is_deterministic():
    a, b = _db(), _db()
    al.link(a), al.link(b)
    q = "SELECT * FROM rapilinks ORDER BY call_id, endpoint_id"
    assert a.execute(q).fetchall() == b.execute(q).fetchall()
