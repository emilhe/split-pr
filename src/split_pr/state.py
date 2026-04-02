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
from split_pr.diff_parser import Hunk, ParsedDiff


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
    """

    original_base: str
    branches: list[BranchPlan] = field(default_factory=list)
    unassigned_hunk_ids: list[str] = field(default_factory=list)
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
        """Total size (lines changed) for a topic."""
        return sum(h.size for h in self.get_topic_hunks(topic_id))

    def get_oversized_topics(self) -> list[str]:
        """Topics that exceed the size threshold."""
        return [
            tid for tid in self.dag.topics
            if self.get_topic_size(tid) > self.size_threshold
        ]

    def _resolve_base_branch(self, topic_id: str) -> str:
        """Determine what branch a topic's PR should target.

        For DAG-based splitting:
        - If the topic has no dependencies, target the original base branch.
        - If it has one dependency, target that dependency's branch.
        - If it has multiple dependencies, target the last one in
          topological order (all others will have been merged by then
          in a linear merge strategy).
        """
        deps = self.dag.get_dependencies(topic_id)
        if not deps:
            return self.base_branch

        if len(deps) == 1:
            return self._branch_name(deps[0])

        # Multiple deps: target the one that comes latest in topo order
        order = self.dag.linearize()
        dep_positions = {d: order.index(d) for d in deps}
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

        # Update topic sizes and hunk_ids from assignments
        for tid, topic in self.dag.topics.items():
            hunks = self.get_topic_hunks(tid)
            topic.hunk_ids = [h.id for h in hunks]
            topic.estimated_size = sum(h.size for h in hunks)

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
                estimated_size=sum(h.size for h in hunks),
                pr_title=topic.name,
                pr_body=topic.description,
            )
            plan.branches.append(branch)

        # Track unassigned hunks
        plan.unassigned_hunk_ids = [h.id for h in self.get_unassigned_hunks()]

        return plan

    def get_hunk(self, hunk_id: str) -> Hunk:
        """Look up a hunk by ID."""
        if hunk_id not in self._hunk_index:
            raise KeyError(f"Hunk '{hunk_id}' not found")
        return self._hunk_index[hunk_id]

    def plan_to_json(self, plan: SplitPlan) -> str:
        """Serialize a plan to JSON for agent consumption."""
        return json.dumps(
            {
                "original_base": plan.original_base,
                "iteration": plan.iteration,
                "branch_count": plan.branch_count,
                "total_size": plan.total_size,
                "has_unassigned": plan.has_unassigned,
                "unassigned_count": len(plan.unassigned_hunk_ids),
                "branches": [
                    {
                        "topic_id": b.topic_id,
                        "branch_name": b.branch_name,
                        "base_branch": b.base_branch,
                        "hunk_count": len(b.hunk_ids),
                        "estimated_size": b.estimated_size,
                        "pr_title": b.pr_title,
                        "validation_status": b.validation_status.value,
                        "validation_errors": b.validation_errors,
                        "hunks": [
                            {
                                "id": h.id,
                                "file_path": h.file_path,
                                "size": h.size,
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
