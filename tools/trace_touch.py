"""Record, per pytest test, which sqlglot classes it used and which sqlglot
functions ran during its setup phase. Run on the untouched pinned checkout:

    TOUCH_OUT=/abs/dir pytest tests -p trace_touch -n 4 -p no:cacheprovider

Class use is observed through the receiver (`self` / `cls`) of every sqlglot
function call, recorded with the receiver's whole MRO, so an edit to a class
body can be mapped to every test that used that class or a subclass. Setup
phase calls cover setUpClass / setUpModule / fixtures: a change there can break
every test that shares the setup, not just the first one that triggered it.
"""
import json
import os
import sys
import threading
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path.cwd()
PACKAGE = str(ROOT / "sqlglot") + os.sep
OUT = Path(os.environ["TOUCH_OUT"])
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")

ACTIVE = ""
PHASE = ""
SEEN = set()
CLASSES = defaultdict(set)   # "module:qualname" -> nodeids
SETUP = defaultdict(set)     # "relpath:firstlineno" -> nodeids whose setup ran it
RELPATHS = {}


def relpath(filename):
    path = RELPATHS.get(filename)
    if path is None:
        path = RELPATHS[filename] = Path(filename).resolve().relative_to(ROOT).as_posix()
    return path


def profile(frame, event, arg):
    if event != "call" or not ACTIVE:
        return
    code = frame.f_code
    filename = code.co_filename
    if not filename.startswith(PACKAGE):
        return
    if PHASE == "setup":
        SETUP[relpath(filename) + ":" + str(code.co_firstlineno)].add(ACTIVE)
    if not code.co_argcount:
        return
    name = code.co_varnames[0]
    if name not in ("self", "cls"):
        return
    try:
        receiver = frame.f_locals[name]
    except Exception:
        return
    kind = receiver if isinstance(receiver, type) else type(receiver)
    if kind in SEEN:
        return
    SEEN.add(kind)
    for base in getattr(kind, "__mro__", ()):
        module = getattr(base, "__module__", "") or ""
        if module.startswith("sqlglot"):
            CLASSES[module + ":" + base.__qualname__].add(ACTIVE)


def pytest_sessionstart(session):
    if hasattr(session.config, "workerinput") or not session.config.getoption("numprocesses", None):
        sys.setprofile(profile)
        threading.setprofile(profile)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global ACTIVE
    ACTIVE = item.nodeid
    SEEN.clear()
    try:
        return (yield)
    finally:
        ACTIVE = ""


def _phase(name):
    @pytest.hookimpl(wrapper=True)
    def hook(item, *args, **kwargs):
        global PHASE
        PHASE = name
        try:
            return (yield)
        finally:
            PHASE = ""
    return hook


pytest_runtest_setup = _phase("setup")
pytest_runtest_call = _phase("call")
pytest_runtest_teardown = _phase("teardown")


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput") or not session.config.getoption("numprocesses", None):
        sys.setprofile(None)
        threading.setprofile(None)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / (WORKER + ".json")).write_text(json.dumps({
            "classes": {key: sorted(ids) for key, ids in CLASSES.items()},
            "setup": {key: sorted(ids) for key, ids in SETUP.items()},
        }))
