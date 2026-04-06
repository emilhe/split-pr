"""Tests for split_pr.analyzer — virtual hunk splitting via tree-sitter."""

import json
import tempfile
from pathlib import Path

from split_pr.analyzer import enrich_hunks
from split_pr.diff_parser import parse_diff


# A new Python file with 3 functions, >100 lines total.
# This must produce per-function virtual hunks after analysis.
NEW_FILE_SOURCE = '''\
import os
import sys
from typing import Optional


def get_user(user_id: int) -> dict:
    """Fetch a user by ID."""
    conn = get_connection()
    result = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = result.fetchone()
    if row is None:
        raise ValueError(f"User {user_id} not found")
    return {
        "id": row[0],
        "name": row[1],
        "email": row[2],
        "role": row[3],
        "created_at": row[4],
        "updated_at": row[5],
    }


def create_user(name: str, email: str, role: str = "viewer") -> dict:
    """Create a new user."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO users (name, email, role) VALUES (?, ?, ?)",
        (name, email, role),
    )
    conn.commit()
    user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return get_user(user_id)


def delete_user(user_id: int) -> None:
    """Delete a user by ID."""
    conn = get_connection()
    result = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if result.rowcount == 0:
        raise ValueError(f"User {user_id} not found")
    conn.commit()


def list_users(
    role: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List users with optional role filter."""
    conn = get_connection()
    query = "SELECT * FROM users"
    params: list = []
    if role:
        query += " WHERE role = ?"
        params.append(role)
    query += f" LIMIT {limit} OFFSET {offset}"
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "email": row[2],
            "role": row[3],
            "created_at": row[4],
            "updated_at": row[5],
        }
        for row in rows
    ]


def update_user(user_id: int, **kwargs) -> dict:
    """Update user fields."""
    conn = get_connection()
    sets = []
    params = []
    for key, value in kwargs.items():
        if key not in ("name", "email", "role"):
            raise ValueError(f"Invalid field: {key}")
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        raise ValueError("No fields to update")
    params.append(user_id)
    conn.execute(
        f"UPDATE users SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    conn.commit()
    return get_user(user_id)


def get_connection():
    """Get database connection."""
    return None


def get_user_permissions(user_id: int) -> list[str]:
    """Get permissions for a user."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT permission FROM user_permissions WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return [row[0] for row in rows]
'''


def _make_new_file_diff(path: str, content: str) -> str:
    """Build a unified diff for a new file from its content."""
    lines = content.splitlines()
    added = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"index 0000000..abcdef1\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{added}\n"
    )


def test_new_python_file_split_into_virtual_hunks():
    """A new Python file with multiple functions must be split by tree-sitter."""
    diff_text = _make_new_file_diff("src/users.py", NEW_FILE_SOURCE)
    parsed = parse_diff(diff_text)

    # Sanity: parse produces 1 file, 1 hunk
    assert len(parsed.files) == 1
    assert len(parsed.files[0].hunks) == 1
    assert parsed.files[0].hunks[0].added_lines > 100

    hunks_data = json.loads(parsed.to_json())

    with tempfile.TemporaryDirectory() as tmp:
        # Write the source file so tree-sitter can parse it
        src_path = Path(tmp) / "src" / "users.py"
        src_path.parent.mkdir(parents=True)
        src_path.write_text(NEW_FILE_SOURCE)

        enriched = enrich_hunks(hunks_data, source_dir=tmp)

    # Must have been split into multiple virtual hunks
    file_info = enriched["files"][0]
    hunks = file_info["hunks"]
    assert len(hunks) > 1, (
        f"Expected virtual hunks but got {len(hunks)}. "
        "Tree-sitter language grammars may not be installed."
    )

    # Each virtual hunk should have scope info
    scopes = [h.get("scope", []) for h in hunks]
    scope_names = [s[0] for s in scopes if s]
    assert "get_user" in scope_names
    assert "create_user" in scope_names
    assert "delete_user" in scope_names
    assert "list_users" in scope_names
    assert "update_user" in scope_names


def test_skip_patterns_prevent_splitting():
    """Files matching skip patterns should not be split or enriched."""
    diff_text = _make_new_file_diff("vendor/lib.py", NEW_FILE_SOURCE)
    parsed = parse_diff(diff_text)
    hunks_data = json.loads(parsed.to_json())

    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / "vendor" / "lib.py"
        src_path.parent.mkdir(parents=True)
        src_path.write_text(NEW_FILE_SOURCE)

        enriched = enrich_hunks(hunks_data, source_dir=tmp, skip_patterns=("vendor/",))

    # Should NOT be split — skip pattern matches
    file_info = enriched["files"][0]
    assert len(file_info["hunks"]) == 1


def test_skip_patterns_only_match_specified_paths():
    """Skip patterns should not match unrelated paths."""
    diff_text = _make_new_file_diff("src/adapter.py", NEW_FILE_SOURCE)
    parsed = parse_diff(diff_text)
    hunks_data = json.loads(parsed.to_json())

    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / "src" / "adapter.py"
        src_path.parent.mkdir(parents=True)
        src_path.write_text(NEW_FILE_SOURCE)

        # Skip pattern is for vendor/, should NOT affect src/adapter.py
        enriched = enrich_hunks(hunks_data, source_dir=tmp, skip_patterns=("vendor/",))

    file_info = enriched["files"][0]
    assert len(file_info["hunks"]) > 1, "adapter.py should be split — skip pattern doesn't match"
