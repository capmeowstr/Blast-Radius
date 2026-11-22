"""Record coverage by pytest test for the pinned, unchanged source tree."""
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from collections import defaultdict
from pathlib import Path

import coverage
import pytest

ROOT = Path.cwd()
OUT = Path(os.environ["TRACE_OUT"])
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
COV = coverage.Coverage(
    data_file=str(OUT / (".coverage." + WORKER)),
    config_file=False,
    include=[str(ROOT / root / "*") for root in ("sqlglot", "tests")],
)
FIXTURES = defaultdict(set)
ACTIVE = ""
PASSED = set()
CHILD_COVERAGE = None
ORIGINAL_SUBMIT = ProcessPoolExecutor.submit


def report_child():
    CHILD_COVERAGE.stop()
    CHILD_COVERAGE.save()


def trace_job(nodeid, function, args, kwargs):
    global CHILD_COVERAGE
    if CHILD_COVERAGE is None:
        from concurrent.futures import process
        from multiprocessing.util import Finalize

        CHILD_COVERAGE = coverage.Coverage(
            data_file=str(OUT / (".coverage.job-" + str(os.getpid()))),
            config_file=False,
            include=[str(ROOT / root / "*") for root in ("sqlglot", "tests")],
        )
        CHILD_COVERAGE.start()
        Finalize(None, report_child, exitpriority=1)
        send_result = process._sendback_result

        def finish_result(*result_args, **result_kwargs):
            try:
                return send_result(*result_args, **result_kwargs)
            finally:
                CHILD_COVERAGE.switch_context("")

        process._sendback_result = finish_result
    CHILD_COVERAGE.switch_context(nodeid)
    return function(*args, **kwargs)


def tracked_submit(pool, function, /, *args, **kwargs):
    if ACTIVE:
        return ORIGINAL_SUBMIT(pool, trace_job, ACTIVE, function, args, kwargs)
    return ORIGINAL_SUBMIT(pool, function, *args, **kwargs)

def observe_open(event, args):
    if event != "open" or not ACTIVE:
        return
    path = args[0]
    if isinstance(path, str) and "fixtures/" in path:
        path = os.path.relpath(os.path.abspath(path), ROOT)
        if path.startswith("tests/fixtures/"):
            FIXTURES[path].add(ACTIVE)

def pytest_sessionstart(session):
    if hasattr(session.config, "workerinput") or not session.config.getoption("numprocesses"):
        COV.start()
        sys.addaudithook(observe_open)
        ProcessPoolExecutor.submit = tracked_submit

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global ACTIVE
    ACTIVE = item.nodeid
    COV.switch_context(ACTIVE)
    try:
        return (yield)
    finally:
        ACTIVE = ""
        COV.switch_context("")

def pytest_runtest_logreport(report):
    if report.when == "call" and report.passed and ACTIVE:
        PASSED.add(report.nodeid)

def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput") or not session.config.getoption("numprocesses"):
        COV.stop()
        COV.save()
        (OUT / (WORKER + ".json")).write_text(json.dumps({
            "passed": sorted(PASSED),
            "fixtures": {path: sorted(ids) for path, ids in FIXTURES.items()},
        }))
