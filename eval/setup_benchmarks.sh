#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Set up evaluation benchmarks
#
# Creates test repos with "mega PR" branches that split-pr should decompose.
# Run occasionally — only when adding/updating benchmarks.
#
# Usage:
#   ./setup_benchmarks.sh              # set up all benchmarks
#   ./setup_benchmarks.sh synthetic    # set up only synthetic
#   ./setup_benchmarks.sh fastapi      # set up only FastAPI
# =============================================================================

EVAL_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="${EVAL_DIR}/workdir"

info()  { echo -e "\033[0;34m[INFO]\033[0m $*"; }
ok()    { echo -e "\033[0;32m[OK]\033[0m $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m $*"; }
err()   { echo -e "\033[0;31m[ERROR]\033[0m $*"; }

# ---------------------------------------------------------------------------

setup_synthetic() {
    info "Setting up synthetic benchmark..."
    local target="${WORK_DIR}/synthetic"

    if [ -d "${target}/.git" ]; then
        # Verify mega-pr branch exists
        if git -C "$target" rev-parse --verify mega-pr >/dev/null 2>&1; then
            ok "Synthetic repo already set up at ${target}"
            return
        fi
        warn "Repo exists but mega-pr branch missing — recreating"
        rm -rf "$target"
    fi

    mkdir -p "$target"
    cp -r "${EVAL_DIR}/synthetic/base/"* "$target/"

    export SYNTHETIC_DIR="$target"
    bash "${EVAL_DIR}/synthetic/setup.sh"

    # Verify
    local commit_count
    commit_count=$(git -C "$target" rev-list main..mega-pr --count)
    ok "Synthetic benchmark ready: ${commit_count} commits on mega-pr"
    git -C "$target" diff main...mega-pr --stat | tail -1
}

setup_fastapi() {
    info "Setting up FastAPI benchmark..."
    local target="${WORK_DIR}/fastapi"

    if [ -d "${target}/.git" ]; then
        if git -C "$target" rev-parse --verify mega-pr >/dev/null 2>&1; then
            ok "FastAPI repo already set up at ${target}"
            return
        fi
        warn "Repo exists but mega-pr branch missing — recreating"
        rm -rf "$target"
    fi

    export FASTAPI_DIR="$target"
    bash "${EVAL_DIR}/fastapi/setup.sh"

    if git -C "$target" rev-parse --verify mega-pr >/dev/null 2>&1; then
        local commit_count
        commit_count=$(git -C "$target" rev-list --count "$(git -C "$target" merge-base main mega-pr)..mega-pr")
        ok "FastAPI benchmark ready: ${commit_count} commits on mega-pr"
        git -C "$target" diff "$(git -C "$target" merge-base main mega-pr)...mega-pr" --stat | tail -1
    else
        err "FastAPI setup failed — mega-pr branch not created (likely cherry-pick conflicts)"
        err "This benchmark may need manual conflict resolution. See eval/fastapi/README.md"
    fi
}

# ---------------------------------------------------------------------------

echo "============================================================"
echo "  split-pr benchmark setup"
echo "============================================================"
echo ""

case "${1:-all}" in
    synthetic)
        setup_synthetic
        ;;
    fastapi)
        setup_fastapi
        ;;
    all)
        setup_synthetic
        echo ""
        setup_fastapi
        ;;
    *)
        err "Unknown benchmark: $1"
        echo "Usage: $0 [synthetic|fastapi|all]"
        exit 1
        ;;
esac

echo ""
echo "Benchmarks are in: ${WORK_DIR}/"
echo "Next: run /run-eval in a Claude Code session to execute benchmarks"
