"""Record which baseline pytest setup scopes consume files."""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path.cwd()
OUTPUT = Path(os.environ["FIXTURE_SCOPE_OUTPUT"])
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
ACTIVE_SETUP_SCOPE = None
READS = defaultdict(set)


def audit(event, args):
    if event != "open" or ACTIVE_SETUP_SCOPE is None:
        return
    path, mode, flags = args
    if not isinstance(path, str):
        return
    if flags & os.O_ACCMODE == os.O_WRONLY:
        return
    relative = os.path.relpath(os.path.abspath(path), ROOT)
    if relative.startswith("tests/fixtures/"):
        READS[relative].add(ACTIVE_SETUP_SCOPE)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    global ACTIVE_SETUP_SCOPE
    previous = ACTIVE_SETUP_SCOPE
    ACTIVE_SETUP_SCOPE = request.node.nodeid
    try:
        return (yield)
    finally:
        ACTIVE_SETUP_SCOPE = previous


def pytest_sessionstart(session):
    if hasattr(session.config, "workerinput") or not session.config.getoption("numprocesses", 0):
        sys.addaudithook(audit)


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput") or not session.config.getoption("numprocesses", 0):
        output = OUTPUT / f"{WORKER}.json"
        output.write_text(json.dumps({
            path: sorted(scopes) for path, scopes in sorted(READS.items())
        }, separators=(",", ":")))
