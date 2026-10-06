"""pytest spec pinning correct Calculator behavior.

`test_multiply_*` currently FAIL against the intentional bug in calculator.py.
A correct fix (multiply using `*`) turns the whole suite green.
"""
import pytest

from calculator import Calculator


@pytest.fixture
def calc():
    return Calculator()


def test_add(calc):
    assert calc.add(2, 3) == 5
    assert calc.add(-1, 1) == 0


def test_subtract(calc):
    assert calc.subtract(10, 4) == 6
    assert calc.subtract(0, 5) == -5


def test_multiply(calc):
    assert calc.multiply(3, 4) == 12          # fails while bug present (3+4=7)
    assert calc.multiply(-2, 5) == -10        # fails while bug present (-2+5=3)
    assert calc.multiply(0, 99) == 0


def test_divide(calc):
    assert calc.divide(10, 2) == 5
    assert calc.divide(9, 3) == 3


def test_divide_by_zero_raises(calc):
    with pytest.raises(ZeroDivisionError):
        calc.divide(1, 0)
