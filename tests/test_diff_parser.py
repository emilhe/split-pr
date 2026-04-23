"""Tests for split_pr.diff_parser."""

import json

from split_pr.diff_parser import (
    FileDiff,
    Hunk,
    ParsedDiff,
    build_patch,
    parse_diff,
    weighted_size,
)


# -- Fixtures: realistic diff text snippets --

SIMPLE_DIFF = """\
diff --git a/src/models.py b/src/models.py
index abc1234..def5678 100644
--- a/src/models.py
+++ b/src/models.py
@@ -10,6 +10,8 @@ class User:
     name: str
     email: str

+    age: int
+    role: str

     def greet(self):
         return f"Hello, {self.name}"
"""

MULTI_FILE_DIFF = """\
diff --git a/src/models.py b/src/models.py
index abc1234..def5678 100644
--- a/src/models.py
+++ b/src/models.py
@@ -10,6 +10,8 @@ class User:
     name: str
     email: str

+    age: int
+    role: str

     def greet(self):
         return f"Hello, {self.name}"
diff --git a/src/api.py b/src/api.py
index 1111111..2222222 100644
--- a/src/api.py
+++ b/src/api.py
@@ -1,4 +1,5 @@
 from flask import Flask
+from flask import jsonify

 app = Flask(__name__)

@@ -20,1 +21,1 @@ def get_users():
-    return users
+    return jsonify(users)
"""

MULTI_HUNK_DIFF = """\
diff --git a/src/utils.py b/src/utils.py
index aaa1111..bbb2222 100644
--- a/src/utils.py
+++ b/src/utils.py
@@ -5,6 +5,7 @@ import os
 import sys
 import json

+import logging

 def parse_config(path):
     with open(path) as f:
@@ -50,5 +51,7 @@ def validate(data):
     if not data:
         return False
-    if "name" not in data:
-        return False
+    required = ["name", "email", "age"]
+    for field in required:
+        if field not in data:
+            return False
     return True
"""

NEW_FILE_DIFF = """\
diff --git a/src/new_module.py b/src/new_module.py
new file mode 100644
index 0000000..abc1234
--- /dev/null
+++ b/src/new_module.py
@@ -0,0 +1,5 @@
+\"\"\"Brand new module.\"\"\"
+
+
+def new_function():
+    return 42
"""

DELETED_FILE_DIFF = """\
diff --git a/src/old_module.py b/src/old_module.py
deleted file mode 100644
index abc1234..0000000
--- a/src/old_module.py
+++ /dev/null
@@ -1,3 +0,0 @@
-\"\"\"Old module to remove.\"\"\"
-
-OLD_CONSTANT = True
"""


class TestParseEmpty:
    def test_empty_string(self):
        result = parse_diff("")
        assert result.total_size == 0
        assert result.file_count == 0
        assert result.hunk_count == 0
        assert result.all_hunks == []

    def test_whitespace_only(self):
        result = parse_diff("   \n\n  \n")
        assert result.total_size == 0


class TestParseSingleFile:
    def test_basic_parse(self):
        result = parse_diff(SIMPLE_DIFF)
        assert result.file_count == 1
        assert result.hunk_count == 1
        assert result.files[0].path == "src/models.py"

    def test_hunk_line_counts(self):
        result = parse_diff(SIMPLE_DIFF)
        hunk = result.all_hunks[0]
        assert hunk.added_lines == 2
        assert hunk.removed_lines == 0
        assert hunk.size == 2

    def test_hunk_has_id(self):
        result = parse_diff(SIMPLE_DIFF)
        hunk = result.all_hunks[0]
        assert len(hunk.id) == 12  # sha256[:12]

    def test_hunk_id_is_deterministic(self):
        r1 = parse_diff(SIMPLE_DIFF)
        r2 = parse_diff(SIMPLE_DIFF)
        assert r1.all_hunks[0].id == r2.all_hunks[0].id

    def test_hunk_file_metadata(self):
        result = parse_diff(SIMPLE_DIFF)
        hunk = result.all_hunks[0]
        assert hunk.file_path == "src/models.py"
        assert hunk.file_extension == ".py"
        assert hunk.file_directory == "src"

    def test_file_not_new_or_deleted(self):
        result = parse_diff(SIMPLE_DIFF)
        f = result.files[0]
        assert not f.is_new
        assert not f.is_deleted
        assert not f.is_rename

    def test_section_header(self):
        result = parse_diff(SIMPLE_DIFF)
        hunk = result.all_hunks[0]
        assert "class User" in hunk.section_header


class TestParseMultiFile:
    def test_file_count(self):
        result = parse_diff(MULTI_FILE_DIFF)
        assert result.file_count == 2

    def test_file_paths(self):
        result = parse_diff(MULTI_FILE_DIFF)
        paths = [f.path for f in result.files]
        assert "src/models.py" in paths
        assert "src/api.py" in paths

    def test_total_hunks(self):
        result = parse_diff(MULTI_FILE_DIFF)
        # models.py has 1 hunk, api.py has 2 hunks
        assert result.hunk_count == 3

    def test_hunks_by_file(self):
        result = parse_diff(MULTI_FILE_DIFF)
        by_file = result.hunks_by_file()
        assert len(by_file["src/models.py"]) == 1
        assert len(by_file["src/api.py"]) == 2

    def test_total_size(self):
        result = parse_diff(MULTI_FILE_DIFF)
        # Hunk.size counts only added lines (default delete_weight=0):
        # models.py +2, api.py hunk1 +1, api.py hunk2 +1 = 4.
        assert result.total_size == 4

    def test_per_file_sizes(self):
        result = parse_diff(MULTI_FILE_DIFF)
        sizes = {f.path: f.total_size for f in result.files}
        assert sizes["src/models.py"] == 2
        assert sizes["src/api.py"] == 2  # +1 + +1 (removals don't count)


class TestParseMultiHunk:
    def test_hunk_count(self):
        result = parse_diff(MULTI_HUNK_DIFF)
        assert result.hunk_count == 2
        assert result.file_count == 1

    def test_hunks_are_distinct(self):
        result = parse_diff(MULTI_HUNK_DIFF)
        hunks = result.all_hunks
        assert hunks[0].id != hunks[1].id

    def test_hunk_positions(self):
        result = parse_diff(MULTI_HUNK_DIFF)
        hunks = result.all_hunks
        # First hunk starts at line 5, second at line 50
        assert hunks[0].source_start == 5
        assert hunks[1].source_start == 50


class TestNewAndDeletedFiles:
    def test_new_file(self):
        result = parse_diff(NEW_FILE_DIFF)
        assert result.file_count == 1
        f = result.files[0]
        assert f.is_new
        assert not f.is_deleted
        assert f.path == "src/new_module.py"

    def test_new_file_lines(self):
        result = parse_diff(NEW_FILE_DIFF)
        hunk = result.all_hunks[0]
        assert hunk.added_lines == 5
        assert hunk.removed_lines == 0

    def test_deleted_file(self):
        result = parse_diff(DELETED_FILE_DIFF)
        assert result.file_count == 1
        f = result.files[0]
        assert f.is_deleted
        assert not f.is_new

    def test_deleted_file_lines(self):
        result = parse_diff(DELETED_FILE_DIFF)
        hunk = result.all_hunks[0]
        assert hunk.added_lines == 0
        assert hunk.removed_lines == 3


class TestFileDiff:
    def test_total_size(self):
        h1 = Hunk(id="a", file_path="f.py", source_start=1, source_length=5,
                   target_start=1, target_length=7, content="...",
                   added_lines=3, removed_lines=1)
        h2 = Hunk(id="b", file_path="f.py", source_start=20, source_length=3,
                   target_start=22, target_length=5, content="...",
                   added_lines=2, removed_lines=0)
        fd = FileDiff(path="f.py", is_new=False, is_deleted=False,
                      is_rename=False, old_path=None, hunks=[h1, h2])
        # Hunk.size counts added only (default delete_weight=0).
        assert fd.total_size == 5  # 3 + 2
        assert fd.added_lines == 5
        assert fd.removed_lines == 1


class TestParsedDiffJson:
    def test_to_json_roundtrip(self):
        result = parse_diff(MULTI_FILE_DIFF)
        json_str = result.to_json()
        data = json.loads(json_str)
        assert data["file_count"] == 2
        assert data["hunk_count"] == 3
        assert len(data["files"]) == 2

    def test_to_json_includes_hunk_ids(self):
        result = parse_diff(SIMPLE_DIFF)
        data = json.loads(result.to_json())
        hunk = data["files"][0]["hunks"][0]
        assert "id" in hunk
        assert len(hunk["id"]) == 12


class TestBuildPatch:
    def test_single_hunk(self):
        parsed = parse_diff(SIMPLE_DIFF)
        hunk = parsed.all_hunks[0]
        patch = build_patch(parsed, {hunk.id})
        assert "diff --git" in patch
        assert "--- a/src/models.py" in patch
        assert "+++ b/src/models.py" in patch
        assert "+    age: int" in patch

    def test_patch_is_reparseable(self):
        """A built patch should itself be a valid unified diff."""
        parsed = parse_diff(SIMPLE_DIFF)
        hunk = parsed.all_hunks[0]
        patch = build_patch(parsed, {hunk.id})
        reparsed = parse_diff(patch)
        assert reparsed.file_count == 1
        assert reparsed.hunk_count == 1
        assert reparsed.all_hunks[0].added_lines == hunk.added_lines

    def test_subset_of_hunks(self):
        """Select only some hunks from a multi-hunk file."""
        parsed = parse_diff(MULTI_HUNK_DIFF)
        hunks = parsed.all_hunks
        assert len(hunks) == 2
        # Select only the first hunk
        patch = build_patch(parsed, {hunks[0].id})
        reparsed = parse_diff(patch)
        assert reparsed.hunk_count == 1
        assert reparsed.all_hunks[0].added_lines == hunks[0].added_lines

    def test_subset_of_files(self):
        """Select hunks from only one file in a multi-file diff."""
        parsed = parse_diff(MULTI_FILE_DIFF)
        by_file = parsed.hunks_by_file()
        # Only include hunks from models.py
        model_ids = {h.id for h in by_file["src/models.py"]}
        patch = build_patch(parsed, model_ids)
        reparsed = parse_diff(patch)
        assert reparsed.file_count == 1
        assert reparsed.files[0].path == "src/models.py"

    def test_new_file(self):
        parsed = parse_diff(NEW_FILE_DIFF)
        hunk = parsed.all_hunks[0]
        patch = build_patch(parsed, {hunk.id})
        assert "new file mode" in patch
        assert "--- /dev/null" in patch
        reparsed = parse_diff(patch)
        assert reparsed.files[0].is_new

    def test_deleted_file(self):
        parsed = parse_diff(DELETED_FILE_DIFF)
        hunk = parsed.all_hunks[0]
        patch = build_patch(parsed, {hunk.id})
        assert "deleted file mode" in patch
        assert "+++ /dev/null" in patch
        reparsed = parse_diff(patch)
        assert reparsed.files[0].is_deleted

    def test_empty_selection(self):
        parsed = parse_diff(SIMPLE_DIFF)
        patch = build_patch(parsed, set())
        assert patch == ""

    def test_all_hunks_roundtrip(self):
        """Selecting all hunks should produce a patch with same structure."""
        parsed = parse_diff(MULTI_FILE_DIFF)
        all_ids = {h.id for h in parsed.all_hunks}
        patch = build_patch(parsed, all_ids)
        reparsed = parse_diff(patch)
        assert reparsed.file_count == parsed.file_count
        assert reparsed.hunk_count == parsed.hunk_count


# -- Pure renames and binary files: operations with no unidiff hunks --

PURE_RENAME_DIFF = """\
diff --git a/src/old/__init__.py b/src/new/__init__.py
similarity index 100%
rename from src/old/__init__.py
rename to src/new/__init__.py
"""

BINARY_DELETE_DIFF = """\
diff --git a/data/sample.parquet b/data/sample.parquet
deleted file mode 100644
index cb55ddd..0000000
Binary files a/data/sample.parquet and /dev/null differ
"""


class TestPureRename:
    """Pure renames (100% similarity, no content change) must survive the pipeline."""

    def test_parse_creates_synthetic_hunk(self):
        parsed = parse_diff(PURE_RENAME_DIFF)
        assert parsed.file_count == 1
        f = parsed.files[0]
        assert f.is_rename
        assert not f.is_new
        assert not f.is_deleted
        assert f.old_path == "src/old/__init__.py"
        assert f.path == "src/new/__init__.py"
        # A synthetic hunk must exist so the file is assignable to a topic.
        assert len(f.hunks) == 1
        assert f.hunks[0].content == ""

    def test_build_patch_emits_similarity_index(self):
        parsed = parse_diff(PURE_RENAME_DIFF)
        all_ids = {h.id for h in parsed.all_hunks}
        patch = build_patch(parsed, all_ids)
        assert "similarity index 100%" in patch
        assert "rename from src/old/__init__.py" in patch
        assert "rename to src/new/__init__.py" in patch
        # No content body for a pure rename.
        assert "---" not in patch.replace("--- ", "")  # no --- a/… lines
        assert "+++" not in patch
        assert "@@" not in patch

    def test_roundtrip_is_detected_as_rename(self):
        parsed = parse_diff(PURE_RENAME_DIFF)
        all_ids = {h.id for h in parsed.all_hunks}
        patch = build_patch(parsed, all_ids)
        reparsed = parse_diff(patch)
        assert reparsed.files[0].is_rename


class TestBinaryFiles:
    """Binary files are flagged and excluded from text patches."""

    def test_parse_flags_binary_delete(self):
        parsed = parse_diff(BINARY_DELETE_DIFF)
        assert parsed.file_count == 1
        f = parsed.files[0]
        assert f.is_binary
        assert f.is_deleted
        # Synthetic hunk so the file is assignable to a topic.
        assert len(f.hunks) == 1

    def test_binary_excluded_from_patch(self):
        """Binary files must not appear in build_patch output — git apply can't handle them."""
        parsed = parse_diff(BINARY_DELETE_DIFF)
        all_ids = {h.id for h in parsed.all_hunks}
        patch = build_patch(parsed, all_ids)
        assert patch == ""

    def test_is_binary_serialized_in_json(self):
        parsed = parse_diff(BINARY_DELETE_DIFF)
        data = json.loads(parsed.to_json())
        assert data["files"][0]["is_binary"] is True

    def test_binary_mixed_with_text_only_emits_text(self):
        """A diff with both binary and text files emits only the text part in the patch."""
        combined = BINARY_DELETE_DIFF + SIMPLE_DIFF
        parsed = parse_diff(combined)
        all_ids = {h.id for h in parsed.all_hunks}
        patch = build_patch(parsed, all_ids)
        assert "sample.parquet" not in patch
        assert "src/models.py" in patch


class TestWeightedSize:
    """weighted_size defaults to additions only; opt into deletes via weight."""

    def test_default_weight_is_zero(self):
        assert weighted_size(10, 5) == 10

    def test_custom_weight(self):
        assert weighted_size(10, 8, delete_weight=0.25) == 12

    def test_weight_one_is_old_behavior(self):
        assert weighted_size(10, 5, delete_weight=1.0) == 15

    def test_pure_delete_with_zero_weight_is_zero(self):
        assert weighted_size(0, 500) == 0

    def test_pure_delete_with_weight_counts(self):
        assert weighted_size(0, 500, delete_weight=0.25) == 125

    def test_hunk_size_ignores_removed_by_default(self):
        h = Hunk(
            id="x", file_path="f.py", source_start=1, source_length=5,
            target_start=1, target_length=2, content="...",
            added_lines=2, removed_lines=500,
        )
        # size = added_lines only (default delete_weight=0).
        assert h.size == 2


class TestCollectBinaryOps:
    """_collect_binary_ops returns the git commands needed for binary files."""

    def test_binary_delete_becomes_rm(self):
        from split_pr.cli import _collect_binary_ops

        parsed = parse_diff(BINARY_DELETE_DIFF)
        all_ids = {h.id for h in parsed.all_hunks}
        ops = _collect_binary_ops(parsed, all_ids)
        assert ops == [("rm", "data/sample.parquet")]

    def test_binary_not_selected_omitted(self):
        from split_pr.cli import _collect_binary_ops

        parsed = parse_diff(BINARY_DELETE_DIFF)
        ops = _collect_binary_ops(parsed, set())  # no hunks selected
        assert ops == []

    def test_text_only_returns_empty(self):
        from split_pr.cli import _collect_binary_ops

        parsed = parse_diff(SIMPLE_DIFF)
        all_ids = {h.id for h in parsed.all_hunks}
        ops = _collect_binary_ops(parsed, all_ids)
        assert ops == []
