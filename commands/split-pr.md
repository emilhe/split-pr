---
name: split-pr
description: >
  Split a large PR (or branch diff) into a chain of smaller, reviewable PRs.
  Use when the user says "/split-pr", asks to split a PR, or wants to break
  a large branch into smaller pieces. Accepts optional arguments:
  --base <branch> (default: main), --threshold <lines> (default: 400),
  --max-files <n> (default: 10), --auto (skip interactive reviews).
user-invocable: true
allowed-tools:
  - Bash(split-pr-tools *)
  - Write(/tmp/split-pr-*)
  - Bash(git checkout *)
  - Bash(git apply *)
  - Bash(git add *)
  - Bash(git commit *)
  - Bash(git push -u *)
  - Bash(git diff *)
  - Bash(git log *)
  - Bash(git -C * checkout *)
  - Bash(git -C * apply *)
  - Bash(git -C * add *)
  - Bash(git -C * commit *)
  - Bash(git -C * push -u *)
  - Bash(git -C * diff *)
  - Bash(git -C * log *)
  - Bash(git -C * status *)
  - Bash(git -C * branch *)
  - Bash(gh pr create *)
  - Bash(gh pr edit *)
  - Bash(gh api *)
  - Bash(gh issue create *)
  - Bash(gh repo view *)
  - Bash(bash /tmp/split-pr-*)
---

# Split PR

Split a large PR or branch diff into a chain of smaller, reviewable PRs.

## Rules

1. Use `split-pr-tools <command>` for all computation. No `python3 -c`, no inline Python.
2. No compound shell commands (`cd && ...`, pipes through `grep`/`sort`). Use absolute paths or `git -C`.
3. If the CLI cannot do something, report it as a gap — do not work around it.

## CLI Reference

| Command | Purpose |
|---------|---------|
| `parse-diff <diff>` | Parse unified diff into hunks JSON |
| `analyze <hunks> <repo_dir> [--bulk <paths>]` | Enrich hunks with AST analysis, split large new files |
| `bundle-context <hunks> <repo_dir> [output] [--skip <paths>]` | Bundle all changed files into one context file |
| `stats <hunks> [--sort size] [--top N]` | Summary: file count, hunk count, per-file sizes |
| `list-hunks <hunks> --detail --skip X --only X --scope X --status MOD --sort size --top N --summary` | All files with scopes, signatures, filtering, sorting |
| `find-symbol <hunks> <name> --exact --summary` | Import tracing: find where a symbol is defined and who references it |
| `show-discovery <hunks> <discovery> --topic X --sort size --only X --skip X` | Topic summary with real sizes, or drill into one topic's files |
| `update-metadata <discovery> [metadata.json] --set "topic:field=value"` | Update topic metadata inline or from a JSON file |
| `assign-hunks <hunks> <output> --topic X --bulk-topic X --dep X` | Assign hunks to topics by scope/path (no IDs needed) |
| `edit-edges <discovery> --add "from:to" --remove "from:to"` | Add/remove edges without re-running assign-hunks |
| `merge-topics <discovery> "a,b" "New Name"` | Merge topics, preserving external edges and metadata |
| `split-topic <hunks> <discovery> <topic> --into "new_id:path:p1,p2" --dep "a:b"` | Split one topic into sub-topics by path or scope; transfers external edges |
| `show-hunks <hunks> [ids] --file X --preview N` | Inspect hunks by ID or file path, with content preview |
| `show-plan <plan> -v --branch X` | Plan summary with dependencies, files per branch. **Branches are in merge order.** |
| `build-plan <diff> <discovery> <base> <threshold> --hunks <hunks>` | Generate split plan from discovery |
| `build-patches <diff> <plan> -o <dir>` | Write patch files for each branch |
| `create-branches <diff> <plan> <repo> --author X --prefix X` | Create all branches, apply patches, commit (one command) |
| `push-branches <plan> <repo>` | Push all split branches to remote (one command) |
| `create-prs <plan> <discovery> <owner/repo> [--name X] [--original-pr N] [--tracking-issue N]` | Create all PRs with DAG diagrams (one command) |
| `check-sizes <diff> <discovery> <threshold> --hunks <hunks>` | Report oversized topics (pass --hunks for analyzed diffs) |
| `validate-discovery <hunks> <discovery>` | Check assignments, cycles, topic stats |
| `verify <diff> <plan>` | Verify split is lossless before execution |
| `verify-chain <plan> <repo> <base>` | Merge all branches in sequence, report conflicts (one command) |
| `verify-git <plan> <repo> <branch>` | Verify split branches reproduce original branch exactly |
| `render-dag <discovery> -h <topic> -l <links>` | Mermaid DAG, highlighted node, clickable |
| `render-dag-full <discovery> [plan] -l <links>` | Full DAG for tracking issue |
| `detect-validators` | Detect ruff/tsc/pytest/etc in CWD |

## Arguments

Parse the user's input after `/split-pr` for these optional flags:

| Flag | Default | Meaning |
|------|---------|---------|
| `--base <branch>` | `main` | Branch to diff against |
| `--threshold <n>` | `400` | Max lines per split PR |
| `--max-files <n>` | `10` | Max files per split PR |
| `--auto` | off | Skip interactive review steps |
| `--name <label>` | branch name (truncated to 20 chars) | Short label for PR title prefix |
| `--pr <number>` | none | Analyze an existing PR instead of current branch |
| `--bulk <paths>` | none | Comma-separated path patterns for vendored/bulk code (skips AST analysis) |

## Flow

### Phase 0: Create a run directory

Generate a short random ID and create an isolated temp directory for this run:

```bash
mkdir /tmp/split-pr-$(head -c4 /dev/urandom | xxd -p)
```

Use this directory (e.g., `/tmp/split-pr-a1b2c3d4`) for ALL temp files in
this run. Pass it to agents so they use the same directory. This prevents
collisions between concurrent or sequential runs.

All file paths below use `$RUN` as shorthand for this directory.

### Phase 1: Get the diff

If `--pr <number>` was given:
```bash
gh pr diff <number> > $RUN/diff.txt
```

Otherwise, diff the current branch against base:
```bash
git diff <base>...HEAD > $RUN/diff.txt
```

If the diff is empty, tell the user and stop.

### Phase 2: Parse and analyze the diff

```bash
split-pr-tools parse-diff $RUN/diff.txt > $RUN/hunks.json
```

```bash
split-pr-tools analyze $RUN/hunks.json <repo_dir>
```

If `--bulk` was passed, add it: `split-pr-tools analyze $RUN/hunks.json <repo_dir> --bulk "<paths>"`

The `analyze` command enriches hunks with AST analysis using tree-sitter:
- Splits large new-file hunks into per-declaration virtual hunks
- Adds scope info (which function/class each hunk is inside)
- This enables hunk-level splitting of files like adapter.py

```bash
split-pr-tools stats $RUN/hunks.json
```

Bundle all source files for the discovery agent (one read instead of 50+):

```bash
split-pr-tools bundle-context $RUN/hunks.json <repo_dir> $RUN/context.txt
```

If `--bulk` was passed, also skip those paths in the bundle:
`split-pr-tools bundle-context $RUN/hunks.json <repo_dir> $RUN/context.txt --skip "<paths>"`

Report the summary to the user. If total size is under the threshold, tell
the user the PR is already small enough and stop (unless they insist).

### Phase 3: Discovery

Launch the `pr-discovery` agent to analyze the hunks and build the topic DAG.

Pass it:
- The run directory: `$RUN`
- The size threshold (lines)
- The max files threshold
- The working directory (so it can read source files for context)
- If `--bulk` was passed, tell the agent which paths are bulk

**Do NOT include your own analysis of the diff, suggested topic boundaries,
or commentary about how files should be grouped.** The discovery agent has
its own heuristics and rules for classification. If you pre-digest the diff
("the adapter is one layer", "shims could be grouped by subpackage"), the
agent will follow your framing instead of applying its own rules — even
when your framing contradicts those rules. Pass only the mechanical inputs
listed above and let the agent do its job.

The agent reads `$RUN/hunks.json` and writes `$RUN/discovery.json`.

### Phase 4: Review (unless --auto)

Read the discovery output:

```bash
split-pr-tools render-dag $RUN/discovery.json
split-pr-tools check-sizes $RUN/diff.txt $RUN/discovery.json <threshold> --hunks $RUN/hunks.json
split-pr-tools show-discovery $RUN/hunks.json $RUN/discovery.json
```

Present to the user:

1. **Topic tree** with estimated sizes
2. **Dependency graph** (which topics depend on which)
3. **Independent groups** (topics that can be reviewed in parallel)
4. **Any oversized topics** that will need further decomposition

Ask the user if they want to:
- Approve and proceed
- Merge specific topics (`merge-topics`)
- Split a topic into sub-topics (`split-topic`, e.g. when an oversized
  topic bundles multiple logical units)
- Rename topics or adjust descriptions (`update-metadata`)
- Adjust dependencies (`edit-edges`)
- Abort

If they request changes, use the targeted commands above — do NOT
re-run `assign-hunks` (that rebuilds discovery.json from scratch and
loses edits). Re-present until approved.

If `--auto`, skip this phase entirely.

### Phase 5: Build the split plan

```bash
split-pr-tools build-plan $RUN/diff.txt $RUN/discovery.json <base> <threshold> --hunks $RUN/hunks.json > $RUN/plan.json
```

Then verify the split is lossless — all hunks accounted for, none duplicated:

```bash
split-pr-tools verify $RUN/diff.txt $RUN/plan.json
```

If verification fails, DO NOT proceed. Report the issues to the user.

### Phase 6: Execute the split

Launch the `pr-splitter` agent **in a worktree** to execute the plan.

Pass it:
- The run directory: `$RUN`
- The `--name` label (for PR title prefix)
- The original branch name (for reference in PR descriptions)

The agent creates branches, validates, pushes, and creates PRs. It runs
`verify-git` internally to confirm the split is lossless before pushing.

### Phase 7: Report

When the splitter agent completes, present the results:
- List of created PRs with URLs, titles, and sizes
- Any validation warnings
- The dependency/merge order
- If any branches failed validation, explain what happened

Generate the full DAG for the tracking issue:

```bash
split-pr-tools render-dag-full $RUN/discovery.json $RUN/plan.json
```

Then create the **tracking issue** with the DAG included:

```bash
gh issue create --title "Split: <original branch description>" --body "$(cat <<'EOF'
## Split: <original branch name>

Original branch: `<branch>` (<link to original PR if exists>)

Split into N PRs. Click nodes in the graph to navigate.

<paste render-dag-full output with --links here>

## Review guide

- **Review** in any order, but **merge** top-down (the DAG shows what's ready)
- Click nodes in the graph to navigate between PRs
- Leave comments on the split PR, not the original branch
- Small fixes: push to the split branch directly
- Cross-PR fixes: fix in the earliest branch; downstream PRs inherit on merge
- Wrong split boundary? Comment here on the tracking issue

## PRs (merge in order)
- [ ] #<pr1> — <title> (foundation)
- [ ] #<pr2> — <title> (depends on #<pr1>)
- [ ] #<pr3> — <title> (independent)
...

---
Generated by split-pr
EOF
)"
```

This is a standard part of the flow, not optional. The tracking issue
serves as the single reference point for the entire split.

## Error handling

- If discovery fails, report the error and suggest the user try with `--threshold`
  set higher (larger allowed PRs = fewer splits = easier decomposition)
- If splitting fails on a specific topic, report which topic and why. The user
  can re-run with adjusted parameters or manually handle that topic.
- If `gh` is not authenticated, tell the user to run `gh auth login`

## Setup

For hands-free operation with `--auto`, add to `~/.claude/settings.json`
under `permissions.allow`:

```
Bash(git checkout *), Bash(git apply *), Bash(git add *),
Bash(git commit *), Bash(git push *), Bash(gh pr create *),
Bash(gh issue create *)
```

If `gh` is not authenticated, run `gh auth login` first.
