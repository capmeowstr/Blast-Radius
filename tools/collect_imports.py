"""Record pinned SQLGlot code executed before pytest finishes collecting tests."""
import argparse
import ast
from collections import defaultdict
import gzip
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    prefix = str(source / "sqlglot") + os.sep

    # Resolve relative filenames exactly once for each code object.
    filenames = {}
    lines = defaultdict(set)
    tool = sys.monitoring.COVERAGE_ID
    sys.monitoring.use_tool_id(tool, "blast-radius-import-lines")

    def on_line(code, line):
        filename = code.co_filename
        if filename not in filenames:
            absolute = os.path.abspath(filename)
            filenames[filename] = os.path.relpath(absolute, source) if absolute.startswith(prefix) else ""
        relative = filenames[filename]
        if relative:
            lines[relative].add(line)

    os.chdir(source)
    sys.path.insert(0, str(source))
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, on_line)
    sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
    try:
        import pytest

        status = pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", "tests"])
    finally:
        sys.monitoring.set_events(tool, 0)
        sys.monitoring.free_tool_id(tool)
    if status != 0:
        return status
    scopes = {}
    for path, used in sorted(lines.items()):
        tree = ast.parse((source / path).read_bytes())
        active = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(node.body[0].lineno <= line <= node.end_lineno for line in used):
                    active.append([node.lineno, node.end_lineno])
        if active:
            scopes[path] = sorted(active)
    data = json.dumps(scopes, separators=(",", ":")).encode()
    output.write_bytes(gzip.compress(data, mtime=0))
    print(f"{len(scopes)} source files contain functions executed during collection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
