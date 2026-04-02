#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# split-pr evaluation pipeline
#
# Usage:
#   ./run_eval.sh setup [synthetic|fastapi|all]   — create test repos
#   ./run_eval.sh score <benchmark> <run-dir>     — evaluate a discovery result
#   ./run_eval.sh report                          — show all saved results
#
# The discovery step (running /split-pr) is manual — it requires Claude Code.
# This script handles everything before and after.
# =============================================================================

EVAL_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="${EVAL_DIR}/.workdir"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

setup_synthetic() {
    info "Setting up synthetic benchmark..."
    if [ -d "${WORK_DIR}/synthetic/.git" ]; then
        warn "Synthetic repo already exists at ${WORK_DIR}/synthetic — skipping"
        return
    fi

    export SYNTHETIC_DIR="${WORK_DIR}/synthetic"
    mkdir -p "${SYNTHETIC_DIR}"

    # Copy base project files first (the setup script expects them to exist)
    cp -r "${EVAL_DIR}/synthetic/base/"* "${SYNTHETIC_DIR}/"

    # Run the setup script (inits git, creates mega-pr branch)
    bash "${EVAL_DIR}/synthetic/setup.sh"
    ok "Synthetic repo ready at ${WORK_DIR}/synthetic"

    echo ""
    info "To run discovery:"
    echo "  cd ${WORK_DIR}/synthetic"
    echo "  git checkout mega-pr"
    echo "  /split-pr --base main"
    echo ""
    info "After discovery completes, copy the run directory:"
    echo "  ./run_eval.sh score synthetic /tmp/split-pr-<run-id>"
}

setup_fastapi() {
    info "Setting up FastAPI benchmark..."
    if [ -d "${WORK_DIR}/fastapi/.git" ]; then
        warn "FastAPI repo already exists at ${WORK_DIR}/fastapi — skipping"
        return
    fi

    mkdir -p "${WORK_DIR}"

    # Run the merge script
    export FASTAPI_DIR="${WORK_DIR}/fastapi"
    bash "${EVAL_DIR}/fastapi/setup.sh"
    ok "FastAPI repo ready at ${WORK_DIR}/fastapi"

    echo ""
    info "To run discovery:"
    echo "  cd ${WORK_DIR}/fastapi"
    echo "  git checkout mega-pr"
    echo "  /split-pr --base <base-commit>"
    echo ""
    info "After discovery completes, copy the run directory:"
    echo "  ./run_eval.sh score fastapi /tmp/split-pr-<run-id>"
}

setup_all() {
    setup_synthetic
    echo ""
    setup_fastapi
}

# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

score() {
    local benchmark="$1"
    local run_dir="$2"

    # Validate inputs
    local gt_file="${EVAL_DIR}/${benchmark}/ground_truth.json"
    if [ ! -f "$gt_file" ]; then
        err "Unknown benchmark: ${benchmark}"
        err "Expected ground truth at: ${gt_file}"
        exit 1
    fi

    local discovery_file="${run_dir}/discovery.json"
    if [ ! -f "$discovery_file" ]; then
        err "No discovery.json found in ${run_dir}"
        exit 1
    fi

    # Check for hunks file (needed for split-pr format)
    local hunks_arg=""
    if [ -f "${run_dir}/hunks.json" ]; then
        hunks_arg="${run_dir}/hunks.json"
    fi

    info "Evaluating ${benchmark} benchmark..."
    info "  Discovery: ${discovery_file}"
    info "  Ground truth: ${gt_file}"
    if [ -n "$hunks_arg" ]; then
        info "  Hunks: ${hunks_arg}"
    fi
    echo ""

    # Run evaluation
    python3 "${EVAL_DIR}/evaluate.py" "$discovery_file" "$gt_file" $hunks_arg

    # Save results
    local results_dir="${EVAL_DIR}/results"
    mkdir -p "$results_dir"
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)
    local result_file="${results_dir}/${benchmark}_${timestamp}.eval.json"

    if [ -f "${discovery_file%.json}.eval.json" ]; then
        cp "${discovery_file%.json}.eval.json" "$result_file"
        ok "Results saved to ${result_file}"
    fi
}

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

report() {
    local results_dir="${EVAL_DIR}/results"
    if [ ! -d "$results_dir" ] || [ -z "$(ls -A "$results_dir" 2>/dev/null)" ]; then
        warn "No evaluation results found. Run some benchmarks first."
        exit 0
    fi

    echo ""
    echo "========================================================================"
    echo "  EVALUATION HISTORY"
    echo "========================================================================"
    echo ""

    for result_file in "$results_dir"/*.eval.json; do
        local basename
        basename=$(basename "$result_file")
        local benchmark="${basename%%_[0-9]*}"
        local timestamp="${basename#*_}"
        timestamp="${timestamp%.eval.json}"

        # Extract summary from the JSON
        local avg_f1 topic_count
        avg_f1=$(python3 -c "import json; d=json.load(open('$result_file')); print(d['summary']['avg_f1'])" 2>/dev/null || echo "?")
        topic_count=$(python3 -c "import json; d=json.load(open('$result_file')); print(d['summary']['topic_count_gt'])" 2>/dev/null || echo "?")

        printf "  %-12s  %s  Topics: %-3s  Avg F1: %s\n" "$benchmark" "$timestamp" "$topic_count" "$avg_f1"
    done
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

usage() {
    echo "Usage:"
    echo "  $0 setup [synthetic|fastapi|all]   — create test repos"
    echo "  $0 score <benchmark> <run-dir>     — evaluate a discovery result"
    echo "  $0 report                          — show all saved results"
    echo ""
    echo "Benchmarks: synthetic, fastapi"
}

case "${1:-}" in
    setup)
        case "${2:-all}" in
            synthetic) setup_synthetic ;;
            fastapi)   setup_fastapi ;;
            all)       setup_all ;;
            *)         err "Unknown benchmark: ${2}"; usage; exit 1 ;;
        esac
        ;;
    score)
        if [ $# -lt 3 ]; then
            err "Usage: $0 score <benchmark> <run-dir>"
            exit 1
        fi
        score "$2" "$3"
        ;;
    report)
        report
        ;;
    *)
        usage
        exit 1
        ;;
esac
