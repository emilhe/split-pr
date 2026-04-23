"""Parse unified diffs into structured Hunk objects.

Pure function: diff text in, structured data out. No git dependency.
Uses the `unidiff` library for robust parsing of unified diff format.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from pathlib import PurePosixPath

from unidiff import PatchSet


def weighted_size(added: int, removed: int, delete_weight: float = 0.0) -> int:
    """Review-cost estimate for a change: ``added + removed * delete_weight``.

    The default ``delete_weight=0`` counts only added lines, on the theory
    that reviewing a deletion is mostly "scan for references elsewhere"
    rather than line-by-line correctness. Callers that want deletes to
    contribute can pass e.g. ``0.25`` to count a deletion as a quarter of
    an addition. Returns an integer so size arithmetic stays simple.
    """
    return int(added + removed * delete_weight)


@dataclass(frozen=True)
class Hunk:
    """A single contiguous change within a file.

    This is the atomic unit of work for PR splitting. Each hunk belongs
    to exactly one file and represents a contiguous block of added/removed/
    modified lines.
    """

    id: str
    file_path: str
    source_start: int
    source_length: int
    target_start: int
    target_length: int
    content: str
    added_lines: int
    removed_lines: int
    section_header: str = ""

    @property
    def size(self) -> int:
        """Review cost in lines (added only by default).

        See :func:`weighted_size`. To include deletes, compute explicitly
        with ``weighted_size(h.added_lines, h.removed_lines, weight)``.
        """
        return weighted_size(self.added_lines, self.removed_lines, 0.0)

    @property
    def file_extension(self) -> str:
        return PurePosixPath(self.file_path).suffix

    @property
    def file_directory(self) -> str:
        return str(PurePosixPath(self.file_path).parent)


@dataclass
class FileDiff:
    """All hunks for a single file, plus file-level metadata."""

    path: str
    is_new: bool
    is_deleted: bool
    is_rename: bool
    old_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def total_size(self) -> int:
        return sum(h.size for h in self.hunks)

    @property
    def added_lines(self) -> int:
        return sum(h.added_lines for h in self.hunks)

    @property
    def removed_lines(self) -> int:
        return sum(h.removed_lines for h in self.hunks)


@dataclass
class ParsedDiff:
    """Complete parsed diff: all files and all hunks."""

    files: list[FileDiff] = field(default_factory=list)

    @property
    def all_hunks(self) -> list[Hunk]:
        return [h for f in self.files for h in f.hunks]

    @property
    def total_size(self) -> int:
        return sum(f.total_size for f in self.files)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def hunk_count(self) -> int:
        return sum(len(f.hunks) for f in self.files)

    def hunks_by_file(self) -> dict[str, list[Hunk]]:
        result: dict[str, list[Hunk]] = {}
        for f in self.files:
            result[f.path] = list(f.hunks)
        return result

    def to_json(self) -> str:
        return json.dumps(
            {
                "total_size": self.total_size,
                "file_count": self.file_count,
                "hunk_count": self.hunk_count,
                "files": [
                    {
                        "path": f.path,
                        "is_new": f.is_new,
                        "is_deleted": f.is_deleted,
                        "is_rename": f.is_rename,
                        "is_binary": f.is_binary,
                        "old_path": f.old_path,
                        "total_size": f.total_size,
                        "hunks": [asdict(h) for h in f.hunks],
                    }
                    for f in self.files
                ],
            },
            indent=2,
        )


def build_patch(parsed_diff: ParsedDiff, hunk_ids: set[str]) -> str:
    """Build a valid unified diff from selected hunks.

    Takes a ParsedDiff and a set of hunk IDs, and produces a patch string
    that can be fed to `git apply`. Reconstructs proper file headers for
    each file that has selected hunks.

    Args:
        parsed_diff: The full parsed diff.
        hunk_ids: Set of hunk IDs to include in the patch.

    Returns:
        A unified diff string ready for `git apply`.
    """
    parts: list[str] = []

    for file_diff in parsed_diff.files:
        selected = [h for h in file_diff.hunks if h.id in hunk_ids]
        if not selected:
            continue

        # Binary files are not representable as text patches without the
        # actual blob content. Skip them entirely — the post-apply step
        # in create-branches inspects the plan's file metadata and uses
        # git commands (rm/add) to execute binary operations directly.
        if file_diff.is_binary:
            continue

        path = file_diff.path
        old_path = file_diff.old_path or path

        # File header
        parts.append(f"diff --git a/{old_path} b/{path}")
        all_empty = all(h.content == "" for h in selected)
        if file_diff.is_new:
            parts.append("new file mode 100644")
            # Empty new files: header only, no --- / +++ / hunk body
            if all_empty:
                continue
            parts.append("--- /dev/null")
            parts.append(f"+++ b/{path}")
        elif file_diff.is_deleted:
            parts.append("deleted file mode 100644")
            if all_empty:
                continue
            parts.append(f"--- a/{old_path}")
            parts.append("+++ /dev/null")
        else:
            if file_diff.is_rename:
                # Pure renames have no diff body — git apply accepts them
                # with `similarity index 100%` + rename from/to and nothing
                # else. Renames with content changes emit the rename header
                # plus a normal unified diff; git apply tolerates those
                # without the similarity-index line.
                if all_empty:
                    parts.append("similarity index 100%")
                    parts.append(f"rename from {old_path}")
                    parts.append(f"rename to {path}")
                    continue
                parts.append(f"rename from {old_path}")
                parts.append(f"rename to {path}")
            parts.append(f"--- a/{old_path}")
            parts.append(f"+++ b/{path}")

        # Hunk content (already includes @@ header lines)
        for hunk in selected:
            if not hunk.content:
                continue  # Skip synthetic empty hunks
            content = hunk.content
            if not content.endswith("\n"):
                content += "\n"
            parts.append(content.rstrip("\n"))

    if not parts:
        return ""

    return "\n".join(parts) + "\n"


def _make_hunk_id(file_path: str, hunk_index: int, content: str) -> str:
    """Deterministic ID for a hunk based on its location and content."""
    raw = f"{file_path}:{hunk_index}:{content}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def parse_diff(diff_text: str) -> ParsedDiff:
    """Parse a unified diff string into structured data.

    Args:
        diff_text: Full unified diff output (e.g., from `git diff`).

    Returns:
        ParsedDiff with all files and hunks.
    """
    if not diff_text.strip():
        return ParsedDiff()

    patch = PatchSet(diff_text)
    files: list[FileDiff] = []

    for patched_file in patch:
        path = patched_file.path
        # unidiff uses 'b/' prefix removal, but path should be clean
        if path.startswith("b/"):
            path = path[2:]

        old_path = None
        source = patched_file.source_file or ""
        target = patched_file.target_file or ""
        # Strip a/ and b/ prefixes before comparing to detect real renames
        source_clean = source[2:] if source.startswith("a/") else source
        target_clean = target[2:] if target.startswith("b/") else target
        # /dev/null is not a real path — don't treat new/deleted files as renames
        is_rename = bool(source_clean and target_clean
                         and source_clean != target_clean
                         and source_clean != "/dev/null"
                         and target_clean != "/dev/null")
        if is_rename:
            old_path = source_clean

        file_diff = FileDiff(
            path=path,
            is_new=patched_file.is_added_file,
            is_deleted=patched_file.is_removed_file,
            is_rename=is_rename,
            old_path=old_path,
            is_binary=bool(getattr(patched_file, "is_binary_file", False)),
        )

        for i, hunk in enumerate(patched_file):
            content_lines = str(hunk)
            added = sum(1 for line in hunk if line.is_added)
            removed = sum(1 for line in hunk if line.is_removed)

            hunk_obj = Hunk(
                id=_make_hunk_id(path, i, content_lines),
                file_path=path,
                source_start=hunk.source_start,
                source_length=hunk.source_length,
                target_start=hunk.target_start,
                target_length=hunk.target_length,
                content=content_lines,
                added_lines=added,
                removed_lines=removed,
                section_header=hunk.section_header or "",
            )
            file_diff.hunks.append(hunk_obj)

        # Synthetic hunk for file-level operations unidiff doesn't produce
        # hunks for: empty new/deleted files, pure renames (100% similarity),
        # and binary files. These must flow through the pipeline so they
        # get assigned to a topic and executed during patch application.
        if not file_diff.hunks and (
            file_diff.is_new
            or file_diff.is_deleted
            or file_diff.is_rename
            or file_diff.is_binary
        ):
            marker = (
                "__binary__"
                if file_diff.is_binary
                else "__rename__"
                if file_diff.is_rename
                else "__empty_file__"
            )
            file_diff.hunks.append(Hunk(
                id=_make_hunk_id(path, 0, marker),
                file_path=path,
                source_start=0,
                source_length=0,
                target_start=0,
                target_length=0,
                content="",
                added_lines=0,
                removed_lines=0,
                section_header="",
            ))

        files.append(file_diff)

    return ParsedDiff(files=files)
