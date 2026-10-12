"""Pair DTOs across the migration: a legacy (net48) type with its new-API (net8) counterpart.

Three kinds of evidence, all from the compiler index:

  api_contract  the legacy call site serializes type X as the request body of a
                call whose URL matches (route_exact) an endpoint whose handler
                takes type Y as its body -- X and Y are the two ends of one wire
                contract, whatever their names
  nested_contract  X <-> Y is a contract and member X.M is an A where Y.M is a B:
                A <-> B belongs to the same contract (followed to a fixed point)
  same_name     a type with the same simple name on the other side

Every pair gets a shape comparison of public instance properties/fields as
they serialize: inherited members included, compared by wire name (the
[JsonProperty]/[JsonPropertyName]/[DataMember] name when renamed, else the C#
name), case-insensitively as JSON binding is -- which members both have, which
only one has, and which disagree on type.  Types are compared without namespaces and
nullability, and collections by element (List<T>, T[], IEnumerable<T>, ... are
the same shape on the wire).  A pair is a CORRESPONDENCE with evidence, not a
proof of equivalence.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

TYPE_KINDS = ("class", "record", "struct", "enum")
# Never paired as nested contract types, whatever a project happens to declare
# (one legacy project has a class named String).
BUILTIN = frozenset("""
string object bool boolean byte sbyte short ushort int int16 int32 int64 uint long ulong
float single double decimal char datetime datetimeoffset timespan guid dynamic uri
""".split())
_COLLECTION = re.compile(
    r"^(?:List|IList|IEnumerable|ICollection|IReadOnlyList|IReadOnlyCollection|Collection|HashSet|ISet)<(.+)>$")


def norm_type(t: str | None) -> str:
    """'List<AuditRecord?>?' -> '[auditrecord]';  'Nullable<int>' / 'int?' -> 'int'."""
    if not t:
        return ""
    t = t.replace(" ", "").replace("?", "")
    m = re.fullmatch(r"Nullable<(.+)>", t)
    if m:
        t = m.group(1)
    if t.endswith("[]"):
        return "[" + norm_type(t[:-2]) + "]"
    m = _COLLECTION.match(t)
    if m:
        return "[" + norm_type(m.group(1)) + "]"
    return t.rsplit(".", 1)[-1].lower()


@dataclass
class Shape:
    common: list[str]
    only_legacy: list[str]
    only_new: list[str]
    type_differences: list[str]
    score: float
    identical: bool
    nested: list[str]


def _element(t: str) -> tuple[bool, str]:
    """Normalized type -> (is collection, element name)."""
    return (t.startswith("["), t.strip("[]"))


def compare(legacy: dict[str, tuple[str, str]], new: dict[str, tuple[str, str]],
            linked: frozenset[tuple[str, str]] = frozenset(), names_only: bool = False) -> Shape:
    """Members as {wire name lower: (display name, type)}.

    ``linked`` holds (legacy type, new type) simple names already known to be
    the same contract: a member typed with such a pair is compatible (listed in
    ``nested``), not a type difference.
    """
    common = sorted(set(legacy) & set(new))
    diffs, nested = [], []
    for k in common:
        lt, nt = norm_type(legacy[k][1]), norm_type(new[k][1])
        if lt == nt or names_only:  # enum members: only the names are the contract
            continue
        (lc, le), (nc, ne) = _element(lt), _element(nt)
        if lc == nc and (le, ne) in linked:
            nested.append(f"{legacy[k][0]}: {legacy[k][1]} ~ {new[k][1]}")
        else:
            diffs.append(f"{legacy[k][0]}: {legacy[k][1]} vs {new[k][1]}")
    only_l = sorted(legacy[k][0] for k in set(legacy) - set(new))
    only_n = sorted(new[k][0] for k in set(new) - set(legacy))
    union = len(set(legacy) | set(new))
    score = round(len(common) / union, 3) if union else 0.0
    return Shape(common=[legacy[k][0] for k in common], only_legacy=only_l, only_new=only_n,
                 type_differences=diffs, score=score, nested=nested,
                 identical=bool(union) and not only_l and not only_n and not diffs)


def _own_members(con: sqlite3.Connection, keys: list[str],
                 enums: set[str]) -> dict[str, dict[str, tuple[str, str]]]:
    """Declared public instance properties/fields (an enum's members: its static
    fields), keyed by wire name (JSON name if renamed)."""
    out: dict[str, dict[str, tuple[str, str]]] = {k: {} for k in keys}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        for container, name, typ, json_name, mods in con.execute(
            f"SELECT container_key, name, type, json_name, modifiers FROM rsymbols WHERE container_key IN "
            f"({','.join('?' * len(chunk))}) AND kind IN ('property','field') AND accessibility = 'public' "
            "ORDER BY container_key, line, name",
            chunk,
        ):
            if "static" in (mods or "") and container not in enums:
                continue
            wire = json_name or name
            out[container].setdefault(wire.lower(), (wire, typ or ""))
    return out


def _members(con: sqlite3.Connection, keys: set[str],
             enums: set[str] = frozenset()) -> dict[str, dict[str, tuple[str, str]]]:
    """Members as serialized: the type's own plus those inherited from source base classes.

    A DTO is often an empty subclass (AnonymizeDataRequest : AnonymizationOptions {})
    whose wire shape is entirely inherited; the derived type's own members win.
    """
    bases: dict[str, list[str]] = {}
    frontier = sorted(keys)
    chain_keys: set[str] = set(keys)
    while frontier:
        nxt = []
        for i in range(0, len(frontier), 500):
            chunk = frontier[i:i + 500]
            for src, dst in con.execute(
                f"SELECT src_key, dst_key FROM redges WHERE edge_type = 'inherits' AND src_key IN "
                f"({','.join('?' * len(chunk))}) ORDER BY src_key", chunk):
                bases.setdefault(src, []).append(dst)
                if dst not in chain_keys:
                    chain_keys.add(dst)
                    nxt.append(dst)
        frontier = sorted(nxt)
    own = _own_members(con, sorted(chain_keys), set(enums))

    def collect(k: str, seen: set[str]) -> dict[str, tuple[str, str]]:
        if k in seen:
            return {}
        seen.add(k)
        merged: dict[str, tuple[str, str]] = {}
        for b in bases.get(k, []):
            merged.update(collect(b, seen))
        merged.update(own.get(k, {}))
        return merged

    return {k: collect(k, set()) for k in keys}


def compute(con: sqlite3.Connection) -> dict:
    con.execute("DELETE FROM rtwins")
    marks = ",".join("?" * len(TYPE_KINDS))
    types: dict[str, dict[str, list[str]]] = {"net48": {}, "net8": {}}
    kind_of: dict[str, str] = {}
    for key, side, name, kind in con.execute(
            f"SELECT DISTINCT key, side, name, kind FROM rsymbols WHERE kind IN ({marks}) AND is_generated = 0",
            TYPE_KINDS):
        types[side].setdefault(name.lower(), []).append(key)
        kind_of[key] = kind

    evidence: dict[tuple[str, str], dict] = {}

    def add(legacy: str, new: str, ev: str, route: str | None = None):
        e = evidence.setdefault((legacy, new), {"evidence": set(), "routes": set()})
        e["evidence"].add(ev)
        if route:
            e["routes"].add(route)

    for c_body, e_body, c_side, e_side, verb, route in con.execute(
        "SELECT c.body_type_key, e.body_type_key, c.side, e.side, e.verb, e.route FROM rapilinks l "
        "JOIN rhttpcalls c ON c.call_id = l.call_id JOIN rendpoints e ON e.endpoint_id = l.endpoint_id "
        "WHERE l.match = 'route_exact' AND c.body_type_key IS NOT NULL AND e.body_type_key IS NOT NULL"
    ):
        if c_side == "net48" and e_side == "net8":
            add(c_body, e_body, "api_contract", f"{verb} {route}")
        elif c_side == "net8" and e_side == "net48":
            add(e_body, c_body, "api_contract", f"{verb} {route}")

    for lname, legacy_keys in types["net48"].items():
        for lk in legacy_keys:
            for nk in types["net8"].get(lname, []):
                add(lk, nk, "same_name")

    members: dict[str, dict[str, tuple[str, str]]] = {}

    enums = {k for k, kind in kind_of.items() if kind == "enum"}

    def ensure(keys: set[str]) -> None:
        missing = {k for k in keys if k not in members}
        if missing:
            members.update(_members(con, missing, enums))

    def project(key: str) -> str:
        return key.split("|", 1)[0]

    def pick(cands: list[str], near: str) -> list[str]:
        """The candidate declared in the parent type's own project, if the name repeats."""
        if len(cands) <= 1:
            return cands
        same = [c for c in cands if project(c) == project(near)]
        return same if len(same) == 1 else cands

    # nested_contract: when X <-> Y is a wire contract and X.Data is an A while
    # Y.Data is a B, then A <-> B is part of the same contract.  Followed until
    # nothing new appears; only when each name is one type on its side.
    contract = {"api_contract", "nested_contract"}
    queue = sorted(p for p, e in evidence.items() if e["evidence"] & contract)
    seen = set(queue)
    while queue:
        ensure({k for p in queue for k in p})
        found = []
        for lk, nk in queue:
            ml, mn = members.get(lk, {}), members.get(nk, {})
            via = sorted(evidence[(lk, nk)]["routes"])[:1]
            for name in sorted(set(ml) & set(mn)):
                (lc, le), (nc, ne) = _element(norm_type(ml[name][1])), _element(norm_type(mn[name][1]))
                if le == ne or lc != nc or le in BUILTIN or ne in BUILTIN:
                    continue
                lks = pick(types["net48"].get(le, []), lk)
                nks = pick(types["net8"].get(ne, []), nk)
                if len(lks) == 1 and len(nks) == 1:
                    pair = (lks[0], nks[0])
                    add(*pair, "nested_contract", (via[0] + " ." if via else ".") + ml[name][0])
                    if pair not in seen:
                        seen.add(pair)
                        found.append(pair)
        queue = sorted(found)

    ensure({k for pair in evidence for k in pair})

    def simple(key: str) -> str:
        return key.split("|", 1)[1][2:].rsplit(".", 1)[-1].lower()

    linked = frozenset((simple(lk), simple(nk)) for (lk, nk), e in evidence.items()
                       if e["evidence"] & contract)
    rows = []
    stats = {"pairs": 0, "api_contract": 0, "same_name_only": 0, "identical": 0}
    for (lk, nk), e in sorted(evidence.items()):
        ml, mn = members.get(lk, {}), members.get(nk, {})
        # A same-name pair of two member-less types (Program, Startup, ...) says nothing.
        if e["evidence"] == {"same_name"} and not ml and not mn and kind_of.get(lk) != "enum":
            continue
        sh = compare(ml, mn, linked, names_only=lk in enums and nk in enums)
        rows.append((lk, nk, ",".join(sorted(e["evidence"])), json.dumps(sorted(e["routes"])),
                     sh.score, int(sh.identical), len(sh.common), json.dumps(sh.only_legacy),
                     json.dumps(sh.only_new), json.dumps(sh.type_differences), json.dumps(sh.nested)))
        stats["pairs"] += 1
        kind = ("api_contract" if "api_contract" in e["evidence"]
                else "nested_contract" if "nested_contract" in e["evidence"] else "same_name_only")
        stats[kind] = stats.get(kind, 0) + 1
        stats["identical"] += int(sh.identical)
    con.executemany("INSERT INTO rtwins VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return stats
