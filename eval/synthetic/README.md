# Synthetic Evaluation Dataset for split-pr

## Overview

This dataset provides a controlled environment for testing split-pr's ability to decompose a large, entangled PR into logically cohesive sub-PRs. The "inventory-service" is a small Python project with 5 modules, and the mega-pr branch contains 8 commits that mix 9 distinct change topics in realistic ways.

## Repository Structure

```
synthetic/
  models/       — Domain models (Product, Warehouse, Inventory, Preferences)
  api/          — FastAPI endpoint definitions
  database/     — Connection management, raw and cached query repositories
  auth/         — JWT tokens and role-based permissions
  utils/        — Formatting, validation, and caching utilities
  tests/        — Unit tests, integration tests, fixtures, helpers
  _vendor/      — Vendored rate-limiting library
  reporting/    — New reporting package (with empty __init__.py files)
```

## Setup

```bash
bash /tmp/split-pr-eval/setup_synthetic.sh
```

This initializes the git repo, creates the base commit on `main`, then creates the `mega-pr` branch with 8 entangled commits.

## Ground Truth Topics (9 total)

| ID | Name | Files | Difficulty | Key Challenge |
|----|------|-------|------------|---------------|
| clean-feature | User preferences endpoint | 5 | Medium | Spans 3 commits, depends on cache |
| cross-cutting-rename | Rename build_log_prefix to log_context | 5 | Hard | Must not over-split a rename |
| shared-cache | TTL cache infrastructure | 4 | Medium | Split across commits 1 and 8 |
| bug-fix-low-stock | Fix off-by-one in low stock query | 1 | Hard | Single line in a file shared with another topic |
| new-warehouse-queries | WarehouseRepository | 2 | Medium | Same file as bug fix |
| vendored-ratelimit | Vendor rate limiting library | 5 | Easy | All in _vendor/ directory |
| reporting-package | New reporting package | 3 | Easy | Empty __init__.py files |
| test-infrastructure | Test fixtures, helpers, integration | 3 | Medium | Spread across 3 commits |
| config-deps-update | pyproject.toml + .gitignore | 2 | Easy | Standard config changes |

## Entanglement Design

No commit is "clean" (single-topic) except commit 7 (test helpers). All others intentionally mix concerns:

- **Commit 1**: shared-cache + bug-fix-low-stock (infrastructure mixed with bug fix)
- **Commit 2**: clean-feature + test-infrastructure (model mixed with test fixtures)
- **Commit 3**: clean-feature + cross-cutting-rename (new endpoint mixed with global rename)
- **Commit 4**: vendored-ratelimit + config-deps-update (vendored code mixed with config)
- **Commit 5**: new-warehouse-queries + reporting-package + test-infrastructure (three topics)
- **Commit 6**: clean-feature + config-deps-update (tests mixed with gitignore)
- **Commit 7**: test-infrastructure (clean — only topic)
- **Commit 8**: shared-cache + new-warehouse-queries (cache wiring shares __init__ updates)

## DAG (Dependency Edges)

```
shared-cache ──> clean-feature
cross-cutting-rename ──> new-warehouse-queries
```

The preferences endpoint uses the cache layer, and the warehouse repository uses the renamed `log_context` function.

## Shared Files

Several files belong to multiple topics (different hunks within the same file):

- `database/queries.py`: bug-fix-low-stock + new-warehouse-queries + cross-cutting-rename
- `database/__init__.py`: shared-cache + new-warehouse-queries
- `utils/__init__.py`: shared-cache + cross-cutting-rename
- `api/__init__.py`: clean-feature
- `models/__init__.py`: clean-feature

## Running the Evaluation

```bash
# First, run split-pr's discovery phase on the synthetic repo
# (this produces discovery.json)

# Then evaluate against ground truth
python3 /tmp/split-pr-eval/evaluate.py discovery.json

# Or with explicit ground truth path
python3 /tmp/split-pr-eval/evaluate.py discovery.json /tmp/split-pr-eval/synthetic_ground_truth.json
```

## Scoring

The evaluation script computes:

1. **Topic Recall**: For each ground-truth topic, what fraction of its expected files ended up in the same discovered topic?
2. **Topic Precision**: What fraction of a matched discovered topic's files actually belong to this ground-truth topic?
3. **DAG F1**: Edge-level precision and recall on dependency edges.
4. **Split Quality**: Counts of over-splits (one GT topic scattered across multiple discovered topics) and under-splits (multiple GT topics merged into one discovered topic).
5. **Composite Score**: Weighted combination (50% topic F1, 25% DAG F1, 25% split quality).

## Key Test Cases

### Hard: Same-file hunk splitting (bug-fix-low-stock vs new-warehouse-queries)
Both topics modify `database/queries.py`. The bug fix changes one character (`<` to `<=`) in an existing method, while the new queries append an entire class. The tool must split at the hunk level.

### Hard: Cross-cutting rename (cross-cutting-rename)
The rename of `build_log_prefix` to `log_context` touches 5 files across 3 modules. A naive file-level grouping would scatter these into separate topics. The tool must recognize semantic cohesion.

### Medium: Temporal scatter (shared-cache)
The cache module is created in commit 1 but its database integration appears in commit 8. The tool cannot rely on commit boundaries alone.

### Easy: Directory-based grouping (vendored-ratelimit)
All files in `_vendor/ratelimit/` should trivially group together.
