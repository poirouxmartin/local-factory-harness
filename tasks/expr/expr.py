"""Arithmetic expression evaluator — a HARD Loop Engineering task.

Supports +, -, *, /, parentheses, unary minus, and whitespace, with correct
operator precedence. Currently the implementation is INTENTIONALLY naive: it
evaluates strictly left-to-right, ignores precedence, and cannot handle
parentheses or unary minus. A real fix needs a proper parser (shunting-yard or
recursive descent) -- this is where a 7B and a 30B model tend to diverge.
"""


def _apply(a, op, b):
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return a / b
    raise ValueError(f"bad operator: {op}")


def evaluate(expression):
    s = expression.replace(" ", "")
    # BUG: left-to-right, no precedence, no parentheses, no unary minus.
    num = ""
    result = None
    op = None
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch in "+-*/":
            value = float(num)
            num = ""
            result = value if result is None else _apply(result, op, value)
            op = ch
        else:
            raise ValueError(f"bad character: {ch}")
    value = float(num)
    result = value if result is None else _apply(result, op, value)
    return result
