"""Safe expression evaluation + parameter binding.

Postconditions and compensation mappings use small, safe expressions like:

    result.booking_status == 'confirmed'
    result.all_confirmed == true

We deliberately do NOT use ``eval``. A tiny recursive-descent-ish evaluator
handles dotted-path lookups, literals, and a fixed set of comparison operators.
Anything it cannot parse safely evaluates to ``False`` (fail-closed for checks).

Binding resolves references like ``result.transaction_id`` / ``params.to`` /
``variables.user_id`` against a context dict.
"""

from __future__ import annotations

import re
from typing import Any

_TRUE = {"true", "True", "TRUE"}
_FALSE = {"false", "False", "FALSE"}
_NULL = {"null", "None", "none"}

# operators ordered so multi-char ones match first
_OPERATORS = ["==", "!=", ">=", "<=", ">", "<"]


class ExpressionError(Exception):
    pass


def resolve_path(path: str, context: dict[str, Any]) -> Any:
    """Resolve a dotted path such as ``result.booking_id`` against ``context``.

    Returns ``None`` if any segment is missing.
    """
    parts = path.split(".")
    current: Any = context
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _coerce_literal(token: str, context: dict[str, Any]) -> Any:
    """Turn a token into a Python value: literal or resolved path."""
    token = token.strip()
    if not token:
        return None
    # String literal
    if (token[0] == token[-1]) and token[0] in {"'", '"'} and len(token) >= 2:
        return token[1:-1]
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    if token in _NULL:
        return None
    # Number
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+\.\d+", token):
        return float(token)
    # Otherwise treat as a dotted path into the context.
    return resolve_path(token, context)


def evaluate(expression: str, context: dict[str, Any]) -> bool:
    """Evaluate a boolean expression safely.

    Supported forms:
      - ``<path_or_literal> <op> <path_or_literal>`` where op is one of
        == != > < >= <=
      - a bare truthy path/literal (e.g. ``result.all_confirmed``)

    On any parse ambiguity, returns ``False`` (fail-closed).
    """
    if expression is None:
        return False
    expr = expression.strip()
    if not expr:
        return False

    for op in _OPERATORS:
        # Split only on the first occurrence to keep it simple + safe.
        idx = _find_operator(expr, op)
        if idx is not None:
            left = expr[:idx].strip()
            right = expr[idx + len(op):].strip()
            lval = _coerce_literal(left, context)
            rval = _coerce_literal(right, context)
            return _apply(op, lval, rval)

    # Bare value -> truthiness.
    val = _coerce_literal(expr, context)
    return bool(val)


def _find_operator(expr: str, op: str) -> int | None:
    """Find an operator not enclosed in quotes; avoid matching inside strings."""
    in_quote: str | None = None
    i = 0
    while i < len(expr):
        ch = expr[i]
        if in_quote:
            if ch == in_quote:
                in_quote = None
            i += 1
            continue
        if ch in {"'", '"'}:
            in_quote = ch
            i += 1
            continue
        if expr[i:i + len(op)] == op:
            # Avoid matching '>' inside '>=' etc: caller checks multi-char first,
            # but ensure the next char isn't '=' when op is single-char.
            if op in {">", "<"} and i + 1 < len(expr) and expr[i + 1] == "=":
                i += 1
                continue
            return i
        i += 1
    return None


def _apply(op: str, lval: Any, rval: Any) -> bool:
    try:
        if op == "==":
            return lval == rval
        if op == "!=":
            return lval != rval
        if op == ">":
            return lval > rval  # type: ignore[operator]
        if op == "<":
            return lval < rval  # type: ignore[operator]
        if op == ">=":
            return lval >= rval  # type: ignore[operator]
        if op == "<=":
            return lval <= rval  # type: ignore[operator]
    except TypeError:
        return False
    return False


def bind_parameters(
    mapping: dict[str, str], context: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Resolve a compensation parameter mapping against a context.

    Returns ``(bound_params, unresolved_refs)``. A reference is 'unresolved' when
    a ``result.``/``params.``/``variables.`` path yields ``None`` because the
    field is absent - which signals a stale/mismatched compensation contract.
    """
    bound: dict[str, Any] = {}
    unresolved: list[str] = []
    for param, ref in mapping.items():
        if isinstance(ref, str) and (
            ref.startswith("result.")
            or ref.startswith("params.")
            or ref.startswith("variables.")
        ):
            value = resolve_path(ref, context)
            if value is None:
                unresolved.append(f"{param} <- {ref}")
            else:
                bound[param] = value
        else:
            # Literal binding.
            bound[param] = _coerce_literal(ref, context) if isinstance(ref, str) else ref
    return bound, unresolved
