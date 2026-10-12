"""The reference-resolution ladder.

For each ``refs`` row, the rules are tried in order and the first hit wins, with
the rule that fired recorded in ``refs.resolution``:

  1 exact_fqn      Roslyn already answered for this file:line
  2 same_file      a symbol with this name is declared in the same file
  3 same_type      a member of the enclosing type or one of its ancestors
  4 same_namespace exactly one symbol with this name in the same namespace
  5 unique_name    exactly one symbol with this name on the same side
  6 ambiguous      two or more candidates repo-wide
  7 unresolved     no candidates

``ambiguous`` is a first-class answer, not a failure.  It is what lets the agent
say "12 confirmed callers, plus 3 sites where the name is ambiguous between X
and Y" instead of inventing certainty.  Rules 4-6 apply only where Roslyn cannot
answer -- JavaScript, markup, and C# outside a loaded project.  For C# inside a
loaded project Roslyn always wins.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

RESOLUTION_ORDER = (
    "exact_fqn",
    "same_file",
    "same_type",
    "same_namespace",
    "unique_name",
    "markup_binding",
    "ambiguous",
    "unresolved",
)


class SymbolLookup:
    """In-memory indexes over ``symbols`` for one resolution pass."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.by_name_side: dict[tuple[str, str], list[int]] = defaultdict(list)
        self.by_file: dict[tuple[int, str], list[int]] = defaultdict(list)
        self.by_container: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        self.by_fqn: dict[tuple[str, str], list[int]] = defaultdict(list)
        self.kind: dict[int, str] = {}
        self.container: dict[int, str | None] = {}
        self.namespace: dict[int, str | None] = {}
        self.name: dict[int, str] = {}
        self.side: dict[int, str] = {}
        self.fqn: dict[int, str | None] = {}

        rows = con.execute(
            "SELECT symbol_id, file_id, side, kind, name, container, fqn FROM symbols"
        )
        for sid, fid, side, kind, name, container, fqn in rows:
            self.by_name_side[(name, side)].append(sid)
            self.by_file[(fid, name)].append(sid)
            self.kind[sid] = kind
            self.container[sid] = container
            self.name[sid] = name
            self.side[sid] = side
            self.fqn[sid] = fqn
            if container:
                self.by_container[(container, name, side)].append(sid)
            if fqn:
                self.by_fqn[(fqn, side)].append(sid)
            self.namespace[sid] = _namespace_of(container, kind)

    # -- ancestry ---------------------------------------------------------

    def build_ancestors(self, con: sqlite3.Connection) -> dict[str, set[str]]:
        """type fqn -> set of base type names, transitively (bounded)."""
        direct: dict[str, set[str]] = defaultdict(set)
        for src_id, dst_name in con.execute(
            "SELECT src_id, dst_name FROM edges "
            "WHERE edge_type IN ('inherits','implements') AND src_kind='symbol' "
            "AND dst_name IS NOT NULL"
        ):
            direct[str(src_id)].add(dst_name)
        return direct


def _namespace_of(container: str | None, kind: str) -> str | None:
    """The namespace part of a container, i.e. everything before the type."""
    if not container:
        return None
    return container


def resolve_all(con: sqlite3.Connection, *, side: str | None = None) -> dict[str, int]:
    """Run the ladder over every unresolved ref.  Returns a histogram."""
    lookup = SymbolLookup(con)

    # Members of each type, by simple name, for rule 3.
    members: dict[tuple[str, str], list[int]] = lookup.by_container

    # Base type names per symbol_id, for rule 3's ancestor walk.
    bases: dict[int, list[str]] = defaultdict(list)
    for src_id, dst_name in con.execute(
        "SELECT src_id, dst_name FROM edges WHERE edge_type IN ('inherits','implements') "
        "AND src_kind='symbol' AND dst_name IS NOT NULL"
    ):
        bases[src_id].append(_simple(dst_name))

    # symbol_id -> its own fqn, so an enclosing member maps to its type.
    own_container = lookup.container

    # type fqn -> base type names, for the rule-3 ancestor walk.  Keyed by fqn
    # rather than by simple name so a base is only followed when the enclosing
    # type is identified unambiguously.
    bases_by_fqn: dict[tuple[str, str], list[str]] = defaultdict(list)
    for sid, names in bases.items():
        if lookup.kind.get(sid) not in ("class", "struct", "record", "interface"):
            continue
        fqn = lookup.fqn.get(sid)
        if fqn:
            bases_by_fqn[(fqn, lookup.side[sid])].extend(names)

    def ancestor_chain(type_fqn: str, rside: str, depth: int = 4) -> list[str]:
        """The enclosing type followed by its base types, resolved by fqn.

        A base name is followed only when exactly one type on this side carries
        it; an ambiguous base is dropped rather than guessed, because picking
        the wrong one would attribute a member to a type that does not declare it.
        """
        out: list[str] = [type_fqn]
        seen = {type_fqn}
        frontier = [type_fqn]
        while frontier and depth > 0:
            depth -= 1
            nxt: list[str] = []
            for f in frontier:
                for b in bases_by_fqn.get((f, rside), []):
                    simple = _simple(b)
                    cands = [
                        s
                        for s in lookup.by_name_side.get((simple, rside), [])
                        if lookup.kind.get(s)
                        in ("class", "struct", "record", "interface")
                    ]
                    if len(cands) != 1:
                        continue
                    c = lookup.container.get(cands[0])
                    bf = f"{c}.{simple}" if c else simple
                    if bf not in seen:
                        seen.add(bf)
                        out.append(bf)
                        nxt.append(bf)
            frontier = nxt
        return out

    where = "WHERE r.resolution = 'unresolved'"
    params: list = []
    if side:
        where += " AND r.side = ?"
        params.append(side)

    updates: list[tuple[int | None, str, int, int]] = []
    hist: dict[str, int] = defaultdict(int)

    q = con.execute(
        f"SELECT r.ref_id, r.file_id, r.side, r.name, r.enclosing_sym FROM refs r {where}",
        params,
    )
    for ref_id, file_id, rside, name, encl in q:
        # rule 2 -- same file
        cand = lookup.by_file.get((file_id, name))
        if cand:
            updates.append((cand[0], "same_file", len(cand), ref_id))
            hist["same_file"] += 1
            continue

        # rule 3 -- a member of the enclosing type, or of one of its ancestors
        hit = None
        if encl is not None:
            type_fqn = own_container.get(encl)
            if type_fqn:
                for c in ancestor_chain(type_fqn, rside):
                    m = members.get((c, name, rside))
                    if m:
                        hit = m
                        break
        if hit:
            updates.append((hit[0], "same_type", len(hit), ref_id))
            hist["same_type"] += 1
            continue

        # rules 4-7 -- namespace, then repo-wide on the same side
        cands = lookup.by_name_side.get((name, rside), [])
        if len(cands) == 1:
            updates.append((cands[0], "unique_name", 1, ref_id))
            hist["unique_name"] += 1
        elif len(cands) > 1:
            same_ns = _same_namespace(cands, encl, own_container, lookup)
            if len(same_ns) == 1:
                updates.append((same_ns[0], "same_namespace", 1, ref_id))
                hist["same_namespace"] += 1
            else:
                updates.append((None, "ambiguous", len(cands), ref_id))
                hist["ambiguous"] += 1
        else:
            hist["unresolved"] += 1

    con.executemany(
        "UPDATE refs SET resolved_sym=?, resolution=?, candidates=? WHERE ref_id=?",
        updates,
    )
    con.commit()
    return dict(hist)


def _same_namespace(cands, encl, own_container, lookup) -> list[int]:
    if encl is None:
        return []
    ns = own_container.get(encl)
    if not ns:
        return []
    root = ns.split(".")[0]
    out = [c for c in cands if (own_container.get(c) or "").split(".")[0] == root]
    return out


def _simple(name: str) -> str:
    n = name.strip()
    if "<" in n:
        n = n[: n.index("<")]
    if "." in n:
        n = n.rsplit(".", 1)[1]
    return n.strip()


def materialise_inheritance(con: sqlite3.Connection) -> dict[str, int]:
    """Turn base-list edges into ``inherits`` vs ``implements`` using the *kind*
    of the resolved destination.

    A base type whose declaration is an ``interface`` is an implementation; one
    whose declaration is a class is inheritance.  When the base type cannot be
    resolved to a declaration in this repository (it is a framework or
    third-party type) the edge stays ``inherits`` and is marked
    ``confidence='heuristic'`` -- the name is recorded, the relationship is not
    asserted.
    """
    kinds: dict[tuple[str, str], list[str]] = defaultdict(list)
    ids: dict[tuple[str, str], list[int]] = defaultdict(list)
    for sid, side, name, kind in con.execute(
        "SELECT symbol_id, side, name, kind FROM symbols "
        "WHERE kind IN ('class','interface','struct','record','enum','delegate')"
    ):
        kinds[(name, side)].append(kind)
        ids[(name, side)].append(sid)

    stats: dict[str, int] = defaultdict(int)
    updates: list[tuple[str, int | None, str, int]] = []
    for edge_id, dst_name, side in con.execute(
        "SELECT e.edge_id, e.dst_name, s.side FROM edges e "
        "JOIN symbols s ON s.symbol_id = e.src_id "
        "WHERE e.edge_type IN ('inherits','implements') AND e.src_kind='symbol' "
        "AND e.dst_name IS NOT NULL"
    ):
        key = (_simple(dst_name), side)
        ks = kinds.get(key)
        if not ks:
            updates.append(("inherits", None, "heuristic", edge_id))
            stats["unresolved_base"] += 1
            continue
        if len(set(ks)) > 1 or len(ids[key]) > 1:
            et = "implements" if ks[0] == "interface" else "inherits"
            updates.append((et, None, "heuristic", edge_id))
            stats["ambiguous_base"] += 1
            continue
        et = "implements" if ks[0] == "interface" else "inherits"
        updates.append((et, ids[key][0], "exact", edge_id))
        stats[et] += 1

    con.executemany(
        "UPDATE edges SET edge_type=?, dst_id=?, confidence=? WHERE edge_id=?", updates
    )
    con.commit()
    return dict(stats)
