"""CLI entry point for split-pr tools.

Provides subcommands that the skill and agents call. Each subcommand is a thin
wrapper around the library functions, designed to be invoked as:

    uv run --project ~/projects/split-pr split-pr-tools <command> <args>

This makes all invocations match a single whitelistable pattern.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import typer

from split_pr.diff_parser import build_patch, parse_diff
from split_pr.dag import TopicDAG
from split_pr.state import SplitPlanner

app = typer.Typer(help="Split-PR tools — called by the split-pr skill and agents.")


@app.callback()
def _audit_log(ctx: typer.Context) -> None:
    """Log every CLI invocation to the run directory's audit log."""
    # Find a /tmp/split-pr-* path in the arguments to determine the run dir
    run_dir = None
    for arg in sys.argv[1:]:
        if "/tmp/split-pr-" in arg:
            # Extract the run directory from the path
            parts = arg.split("/")
            for i, p in enumerate(parts):
                if p.startswith("split-pr-") and i > 0:
                    run_dir = "/".join(parts[: i + 1])
                    break
            if run_dir:
                break

    if run_dir:
        log_path = Path(run_dir) / "cli-audit.log"
        try:
            with open(log_path, "a") as f:
                ts = datetime.now().strftime("%H:%M:%S")
                cmd = " ".join(sys.argv[1:])
                f.write(f"{ts}  {cmd}\n")
        except OSError:
            pass  # Don't fail if we can't write the log


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


@app.command(name="bundle-context")
def bundle_context(
    hunks_file: Path = typer.Argument(..., help="Path to hunks JSON"),
    source_dir: Path = typer.Argument(..., help="Path to the repo root"),
    output_file: Path = typer.Argument(None, help="Output file (default: stdout)"),
    max_lines: int = typer.Option(200, "--max-lines", help="Max lines per file (0 = unlimited)"),
    skip: str = typer.Option("", "--skip", help="Comma-separated path patterns to exclude (e.g., vendored code)"),
) -> None:
    """Bundle source files for all changed files into one readable file.

    Produces a single file with all source files concatenated, clearly
    delimited. The discovery agent reads this once instead of making
    50+ individual file reads.

    Only includes files that have hunks in the diff. For large files,
    truncates to --max-lines (showing the start, which has imports and
    key declarations).
    """
    data = json.loads(hunks_file.read_text())
    parts: list[str] = []
    file_count = 0

    skip_patterns = tuple(p.strip() for p in skip.split(",") if p.strip()) if skip else ()

    for file_info in data["files"]:
        path = file_info.get("path", "")

        if skip_patterns and any(p in path for p in skip_patterns):
            continue

        full_path = source_dir / path
        if not full_path.exists():
            continue

        try:
            content = full_path.read_text()
        except Exception:
            parts.append(f"=== {path} === (read error)\n")
            continue

        lines = content.splitlines()
        total = len(lines)
        is_new = file_info.get("is_new", False)
        hunk_count = len(file_info.get("hunks", []))

        hunk_ids = [h["id"] for h in file_info.get("hunks", [])]
        hunk_attr = " ".join(hunk_ids[:5])
        if len(hunk_ids) > 5:
            hunk_attr += f" (+{len(hunk_ids)-5} more)"

        attrs = f'path="{path}" lines="{total}" hunks="{hunk_count}" hunk_ids="{hunk_attr}"'
        if is_new:
            attrs += ' status="NEW"'

        if max_lines > 0 and total > max_lines:
            truncated = lines[:max_lines]
            parts.append(f"<file {attrs}>")
            parts.append("\n".join(truncated))
            parts.append(f"... ({total - max_lines} more lines truncated)")
            parts.append("</file>")
        else:
            parts.append(f"<file {attrs}>")
            parts.append(content.rstrip())
            parts.append("</file>")
        file_count += 1

    result = "\n".join(parts)

    if output_file:
        output_file.write_text(result)
        typer.echo(f"Bundled {file_count} files to {output_file}")
    else:
        typer.echo(result)


@app.command()
def stats(
    hunks_file: Path = typer.Argument(..., help="Path to hunks JSON from parse-diff"),
    sort: str = typer.Option("", "--sort", help="Sort files by: size (descending)"),
    top: int = typer.Option(0, "--top", "-n", help="Show only the top N files"),
) -> None:
    """Print summary statistics for a parsed diff."""
    data = json.loads(hunks_file.read_text())
    typer.echo(f"Files: {data['file_count']}")
    typer.echo(f"Hunks: {data['hunk_count']}")
    typer.echo(f"Total lines changed: {data['total_size']}")
    files = data["files"]
    if sort == "size":
        files = sorted(files, key=lambda f: f["total_size"], reverse=True)
    if top > 0:
        files = files[:top]
    for f in files:
        typer.echo(f"  {f['path']}: {f['total_size']} lines, {len(f['hunks'])} hunks"
                   + (" [new]" if f["is_new"] else "")
                   + (" [deleted]" if f["is_deleted"] else ""))


@app.command(name="list-hunks")
def list_hunks(
    hunks_file: Path = typer.Argument(..., help="Path to hunks JSON from parse-diff"),
    detail: bool = typer.Option(False, "--detail", "-d", help="Show scope, signature, and refs per hunk"),
    skip: str = typer.Option("", "--skip", help="Comma-separated path patterns to exclude"),
    only: str = typer.Option("", "--only", help="Only show files matching these path patterns"),
    scope: str = typer.Option("", "--scope", help="Only show hunks whose scope contains this substring"),
    status: str = typer.Option("", "--status", help="Filter by status: NEW, MOD, or DEL"),
    sort: str = typer.Option("", "--sort", help="Sort files by: size (descending) or name"),
    top: int = typer.Option(0, "--top", "-n", help="Show only the top N files"),
    summary: bool = typer.Option(False, "--summary", "-s", help="Show totals at the end"),
) -> None:
    """List all files with their hunk IDs, sizes, and flags.

    Use --detail to include scope, signature, and symbol references per hunk.
    Use --skip to exclude paths. Use --only to show only matching paths.
    Use --scope to filter by function/class name. Use --sort size to rank by size.
    Use --top N to limit output. Use --summary for totals.
    """
    data = json.loads(hunks_file.read_text())
    skip_patterns = tuple(p.strip() for p in skip.split(",") if p.strip()) if skip else ()
    only_patterns = tuple(p.strip() for p in only.split(",") if p.strip()) if only else ()
    status_filter = status.upper() if status else ""

    # First pass: collect file entries with computed metadata
    file_entries: list[dict] = []
    for f in data["files"]:
        if skip_patterns and any(p in f["path"] for p in skip_patterns):
            continue
        if only_patterns and not any(p in f["path"] for p in only_patterns):
            continue

        hunks = f["hunks"]
        if scope:
            hunks = [h for h in hunks if any(scope in s for s in h.get("scope", []))]
            if not hunks:
                continue

        if f["is_new"]:
            marker = "NEW"
        elif f["is_deleted"]:
            marker = "DEL"
        else:
            marker = "MOD"

        if status_filter and marker != status_filter:
            continue

        file_size = sum(h.get("added_lines", 0) + h.get("removed_lines", 0) for h in hunks)
        file_entries.append({
            "file": f, "hunks": hunks, "marker": marker, "size": file_size,
        })

    # Sort
    if sort == "size":
        file_entries.sort(key=lambda e: e["size"], reverse=True)
    elif sort == "name":
        file_entries.sort(key=lambda e: e["file"]["path"])

    # Limit
    if top > 0:
        file_entries = file_entries[:top]

    # Output
    total_files = 0
    total_hunks = 0
    total_lines = 0

    for entry in file_entries:
        f = entry["file"]
        hunks = entry["hunks"]
        marker = entry["marker"]

        total_files += 1
        total_hunks += len(hunks)
        total_lines += entry["size"]

        if not detail:
            hunk_ids = [h["id"] for h in hunks]
            typer.echo(
                f"{marker:3} {f['total_size']:5} {f['path']}  "
                f"[{len(hunk_ids)} hunks: {','.join(hunk_ids)}]"
            )
        else:
            typer.echo(f"{marker:3} {f['total_size']:5} {f['path']}")
            for h in hunks:
                size = h.get("added_lines", 0) + h.get("removed_lines", 0)
                h_scope = h.get("scope", [])
                sig = h.get("signature", "")
                scope_str = f"  scope={h_scope}" if h_scope else ""
                sig_str = f"  sig={sig[:80]}" if sig else ""
                typer.echo(f"    {h['id']}  size={size}{scope_str}{sig_str}")

    if summary:
        typer.echo(f"\nTotal: {total_files} files, {total_hunks} hunks, {total_lines} lines")


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
            # Show enriched metadata if present
            if h.get("signature"):
                sig = h["signature"] if isinstance(h["signature"], str) else h["signature"][0]
                typer.echo(f"  sig: {sig[:120]}")
            if h.get("symbols_referenced"):
                refs = h["symbols_referenced"][:10]
                typer.echo(f"  refs: {', '.join(refs)}")
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


@app.command(name="find-symbol")
def find_symbol(
    hunks_file: Path = typer.Argument(..., help="Path to analyzed hunks JSON"),
    symbol: str = typer.Argument(..., help="Symbol name to search for"),
    exact: bool = typer.Option(False, "--exact", "-e", help="Exact match (default: substring)"),
    summary: bool = typer.Option(False, "--summary", "-s", help="Show counts only"),
) -> None:
    """Find which hunks define or reference a symbol.

    Searches symbols_defined and symbols_referenced in analyzed hunks.
    Use for import tracing: find where a function is defined and who calls it.
    Use --summary for counts only (no per-hunk output).
    """
    data = json.loads(hunks_file.read_text())

    def matches(name: str) -> bool:
        return name == symbol if exact else symbol in name

    defined_count = 0
    referenced_count = 0

    for file_info in data["files"]:
        for h in file_info["hunks"]:
            fp = file_info.get("path", h.get("file_path", "?"))
            scope = h.get("scope", [])

            defined = [s for s in h.get("symbols_defined", []) if matches(s)]
            referenced = [s for s in h.get("symbols_referenced", []) if matches(s)]

            if defined:
                defined_count += 1
                if not summary:
                    typer.echo(
                        f"DEFINED:    {fp:50} scope={scope}  +{h.get('added_lines',0)}/-{h.get('removed_lines',0)}"
                        f"  symbols={defined}"
                    )
            if referenced:
                referenced_count += 1
                if not summary:
                    typer.echo(
                        f"REFERENCED: {fp:50} scope={scope}  +{h.get('added_lines',0)}/-{h.get('removed_lines',0)}"
                        f"  symbols={referenced}"
                    )

    if summary or (defined_count + referenced_count > 0):
        typer.echo(f"\n{defined_count} definitions, {referenced_count} references")


@app.command(name="show-discovery")
def show_discovery(
    hunks_file: Path = typer.Argument(..., help="Path to analyzed hunks JSON"),
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
    topic: str = typer.Option("", "--topic", "-t", help="Show details for one topic"),
    sort: str = typer.Option("", "--sort", help="Sort topics by: size (descending)"),
    only: str = typer.Option("", "--only", help="With --topic: only show files matching these patterns"),
    skip: str = typer.Option("", "--skip", help="With --topic: exclude files matching these patterns"),
) -> None:
    """Show discovery state: topics, sizes, and unassigned hunks.

    Computes real sizes from hunk data (not estimated_size). Use --topic
    to drill into one topic's files and scopes. Use --sort size to rank
    topics by size. Use --only/--skip with --topic to filter files.
    """
    hunks_data = json.loads(hunks_file.read_text())
    discovery = json.loads(discovery_file.read_text())

    # Build hunk index
    hunk_info: dict[str, dict] = {}
    for file_info in hunks_data["files"]:
        for h in file_info["hunks"]:
            hunk_info[h["id"]] = {
                "file": file_info.get("path", h.get("file_path", "?")),
                "scope": h.get("scope", []),
                "size": h.get("added_lines", 0) + h.get("removed_lines", 0),
            }

    assignments = _get_assignments(discovery)
    all_hunk_ids = set(hunk_info.keys())
    assigned_ids = set(assignments.keys())

    # Get DAG edges for dependencies
    edges = discovery.get("dag", {}).get("edges", [])
    dep_map: dict[str, list[str]] = {}
    for e in edges:
        dep_map.setdefault(e["to"], []).append(e["from"])

    if topic:
        # Detail view for one topic
        topic_hunks = [hid for hid, tid in assignments.items() if tid == topic]
        if not topic_hunks:
            typer.echo(f"Topic '{topic}' not found or has no assignments.")
            raise typer.Exit(1)

        total_size = sum(hunk_info[h]["size"] for h in topic_hunks if h in hunk_info)
        files: dict[str, list[dict]] = {}
        for hid in topic_hunks:
            info = hunk_info.get(hid)
            if info:
                files.setdefault(info["file"], []).append({"id": hid, **info})

        deps = dep_map.get(topic, [])
        dep_str = f"  depends_on: {', '.join(deps)}" if deps else ""
        typer.echo(f"{topic}: {len(topic_hunks)} hunks, {total_size} lines, {len(files)} files{dep_str}")

        # Show topic description if available
        topic_data = discovery.get("dag", {}).get("topics", {}).get(topic, {})
        desc = topic_data.get("description", "")
        if desc and not desc.startswith("Assigned by"):
            typer.echo(f"  {desc[:200]}")

        skip_patterns = tuple(p.strip() for p in skip.split(",") if p.strip()) if skip else ()
        only_patterns = tuple(p.strip() for p in only.split(",") if p.strip()) if only else ()

        typer.echo()
        for fp in sorted(files.keys()):
            if skip_patterns and any(p in fp for p in skip_patterns):
                continue
            if only_patterns and not any(p in fp for p in only_patterns):
                continue
            file_hunks = files[fp]
            file_size = sum(h["size"] for h in file_hunks)
            typer.echo(f"  {fp}  ({len(file_hunks)} hunks, {file_size} lines)")
            for h in file_hunks:
                scope_str = f"  scope={h['scope']}" if h["scope"] else ""
                typer.echo(f"    {h['id']}  size={h['size']}{scope_str}")
    else:
        # Summary view
        unassigned = all_hunk_ids - assigned_ids
        typer.echo(f"{len(discovery['dag']['topics'])} topics, {len(assigned_ids)} assigned, {len(unassigned)} unassigned\n")

        # Per-topic summary
        topic_stats: dict[str, dict] = {}
        for hid, tid in assignments.items():
            if tid not in topic_stats:
                topic_stats[tid] = {"hunks": 0, "size": 0, "files": set()}
            info = hunk_info.get(hid)
            if info:
                topic_stats[tid]["hunks"] += 1
                topic_stats[tid]["size"] += info["size"]
                topic_stats[tid]["files"].add(info["file"])

        # Determine order
        if sort == "size":
            order = sorted(topic_stats.keys(), key=lambda t: topic_stats[t]["size"], reverse=True)
        else:
            # Default: topological order
            try:
                dag = TopicDAG.from_dict(discovery["dag"])
                order = dag.topological_sort()
            except Exception:
                order = sorted(topic_stats.keys())

        for tid in order:
            s = topic_stats.get(tid)
            if not s:
                continue
            deps = dep_map.get(tid, [])
            dep_str = f"  depends: {', '.join(deps)}" if deps else ""
            typer.echo(
                f"  {tid:40} {s['hunks']:4} hunks  {s['size']:5} lines  "
                f"{len(s['files']):2} files{dep_str}"
            )

        if unassigned:
            typer.echo(f"\nUnassigned ({len(unassigned)}):")
            for hid in sorted(unassigned):
                info = hunk_info.get(hid)
                if info:
                    scope_str = f"  scope={info['scope']}" if info["scope"] else ""
                    typer.echo(f"  {info['file']}{scope_str}  size={info['size']}")



@app.command(name="update-metadata")
def update_metadata(
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
    metadata_file: Path = typer.Argument(None, help="Path to metadata JSON (optional if using --set)"),
    sets: list[str] = typer.Option([], "--set", "-s",
        help="Inline metadata: 'topic:field=value' (repeatable)"),
) -> None:
    """Update topic metadata in discovery.json.

    Two modes:
    1. From a file: update-metadata discovery.json metadata.json
    2. Inline:      update-metadata discovery.json --set "config:description=Foundation config"

    Supported fields: name, description, is_shared.
    Both modes can be combined. --set values override file values.

    Example --set usage:
        --set "config:description=Foundation config and dependency changes"
        --set "auth:name=Auth module refactoring"
        --set "auth:description=Cached authorization table, email lookup, query-token auth"
    """
    discovery = json.loads(discovery_file.read_text())
    topics = discovery.get("dag", {}).get("topics", {})
    updated = set()
    skipped = []

    # Apply file-based metadata if provided
    if metadata_file:
        metadata = json.loads(metadata_file.read_text())
        for topic_id, updates in metadata.items():
            if topic_id not in topics:
                skipped.append(topic_id)
                continue
            for field_name in ("name", "description", "is_shared", "key_files"):
                if field_name in updates:
                    topics[topic_id][field_name] = updates[field_name]
            updated.add(topic_id)

    # Apply inline --set overrides
    for spec in sets:
        # Format: "topic:field=value"
        colon_idx = spec.find(":")
        if colon_idx < 0:
            typer.echo(f"ERROR: invalid --set '{spec}' — expected 'topic:field=value'", err=True)
            raise typer.Exit(1)
        topic_id = spec[:colon_idx]
        rest = spec[colon_idx + 1:]
        eq_idx = rest.find("=")
        if eq_idx < 0:
            typer.echo(f"ERROR: invalid --set '{spec}' — expected 'topic:field=value'", err=True)
            raise typer.Exit(1)
        field_name = rest[:eq_idx]
        value = rest[eq_idx + 1:]

        if topic_id not in topics:
            skipped.append(topic_id)
            continue
        if field_name not in ("name", "description", "is_shared", "key_files"):
            typer.echo(f"ERROR: unknown field '{field_name}' — expected name/description/is_shared/key_files", err=True)
            raise typer.Exit(1)

        # Parse boolean/json for specific fields
        if field_name == "is_shared":
            value = value.lower() in ("true", "1", "yes")
        elif field_name == "key_files":
            value = json.loads(value)

        topics[topic_id][field_name] = value
        updated.add(topic_id)

    if not metadata_file and not sets:
        typer.echo("ERROR: provide a metadata file or --set flags", err=True)
        raise typer.Exit(1)

    discovery_file.write_text(json.dumps(discovery, indent=2))
    typer.echo(f"Updated {len(updated)} topics in {discovery_file}")
    if skipped:
        typer.echo(f"Skipped (not found): {', '.join(skipped)}")


@app.command(name="assign-hunks")
def assign_hunks(
    hunks_file: Path = typer.Argument(..., help="Path to analyzed hunks JSON"),
    output_file: Path = typer.Argument(..., help="Path to write discovery JSON"),
    topics: list[str] = typer.Option([], "--topic", "-t",
        help="Topic assignment: 'name:scope1,scope2' or 'name:path:pattern'"),
    bulk_topic: str = typer.Option("", "--bulk-topic",
        help="Topic name for bulk/vendored hunks (assigns all hunks matching --bulk-path)"),
    bulk_path: str = typer.Option("", "--bulk-path",
        help="Path pattern for bulk hunks"),
    remainder_topic: str = typer.Option("", "--remainder",
        help="Topic name for any unassigned hunks"),
    deps: list[str] = typer.Option([], "--dep", "-d",
        help="Dependency edge: 'from_topic:to_topic'"),
) -> None:
    """Assign hunks to topics by scope name or file path — no hunk IDs needed.

    The agent works with function names and file paths. This command
    resolves them to the correct hunk IDs internally.

    Examples:
        --topic "forecast-adapter:scope:get_versions_adapter,fill_in_otb_adapter"
        --topic "manage-cubes:scope:clone_brand_adapter,trim_cube_adapter"
        --topic "config:path:config.py,pyproject.toml,.gitignore"
        --topic "database:path:database/"
        --bulk-topic "legacy-shims" --bulk-path "_legacy/_shims/"
        --remainder "misc"
        --dep "config:database" --dep "database:caching"
    """
    hunks_data = json.loads(hunks_file.read_text())

    # Build lookup structures
    all_hunks: list[dict] = []
    for file_info in hunks_data["files"]:
        for h in file_info["hunks"]:
            h["_file_path"] = file_info.get("path", h.get("file_path", ""))
            all_hunks.append(h)

    # Track assignments: hunk_id -> topic_id
    assignments: dict[str, str] = {}
    topic_meta: dict[str, dict] = {}  # topic_id -> {name, description, hunk_ids, ...}

    # Process --bulk-topic first
    if bulk_topic and bulk_path:
        bulk_patterns = [p.strip() for p in bulk_path.split(",")]
        bulk_hunk_ids = []
        bulk_size = 0
        bulk_files = set()
        for h in all_hunks:
            if any(p in h["_file_path"] for p in bulk_patterns):
                assignments[h["id"]] = bulk_topic
                bulk_hunk_ids.append(h["id"])
                bulk_size += h.get("added_lines", 0) + h.get("removed_lines", 0)
                bulk_files.add(h["_file_path"])
        topic_meta[bulk_topic] = {
            "id": bulk_topic, "name": f"Bulk: {bulk_path}",
            "description": f"Vendored/bulk code from {bulk_path}",
            "estimated_size": bulk_size, "hunk_ids": bulk_hunk_ids,
            "is_shared": False,
        }
        typer.echo(f"  {bulk_topic}: {len(bulk_hunk_ids)} hunks, {bulk_size} lines, {len(bulk_files)} files")

    # Process --topic assignments
    for topic_spec in topics:
        # Parse "name:type:values" format
        parts = topic_spec.split(":", 2)
        if len(parts) < 2:
            typer.echo(f"ERROR: Invalid topic spec '{topic_spec}' — expected 'name:scope:x,y' or 'name:path:x,y'", err=True)
            raise typer.Exit(1)

        topic_name = parts[0]
        if len(parts) == 3:
            match_type = parts[1]  # "scope" or "path"
            match_values = [v.strip() for v in parts[2].split(",")]
        else:
            # Default: try scope first, fallback to path
            match_values = [v.strip() for v in parts[1].split(",")]
            # Detect if these look like paths (contain / or .)
            if any("/" in v or v.endswith(".py") or v.endswith(".ts") for v in match_values):
                match_type = "path"
            else:
                match_type = "scope"

        matched_ids = []
        matched_size = 0
        matched_files = set()

        for h in all_hunks:
            if h["id"] in assignments:
                continue  # already assigned

            if match_type == "scope":
                scope = h.get("scope", [])
                section = h.get("section_header", "")
                if any(v in scope or v == section for v in match_values):
                    assignments[h["id"]] = topic_name
                    matched_ids.append(h["id"])
                    matched_size += h.get("added_lines", 0) + h.get("removed_lines", 0)
                    matched_files.add(h["_file_path"])
            elif match_type == "path":
                if any(v in h["_file_path"] for v in match_values):
                    assignments[h["id"]] = topic_name
                    matched_ids.append(h["id"])
                    matched_size += h.get("added_lines", 0) + h.get("removed_lines", 0)
                    matched_files.add(h["_file_path"])

        if topic_name in topic_meta:
            topic_meta[topic_name]["hunk_ids"].extend(matched_ids)
            topic_meta[topic_name]["estimated_size"] += matched_size
        else:
            topic_meta[topic_name] = {
                "id": topic_name, "name": topic_name,
                "description": f"Assigned by {match_type}: {','.join(match_values[:5])}",
                "estimated_size": matched_size, "hunk_ids": matched_ids,
                "is_shared": False,
            }
        typer.echo(f"  {topic_name}: {len(matched_ids)} hunks, {matched_size} lines, {len(matched_files)} files")

    # Process --remainder
    if remainder_topic:
        remainder_ids = []
        remainder_size = 0
        remainder_files = set()
        for h in all_hunks:
            if h["id"] not in assignments:
                assignments[h["id"]] = remainder_topic
                remainder_ids.append(h["id"])
                remainder_size += h.get("added_lines", 0) + h.get("removed_lines", 0)
                remainder_files.add(h["_file_path"])
        if remainder_ids:
            topic_meta[remainder_topic] = {
                "id": remainder_topic, "name": remainder_topic,
                "description": "Remaining unassigned hunks",
                "estimated_size": remainder_size, "hunk_ids": remainder_ids,
                "is_shared": False,
            }
            typer.echo(f"  {remainder_topic}: {len(remainder_ids)} hunks, {remainder_size} lines, {len(remainder_files)} files")

    # Check for unassigned
    unassigned = [h for h in all_hunks if h["id"] not in assignments]
    if unassigned:
        typer.echo(f"\n  WARNING: {len(unassigned)} hunks unassigned:")
        for h in unassigned[:10]:
            typer.echo(f"    {h['_file_path']} scope={h.get('scope', [])}")
        if len(unassigned) > 10:
            typer.echo(f"    ... and {len(unassigned) - 10} more")

    # Build edges
    edges = []
    for dep_spec in deps:
        parts = dep_spec.split(":")
        if len(parts) == 2:
            edges.append({"from": parts[0], "to": parts[1]})

    # Write discovery.json
    discovery = {
        "dag": {
            "topics": topic_meta,
            "edges": edges,
        },
        "assignments": assignments,
    }

    output_file.write_text(json.dumps(discovery, indent=2))
    total = len(assignments)
    unassigned_count = len(unassigned)
    typer.echo(f"\nWrote {output_file}: {total}/{total + unassigned_count} hunks assigned, "
               f"{len(topic_meta)} topics, {len(edges)} edges")

    # Validation summary (same info as validate-discovery)
    if unassigned_count == 0:
        typer.echo("VALID: All hunks assigned.")
    else:
        typer.echo(f"INVALID: {unassigned_count} hunks unassigned.")


@app.command(name="edit-edges")
def edit_edges(
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
    add: list[str] = typer.Option([], "--add", "-a",
        help="Add edge: 'from:to'"),
    remove: list[str] = typer.Option([], "--remove", "-r",
        help="Remove edge: 'from:to'"),
) -> None:
    """Add or remove dependency edges in an existing discovery.json.

    Use this after assign-hunks + update-metadata to adjust edges without
    losing enriched metadata. Validates that referenced topics exist and
    that no cycles are introduced.

    Examples:
        --add "config:database"
        --remove "auth:logging"
    """
    discovery = json.loads(discovery_file.read_text())
    edges = discovery.get("dag", {}).get("edges", [])
    topics = discovery.get("dag", {}).get("topics", {})

    # Remove edges
    removed = 0
    for spec in remove:
        parts = spec.split(":", 1)
        if len(parts) != 2:
            typer.echo(f"ERROR: invalid remove spec '{spec}' — expected 'from:to'", err=True)
            raise typer.Exit(1)
        from_id, to_id = parts
        before = len(edges)
        edges = [e for e in edges if not (e["from"] == from_id and e["to"] == to_id)]
        if len(edges) < before:
            removed += 1
            typer.echo(f"  Removed: {from_id} -> {to_id}")
        else:
            typer.echo(f"  WARNING: edge {from_id} -> {to_id} not found")

    # Add edges
    added = 0
    for spec in add:
        parts = spec.split(":", 1)
        if len(parts) != 2:
            typer.echo(f"ERROR: invalid add spec '{spec}' — expected 'from:to'", err=True)
            raise typer.Exit(1)
        from_id, to_id = parts[0], parts[1]
        for tid in (from_id, to_id):
            if tid not in topics:
                typer.echo(f"ERROR: topic '{tid}' not found", err=True)
                raise typer.Exit(1)
        edge: dict = {"from": from_id, "to": to_id}
        # Check for duplicate
        if any(e["from"] == from_id and e["to"] == to_id for e in edges):
            typer.echo(f"  WARNING: edge {from_id} -> {to_id} already exists, replacing")
            edges = [e for e in edges if not (e["from"] == from_id and e["to"] == to_id)]
        edges.append(edge)
        added += 1
        typer.echo(f"  Added: {from_id} -> {to_id}")

    # Validate: rebuild DAG to check for cycles
    discovery["dag"]["edges"] = edges
    try:
        TopicDAG.from_dict(discovery["dag"])
    except Exception as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        typer.echo("Edge changes would create a cycle. Not written.", err=True)
        raise typer.Exit(1)

    discovery_file.write_text(json.dumps(discovery, indent=2))
    typer.echo(f"\n{added} added, {removed} removed. Total edges: {len(edges)}")


@app.command(name="merge-topics")
def merge_topics_cmd(
    discovery_file: Path = typer.Argument(..., help="Path to discovery JSON"),
    merge: str = typer.Argument(..., help="Comma-separated topic IDs to merge"),
    name: str = typer.Argument(..., help="Name for the merged topic"),
    merged_id: str = typer.Option("", "--id", help="ID for merged topic (default: slugified name)"),
) -> None:
    """Merge multiple topics into one in an existing discovery.json.

    Combines hunk assignments, preserves external edges, drops internal
    edges between the merged topics. Metadata (description, key_files)
    is concatenated from the source topics.

    Example:
        split-pr-tools merge-topics $RUN/discovery.json "auth,auth-tests" "Authentication"
    """
    discovery = json.loads(discovery_file.read_text())
    topic_ids = [t.strip() for t in merge.split(",")]

    if len(topic_ids) < 2:
        typer.echo("ERROR: need at least 2 topics to merge", err=True)
        raise typer.Exit(1)

    topics = discovery.get("dag", {}).get("topics", {})
    for tid in topic_ids:
        if tid not in topics:
            typer.echo(f"ERROR: topic '{tid}' not found", err=True)
            raise typer.Exit(1)

    # Generate merged ID from name if not provided
    if not merged_id:
        merged_id = name.lower().replace(" ", "-").replace("_", "-")
        merged_id = "".join(c for c in merged_id if c.isalnum() or c == "-").strip("-")

    # Rebuild DAG, merge, serialize back
    dag = TopicDAG.from_dict(discovery["dag"])
    merged = dag.merge_topics(topic_ids, merged_id, name)

    # Combine descriptions and key_files from source topics
    descriptions = []
    key_files = []
    for tid in topic_ids:
        src = topics[tid]
        if src.get("description") and not src["description"].startswith("Assigned by"):
            descriptions.append(src["description"])
        key_files.extend(src.get("key_files", []))

    # Update the merged topic in the DAG
    merged.description = " ".join(descriptions) if descriptions else f"Merged from: {', '.join(topic_ids)}"

    # Serialize back
    dag_dict = dag.to_dict()

    # Preserve key_files in the serialized form
    if key_files:
        dag_dict["topics"][merged_id]["key_files"] = key_files

    discovery["dag"] = dag_dict

    # Update assignments
    if "assignments" in discovery:
        for hid, tid in discovery["assignments"].items():
            if tid in topic_ids:
                discovery["assignments"][hid] = merged_id

    discovery_file.write_text(json.dumps(discovery, indent=2))

    typer.echo(f"Merged {len(topic_ids)} topics into '{merged_id}'")
    typer.echo(f"  {merged.hunk_count} hunks, {merged.estimated_size} lines")
    deps = dag.get_dependencies(merged_id)
    dependents = dag.get_dependents(merged_id)
    if deps:
        typer.echo(f"  depends on: {', '.join(deps)}")
    if dependents:
        typer.echo(f"  depended on by: {', '.join(dependents)}")


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
    hunks_file: Path = typer.Option(None, "--hunks", help="Analyzed hunks JSON (for virtual ID resolution)"),
) -> None:
    """Check topic sizes and report any that exceed the threshold."""
    parsed = parse_diff(diff_file.read_text())
    discovery = json.loads(discovery_file.read_text())
    hunks_data = json.loads(hunks_file.read_text()) if hunks_file else None
    dag = TopicDAG.from_dict(discovery["dag"])

    planner = SplitPlanner(parsed, dag, size_threshold=threshold)
    planner.assign_hunks(_get_assignments(discovery, hunks_data))
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
        # For validation, use assignments as-is (no virtual→raw resolution)
        # We want to check that discovery IDs match hunks.json IDs directly
        assignments = _get_assignments(discovery)

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

    # Check for files with per-function virtual hunks assigned entirely to
    # one topic where the file dominates that topic's size. Virtual hunks
    # exist specifically so each function can go to a different topic —
    # assigning them all together defeats the mechanism.
    # This applies to any file with virtual hunks, whether new or existing.
    topic_sizes: dict[str, int] = {}
    for hid, tid in assignments.items():
        topic_sizes[tid] = topic_sizes.get(tid, 0) + hunk_sizes.get(hid, 0)

    unsplit_files = []
    for file_info in hunks_data["files"]:
        hunks_in_file = file_info.get("hunks", [])
        virtual_hunks = [h for h in hunks_in_file if h.get("is_virtual") or h.get("original_hunk_id")]
        if len(virtual_hunks) < 10:
            continue
        path = file_info.get("path", "")
        hunk_ids_in_file = [h["id"] for h in virtual_hunks]
        file_size = sum(hunk_sizes.get(hid, 0) for hid in hunk_ids_in_file)
        topics_for_file = {assignments.get(hid) for hid in hunk_ids_in_file if hid in assignments}
        if len(topics_for_file) == 1:
            topic = topics_for_file.pop()
            t_size = topic_sizes.get(topic, 0)
            if t_size > 0 and file_size / t_size > 0.6:
                unsplit_files.append((path, len(virtual_hunks), file_size, topic))

    if unsplit_files:
        typer.echo(f"\n  WARNING: {len(unsplit_files)} files have per-function virtual hunks "
                   "but are assigned entirely to one topic:")
        for path, count, fsize, topic in unsplit_files:
            typer.echo(f"    {path}: {count} functions, {fsize} lines, all in '{topic}'")
        typer.echo("  These were split by tree-sitter so each function can go to a different topic.")
        typer.echo("  Use show-hunks --file <path> to see functions, then assign by scope.")

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
