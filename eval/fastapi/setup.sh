#!/usr/bin/env bash
#
# merge_fastapi_prs.sh
#
# Creates a "mega branch" by cherry-picking commits from 10 FastAPI PRs
# onto a single branch. The result simulates a large PR that our splitter
# should decompose back into the original topics.
#
# Usage:
#   cd /tmp/split-pr-eval
#   bash merge_fastapi_prs.sh
#
# Prerequisites:
#   - git
#   - Network access to clone from GitHub (or an existing clone)
#
# Output:
#   - A branch called "mega-pr" in /tmp/split-pr-eval/fastapi/ containing
#     all changes from the 10 selected PRs applied on top of the base commit.

set -euo pipefail

REPO_DIR="${FASTAPI_DIR:-/tmp/split-pr-eval/fastapi}"
BASE_COMMIT="54f8aeeb9a15e4d5a12401ec5549840966df0087"
MEGA_BRANCH="mega-pr"

# --- Merge commits for each PR (in chronological order) ---
# These are squash-merge commits on master. We cherry-pick them in order.
declare -a PR_COMMITS=(
    "c5fd75a321496b1f8212744bde217ec8ea956154"   # PR #14616 - Fix Json[list[str]] type
    "ed2512a5ec33d9d70f7a9ef2e5f9fe52d3b77ab2"   # PR #14873 - Fix APIRouter on_startup/on_shutdown
    "d11f820ac38bdea38e50c9ede001a6c9b57eac6e"   # PR #14908 - JWT timing attack docs
    "e8b98d21871f64234520c1e770f39e6bfea3b0d5"   # PR #14953 - JSON Schema bytes contentMediaType
    "083b6ebe9efa76cdee3fe3f74ea686a2ea860f51"   # PR #14957 - Drop fastapi-slim
    "590a5e535587cc07041ba12d308c748433ccb168"   # PR #14962 - Pydantic Rust serialization
    "48e983573232eea970fb4e0261818d4ab9a481b2"   # PR #14964 - Deprecate ORJSONResponse/UJSONResponse
    "22354a253037e0fb23e55dabcb8767943e371702"   # PR #14978 - strict_content_type
    "2686c7f"                                      # PR #14986 - OpenAPI/Swagger escaping (short SHA)
    "749cefdeb1428ba5c3911b03c4a72993f7eb3747"   # PR #15022 - Streaming JSON Lines
)

declare -a PR_LABELS=(
    "#14616 - Fix Json[list[str]] type"
    "#14873 - Fix APIRouter on_startup/on_shutdown"
    "#14908 - JWT timing attack docs"
    "#14953 - JSON Schema bytes contentMediaType"
    "#14957 - Drop fastapi-slim"
    "#14962 - Pydantic Rust serialization"
    "#14964 - Deprecate ORJSONResponse/UJSONResponse"
    "#14978 - strict_content_type"
    "#14986 - OpenAPI/Swagger escaping"
    "#15022 - Streaming JSON Lines"
)

# ---- Step 1: Clone if needed ----
if [ ! -d "$REPO_DIR/.git" ]; then
    echo "==> Cloning FastAPI repository..."
    git clone https://github.com/tiangolo/fastapi.git "$REPO_DIR"
else
    echo "==> Repository already exists at $REPO_DIR"
    cd "$REPO_DIR"
    echo "==> Fetching latest commits..."
    git fetch origin
fi

cd "$REPO_DIR"

# ---- Step 2: Ensure we have all the commits we need ----
echo "==> Verifying all required commits are available..."
for i in "${!PR_COMMITS[@]}"; do
    commit="${PR_COMMITS[$i]}"
    if ! git cat-file -e "$commit" 2>/dev/null; then
        echo "ERROR: Commit $commit (${PR_LABELS[$i]}) not found."
        echo "       You may need a full clone: git fetch --unshallow"
        exit 1
    fi
done
echo "    All commits found."

# ---- Step 3: Create the mega branch from the base commit ----
echo "==> Creating branch '$MEGA_BRANCH' from base commit $BASE_COMMIT..."
git checkout --force "$BASE_COMMIT"
git branch -D "$MEGA_BRANCH" 2>/dev/null || true
git checkout -b "$MEGA_BRANCH"

# ---- Step 4: Cherry-pick each PR's merge commit ----
echo "==> Cherry-picking PR commits..."
FAILED=()
for i in "${!PR_COMMITS[@]}"; do
    commit="${PR_COMMITS[$i]}"
    label="${PR_LABELS[$i]}"
    echo "    [$((i+1))/${#PR_COMMITS[@]}] Cherry-picking ${label}..."

    # Most merge commits on FastAPI are squash merges (single parent),
    # so a plain cherry-pick should work. If it's a true merge commit
    # (two parents), we use -m 1 to pick the first parent's diff.
    if git cat-file -p "$commit" | grep -c '^parent ' | grep -q '^2$'; then
        # True merge commit -- use -m 1
        if ! git cherry-pick -m 1 "$commit"; then
            echo "    WARNING: Conflict cherry-picking ${label}. Attempting to resolve..."
            # Accept theirs for conflicts (we want the PR's version)
            git checkout --theirs .
            git add -A
            git cherry-pick --continue --no-edit || git commit --no-edit -m "Cherry-pick ${label} (resolved conflicts)"
            FAILED+=("$label (conflicts resolved automatically)")
        fi
    else
        # Squash merge commit (single parent) -- plain cherry-pick
        if ! git cherry-pick "$commit"; then
            echo "    WARNING: Conflict cherry-picking ${label}. Attempting to resolve..."
            git checkout --theirs .
            git add -A
            GIT_EDITOR=true git cherry-pick --continue || git commit --no-edit -m "Cherry-pick ${label} (resolved conflicts)"
            FAILED+=("$label (conflicts resolved automatically)")
        fi
    fi
done

# ---- Step 5: Summary ----
echo ""
echo "============================================"
echo "  Mega branch '$MEGA_BRANCH' created!"
echo "============================================"
echo ""
echo "Base commit: $BASE_COMMIT"
echo "PRs merged:  ${#PR_COMMITS[@]}"
echo "Branch:      $MEGA_BRANCH"
echo ""

if [ ${#FAILED[@]} -gt 0 ]; then
    echo "Conflicts were auto-resolved in:"
    for f in "${FAILED[@]}"; do
        echo "  - $f"
    done
    echo ""
    echo "Review these carefully -- auto-resolution may have introduced issues."
fi

echo "To see the combined diff:"
echo "  cd $REPO_DIR"
echo "  git diff $BASE_COMMIT...$MEGA_BRANCH --stat"
echo ""
echo "To run split-pr against this:"
echo "  cd $REPO_DIR"
echo "  split-pr --base $BASE_COMMIT"
