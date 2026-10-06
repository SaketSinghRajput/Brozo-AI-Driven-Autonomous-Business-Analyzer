"""Semantic graph + JoinPathResolver (TASK-SEM-008; design 16).

The graph is the JSON ``SemanticModel.graph`` (nodes + typed edges), not a graph database; it holds
tens of nodes. ``JoinPathResolver`` runs a BFS over entity-to-entity edges (undirected) and
returns the unique shortest path; two or more shortest paths raise
``SchemaError(AMBIGUOUS_JOIN_PATH)``, none raises ``SchemaError(NO_JOIN_PATH)``.
"""

from collections import deque
from typing import Literal

from app.core.errors import SchemaError
from app.data.semantic.domain import GraphEdge, GraphNode, SemanticGraph

NodeKind = Literal["entity", "measure", "dimension", "metric"]
EdgeKind = Literal[
    "places", "contains", "belongs_to", "references", "measured_on", "attribute_of", "derived_from"
]
ENTITY_EDGES = frozenset({"places", "contains", "belongs_to", "references"})


def node_id(kind: NodeKind, name: str) -> str:
    return f"{kind}:{name}"


class GraphBuilder:
    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[tuple[str, str, str], GraphEdge] = {}

    def node(self, kind: NodeKind, name: str) -> str:
        nid = node_id(kind, name)
        if nid not in self._nodes:
            self._nodes[nid] = GraphNode(id=nid, type=kind, name=name)
        return nid

    def edge(self, src: str, dst: str, kind: EdgeKind, confidence: float, evidence: str) -> None:
        if src == dst or src not in self._nodes or dst not in self._nodes:
            return
        key = (src, dst, kind)
        if key not in self._edges:
            self._edges[key] = GraphEdge(
                from_=src, to=dst, type=kind, confidence=round(confidence, 4), evidence=evidence
            )

    def build(self) -> SemanticGraph:
        return SemanticGraph(nodes=list(self._nodes.values()), edges=list(self._edges.values()))


class JoinPathResolver:
    def __init__(self, graph: SemanticGraph) -> None:
        self._entities = {n.id for n in graph.nodes if n.type == "entity"}
        self._adjacent: dict[str, list[tuple[str, GraphEdge]]] = {e: [] for e in self._entities}
        for edge in graph.edges:
            if edge.type in ENTITY_EDGES and {edge.from_, edge.to} <= self._entities:
                self._adjacent[edge.from_].append((edge.to, edge))
                self._adjacent[edge.to].append((edge.from_, edge))
        for links in self._adjacent.values():
            links.sort(key=lambda link: (link[0], link[1].type))

    def path(self, source: str, target: str) -> list[GraphEdge]:
        """Edges of the unique shortest join path between two entity names."""
        start, goal = node_id("entity", source), node_id("entity", target)
        for nid, name in ((start, source), (goal, target)):
            if nid not in self._entities:
                raise SchemaError(f"unknown entity {name!r}", details={"reason": "UNKNOWN_ENTITY"})
        if start == goal:
            return []
        dist = {start: 0}
        ways = {start: 1}
        parent: dict[str, tuple[str, GraphEdge]] = {}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for nxt, edge in self._adjacent[current]:
                if nxt not in dist:
                    dist[nxt] = dist[current] + 1
                    ways[nxt] = ways[current]
                    parent[nxt] = (current, edge)
                    queue.append(nxt)
                elif dist[nxt] == dist[current] + 1:
                    ways[nxt] += ways[current]
        if goal not in dist:
            raise SchemaError(
                f"no join path between {source!r} and {target!r}",
                details={"reason": "NO_JOIN_PATH"},
            )
        if ways[goal] > 1:
            raise SchemaError(
                f"{ways[goal]} equally short join paths between {source!r} and {target!r}",
                details={"reason": "AMBIGUOUS_JOIN_PATH"},
            )
        edges: list[GraphEdge] = []
        node = goal
        while node != start:
            node, edge = parent[node]
            edges.append(edge)
        return list(reversed(edges))
