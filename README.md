# split-pr

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-93%20passing-brightgreen.svg)]()
[![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-plugin-blueviolet.svg)]()

**Stop letting monster PRs die in review purgatory.**

`split-pr` is a Claude Code plugin that takes your massive, tangled pull request and splits it into a chain of small, reviewable PRs — automatically. It understands your code semantically, builds a dependency graph, and creates the PRs in the right order.

---

## The problem

You started with a small feature. Then you fixed a bug you found along the way. Then you refactored the module to make the feature fit better. Then you added tests. Then you noticed the CI config needed updating. Three weeks later, you have a 15,000-line PR that nobody wants to review.

You *know* it should be multiple PRs. But untangling it manually? That's a full day of careful `git cherry-pick`, conflict resolution, and praying you didn't lose anything.

## The solution

```
/split-pr
```

That's it. `split-pr` will:

1. **Analyze** your diff at the hunk level (not commit level — it handles messy history)
2. **Discover** semantic topics using AI ("these hunks are the auth layer, these are the caching infra, these are vendored copies...")
3. **Build a DAG** of dependencies between topics
4. **Verify** the split is lossless before touching anything
5. **Create PRs** with proper base branches, descriptions, and merge order
6. **Create a tracking issue** linking everything together


## How it works

```
┌─────────────────────────────────────────────────────────────┐
│                        /split-pr                            │
│                     (skill orchestrator)                    │
└──────────────┬──────────────────────┬───────────────────────┘
               │                      │
               ▼                      ▼
┌──────────────────────┐  ┌───────────────────────┐
│   Discovery Agent    │  │    Splitter Agent      │
│                      │  │                        │
│  • Classify hunks    │  │  • Create branches     │
│  • Build topic DAG   │  │  • Apply patches       │
│  • Detect vendored   │  │  • Run validation      │
│    code, shims       │  │  • Create PRs via gh   │
│  • Recursive decomp  │  │  • Update descriptions │
└──────────┬───────────┘  └───────────┬────────────┘
           │                          │
           ▼                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    split-pr-tools CLI                        │
│                                                             │
│  parse-diff · stats · list-hunks · show-hunks · build-plan  │
│  build-patches · check-sizes · validate-discovery · verify  │
│  show-plan · detect-validators                              │
│                                                             │
│  Python library: diff_parser · dag · state                  │
│  (pure computation, zero git dependency)                    │
└─────────────────────────────────────────────────────────────┘
```

**AI does the thinking** (topic classification, dependency detection, entanglement resolution). **Code does the math** (DAG operations, diff parsing, patch generation, lossless verification). Neither does the other's job.

## Key features

- **Hunk-level splitting** — works with messy, entangled commits (because that's how people actually work)
- **DAG-based dependencies** — independent topics target `main` directly; dependent ones chain properly
- **Lossless verification** — `verify` command checks every hunk is accounted for before creating any PRs
- **Smart grouping** — vendored/shim code stays bundled; tests travel with the code they test; generated files follow their source
- **Parallel review** — independent branches can be reviewed simultaneously
- **Tracking issue** — auto-created checklist with merge order and dependency links

## Install

### As a Claude Code plugin

```bash
claude plugin install emilhe/split-pr
```

The plugin auto-installs the CLI on first session via `uv tool install`.

### CLI only (no plugin)

```bash
uv tool install split-pr
```

Or from source:

```bash
git clone https://github.com/emilhe/split-pr.git
uv tool install ./split-pr
```

## Usage

In Claude Code:

```
/split-pr                              # split current branch vs main
/split-pr --base develop               # different base branch
/split-pr --name fcst-mig              # custom prefix for PR titles
/split-pr --threshold 600              # larger PRs allowed (default: 400 lines)
/split-pr --auto                       # skip interactive review
/split-pr --pr 42                      # split an existing PR
```

## Local development

```bash
git clone https://github.com/emilhe/split-pr.git
cd split-pr

# Install dependencies
uv sync --all-extras

# Run tests
uv run pytest

# Install CLI globally (for testing)
uv tool install --force .

# Install skill + agents for Claude Code (symlink to avoid copy drift)
ln -sf "$(pwd)/commands/split-pr.md" ~/.claude/skills/split-pr/SKILL.md
mkdir -p ~/.claude/agents
ln -sf "$(pwd)/agents/pr-discovery.md" ~/.claude/agents/pr-discovery.md
ln -sf "$(pwd)/agents/pr-splitter.md" ~/.claude/agents/pr-splitter.md

# After code changes, reinstall CLI
uv tool install --reinstall --force .
```

Add these to your `~/.claude/settings.json` for permission-free CLI calls:

```json
{
  "permissions": {
    "allow": [
      "Bash(split-pr-tools *)",
      "Write(/tmp/split-pr-*)"
    ]
  }
}
```

## Architecture

```
split-pr/
├── .claude-plugin/          # Plugin manifest
│   └── plugin.json
├── commands/                # Claude Code skill
│   └── split-pr.md
├── agents/                  # AI agents
│   ├── pr-discovery.md      #   Semantic analysis + DAG building
│   └── pr-splitter.md       #   Git operations + PR creation
├── hooks/                   # Auto-install CLI on session start
│   └── hooks.json
├── src/split_pr/            # Python library (no git dependency)
│   ├── cli.py               #   Typer CLI — 11 subcommands
│   ├── diff_parser.py       #   Unified diff → structured hunks
│   ├── dag.py               #   Topic DAG (networkx)
│   └── state.py             #   Split planner
└── tests/                   # 93 tests
```

## Tech stack

| Layer | Technology |
|-------|-----------|
| CLI | [Typer](https://typer.tiangolo.com/) |
| Diff parsing | [unidiff](https://github.com/matiasb/python-unidiff) |
| Graph algorithms | [NetworkX](https://networkx.org/) |
| Package management | [uv](https://docs.astral.sh/uv/) |
| AI orchestration | [Claude Code](https://claude.ai/code) skills + agents |
| Git operations | `git` + `gh` CLI (no Python git library) |

## License

MIT
