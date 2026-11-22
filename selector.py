"""Select and schedule sqlglot regression tests from a unified diff."""

import argparse
import ast
import gzip
import io
import json
import re
import tokenize
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from bytecode_safety import same_executable_code
from comparison_types import ComparisonTypes
from import_effects import executable_declarations_changed

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@")
IDENTIFIER_COMMENT = re.compile(r"^\s*#(?!(?:\s*type\s*:|\s*!|.*coding[=:])).*$")
CONTROL_FLOW = re.compile(r"\b(?:and|or|not|if|elif|while|for|in|return|raise|except|match|case)\b")
DANGEROUS_BINDING = re.compile(r"^\s*(?:(?:from|import|class|def|async def|raise|del)\b|@|__all__\b)")
IDENTITY_OPERATOR = re.compile(r"\bis\b")
DIALECT_DIRS = ("dialects", "generators", "parsers", "typing")
BROAD_DIALECTS = frozenset({"dialect"})


@dataclass
class Hunk:
    start: int
    old_remaining: int
    new_remaining: int
    changes: list = field(default_factory=list)

    @property
    def complete(self):
        return self.old_remaining == self.new_remaining == 0


@dataclass
class FileChange:
    path: str
    hunks: list[Hunk] = field(default_factory=list)


def parse_patch(patch):
    """Read the old-side offsets and preserve contexts for validating edits."""
    changes = []
    entry = None
    hunk = None
    for line in patch.split("\n"):
        if hunk is not None and not hunk.complete and line.startswith((" ", "+", "-")):
            kind = line[0]
            hunk.changes.append((kind, line[1:]))
            hunk.old_remaining -= kind != "+"
            hunk.new_remaining -= kind != "-"
            if min(hunk.old_remaining, hunk.new_remaining) < 0:
                raise ValueError("too many lines in hunk")
        elif line.startswith("diff --git "):
            if hunk is not None and not hunk.complete:
                raise ValueError("unterminated hunk")
            hunk = None
            entry = None
        elif line.startswith(("Binary files ", "GIT binary patch", "rename from ", "rename to ", "old mode ", "new mode ")):
            raise ValueError("patch includes a change with no textual hunk")
        elif line.startswith("--- a/"):
            entry = FileChange(line[6:].split("\t", 1)[0])
            changes.append(entry)
        elif line == "--- /dev/null":
            entry = None
        elif line.startswith("+++ b/") and entry is None:
            entry = FileChange(line[6:].split("\t", 1)[0])
            changes.append(entry)
        elif line.startswith("@@ ") and entry is not None:
            match = HUNK.match(line)
            if not match:
                raise ValueError("unrecognized hunk header")
            if hunk is not None and not hunk.complete:
                raise ValueError("unterminated hunk")
            start = int(match[1])
            old_count, new_count = int(match[2] or 1), int(match[3] or 1)
            hunk = Hunk(start + 1 if old_count == 0 else start, old_count, new_count)
            entry.hunks.append(hunk)
    if hunk is not None and not hunk.complete:
        raise ValueError("unterminated hunk")
    return changes


def changes_to_lines(original, hunks):
    """Reconstruct what the diff means without running changed code."""
    old = original.split("\n")
    if old[-1] == "":
        old.pop()
    new = []
    probes = []
    edits = []
    cursor = 0
    for hunk in hunks:
        start = hunk.start - 1
        if not cursor <= start <= len(old):
            raise ValueError("hunk offset does not match pinned source")
        new.extend(old[cursor:start])
        cursor = start
        block_start = cursor
        removed = []
        added = []
        for kind, content in [*hunk.changes, (" ", None)]:
            if kind == " ":
                if removed or added:
                    changed = [old[line - 1] for line in removed] + added
                    flow = any(CONTROL_FLOW.search(text) for text in changed)
                    probes.append((removed, block_start + 1, flow))
                    edits.extend(added)
                    removed = []
                    added = []
                if content is None:
                    break
            if kind in (" ", "-"):
                if cursor >= len(old) or old[cursor] != content:
                    raise ValueError("patch context does not match pinned source")
                if kind == "-":
                    removed.append(cursor + 1)
                    edits.append(content)
                cursor += 1
            if kind in (" ", "+"):
                new.append(content)
            if kind == "+":
                added.append(content)
            if kind == " ":
                block_start = cursor
    new.extend(old[cursor:])
    return "\n".join(new) + "\n", probes, edits


def is_cosmetic(original, revised_tree, edits):
    if not all(not line.strip() or IDENTIFIER_COMMENT.fullmatch(line) for line in edits):
        return False
    before = ast.parse(original, type_comments=True) if isinstance(original, str) else original
    if hasattr(ast, "compare"):
        return ast.compare(before, revised_tree)
    return ast.dump(before) == ast.dump(revised_tree)


def evaluation_spans(source):
    """Expand across an expression whose earlier operands can short-circuit."""
    spans = set()
    tree = ast.parse(source) if isinstance(source, str) else source
    for node in ast.walk(tree):
        expression = None
        if isinstance(node, (ast.If, ast.While, ast.Assert)):
            expression = node.test
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            expression = node.iter
        elif isinstance(node, (ast.BoolOp, ast.IfExp, ast.Compare)):
            expression = node
        if expression is not None:
            spans.add((expression.lineno, expression.end_lineno))
    return spans


def union(items):
    result = 0
    for value in items:
        result |= value
    return result


def load_facts(path):
    if path is None or not path.is_file():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


class CoverageMap:
    def __init__(self, file, tests, *, reads=None, importers=None, types=None, shared_fixtures=None,
                 initialization=None):
        with gzip.open(file, "rt", encoding="utf-8") as stream:
            info = json.load(stream)
        self.names = info["tests"]
        self.files = info["files"]
        self.fixtures = info["fixtures"]
        self.numbers = {test["id"]: test["number"] for test in tests}
        self.by_path = {}
        for index, name in enumerate(self.names):
            path = name.split("::", 1)[0]
            self.by_path[path] = self.by_path.get(path, 0) | 1 << index
        self.language_tests = {
            test["number"] for test in tests
            if test["file"] in (
                "tests/test_dialect_entry_points.py", "tests/test_dialect_imports.py",
                "tests/dialects/test_dialect.py",
            )
        }
        self.tests = tests
        self.decoded = {}
        self.source_trees = {}
        self.source_spans = {}
        self.compiled = {}
        self.source_reads = load_facts(reads).get("source", {})
        self.importers = load_facts(importers)
        self.type_path = types
        self.type_facts = None
        self.shared_fixtures = load_facts(shared_fixtures)
        self.initialization = load_facts(initialization)
        self.all_tests = set(self.numbers.values())
        self.owned_tests = {}
        self.test_classes = {}
        for test in tests:
            self.owned_tests.setdefault(test["file"], set()).add(test["number"])
            owner, _, method = test["id"].rpartition("::")
            if method:
                self.test_classes.setdefault(owner, set()).add(test["number"])

    def source_tree(self, path):
        if path not in self.source_trees:
            self.source_trees[path] = ast.parse(self.files[path][0], type_comments=True)
        return self.source_trees[path]

    def lookup(self, path):
        if path not in self.decoded:
            _, _, patterns, rows = self.files[path]
            masks = [int(pattern, 16) for pattern in patterns]
            self.decoded[path] = {line: masks[index] for line, index in rows}
        return self.decoded[path]

    def dialect_family(self, path, include_generic=False):
        _, directory, *parts = path.split("/")
        stem = Path(parts[-1]).stem if parts else ""
        if directory not in DIALECT_DIRS or stem in BROAD_DIALECTS:
            return 0
        group = self.by_path.get("tests/dialects/test_" + stem + ".py", 0)
        for area in DIALECT_DIRS:
            relative = f"sqlglot/{area}/{stem}.py"
            if relative in self.files:
                group |= union(self.lookup(relative).values())
        if include_generic:
            group |= self.by_path.get("tests/dialects/test_dialect.py", 0)
        return group

    def selected_ids(self, bitmap):
        while bitmap:
            low = bitmap & -bitmap
            bitmap ^= low
            number = self.numbers.get(self.names[low.bit_length() - 1])
            if number is not None:
                yield number

    def python_change(self, path, hunks, isolated=False):
        original, scopes, *_ = self.files[path]
        revised, probes, edits = changes_to_lines(original, hunks)
        if not probes:
            raise ValueError("change has no hunks")
        hints = {line for removed, inserted, _ in probes for line in (removed or [inserted])}
        if min(hints) <= 2:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(revised.encode("utf-8")).readline)
            if encoding != "utf-8":
                raise ValueError("source encoding changed")
        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            revised_tree = ast.parse(revised, type_comments=True)
            compiled = compile(revised_tree, path, "exec", dont_inherit=True)
        if all(not line.strip() or IDENTIFIER_COMMENT.fullmatch(line) for line in edits):
            if is_cosmetic(self.source_tree(path), revised_tree, edits):
                return 0, False, False
        if not any(DANGEROUS_BINDING.match(line) for line in edits):
            if path not in self.compiled:
                self.compiled[path] = compile(original, path, "exec", dont_inherit=True)
            if same_executable_code(self.compiled[path], compiled):
                return 0, False, False
        lines = self.lookup(path)
        spans = ()
        if any(flow for _, _, flow in probes):
            if path not in self.source_spans:
                self.source_spans[path] = evaluation_spans(self.source_tree(path))
            spans = self.source_spans[path]
        mask = 0
        structural = False
        for removed, insert_at, flow in probes:
            points = removed or [insert_at]
            candidate = union(lines.get(line, 0) for line in points)
            for point in points:
                if flow:
                    for start, end in spans:
                        if start <= point <= end:
                            candidate |= union(lines.get(line, 0) for line in range(start, end + 1))
                scope = next((item for item in scopes if item[0] <= point <= item[1]), None)
                if scope is None:
                    structural = True
                    continue
                start, end, body, kind = scope
                if kind == "class" or point < body or not candidate or not removed:
                    if kind == "class" or point < body:
                        structural = True
                    candidate |= union(lines.get(line, 0) for line in range(start, end + 1))
            mask |= candidate
        import_sensitive = any(
            start <= point <= end
            for start, end in self.initialization.get(path, ())
            for point in hints
        )
        if structural or any(not removed for removed, _, _ in probes) or any(
            DANGEROUS_BINDING.match(line) for line in edits
        ):
            import_sensitive |= executable_declarations_changed(self.source_tree(path), revised_tree)
        if structural or import_sensitive:
            mask |= self.dialect_family(path)
            mask |= union(lines.values())
        elif isolated and mask:
            if self.type_path and any(IDENTITY_OPERATOR.search(line) for line in edits):
                if self.type_facts is None and self.type_path.is_file():
                    self.type_facts = ComparisonTypes(self.type_path, len(self.names))
                if self.type_facts is not None:
                    mask = self.type_facts.candidates(path, self.source_tree(path), revised_tree, mask, lines)
        return mask, structural, import_sensitive

    def select(self, patch):
        all_tests = self.all_tests
        mask = 0
        selected = set()
        try:
            changes = parse_patch(patch)
            if not changes:
                return all_tests
            for change in changes:
                path, hunks = change.path, change.hunks
                if not hunks:
                    return all_tests
                if path in self.files:
                    bitmap, structural, import_sensitive = self.python_change(path, hunks, isolated=len(changes) == 1)
                    mask |= bitmap
                    selected.update(self.numbers[name] for name in self.source_reads.get(path, ()) if name in self.numbers)
                    if import_sensitive:
                        for owner in self.importers.get(path, ()):
                            selected.update(self.owned_tests.get(owner, ()))
                        if path.split("/")[1] in DIALECT_DIRS:
                            selected |= self.language_tests
                    if structural and not bitmap:
                        if path.split("/")[1] in DIALECT_DIRS:
                            selected |= self.language_tests
                        else:
                            return all_tests
                elif path in self.fixtures:
                    mask |= int(self.fixtures[path], 16)
                    for owner in self.shared_fixtures.get(path, ()):
                        selected.update(self.test_classes.get(owner, ()))
                elif path.startswith("tests/") and path.endswith(".py"):
                    owned = self.owned_tests.get(path)
                    if not owned or path == "tests/dialects/test_dialect.py":
                        return all_tests
                    selected |= owned
                elif path.startswith(("docs/", ".github/")) or path in ("README.md", "CHANGELOG.md"):
                    selected |= {test["number"] for test in self.tests if test["file"] == "tests/test_docs.py"}
                else:
                    return all_tests
        except (ValueError, SyntaxError, SyntaxWarning, RecursionError):
            return all_tests
        return selected | set(self.selected_ids(mask))


def schedule(numbers, times):
    """Use the best longest-first packing across worker counts."""
    jobs = sorted(numbers, key=lambda number: (-times[number], number))
    if not jobs:
        return []
    best = [jobs]
    total = sum(times[number] for number in jobs)
    score = total + 2000
    for count in range(2, min(len(jobs), 8) + 1):
        if max(times[jobs[0]], (total + count - 1) // count) + 2000 * count >= score:
            continue
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for number in jobs:
            worker = loads.index(min(loads))
            workers[worker].append(number)
            loads[worker] += times[number]
        cost = max(loads) + 2000 * count
        if cost < score:
            best, score = workers, cost
    return best


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--data", type=Path, default=Path("/data"))
    cli.add_argument("--map", type=Path, default=Path(__file__).with_name("coverage-map.json.gz"))
    cli.add_argument("--reads", type=Path, default=Path(__file__).with_name("maps") / "source-reads.json.gz")
    cli.add_argument("--importers", type=Path, default=Path(__file__).with_name("maps") / "importers.json.gz")
    cli.add_argument("--types", type=Path, default=Path(__file__).with_name("maps") / "comparison-types.json.gz")
    cli.add_argument("--shared-fixtures", type=Path, default=Path(__file__).with_name("maps") / "shared-fixtures.json.gz")
    cli.add_argument("--initialization", type=Path, default=Path(__file__).with_name("maps") / "initialization-lines.json.gz")
    args = cli.parse_args()
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    times = {test["number"]: round(test["seconds"] * 1000) for test in tests}
    coverage_map = CoverageMap(args.map, tests, reads=args.reads, importers=args.importers,
                               types=args.types, shared_fixtures=args.shared_fixtures, initialization=args.initialization)
    for patch in sorted((args.data / "commits").glob("*.patch")):
        selected = coverage_map.select(patch.read_text(encoding="utf-8", errors="replace"))
        print(json.dumps({"commit": patch.stem, "workers": schedule(selected, times)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
