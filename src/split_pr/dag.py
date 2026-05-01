"""Topic dependency DAG.

Manages the dependency graph between topics identified during PR discovery.
Supports cycle detection, topological sorting, linearization, and identifying
independent topic groups.

Uses networkx for graph algorithms. No git dependency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import networkx as nx


class CyclicDependencyError(Exception):
    """Raised when adding a dependency would create a cycle."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        path = " -> ".join(cycle)
        super().__init__(f"Cyclic dependency detected: {path}")


@dataclass
class Topic:
    """A semantic unit of work within a PR."""

    id: str
    name: str
    description: str = ""
    estimated_size: int = 0
    hunk_ids: list[str] = field(default_factory=list)
    is_shared: bool = False  # True if this is shared infrastructure

    @property
    def hunk_count(self) -> int:
        return len(self.hunk_ids)


class TopicDAG:
    """Directed acyclic graph of topic dependencies.

    Edges point from dependency to dependent: A -> B means "A must come
    before B" (B depends on A).
    """

    def __init__(self) -> None:
        self._graph: nx.DiGraph = nx.DiGraph()
        self._topics: dict[str, Topic] = {}

    @property
    def topics(self) -> dict[str, Topic]:
        return dict(self._topics)

    @property
    def topic_count(self) -> int:
        return len(self._topics)

    def add_topic(self, topic: Topic) -> None:
        """Add a topic to the DAG."""
        if topic.id in self._topics:
            raise ValueError(f"Topic '{topic.id}' already exists")
        self._topics[topic.id] = topic
        self._graph.add_node(topic.id)

    def remove_topic(self, topic_id: str) -> Topic:
        """Remove a topic and all its edges. Returns the removed topic."""
        if topic_id not in self._topics:
            raise KeyError(f"Topic '{topic_id}' not found")
        topic = self._topics.pop(topic_id)
        self._graph.remove_node(topic_id)
        return topic

    def add_dependency(self, dependency_id: str, dependent_id: str) -> None:
        """Add an edge: dependent_id depends on dependency_id.

        Raises CyclicDependencyError if this would create a cycle.
        """
        for tid in (dependency_id, dependent_id):
            if tid not in self._topics:
                raise KeyError(f"Topic '{tid}' not found")

        if dependency_id == dependent_id:
            raise CyclicDependencyError([dependency_id, dependent_id])

        # Temporarily add the edge to check for cycles
        self._graph.add_edge(dependency_id, dependent_id)
        try:
            cycle = nx.find_cycle(self._graph, orientation="original")
            # Remove the edge we just added before raising
            self._graph.remove_edge(dependency_id, dependent_id)
            cycle_nodes = [u for u, _v, _d in cycle]
            cycle_nodes.append(cycle_nodes[0])
            raise CyclicDependencyError(cycle_nodes)
        except nx.NetworkXNoCycle:
            pass  # No cycle, edge stays

    def remove_dependency(self, dependency_id: str, dependent_id: str) -> None:
        """Remove a dependency edge."""
        if not self._graph.has_edge(dependency_id, dependent_id):
            raise KeyError(
                f"No dependency from '{dependency_id}' to '{dependent_id}'"
            )
        self._graph.remove_edge(dependency_id, dependent_id)

    def get_dependencies(self, topic_id: str) -> list[str]:
        """Get direct dependencies of a topic (what it depends on)."""
        if topic_id not in self._topics:
            raise KeyError(f"Topic '{topic_id}' not found")
        return list(self._graph.predecessors(topic_id))

    def get_dependents(self, topic_id: str) -> list[str]:
        """Get direct dependents of a topic (what depends on it)."""
        if topic_id not in self._topics:
            raise KeyError(f"Topic '{topic_id}' not found")
        return list(self._graph.successors(topic_id))

    def get_all_dependencies(self, topic_id: str) -> set[str]:
        """Get all transitive dependencies (ancestors) of a topic."""
        if topic_id not in self._topics:
            raise KeyError(f"Topic '{topic_id}' not found")
        return nx.ancestors(self._graph, topic_id)

    def reduced_edges(self) -> list[tuple[str, str]]:
        """Edges with transitively-redundant ones removed.

        For rendering: drops any edge u->v where v is already reachable
        from u via another path. Halves edge count on densely-connected
        DAGs and is required for diagrams that need to fit under
        GitHub's ~50KB Mermaid block limit.
        """
        if self._graph.number_of_nodes() == 0:
            return []
        reduced = nx.transitive_reduction(self._graph)
        return list(reduced.edges())

    def topological_sort(self) -> list[str]:
        """Return topics in dependency order (dependencies first).

        Topics with no ordering constraint between them may appear in
        any relative order.
        """
        return list(nx.topological_sort(self._graph))

    def linearize(self) -> list[str]:
        """Return a deterministic linear ordering of topics.

        Like topological_sort but with a stable tie-breaking rule:
        when multiple topics could come next, pick the one with the
        smallest id (alphabetical). This makes the output reproducible.
        """
        return list(nx.lexicographical_topological_sort(self._graph))

    def independent_groups(self) -> list[set[str]]:
        """Find groups of topics that are completely independent.

        Returns weakly connected components — topics within the same
        group have some dependency path between them; topics in different
        groups are fully independent and could be reviewed in parallel.
        """
        return [
            set(component)
            for component in nx.weakly_connected_components(self._graph)
        ]

    def roots(self) -> list[str]:
        """Topics with no dependencies (can start immediately)."""
        return [n for n in self._graph.nodes() if self._graph.in_degree(n) == 0]

    def leaves(self) -> list[str]:
        """Topics that nothing else depends on."""
        return [n for n in self._graph.nodes() if self._graph.out_degree(n) == 0]

    def merge_topics(self, topic_ids: list[str], merged_id: str, merged_name: str) -> Topic:
        """Merge multiple topics into one, preserving external edges.

        All hunks from the merged topics are combined. Dependencies between
        the merged topics are dropped. External dependencies are transferred
        to the new merged topic.
        """
        if len(topic_ids) < 2:
            raise ValueError("Need at least 2 topics to merge")

        topics_to_merge = []
        for tid in topic_ids:
            if tid not in self._topics:
                raise KeyError(f"Topic '{tid}' not found")
            topics_to_merge.append(self._topics[tid])

        merge_set = set(topic_ids)

        # Collect external edges
        external_deps: set[str] = set()
        external_dependents: set[str] = set()

        for tid in topic_ids:
            for pred in self._graph.predecessors(tid):
                if pred not in merge_set:
                    external_deps.add(pred)
            for succ in self._graph.successors(tid):
                if succ not in merge_set:
                    external_dependents.add(succ)

        # Combine hunks and compute size
        all_hunks: list[str] = []
        total_size = 0
        for t in topics_to_merge:
            all_hunks.extend(t.hunk_ids)
            total_size += t.estimated_size

        merged_topic = Topic(
            id=merged_id,
            name=merged_name,
            description=f"Merged from: {', '.join(t.name for t in topics_to_merge)}",
            estimated_size=total_size,
            hunk_ids=all_hunks,
            is_shared=any(t.is_shared for t in topics_to_merge),
        )

        # Remove old topics
        for tid in topic_ids:
            self.remove_topic(tid)

        # Add merged topic with edges
        self.add_topic(merged_topic)
        for dep in external_deps:
            self.add_dependency(dep, merged_id)
        for dependent in external_dependents:
            self.add_dependency(merged_id, dependent)

        return merged_topic

    def split_topic(self, topic_id: str, new_topics: list[Topic],
                    internal_deps: list[tuple[str, str]] | None = None) -> None:
        """Split one topic into multiple, transferring external edges.

        Args:
            topic_id: The topic to split.
            new_topics: The replacement topics (must have disjoint hunk_ids).
            internal_deps: Dependencies between the new topics, as
                (dependency_id, dependent_id) tuples.
        """
        if topic_id not in self._topics:
            raise KeyError(f"Topic '{topic_id}' not found")
        if len(new_topics) < 2:
            raise ValueError("Need at least 2 topics for a split")

        new_ids = {t.id for t in new_topics}

        # Capture external edges before removal
        external_deps = [
            p for p in self._graph.predecessors(topic_id) if p not in new_ids
        ]
        external_dependents = [
            s for s in self._graph.successors(topic_id) if s not in new_ids
        ]

        self.remove_topic(topic_id)

        # Add new topics
        for t in new_topics:
            self.add_topic(t)

        # All new sub-topics inherit the original's external dependencies
        for dep in external_deps:
            for t in new_topics:
                self.add_dependency(dep, t.id)

        # All external dependents now depend on all new sub-topics
        # (conservative — the agent can prune unnecessary edges later)
        for dependent in external_dependents:
            for t in new_topics:
                self.add_dependency(t.id, dependent)

        # Add internal dependencies
        if internal_deps:
            for dep_id, dependent_id in internal_deps:
                self.add_dependency(dep_id, dependent_id)

    def to_dict(self) -> dict:
        """Serialize the DAG to a JSON-compatible dict."""
        return {
            "topics": {
                tid: {
                    "id": t.id,
                    "name": t.name,
                    "description": t.description,
                    "estimated_size": t.estimated_size,
                    "hunk_ids": t.hunk_ids,
                    "is_shared": t.is_shared,
                }
                for tid, t in self._topics.items()
            },
            "edges": [
                {"from": u, "to": v}
                for u, v in self._graph.edges()
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> TopicDAG:
        """Deserialize a DAG from a dict."""
        dag = cls()
        for tid, tdata in data["topics"].items():
            dag.add_topic(Topic(
                id=tdata["id"],
                name=tdata["name"],
                description=tdata.get("description", ""),
                estimated_size=tdata.get("estimated_size", 0),
                hunk_ids=tdata.get("hunk_ids", []),
                is_shared=tdata.get("is_shared", False),
            ))
        for edge in data["edges"]:
            dag.add_dependency(edge["from"], edge["to"])
        return dag

    def summary(self) -> str:
        """Human-readable summary of the DAG."""
        lines = [f"TopicDAG: {self.topic_count} topics, {self._graph.number_of_edges()} dependencies"]
        groups = self.independent_groups()
        if len(groups) > 1:
            lines.append(f"  {len(groups)} independent groups (can be reviewed in parallel)")

        for tid in self.linearize():
            topic = self._topics[tid]
            deps = self.get_dependencies(tid)
            dep_str = f" (depends on: {', '.join(deps)})" if deps else " (root)"
            lines.append(f"  [{topic.id}] {topic.name} ~{topic.estimated_size} lines{dep_str}")

        return "\n".join(lines)
