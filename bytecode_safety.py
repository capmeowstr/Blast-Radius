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
