"""Input/output protocol for the grader.

A suite is a protocol only when every test assertion compares a candidate probe
with a literal expected value. The expected value stays with the parent. The
candidate process receives the probe and returns a value.
"""

from __future__ import annotations

import ast
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class _NotLiteral:
    pass


_NOT = _NotLiteral()


@dataclass(frozen=True)
class ProtocolCase:
    case_id: str
    module: str
    file: str
    kind: str
    attr: tuple[str, ...]
    args: tuple[Any, ...]
    kwargs: tuple[tuple[str, Any], ...]
    expected: Any


def extract_protocol(candidate: Path, suite: Path) -> tuple[ProtocolCase, ...] | None:
    """Return protocol cases, or None when the suite must stay cooperative pytest."""
    files = [path for path in sorted(suite.rglob("*.py")) if path.is_file() and _is_test_file(path)]
    if not files:
        return None
    cases: list[ProtocolCase] = []
    for path in files:
        extracted = _file_cases(candidate, suite, path)
        if extracted is None:
            return None
        cases.extend(extracted)
    if not cases:
        return None
    return tuple(cases)


def probes_document(cases: tuple[ProtocolCase, ...]) -> dict[str, Any]:
    """Probes only. Expected values are intentionally absent."""
    return {
        "cases": [
            {
                "id": case.case_id,
                "module": case.module,
                "file": case.file,
                "kind": case.kind,
                "attr": list(case.attr),
                "args": list(case.args),
                "kwargs": dict(case.kwargs),
            }
            for case in cases
        ]
    }


def values_match(cases: tuple[ProtocolCase, ...], reported: object) -> bool:
    if not isinstance(reported, list) or len(reported) != len(cases):
        return False
    by_id: dict[str, Any] = {}
    for item in reported:
        if not isinstance(item, dict) or set(item) != {"id", "value"} or not isinstance(item["id"], str):
            return False
        if item["id"] in by_id:
            return False
        by_id[item["id"]] = item["value"]
    return all(case.case_id in by_id and by_id[case.case_id] == case.expected for case in cases)


def _is_test_file(path: Path) -> bool:
    return path.name.startswith("test_") or path.name.endswith("_test.py")


def _file_cases(candidate: Path, suite: Path, path: Path) -> list[ProtocolCase] | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return None
    if not all(_replayable_statement(node) for node in tree.body):
        return None
    aliases = _aliases(tree)
    if aliases is None:
        return None
    relative = path.relative_to(suite).as_posix()
    cases: list[ProtocolCase] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test"):
            continue
        converted = _function_cases(candidate, relative, node, aliases)
        if converted is None:
            return None
        cases.extend(converted)
    return cases


# xunit hooks pytest runs around a module's or function's tests; the probes never run them.
_XUNIT_HOOKS = frozenset(
    {"setup_module", "teardown_module", "setUpModule", "tearDownModule", "setup_function", "teardown_function"}
)


def _replayable_statement(node: ast.stmt) -> bool:
    """True only for top-level statements whose whole effect on the run is the literal asserts we replay.

    This is an allowlist on purpose: pytest collects and runs more than top-level ``test*`` functions
    (unittest.TestCase subclasses under any name, test functions imported or bound by assignment, xunit
    hooks, fixtures), so anything outside it keeps the suite on cooperative pytest rather than letting
    the protocol certify a pass from the part it understood.
    """
    if isinstance(node, ast.Expr):
        return isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return not any((alias.asname or alias.name).lower().startswith("test") for alias in node.names)
    if isinstance(node, ast.FunctionDef):
        return node.name.startswith("test") or (not node.decorator_list and node.name not in _XUNIT_HOOKS)
    return False


def _aliases(tree: ast.AST) -> dict[str, tuple[str, tuple[str, ...]]] | None:
    aliases: dict[str, tuple[str, tuple[str, ...]]] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "." in alias.name:
                    return None
                aliases[alias.asname or alias.name] = (alias.name, ())
        elif isinstance(node, ast.ImportFrom):
            loaded = _from_import(node)
            if loaded is None:
                return None
            aliases.update(loaded)
    return aliases


def _from_import(node: ast.ImportFrom) -> dict[str, tuple[str, tuple[str, ...]]] | None:
    if node.level or node.module is None:
        return None
    loaded: dict[str, tuple[str, tuple[str, ...]]] = {}
    for alias in node.names:
        if alias.name == "*":
            return None
        loaded[alias.asname or alias.name] = (node.module, (alias.name,))
    return loaded


def _function_cases(
    candidate: Path,
    relative: str,
    node: ast.FunctionDef,
    aliases: dict[str, tuple[str, tuple[str, ...]]],
) -> list[ProtocolCase] | None:
    if node.decorator_list or not _no_arguments(node):
        return None
    statements = _body_without_docstring(node.body)
    if not statements:
        return None
    cases: list[ProtocolCase] = []
    for index, statement in enumerate(statements):
        built = _assertion_case(candidate, relative, node.name, index, statement, aliases)
        if built is None:
            return None
        cases.append(built)
    return cases


def _no_arguments(node: ast.FunctionDef) -> bool:
    args = node.args
    return not (args.posonlyargs or args.args or args.vararg or args.kwonlyargs or args.kwarg)


def _body_without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        return list(body[1:])
    return list(body)


def _assertion_case(
    candidate: Path,
    relative: str,
    test_name: str,
    index: int,
    statement: ast.stmt,
    aliases: dict[str, tuple[str, tuple[str, ...]]],
) -> ProtocolCase | None:
    if not isinstance(statement, ast.Assert) or not isinstance(statement.test, ast.Compare):
        return None
    compare = statement.test
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], ast.Eq) or len(compare.comparators) != 1:
        return None
    split = _split_comparison(compare.left, compare.comparators[0], aliases)
    if split is None:
        return None
    module, attr, kind, args, kwargs, expected = split
    relative_file = _module_file(candidate, module)
    if relative_file is None:
        return None
    return ProtocolCase(
        case_id=f"{relative}::{test_name}#{index}",
        module=module,
        file=relative_file,
        kind=kind,
        attr=attr,
        args=args,
        kwargs=kwargs,
        expected=expected,
    )


def _split_comparison(
    left: ast.expr,
    right: ast.expr,
    aliases: dict[str, tuple[str, tuple[str, ...]]],
) -> tuple[str, tuple[str, ...], str, tuple[Any, ...], tuple[tuple[str, Any], ...], Any] | None:
    left_literal = _literal(left)
    right_literal = _literal(right)
    if left_literal is _NOT and right_literal is not _NOT:
        probe = _probe(left, aliases)
        expected = right_literal
    elif right_literal is _NOT and left_literal is not _NOT:
        probe = _probe(right, aliases)
        expected = left_literal
    else:
        return None
    if probe is None:
        return None
    module, attr, kind, args, kwargs = probe
    canonical = _canonical(expected)
    if canonical is _NOT:
        return None
    return module, attr, kind, args, kwargs, canonical


def _probe(
    expr: ast.expr,
    aliases: dict[str, tuple[str, tuple[str, ...]]],
) -> tuple[str, tuple[str, ...], str, tuple[Any, ...], tuple[tuple[str, Any], ...]] | None:
    kind = "attr"
    args: tuple[Any, ...] = ()
    kwargs: tuple[tuple[str, Any], ...] = ()
    if isinstance(expr, ast.Call):
        parsed = _call_arguments(expr)
        if parsed is None:
            return None
        args, kwargs = parsed
        expr = expr.func
        kind = "call"
    attrs: list[str] = []
    while isinstance(expr, ast.Attribute):
        if not expr.attr.isidentifier():
            return None
        attrs.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name) or expr.id not in aliases:
        return None
    module, prefix = aliases[expr.id]
    attr = prefix + tuple(reversed(attrs))
    if kind == "attr" and not attr:
        return None
    return module, attr, kind, args, kwargs


def _call_arguments(expr: ast.Call) -> tuple[tuple[Any, ...], tuple[tuple[str, Any], ...]] | None:
    if any(isinstance(arg, ast.Starred) for arg in expr.args):
        return None
    if any(keyword.arg is None for keyword in expr.keywords):
        return None
    args: list[Any] = []
    for arg in expr.args:
        value = _literal(arg)
        if value is _NOT:
            return None
        canonical = _canonical(value)
        if canonical is _NOT:
            return None
        args.append(canonical)
    kwargs: list[tuple[str, Any]] = []
    for keyword in expr.keywords:
        value = _literal(keyword.value)
        if value is _NOT or keyword.arg is None:
            return None
        canonical = _canonical(value)
        if canonical is _NOT:
            return None
        kwargs.append((keyword.arg, canonical))
    return tuple(args), tuple(kwargs)


def _literal(expr: ast.expr) -> Any:
    if isinstance(expr, ast.Constant):
        return _constant(expr.value)
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.USub):
        return _negate(expr.operand)
    if isinstance(expr, ast.List):
        return _sequence(expr.elts, list)
    if isinstance(expr, ast.Tuple):
        return _sequence(expr.elts, list)
    if isinstance(expr, ast.Dict):
        return _mapping(expr)
    return _NOT


def _constant(value: object) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return _NOT


def _negate(expr: ast.expr) -> Any:
    if not isinstance(expr, ast.Constant) or isinstance(expr.value, bool) or not isinstance(expr.value, (int, float)):
        return _NOT
    if isinstance(expr.value, float) and not math.isfinite(expr.value):
        return _NOT
    return -expr.value


def _sequence(elements: list[ast.expr], construct: type[list[Any]]) -> Any:
    values: list[Any] = []
    for element in elements:
        value = _literal(element)
        if value is _NOT:
            return _NOT
        values.append(value)
    return construct(values)


def _mapping(expr: ast.Dict) -> Any:
    mapped: dict[str, Any] = {}
    for key, value in zip(expr.keys, expr.values, strict=True):
        if key is None:
            return _NOT
        literal_key = _literal(key)
        literal_value = _literal(value)
        if not isinstance(literal_key, str) or literal_value is _NOT:
            return _NOT
        mapped[literal_key] = literal_value
    return mapped


def _canonical(value: Any) -> Any:
    if value is _NOT:
        return _NOT
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        return _NOT


def _module_file(candidate: Path, module: str) -> str | None:
    parts = module.split(".")
    if not parts or any(not part.isidentifier() for part in parts):
        return None
    package = candidate.joinpath(*parts, "__init__.py")
    module_path = candidate.joinpath(*parts).with_suffix(".py")
    if _regular_file(package):
        return package.relative_to(candidate).as_posix()
    if _regular_file(module_path):
        return module_path.relative_to(candidate).as_posix()
    return None


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()
