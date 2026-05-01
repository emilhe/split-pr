"""Tests for CLI-level helpers in split_pr.cli.

Covers helpers that don't fit naturally in state/dag tests: virtual-to-raw
hunk resolution with topic absorption, and DAG rendering with absorbed nodes.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from split_pr.cli import _get_assignments, app


runner = CliRunner()


class TestGetAssignmentsAbsorption:
    """Size-based winner selection when virtual hunks converge on a raw hunk."""

    def test_no_virtual_resolution_passes_through(self):
        discovery = {"assignments": {"h1": "topic-a", "h2": "topic-b"}}
        assignments, absorbed = _get_assignments(discovery)
        assert assignments == {"h1": "topic-a", "h2": "topic-b"}
        assert absorbed == {}

    def test_single_topic_per_raw_hunk(self):
        # Two virtual hunks from the same raw, both assigned to the same topic.
        discovery = {"assignments": {"v1": "topic-a", "v2": "topic-a"}}
        hunks_data = {
            "files": [
                {
                    "hunks": [
                        {"id": "v1", "original_hunk_id": "raw1", "size": 10},
                        {"id": "v2", "original_hunk_id": "raw1", "size": 20},
                    ]
                }
            ]
        }
        assignments, absorbed = _get_assignments(discovery, hunks_data)
        assert assignments == {"raw1": "topic-a"}
        assert absorbed == {}

    def test_competing_topics_winner_by_size(self):
        discovery = {
            "assignments": {
                "v1": "topic-small",  # 10 lines
                "v2": "topic-big",  # 100 lines
                "v3": "topic-big",  # 50 lines
            }
        }
        hunks_data = {
            "files": [
                {
                    "hunks": [
                        {"id": "v1", "original_hunk_id": "raw1", "size": 10},
                        {"id": "v2", "original_hunk_id": "raw1", "size": 100},
                        {"id": "v3", "original_hunk_id": "raw1", "size": 50},
                    ]
                }
            ]
        }
        assignments, absorbed = _get_assignments(discovery, hunks_data)
        assert assignments == {"raw1": "topic-big"}
        # topic-small had all its hunks absorbed into topic-big.
        assert absorbed == {"topic-small": "topic-big"}

    def test_loser_preserved_when_it_has_other_raw_hunks(self):
        """A topic that loses one raw hunk but owns another is not absorbed."""
        discovery = {
            "assignments": {
                "v1": "topic-a",  # 10 — loses raw1 to topic-b
                "v2": "topic-b",  # 100 — wins raw1
                "v3": "topic-a",  # 50 — owns raw2 outright
            }
        }
        hunks_data = {
            "files": [
                {
                    "hunks": [
                        {"id": "v1", "original_hunk_id": "raw1", "size": 10},
                        {"id": "v2", "original_hunk_id": "raw1", "size": 100},
                        {"id": "v3", "original_hunk_id": "raw2", "size": 50},
                    ]
                }
            ]
        }
        assignments, absorbed = _get_assignments(discovery, hunks_data)
        assert assignments == {"raw1": "topic-b", "raw2": "topic-a"}
        # topic-a kept raw2, so it's not absorbed.
        assert absorbed == {}

    def test_absorbed_into_picks_winner_with_most_loser_size(self):
        """Loser absorbed by the winner that claimed the most of its size."""
        discovery = {
            "assignments": {
                "v1": "loser",  # 30 lines → raw1, winner is big-a
                "v2": "big-a",  # 100 → raw1
                "v3": "loser",  # 10 → raw2, winner is big-b
                "v4": "big-b",  # 100 → raw2
            }
        }
        hunks_data = {
            "files": [
                {
                    "hunks": [
                        {"id": "v1", "original_hunk_id": "raw1", "size": 30},
                        {"id": "v2", "original_hunk_id": "raw1", "size": 100},
                        {"id": "v3", "original_hunk_id": "raw2", "size": 10},
                        {"id": "v4", "original_hunk_id": "raw2", "size": 100},
                    ]
                }
            ]
        }
        assignments, absorbed = _get_assignments(discovery, hunks_data)
        assert assignments == {"raw1": "big-a", "raw2": "big-b"}
        # loser is absorbed into big-a (30 lines > 10 lines).
        assert absorbed == {"loser": "big-a"}

    def test_mixed_virtual_and_raw_hunks(self):
        """Non-virtual hunks pass through untouched."""
        discovery = {
            "assignments": {
                "v1": "topic-a",  # virtual
                "raw-only": "topic-b",  # no original_hunk_id
            }
        }
        hunks_data = {
            "files": [
                {
                    "hunks": [
                        {"id": "v1", "original_hunk_id": "raw1", "size": 10},
                        {"id": "raw-only", "size": 5},
                    ]
                }
            ]
        }
        assignments, absorbed = _get_assignments(discovery, hunks_data)
        assert assignments == {"raw1": "topic-a", "raw-only": "topic-b"}
        assert absorbed == {}


class TestRenderDagFullAbsorbed:
    """render-dag-full emits click links and dashed style for absorbed nodes."""

    def _write(self, tmp_path: Path, name: str, data: dict) -> Path:
        p = tmp_path / name
        p.write_text(json.dumps(data))
        return p

    def test_absorbed_node_links_to_absorbing_pr(self, tmp_path: Path):
        discovery = {
            "dag": {
                "topics": {
                    "winner": {"id": "winner", "name": "Winner", "estimated_size": 500, "hunk_ids": []},
                    "absorbed": {"id": "absorbed", "name": "Absorbed", "estimated_size": 100, "hunk_ids": []},
                },
                "edges": [],
            }
        }
        plan = {
            "original_base": "main",
            "branch_count": 1,
            "branches": [
                {
                    "topic_id": "winner",
                    "branch_name": "split/winner",
                    "base_branch": "main",
                    "estimated_size": 500,
                }
            ],
            "absorbed_into": {"absorbed": "winner"},
        }
        links = {"winner": "https://example.com/pr/1"}

        discovery_path = self._write(tmp_path, "discovery.json", discovery)
        plan_path = self._write(tmp_path, "plan.json", plan)
        links_path = self._write(tmp_path, "links.json", links)

        result = runner.invoke(
            app,
            [
                "render-dag-full",
                str(discovery_path),
                str(plan_path),
                "--links",
                str(links_path),
            ],
        )
        assert result.exit_code == 0, result.output
        out = result.output

        # Absorbed node is dashed via classDef.
        assert ":::absorbed" in out
        assert "classDef absorbed" in out
        # Label says it lives in the winning PR.
        assert "(in 1/1)" in out
        # Both the winner and the absorbed node click through to the winner URL.
        assert 'click winner href "https://example.com/pr/1"' in out
        assert 'click absorbed href "https://example.com/pr/1"' in out

class TestSplitTopicCommand:
    """split-topic distributes hunks into sub-topics and transfers DAG edges."""

    def _discovery(self, tmp_path: Path) -> tuple[Path, Path]:
        """Build a minimal hunks.json + discovery.json with one big topic."""
        hunks = {
            "files": [
                {
                    "path": "src/adapter/shared.py",
                    "hunks": [
                        {
                            "id": "h1",
                            "file_path": "src/adapter/shared.py",
                            "added_lines": 200,
                            "removed_lines": 0,
                        },
                    ],
                },
                {
                    "path": "src/cube/model.py",
                    "hunks": [
                        {
                            "id": "h2",
                            "file_path": "src/cube/model.py",
                            "added_lines": 400,
                            "removed_lines": 0,
                        },
                    ],
                },
                {
                    "path": "src/inseason/forecast/route.py",
                    "hunks": [
                        {
                            "id": "h3",
                            "file_path": "src/inseason/forecast/route.py",
                            "added_lines": 0,
                            "removed_lines": 300,
                        },
                    ],
                },
            ]
        }
        discovery = {
            "dag": {
                "topics": {
                    "shared": {
                        "id": "shared", "name": "Shared", "estimated_size": 50,
                        "hunk_ids": [], "is_shared": True,
                    },
                    "cube-foundation": {
                        "id": "cube-foundation", "name": "Cube foundation",
                        "estimated_size": 900,
                        "hunk_ids": ["h1", "h2", "h3"], "is_shared": False,
                    },
                    "downstream": {
                        "id": "downstream", "name": "Downstream",
                        "estimated_size": 100, "hunk_ids": [], "is_shared": False,
                    },
                },
                "edges": [
                    {"from": "shared", "to": "cube-foundation"},
                    {"from": "cube-foundation", "to": "downstream"},
                ],
            },
            "assignments": {
                "h1": "cube-foundation",
                "h2": "cube-foundation",
                "h3": "cube-foundation",
            },
        }
        hunks_path = tmp_path / "hunks.json"
        discovery_path = tmp_path / "discovery.json"
        hunks_path.write_text(json.dumps(hunks))
        discovery_path.write_text(json.dumps(discovery))
        return hunks_path, discovery_path

    def test_happy_path_splits_and_rewires(self, tmp_path: Path):
        hunks_path, discovery_path = self._discovery(tmp_path)
        result = runner.invoke(
            app,
            [
                "split-topic", str(hunks_path), str(discovery_path), "cube-foundation",
                "--into", "legacy-adapter-core:path:src/adapter/",
                "--into", "cube-skeleton:path:src/cube/",
                "--into", "old-forecast-removal:path:src/inseason/forecast/",
                "--dep", "cube-skeleton:legacy-adapter-core",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(discovery_path.read_text())
        topics = data["dag"]["topics"]
        assert "cube-foundation" not in topics
        assert set(topics.keys()) == {
            "shared", "legacy-adapter-core", "cube-skeleton",
            "old-forecast-removal", "downstream",
        }

        # Hunks distributed.
        assignments = data["assignments"]
        assert assignments["h1"] == "legacy-adapter-core"
        assert assignments["h2"] == "cube-skeleton"
        assert assignments["h3"] == "old-forecast-removal"

        # External edges transferred to every sub-topic.
        edges = {(e["from"], e["to"]) for e in data["dag"]["edges"]}
        for new_id in ("legacy-adapter-core", "cube-skeleton", "old-forecast-removal"):
            assert ("shared", new_id) in edges
            assert (new_id, "downstream") in edges
        # Intra-split dep applied.
        assert ("cube-skeleton", "legacy-adapter-core") in edges
        # Old topic's edges gone.
        assert not any("cube-foundation" in e for e in edges)

        # Estimated sizes reflect assigned hunks (from hunks.json).
        assert topics["legacy-adapter-core"]["estimated_size"] == 200
        assert topics["cube-skeleton"]["estimated_size"] == 400
        assert topics["old-forecast-removal"]["estimated_size"] == 300

    def test_unmatched_hunks_fail_fast(self, tmp_path: Path):
        hunks_path, discovery_path = self._discovery(tmp_path)
        result = runner.invoke(
            app,
            [
                "split-topic", str(hunks_path), str(discovery_path), "cube-foundation",
                "--into", "legacy-adapter-core:path:src/adapter/",
                "--into", "cube-skeleton:path:src/cube/",
                # h3 (src/inseason/forecast/route.py) isn't covered.
            ],
        )
        assert result.exit_code != 0
        assert "don't match any" in result.output
        # discovery.json unchanged.
        data = json.loads(discovery_path.read_text())
        assert "cube-foundation" in data["dag"]["topics"]

    def test_overlapping_rules_fail_fast(self, tmp_path: Path):
        hunks_path, discovery_path = self._discovery(tmp_path)
        result = runner.invoke(
            app,
            [
                "split-topic", str(hunks_path), str(discovery_path), "cube-foundation",
                # Both rules match h1.
                "--into", "a:path:src/",
                "--into", "b:path:adapter/",
                "--into", "c:path:never-matches",
            ],
        )
        assert result.exit_code != 0
        assert "multiple rules" in result.output

    def test_dry_run_does_not_write(self, tmp_path: Path):
        hunks_path, discovery_path = self._discovery(tmp_path)
        before = discovery_path.read_text()
        result = runner.invoke(
            app,
            [
                "split-topic", str(hunks_path), str(discovery_path), "cube-foundation",
                "--into", "legacy-adapter-core:path:src/adapter/",
                "--into", "cube-skeleton:path:src/cube/",
                "--into", "old-forecast-removal:path:src/inseason/forecast/",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert discovery_path.read_text() == before
        assert "Would split" in result.output


class TestRenderDagFullNoAbsorption:
    def test_no_absorption_no_dashed_style(self, tmp_path: Path):
        discovery = {
            "dag": {
                "topics": {
                    "a": {"id": "a", "name": "A", "estimated_size": 50, "hunk_ids": []},
                    "b": {"id": "b", "name": "B", "estimated_size": 50, "hunk_ids": []},
                },
                "edges": [{"from": "a", "to": "b"}],
            }
        }
        plan = {
            "original_base": "main",
            "branch_count": 2,
            "branches": [
                {"topic_id": "a", "branch_name": "split/a", "base_branch": "main", "estimated_size": 50},
                {"topic_id": "b", "branch_name": "split/b", "base_branch": "split/a", "estimated_size": 50},
            ],
            "absorbed_into": {},
        }
        discovery_path = tmp_path / "discovery.json"
        plan_path = tmp_path / "plan.json"
        discovery_path.write_text(json.dumps(discovery))
        plan_path.write_text(json.dumps(plan))
        result = runner.invoke(
            app, ["render-dag-full", str(discovery_path), str(plan_path)]
        )
        assert result.exit_code == 0, result.output
        # No absorbed nodes → no classDef line emitted.
        assert ":::absorbed" not in result.output
        assert "classDef absorbed" not in result.output


class TestRenderDagReduce:
    """render-dag and render-dag-full transitively reduce edges by default."""

    def _discovery_with_redundant_edge(self, tmp_path: Path) -> Path:
        # a -> b -> c plus the redundant a -> c.
        discovery = {
            "dag": {
                "topics": {
                    "a": {"id": "a", "name": "A", "estimated_size": 10, "hunk_ids": []},
                    "b": {"id": "b", "name": "B", "estimated_size": 10, "hunk_ids": []},
                    "c": {"id": "c", "name": "C", "estimated_size": 10, "hunk_ids": []},
                },
                "edges": [
                    {"from": "a", "to": "b"},
                    {"from": "b", "to": "c"},
                    {"from": "a", "to": "c"},
                ],
            }
        }
        p = tmp_path / "discovery.json"
        p.write_text(json.dumps(discovery))
        return p

    def test_render_dag_reduces_by_default(self, tmp_path: Path):
        path = self._discovery_with_redundant_edge(tmp_path)
        result = runner.invoke(app, ["render-dag", str(path)])
        assert result.exit_code == 0, result.output
        assert "a --> b" in result.output
        assert "b --> c" in result.output
        # The transitive a -> c edge is dropped.
        assert "a --> c" not in result.output

    def test_render_dag_no_reduce_keeps_all_edges(self, tmp_path: Path):
        path = self._discovery_with_redundant_edge(tmp_path)
        result = runner.invoke(app, ["render-dag", str(path), "--no-reduce"])
        assert result.exit_code == 0, result.output
        assert "a --> b" in result.output
        assert "b --> c" in result.output
        assert "a --> c" in result.output

    def test_render_dag_full_reduces_by_default(self, tmp_path: Path):
        path = self._discovery_with_redundant_edge(tmp_path)
        plan = {
            "original_base": "main",
            "branch_count": 3,
            "branches": [
                {"topic_id": "a", "branch_name": "split/a", "base_branch": "main", "estimated_size": 10},
                {"topic_id": "b", "branch_name": "split/b", "base_branch": "split/a", "estimated_size": 10},
                {"topic_id": "c", "branch_name": "split/c", "base_branch": "split/b", "estimated_size": 10},
            ],
            "absorbed_into": {},
        }
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan))
        result = runner.invoke(app, ["render-dag-full", str(path), str(plan_path)])
        assert result.exit_code == 0, result.output
        assert "a --> b" in result.output
        assert "b --> c" in result.output
        assert "a --> c" not in result.output


class TestRenderDagSizeWarning:
    """render-dag warns to stderr if the rendered block exceeds GitHub's limit."""

    def test_warns_when_block_over_limit(self, tmp_path: Path):
        # Build a discovery whose rendered Mermaid block is forced over 50_000
        # chars by inflating one topic's name. One topic, no edges.
        discovery = {
            "dag": {
                "topics": {
                    "huge": {
                        "id": "huge",
                        "name": "X" * 60_000,
                        "estimated_size": 1,
                        "hunk_ids": [],
                    }
                },
                "edges": [],
            }
        }
        path = tmp_path / "discovery.json"
        path.write_text(json.dumps(discovery))
        result = runner.invoke(app, ["render-dag", str(path)])
        assert result.exit_code == 0, result.output
        # Typer CliRunner merges stderr into output by default; mix is fine.
        assert "Maximum text size" in result.output or "over GitHub" in result.output

    def test_no_warning_for_small_block(self, tmp_path: Path):
        discovery = {
            "dag": {
                "topics": {
                    "small": {"id": "small", "name": "S", "estimated_size": 1, "hunk_ids": []}
                },
                "edges": [],
            }
        }
        path = tmp_path / "discovery.json"
        path.write_text(json.dumps(discovery))
        result = runner.invoke(app, ["render-dag", str(path)])
        assert result.exit_code == 0
        assert "WARNING" not in result.output
