#!/usr/bin/env python3
"""Evaluate split-pr discovery output against synthetic ground truth.

Usage:
    python3 evaluate.py discovery.json [ground_truth.json]

    discovery.json  — output from split-pr's discovery phase
    ground_truth.json — ground truth (default: synthetic_ground_truth.json
                        in the same directory as this script)

The script computes:
    1. Topic recall: for each ground truth topic, what fraction of its
       expected files ended up together in a single discovered topic?
    2. Topic precision: how much noise (unexpected files) does each
       matched discovered topic contain?
    3. DAG similarity: did we get dependency edges right?
    4. Over-split penalty: did we incorrectly split a single logical
       change into multiple topics?
    5. Under-split penalty: did we merge distinct logical changes into
       one topic?
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def load_json(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# File-to-topic matching
# ---------------------------------------------------------------------------

def build_file_to_topics(ground_truth: dict) -> dict[str, list[str]]:
    """Map each file to the ground-truth topic(s) that claim it."""
    mapping: dict[str, list[str]] = {}
    for topic in ground_truth["topics"]:
        for f in topic["files"]:
            mapping.setdefault(f, []).append(topic["id"])
    return mapping


def build_discovered_topics(discovery: dict, hunks_data: dict | None = None) -> list[dict]:
    """Normalize discovery.json into a list of {id, files} dicts.

    Supports multiple formats:
      - split-pr format: {"dag": {"topics": {...}}, "assignments": {...}}
      - flat format: {"topics": [{"id": ..., "files": [...]}]}
      - grouped format: {"groups": [{"name": ..., "files": [...]}]}
    """
    topics = []

    # split-pr native format: dag + assignments
    if "dag" in discovery and "assignments" in discovery:
        dag_topics = discovery["dag"]["topics"]
        assignments = discovery["assignments"]

        # Build hunk_id -> file_path mapping from hunks data
        hunk_to_file: dict[str, str] = {}
        if hunks_data:
            for file_info in hunks_data["files"]:
                for h in file_info["hunks"]:
                    hunk_to_file[h["id"]] = normalize_path(h["file_path"])

        # Group files by topic using assignments
        topic_files: dict[str, set[str]] = {}
        for hunk_id, topic_id in assignments.items():
            topic_files.setdefault(topic_id, set())
            if hunk_id in hunk_to_file:
                topic_files[topic_id].add(hunk_to_file[hunk_id])

        for tid, tdata in dag_topics.items():
            files = sorted(topic_files.get(tid, set()))
            # Also check hunk_ids in topic data
            if not files and "hunk_ids" in tdata:
                files = sorted({hunk_to_file.get(h, "") for h in tdata["hunk_ids"]} - {""})
            topics.append({"id": tid, "files": files})

        return topics

    # Flat/grouped formats
    if "topics" in discovery:
        raw = discovery["topics"]
        if isinstance(raw, dict):
            raw = list(raw.values())
    elif "groups" in discovery:
        raw = discovery["groups"]
    else:
        print("WARNING: discovery.json has unrecognized format", file=sys.stderr)
        return []

    for i, item in enumerate(raw):
        tid = item.get("id") or item.get("name") or f"topic-{i}"
        files: list[str] = []
        if "files" in item:
            files = [normalize_path(f) for f in item["files"]]
        elif "hunks" in item:
            seen = set()
            for hunk in item["hunks"]:
                p = normalize_path(hunk.get("path", ""))
                if p and p not in seen:
                    files.append(p)
                    seen.add(p)
        topics.append({"id": tid, "files": files})

    return topics


def normalize_path(path: str) -> str:
    """Strip leading ./ or / from paths for consistent comparison."""
    p = path.strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------

def compute_topic_overlap(
    gt_topics: list[dict], discovered: list[dict]
) -> list[dict]:
    """For each GT topic, find the best-matching discovered topic.

    Returns a list of per-GT-topic scores with:
      - recall: fraction of GT files found in the best matching discovered topic
      - precision: fraction of matched discovered topic files that are in this GT topic
      - best_match: ID of the best matching discovered topic
    """
    results = []

    for gt in gt_topics:
        gt_files = set(gt["files"])
        best_match_id = None
        best_recall = 0.0
        best_precision = 0.0
        best_overlap = 0

        for disc in discovered:
            disc_files = set(disc["files"])
            overlap = gt_files & disc_files
            if not overlap:
                continue

            recall = len(overlap) / len(gt_files)
            precision = len(overlap) / len(disc_files) if disc_files else 0.0

            # Pick the discovered topic with highest recall; break ties by precision
            if recall > best_recall or (recall == best_recall and precision > best_precision):
                best_recall = recall
                best_precision = precision
                best_match_id = disc["id"]
                best_overlap = len(overlap)

        results.append({
            "gt_topic": gt["id"],
            "gt_file_count": len(gt_files),
            "best_match": best_match_id,
            "overlap_count": best_overlap,
            "recall": round(best_recall, 3),
            "precision": round(best_precision, 3),
            "f1": round(
                2 * best_recall * best_precision / max(best_recall + best_precision, 1e-9),
                3,
            ),
        })

    return results


def compute_dag_similarity(
    gt_edges: list[list[str]], discovered: dict
) -> dict:
    """Compare ground truth DAG edges with discovered dependencies.

    Returns edge-level precision, recall, and F1.
    """
    gt_edge_set = {(e[0], e[1]) for e in gt_edges}

    # Extract discovered edges from various formats
    disc_edges: set[tuple[str, str]] = set()
    if "dag" in discovered and "edges" in discovered["dag"]:
        # split-pr native format
        for e in discovered["dag"]["edges"]:
            disc_edges.add((e["from"], e["to"]))
    elif "expected_dag_edges" in discovered:
        for e in discovered["expected_dag_edges"]:
            disc_edges.add((e[0], e[1]))
    elif "edges" in discovered:
        for e in discovered["edges"]:
            src = e.get("from") or e.get("source") or e[0]
            dst = e.get("to") or e.get("target") or e[1]
            disc_edges.add((src, dst))
    elif "topics" in discovered:
        # Try to extract depends_on from topics
        raw = discovered["topics"]
        if isinstance(raw, dict):
            raw = list(raw.values())
        for topic in raw:
            tid = topic.get("id") or topic.get("name", "")
            deps = topic.get("depends_on", [])
            for dep in deps:
                disc_edges.add((dep, tid))

    if not gt_edge_set:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0, "gt_edges": 0, "disc_edges": 0}

    true_positives = gt_edge_set & disc_edges
    precision = len(true_positives) / max(len(disc_edges), 1)
    recall = len(true_positives) / max(len(gt_edge_set), 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)

    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "gt_edges": len(gt_edge_set),
        "disc_edges": len(disc_edges),
        "true_positives": len(true_positives),
        "missed_edges": sorted(gt_edge_set - disc_edges),
        "extra_edges": sorted(disc_edges - gt_edge_set),
    }


def compute_split_quality(
    gt_topics: list[dict], discovered: list[dict]
) -> dict:
    """Detect over-splitting and under-splitting.

    Over-split: a GT topic's files are scattered across multiple discovered topics.
    Under-split: multiple GT topics' files are merged into one discovered topic.
    """
    over_splits = []
    under_splits = []

    # Check over-splitting: GT topic files in multiple discovered topics
    for gt in gt_topics:
        gt_files = set(gt["files"])
        matching_disc = []
        for disc in discovered:
            disc_files = set(disc["files"])
            if gt_files & disc_files:
                matching_disc.append(disc["id"])
        if len(matching_disc) > 1:
            over_splits.append({
                "gt_topic": gt["id"],
                "split_across": matching_disc,
                "count": len(matching_disc),
            })

    # Check under-splitting: discovered topic contains files from multiple GT topics
    for disc in discovered:
        disc_files = set(disc["files"])
        matching_gt = []
        for gt in gt_topics:
            gt_files = set(gt["files"])
            if gt_files & disc_files:
                matching_gt.append(gt["id"])
        if len(matching_gt) > 1:
            under_splits.append({
                "disc_topic": disc["id"],
                "merged_gt_topics": matching_gt,
                "count": len(matching_gt),
            })

    return {
        "over_split_count": len(over_splits),
        "under_split_count": len(under_splits),
        "over_splits": over_splits,
        "under_splits": under_splits,
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def compute_summary(
    topic_scores: list[dict],
    dag_score: dict,
    split_quality: dict,
) -> dict:
    """Compute aggregate scores."""
    recalls = [t["recall"] for t in topic_scores]
    precisions = [t["precision"] for t in topic_scores]
    f1s = [t["f1"] for t in topic_scores]
    matched = sum(1 for t in topic_scores if t["best_match"] is not None)

    return {
        "topic_count_gt": len(topic_scores),
        "topic_count_matched": matched,
        "topic_count_unmatched": len(topic_scores) - matched,
        "avg_recall": round(sum(recalls) / max(len(recalls), 1), 3),
        "avg_precision": round(sum(precisions) / max(len(precisions), 1), 3),
        "avg_f1": round(sum(f1s) / max(len(f1s), 1), 3),
        "dag_f1": dag_score["f1"],
        "over_split_count": split_quality["over_split_count"],
        "under_split_count": split_quality["under_split_count"],
    }


def print_report(
    topic_scores: list[dict],
    dag_score: dict,
    split_quality: dict,
    summary: dict,
) -> None:
    """Print a human-readable evaluation report."""
    print("=" * 72)
    print("  SPLIT-PR EVALUATION REPORT")
    print("=" * 72)

    print("\n--- Topic Matching ---\n")
    print(f"  {'GT Topic':<30} {'Match':<25} {'Recall':>7} {'Prec':>7} {'F1':>7}")
    print(f"  {'-'*30} {'-'*25} {'-'*7} {'-'*7} {'-'*7}")
    for t in topic_scores:
        match_str = t["best_match"] or "(none)"
        print(
            f"  {t['gt_topic']:<30} {match_str:<25} "
            f"{t['recall']:>7.1%} {t['precision']:>7.1%} {t['f1']:>7.1%}"
        )

    print(f"\n  Average recall:    {summary['avg_recall']:.1%}")
    print(f"  Average precision: {summary['avg_precision']:.1%}")
    print(f"  Average F1:        {summary['avg_f1']:.1%}")
    print(f"  Matched/Total:     {summary['topic_count_matched']}/{summary['topic_count_gt']}")

    print("\n--- DAG Similarity ---\n")
    print(f"  GT edges:        {dag_score['gt_edges']}")
    print(f"  Discovered edges: {dag_score['disc_edges']}")
    print(f"  True positives:   {dag_score.get('true_positives', 'N/A')}")
    print(f"  DAG F1:           {dag_score['f1']:.1%}")
    if dag_score.get("missed_edges"):
        print(f"  Missed edges:     {dag_score['missed_edges']}")
    if dag_score.get("extra_edges"):
        print(f"  Extra edges:      {dag_score['extra_edges']}")

    print("\n--- Split Quality ---\n")
    print(f"  Over-splits:  {split_quality['over_split_count']}")
    for os_item in split_quality["over_splits"]:
        print(f"    - {os_item['gt_topic']} split across: {os_item['split_across']}")
    print(f"  Under-splits: {split_quality['under_split_count']}")
    for us_item in split_quality["under_splits"]:
        print(
            f"    - {us_item['disc_topic']} merges: {us_item['merged_gt_topics']}"
        )

    print("\n--- Overall Score ---\n")
    # Weighted composite: 50% topic F1, 25% DAG F1, 25% split penalty
    split_penalty = (
        split_quality["over_split_count"] + split_quality["under_split_count"]
    )
    max_penalty = summary["topic_count_gt"]  # normalize
    split_score = max(0, 1.0 - split_penalty / max(max_penalty, 1))
    composite = 0.50 * summary["avg_f1"] + 0.25 * dag_score["f1"] + 0.25 * split_score
    print(f"  Composite score: {composite:.1%}")
    print(f"    (50% topic F1 + 25% DAG F1 + 25% split quality)")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} discovery.json ground_truth.json [hunks.json]", file=sys.stderr)
        print(f"  discovery.json   — output from split-pr's discovery phase", file=sys.stderr)
        print(f"  ground_truth.json — ground truth file", file=sys.stderr)
        print(f"  hunks.json       — (optional) parsed hunks for file-path resolution", file=sys.stderr)
        sys.exit(1)

    discovery_path = sys.argv[1]
    gt_path = sys.argv[2]
    hunks_path = sys.argv[3] if len(sys.argv) > 3 else None

    discovery = load_json(discovery_path)
    ground_truth = load_json(gt_path)
    hunks_data = load_json(hunks_path) if hunks_path else None

    gt_topics = ground_truth["topics"]
    gt_edges = ground_truth.get("expected_dag_edges", [])

    discovered = build_discovered_topics(discovery, hunks_data)

    if not discovered:
        print("ERROR: No topics found in discovery.json", file=sys.stderr)
        sys.exit(1)

    # Compute scores
    topic_scores = compute_topic_overlap(gt_topics, discovered)
    dag_score = compute_dag_similarity(gt_edges, discovery)
    split_quality = compute_split_quality(gt_topics, discovered)
    summary = compute_summary(topic_scores, dag_score, split_quality)

    # Print report
    print_report(topic_scores, dag_score, split_quality, summary)

    # Also write machine-readable results
    results = {
        "topic_scores": topic_scores,
        "dag_score": dag_score,
        "split_quality": split_quality,
        "summary": summary,
    }
    results_path = Path(discovery_path).with_suffix(".eval.json")
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Machine-readable results written to: {results_path}")


if __name__ == "__main__":
    main()
