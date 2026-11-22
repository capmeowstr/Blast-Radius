"""Load a compact manifest and decompress source data by file on demand."""

import ast
import gzip
import json
import marshal
import struct
import zlib

MAGIC = b"BRM3"
HEADER_SIZE = 8


class Files:
    def __init__(self, payload, positions, base):
        self.payload = payload
        self.positions = positions
        self.base = base
        self.entries = {}

    def __contains__(self, path):
        return path in self.positions

    def __getitem__(self, path):
        if path not in self.entries:
            offset, length = self.positions[path]
            start = self.base + offset
            self.entries[path] = marshal.loads(zlib.decompress(self.payload[start:start + length]))
        return self.entries[path]


def read_coverage(path):
    data = path.read_bytes()
    if not data.startswith(MAGIC):
        return json.loads(gzip.decompress(data))
    offset = HEADER_SIZE + struct.unpack_from("<I", data, 4)[0]
    names, fixtures, positions = marshal.loads(data[HEADER_SIZE:offset])
    return {
        "tests": names, "fixtures": fixtures, "files": Files(data, positions, offset),
    }


def evaluation_spans(source):
    """Locate expressions whose earlier operands can short-circuit."""
    tree = ast.parse(source) if isinstance(source, str) else source
    spans = set()
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


def make_index(coverage):
    payload = bytearray()
    positions = {}
    for path, (source, scopes, patterns, lines) in coverage["files"].items():
        new_patterns = [int(pattern, 16) for pattern in patterns]
        member = marshal.dumps((source, scopes, new_patterns, lines, tuple(sorted(evaluation_spans(source)))))
        block = zlib.compress(member, level=8)
        positions[path] = len(payload), len(block)
        payload.extend(block)
    metadata = marshal.dumps((coverage["tests"], coverage["fixtures"], positions))
    return MAGIC + struct.pack("<I", len(metadata)) + metadata + payload
