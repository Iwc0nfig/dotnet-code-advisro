"""Link HTTP call sites to the endpoints they hit.

Both ends come from the compiler (roslyn-host/HttpLinks.cs): a call site's
HttpMethod and the literal parts of its URL, an endpoint's verb, full route
and handler method.  What is NOT compiler-exact is matching the URL to the
route, so every link carries how it matched:

  route_exact      every literal segment of the route is matched by the same
                   literal in the URL, and every hole in the URL ({}, computed
                   at run time) falls on a route parameter or on a segment
                   whose literal parts agree ("v{}" vs "v{version:apiVersion}")
  route_partial    the shapes agree, but some hole in the URL lands on a
                   literal route segment -- it could be that segment, or not
  route_ambiguous  more than one endpoint matches exactly

Unmatched call sites stay unlinked and are counted, never guessed.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

HOLE = "{}"
_ROUTE_PARAM = re.compile(r"\{[^{}]*\}")


def route_segments(route: str) -> list[str]:
    """'/api/v{version:apiVersion}/audit/{id:int?}' -> ['sys', 'v{}', 'audit', '{}']"""
    # No query-string handling: in a route template '?' marks an optional
    # parameter ({id:int?}), never the start of a query.
    r = route.strip().lstrip("~")
    return [_ROUTE_PARAM.sub(HOLE, s).lower() for s in r.split("/") if s]


def url_segments(url: str) -> list[str]:
    """'{}/api/v{}/records/search?x={}' -> ['sys', 'v{}', 'audit', 'searchbykey'].

    The scheme and host, and any leading computed segments (the base URL held
    in a field), are dropped: routes are relative to the API's root.
    """
    u = url.strip()
    u = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", u, flags=re.IGNORECASE)
    u = u.split("?", 1)[0].split("#", 1)[0]
    segs = [s.lower() for s in u.split("/") if s]
    while segs and segs[0] == HOLE:
        segs.pop(0)
    # A computed suffix glued to the last segment -- ".../sessions{sb}" where
    # sb holds "?leadEmail=..." -- is a query string, not part of the path.
    if segs and segs[-1].endswith(HOLE) and segs[-1].rstrip("{}"):
        last = segs[-1]
        while last.endswith(HOLE):
            last = last[: -len(HOLE)]
        segs[-1] = last
    return segs


def _seg_regex(seg: str) -> re.Pattern:
    return re.compile("^" + ".+".join(re.escape(p) for p in seg.split(HOLE)) + "$")


def segment_match(call: str, route: str) -> str | None:
    """'exact', 'partial' or None for one URL segment against one route segment."""
    if call == route:
        return "exact"
    if route == HOLE:                                   # route parameter takes anything
        return "exact"
    if HOLE in route and HOLE not in call:              # "v1" against "v{}"
        return "exact" if _seg_regex(route).match(call) else None
    if HOLE in call and HOLE not in route:              # computed part against a literal
        return "partial" if _seg_regex(call).match(route) else None
    if HOLE in call and HOLE in route:                  # both partly computed: compare the literal skeletons
        return "exact" if call.replace(HOLE, "") == route.replace(HOLE, "") else "partial"
    return None


def verbs_compatible(call_verb: str, ep_verb: str) -> str | None:
    if ep_verb in ("*", "") or call_verb == "?":
        return "partial"
    return "exact" if call_verb.upper() in {v.strip().upper() for v in ep_verb.split(",")} else None


def match(call_verb: str, call_url: str, ep_verb: str, ep_route: str) -> str | None:
    """'exact' / 'partial' / None for one call against one endpoint."""
    v = verbs_compatible(call_verb, ep_verb)
    if v is None:
        return None
    cs, rs = url_segments(call_url), route_segments(ep_route)
    if not cs or len(cs) != len(rs):
        return None
    kinds = [segment_match(c, r) for c, r in zip(cs, rs)]
    if any(k is None for k in kinds):
        return None
    # A URL that is nothing but holes against a route says nothing.
    if all(c == HOLE for c in cs):
        return None
    return "exact" if v == "exact" and all(k == "exact" for k in kinds) else "partial"


@dataclass
class LinkStats:
    calls: int = 0
    endpoints: int = 0
    route_exact: int = 0
    route_ambiguous: int = 0
    route_partial: int = 0
    unmatched: int = 0


def link(con: sqlite3.Connection) -> LinkStats:
    """Fill rapilinks from rhttpcalls x rendpoints.  Deterministic: ordered by id."""
    con.execute("DELETE FROM rapilinks")
    endpoints = con.execute(
        "SELECT endpoint_id, verb, route FROM rendpoints ORDER BY endpoint_id").fetchall()
    # Bucket endpoints by segment count; only equal lengths can match.
    by_len: dict[int, list] = {}
    specificity: dict[int, int] = {}
    for e in endpoints:
        segs = route_segments(e[2])
        by_len.setdefault(len(segs), []).append(e)
        specificity[e[0]] = sum(1 for s in segs if s != HOLE)

    st = LinkStats(endpoints=len(endpoints))
    rows: list[tuple] = []
    for call_id, verb, url in con.execute(
            "SELECT call_id, verb, url FROM rhttpcalls ORDER BY call_id").fetchall():
        st.calls += 1
        n = len(url_segments(url))
        exact, partial = [], []
        for e in by_len.get(n, []):
            m = match(verb, url, e[1], e[2])
            if m == "exact":
                exact.append(e[0])
            elif m == "partial":
                partial.append(e[0])
        if len(exact) > 1:
            # ASP.NET route precedence: a literal segment beats a parameter, so
            # /records/search wins over /audit/{op}.  Keep the most specific
            # route when it is unique; a tie stays ambiguous.
            spec = {eid: specificity[eid] for eid in exact}
            top = max(spec.values())
            winners = [eid for eid, s in spec.items() if s == top]
            if len(winners) == 1:
                exact = winners
        if len(exact) == 1:
            rows.append((call_id, exact[0], "route_exact"))
            st.route_exact += 1
        elif exact:
            rows.extend((call_id, eid, "route_ambiguous") for eid in exact)
            st.route_ambiguous += 1
        elif partial:
            rows.extend((call_id, eid, "route_partial") for eid in partial)
            st.route_partial += 1
        else:
            st.unmatched += 1
    con.executemany("INSERT INTO rapilinks(call_id, endpoint_id, match) VALUES (?,?,?)", rows)
    return st
