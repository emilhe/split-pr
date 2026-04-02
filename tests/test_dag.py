"""Tests for split_pr.dag."""

import json

import pytest

from split_pr.dag import Topic, TopicDAG, CyclicDependencyError


def make_topic(id: str, name: str | None = None, size: int = 100) -> Topic:
    return Topic(id=id, name=name or id.title(), estimated_size=size)


class TestBasicOperations:
    def test_add_topic(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("cats"))
        assert dag.topic_count == 1
        assert "cats" in dag.topics

    def test_add_duplicate_raises(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("cats"))
        with pytest.raises(ValueError, match="already exists"):
            dag.add_topic(make_topic("cats"))

    def test_remove_topic(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("cats"))
        removed = dag.remove_topic("cats")
        assert removed.id == "cats"
        assert dag.topic_count == 0

    def test_remove_nonexistent_raises(self):
        dag = TopicDAG()
        with pytest.raises(KeyError, match="not found"):
            dag.remove_topic("nope")

    def test_remove_cleans_edges(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("a"))
        dag.add_topic(make_topic("b"))
        dag.add_topic(make_topic("c"))
        dag.add_dependency("a", "b")
        dag.add_dependency("b", "c")
        dag.remove_topic("b")
        # c should have no dependencies now
        assert dag.get_dependencies("c") == []
        assert dag.get_dependents("a") == []


class TestDependencies:
    def test_add_dependency(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("schemas"))
        dag.add_topic(make_topic("api"))
        dag.add_dependency("schemas", "api")
        assert dag.get_dependencies("api") == ["schemas"]
        assert dag.get_dependents("schemas") == ["api"]

    def test_dependency_on_missing_topic_raises(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("a"))
        with pytest.raises(KeyError, match="not found"):
            dag.add_dependency("a", "missing")

    def test_self_dependency_raises(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("a"))
        with pytest.raises(CyclicDependencyError):
            dag.add_dependency("a", "a")

    def test_direct_cycle_raises(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("a"))
        dag.add_topic(make_topic("b"))
        dag.add_dependency("a", "b")
        with pytest.raises(CyclicDependencyError) as exc_info:
            dag.add_dependency("b", "a")
        assert "a" in exc_info.value.cycle
        assert "b" in exc_info.value.cycle

    def test_indirect_cycle_raises(self):
        dag = TopicDAG()
        for name in "abcd":
            dag.add_topic(make_topic(name))
        dag.add_dependency("a", "b")
        dag.add_dependency("b", "c")
        dag.add_dependency("c", "d")
        with pytest.raises(CyclicDependencyError):
            dag.add_dependency("d", "a")

    def test_cycle_does_not_leave_edge(self):
        """After a rejected cyclic add, the edge should not exist."""
        dag = TopicDAG()
        dag.add_topic(make_topic("a"))
        dag.add_topic(make_topic("b"))
        dag.add_dependency("a", "b")
        with pytest.raises(CyclicDependencyError):
            dag.add_dependency("b", "a")
        # b should still only depend on a
        assert dag.get_dependencies("b") == ["a"]
        assert dag.get_dependents("b") == []

    def test_remove_dependency(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("a"))
        dag.add_topic(make_topic("b"))
        dag.add_dependency("a", "b")
        dag.remove_dependency("a", "b")
        assert dag.get_dependencies("b") == []

    def test_remove_nonexistent_dependency_raises(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("a"))
        dag.add_topic(make_topic("b"))
        with pytest.raises(KeyError, match="No dependency"):
            dag.remove_dependency("a", "b")

    def test_transitive_dependencies(self):
        dag = TopicDAG()
        for name in "abcd":
            dag.add_topic(make_topic(name))
        dag.add_dependency("a", "b")
        dag.add_dependency("b", "c")
        dag.add_dependency("a", "c")
        dag.add_dependency("c", "d")
        assert dag.get_all_dependencies("d") == {"a", "b", "c"}
        assert dag.get_all_dependencies("a") == set()


class TestSorting:
    def test_topological_sort_respects_deps(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("api"))
        dag.add_topic(make_topic("schemas"))
        dag.add_topic(make_topic("tests"))
        dag.add_dependency("schemas", "api")
        dag.add_dependency("api", "tests")
        order = dag.topological_sort()
        assert order.index("schemas") < order.index("api")
        assert order.index("api") < order.index("tests")

    def test_linearize_is_deterministic(self):
        dag = TopicDAG()
        # Add in non-alphabetical order
        for name in ["delta", "alpha", "charlie", "bravo"]:
            dag.add_topic(make_topic(name))
        # All independent — linearize should sort alphabetically
        order = dag.linearize()
        assert order == ["alpha", "bravo", "charlie", "delta"]

    def test_linearize_respects_deps_and_breaks_ties(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("z-first"))
        dag.add_topic(make_topic("a-second"))
        dag.add_topic(make_topic("m-third"))
        dag.add_dependency("z-first", "a-second")
        dag.add_dependency("z-first", "m-third")
        order = dag.linearize()
        assert order[0] == "z-first"  # must be first (dependency)
        # a-second and m-third tied, alphabetical: a < m
        assert order[1] == "a-second"
        assert order[2] == "m-third"

    def test_empty_dag(self):
        dag = TopicDAG()
        assert dag.topological_sort() == []
        assert dag.linearize() == []

    def test_single_topic(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("alone"))
        assert dag.topological_sort() == ["alone"]


class TestGroups:
    def test_independent_groups(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("cat-models"))
        dag.add_topic(make_topic("cat-api"))
        dag.add_topic(make_topic("dog-models"))
        dag.add_topic(make_topic("dog-api"))
        dag.add_dependency("cat-models", "cat-api")
        dag.add_dependency("dog-models", "dog-api")
        groups = dag.independent_groups()
        assert len(groups) == 2
        group_sets = [frozenset(g) for g in groups]
        assert frozenset({"cat-models", "cat-api"}) in group_sets
        assert frozenset({"dog-models", "dog-api"}) in group_sets

    def test_single_group_when_connected(self):
        dag = TopicDAG()
        for name in "abc":
            dag.add_topic(make_topic(name))
        dag.add_dependency("a", "b")
        dag.add_dependency("b", "c")
        groups = dag.independent_groups()
        assert len(groups) == 1

    def test_all_independent(self):
        dag = TopicDAG()
        for name in "abc":
            dag.add_topic(make_topic(name))
        groups = dag.independent_groups()
        assert len(groups) == 3

    def test_roots_and_leaves(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("a"))
        dag.add_topic(make_topic("b"))
        dag.add_topic(make_topic("c"))
        dag.add_dependency("a", "b")
        dag.add_dependency("b", "c")
        assert set(dag.roots()) == {"a"}
        assert set(dag.leaves()) == {"c"}

    def test_roots_and_leaves_diamond(self):
        dag = TopicDAG()
        for name in "abcd":
            dag.add_topic(make_topic(name))
        dag.add_dependency("a", "b")
        dag.add_dependency("a", "c")
        dag.add_dependency("b", "d")
        dag.add_dependency("c", "d")
        assert set(dag.roots()) == {"a"}
        assert set(dag.leaves()) == {"d"}


class TestMerge:
    def test_basic_merge(self):
        dag = TopicDAG()
        dag.add_topic(Topic(id="a", name="A", hunk_ids=["h1", "h2"], estimated_size=50))
        dag.add_topic(Topic(id="b", name="B", hunk_ids=["h3"], estimated_size=30))
        merged = dag.merge_topics(["a", "b"], "ab", "A and B")
        assert dag.topic_count == 1
        assert merged.id == "ab"
        assert set(merged.hunk_ids) == {"h1", "h2", "h3"}
        assert merged.estimated_size == 80

    def test_merge_preserves_external_edges(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("base"))
        dag.add_topic(make_topic("a"))
        dag.add_topic(make_topic("b"))
        dag.add_topic(make_topic("consumer"))
        dag.add_dependency("base", "a")
        dag.add_dependency("base", "b")
        dag.add_dependency("a", "consumer")
        dag.add_dependency("b", "consumer")

        dag.merge_topics(["a", "b"], "ab", "AB")

        assert "base" in dag.get_dependencies("ab")
        assert "ab" in dag.get_dependencies("consumer")
        assert dag.topic_count == 3

    def test_merge_drops_internal_edges(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("a"))
        dag.add_topic(make_topic("b"))
        dag.add_dependency("a", "b")
        dag.merge_topics(["a", "b"], "ab", "AB")
        # Merged topic should have no self-edges
        assert dag.get_dependencies("ab") == []
        assert dag.get_dependents("ab") == []

    def test_merge_fewer_than_two_raises(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("a"))
        with pytest.raises(ValueError, match="at least 2"):
            dag.merge_topics(["a"], "a", "A")

    def test_merge_inherits_shared_flag(self):
        dag = TopicDAG()
        dag.add_topic(Topic(id="a", name="A", is_shared=True))
        dag.add_topic(Topic(id="b", name="B", is_shared=False))
        merged = dag.merge_topics(["a", "b"], "ab", "AB")
        assert merged.is_shared is True


class TestSplit:
    def test_basic_split(self):
        dag = TopicDAG()
        dag.add_topic(Topic(id="big", name="Big", hunk_ids=["h1", "h2", "h3"]))
        new_a = Topic(id="big-a", name="Big part A", hunk_ids=["h1"])
        new_b = Topic(id="big-b", name="Big part B", hunk_ids=["h2", "h3"])
        dag.split_topic("big", [new_a, new_b])
        assert dag.topic_count == 2
        assert "big" not in dag.topics

    def test_split_transfers_external_deps(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("base"))
        dag.add_topic(make_topic("big"))
        dag.add_topic(make_topic("consumer"))
        dag.add_dependency("base", "big")
        dag.add_dependency("big", "consumer")

        new_a = Topic(id="big-a", name="A")
        new_b = Topic(id="big-b", name="B")
        dag.split_topic("big", [new_a, new_b])

        # Both new topics inherit base as dependency
        assert "base" in dag.get_dependencies("big-a")
        assert "base" in dag.get_dependencies("big-b")
        # Consumer depends on both (conservative)
        assert "big-a" in dag.get_dependencies("consumer")
        assert "big-b" in dag.get_dependencies("consumer")

    def test_split_with_internal_deps(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("big"))
        new_a = Topic(id="big-a", name="A")
        new_b = Topic(id="big-b", name="B")
        dag.split_topic("big", [new_a, new_b],
                        internal_deps=[("big-a", "big-b")])
        assert "big-a" in dag.get_dependencies("big-b")

    def test_split_fewer_than_two_raises(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("x"))
        with pytest.raises(ValueError, match="at least 2"):
            dag.split_topic("x", [make_topic("y")])


class TestSerialization:
    def test_roundtrip(self):
        dag = TopicDAG()
        dag.add_topic(Topic(id="a", name="Alpha", description="first",
                            estimated_size=100, hunk_ids=["h1", "h2"],
                            is_shared=True))
        dag.add_topic(Topic(id="b", name="Bravo", estimated_size=50))
        dag.add_dependency("a", "b")

        data = dag.to_dict()
        restored = TopicDAG.from_dict(data)

        assert restored.topic_count == 2
        assert restored.topics["a"].name == "Alpha"
        assert restored.topics["a"].is_shared is True
        assert restored.topics["a"].hunk_ids == ["h1", "h2"]
        assert restored.get_dependencies("b") == ["a"]

    def test_to_json_valid(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("x"))
        data = json.loads(dag.to_json())
        assert "topics" in data
        assert "edges" in data

    def test_summary(self):
        dag = TopicDAG()
        dag.add_topic(make_topic("schemas", size=200))
        dag.add_topic(make_topic("api", size=300))
        dag.add_topic(make_topic("tests", size=150))
        dag.add_dependency("schemas", "api")
        dag.add_dependency("api", "tests")
        summary = dag.summary()
        assert "3 topics" in summary
        assert "schemas" in summary.lower()
