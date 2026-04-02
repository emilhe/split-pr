# Evaluation Framework

Measures how well `split-pr` decomposes a "mega PR" back into its original logical topics.

## Benchmarks

| Benchmark | Topics | Lines | Key challenge |
|-----------|--------|-------|---------------|
| **synthetic** | 9 | ~800 | Controlled entanglement: same-file hunk splitting, cross-cutting renames, empty `__init__.py`, vendored code |
| **fastapi** | 10 | ~1,839 | Real PRs from [tiangolo/fastapi](https://github.com/tiangolo/fastapi): `routing.py` touched by 4 PRs, `pyproject.toml` by 4 |

## Workflow

```bash
# 1. Setup — create test repos with mega branches
./run_eval.sh setup all

# 2. Discover — run /split-pr in Claude Code (manual step)
cd eval/.workdir/synthetic
git checkout mega-pr
# In Claude Code: /split-pr --base main

# 3. Score — evaluate discovery output against ground truth
./run_eval.sh score synthetic /tmp/split-pr-<run-id>

# 4. Report — view all past results
./run_eval.sh report
```

## Metrics

| Metric | What it measures |
|--------|-----------------|
| **Topic Recall** | What % of a ground-truth topic's files landed in the same discovered topic? |
| **Topic Precision** | What % of a discovered topic's files belong to the matched ground-truth topic? |
| **Topic F1** | Harmonic mean of recall and precision |
| **DAG F1** | Did we get the dependency edges right? |
| **Over-split count** | Ground-truth topics incorrectly scattered across multiple discovered topics |
| **Under-split count** | Multiple ground-truth topics incorrectly merged into one discovered topic |
| **Composite** | 50% topic F1 + 25% DAG F1 + 25% split quality |

## Adding new benchmarks

1. Create `eval/<name>/` with `ground_truth.json` and `setup.sh`
2. Ground truth format:
   ```json
   {
     "topics": [
       {"id": "topic-name", "files": ["path/to/file.py"], "category": "feature"}
     ],
     "expected_dag_edges": [["dep-topic", "dependent-topic"]]
   }
   ```
3. `setup.sh` should create a git repo with a `mega-pr` branch at `eval/.workdir/<name>/`
