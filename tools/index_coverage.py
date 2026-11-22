"""Convert a pinned JSON coverage map into indexed compressed source blocks."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from indexed_coverage import make_index, read_coverage


if __name__ == "__main__":
    source, target = map(Path, sys.argv[1:])
    target.write_bytes(make_index(read_coverage(source)))
