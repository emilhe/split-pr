# FastAPI Evaluation Dataset for split-pr

## Overview

This dataset uses 10 real, merged PRs from [tiangolo/fastapi](https://github.com/tiangolo/fastapi) to evaluate the `split-pr` tool. The PRs span February 5-27, 2026, and cover a diverse mix of features, bug fixes, refactoring, and documentation changes.

The idea: merge all 10 PRs onto a single branch to create a "mega PR," then test whether `split-pr` can decompose it back into the original logical topics.

## Selected PRs

| # | PR | Category | Lines | Key Files |
|---|-----|----------|-------|-----------|
| 1 | #14616 Fix `Json[list[str]]` type | fix | 75 | `dependencies/utils.py` |
| 2 | #14873 Fix `APIRouter` `on_startup`/`on_shutdown` | fix | 81 | `routing.py` |
| 3 | #14908 JWT timing attack prevention docs | docs | 24 | `docs_src/security/` |
| 4 | #14953 JSON Schema bytes `contentMediaType` | refactor | 253 | `_compat/v2.py`, 21 files |
| 5 | #14957 Drop `fastapi-slim` support | refactor | 67 | `pdm_build.py`, `pyproject.toml` |
| 6 | #14962 Pydantic Rust serialization fast-path | feature | 241 | `routing.py`, `_compat/v2.py` |
| 7 | #14964 Deprecate `ORJSONResponse`/`UJSONResponse` | refactor | 173 | `responses.py`, `pyproject.toml` |
| 8 | #14978 Add `strict_content_type` checking | feature | 215 | `applications.py`, `routing.py` |
| 9 | #14986 Fix OpenAPI/Swagger UI escaping (XSS) | fix | 148 | `applications.py`, `openapi/docs.py` |
| 10 | #15022 Streaming JSON Lines + binary data | feature | 562 | `routing.py`, 21 files |

**Total: ~1,839 lines changed across ~79 files**

## Why These PRs

Selection criteria:
- **Self-contained**: Each PR addresses one logical concern (a feature, a fix, or a refactoring).
- **Diverse**: 3 features, 3 fixes, 3 refactors, 1 docs change.
- **Non-trivial**: Most PRs touch 50+ lines. The largest (#15022) adds 562 lines.
- **Code-heavy**: Every PR except #14908 modifies Python source code, not just docs.
- **Realistic overlaps**: Several PRs touch the same files (see below), which is the hard part for a splitter.

## File Overlaps (Difficulty Factors)

These files are modified by multiple PRs, creating realistic ambiguity:

| File | PRs | Difficulty |
|------|-----|------------|
| `fastapi/routing.py` | #14873, #14962, #14978, #15022 | **Hard** -- 4 PRs touch this file with unrelated changes |
| `fastapi/_compat/v2.py` | #14953, #14962 | Medium -- both add new functions |
| `fastapi/applications.py` | #14978, #14986 | Medium -- different concerns (content-type vs escaping) |
| `fastapi/dependencies/utils.py` | #14616, #15022 | Medium -- both modify dependency resolution |
| `pyproject.toml` | #14953, #14957, #14964, #15022 | **Hard** -- 4 PRs modify project config for different reasons |
| `docs/en/mkdocs.yml` | #14953, #14978, #15022 | Easy -- each adds a distinct nav entry |
| `tests/.../test_tutorial001.py` | #14962, #14964 | Easy -- different test changes |
| `uv.lock` | #14964, #15022 | Easy -- lock file, attributable by context |

## How to Build the Mega Branch

```bash
cd /tmp/split-pr-eval
bash merge_fastapi_prs.sh
```

The script will:
1. Clone FastAPI (or use existing clone)
2. Check out the base commit (`54f8aee...`)
3. Cherry-pick each PR's merge commit in chronological order
4. Create a branch called `mega-pr`

If there are cherry-pick conflicts (likely for `routing.py` and `pyproject.toml`), the script auto-resolves by accepting the incoming version. Review the output for any conflict warnings.

## How to Evaluate the Split

### Running the splitter

```bash
cd /tmp/split-pr-eval/fastapi
split-pr --base 54f8aeeb9a15e4d5a12401ec5549840966df0087
```

### Proposed Metric: Topic-File Overlap Score

For each predicted split group `G_i` and each ground-truth PR `P_j`, compute:

```
overlap(G_i, P_j) = |files(G_i) ∩ files(P_j)| / |files(G_i) ∪ files(P_j)|
```

Then find the best matching via Hungarian algorithm (or greedy assignment):

```
score = (1/N) * Σ max_j overlap(G_i, P_j)
```

Where N = max(|predicted groups|, |ground truth PRs|).

**Perfect score**: 1.0 (each predicted group exactly matches one PR's file set).
**Baseline**: A splitter that puts everything in one group scores ~0.1.

### Alternative Metrics

- **Hunk-level precision/recall**: Instead of file-level overlap, compare at the diff-hunk level. More precise but harder to compute.
- **Topic count accuracy**: |predicted groups| vs 10 (the true count). Penalize both over- and under-splitting.
- **Contamination rate**: For each predicted group, what fraction of its hunks belong to a single ground-truth PR? High contamination = bad splitting.

## Caveats

1. **Cherry-pick order matters.** PRs #14962 and #14964 are related (deprecating ORJSON follows from adding Pydantic serialization). A splitter might reasonably group these together. Consider this a "soft" boundary.

2. **`routing.py` is the hardest file.** Four PRs add different functionality to this file. A good splitter needs to attribute individual hunks/functions to the right topic, not just the file.

3. **`pyproject.toml` changes are heterogeneous.** Dependency removals (#14957), extras changes (#14964), new dependencies (#14953, #15022) -- a splitter needs to read the semantic context of each hunk.

4. **Some PRs have intermediate commits between them** on master (translation updates, CI tweaks, etc.). The cherry-pick approach skips these, so the mega branch may have minor inconsistencies in files touched by skipped commits. This should not affect the evaluation since we compare against the specific PR file sets.

5. **The docs PR (#14908)** is small (24 lines) and touches only `docs_src/security/` files that no other PR touches. It should be trivially separable -- a good sanity check.

6. **Lock file (`uv.lock`)** changes are hard to attribute meaningfully. Consider excluding this file from scoring.

## Files in This Dataset

```
/tmp/split-pr-eval/
├── fastapi_eval_README.md      # This file
├── fastapi_ground_truth.json   # Ground truth: PR metadata, files, categories
├── merge_fastapi_prs.sh        # Script to build the mega branch
└── fastapi/                    # FastAPI clone (created by merge script)
```
