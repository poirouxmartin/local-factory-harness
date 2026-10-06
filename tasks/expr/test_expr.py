"""Spec for the arithmetic evaluator. The naive seed fails precedence, parentheses,
and unary-minus cases. A correct parser turns everything green."""
import pytest

from expr import evaluate


@pytest.mark.parametrize("s,expected", [
    ("2+3", 5),
    ("2+3*4", 14),           # precedence: naive gives 20
    ("2*3+4", 10),
    ("2+3*4-1", 13),
    ("10/2/5", 1),           # left-assoc division
    ("(2+3)*4", 20),         # parentheses: naive errors
    ("2*(3+4)", 14),
    ("((1+2)*(3+4))", 21),   # nested parentheses
    ("-3+5", 2),             # unary minus: naive errors
    ("2*-3", -6),            # unary minus after operator
    ("2 + 3 * 4", 14),       # whitespace
    ("100", 100),
])
def test_eval(s, expected):
    assert evaluate(s) == expected


def test_div_by_zero():
    with pytest.raises(ZeroDivisionError):
        evaluate("1/0")


def test_bad_token():
    with pytest.raises(ValueError):
        evaluate("2+a")
