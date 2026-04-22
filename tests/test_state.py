"""Tests for split_pr.state."""

import json

import pytest

from split_pr.dag import Topic, TopicDAG
from split_pr.diff_parser import parse_diff, ParsedDiff
from split_pr.state import SplitPlanner, SplitPlan, BranchPlan, ValidationStatus


# -- Realistic multi-topic diff for integration-style tests --

CATS_AND_DOGS_DIFF = """\
diff --git a/src/cats/models.py b/src/cats/models.py
index abc1234..def5678 100644
--- a/src/cats/models.py
+++ b/src/cats/models.py
@@ -1,5 +1,8 @@
 class Cat:
     name: str
+    breed: str
+    indoor: bool
+    weight: float

     def meow(self):
         return "meow"
diff --git a/src/dogs/models.py b/src/dogs/models.py
index 1111111..2222222 100644
--- a/src/dogs/models.py
+++ b/src/dogs/models.py
@@ -1,5 +1,9 @@
 class Dog:
     name: str
+    breed: str
+    size: str
+    good_boy: bool
+    fetch_skill: int

     def bark(self):
         return "woof"
diff --git a/src/dogs/api.py b/src/dogs/api.py
index 3333333..4444444 100644
--- a/src/dogs/api.py
+++ b/src/dogs/api.py
@@ -5,4 +5,12 @@ from src.dogs.models import Dog

 @app.get("/dogs")
 def list_dogs():
-    return []
+    return get_all_dogs()
+
+
+@app.get("/dogs/{dog_id}")
+def get_dog(dog_id: int):
+    return get_dog_by_id(dog_id)
+
+
+def get_all_dogs():
+    return Dog.query.all()
diff --git a/src/shared/utils.py b/src/shared/utils.py
index 5555555..6666666 100644
--- a/src/shared/utils.py
+++ b/src/shared/utils.py
@@ -1,3 +1,6 @@
+import logging
+
+logger = logging.getLogger(__name__)

 def validate_name(name: str) -> bool:
     return bool(name and len(name) < 100)
"""


def _build_test_setup() -> tuple[ParsedDiff, TopicDAG, dict[str, str]]:
    """Create a standard test setup with cats, dogs, and shared topics."""
    parsed = parse_diff(CATS_AND_DOGS_DIFF)
    hunks = parsed.all_hunks
    assert len(hunks) == 4  # sanity check

    dag = TopicDAG()
    dag.add_topic(Topic(id="shared", name="Shared utilities", is_shared=True))
    dag.add_topic(Topic(id="cats", name="Cat models"))
    dag.add_topic(Topic(id="dogs", name="Dog models and API"))
    dag.add_dependency("shared", "cats")
    dag.add_dependency("shared", "dogs")

    # Assign hunks to topics based on file path
    assignments: dict[str, str] = {}
    for h in hunks:
        if "shared" in h.file_path:
            assignments[h.id] = "shared"
        elif "cats" in h.file_path:
            assignments[h.id] = "cats"
        elif "dogs" in h.file_path:
            assignments[h.id] = "dogs"

    return parsed, dag, assignments


class TestSplitPlanner:
    def test_assign_hunk(self):
        parsed, dag, _ = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        hunk = parsed.all_hunks[0]
        tid = "shared" if "shared" in hunk.file_path else "cats"
        planner.assign_hunk(hunk.id, tid)
        assert planner.get_topic_hunks(tid) == [hunk]

    def test_assign_invalid_hunk_raises(self):
        parsed, dag, _ = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        with pytest.raises(KeyError, match="Hunk"):
            planner.assign_hunk("nonexistent", "cats")

    def test_assign_invalid_topic_raises(self):
        parsed, dag, _ = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        hunk = parsed.all_hunks[0]
        with pytest.raises(KeyError, match="Topic"):
            planner.assign_hunk(hunk.id, "nonexistent")

    def test_bulk_assign(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        planner.assign_hunks(assignments)
        assert planner.get_unassigned_hunks() == []

    def test_unassigned_hunks(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        # Assign only some
        first_hunk = parsed.all_hunks[0]
        tid = assignments[first_hunk.id]
        planner.assign_hunk(first_hunk.id, tid)
        unassigned = planner.get_unassigned_hunks()
        assert len(unassigned) == len(parsed.all_hunks) - 1

    def test_topic_size(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        planner.assign_hunks(assignments)
        # Each topic should have nonzero size
        for tid in dag.topics:
            assert planner.get_topic_size(tid) > 0

    def test_oversized_topics(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag, size_threshold=5)
        planner.assign_hunks(assignments)
        oversized = planner.get_oversized_topics()
        # dogs topic has the most lines, should be oversized at threshold=5
        assert "dogs" in oversized


class TestBuildPlan:
    def test_plan_structure(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        planner.assign_hunks(assignments)
        plan = planner.build_plan()

        assert isinstance(plan, SplitPlan)
        assert plan.branch_count == 3
        assert plan.original_base == "main"
        assert not plan.has_unassigned

    def test_plan_ordering_respects_deps(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        planner.assign_hunks(assignments)
        plan = planner.build_plan()

        topic_order = [b.topic_id for b in plan.branches]
        # shared must come before cats and dogs
        assert topic_order.index("shared") < topic_order.index("cats")
        assert topic_order.index("shared") < topic_order.index("dogs")

    def test_base_branches(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        planner.assign_hunks(assignments)
        plan = planner.build_plan()

        for branch in plan.branches:
            if branch.topic_id == "shared":
                assert branch.base_branch == "main"
            else:
                # cats and dogs depend on shared
                assert "shared" in branch.base_branch

    def test_branch_names(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        planner.assign_hunks(assignments)
        plan = planner.build_plan()

        for branch in plan.branches:
            assert branch.branch_name.startswith("split/")
            assert "/" in branch.branch_name

    def test_custom_branch_prefix(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag, branch_prefix="pr-split")
        planner.assign_hunks(assignments)
        plan = planner.build_plan()

        for branch in plan.branches:
            assert branch.branch_name.startswith("pr-split/")

    def test_custom_base_branch(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag, base_branch="develop")
        planner.assign_hunks(assignments)
        plan = planner.build_plan()

        assert plan.original_base == "develop"
        shared = plan.get_branch("shared")
        assert shared is not None
        assert shared.base_branch == "develop"

    def test_empty_topics_skipped(self):
        parsed, dag, _ = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        # Assign nothing — all topics empty
        plan = planner.build_plan()
        assert plan.branch_count == 0
        assert plan.has_unassigned

    def test_plan_with_unassigned(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        # Only assign shared hunks
        for hid, tid in assignments.items():
            if tid == "shared":
                planner.assign_hunk(hid, tid)
        plan = planner.build_plan()
        assert plan.has_unassigned
        assert plan.branch_count == 1

    def test_get_branch(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        planner.assign_hunks(assignments)
        plan = planner.build_plan()

        branch = plan.get_branch("cats")
        assert branch is not None
        assert branch.topic_id == "cats"

        assert plan.get_branch("nonexistent") is None


class TestPlanSerialization:
    def test_plan_to_json(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        planner.assign_hunks(assignments)
        plan = planner.build_plan()

        json_str = planner.plan_to_json(plan)
        data = json.loads(json_str)

        assert data["original_base"] == "main"
        assert data["branch_count"] == 3
        assert not data["has_unassigned"]
        assert len(data["branches"]) == 3

    def test_plan_json_includes_hunks(self):
        parsed, dag, assignments = _build_test_setup()
        planner = SplitPlanner(parsed, dag)
        planner.assign_hunks(assignments)
        plan = planner.build_plan()

        data = json.loads(planner.plan_to_json(plan))
        for branch in data["branches"]:
            assert "hunks" in branch
            assert len(branch["hunks"]) > 0
            for hunk in branch["hunks"]:
                assert "id" in hunk
                assert "file_path" in hunk
                assert "content" in hunk

    def test_plan_json_includes_absorbed_into(self):
        parsed, dag, assignments = _build_test_setup()
        # cats is empty → pretend it was absorbed into dogs so downstream
        # consumers can route cats references to the dogs branch.
        dag_copy = TopicDAG()
        for t in dag.topics.values():
            dag_copy.add_topic(t)
        for dep in ("cats", "dogs"):
            dag_copy.add_dependency("shared", dep)
        planner = SplitPlanner(parsed, dag_copy, absorbed_into={"cats": "dogs"})
        # Only give cats' hunks to dogs so the "cats" topic itself is empty.
        cats_to_dogs = {
            hid: ("dogs" if tid == "cats" else tid) for hid, tid in assignments.items()
        }
        planner.assign_hunks(cats_to_dogs)
        plan = planner.build_plan()

        data = json.loads(planner.plan_to_json(plan))
        assert data["absorbed_into"] == {"cats": "dogs"}


class TestValidationStatus:
    def test_default_pending(self):
        bp = BranchPlan(topic_id="t", branch_name="b", base_branch="main")
        assert bp.validation_status == ValidationStatus.PENDING
        assert not bp.is_valid

    def test_passed(self):
        bp = BranchPlan(topic_id="t", branch_name="b", base_branch="main",
                        validation_status=ValidationStatus.PASSED)
        assert bp.is_valid

    def test_plan_all_valid(self):
        plan = SplitPlan(
            original_base="main",
            branches=[
                BranchPlan(topic_id="a", branch_name="b1", base_branch="main",
                           validation_status=ValidationStatus.PASSED),
                BranchPlan(topic_id="b", branch_name="b2", base_branch="b1",
                           validation_status=ValidationStatus.PASSED),
            ],
        )
        assert plan.all_valid

    def test_plan_not_all_valid(self):
        plan = SplitPlan(
            original_base="main",
            branches=[
                BranchPlan(topic_id="a", branch_name="b1", base_branch="main",
                           validation_status=ValidationStatus.PASSED),
                BranchPlan(topic_id="b", branch_name="b2", base_branch="b1",
                           validation_status=ValidationStatus.FAILED,
                           validation_errors=["ruff: E501"]),
            ],
        )
        assert not plan.all_valid


class TestDAGBasedBranching:
    """Test that the planner correctly handles DAG-based base branch resolution."""

    def test_diamond_dependency(self):
        """A -> B, A -> C, B -> D, C -> D: D should base on whichever of B/C comes last."""
        diff_text = """\
diff --git a/a.py b/a.py
index 0000001..0000002 100644
--- a/a.py
+++ b/a.py
@@ -1,1 +1,2 @@
 x = 1
+y = 2
diff --git a/b.py b/b.py
index 0000003..0000004 100644
--- a/b.py
+++ b/b.py
@@ -1,1 +1,2 @@
 x = 1
+y = 2
diff --git a/c.py b/c.py
index 0000005..0000006 100644
--- a/c.py
+++ b/c.py
@@ -1,1 +1,2 @@
 x = 1
+y = 2
diff --git a/d.py b/d.py
index 0000007..0000008 100644
--- a/d.py
+++ b/d.py
@@ -1,1 +1,2 @@
 x = 1
+y = 2
"""
        parsed = parse_diff(diff_text)
        hunks = parsed.all_hunks

        dag = TopicDAG()
        dag.add_topic(Topic(id="a", name="A"))
        dag.add_topic(Topic(id="b", name="B"))
        dag.add_topic(Topic(id="c", name="C"))
        dag.add_topic(Topic(id="d", name="D"))
        dag.add_dependency("a", "b")
        dag.add_dependency("a", "c")
        dag.add_dependency("b", "d")
        dag.add_dependency("c", "d")

        planner = SplitPlanner(parsed, dag)
        # Assign each hunk to the matching topic by file name
        for h in hunks:
            topic_id = h.file_path.replace(".py", "")
            planner.assign_hunk(h.id, topic_id)

        plan = planner.build_plan()
        d_branch = plan.get_branch("d")
        assert d_branch is not None
        # D depends on both B and C; its base should be whichever comes
        # later in the linearized order
        order = dag.linearize()
        b_pos = order.index("b")
        c_pos = order.index("c")
        later = "b" if b_pos > c_pos else "c"
        assert later in d_branch.base_branch

    def test_independent_topics_target_main(self):
        diff_text = """\
diff --git a/x.py b/x.py
index 0000001..0000002 100644
--- a/x.py
+++ b/x.py
@@ -1,1 +1,2 @@
 a = 1
+b = 2
diff --git a/y.py b/y.py
index 0000003..0000004 100644
--- a/y.py
+++ b/y.py
@@ -1,1 +1,2 @@
 a = 1
+b = 2
"""
        parsed = parse_diff(diff_text)
        dag = TopicDAG()
        dag.add_topic(Topic(id="x", name="X"))
        dag.add_topic(Topic(id="y", name="Y"))
        # No dependencies between x and y

        planner = SplitPlanner(parsed, dag)
        for h in parsed.all_hunks:
            planner.assign_hunk(h.id, h.file_path.replace(".py", ""))

        plan = planner.build_plan()
        for branch in plan.branches:
            assert branch.base_branch == "main"


class TestAbsorption:
    """Dependency resolution and base-branch selection with absorbed topics."""

    def _abc_setup(self):
        """Three-topic linear chain: a -> b -> c.

        a has no hunks (simulating absorption); b and c have one each.
        """
        diff_text = """\
diff --git a/b.py b/b.py
index 0000001..0000002 100644
--- a/b.py
+++ b/b.py
@@ -1,1 +1,2 @@
 x = 1
+y = 2
diff --git a/c.py b/c.py
index 0000003..0000004 100644
--- a/c.py
+++ b/c.py
@@ -1,1 +1,2 @@
 x = 1
+y = 2
"""
        parsed = parse_diff(diff_text)
        dag = TopicDAG()
        dag.add_topic(Topic(id="a", name="A"))
        dag.add_topic(Topic(id="b", name="B"))
        dag.add_topic(Topic(id="c", name="C"))
        dag.add_dependency("a", "b")
        dag.add_dependency("b", "c")
        return parsed, dag

    def test_absorbed_dep_follows_chain(self):
        """c depends on b; if b was absorbed into a, c targets a's branch."""
        parsed, dag = self._abc_setup()
        planner = SplitPlanner(parsed, dag, absorbed_into={"b": "a"})
        # All hunks go to a (b is absorbed into a).
        for h in parsed.all_hunks:
            tid = "a" if h.file_path == "b.py" else "c"
            planner.assign_hunk(h.id, tid)
        plan = planner.build_plan()

        c_branch = plan.get_branch("c")
        assert c_branch is not None
        # c's stated dep is b, but b was absorbed into a — so c targets a's branch.
        assert "a" in c_branch.base_branch
        assert "b" not in c_branch.base_branch

    def test_absorbed_dep_chain_multi_hop(self):
        """a -> b -> c -> d, where b is absorbed into a and c is absorbed into a."""
        diff_text = """\
diff --git a/a.py b/a.py
index 0000001..0000002 100644
--- a/a.py
+++ b/a.py
@@ -1,1 +1,2 @@
 x = 1
+y = 2
diff --git a/d.py b/d.py
index 0000003..0000004 100644
--- a/d.py
+++ b/d.py
@@ -1,1 +1,2 @@
 x = 1
+y = 2
"""
        parsed = parse_diff(diff_text)
        dag = TopicDAG()
        for tid in ("a", "b", "c", "d"):
            dag.add_topic(Topic(id=tid, name=tid.upper()))
        dag.add_dependency("a", "b")
        dag.add_dependency("b", "c")
        dag.add_dependency("c", "d")

        planner = SplitPlanner(parsed, dag, absorbed_into={"b": "a", "c": "a"})
        for h in parsed.all_hunks:
            planner.assign_hunk(h.id, "a" if h.file_path == "a.py" else "d")
        plan = planner.build_plan()

        d_branch = plan.get_branch("d")
        assert d_branch is not None
        # d -> c, c absorbed into a → d targets a's branch after the walk.
        assert "a" in d_branch.base_branch

    def test_resolve_absorption_stops_at_live_topic(self):
        parsed, dag = self._abc_setup()
        planner = SplitPlanner(parsed, dag, absorbed_into={"b": "a"})
        assert planner.resolve_absorption("b") == "a"
        assert planner.resolve_absorption("a") == "a"  # not absorbed
        assert planner.resolve_absorption("c") == "c"  # not absorbed

    def test_resolve_absorption_handles_self_cycle(self):
        """Self-referential absorption (shouldn't happen) doesn't loop."""
        parsed, dag = self._abc_setup()
        planner = SplitPlanner(parsed, dag, absorbed_into={"a": "a"})
        # Should return without infinite-looping; landing topic is "a".
        assert planner.resolve_absorption("a") == "a"
