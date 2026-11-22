"""Build a compact map of raw baseline Python operand shapes and their contexts."""

import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path


def main():
    trace, coverage, destination = map(Path, sys.argv[1:])
    with gzip.open(coverage, "rt", encoding="utf-8") as source:
        names = json.load(source)["tests"]
    masks = {name: 1 << index for index, name in enumerate(names)}
    facts = defaultdict(lambda: defaultdict(int))
    passed = set()
    for path in sorted(trace.glob("types-*.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        passed.update(result["passed"])
        for site, contexts in result["sites"].items():
            for context, shapes in contexts.items():
                mask = masks.get(context, 0)
                for left, right, same_type in shapes:
                    key = tuple(left), tuple(right), same_type
                    facts[site][key] |= mask if context else 1 << len(names)
    pairs = sorted({pair for site in facts.values() for pair in site})
    indexes = {pair: index for index, pair in enumerate(pairs)}
    types = sorted({shape for left, right, _ in pairs for shape in (left, right)})
    type_ids = {shape: index for index, shape in enumerate(types)}
    contexts = sorted({mask for site in facts.values() for mask in site.values()})
    context_ids = {mask: index for index, mask in enumerate(contexts)}
    payload = {
        "types": types,
        "pairs": [[type_ids[left], type_ids[right], same_type] for left, right, same_type in pairs],
        "masks": [format(mask, "x") for mask in contexts],
        "passed": format(sum(masks[name] for name in passed & masks.keys()), "x"),
        "sites": {
            site: [[indexes[pair], context_ids[mask]] for pair, mask in sorted(observed.items())]
            for site, observed in sorted(facts.items())
        },
    }
    destination.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(passed)} passing tests, {len(facts)} observed sites, {len(pairs)} operand type pairs")


if __name__ == "__main__":
    main()
