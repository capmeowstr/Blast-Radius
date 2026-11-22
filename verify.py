"""Run selected sqlglot tests against a patched tree and report which fail.

The selector proposes a safe superset of tests. Running that superset on the
patched source lets us print only the tests that actually fail, so a change no
test catches costs CPU time instead of simulated CI time.

Soundness rules (a wrong "pass" is a 300 s miss, a wrong "fail" only costs time):
- A test counts as passing only if it started, finished, and reported no
  error, failure, failed subtest or unexpected success.
- Anything not finished when the child exits or hits its deadline is failing.
- If a test module fails to import, every selected test in it is failing.
- Tests whose outcome depends on timing, memory or giant runtimes are never run.
"""

import importlib
import json
import os
import pickle
import shutil
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path

NEVER_RUN = frozenset({
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
    "tests/test_schema.py::TestSchema::test_cache_does_not_retain_queries",
})
# Rough CPU model, in milliseconds: child start + sqlglot import, then about the
# recorded duration of each test (recorded on the labelling machine).
OVERHEAD_MS = 800   # measured in the grader-like container: ~0.75-0.83 s per child
TEST_FACTOR = 1.0


def estimate_cpu_ms(durations_ms):
    """Child CPU model fitted on the sample in the grader-like container:
    long tests cost about their recorded time; short ones much less (their
    recorded time is mostly pytest's per-test overhead, which we skip)."""
    return OVERHEAD_MS + sum(0.9 * t if t > 200 else 0.4 * t for t in durations_ms)
# Chance that every runnable selected test passes. Fixture edits change expected
# output, so the tests reading them almost always fail: verifying them only burns
# CPU. This only decides whether to spend CPU, never which tests are printed.
P_PASS_DEFAULT = 0.6
P_PASS_FIXTURE_ONLY = 0.0
LAST_CPU = [0.0]  # CPU seconds of the most recent child (fork path only)


class InlinePool:
    """Stand-in for ProcessPoolExecutor that keeps the process boundary's
    pickling but runs work in-process, so no interpreter is spawned."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            # Worker initialisers mutate worker globals; can't be emulated safely.
            raise RuntimeError("InlinePool cannot emulate a worker initializer")

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as error:  # noqa: BLE001 - mirrored into the future
            future.set_exception(error)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False


def build_tree(base, workdir, files):
    """Mirror `base` into `workdir` with symlinks, writing only changed files.

    files: {posix relpath: new text, or None when the patch deletes the file}.
    """
    base = Path(base)
    workdir = Path(workdir)
    materialized = {""}
    for rel in files:
        parts = rel.split("/")
        for depth in range(1, len(parts)):
            materialized.add("/".join(parts[:depth]))
    for directory in sorted(materialized, key=len):
        source = base / directory if directory else base
        target = workdir / directory if directory else workdir
        target.mkdir(parents=True, exist_ok=True)
        if not source.is_dir():
            continue
        changed_stems = {Path(rel).name.rsplit(".", 1)[0] for rel in files
                         if (rel.rpartition("/")[0]) == directory}
        for entry in os.listdir(source):
            rel = f"{directory}/{entry}" if directory else entry
            if rel in materialized or rel in files:
                continue
            if entry == "__pycache__":
                cache = target / "__pycache__"
                cache.mkdir()
                for compiled in os.listdir(source / entry):
                    # Never reuse bytecode of a module whose source changed.
                    if compiled.split(".", 1)[0] not in changed_stems:
                        os.symlink(source / entry / compiled, cache / compiled)
                continue
            os.symlink(source / entry, target / entry)
    for rel, text in files.items():
        if text is not None:
            path = workdir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


class Recorder(unittest.TestResult):
    def __init__(self, stream):
        super().__init__()
        self.stream = stream
        self.current_failed = False

    def _id(self, test):
        return getattr(test, "_blast_id", None)

    def _emit(self, kind, test_id):
        self.stream.write(f"{kind} {test_id}\n")
        self.stream.flush()

    def startTest(self, test):
        super().startTest(test)
        self.current_failed = False

    def _fail(self, test):
        test_id = self._id(test)
        if test_id is None:
            # setUpClass / tearDownClass / setUpModule errors arrive as
            # _ErrorHolder objects: fail every selected test they could affect.
            self._emit("CLASSFAIL", str(test))
        else:
            self.current_failed = True
            self._emit("FAIL", test_id)

    def addError(self, test, err):
        self._fail(test)

    def addFailure(self, test, err):
        self._fail(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._fail(test)

    def addUnexpectedSuccess(self, test):
        self._fail(test)

    def stopTest(self, test):
        super().stopTest(test)
        test_id = self._id(test)
        if test_id is not None:
            self._emit("DONE", test_id)


def child_main(workdir, test_ids, stream):
    """Run inside the forked/spawned child. Writes result lines to stream."""
    import concurrent.futures
    import concurrent.futures.process

    concurrent.futures.ProcessPoolExecutor = InlinePool
    concurrent.futures.process.ProcessPoolExecutor = InlinePool
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(workdir)
    sys.path.insert(0, workdir)
    for name in list(sys.modules):
        if name == "sqlglot" or name.startswith(("sqlglot.", "tests")):
            del sys.modules[name]
    suite = unittest.TestSuite()
    by_file = {}
    for test_id in test_ids:
        by_file.setdefault(test_id.split("::", 1)[0], []).append(test_id)
    for file, ids in by_file.items():
        module_name = file[:-3].replace("/", ".")
        try:
            module = importlib.import_module(module_name)
        except BaseException:  # noqa: BLE001 - import failure fails the file
            for test_id in ids:
                stream.write(f"FAIL {test_id}\n")
            stream.flush()
            continue
        for test_id in ids:
            _, cls_name, method = test_id.split("::")
            try:
                test = getattr(module, cls_name)(method)
            except BaseException:  # noqa: BLE001
                stream.write(f"FAIL {test_id}\n")
                continue
            test._blast_id = test_id
            suite.addTest(test)
    stream.flush()
    suite.run(Recorder(stream))


def _child_entry():
    spec = json.loads(sys.stdin.read())
    child_main(spec["workdir"], spec["tests"], sys.stdout)


def run_tests(base, files, test_ids, deadline_s):
    """Return the subset of test_ids that FAILED (or could not be confirmed)."""
    if not test_ids:
        return set()
    test_ids = list(test_ids)
    scratch = Path(tempfile.mkdtemp(prefix="blast-", dir=os.environ.get("TMPDIR", "/tmp")))
    workdir = scratch / "tree"
    try:
        try:
            build_tree(base, workdir, files)
        except OSError:
            # No symlinks (e.g. Windows without developer mode): full copy.
            shutil.rmtree(workdir, ignore_errors=True)
            shutil.copytree(base, workdir, ignore=shutil.ignore_patterns("__pycache__", ".git"))
            for rel, text in files.items():
                path = workdir / rel
                if text is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
        lines = _run_child(str(workdir), test_ids, deadline_s)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    failed, done, class_failures = set(), set(), []
    for line in lines:
        kind, _, value = line.partition(" ")
        if kind == "FAIL":
            failed.add(value)
        elif kind == "DONE":
            done.add(value)
        elif kind == "CLASSFAIL":
            class_failures.append(value)
    for holder in class_failures:
        # "setUpClass (tests.test_x.TestX)" or "setUpModule (tests.test_x)".
        target = holder.rpartition("(")[2].rstrip(")")
        for test_id in test_ids:
            file, cls_name, _ = test_id.split("::")
            module = file[:-3].replace("/", ".")
            if target in (module, f"{module}.{cls_name}"):
                failed.add(test_id)
    unconfirmed = {test_id for test_id in test_ids if test_id not in done}
    return failed | unconfirmed


def _run_child(workdir, test_ids, deadline_s):
    if hasattr(os, "fork"):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            os.close(read_fd)
            code = 0
            try:
                with os.fdopen(write_fd, "w", buffering=1) as stream:
                    sys.stdout = sys.stderr  # tests must never write to our stdout
                    child_main(workdir, test_ids, stream)
            except BaseException:  # noqa: BLE001
                code = 1
            finally:
                os._exit(code)
        os.close(write_fd)
        return _collect(read_fd, pid, deadline_s)
    import subprocess

    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", __file__, "--child"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        out, _ = process.communicate(json.dumps({"workdir": workdir, "tests": test_ids}), timeout=deadline_s)
    except subprocess.TimeoutExpired:
        process.kill()
        out, _ = process.communicate()
    return out.splitlines()


def _collect(read_fd, pid, deadline_s):
    import select
    import signal

    chunks = []
    end = time.monotonic() + deadline_s
    with os.fdopen(read_fd, "r") as stream:
        fd = stream.fileno()
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                os.kill(pid, signal.SIGKILL)
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            data = os.read(fd, 65536)
            if not data:
                break
            chunks.append(data)
    _, _, usage = os.wait4(pid, 0)
    LAST_CPU[0] = usage.ru_utime + usage.ru_stime
    return b"".join(chunks).decode("utf-8", "replace").splitlines()


if __name__ == "__main__" and "--child" in sys.argv:
    _child_entry()
