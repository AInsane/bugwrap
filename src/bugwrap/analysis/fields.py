"""Class field contracts: which data attributes a class promises its users.

A removed dataclass field or `self.x = ...` assignment breaks every `obj.x`
reader in the repo — invisible to signature diffing, caught here by comparing
the field set of each class between the base and new revisions and joining
against the attribute-read index.
"""

from __future__ import annotations

import ast

from .signature import find_node

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def class_fields(source: str, class_qualname: str) -> dict[str, int] | None:
    """Field name -> definition line. None if the class isn't in this source."""
    node = find_node(source, class_qualname)
    if not isinstance(node, ast.ClassDef):
        return None

    fields: dict[str, int] = {}
    for stmt in node.body:
        # class-level: dataclass fields, annotated or plain class attributes
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.setdefault(stmt.target.id, stmt.lineno)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    fields.setdefault(target.id, stmt.lineno)

    # instance attributes: self.x = ... anywhere in the class's own methods
    for stmt in node.body:
        if not isinstance(stmt, _FUNC):
            continue
        for sub in ast.walk(stmt):
            targets = []
            if isinstance(sub, ast.Assign):
                targets = sub.targets
            elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
                targets = [sub.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    fields.setdefault(target.attr, target.lineno)
    return fields


def _exposed_names(source: str, class_qualname: str) -> set[str] | None:
    """Every attribute name the class answers to: fields, methods, properties."""
    fields = class_fields(source, class_qualname)
    if fields is None:
        return None
    node = find_node(source, class_qualname)
    method_names = {stmt.name for stmt in node.body if isinstance(stmt, _FUNC)}
    return set(fields) | method_names


def removed_fields(
    old_source: str, new_source: str, class_qualname: str
) -> list[tuple[str, int]]:
    """(field, old_lineno) for fields the class had at base and lost in the change.

    A name that survives as *anything* — another field, a method, a `@property`
    shim — is not removed: `obj.x` still answers. That's what keeps a
    field-renamed-behind-a-property refactor from becoming a false positive.
    """
    old = class_fields(old_source, class_qualname)
    new_exposed = _exposed_names(new_source, class_qualname)
    if old is None or new_exposed is None:  # class added or removed — other paths
        return []
    return sorted(
        ((name, old[name]) for name in old.keys() - new_exposed),
        key=lambda item: item[1],
    )
