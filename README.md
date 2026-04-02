# split-pr

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-93%20passing-brightgreen.svg)]()
[![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-plugin-blueviolet.svg)]()

**Stop letting monster PRs die in review purgatory.**

A Claude Code plugin that splits large PRs into chains of small, reviewable PRs. It analyzes your diff semantically, builds a dependency DAG, verifies the split is lossless, and creates the PRs with proper base branches and merge order.

## Install

```bash
/plugin marketplace add emilhe/split-pr
/plugin install split-pr@emilhe-split-pr
```

## Usage

```
/split-pr                              # split current branch vs main
/split-pr --base develop               # different base branch
/split-pr --name fcst-mig              # custom prefix for PR titles
/split-pr --threshold 600              # larger PRs allowed (default: 400 lines)
/split-pr --auto                       # skip interactive review
/split-pr --pr 42                      # split an existing PR
```

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
│  Classify hunks      │  │  Create branches       │
│  Build topic DAG     │  │  Apply patches         │
│  Detect vendored     │  │  Run validation        │
│  code, shims         │  │  Create PRs via gh     │
│  Recursive decomp    │  │  Update descriptions   │
└──────────┬───────────┘  └───────────┬────────────┘
           │                          │
           ▼                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    split-pr-tools CLI                        │
│                                                             │
│  Python library: diff_parser, dag, state                    │
│  (pure computation, zero git dependency)                    │
└─────────────────────────────────────────────────────────────┘
```

**AI does the thinking** (topic classification, dependency detection). **Code does the math** (DAG operations, diff parsing, lossless verification).

### Key features

- **Hunk-level splitting** — works with messy, entangled commits
- **DAG-based dependencies** — independent topics can be reviewed in parallel
- **Lossless verification** — every hunk accounted for before any PRs are created
- **Smart grouping** — vendored code stays bundled, tests travel with the code they test
- **Tracking issue** — auto-created checklist with merge order

## Local development

```bash
git clone https://github.com/emilhe/split-pr.git
cd split-pr

uv sync --all-extras          # install deps
uv run pytest                 # run tests
uv tool install --force .     # install CLI globally

# Symlink skill + agents into Claude Code
mkdir -p ~/.claude/skills/split-pr ~/.claude/agents
ln -sf "$(pwd)/commands/split-pr.md" ~/.claude/skills/split-pr/SKILL.md
ln -sf "$(pwd)/agents/pr-discovery.md" ~/.claude/agents/pr-discovery.md
ln -sf "$(pwd)/agents/pr-splitter.md" ~/.claude/agents/pr-splitter.md
```

Add to `~/.claude/settings.json` for permission-free CLI calls:

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

## Tech stack

| | |
|---|---|
| CLI | [Typer](https://typer.tiangolo.com/) |
| Diff parsing | [unidiff](https://github.com/matiasb/python-unidiff) |
| Graph algorithms | [NetworkX](https://networkx.org/) |
| Package management | [uv](https://docs.astral.sh/uv/) |
| AI orchestration | [Claude Code](https://claude.ai/code) skills + agents |
| Git operations | `git` + `gh` CLI |

## License

MIT
