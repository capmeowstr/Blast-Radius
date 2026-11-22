"""Refine isolated pure-expression changes using inputs from the pinned tests."""

import ast
import gzip
import json
import operator
from pathlib import Path


OPERATORS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
}
IDENTITY = (ast.Is, ast.IsNot)
INTEGERS = (bool, int)
UNKNOWN = object()


def freeze(value):
    """Keep exact builtin types and bound the size of observed inputs."""
    if value is None:
        return ("none",)
    kind = type(value)
    if kind is bool:
        return ("bool", value)
    if kind is int and value.bit_length() <= 128:
        return ("int", value)
    if kind is str and len(value) <= 64:
        return ("str", value)
    if kind is tuple and len(value) <= 6 and all(type(item) in INTEGERS for item in value):
        items = tuple(freeze(item) for item in value)
        if all(item is not UNKNOWN for item in items):
            return ("tuple", items)
    return UNKNOWN


def thaw(value):
    if value[0] == "none":
        return None
    if value[0] == "tuple":
        return tuple(thaw(item) for item in value[1])
    return value[1]


def literal(expr):
    if isinstance(expr, ast.Constant):
        return freeze(expr.value)
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.USub):
        if isinstance(expr.operand, ast.Constant) and type(expr.operand.value) is int:
            return freeze(-expr.operand.value)
    if isinstance(expr, ast.Tuple) and all(
        isinstance(item, ast.Constant) and type(item.value) in INTEGERS for item in expr.elts
    ):
        return freeze(tuple(item.value for item in expr.elts))
    return UNKNOWN


def operands(node):
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        return node.left, type(node.ops[0]), node.comparators[0]
    if isinstance(node, ast.BinOp):
        return node.left, type(node.op), node.right
    return None


def position(node):
    return f"{node.lineno}:{node.col_offset}"


def equivalent_operands(before, after):
    """Return revised literal replacements for an isolated eager operation."""
    old = operands(before)
    new = operands(after)
    if type(before) is not type(after) or old is None or new is None:
        return None
    if old[1] not in OPERATORS or new[1] not in OPERATORS:
        return None
    retained = (ast_equal(old[0], new[0]), ast_equal(old[2], new[2]))
    originals = (literal(old[0]), literal(old[2]))
    replacements = (literal(new[0]), literal(new[2]))
    if all(item is UNKNOWN for item in originals):
        return None
    if not all(
        kept or (original is not UNKNOWN and replacement is not UNKNOWN)
        for kept, original, replacement in zip(retained, originals, replacements)
    ):
        return None
    if (old[1] in IDENTITY or new[1] in IDENTITY) and not all(retained):
        return None
    if new[1] in IDENTITY and any(
        isinstance(node, ast.Constant) and node.value is not None and type(node.value) is not bool
        for node in (new[0], new[2])
    ):
        return None
    return old[1], new[1], tuple(UNKNOWN if kept else item for kept, item in zip(retained, replacements))


def ast_equal(left, right):
    if hasattr(ast, "compare"):
        return ast.compare(left, right)
    return ast.dump(left) == ast.dump(right)


def candidate_change(before, after, lines):
    """Locate one operation change while requiring the rest of the AST to match."""
    originals = {
        position(node): node for node in ast.walk(before)
        if isinstance(node, (ast.Compare, ast.BinOp))
        and any(node.lineno <= line <= node.end_lineno for line in lines)
    }
    if not originals:
        return None
    for parent in ast.walk(after):
        for field, child in ast.iter_fields(parent):
            nodes = child if isinstance(child, list) else [child]
            for index, node in enumerate(nodes):
                if not isinstance(node, (ast.Compare, ast.BinOp)):
                    continue
                old = originals.get(position(node))
                if old is None or ast_equal(old, node):
                    continue
                model = equivalent_operands(old, node)
                if model is None:
                    continue
                if isinstance(child, list):
                    child[index] = old
                    exact = ast_equal(before, after)
                    child[index] = node
                else:
                    setattr(parent, field, old)
                    exact = ast_equal(before, after)
                    setattr(parent, field, node)
                if exact:
                    return position(old), model
    return None


def same_result(model, state):
    old_op, revised_op, replacements = model
    left, right, identity = state
    original = thaw(left), thaw(right)
    revised = tuple(
        value if updated is UNKNOWN else thaw(updated)
        for value, updated in zip(original, replacements)
    )

    def result(operation, values):
        if operation is ast.Is:
            return identity
        if operation is ast.IsNot:
            return not identity
        if operation not in (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE):
            if any(type(value) not in INTEGERS for value in values):
                return UNKNOWN
        return OPERATORS[operation](*values)

    try:
        before, after = result(old_op, original), result(revised_op, revised)
    except (TypeError, ArithmeticError):
        return False
    if type(before) is not type(after) or before != after:
        return False
    # Boolean comparisons return singletons. CPython also interns these integers.
    return type(before) is bool or (type(before) is int and -5 <= before <= 256)


class ValueProfile:
    def __init__(self, path):
        self.path = Path(path)
        self.probes = None
        self.observed = None

    def confirmed(self, path, get_before, after, lines):
        if self.probes is None:
            with gzip.open(self.path, "rt", encoding="utf-8") as stream:
                self.probes = json.load(stream)
            self.observed = {}
            for site in self.probes:
                source, line, _ = site.rsplit(":", 2)
                self.observed.setdefault(source, set()).add(int(line))
        eligible = lines & self.observed.get(path, set())
        if not eligible:
            return ()
        candidate = candidate_change(get_before(path), after, eligible)
        if candidate is None:
            return ()
        location, model = candidate
        safe = set()
        unsafe = set()
        for left, right, identity, names in self.probes.get(f"{path}:{location}", ()):
            if left is None or not same_result(model, (left, right, identity)):
                unsafe.update(names)
            else:
                safe.update(names)
        return safe - unsafe
