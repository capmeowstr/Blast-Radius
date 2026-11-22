"""Collect per-test operand type facts from the untouched SQLGlot checkout."""

import ast
import atexit
import importlib.machinery
import json
import operator
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from enum import Enum
from pathlib import Path

import pytest

ROOT = Path(os.environ["TYPE_TRACE_ROOT"]).resolve()
OUTPUT = Path(os.environ["TYPE_TRACE_OUTPUT"]).resolve()
FACTS = defaultdict(lambda: defaultdict(set))
OPERATIONS = {"Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq, "NotEq": operator.ne}
PROBE = "_blast_observe_comparison_types"
ACTIVE = ""
PASSED = set()
SUBMIT = ProcessPoolExecutor.submit
CHILD = False
BUILTIN_SHAPES = {}
CLASS_SHAPES = {}
CHUNK = 0
COMPLETED = 0
CALL_PASSED = False
RUN_FAILED = False
BUILTINS = (type(None), bool, int, float, str, bytes, complex, tuple, list, set, frozenset, dict)


def descriptor_owner(cls, name):
    for base in type.__getattribute__(cls, "__mro__"):
        namespace = type.__getattribute__(base, "__dict__")
        if name in namespace:
            return base, namespace[name]


def symbol(cls):
    return f'{type.__getattribute__(cls, "__module__")}.{type.__getattribute__(cls, "__qualname__")}'


def builtin_shape(cls):
    return (
        symbol(cls),
        symbol(descriptor_owner(cls, "__eq__")[0]),
        symbol(descriptor_owner(cls, "__ne__")[0]),
        False,
    )


for _builtin in BUILTINS:
    BUILTIN_SHAPES[id(_builtin)] = builtin_shape(_builtin)


def canonical_enum_member(value, cls, base):
    # Two values with equal primitive representations have the same key.
    # The canonical enum's reverse map can only resolve that key to one object.
    members = type.__getattribute__(cls, "_value2member_map_")
    extract = str.__str__ if base is str else int.__int__
    return type(members) is dict and members.get(extract(value)) is value


def shape(value):
    cls = type(value)
    builtin = BUILTIN_SHAPES.get(id(cls))
    if builtin is not None:
        return builtin
    equal = type.__getattribute__(cls, "__eq__")
    unequal = type.__getattribute__(cls, "__ne__")
    cached = CLASS_SHAPES.get(id(cls))
    if cached is None or cached[1] is not equal or cached[2] is not unequal:
        eq, _ = descriptor_owner(cls, "__eq__")
        ne, _ = descriptor_owner(cls, "__ne__")
        enum_base = None
        if (eq is str or eq is int) and ne is eq and equal is eq.__eq__ and unequal is eq.__ne__:
            if any(base is Enum for base in type.__getattribute__(cls, "__mro__")):
                enum_base = eq
        description = symbol(cls), symbol(eq), symbol(ne)
        cached = cls, equal, unequal, enum_base, (*description, False), (*description, True)
        CLASS_SHAPES[id(cls)] = cached
    enum_base = cached[3]
    if enum_base and canonical_enum_member(value, cls, enum_base):
        return cached[5]
    return cached[4]


def observe(site, operation, left, right):
    pair = shape(left), shape(right), type(left) is type(right)
    FACTS[site][ACTIVE].add(pair)
    return operation(left, right)


def dump():
    global CHUNK
    path = OUTPUT / f"types-{os.getpid()}-{CHUNK}.json"
    payload = {
        "passed": sorted(PASSED),
        "sites": {
            site: {context: sorted(facts) for context, facts in contexts.items()}
            for site, contexts in FACTS.items()
        },
    }
    path.write_text(json.dumps(payload, separators=(",", ":")))
    FACTS.clear()
    PASSED.clear()
    CHUNK += 1


def trace_job(context, function, args, kwargs):
    global ACTIVE, CHILD
    if not CHILD:
        from concurrent.futures import process
        from multiprocessing.util import Finalize

        Finalize(None, dump, exitpriority=1)
        send_result = process._sendback_result

        def finish_result(*args, **kwargs):
            global ACTIVE
            try:
                return send_result(*args, **kwargs)
            finally:
                ACTIVE = ""

        process._sendback_result = finish_result
        CHILD = True
    ACTIVE = context
    return function(*args, **kwargs)


def tracked_submit(pool, function, /, *args, **kwargs):
    if ACTIVE:
        return SUBMIT(pool, trace_job, ACTIVE, function, args, kwargs)
    return SUBMIT(pool, function, *args, **kwargs)


class Instrumentation(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        name = type(node.ops[0]).__name__
        if name not in OPERATIONS:
            return node
        right = node.comparators[0]
        if name in ("Eq", "NotEq") and not any(
            isinstance(operand, (ast.Constant, ast.Attribute)) for operand in (node.left, right)
        ):
            return node
        site = f"{self.path}:{node.lineno}:{node.col_offset}"
        probe = ast.Call(
            ast.Name(PROBE, ast.Load()),
            [ast.Constant(site), ast.Name(PROBE + name, ast.Load()), node.left, right],
            [],
        )
        return ast.copy_location(probe, node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        relative = Path(self.path).relative_to(ROOT).as_posix()
        tree = Instrumentation(relative).visit(ast.parse(self.get_data(self.path), self.path))
        return compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__[PROBE] = observe
        for name, operation in OPERATIONS.items():
            module.__dict__[PROBE + name] = operation
        super().exec_module(module)


class Finder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = Loader(fullname, spec.origin)
        return spec


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global ACTIVE, CALL_PASSED, RUN_FAILED
    ACTIVE = item.nodeid
    CALL_PASSED = False
    RUN_FAILED = False
    try:
        return (yield)
    finally:
        ACTIVE = ""


def pytest_runtest_logreport(report):
    global CALL_PASSED, COMPLETED, RUN_FAILED
    if not ACTIVE:
        return
    RUN_FAILED |= report.failed
    if report.when == "call":
        CALL_PASSED = report.passed
    if report.when == "teardown":
        if report.passed and CALL_PASSED and not RUN_FAILED:
            PASSED.add(report.nodeid)
        COMPLETED += 1
        if COMPLETED % 100 == 0:
            dump()


sys.meta_path.insert(0, Finder())
ProcessPoolExecutor.submit = tracked_submit
atexit.register(dump)
