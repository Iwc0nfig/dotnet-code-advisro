"""get_dependency_graph and find_dead_code.  """

from __future__ import annotations

from typing import Literal, Optional

from mcp.types import ToolAnnotations

from ..models import (
    DeadCodeOut,
    DependencyEdge,
    DependencyGraphOut,
    ProjectNode,
)
from . import common as c
from .common import Ctx
from .symbols import _symbol_from_roslyn

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(
        name="get_dependency_graph",
        annotations=RO,
        description=(
            "Project-level or type-level dependencies, with cycle detection. "
            "level='project' uses the compiler's own project graph. level='type' uses "
            "the structural graph's inherits/implements edges and is a name-based match. "
            "Use it to size a blast radius before proposing a change."
        ),
    )
    def get_dependency_graph(
        level: Literal["project", "type"] = "project",
        side: Literal["net48", "net8", "both"] = "both",
        root: Optional[str] = None,
        max: int = 300,
    ) -> DependencyGraphOut:
        limit = min(int(max), ctx.cfg.limits.max_results_hard)
        sides = list(ctx.cfg.paths.roots) if side == "both" else [side]

        if level == "project":
            projects: list[ProjectNode] = []
            edges: list[DependencyEdge] = []
            unavailable: list[str] = []
            for s in sides:
                res, why = c.try_roslyn(ctx, s, "project_graph", {})
                if why:
                    unavailable.append(f"{s}: {why}")
                    continue
                for p in res.get("projects", []):
                    projects.append(
                        ProjectNode(
                            name=p["name"], path=p.get("path"),
                            documents=p.get("documents", 0),
                            is_synthetic=bool(p.get("is_synthetic")),
                        )
                    )
                for e in res.get("edges", []):
                    edges.append(
                        DependencyEdge(
                            src=e["from"], dst=e["to"], edge_type="project_ref",
                            evidence="<ProjectReference>", confidence="exact",
                        )
                    )
            if not projects and unavailable:
                return DependencyGraphOut(
                    status="unavailable", layer="roslyn",
                    searched_scope="; ".join(ctx.roslyn_scope(s) for s in sides),
                    caveats=unavailable
                    + ["No results were produced; this is not 'no dependencies'."],
                )
            caveats = list(unavailable)
            if any(p.is_synthetic for p in projects):
                caveats.append(
                    "Synthetic Website Projects represent configured sites without a .csproj. "
                    "Their project references are taken from solution WebsiteProperties."

                )
            return DependencyGraphOut(
                status="ok" if projects else "no_results",
                layer="roslyn",
                searched_scope="; ".join(ctx.roslyn_scope(s) for s in sides),
                projects=projects,
                edges=edges[:limit],
                cycles=_cycles([(e.src, e.dst) for e in edges]),
                truncated=len(edges) > limit,
                total_available=len(edges),
                caveats=caveats,
            )

        # type level -- structural
        params: list = []
        where = "e.edge_type IN ('inherits','implements')"
        if side != "both":
            where += " AND s.side = ?"
            params.append(side)
        if root:
            where += " AND (s.name = ? OR e.dst_name = ?)"
            params += [root, root]
        rows = ctx.con.execute(
            "SELECT e.edge_type, e.dst_name, e.evidence, e.confidence, s.name AS src_name, "
            "s.side FROM edges e JOIN symbols s ON s.symbol_id = e.src_id "
            f"WHERE e.src_kind='symbol' AND {where} LIMIT ?",
            params + [limit],
        ).fetchall()
        edges = [
            DependencyEdge(
                src=r["src_name"], dst=r["dst_name"] or "?",
                edge_type=r["edge_type"], evidence=r["evidence"],
                confidence=r["confidence"],
            )
            for r in rows
        ]
        heuristic = sum(1 for e in edges if e.confidence == "heuristic")
        caveats = [
            "Type-level edges are name-based structural matches from the tree-sitter "
            "graph, not compiler answers."
        ]
        if heuristic:
            caveats.append(
                f"{heuristic} edge(s) have confidence 'heuristic': the base type name "
                "could not be resolved to a declaration in this repository (it is a "
                "framework or third-party type, or the name is ambiguous)."
            )
        return DependencyGraphOut(
            status="ok" if edges else "no_results",
            layer="graph",
            searched_scope=ctx.graph_scope(),
            edges=edges,
            cycles=_cycles([(e.src, e.dst) for e in edges]),
            truncated=len(rows) >= limit,
            caveats=caveats,
        )

    @mcp.tool(
        name="find_dead_code",
        annotations=RO,
        description=(
            "Private and internal members with zero references, within ONE project or "
            "ONE file. Solution-wide sweeps are refused by design: they take tens of "
            "minutes here and the WebForms reflection surface makes the results "
            "untrustworthy. The exclusions actually applied are returned so you can "
            "state the caveat rather than invent one."
        ),
    )
    def find_dead_code(
        scope: str,
        side: Literal["net48", "net8"] = "net48",
        max: int = 100,
    ) -> DeadCodeOut:
        res, why = c.try_roslyn(
            ctx, side, "dead_code",
            {"scope": scope, "max": min(int(max), ctx.cfg.limits.max_results_hard)},
        )
        if why:
            return DeadCodeOut(
                status="unavailable", layer="roslyn",
                searched_scope=ctx.roslyn_scope(side), scope=scope,
                caveats=[why, "No results were produced; this is not 'no dead code'."],
            )
        syms = [
            s for s in (_symbol_from_roslyn(ctx, u, side) for u in res.get("unreferenced", []))
            if s
        ]
        return DeadCodeOut(
            status="ok" if syms else "no_results",
            layer="roslyn",
            searched_scope=f"{ctx.roslyn_scope(side)}; scope='{scope}'",
            scope=scope,
            candidates_considered=res.get("candidates_considered", 0),
            unreferenced=syms,
            exclusions_applied=res.get("exclusions_applied", []),
            truncated=bool(res.get("truncated")),
            caveats=[
                "A member with no compiler-visible references can still be reached at "
                "runtime through reflection, an .aspx handler attribute, or a "
                "configuration file. Treat this as a candidate list, not proof.",
            ],
        )


def _cycles(edges: list[tuple[str, str]], limit: int = 20) -> list[list[str]]:
    """Cycles in the dependency graph, via iterative DFS with a colour map."""
    adj: dict[str, list[str]] = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, [])
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(adj, WHITE)
    out: list[list[str]] = []

    for start in list(adj):
        if colour[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        colour[start] = GREY
        path.append(start)
        while stack:
            node, i = stack[-1]
            if i < len(adj.get(node, [])):
                stack[-1] = (node, i + 1)
                nxt = adj[node][i]
                if colour.get(nxt, WHITE) == GREY:
                    if nxt in path:
                        cyc = path[path.index(nxt):] + [nxt]
                        if cyc not in out:
                            out.append(cyc)
                            if len(out) >= limit:
                                return out
                elif colour.get(nxt, WHITE) == WHITE:
                    colour[nxt] = GREY
                    path.append(nxt)
                    stack.append((nxt, 0))
            else:
                colour[node] = BLACK
                stack.pop()
                if path and path[-1] == node:
                    path.pop()
    return out
