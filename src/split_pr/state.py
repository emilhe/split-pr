"""Split plan state management.

Ties together the diff parser and DAG to produce a concrete split plan:
which hunks go to which branch, in what order, with what commit messages.

This is the central data structure that the discovery agent writes to
and the splitter agent reads from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from split_pr.dag import TopicDAG, Topic
from split_pr.diff_parser import Hunk, ParsedDiff, weighted_size


class ValidationStatus(Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class BranchPlan:
    """Plan for a single branch/PR in the split."""

    topic_id: str
    branch_name: str
    base_branch: str  # what this branch is based on (main, or another split branch)
    hunk_ids: list[str] = field(default_factory=list)
    commit_message: str = ""
    pr_title: str = ""
    pr_body: str = ""
    validation_status: ValidationStatus = ValidationStatus.PENDING
    validation_errors: list[str] = field(default_factory=list)
    estimated_size: int = 0

    @property
    def is_valid(self) -> bool:
        return self.validation_status == ValidationStatus.PASSED


@dataclass
class SplitPlan:
    """Complete plan for splitting a PR into multiple branches/PRs.

    This is the output of the planning phase and the input to the
    execution phase.

    ``absorbed_into`` records discovery topics that ended up with no
    raw hunks after virtual-to-raw resolution, and the topic whose
    branch actually carries their content. Used by base-branch
    resolution (so `cube-routes -> legacy-adapter-summary` falls back
    to the branch that absorbed summary) and by the DAG renderer (so
    orphan nodes still link to the landing PR).
    """

    original_base: str
    branches: list[BranchPlan] = field(default_factory=list)
    unassigned_hunk_ids: list[str] = field(default_factory=list)
    absorbed_into: dict[str, str] = field(default_factory=dict)
    iteration: int = 0
    max_iterations: int = 5

    @property
    def branch_count(self) -> int:
        return len(self.branches)

    @property
    def total_size(self) -> int:
        return sum(b.estimated_size for b in self.branches)

    @property
    def all_valid(self) -> bool:
        return all(b.is_valid for b in self.branches)

    @property
    def has_unassigned(self) -> bool:
        return len(self.unassigned_hunk_ids) > 0

    def get_branch(self, topic_id: str) -> BranchPlan | None:
        for b in self.branches:
            if b.topic_id == topic_id:
                return b
        return None


class SplitPlanner:
    """Builds a SplitPlan from a ParsedDiff and TopicDAG.

    The planner takes:
    - A parsed diff (all the hunks)
    - A topic DAG (how topics relate to each other)
    - Hunk-to-topic assignments (which hunks belong to which topic)

    And produces a SplitPlan that the agent can execute with git operations.
    """

    def __init__(
        self,
        parsed_diff: ParsedDiff,
        dag: TopicDAG,
        base_branch: str = "main",
        size_threshold: int = 400,
        branch_prefix: str = "split",
        absorbed_into: dict[str, str] | None = None,
        delete_weight: float = 0.0,
    ) -> None:
        self.parsed_diff = parsed_diff
        self.dag = dag
        self.base_branch = base_branch
        self.size_threshold = size_threshold
        self.branch_prefix = branch_prefix
        self._hunk_index: dict[str, Hunk] = {
            h.id: h for h in parsed_diff.all_hunks
        }
        self._assignments: dict[str, str] = {}  # hunk_id -> topic_id
        self._absorbed_into: dict[str, str] = dict(absorbed_into or {})
        self.delete_weight = delete_weight

    def _size(self, hunk: Hunk) -> int:
        return weighted_size(hunk.added_lines, hunk.removed_lines, self.delete_weight)

    def assign_hunk(self, hunk_id: str, topic_id: str) -> None:
        """Assign a hunk to a topic."""
        if hunk_id not in self._hunk_index:
            raise KeyError(f"Hunk '{hunk_id}' not found")
        if topic_id not in self.dag.topics:
            raise KeyError(f"Topic '{topic_id}' not found")
        self._assignments[hunk_id] = topic_id

    def assign_hunks(self, assignments: dict[str, str]) -> None:
        """Bulk assign hunks to topics."""
        for hunk_id, topic_id in assignments.items():
            self.assign_hunk(hunk_id, topic_id)

    def get_unassigned_hunks(self) -> list[Hunk]:
        """Hunks not yet assigned to any topic."""
        assigned = set(self._assignments.keys())
        return [h for h in self.parsed_diff.all_hunks if h.id not in assigned]

    def get_topic_hunks(self, topic_id: str) -> list[Hunk]:
        """Get all hunks assigned to a topic."""
        return [
            self._hunk_index[hid]
            for hid, tid in self._assignments.items()
            if tid == topic_id
        ]

    def get_topic_size(self, topic_id: str) -> int:
        """Weighted review size for a topic.

        Applies the planner's ``delete_weight`` (default 0, so additions
        only). This is what drives oversized-topic detection.
        """
        return sum(self._size(h) for h in self.get_topic_hunks(topic_id))

    def get_topic_removed_lines(self, topic_id: str) -> int:
        """Raw removed-line count for a topic (informational only)."""
        return sum(h.removed_lines for h in self.get_topic_hunks(topic_id))

    def get_oversized_topics(self) -> list[str]:
        """Topics that exceed the size threshold."""
        return [
            tid for tid in self.dag.topics
            if self.get_topic_size(tid) > self.size_threshold
        ]

    def resolve_absorption(self, topic_id: str) -> str:
        """Follow absorption edges to the topic that actually has a branch.

        If ``topic_id`` was absorbed into another topic during virtual-to-raw
        hunk resolution (because two topics claimed virtual hunks from the
        same underlying raw hunk), walk the absorption chain. Returns
        ``topic_id`` unchanged if it wasn't absorbed.
        """
        seen = {topic_id}
        current = topic_id
        while current in self._absorbed_into:
            current = self._absorbed_into[current]
            if current in seen:
                # Guard against self-referential absorption; shouldn't
                # happen but silent infinite loops would be worse.
                break
            seen.add(current)
        return current

    def _resolve_base_branch(self, topic_id: str) -> str:
        """Determine what branch a topic's PR should target.

        For DAG-based splitting:
        - If the topic has no dependencies, target the original base branch.
        - If it has one dependency, target that dependency's branch.
        - If it has multiple dependencies, target the last one in
          topological order (all others will have been merged by then
          in a linear merge strategy).

        Absorbed dependencies (no branch of their own) are followed to
        the topic that actually carries their hunks.
        """
        deps = self.dag.get_dependencies(topic_id)
        if not deps:
            return self.base_branch

        # Resolve any absorbed deps to the topic that actually has a branch.
        resolved_deps = [self.resolve_absorption(d) for d in deps]
        # Drop the current topic if it ends up depending on itself post-absorption,
        # and deduplicate while preserving order.
        seen: set[str] = set()
        unique_deps: list[str] = []
        for d in resolved_deps:
            if d == topic_id or d in seen:
                continue
            seen.add(d)
            unique_deps.append(d)

        if not unique_deps:
            return self.base_branch

        if len(unique_deps) == 1:
            return self._branch_name(unique_deps[0])

        # Multiple deps: target the one that comes latest in topo order
        order = self.dag.linearize()
        # Absorbed topics are no longer in the linearization order of live
        # topics, but resolve_absorption guarantees `unique_deps` are all
        # live. If a dep is somehow missing from order (shouldn't happen),
        # fall back to treating it as position 0.
        dep_positions = {d: order.index(d) if d in order else 0 for d in unique_deps}
        latest = max(dep_positions, key=dep_positions.get)  # type: ignore[arg-type]
        return self._branch_name(latest)

    def _branch_name(self, topic_id: str) -> str:
        topic = self.dag.topics[topic_id]
        slug = topic.name.lower().replace(" ", "-").replace("_", "-")
        # Keep it short and valid as a git branch name
        slug = "".join(c for c in slug if c.isalnum() or c == "-")
        slug = slug.strip("-")[:50]
        return f"{self.branch_prefix}/{slug}"

    def build_plan(self) -> SplitPlan:
        """Generate the split plan from current assignments and DAG.

        Returns a SplitPlan with branches ordered by dependency
        (topological order), each targeting the correct base branch.
        """
        plan = SplitPlan(original_base=self.base_branch)

        # Update topic sizes and hunk_ids from assignments. Size is the
        # weighted review cost (default: additions only) — the metric
        # used for threshold decisions and displayed to the user.
        for tid, topic in self.dag.topics.items():
            hunks = self.get_topic_hunks(tid)
            topic.hunk_ids = [h.id for h in hunks]
            topic.estimated_size = sum(self._size(h) for h in hunks)

        # Build branches in linearized order
        for topic_id in self.dag.linearize():
            topic = self.dag.topics[topic_id]
            hunks = self.get_topic_hunks(topic_id)

            if not hunks:
                continue  # Skip empty topics

            branch = BranchPlan(
                topic_id=topic_id,
                branch_name=self._branch_name(topic_id),
                base_branch=self._resolve_base_branch(topic_id),
                hunk_ids=[h.id for h in hunks],
                estimated_size=sum(self._size(h) for h in hunks),
                pr_title=topic.name,
                pr_body=topic.description,
            )
            plan.branches.append(branch)

        # Track unassigned hunks
        plan.unassigned_hunk_ids = [h.id for h in self.get_unassigned_hunks()]

        # Carry absorption map through to downstream tools (render-dag-full,
        # create-prs) so orphan topics can link to the PR that absorbed them.
        plan.absorbed_into = dict(self._absorbed_into)

        return plan

    def get_hunk(self, hunk_id: str) -> Hunk:
        """Look up a hunk by ID."""
        if hunk_id not in self._hunk_index:
            raise KeyError(f"Hunk '{hunk_id}' not found")
        return self._hunk_index[hunk_id]

    def plan_to_json(self, plan: SplitPlan) -> str:
        """Serialize a plan to JSON for agent consumption.

        ``estimated_size`` is the weighted review size. ``removed_lines``
        per branch is informational only — it shows reviewers how much
        deletion the branch contains without influencing split decisions.
        Each hunk also ships its raw ``added_lines`` / ``removed_lines``
        so downstream tools (PR body rendering, etc.) can format
        ``+N/-M`` without re-parsing the diff.
        """
        return json.dumps(
            {
                "original_base": plan.original_base,
                "iteration": plan.iteration,
                "branch_count": plan.branch_count,
                "total_size": plan.total_size,
                "delete_weight": self.delete_weight,
                "has_unassigned": plan.has_unassigned,
                "unassigned_count": len(plan.unassigned_hunk_ids),
                "absorbed_into": dict(plan.absorbed_into),
                "branches": [
                    {
                        "topic_id": b.topic_id,
                        "branch_name": b.branch_name,
                        "base_branch": b.base_branch,
                        "hunk_count": len(b.hunk_ids),
                        "estimated_size": b.estimated_size,
                        "removed_lines": sum(
                            self.get_hunk(hid).removed_lines for hid in b.hunk_ids
                        ),
                        "pr_title": b.pr_title,
                        "validation_status": b.validation_status.value,
                        "validation_errors": b.validation_errors,
                        "hunks": [
                            {
                                "id": h.id,
                                "file_path": h.file_path,
                                "size": self._size(h),
                                "added_lines": h.added_lines,
                                "removed_lines": h.removed_lines,
                                "content": h.content,
                            }
                            for h in (self.get_hunk(hid) for hid in b.hunk_ids)
                        ],
                    }
                    for b in plan.branches
                ],
            },
            indent=2,
        )
