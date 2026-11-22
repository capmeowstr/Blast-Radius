"""Identify changes to code executed when modules and classes are declared."""

import ast


def literal_shape(node):
    """Describe a literal's construction and keys without its scalar values."""
    if isinstance(node, ast.Constant):
        return type(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        if isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float, complex):
            return type(node.operand.value)
    if isinstance(node, (ast.Tuple, ast.List)):
        elements = tuple(literal_shape(item) for item in node.elts)
        if all(element is not None for element in elements):
            return type(node), elements
    if isinstance(node, ast.Dict) and all(isinstance(key, ast.Constant) for key in node.keys):
        values = tuple(literal_shape(value) for value in node.values)
        if all(value is not None for value in values):
            return type(node), tuple(ast.dump(key) for key in node.keys), values
    return None


def eager_reads(node):
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        yield node.id
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        yield node.attr
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        fields = (value for name, value in ast.iter_fields(node) if name != "body")
        for value in fields:
            children = value if isinstance(value, list) else [value]
            for child in children:
                if isinstance(child, ast.AST):
                    yield from eager_reads(child)
    else:
        for child in ast.iter_child_nodes(node):
            yield from eager_reads(child)


def declaration(node, read_names):
    if isinstance(node, (ast.Module, ast.ClassDef)):
        fields = tuple(ast.dump(value) if isinstance(value, ast.AST)
                       else tuple(ast.dump(entry) for entry in value) if isinstance(value, list)
                       else value
                       for name, value in ast.iter_fields(node) if name != "body")
        return type(node), fields, tuple(declaration(statement, read_names) for statement in node.body)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        definition = type(node)(**{name: value for name, value in ast.iter_fields(node) if name != "body"}, body=[])
        return ast.dump(definition)
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        shape = literal_shape(node.value)
        if shape is not None and all(
            isinstance(target, ast.Name) and not target.id.startswith("__") and target.id not in read_names
            for target in targets
        ):
            definition = type(node)(**{name: value for name, value in ast.iter_fields(node) if name != "value"},
                                    value=ast.Constant(None))
            return ast.dump(definition), shape
    return ast.dump(node)


def executable_declarations_changed(before, after):
    read_names = set(eager_reads(before)) | set(eager_reads(after))
    return declaration(before, read_names) != declaration(after, read_names)
