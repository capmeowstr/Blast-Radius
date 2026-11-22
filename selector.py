"""Select and schedule sqlglot regression tests from a unified diff."""

import argparse
import ast
import gzip
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from comparison_types import ComparisonTypes
from indexed_coverage import evaluation_spans, read_coverage
from value_profile import ValueProfile

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@")
IDENTIFIER_COMMENT = re.compile(r"^\s*#(?!(?:\s*type\s*:|\s*!|.*coding[=:])).*$")
CONTROL_FLOW = re.compile(r"\b(?:and|or|not|if|elif|while|for|in|return|raise|except|match|case)\b")
DIALECT_DIRS = ("dialects", "generators", "parsers", "typing")
BROAD_DIALECTS = frozenset({"dialect"})
IDENTITY_OPERATOR = re.compile(r"\bis\b")
EQUALITY_OPERATOR = re.compile(r"[!=]=")
VALUE_OPERATOR = re.compile(r"[<>=+*%/&|^-]")


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
    for line in patch.splitlines():
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
    old = original.splitlines()
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
    before = ast.parse(original, type_comments=True)
    return ast.compare(before, revised_tree) if hasattr(ast, "compare") else ast.dump(before) == ast.dump(revised_tree)


def union(items):
    result = 0
    for value in items:
        result |= value
    return result


class CoverageMap:
    def __init__(self, file, tests, *, types=None, values=None, reads=None):
        info = read_coverage(file)
        self.names = info["tests"]
        self.files = info["files"]
        self.fixtures = info["fixtures"]
        self.numbers = {test["id"]: test["number"] for test in tests}
        self.name_bits = {name: 1 << index for index, name in enumerate(self.names)}
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
        self.decoded = {}
        self.trees = {}
        self.spans = {}
        self.all_tests = set(self.numbers.values())
        self.by_file = {}
        for test in tests:
            self.by_file.setdefault(test["file"], set()).add(test["number"])
        self.types = ComparisonTypes(types, len(self.names)) if types and types.is_file() else None
        self.values = ValueProfile(values) if values and values.is_file() else None
        self.readers = {}
        if reads and reads.is_file():
            with gzip.open(reads, "rt", encoding="utf-8") as stream:
                self.readers = json.load(stream).get("source", {})

    def original_tree(self, path):
        if path not in self.trees:
            self.trees[path] = ast.parse(self.files[path][0], type_comments=True)
        return self.trees[path]

    def lookup(self, path):
        if path not in self.decoded:
            _, _, patterns, rows = self.files[path][:4]
            masks = [int(pattern, 16) if isinstance(pattern, str) else pattern for pattern in patterns]
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

    def python_change(self, path, hunks, isolated):
        original, scopes, _, _, *packed_spans = self.files[path]
        revised, probes, edits = changes_to_lines(original, hunks)
        if not probes:
            raise ValueError("change has no hunks")
        revised_tree = ast.parse(revised, type_comments=True)
        if is_cosmetic(original, revised_tree, edits):
            return 0, 0, False
        lines = self.lookup(path)
        spans = ()
        if any(flow for _, _, flow in probes):
            if path not in self.spans:
                self.spans[path] = packed_spans[0] if packed_spans else evaluation_spans(self.original_tree(path))
            spans = self.spans[path]
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
        if structural:
            mask |= self.dialect_family(path)
            mask |= union(lines.values())
        refined = mask
        if isolated and mask:
            if self.types and any(IDENTITY_OPERATOR.search(line) for line in edits) and any(
                EQUALITY_OPERATOR.search(line) for line in edits
            ):
                refined &= self.types.candidates(path, self.original_tree(path), revised_tree, refined, lines)
            if self.values and not structural and all(removed for removed, _, _ in probes) and any(
                VALUE_OPERATOR.search(line) for line in edits
            ):
                changed = {line for removed, _, _ in probes for line in removed}
                verified = self.values.confirmed(path, self.original_tree, revised_tree, changed)
                refined &= ~union(self.name_bits.get(name, 0) for name in verified)
        return mask, refined, structural

    def assess(self, patch):
        all_tests = self.all_tests
        mask = 0
        refined = 0
        selected = set()
        source_checks = set()
        try:
            changes = parse_patch(patch)
            if not changes:
                return all_tests, all_tests, set()
            for change in changes:
                path, hunks = change.path, change.hunks
                if not hunks:
                    return all_tests, all_tests, set()
                if path in self.files:
                    bitmap, narrowed, structural = self.python_change(path, hunks, isolated=len(changes) == 1)
                    mask |= bitmap
                    refined |= narrowed
                    source_checks.update(self.numbers[name] for name in self.readers.get(path, ()) if name in self.numbers)
                    if structural and not bitmap:
                        if path.split("/")[1] in DIALECT_DIRS:
                            selected |= self.language_tests
                        else:
                            return all_tests, all_tests, set()
                elif path in self.fixtures:
                    dependencies = int(self.fixtures[path], 16)
                    mask |= dependencies
                    refined |= dependencies
                elif path.startswith("tests/") and path.endswith(".py"):
                    owned = self.by_file.get(path)
                    if not owned:
                        return all_tests, all_tests, set()
                    selected |= owned
                elif path.startswith(("docs/", ".github/")) or path in ("README.md", "CHANGELOG.md"):
                    selected |= self.by_file.get("tests/test_docs.py", set())
                else:
                    return all_tests, all_tests, set()
        except (ValueError, SyntaxError, RecursionError):
            return all_tests, all_tests, set()
        return selected | set(self.selected_ids(mask)), selected | set(self.selected_ids(refined)), source_checks

    def select(self, patch):
        _, refined, readers = self.assess(patch)
        return refined | readers


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


def cost(workers, times):
    if not workers:
        return 0
    return 2000 * len(workers) + max(sum(times[number] for number in worker) for worker in workers)


def bounded_schedule(baseline, refined, source_checks, times):
    previous = schedule(baseline, times)
    if refined == baseline and not source_checks - baseline:
        return previous
    subset = [[number for number in worker if number in refined] for worker in previous]
    subset = [worker for worker in subset if worker]
    improved = min((subset, schedule(refined, times)), key=lambda workers: cost(workers, times))
    if source_checks - refined:
        guarded = schedule(refined | source_checks, times)
        if cost(guarded, times) <= cost(previous, times):
            return guarded
    return improved


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--data", type=Path, default=Path("/data"))
    default_map = Path(__file__).with_name("coverage-map.bin")
    if not default_map.is_file():
        default_map = Path(__file__).with_name("coverage-map.json.gz")
    cli.add_argument("--map", type=Path, default=default_map)
    maps = Path(__file__).with_name("maps")
    cli.add_argument("--types", type=Path, default=maps / "comparison-types.json.gz")
    cli.add_argument("--values", type=Path, default=maps / "operand-values.json.gz")
    cli.add_argument("--reads", type=Path, default=maps / "source-reads.json.gz")
    args = cli.parse_args()
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    times = {test["number"]: round(test["seconds"] * 1000) for test in tests}
    coverage_map = CoverageMap(args.map, tests, types=args.types, values=args.values, reads=args.reads)
    for patch in sorted((args.data / "commits").glob("*.patch")):
        baseline, selected, readers = coverage_map.assess(patch.read_text(encoding="utf-8", errors="replace"))
        workers = bounded_schedule(baseline, selected, readers, times)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
