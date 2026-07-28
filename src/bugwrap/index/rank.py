"""PageRank over the call graph — Aider's repo-map insight, applied to audits.

Centrality is the wrong ranking for diff review (blast radius beats popularity),
but for whole-file audits it is exactly right: with a fixed token budget, review
the load-bearing symbols first. Plain power iteration, no dependencies.
"""

from __future__ import annotations

from .callgraph import CallGraph

DAMPING = 0.85
ITERATIONS = 25


def pagerank(graph: CallGraph) -> dict[str, float]:
    """symbol fqname -> centrality score, from resolved call edges."""
    # caller fqnames are function-qualified ("mod.func"); map both ends to symbols
    edges: list[tuple[str, str]] = []
    nodes: set[str] = set()
    for target, sites in graph.callers_by_target.items():
        nodes.add(target)
        for site in sites:
            caller = site.caller.removesuffix(".<module>")
            if caller and caller != target:
                edges.append((caller, target))
                nodes.add(caller)
    if not nodes:
        return {}

    out_degree: dict[str, int] = {}
    incoming: dict[str, list[str]] = {}
    for src, dst in edges:
        out_degree[src] = out_degree.get(src, 0) + 1
        incoming.setdefault(dst, []).append(src)

    n = len(nodes)
    rank = dict.fromkeys(nodes, 1.0 / n)
    base = (1.0 - DAMPING) / n
    for _ in range(ITERATIONS):
        # mass from dangling nodes is redistributed uniformly
        dangling = sum(rank[node] for node in nodes if node not in out_degree)
        spread = DAMPING * dangling / n
        rank = {
            node: base
            + spread
            + DAMPING * sum(rank[src] / out_degree[src] for src in incoming.get(node, []))
            for node in nodes
        }
    return rank
