"""Build a reproducible runtime coverage map from the untouched pinned checkout."""

import ast
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

from coverage import CoverageData


def code_scopes(source):
    tree = ast.parse(source)
    scopes = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
            body = node.body[0].lineno
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            scopes.append([start, node.end_lineno, body, kind])
    return sorted(scopes, key=lambda scope: (scope[1] - scope[0], scope[0]))


def main():
    root, output, *traces = (Path(arg).resolve() for arg in sys.argv[1:])
    source_coverage = defaultdict(lambda: defaultdict(set))
    fixtures = defaultdict(set)
    names = set()
    passed = set()
    records = (path for trace in traces for path in sorted(trace.glob("gw*.json")))
    databases = (path for trace in traces for path in sorted(trace.glob(".coverage.*")))
    for record in records:
        report = json.loads(record.read_text())
        passed.update(report["passed"])
        for path, refs in report["fixtures"].items():
            fixtures[path].update(refs)
            names.update(refs)
    for database_path in databases:
        db = CoverageData(basename=str(database_path))
        db.read()
        for file in db.measured_files():
            path = Path(file).relative_to(root).as_posix()
            for line, contexts in db.contexts_by_lineno(file).items():
                tests = set(contexts) - {""}
                if tests:
                    source_coverage[path][line].update(tests)
                    names.update(tests)
    ids = sorted(names)
    mask = {name: 1 << idx for idx, name in enumerate(ids)}

    def encode(refs):
        return format(sum(mask[test] for test in refs), "x")

    files = {}
    for source in sorted(root.joinpath("sqlglot").rglob("*.py")):
        file = source.relative_to(root).as_posix()
        content = source.read_text()
        lines = source_coverage[file]
        patterns = sorted({encode(refs) for refs in lines.values()})
        positions = {value: idx for idx, value in enumerate(patterns)}
        files[file] = [content, code_scopes(content), patterns,
                       [[line, positions[encode(refs)]] for line, refs in sorted(lines.items())]]
    built = {"tests": ids, "files": files,
             "fixtures": {file: encode(refs) for file, refs in sorted(fixtures.items())}}
    output.write_bytes(gzip.compress(json.dumps(built, separators=(",", ":"), ensure_ascii=False).encode(), mtime=0))
    print(f"{len(files)} Python files, {len(ids)} coverage contexts, {len(passed)} passing tests, {len(fixtures)} observed fixtures")


if __name__ == "__main__":
    main()
