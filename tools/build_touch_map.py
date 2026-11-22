"""Merge trace_touch worker outputs into maps/touch.json.gz.

    python tools/build_touch_map.py TOUCH_OUT maps/touch.json.gz
"""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path


def main():
    trace, output = Path(sys.argv[1]), Path(sys.argv[2])
    classes = defaultdict(set)
    setup = defaultdict(set)
    for record in sorted(trace.glob("*.json")):
        report = json.loads(record.read_text())
        for key, ids in report["classes"].items():
            classes[key].update(ids)
        for key, ids in report["setup"].items():
            setup[key].update(ids)
    names = sorted({name for group in (classes, setup) for ids in group.values() for name in ids})
    index = {name: i for i, name in enumerate(names)}
    built = {
        "tests": names,
        "classes": {key: sorted(index[n] for n in ids) for key, ids in sorted(classes.items())},
        "setup": {key: sorted(index[n] for n in ids) for key, ids in sorted(setup.items())},
    }
    output.write_bytes(gzip.compress(json.dumps(built, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(names)} tests, {len(classes)} classes, {len(setup)} setup functions")


if __name__ == "__main__":
    main()
