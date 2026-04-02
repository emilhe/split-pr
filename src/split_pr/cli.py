"""CLI entry point for split-pr tools.

Provides subcommands that the skill and agents call. Each subcommand is a thin
wrapper around the library functions, designed to be invoked as:

    uv run --project ~/projects/split-pr split-pr-tools <command> <args>

This makes all invocations match a single whitelistable pattern.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import typer

from split_pr.diff_parser import build_patch, parse_diff
from split_pr.dag import TopicDAG
from split_pr.state import SplitPlanner

app = typer.Typer(help="Split-PR tools — called by the split-pr skill and agents.")


@app.command(name="parse-diff")
def parse_diff_cmd(
    diff_file: Path = typer.Argument(..., help="Path to unified diff file"),
) -> None:
    """Parse a unified diff into structured hunk JSON."""
    parsed = parse_diff(diff_file.read_text())
    typer.echo(parsed.to_json())


@app.command()
def stats(
    hunks_file: Path = typer.Argument(..., help="Path to hunks JSON from parse-diff"),
) -> None:
    """Print summary statistics for a parsed diff."""
    data = json.loads(hunks_file.read_text())
    typer.echo(f"Files: {data['file_count']}")
    typer.echo(f"Hunks: {data['hunk_count']}")
    typer.echo(f"Total lines changed: {data['total_size']}")
    for f in data["files"]:
        typer.echo(f"  {f['path']}: {f['total_size']} lines, {len(f['hunks'])} hunks"
                   + (" [new]" if f["is_new"] else "")
                   + (" [deleted]" if f["is_deleted"] else ""))


@app.command(name="list-hunks")
def list_hunks(
    hunks_file: Path = typer.Argument(..., help="Path to hunks JSON from parse-diff"),
) -> None:
    """List all files with their hunk IDs, sizes, and flags.

    Output format per line: NEW|MOD|DEL  <size>  <path>  [<hunk_count> hunks: <id1>,<id2>,...]
    This gives agents everything needed for topic assignment.
    """
    data = json.loads(hunks_file.read_text())
    for f in data["files"]:
        if f["is_new"]:
            marker = "NEW"
        elif f["is_deleted"]:
            marker = "DEL"
        else:
            marker = "MOD"
        hunk_ids = [h["id"] for h in f["hunks"]]
        typer.echo(
            f"{marker:3} {f['total_size']:5} {f['path']}  "
            f"[{len(hunk_ids)} hunks: {','.join(hunk_ids)}]"
        )


@app.command(name="show-hunks")
def show_hunks(
    hunks_file: Path = typer.Argument(..., help="Path to hunks JSON"),
    hunk_ids: str = typer.Argument(..., help="Comma-separated hunk IDs to inspect"),
) -> None:
    """Show detailed info for specific hunks by ID.

    Pass a comma-separated list of hunk IDs to see their file paths,
    added/removed lines, and sizes.
    """
    data = json.loads(hunks_file.read_text())
    ids_wanted = {h.strip() for h in hunk_ids.split(",")}

    found = set()
    for file_info in data["files"]:
        for h in file_info["hunks"]:
            if h["id"] in ids_wanted:
                found.add(h["id"])
                total = h["added_lines"] + h["removed_lines"]
                typer.echo(
                    f"{h['id']} {h['file_path']:50} "
                    f"+{h['added_lines']}/-{h['removed_lines']} = {total}"
                )

    missing = ids_wanted - found
    if missing:
        for m in sorted(missing):
            typer.echo(f"{m} NOT FOUND")


@app.command(name="show-plan")
def show_plan(
    plan_file: Path = typer.Argument(..., help="Path to plan JSON"),
) -> None:
    """Summarize a split plan: branches, sizes, base branches, hunk counts."""
    plan = json.loads(plan_file.read_text())
    typer.echo(f"Base: {plan['original_base']}")
    typer.echo(f"Branches: {plan['branch_count']}")
    typer.echo(f"Total size: {plan['total_size']} lines")
    typer.echo(f"Unassigned hunks: {plan['unassigned_count']}")
    typer.echo()
    for i, b in enumerate(plan["branches"], 1):
        typer.echo(
            f"{i}. {b['topic_id']}  "
            f"base={b['base_branch']}  "
            f"branch={b['branch_name']}  "
            f"{b['estimated_size']} lines, {b['hunk_count']} hunks"
        )


@app.command()
def build_plan(
    diff_file: Path = typer.Argument(..., help="Path to unified diff file"),
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
    base: str = typer.Argument("main", help="Base branch name"),
    threshold: int = typer.Argument(400, help="Max lines per split PR"),
) -> None:
    """Build a split plan from a diff and discovery output."""
    parsed = parse_diff(diff_file.read_text())
    discovery = json.loads(discovery_file.read_text())
    dag = TopicDAG.from_dict(discovery["dag"])
    assignments = discovery["assignments"]

    planner = SplitPlanner(parsed, dag, base_branch=base, size_threshold=threshold)
    planner.assign_hunks(assignments)
    plan = planner.build_plan()
    typer.echo(planner.plan_to_json(plan))


@app.command()
def build_patches(
    diff_file: Path = typer.Argument(..., help="Path to unified diff file"),
    plan_file: Path = typer.Argument(..., help="Path to plan JSON"),
    output_dir: Path = typer.Option("/tmp", "--output-dir", "-o", help="Directory to write patch files"),
) -> None:
    """Build patch files for each branch in the plan."""
    parsed = parse_diff(diff_file.read_text())
    plan = json.loads(plan_file.read_text())

    for branch in plan["branches"]:
        hunk_ids = {h["id"] for h in branch["hunks"]}
        patch = build_patch(parsed, hunk_ids)
        patch_path = output_dir / f"patch-{branch['topic_id']}.patch"
        patch_path.write_text(patch)
        lines = patch.count("\n")
        typer.echo(f"{branch['topic_id']}: {patch_path} ({lines} lines)")


@app.command()
def check_sizes(
    diff_file: Path = typer.Argument(..., help="Path to unified diff file"),
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
    threshold: int = typer.Argument(400, help="Max lines per split PR"),
) -> None:
    """Check topic sizes and report any that exceed the threshold."""
    parsed = parse_diff(diff_file.read_text())
    discovery = json.loads(discovery_file.read_text())
    dag = TopicDAG.from_dict(discovery["dag"])
    assignments = discovery["assignments"]

    planner = SplitPlanner(parsed, dag, size_threshold=threshold)
    planner.assign_hunks(assignments)
    oversized = planner.get_oversized_topics()

    if not oversized:
        typer.echo("All topics within threshold.")
        return

    for tid in oversized:
        size = planner.get_topic_size(tid)
        hunks = planner.get_topic_hunks(tid)
        files = len({h.file_path for h in hunks})
        typer.echo(f"{tid}: {size} lines, {len(hunks)} hunks, {files} files")


@app.command(name="validate-discovery")
def validate_discovery(
    hunks_file: Path = typer.Argument(..., help="Path to hunks JSON"),
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
) -> None:
    """Validate discovery output: check assignments, cycles, and topic stats."""
    hunks_data = json.loads(hunks_file.read_text())
    discovery = json.loads(discovery_file.read_text())

    # Collect all hunk IDs from the diff
    all_hunk_ids = set()
    hunk_file_map: dict[str, str] = {}
    for file_info in hunks_data["files"]:
        for h in file_info["hunks"]:
            all_hunk_ids.add(h["id"])
            hunk_file_map[h["id"]] = h["file_path"]

    assigned_ids = set(discovery["assignments"].keys())

    # Coverage check
    missing = all_hunk_ids - assigned_ids
    extra = assigned_ids - all_hunk_ids
    typer.echo(f"Total hunks in diff: {len(all_hunk_ids)}")
    typer.echo(f"Total assigned:      {len(assigned_ids)}")
    if missing:
        typer.echo(f"MISSING from assignments: {len(missing)}")
        for m in sorted(missing):
            typer.echo(f"  MISSING: {m} ({hunk_file_map.get(m, '?')})")
    if extra:
        typer.echo(f"EXTRA in assignments: {len(extra)}")
        for e in sorted(extra):
            typer.echo(f"  EXTRA: {e}")

    # DAG validation
    dag = TopicDAG.from_dict(discovery["dag"])
    typer.echo(f"\nTopics: {dag.topic_count}")
    typer.echo(f"Edges:  {len(discovery['dag']['edges'])}")

    # Cycle check (TopicDAG prevents cycles on construction, so if we got
    # here it's valid, but let's confirm)
    order = dag.topological_sort()
    typer.echo("DAG: No cycles found")

    # Independent groups
    groups = dag.independent_groups()
    if len(groups) > 1:
        typer.echo(f"Independent groups: {len(groups)}")

    # Per-topic stats
    typer.echo("\nTopic hunk counts:")
    assignments = discovery["assignments"]
    for tid in order:
        topic = dag.topics[tid]
        topic_hunks = [hid for hid, t in assignments.items() if t == tid]
        files = len({hunk_file_map[hid] for hid in topic_hunks if hid in hunk_file_map})
        deps = dag.get_dependencies(tid)
        dep_str = f" (depends on: {', '.join(deps)})" if deps else ""
        typer.echo(
            f"  {tid}: {len(topic_hunks)} hunks, ~{topic.estimated_size} lines, "
            f"{files} files{dep_str}"
        )

    # Summary
    if not missing and not extra:
        typer.echo("\nVALID: All hunks assigned, no cycles.")
    else:
        typer.echo("\nINVALID: See issues above.")
        raise typer.Exit(1)


@app.command()
def verify(
    diff_file: Path = typer.Argument(..., help="Path to original diff file"),
    plan_file: Path = typer.Argument(..., help="Path to plan JSON"),
) -> None:
    """Verify that all patches combined reproduce the original diff exactly.

    Concatenates all patch hunks in topological order and compares against
    the original diff. If they match, the split is lossless. This is a
    pure text check — no git operations needed.
    """
    original = parse_diff(diff_file.read_text())
    plan = json.loads(plan_file.read_text())

    # Collect all hunk IDs from all branches in the plan
    plan_hunk_ids: set[str] = set()
    for branch in plan["branches"]:
        for h in branch["hunks"]:
            plan_hunk_ids.add(h["id"])

    # Collect all hunk IDs from the original diff
    original_hunk_ids = {h.id for h in original.all_hunks}

    # Check for missing or extra hunks
    missing = original_hunk_ids - plan_hunk_ids
    extra = plan_hunk_ids - original_hunk_ids

    # Check for duplicates (hunk assigned to multiple branches)
    all_plan_ids: list[str] = []
    for branch in plan["branches"]:
        for h in branch["hunks"]:
            all_plan_ids.append(h["id"])
    duplicates = {hid for hid in all_plan_ids if all_plan_ids.count(hid) > 1}

    ok = True

    if missing:
        ok = False
        typer.echo(f"MISSING: {len(missing)} hunks in original but not in plan:")
        hunk_map = {h.id: h for h in original.all_hunks}
        for m in sorted(missing):
            h = hunk_map.get(m)
            path = h.file_path if h else "?"
            typer.echo(f"  {m} ({path})")

    if extra:
        ok = False
        typer.echo(f"EXTRA: {len(extra)} hunks in plan but not in original:")
        for e in sorted(extra):
            typer.echo(f"  {e}")

    if duplicates:
        ok = False
        typer.echo(f"DUPLICATED: {len(duplicates)} hunks appear in multiple branches:")
        for d in sorted(duplicates):
            branches = [b["topic_id"] for b in plan["branches"] if d in {h["id"] for h in b["hunks"]}]
            typer.echo(f"  {d} -> {', '.join(branches)}")

    # Verify combined patch reproduces original
    combined_patch = build_patch(original, plan_hunk_ids)
    original_patch = build_patch(original, original_hunk_ids)

    if combined_patch == original_patch:
        typer.echo(f"\nPatch content: MATCH")
    else:
        ok = False
        # Find which lines differ
        combined_lines = combined_patch.splitlines()
        original_lines = original_patch.splitlines()
        typer.echo(f"\nPatch content: MISMATCH")
        typer.echo(f"  Original patch: {len(original_lines)} lines")
        typer.echo(f"  Combined patch: {len(combined_lines)} lines")

    typer.echo(f"\nTotal hunks: {len(original_hunk_ids)} original, {len(plan_hunk_ids)} in plan")

    if ok:
        typer.echo("VERIFIED: Split is lossless — all patches combined reproduce the original diff.")
    else:
        typer.echo("FAILED: Split does not reproduce the original diff. See issues above.")
        raise typer.Exit(1)


@app.command()
def score(
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
    ground_truth_file: Path = typer.Argument(..., help="Path to ground truth JSON"),
    hunks_file: Path = typer.Argument(None, help="Path to hunks JSON (for file-path resolution)"),
) -> None:
    """Score a discovery result against ground truth. Outputs JSON."""
    discovery = json.loads(discovery_file.read_text())
    ground_truth = json.loads(ground_truth_file.read_text())
    hunks_data = json.loads(hunks_file.read_text()) if hunks_file else None

    gt_topics = ground_truth["topics"]
    gt_edges = ground_truth.get("expected_dag_edges", [])

    # Build hunk->file mapping
    hunk_to_file: dict[str, str] = {}
    if hunks_data:
        for fi in hunks_data["files"]:
            for h in fi["hunks"]:
                hunk_to_file[h["id"]] = h["file_path"]

    # Extract discovered topics as {id, files} from our native format
    disc_topics: list[dict[str, Any]] = []
    if "dag" in discovery and "assignments" in discovery:
        topic_files: dict[str, set[str]] = {}
        for hunk_id, topic_id in discovery["assignments"].items():
            topic_files.setdefault(topic_id, set())
            if hunk_id in hunk_to_file:
                topic_files[topic_id].add(hunk_to_file[hunk_id])
        for tid in discovery["dag"]["topics"]:
            disc_topics.append({"id": tid, "files": sorted(topic_files.get(tid, set()))})
    else:
        typer.echo("ERROR: Unsupported discovery format", err=True)
        raise typer.Exit(1)

    # Topic matching: for each GT topic, find best-matching discovered topic
    topic_scores = []
    for gt in gt_topics:
        gt_files = set(gt["files"])
        best = {"id": None, "recall": 0.0, "precision": 0.0}
        for disc in disc_topics:
            disc_files = set(disc["files"])
            overlap = gt_files & disc_files
            if not overlap:
                continue
            recall = len(overlap) / len(gt_files)
            precision = len(overlap) / len(disc_files)
            if recall > best["recall"] or (recall == best["recall"] and precision > best["precision"]):
                best = {"id": disc["id"], "recall": recall, "precision": precision}
        f1 = 2 * best["recall"] * best["precision"] / max(best["recall"] + best["precision"], 1e-9)
        topic_scores.append({
            "gt_topic": gt["id"], "match": best["id"],
            "recall": round(best["recall"], 3),
            "precision": round(best["precision"], 3),
            "f1": round(f1, 3),
        })

    # DAG similarity — translate discovered edges via topic matching
    # Build mapping: discovered topic ID → ground truth topic ID
    disc_to_gt: dict[str, str] = {}
    for ts in topic_scores:
        if ts["match"]:
            disc_to_gt[ts["match"]] = ts["gt_topic"]

    gt_edge_set = {(e[0], e[1]) for e in gt_edges}
    disc_edges_raw: set[tuple[str, str]] = set()
    if "dag" in discovery:
        for e in discovery["dag"].get("edges", []):
            disc_edges_raw.add((e["from"], e["to"]))

    # Translate discovered edges to ground truth namespace
    disc_edges: set[tuple[str, str]] = set()
    for src, dst in disc_edges_raw:
        mapped_src = disc_to_gt.get(src, src)
        mapped_dst = disc_to_gt.get(dst, dst)
        disc_edges.add((mapped_src, mapped_dst))

    tp = gt_edge_set & disc_edges
    dag_precision = len(tp) / max(len(disc_edges), 1)
    dag_recall = len(tp) / max(len(gt_edge_set), 1)
    dag_f1 = 2 * dag_precision * dag_recall / max(dag_precision + dag_recall, 1e-9)

    # Over/under splits
    over_splits = sum(
        1 for gt in gt_topics
        if sum(1 for d in disc_topics if set(gt["files"]) & set(d["files"])) > 1
    )
    under_splits = sum(
        1 for d in disc_topics
        if sum(1 for gt in gt_topics if set(gt["files"]) & set(d["files"])) > 1
    )

    # Aggregates
    avg_f1 = round(sum(t["f1"] for t in topic_scores) / max(len(topic_scores), 1), 3)
    avg_recall = round(sum(t["recall"] for t in topic_scores) / max(len(topic_scores), 1), 3)
    avg_precision = round(sum(t["precision"] for t in topic_scores) / max(len(topic_scores), 1), 3)

    split_penalty = (over_splits + under_splits) / max(len(gt_topics), 1)
    split_score = round(max(0.0, 1.0 - split_penalty), 3)
    composite = round(0.50 * avg_f1 + 0.25 * dag_f1 + 0.25 * split_score, 3)

    result = {
        "topic_f1": avg_f1,
        "topic_recall": avg_recall,
        "topic_precision": avg_precision,
        "dag_f1": round(dag_f1, 3),
        "split_score": split_score,
        "composite": composite,
        "topic_count_gt": len(gt_topics),
        "topic_count_discovered": len(disc_topics),
        "over_splits": over_splits,
        "under_splits": under_splits,
        "details": topic_scores,
    }

    typer.echo(json.dumps(result, indent=2))


@app.command()
def detect_validators() -> None:
    """Detect available validation tools in the current directory."""
    checks = {
        "python-project": Path("pyproject.toml").exists(),
        "node-project": Path("package.json").exists(),
        "pre-commit": Path(".pre-commit-config.yaml").exists(),
        "ruff": shutil.which("ruff") is not None,
        "tsc": shutil.which("tsc") is not None,
        "eslint": shutil.which("eslint") is not None,
        "pytest": shutil.which("pytest") is not None,
        "mypy": shutil.which("mypy") is not None,
    }
    for name, found in checks.items():
        if found:
            typer.echo(name)
