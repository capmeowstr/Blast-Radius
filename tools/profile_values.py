"""Collect immutable operand observations from the unchanged sqlglot checkout.

Run pytest with this plugin, VALUE_OUT set, and the repository on PYTHONPATH.
Run this file on the resulting directory to merge the observations.
"""

import ast
import atexit
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import gzip
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys

from value_profile import OPERATORS, UNKNOWN, freeze, literal, operands, position


ROOT = Path.cwd().resolve()
OUTPUT = os.environ.get("VALUE_OUT")
MAX_STATES = 6
PROBE = "_blast_profile_operand_"
ACTIVE = ""
STATES = defaultdict(dict)
ORIGINAL_SUBMIT = ProcessPoolExecutor.submit
OPERATOR_NAMES = {kind.__name__: function for kind, function in OPERATORS.items()}
CHILD_PATCHED = False
PASSED = set()
FAILED = set()


def record(location, name, left, right):
    tests = STATES[location]
    observations = tests.setdefault(ACTIVE, set() if ACTIVE else None)
    if observations is not None:
        left_value, right_value = freeze(left), freeze(right)
        if left_value is UNKNOWN or right_value is UNKNOWN:
            tests[ACTIVE] = None
        else:
            observations.add((left_value, right_value, left is right))
            if len(observations) > MAX_STATES:
                tests[ACTIVE] = None
    return OPERATOR_NAMES[name](left, right)


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_eager(self, node):
        measured = operands(node)
        if measured is None:
            return self.generic_visit(node)
        left, op, right = measured
        if op not in OPERATORS or all(literal(item) is UNKNOWN for item in (left, right)):
            return self.generic_visit(node)
        key = f"{self.path}:{position(node)}"
        return ast.copy_location(
            ast.Call(ast.Name(PROBE, ast.Load()), [
                ast.Constant(key), ast.Constant(op.__name__), self.visit(left), self.visit(right),
            ], []),
            node,
        )

    visit_Compare = visit_eager
    visit_BinOp = visit_eager


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = Path(self.path).relative_to(ROOT).as_posix()
        tree = ast.parse(self.get_data(self.path), self.path)
        tree = Instrument(path).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__[PROBE] = record
        super().exec_module(module)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            if Path(spec.origin).is_relative_to(ROOT / "sqlglot"):
                spec.loader = Loader(fullname, spec.origin)
        return spec


def write_report():
    if not STATES and not PASSED:
        return
    report = Path(OUTPUT) / f"operands-{os.getpid()}.json"
    payload = {
        key: {nodeid: sorted(values, key=repr) if values is not None else None for nodeid, values in tests.items()}
        for key, tests in STATES.items()
    }
    report.write_text(json.dumps({"sites": payload, "passed": sorted(PASSED - FAILED)},
                                 separators=(",", ":")), encoding="utf-8")


def trace_job(nodeid, function, args, kwargs):
    global ACTIVE, CHILD_PATCHED
    if not CHILD_PATCHED:
        from concurrent.futures import process
        from multiprocessing.util import Finalize

        CHILD_PATCHED = True
        Finalize(None, write_report, exitpriority=2)
        original = process._sendback_result

        def send_result(*result_args, **result_kwargs):
            global ACTIVE
            try:
                return original(*result_args, **result_kwargs)
            finally:
                ACTIVE = ""

        process._sendback_result = send_result
    ACTIVE = nodeid
    return function(*args, **kwargs)


def traced_submit(pool, function, /, *args, **kwargs):
    if ACTIVE:
        return ORIGINAL_SUBMIT(pool, trace_job, ACTIVE, function, args, kwargs)
    return ORIGINAL_SUBMIT(pool, function, *args, **kwargs)


def merge(source, destination):
    sites = defaultdict(lambda: defaultdict(set))
    unscoped = set()
    passed = set()
    for report in sorted(Path(source).glob("operands-*.json")):
        result = json.loads(report.read_text(encoding="utf-8"))
        passed.update(result["passed"])
        for site, tests in result["sites"].items():
            for nodeid, values in tests.items():
                if not nodeid:
                    unscoped.add(site)
                    continue
                previous = sites[site][nodeid]
                if values is None:
                    sites[site][nodeid] = None
                elif previous is not None:
                    previous.update(json.dumps(value, separators=(",", ":")) for value in values)
                    if len(previous) > MAX_STATES:
                        sites[site][nodeid] = None
    compact = {}
    for site, tests in sorted(sites.items()):
        if site in unscoped or not any(tests.values()):
            continue
        rows = defaultdict(list)
        for nodeid, observations in sorted(tests.items()):
            if nodeid not in passed:
                continue
            for observation in sorted(observations or {"[null,null,false]"}):
                rows[observation].append(nodeid)
        compact[site] = [[*json.loads(value), names] for value, names in sorted(rows.items())]
    payload = json.dumps(compact, separators=(",", ":")).encode()
    Path(destination).write_bytes(gzip.compress(payload, mtime=0))
    print(f"Compiled {len(compact)} baseline operand sites")


if __name__ == "__main__":
    merge(*sys.argv[1:])
elif OUTPUT:
    import pytest

    sys.meta_path.insert(0, Finder())
    ProcessPoolExecutor.submit = traced_submit
    atexit.register(write_report)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(item, nextitem):
        global ACTIVE
        ACTIVE = item.nodeid
        try:
            return (yield)
        finally:
            ACTIVE = ""

    def pytest_sessionfinish(session, exitstatus):
        write_report()

    def pytest_runtest_logreport(report):
        if not ACTIVE or report.nodeid != ACTIVE:
            return
        if report.failed or report.skipped:
            FAILED.add(ACTIVE)
        elif report.when == "call" and report.passed and type(report).__name__ != "SubtestReport":
            PASSED.add(ACTIVE)
