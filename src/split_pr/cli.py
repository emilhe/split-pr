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

import typer

from split_pr.diff_parser import build_patch, parse_diff
from split_pr.dag import TopicDAG
from split_pr.state import SplitPlanner

app = typer.Typer(help="Split-PR tools — called by the split-pr skill and agents.")


def _get_assignments(discovery: dict, hunks_data: dict | None = None) -> dict[str, str]:
    """Get hunk-to-topic assignments, deriving from hunk_ids if needed.

    The discovery agent may write assignments as either:
    - Top-level "assignments" dict: {"hunk_id": "topic_id", ...}
    - Per-topic "hunk_ids" lists in dag.topics

    If hunks_data is provided, resolves virtual hunk IDs (from tree-sitter
    analysis) back to their original raw diff hunk IDs via the
    original_hunk_id field.
    """
    if "assignments" in discovery and discovery["assignments"]:
        raw = discovery["assignments"]
    else:
        # Derive from hunk_ids in topics
        raw: dict[str, str] = {}
        if "dag" in discovery and "topics" in discovery["dag"]:
            for topic_id, topic in discovery["dag"]["topics"].items():
                for hunk_id in topic.get("hunk_ids", []):
                    raw[hunk_id] = topic_id

    if not hunks_data:
        return raw

    # Build virtual-to-raw ID mapping from the analyzed hunks
    virtual_to_raw: dict[str, str] = {}
    for file_info in hunks_data.get("files", []):
        for h in file_info.get("hunks", []):
            original = h.get("original_hunk_id")
            if original:
                virtual_to_raw[h["id"]] = original

    if not virtual_to_raw:
        return raw

    # Resolve virtual IDs to raw IDs (multiple virtual hunks may map
    # to the same raw hunk — deduplicate by keeping the first assignment)
    resolved: dict[str, str] = {}
    for hunk_id, topic_id in raw.items():
        raw_id = virtual_to_raw.get(hunk_id, hunk_id)
        if raw_id not in resolved:
            resolved[raw_id] = topic_id

    return resolved


@app.command(name="parse-diff")
def parse_diff_cmd(
    diff_file: Path = typer.Argument(..., help="Path to unified diff file"),
) -> None:
    """Parse a unified diff into structured hunk JSON."""
    parsed = parse_diff(diff_file.read_text())
    typer.echo(parsed.to_json())


@app.command()
def analyze(
    hunks_file: Path = typer.Argument(..., help="Path to hunks JSON from parse-diff"),
    source_dir: Path = typer.Argument(..., help="Path to the repo root (for reading source files)"),
    output_file: Path = typer.Argument(None, help="Output file (default: overwrite hunks file)"),
    min_split_size: int = typer.Option(100, "--min-split", help="Only split new files larger than this"),
    bulk: str = typer.Option("", "--bulk", help="Comma-separated path patterns to skip (vendored/bulk code)"),
) -> None:
    """Enrich hunks with AST analysis using tree-sitter.

    For each hunk, adds scope (what declaration it's inside), symbols_defined,
    and symbols_referenced. For large new-file hunks, splits them into
    per-declaration virtual hunks.

    Run after parse-diff, before discovery:
        parse-diff → analyze → stats → discovery
    """
    from split_pr.analyzer import enrich_hunks

    skip_patterns = tuple(p.strip() for p in bulk.split(",") if p.strip()) if bulk else ()

    hunks_data = json.loads(hunks_file.read_text())
    enriched = enrich_hunks(hunks_data, str(source_dir), skip_patterns=skip_patterns)

    out = output_file or hunks_file
    out.write_text(json.dumps(enriched, indent=2))

    # Report what changed
    original_count = json.loads(hunks_file.read_text())["hunk_count"] if output_file else hunks_data["hunk_count"]
    new_count = enriched["hunk_count"]
    virtual = sum(
        1 for f in enriched["files"] for h in f["hunks"]
        if isinstance(h, dict) and h.get("is_virtual")
    )
    typer.echo(f"Analyzed {enriched['file_count']} files, {new_count} hunks")
    if virtual:
        typer.echo(f"Split {virtual} virtual hunks from large new files")
    scoped = sum(
        1 for f in enriched["files"] for h in f["hunks"]
        if isinstance(h, dict) and h.get("scope")
    )
    if scoped:
        typer.echo(f"Added scope info to {scoped} hunks")


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
    hunk_ids: str = typer.Argument(None, help="Comma-separated hunk IDs to inspect"),
    file_path: str = typer.Option(None, "--file", "-f", help="Filter by file path (substring match)"),
    preview: int = typer.Option(0, "--preview", "-p", help="Show first N lines of each hunk's content"),
) -> None:
    """Show detailed info for specific hunks.

    Filter by hunk IDs (positional) or by file path (--file). Use --preview
    to see the first N lines of each hunk's diff content.
    """
    data = json.loads(hunks_file.read_text())
    ids_wanted = {h.strip() for h in hunk_ids.split(",")} if hunk_ids else None

    found = set()
    for file_info in data["files"]:
        for h in file_info["hunks"]:
            # Filter by IDs if provided
            if ids_wanted and h["id"] not in ids_wanted:
                continue
            # Filter by file path if provided
            if file_path and file_path not in h["file_path"]:
                continue
            # If no filters, skip (don't dump everything)
            if not ids_wanted and not file_path:
                typer.echo("ERROR: provide hunk IDs or --file filter", err=True)
                raise typer.Exit(1)

            found.add(h["id"])
            total = h["added_lines"] + h["removed_lines"]
            section = f" {h.get('section_header', '')}" if h.get("section_header") else ""
            typer.echo(
                f"{h['id']} {h['file_path']:50} "
                f"+{h['added_lines']}/-{h['removed_lines']} = {total}{section}"
            )
            if preview > 0 and h.get("content"):
                lines = h["content"].split("\n")[:preview]
                for line in lines:
                    typer.echo(f"  {line[:120]}")
                typer.echo()

    if ids_wanted:
        missing = ids_wanted - found
        if missing:
            for m in sorted(missing):
                typer.echo(f"{m} NOT FOUND")


@app.command(name="show-plan")
def show_plan(
    plan_file: Path = typer.Argument(..., help="Path to plan JSON"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show per-branch file lists and dependencies"),
    branch_id: str = typer.Option(None, "--branch", "-b", help="Show details for a single branch only"),
) -> None:
    """Summarize a split plan: branches, sizes, base branches, dependencies.

    Use --verbose for per-branch file lists. Use --branch to inspect one branch.
    """
    plan = json.loads(plan_file.read_text())

    # Build branch name → topic ID mapping for dependency display
    name_to_topic: dict[str, str] = {}
    for b in plan["branches"]:
        name_to_topic[b["branch_name"]] = b["topic_id"]

    if not branch_id:
        typer.echo(f"Base: {plan['original_base']}")
        typer.echo(f"Branches: {plan['branch_count']}")
        typer.echo(f"Total size: {plan['total_size']} lines")
        typer.echo(f"Unassigned hunks: {plan['unassigned_count']}")
        typer.echo()

    for i, b in enumerate(plan["branches"], 1):
        if branch_id and b["topic_id"] != branch_id:
            continue

        base_topic = name_to_topic.get(b["base_branch"], b["base_branch"])
        typer.echo(
            f"{i}. {b['topic_id']}  "
            f"{b['estimated_size']} lines, {b['hunk_count']} hunks  "
            f"depends_on={base_topic}"
        )
        typer.echo(f"   branch={b['branch_name']}")
        typer.echo(f"   title={b.get('pr_title', '')}")

        if verbose or branch_id:
            # Show files touched by this branch
            files: dict[str, int] = {}
            for h in b["hunks"]:
                fp = h.get("file_path", "?")
                files[fp] = files.get(fp, 0) + h.get("size", 0)
            typer.echo(f"   files ({len(files)}):")
            for fp, size in sorted(files.items()):
                typer.echo(f"     {fp} ({size} lines)")
            typer.echo()


@app.command()
def build_plan(
    diff_file: Path = typer.Argument(..., help="Path to unified diff file"),
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
    base: str = typer.Argument("main", help="Base branch name"),
    threshold: int = typer.Argument(400, help="Max lines per split PR"),
    hunks_file: Path = typer.Option(None, "--hunks", help="Analyzed hunks JSON (for virtual ID resolution)"),
) -> None:
    """Build a split plan from a diff and discovery output."""
    parsed = parse_diff(diff_file.read_text())
    discovery = json.loads(discovery_file.read_text())
    hunks_data = json.loads(hunks_file.read_text()) if hunks_file else None
    dag = TopicDAG.from_dict(discovery["dag"])

    planner = SplitPlanner(parsed, dag, base_branch=base, size_threshold=threshold)
    planner.assign_hunks(_get_assignments(discovery, hunks_data))
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


@app.command(name="create-branches")
def create_branches(
    diff_file: Path = typer.Argument(..., help="Path to unified diff file"),
    plan_file: Path = typer.Argument(..., help="Path to plan JSON"),
    repo_dir: Path = typer.Argument(..., help="Path to the git repository"),
    author: str = typer.Option(None, "--author", "-a", help="Git author (e.g., 'Name <email>')"),
    name_prefix: str = typer.Option("split", "--prefix", help="Branch name prefix"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done without doing it"),
) -> None:
    """Create all split branches, apply patches, and commit.

    Reads the plan, creates a branch for each topic in topological order,
    applies the corresponding patch, and commits. All in one command —
    one permission prompt instead of N*4.

    Uses --3way fallback if clean apply fails. Stops on unresolvable failures.
    """
    parsed = parse_diff(diff_file.read_text())
    plan = json.loads(plan_file.read_text())

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo_dir)] + list(args),
            capture_output=True, text=True,
        )

    results = []
    for i, branch in enumerate(plan["branches"], 1):
        topic_id = branch["topic_id"]
        branch_name = branch["branch_name"]
        base_branch = branch["base_branch"]
        title = branch.get("pr_title", topic_id)
        total = plan["branch_count"]

        typer.echo(f"\n[{i}/{total}] {topic_id}")

        if dry_run:
            typer.echo(f"  Would create {branch_name} from {base_branch}")
            typer.echo(f"  Would apply patch with {branch['hunk_count']} hunks ({branch['estimated_size']} lines)")
            results.append({"topic_id": topic_id, "status": "dry-run"})
            continue

        # Build the patch
        hunk_ids = {h["id"] for h in branch["hunks"]}
        patch = build_patch(parsed, hunk_ids)

        if not patch.strip():
            typer.echo(f"  WARNING: Empty patch for {topic_id}, skipping")
            results.append({"topic_id": topic_id, "status": "empty"})
            continue

        # Write patch to temp file
        patch_path = Path(f"/tmp/split-pr-patch-{topic_id}.patch")
        patch_path.write_text(patch)

        # Checkout base and create branch
        r = git("checkout", base_branch)
        if r.returncode != 0:
            typer.echo(f"  ERROR: checkout {base_branch} failed: {r.stderr.strip()}", err=True)
            raise typer.Exit(1)

        r = git("checkout", "-b", branch_name)
        if r.returncode != 0:
            typer.echo(f"  ERROR: create branch failed: {r.stderr.strip()}", err=True)
            raise typer.Exit(1)

        # Apply patch: try clean first, then --3way
        r = git("apply", "--check", str(patch_path))
        if r.returncode == 0:
            git("apply", str(patch_path))
            typer.echo(f"  Patch applied cleanly")
        else:
            r = git("apply", "--3way", str(patch_path))
            if r.returncode == 0:
                typer.echo(f"  Patch applied with --3way")
            else:
                # Try --reject as last resort
                r = git("apply", "--3way", "--reject", str(patch_path))
                rej = subprocess.run(
                    ["find", str(repo_dir), "-name", "*.rej"],
                    capture_output=True, text=True,
                )
                if rej.stdout.strip():
                    typer.echo(f"  FAILED: Unresolvable conflicts", err=True)
                    typer.echo(f"  Reject files: {rej.stdout.strip()}", err=True)
                    raise typer.Exit(1)
                else:
                    typer.echo(f"  Patch applied with --reject (no reject files)")

        # Stage and commit
        git("add", "-A")

        commit_args = ["-m", f"{title}\n\nPart of split from branch '{name_prefix}'."]
        if author:
            commit_args = [f"--author={author}"] + commit_args

        r = git("commit", *commit_args)
        if r.returncode != 0:
            if "nothing to commit" in r.stdout:
                typer.echo(f"  WARNING: Nothing to commit")
                results.append({"topic_id": topic_id, "status": "empty-commit"})
                continue
            typer.echo(f"  ERROR: commit failed: {r.stderr.strip()}", err=True)
            raise typer.Exit(1)

        # Get stats
        stat = git("diff", "--stat", "HEAD~1..HEAD")
        last_line = stat.stdout.strip().splitlines()[-1] if stat.stdout.strip() else ""
        typer.echo(f"  Committed: {last_line}")

        results.append({
            "topic_id": topic_id,
            "branch_name": branch_name,
            "status": "ok",
        })

    # Summary
    typer.echo(f"\n{'DRY RUN: ' if dry_run else ''}Created {len(results)} branches")
    ok = sum(1 for r in results if r["status"] == "ok")
    if ok < len(results):
        warnings = [r for r in results if r["status"] != "ok"]
        for w in warnings:
            typer.echo(f"  {w['topic_id']}: {w['status']}")


@app.command(name="verify-git")
def verify_git(
    plan_file: Path = typer.Argument(..., help="Path to plan JSON"),
    repo_dir: Path = typer.Argument(..., help="Path to the git repository"),
    original_branch: str = typer.Argument(..., help="Original branch to compare against"),
) -> None:
    """Verify the split branches reproduce the original branch exactly.

    Checks out the last branch in the chain and diffs it against the
    original branch. If the diff is empty, the split is perfect.
    Run after create-branches, before push-branches.
    """
    plan = json.loads(plan_file.read_text())
    branches = plan["branches"]

    if not branches:
        typer.echo("ERROR: No branches in plan", err=True)
        raise typer.Exit(1)

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo_dir)] + list(args),
            capture_output=True, text=True,
        )

    # The last branch in the chain should contain all changes
    last_branch = branches[-1]["branch_name"]

    typer.echo(f"Comparing {last_branch} against {original_branch}...")

    # Diff the last split branch against the original
    r = git("diff", f"{last_branch}..{original_branch}")

    if r.returncode != 0:
        typer.echo(f"ERROR: git diff failed: {r.stderr.strip()}", err=True)
        raise typer.Exit(1)

    diff_output = r.stdout.strip()

    if not diff_output:
        typer.echo("VERIFIED: Split branches reproduce the original branch exactly.")
    else:
        # Count what's different
        diff_lines = diff_output.splitlines()
        added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

        # Show which files differ
        r_stat = git("diff", "--stat", f"{last_branch}..{original_branch}")

        typer.echo("MISMATCH: Split branches do not reproduce the original branch.")
        typer.echo(f"  Diff: +{added}/-{removed} lines")
        if r_stat.stdout.strip():
            typer.echo(f"  Files:\n{r_stat.stdout.strip()}")
        typer.echo("\nThis means some changes were lost or altered during splitting.")
        raise typer.Exit(1)


@app.command(name="push-branches")
def push_branches(
    plan_file: Path = typer.Argument(..., help="Path to plan JSON"),
    repo_dir: Path = typer.Argument(..., help="Path to the git repository"),
) -> None:
    """Push all split branches to the remote. One command, one prompt."""
    plan = json.loads(plan_file.read_text())

    for i, branch in enumerate(plan["branches"], 1):
        branch_name = branch["branch_name"]
        typer.echo(f"[{i}/{plan['branch_count']}] Pushing {branch_name}...")
        r = subprocess.run(
            ["git", "-C", str(repo_dir), "push", "-u", "origin", branch_name],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            typer.echo(f"  ERROR: {r.stderr.strip()}", err=True)
            raise typer.Exit(1)

    typer.echo(f"\nPushed {plan['branch_count']} branches")


@app.command(name="create-prs")
def create_prs(
    plan_file: Path = typer.Argument(..., help="Path to plan JSON"),
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
    repo: str = typer.Argument(..., help="GitHub repo (owner/name)"),
    name: str = typer.Option("split", "--name", "-n", help="PR title prefix"),
    original_branch: str = typer.Option("", "--branch", "-b", help="Original branch name for descriptions"),
    original_pr_url: str = typer.Option("", "--original-pr", help="URL of the original PR (for linking)"),
    tracking_issue_url: str = typer.Option("", "--tracking-issue", help="URL of the tracking issue"),
    links_out: Path = typer.Option(None, "--links-out", help="Write topic->PR URL mapping to this file"),
) -> None:
    """Create all PRs from split branches. One command, one prompt.

    Creates PRs in topological order with DAG diagrams (highlighted per-PR).
    After all PRs are created, updates descriptions with cross-references
    and writes a links.json for clickable DAGs.
    """
    plan = json.loads(plan_file.read_text())
    discovery = json.loads(discovery_file.read_text())
    total = plan["branch_count"]

    # Phase 1: Create PRs and collect URLs
    pr_map: dict[str, dict] = {}  # topic_id -> {number, url}
    for i, branch in enumerate(plan["branches"], 1):
        topic_id = branch["topic_id"]
        branch_name = branch["branch_name"]
        base_branch = branch["base_branch"]
        pr_title = branch.get("pr_title", topic_id)
        pr_body = branch.get("pr_body", "") or pr_title
        title = f"[{name} {i}/{total}] {pr_title}"

        # Build file list for the summary
        files_in_branch: dict[str, int] = {}
        for h in branch["hunks"]:
            fp = h.get("file_path", "?")
            files_in_branch[fp] = files_in_branch.get(fp, 0) + h.get("size", 0)
        file_list = "\n".join(f"- `{fp}` ({sz} lines)" for fp, sz in sorted(files_in_branch.items())[:15])
        if len(files_in_branch) > 15:
            file_list += f"\n- ... and {len(files_in_branch) - 15} more files"

        body = f"## Summary\n\n{pr_body}\n\n**Files ({len(files_in_branch)}):**\n{file_list}\n\n---\nGenerated by split-pr"

        typer.echo(f"[{i}/{total}] Creating PR: {title}")

        r = subprocess.run(
            ["gh", "pr", "create",
             "--repo", repo,
             "--base", base_branch,
             "--head", branch_name,
             "--title", title,
             "--body", body],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            typer.echo(f"  ERROR: {r.stderr.strip()}", err=True)
            raise typer.Exit(1)

        # gh pr create outputs the PR URL
        pr_url = r.stdout.strip()
        # Extract PR number from URL
        pr_number = pr_url.rstrip("/").split("/")[-1] if pr_url else "?"
        pr_map[topic_id] = {"number": pr_number, "url": pr_url}
        typer.echo(f"  Created: {pr_url}")

    # Build links file
    links = {tid: info["url"] for tid, info in pr_map.items()}
    if links_out:
        links_out.write_text(json.dumps(links, indent=2))
        typer.echo(f"\nLinks written to {links_out}")

    # Build ordered list for prev/next links
    ordered_topics = [b["topic_id"] for b in plan["branches"]]

    # Phase 2: Update PR descriptions with DAG diagrams and navigation
    typer.echo(f"\nUpdating PR descriptions with DAG diagrams...")
    for i, branch in enumerate(plan["branches"], 1):
        topic_id = branch["topic_id"]
        info = pr_map[topic_id]

        # Build highlighted DAG with links
        dag_lines = ["```mermaid", "graph LR"]
        for tid, tdata in discovery["dag"]["topics"].items():
            tname = tdata.get("name", tid)
            size = tdata.get("estimated_size", 0)
            label = f"{tname}<br/>{size} lines"
            if tid == topic_id:
                dag_lines.append(f'    {tid}["{label}"]:::current')
            else:
                dag_lines.append(f'    {tid}["{label}"]')
        for e in discovery["dag"].get("edges", []):
            dag_lines.append(f"    {e['from']} --> {e['to']}")
        dag_lines.append("")
        for tid, url in links.items():
            if tid in discovery["dag"]["topics"]:
                dag_lines.append(f'    click {tid} href "{url}" _blank')
        dag_lines.append("")
        dag_lines.append("    classDef current fill:#4CAF50,stroke:#333,color:#fff,stroke-width:3px")
        dag_lines.append("```")
        dag = "\n".join(dag_lines)

        # Build navigation line
        split_ref = f"[{original_branch} split]({original_pr_url})" if original_pr_url else f"`{original_branch}` split"

        if i == 1:
            base_text = "Base: `main`"
        else:
            prev_tid = ordered_topics[i - 2]
            prev_info = pr_map.get(prev_tid, {})
            prev_title = next((b.get("pr_title", prev_tid) for b in plan["branches"] if b["topic_id"] == prev_tid), prev_tid)
            prev_url = prev_info.get("url", "")
            base_text = f"Base: [PR {i-1} — {prev_title}]({prev_url})" if prev_url else f"Base: PR {i-1}"

        if i < total:
            next_tid = ordered_topics[i]
            next_info = pr_map.get(next_tid, {})
            next_title = next((b.get("pr_title", next_tid) for b in plan["branches"] if b["topic_id"] == next_tid), next_tid)
            next_url = next_info.get("url", "")
            next_text = f"Next: [PR {i+1} — {next_title}]({next_url})" if next_url else f"Next: PR {i+1}"
        else:
            next_text = "This is the last PR in the chain."

        tracking_ref = f" [Tracking issue]({tracking_issue_url})." if tracking_issue_url else ""

        pr_body_text = branch.get("pr_body", "") or branch.get("pr_title", topic_id)

        # Build file list — prefer annotated key_files from discovery
        topic_data = discovery["dag"]["topics"].get(topic_id, {})
        key_files = topic_data.get("key_files", [])

        if key_files:
            file_list = "\n".join(f"- `{kf['path']}` — {kf.get('note', '')}" for kf in key_files)
        else:
            # Fallback: auto-generate from hunks
            branch_files: dict[str, int] = {}
            for h in branch["hunks"]:
                fp = h.get("file_path", "?")
                branch_files[fp] = branch_files.get(fp, 0) + h.get("size", 0)
            file_list = "\n".join(f"- `{fp}` ({sz} lines)" for fp, sz in sorted(branch_files.items())[:15])
            if len(branch_files) > 15:
                file_list += f"\n- ... and {len(branch_files) - 15} more files"

        body = f"""## Summary

{pr_body_text}

### Key files
{file_list}

## Context

PR {i} of {total} in the {split_ref}. {base_text}. {next_text}{tracking_ref}

{dag}

---
Generated by split-pr"""

        r = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{info['number']}",
             "-X", "PATCH", "-f", f"body={body}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            typer.echo(f"  WARNING: Failed to update #{info['number']}: {r.stderr.strip()}")

    typer.echo(f"\nDone! {total} PRs created and updated with DAG diagrams")


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

    planner = SplitPlanner(parsed, dag, size_threshold=threshold)
    planner.assign_hunks(_get_assignments(discovery))
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
    fix: bool = typer.Option(False, "--fix", help="Fix discovery: compute sizes, build assignments, write back"),
) -> None:
    """Validate discovery output: check assignments, cycles, and topic stats.

    With --fix: computes estimated_size per topic from actual hunk data,
    builds the assignments dict from hunk_ids, and writes discovery back.
    """
    hunks_data = json.loads(hunks_file.read_text())
    discovery = json.loads(discovery_file.read_text())

    # Collect all hunk IDs and sizes from the diff
    all_hunk_ids = set()
    hunk_file_map: dict[str, str] = {}
    hunk_sizes: dict[str, int] = {}
    for file_info in hunks_data["files"]:
        for h in file_info["hunks"]:
            all_hunk_ids.add(h["id"])
            hunk_file_map[h["id"]] = h.get("file_path", file_info.get("path", "?"))
            hunk_sizes[h["id"]] = h.get("added_lines", 0) + h.get("removed_lines", 0)

    if fix:
        # Build assignments directly from hunk_ids (no virtual→raw resolution)
        # This keeps assignments consistent with hunk_ids in topics.
        # Virtual→raw resolution happens later in build-plan.
        fixed_assignments: dict[str, str] = {}
        if "dag" in discovery and "topics" in discovery["dag"]:
            for tid, topic in discovery["dag"]["topics"].items():
                for hid in topic.get("hunk_ids", []):
                    fixed_assignments[hid] = tid
                # Compute estimated_size from actual hunk data
                topic["estimated_size"] = sum(
                    hunk_sizes.get(h, 0) for h in topic.get("hunk_ids", [])
                )
        discovery["assignments"] = fixed_assignments
        discovery_file.write_text(json.dumps(discovery, indent=2))
        typer.echo(f"Fixed: {len(fixed_assignments)} assignments, sizes computed")
        assignments = fixed_assignments
    else:
        assignments = _get_assignments(discovery, hunks_data)

    assigned_ids = set(assignments.keys())

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
    for tid in order:
        topic_hunks = [hid for hid, t in assignments.items() if t == tid]
        size = sum(hunk_sizes.get(h, 0) for h in topic_hunks)
        files = len({hunk_file_map[hid] for hid in topic_hunks if hid in hunk_file_map})
        deps = dag.get_dependencies(tid)
        dep_str = f" (depends on: {', '.join(deps)})" if deps else ""
        typer.echo(
            f"  {tid}: {len(topic_hunks)} hunks, {size} lines, "
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


@app.command(name="render-dag")
def render_dag(
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
    highlight: str = typer.Option(None, "--highlight", "-h", help="Topic ID to highlight (for per-PR views)"),
    links_file: Path = typer.Option(None, "--links", "-l", help="JSON file mapping topic IDs to PR URLs"),
) -> None:
    """Render the topic DAG as a Mermaid diagram.

    Outputs a Mermaid graph definition that GitHub renders natively
    in markdown. Use --highlight to mark a specific topic. Use --links
    to make nodes clickable (linking to their PRs).
    """
    discovery = json.loads(discovery_file.read_text())
    links: dict[str, str] = json.loads(links_file.read_text()) if links_file else {}

    if "dag" not in discovery:
        typer.echo("ERROR: No DAG found in discovery", err=True)
        raise typer.Exit(1)

    dag_data = discovery["dag"]
    topics = dag_data["topics"]
    edges = dag_data.get("edges", [])

    lines = ["```mermaid", "graph LR"]

    # Node definitions with labels
    for tid, tdata in topics.items():
        name = tdata.get("name", tid)
        size = tdata.get("estimated_size", 0)
        label = f"{name}<br/>{size} lines"
        if highlight and tid == highlight:
            lines.append(f'    {tid}["{label}"]:::current')
        else:
            lines.append(f'    {tid}["{label}"]')

    # Edges
    for e in edges:
        lines.append(f"    {e['from']} --> {e['to']}")

    # Click links to PRs
    if links:
        lines.append("")
        for tid, url in links.items():
            if tid in topics:
                lines.append(f'    click {tid} href "{url}" _blank')

    # Styles
    lines.append("")
    lines.append("    classDef current fill:#4CAF50,stroke:#333,color:#fff,stroke-width:3px")

    lines.append("```")

    typer.echo("\n".join(lines))


@app.command(name="render-dag-full")
def render_dag_full(
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
    plan_file: Path = typer.Argument(None, help="Path to plan JSON (adds PR numbers/branch info)"),
    links_file: Path = typer.Option(None, "--links", "-l", help="JSON file mapping topic IDs to PR URLs"),
) -> None:
    """Render the full DAG for the tracking issue.

    Includes topic sizes, PR numbers (if plan provided), parallel
    group annotations, and clickable links to PRs (if --links provided).
    """
    discovery = json.loads(discovery_file.read_text())
    plan = json.loads(plan_file.read_text()) if plan_file else None
    links: dict[str, str] = json.loads(links_file.read_text()) if links_file else {}

    dag_data = discovery["dag"]
    topics = dag_data["topics"]
    edges = dag_data.get("edges", [])

    # Build topic-to-branch mapping from plan
    topic_info: dict[str, dict] = {}
    if plan:
        for i, b in enumerate(plan["branches"], 1):
            topic_info[b["topic_id"]] = {
                "index": i,
                "total": plan["branch_count"],
                "size": b["estimated_size"],
                "base": b["base_branch"],
            }

    lines = ["```mermaid", "graph LR"]

    # Node definitions
    for tid, tdata in topics.items():
        name = tdata.get("name", tid)
        size = tdata.get("estimated_size", 0)
        info = topic_info.get(tid)
        if info:
            label = f"#{info['index']}/{info['total']} {name}<br/>{size} lines"
        else:
            label = f"{name}<br/>{size} lines"

        lines.append(f'    {tid}["{label}"]')

    # Edges
    for e in edges:
        lines.append(f"    {e['from']} --> {e['to']}")

    # Find independent groups (nodes with no edges between them)
    # by checking weakly connected components
    dag = TopicDAG.from_dict(dag_data)
    groups = dag.independent_groups()
    if len(groups) > 1:
        lines.append("")
        for i, group in enumerate(groups):
            if len(group) > 1:
                lines.append(f"    subgraph group{i}[Parallel group {i+1}]")
                for tid in sorted(group):
                    lines.append(f"        {tid}")
                lines.append("    end")

    # Click links to PRs
    if links:
        lines.append("")
        for tid, url in links.items():
            if tid in topics:
                lines.append(f'    click {tid} href "{url}" _blank')

    lines.append("```")

    typer.echo("\n".join(lines))


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
