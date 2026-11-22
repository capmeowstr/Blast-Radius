"""Record direct source-file and package-directory reads by unmodified tests."""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path.cwd()
OUT = Path(os.environ["READ_TRACE_OUTPUT"])
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
ACTIVE = ""
READS = defaultdict(set)
DIRECTORIES = defaultdict(set)


def audit(event, args):
    if not ACTIVE or event not in ("open", "os.listdir", "os.scandir"):
        return
    path = args[0]
    if not isinstance(path, str):
        return
    absolute = os.path.abspath(path)
    if not absolute.startswith(str(ROOT) + os.sep):
        return
    relative = os.path.relpath(absolute, ROOT)
    if not relative.startswith(("sqlglot/", "tests/")):
        return
    if event == "open":
        if not relative.endswith(".py"):
            return
        # Import machinery source reads are covered by import dependencies.
        caller = sys._getframe(1).f_code.co_filename
        if caller.startswith("<frozen importlib."):
            return
        READS[relative].add(ACTIVE)
    else:
        DIRECTORIES[relative].add(ACTIVE)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global ACTIVE
    ACTIVE = item.nodeid
    try:
        return (yield)
    finally:
        ACTIVE = ""


def pytest_sessionstart(session):
    if hasattr(session.config, "workerinput") or not session.config.getoption("numprocesses", 0):
        sys.addaudithook(audit)


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput") or not session.config.getoption("numprocesses", 0):
        output = OUT / (WORKER + ".json")
        output.write_text(json.dumps({
            "source": {path: sorted(names) for path, names in READS.items()},
            "directories": {path: sorted(names) for path, names in DIRECTORIES.items()},
        }))
