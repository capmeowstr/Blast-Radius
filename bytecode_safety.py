"""Check whether the existing and revised code compile to the same program."""

import marshal
from types import CodeType


def executable_code(code):
    """Keep code-object fields while omitting source positions."""
    normalized = {}

    def visit(obj):
        key = id(obj)
        if key not in normalized:
            constants = tuple(
                visit(value) if isinstance(value, CodeType) else value
                for value in obj.co_consts
            )
            normalized[key] = obj.replace(
                co_consts=constants,
                co_firstlineno=1,
                co_linetable=b"",
            )
        return normalized[key]

    return marshal.dumps(visit(code))


def same_executable_code(before, after):
    """Compare activated code, including nested functions and constant types."""
    return executable_code(before) == executable_code(after)


def _functions(code, prefix=""):
    """Yield (qualified key, code object) for every nested code object."""
    for value in code.co_consts:
        if isinstance(value, CodeType):
            key = prefix + "/" + value.co_qualname
            yield key, value
            yield from _functions(value, key)


def _shape(code):
    # Generator/coroutine flags and local/cell/free variable sets decide how
    # every call behaves, not only the edited line.
    return (code.co_flags, frozenset(code.co_varnames), frozenset(code.co_cellvars),
            frozenset(code.co_freevars), code.co_argcount, code.co_kwonlyargcount)


def reshaped_functions(before, after):
    """Return first lines (in `before`) of functions whose call shape changed."""
    old = {}
    for key, code in _functions(before):
        old.setdefault(key, []).append(code)
    new = {}
    for key, code in _functions(after):
        new.setdefault(key, []).append(code)
    lines = set()
    for key, codes in old.items():
        others = new.get(key, [])
        if len(others) != len(codes):
            lines.update(code.co_firstlineno for code in codes)
            continue
        for code, other in zip(codes, others):
            if _shape(code) != _shape(other):
                lines.add(code.co_firstlineno)
    return lines
