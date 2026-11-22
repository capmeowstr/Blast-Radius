"""Build a conservative import dependency map from the pinned sources."""

import ast
import gzip
import json
import sys
import importlib.util
from collections import defaultdict
from pathlib import Path


def module_name(path):
    parts = path.with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def import_closures(root):
    """Build ordinary transitive import dependencies from the pinned sources."""
    paths = {
        module_name(path.relative_to(root)): path
        for directory in ("sqlglot", "tests")
        for path in (root / directory).rglob("*.py")
    }
    dependencies = defaultdict(set)

    def register(module, target):
        for end in range(1, len(target.split(".")) + 1):
            dep = ".".join(target.split(".")[:end])
            if dep in paths and dep != module:
                dependencies[module].add(dep)

    for module, path in paths.items():
        register(module, module)
        package = module if path.stem == "__init__" else module.rpartition(".")[0]
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    register(module, alias.name)
            elif isinstance(node, ast.ImportFrom):
                target = node.module or ""
                if node.level:
                    target = importlib.util.resolve_name("." * node.level + target, package)
                register(module, target)
                for alias in node.names:
                    register(module, target + "." + alias.name)

    importers = defaultdict(set)
    for module in paths:
        if not module.startswith("tests."):
            continue
        seen = {module}
        remaining = list(dependencies[module])
        while remaining:
            target = remaining.pop()
            if target in seen:
                continue
            seen.add(target)
            remaining.extend(dependencies[target] - seen)
            importers[paths[target].relative_to(root).as_posix()].add(
                paths[module].relative_to(root).as_posix()
            )
    return {path: sorted(importers[path]) for path in sorted(importers)}


if __name__ == "__main__":
    root, output = (Path(arg).resolve() for arg in sys.argv[1:])
    data = json.dumps(import_closures(root), separators=(",", ":")).encode()
    output.write_bytes(gzip.compress(data, mtime=0))
