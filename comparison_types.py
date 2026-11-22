"""Prove equality and identity interchangeable for observed operand types."""

import ast
import gzip
import json

POSITIVE = frozenset((ast.Is, ast.Eq))
NEGATIVE = frozenset((ast.IsNot, ast.NotEq))
OBJECT = "builtins.object"
NONE = "builtins.NoneType"
BOOL = "builtins.bool"
BUILTINS = frozenset(f"builtins.{kind}" for kind in (
    "bool", "int", "float", "complex", "str", "bytes", "tuple", "list", "set", "frozenset", "dict",
))


def equivalent(pair, negative):
    left, right, same_type = pair
    left_name, left_eq, left_ne, left_enum = left
    right_name, right_eq, right_ne, right_enum = right

    if left_eq == right_eq == OBJECT and (not negative or left_ne == right_ne == OBJECT):
        return True
    if same_type and left_name == right_name and left_name in (NONE, BOOL):
        return True
    if left_name == NONE or right_name == NONE:
        other_name, other_eq, other_ne, _ = right if left_name == NONE else left
        # These exact builtin types reject comparisons with None before touching their contents.
        return other_name in BUILTINS or (other_eq == OBJECT and (not negative or other_ne == OBJECT))
    # The collector checks that both operands are canonical members and that
    # equality on the stored primitive representation is injective for that enum.
    return same_type and left_enum and right_enum


def changed_sites(before, after):
    """Return old comparison positions only if the entire AST fits the proof."""
    sites = []
    restored = []
    try:
        for left, right in zip(ast.walk(before), ast.walk(after)):
            if not isinstance(left, ast.Compare) or not isinstance(right, ast.Compare):
                continue
            if len(left.ops) != 1 or len(right.ops) != 1:
                continue
            change = frozenset((type(left.ops[0]), type(right.ops[0])))
            if change in (POSITIVE, NEGATIVE):
                if isinstance(right.ops[0], (ast.Is, ast.IsNot)) and any(
                    isinstance(node, ast.Constant) and node.value is not None and type(node.value) is not bool
                    for node in (right.left, right.comparators[0])
                ):
                    continue
                sites.append((left, change == NEGATIVE))
                restored.append((right, right.ops[0]))
                right.ops[0] = left.ops[0]
        return sites if sites and ast.dump(before) == ast.dump(after) else ()
    finally:
        for node, operation in restored:
            node.ops[0] = operation


class ComparisonTypes:
    def __init__(self, path, test_count):
        self.path = path
        self.info = None
        self.import_time = 1 << test_count

    def candidates(self, path, before, after, candidates, lines):
        sites = changed_sites(before, after)
        if not sites:
            return candidates
        if self.info is None:
            with gzip.open(self.path, "rt", encoding="utf-8") as source:
                self.info = json.load(source)
        info = self.info
        keep = candidates & ~int(info["passed"], 16)
        for site, negative in sites:
            key = f"{path}:{site.lineno}:{site.col_offset}"
            facts = info["sites"].get(key)
            if not facts:
                return candidates
            observed = 0
            unsafe = 0
            for pair, mask in facts:
                shape_pair = info["pairs"][pair]
                if "types" in info:
                    left, right, same_type = shape_pair
                    shape_pair = info["types"][left], info["types"][right], same_type
                    mask = info["masks"][mask]
                context = int(mask, 16)
                observed |= context
                if not equivalent(shape_pair, negative):
                    unsafe |= context
            if unsafe & self.import_time:
                return candidates
            scope = 0
            for line in range(site.lineno, site.end_lineno + 1):
                scope |= lines.get(line, 0)
            # Keep covered evaluations when operand data is incomplete. Any
            # baseline import-scope call must also have a valid type proof.
            keep |= unsafe | (candidates & scope & ~observed)
        return keep
