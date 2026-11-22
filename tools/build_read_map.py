"""Merge direct baseline source-read observations."""

import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path


def main():
    trace, output = map(Path, sys.argv[1:])
    reads = {kind: defaultdict(set) for kind in ("source", "directories")}
    for file in sorted(trace.glob("*.json")):
        record = json.loads(file.read_text())
        for kind, paths in record.items():
            for path, tests in paths.items():
                reads[kind][path].update(tests)
    result = {
        kind: {path: sorted(names) for path, names in sorted(paths.items())}
        for kind, paths in reads.items()
    }
    output.write_bytes(gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0))


if __name__ == "__main__":
    main()
