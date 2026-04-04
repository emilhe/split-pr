"""AST-based analysis of diff hunks using tree-sitter.

Enriches hunks with structural information:
- scope: what top-level declaration(s) a hunk is inside
- symbols_defined: functions/classes/variables defined in the hunk
- symbols_referenced: identifiers used in the hunk

For new-file mega-hunks, splits them into per-declaration virtual hunks
so the discovery agent can assign them to different topics.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

try:
    from tree_sitter import Language, Parser, Node
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False

# Extension -> (grammar module name, language factory)
LANGUAGE_MAP: dict[str, tuple[str, str]] = {
    ".py": ("tree_sitter_python", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".js": ("tree_sitter_javascript", "language"),
    ".jsx": ("tree_sitter_javascript", "language"),
    ".go": ("tree_sitter_go", "language"),
    ".rs": ("tree_sitter_rust", "language"),
    ".java": ("tree_sitter_java", "language"),
    ".rb": ("tree_sitter_ruby", "language"),
    ".cs": ("tree_sitter_c_sharp", "language"),
}

# Node types that represent top-level declarations per language family
DECLARATION_TYPES: dict[str, set[str]] = {
    "python": {
        "function_definition", "class_definition", "decorated_definition",
    },
    "typescript": {
        "function_declaration", "class_declaration", "export_statement",
        "interface_declaration", "type_alias_declaration", "enum_declaration",
        "lexical_declaration",
    },
    "javascript": {
        "function_declaration", "class_declaration", "export_statement",
        "lexical_declaration",
    },
    "go": {
        "function_declaration", "method_declaration", "type_declaration",
    },
    "rust": {
        "function_item", "struct_item", "impl_item", "enum_item",
        "trait_item", "mod_item",
    },
    "java": {
        "class_declaration", "interface_declaration", "enum_declaration",
        "method_declaration",
    },
    "ruby": {
        "method", "class", "module",
    },
    "c_sharp": {
        "class_declaration", "method_declaration", "interface_declaration",
        "namespace_declaration",
    },
}

# Map extensions to language family for declaration types
EXT_TO_FAMILY: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".rb": "ruby", ".cs": "c_sharp",
}


@dataclass
class Declaration:
    """A top-level declaration found by tree-sitter."""
    name: str
    kind: str  # function, class, method, etc.
    start_line: int  # 1-indexed
    end_line: int  # 1-indexed, inclusive
    source: str  # the raw source text of this declaration

    @property
    def size(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass
class AnalyzedHunk:
    """A hunk enriched with AST analysis."""
    id: str
    file_path: str
    added_lines: int
    removed_lines: int
    content: str
    # AST enrichments
    scope: list[str] = field(default_factory=list)  # declarations this hunk is inside
    symbols_defined: list[str] = field(default_factory=list)
    symbols_referenced: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return self.added_lines + self.removed_lines


def _load_language(ext: str) -> "Language | None":
    """Try to load the tree-sitter language for a file extension."""
    if not TREE_SITTER_AVAILABLE:
        return None

    info = LANGUAGE_MAP.get(ext)
    if not info:
        return None

    module_name, factory_name = info
    try:
        mod = importlib.import_module(module_name)
        factory = getattr(mod, factory_name)
        return Language(factory())
    except (ImportError, AttributeError):
        return None


def _get_declaration_name(node: "Node") -> str:
    """Extract the name from a declaration node."""
    # For export statements, look inside
    if node.type == "export_statement":
        for child in node.children:
            if child.type in ("function_declaration", "class_declaration",
                              "interface_declaration", "type_alias_declaration",
                              "enum_declaration", "lexical_declaration"):
                return _get_declaration_name(child)
        return ""

    # For decorated definitions (Python), look at the inner definition
    if node.type == "decorated_definition":
        for child in node.children:
            if child.type in ("function_definition", "class_definition"):
                return _get_declaration_name(child)
        return ""

    # For lexical declarations (const/let/var), get the variable name
    if node.type == "lexical_declaration":
        for child in node.children:
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                if name_node:
                    return name_node.text.decode()
        return ""

    name_node = node.child_by_field_name("name")
    if name_node:
        return name_node.text.decode()
    return ""


def _extract_identifiers(node: "Node", identifiers: set[str]) -> None:
    """Recursively extract all identifier names from a node."""
    if node.type == "identifier":
        identifiers.add(node.text.decode())
    for child in node.children:
        _extract_identifiers(child, identifiers)


def parse_declarations(source: str, ext: str) -> list[Declaration]:
    """Parse top-level declarations from source code.

    Returns a list of Declaration objects with name, kind, line range,
    and source text.
    """
    lang = _load_language(ext)
    if lang is None:
        return []

    parser = Parser(lang)
    tree = parser.parse(source.encode())
    root = tree.root_node

    family = EXT_TO_FAMILY.get(ext, "")
    decl_types = DECLARATION_TYPES.get(family, set())

    declarations: list[Declaration] = []
    for child in root.children:
        if child.type not in decl_types:
            continue

        name = _get_declaration_name(child)
        if not name:
            continue

        # Determine kind
        kind = child.type
        if child.type == "export_statement":
            for c in child.children:
                if c.type in decl_types or c.type.endswith("_declaration") or c.type.endswith("_definition"):
                    kind = c.type
                    break

        declarations.append(Declaration(
            name=name,
            kind=kind.replace("_definition", "").replace("_declaration", "").replace("_item", ""),
            start_line=child.start_point[0] + 1,
            end_line=child.end_point[0] + 1,
            source=child.text.decode(),
        ))

    return declarations


def analyze_file(source: str, ext: str) -> dict[str, Any]:
    """Analyze a source file and return structural info.

    Returns dict with:
    - declarations: list of {name, kind, start_line, end_line, size}
    - symbols_defined: list of top-level names
    - symbols_referenced: set of all identifiers used
    """
    lang = _load_language(ext)
    if lang is None:
        return {"declarations": [], "symbols_defined": [], "symbols_referenced": []}

    parser = Parser(lang)
    tree = parser.parse(source.encode())
    root = tree.root_node

    decls = parse_declarations(source, ext)

    # Extract all identifiers
    identifiers: set[str] = set()
    _extract_identifiers(root, identifiers)
    defined = {d.name for d in decls}

    return {
        "declarations": [
            {"name": d.name, "kind": d.kind, "start_line": d.start_line,
             "end_line": d.end_line, "size": d.size}
            for d in decls
        ],
        "symbols_defined": sorted(defined),
        "symbols_referenced": sorted(identifiers - defined),
    }


def split_new_file_hunk(
    hunk_id: str,
    file_path: str,
    content: str,
    source: str,
    min_size: int = 100,
) -> list[dict[str, Any]]:
    """Split a large new-file hunk into per-declaration virtual hunks.

    Args:
        hunk_id: Original hunk ID.
        file_path: Path to the file.
        content: The hunk's diff content (with +/- prefixes).
        source: The actual source code of the new file.
        min_size: Only split if the file has more lines than this.

    Returns:
        List of virtual hunk dicts with the same structure as hunks.json hunks.
        If the file can't be split (too small, no declarations, unsupported
        language), returns a single-element list with the original.
    """
    ext = PurePosixPath(file_path).suffix
    lines = source.splitlines()

    if len(lines) < min_size:
        return []  # Don't split small files

    decls = parse_declarations(source, ext)
    if len(decls) < 2:
        return []  # Nothing to split

    # Build regions: preamble (imports, module-level) + each declaration
    regions: list[tuple[str, int, int]] = []  # (name, start_line, end_line)

    # Preamble: everything before the first declaration
    first_decl_line = decls[0].start_line
    if first_decl_line > 1:
        regions.append(("__preamble__", 1, first_decl_line - 1))

    # Each declaration
    for d in decls:
        regions.append((d.name, d.start_line, d.end_line))

    # Gaps between declarations (module-level code between functions)
    sorted_decls = sorted(decls, key=lambda d: d.start_line)
    for i in range(len(sorted_decls) - 1):
        gap_start = sorted_decls[i].end_line + 1
        gap_end = sorted_decls[i + 1].start_line - 1
        if gap_end >= gap_start:
            # Attach gap to the next declaration
            for r_idx, (name, start, end) in enumerate(regions):
                if name == sorted_decls[i + 1].name:
                    regions[r_idx] = (name, gap_start, end)
                    break

    # Build virtual hunks from regions
    virtual_hunks: list[dict[str, Any]] = []
    # Parse the diff content to extract added lines (lines starting with +)
    diff_lines = content.splitlines()

    for region_name, start_line, end_line in regions:
        # Extract the source lines for this region
        region_source = lines[start_line - 1:end_line]
        if not region_source:
            continue

        # Build a virtual diff content for this region
        # For new files, all lines are additions
        region_diff_lines = [f"+{line}" for line in region_source]
        region_content = f"@@ -0,0 +{start_line},{len(region_source)} @@\n" + "\n".join(region_diff_lines)

        # Generate a deterministic ID
        raw = f"{file_path}:{region_name}:{start_line}:{end_line}"
        virtual_id = hashlib.sha256(raw.encode()).hexdigest()[:12]

        # Analyze symbols in this region
        region_text = "\n".join(region_source)
        lang = _load_language(ext)
        symbols_defined: list[str] = []
        symbols_referenced: list[str] = []
        if lang:
            parser = Parser(lang)
            tree = parser.parse(region_text.encode())
            identifiers: set[str] = set()
            _extract_identifiers(tree.root_node, identifiers)
            # Declarations in this region
            region_decls = parse_declarations(region_text, ext)
            defined = {d.name for d in region_decls}
            symbols_defined = sorted(defined)
            symbols_referenced = sorted(identifiers - defined)

        virtual_hunks.append({
            "id": virtual_id,
            "file_path": file_path,
            "source_start": 0,
            "source_length": 0,
            "target_start": start_line,
            "target_length": len(region_source),
            "content": region_content,
            "added_lines": len(region_source),
            "removed_lines": 0,
            "section_header": region_name,
            "scope": [region_name],
            "symbols_defined": symbols_defined,
            "symbols_referenced": symbols_referenced,
            "is_virtual": True,
            "original_hunk_id": hunk_id,
        })

    return virtual_hunks


def _split_existing_hunk(
    hunk: dict, decls: list[Declaration], file_path: str,
) -> list[dict[str, Any]]:
    """Split an existing (non-new-file) hunk that spans multiple declarations.

    Parses the diff content to identify which lines belong to which
    declaration, and creates virtual sub-hunks per declaration.

    Returns empty list if splitting isn't feasible (e.g., lines can't be
    cleanly attributed to declarations).
    """
    content = hunk.get("content", "")
    hunk_target_start = hunk.get("target_start", 0)
    lines = content.splitlines()
    if not lines:
        return []

    # Skip the @@ header line
    body_lines = []
    header_line = ""
    for line in lines:
        if line.startswith("@@"):
            header_line = line
        else:
            body_lines.append(line)

    if not body_lines:
        return []

    # Map each diff line to a target line number
    # Context lines and + lines advance the target counter
    # - lines don't advance it
    line_assignments: list[tuple[str, int, str | None]] = []  # (diff_line, target_line, decl_name)
    target_line = hunk_target_start
    for diff_line in body_lines:
        if diff_line.startswith("-"):
            # Removed line — attribute to whichever decl owns this source line
            # Use the current target_line as approximation
            assigned = None
            for d in decls:
                if d.start_line <= target_line <= d.end_line:
                    assigned = d.name
                    break
            line_assignments.append((diff_line, target_line, assigned))
        elif diff_line.startswith("+"):
            # Added line
            assigned = None
            for d in decls:
                if d.start_line <= target_line <= d.end_line:
                    assigned = d.name
                    break
            line_assignments.append((diff_line, target_line, assigned))
            target_line += 1
        else:
            # Context line
            assigned = None
            for d in decls:
                if d.start_line <= target_line <= d.end_line:
                    assigned = d.name
                    break
            line_assignments.append((diff_line, target_line, assigned))
            target_line += 1

    # Group consecutive lines by declaration
    groups: list[tuple[str | None, list[str]]] = []
    current_decl = None
    current_lines: list[str] = []
    for diff_line, _, decl_name in line_assignments:
        if decl_name != current_decl and current_lines:
            groups.append((current_decl, current_lines))
            current_lines = []
        current_decl = decl_name
        current_lines.append(diff_line)
    if current_lines:
        groups.append((current_decl, current_lines))

    # Only split if we got at least 2 non-trivial groups
    non_trivial = [(name, lines) for name, lines in groups
                   if any(l.startswith("+") or l.startswith("-") for l in lines)]
    if len(non_trivial) < 2:
        return []

    # Build virtual hunks per group
    virtual_hunks: list[dict[str, Any]] = []
    running_target = hunk_target_start

    for group_name, group_lines in groups:
        added = sum(1 for l in group_lines if l.startswith("+"))
        removed = sum(1 for l in group_lines if l.startswith("-"))
        context = sum(1 for l in group_lines if not l.startswith("+") and not l.startswith("-"))

        if added == 0 and removed == 0:
            # Pure context — attach to next group
            running_target += context
            continue

        source_len = removed + context
        target_len = added + context

        hunk_header = f"@@ -{running_target},{source_len} +{running_target},{target_len} @@ {group_name or ''}"
        group_content = hunk_header + "\n" + "\n".join(group_lines)

        raw = f"{file_path}:{hunk['id']}:{group_name}:{running_target}"
        virtual_id = hashlib.sha256(raw.encode()).hexdigest()[:12]

        virtual_hunks.append({
            "id": virtual_id,
            "file_path": file_path,
            "source_start": running_target,
            "source_length": source_len,
            "target_start": running_target,
            "target_length": target_len,
            "content": group_content,
            "added_lines": added,
            "removed_lines": removed,
            "section_header": group_name or "",
            "scope": [group_name] if group_name else [],
            "is_virtual": True,
            "original_hunk_id": hunk["id"],
        })

        running_target += context + added

    return virtual_hunks if len(virtual_hunks) >= 2 else []


def enrich_hunks(hunks_data: dict, source_dir: str | None = None,
                  skip_patterns: tuple[str, ...] = ()) -> dict:
    """Enrich a hunks.json structure with AST analysis.

    For each hunk:
    - Adds scope, symbols_defined, symbols_referenced
    - Splits large new-file hunks into per-declaration virtual hunks

    Args:
        hunks_data: Parsed hunks.json dict.
        source_dir: Path to the repo root (for reading source files).

    Returns:
        Modified hunks_data with enriched hunks.
    """
    from pathlib import Path

    new_files = []
    for file_info in hunks_data["files"]:
        ext = PurePosixPath(file_info["path"]).suffix
        is_new = file_info.get("is_new", False)
        skip = any(pattern in file_info["path"] for pattern in skip_patterns)

        # Try to read source file for new-file splitting (skip vendored code)
        source_content = None
        if source_dir and is_new and ext in LANGUAGE_MAP and not skip:
            source_path = Path(source_dir) / file_info["path"]
            if source_path.exists():
                source_content = source_path.read_text()

        new_hunks = []
        for hunk in file_info["hunks"]:
            # Try to split large new-file hunks
            if (is_new and source_content
                    and hunk["added_lines"] >= 100
                    and len(file_info["hunks"]) == 1):
                virtual = split_new_file_hunk(
                    hunk["id"], file_info["path"],
                    hunk["content"], source_content,
                )
                if virtual:
                    new_hunks.extend(virtual)
                    continue

            # Enrich existing hunks with scope info and split if multi-scope
            # (skip vendored code — don't split or analyze)
            if source_dir and ext in LANGUAGE_MAP and not skip:
                source_path = Path(source_dir) / file_info["path"]
                if source_path.exists():
                    source_text = source_path.read_text()
                    decls = parse_declarations(source_text, ext)
                    # Find which declarations this hunk overlaps with
                    hunk_start = hunk.get("target_start", 0)
                    hunk_end = hunk_start + hunk.get("target_length", 0)
                    scope = []
                    for d in decls:
                        if d.start_line <= hunk_end and d.end_line >= hunk_start:
                            scope.append(d.name)
                    hunk["scope"] = scope

                    # If hunk spans multiple declarations, try to split it
                    if len(scope) >= 2:
                        split = _split_existing_hunk(hunk, decls, file_info["path"])
                        if split:
                            new_hunks.extend(split)
                            continue

            new_hunks.append(hunk)

        file_info["hunks"] = new_hunks

        # Update file-level stats
        file_info["total_size"] = sum(
            h.get("added_lines", 0) + h.get("removed_lines", 0)
            for h in new_hunks
        )
        new_files.append(file_info)

    hunks_data["files"] = new_files

    # Recalculate totals
    hunks_data["hunk_count"] = sum(len(f["hunks"]) for f in hunks_data["files"])
    hunks_data["total_size"] = sum(f["total_size"] for f in hunks_data["files"])

    return hunks_data
