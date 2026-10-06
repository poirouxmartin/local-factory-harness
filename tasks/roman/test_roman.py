"""Spec for the Roman numeral converter.

The `from_roman` subtractive cases FAIL until the intentional bug is fixed.
A correct fix turns every test in this file green.
"""
import pytest

from roman import to_roman, from_roman


@pytest.mark.parametrize("n,expected", [
    (1, "I"), (4, "IV"), (9, "IX"), (40, "XL"), (90, "XC"),
    (400, "CD"), (900, "CM"), (2024, "MMXXIV"), (1994, "MCMXCIV"), (3999, "MMMCMXCIX"),
])
def test_to_roman(n, expected):
    assert to_roman(n) == expected


@pytest.mark.parametrize("s,expected", [
    ("I", 1),
    ("IV", 4),        # subtractive — fails while bug present (naive sum = 6)
    ("IX", 9),        # subtractive — fails while bug present (naive sum = 11)
    ("XL", 40),
    ("XC", 90),
    ("MCMXCIV", 1994),  # fails while bug present (naive sum = 2216)
    ("MMXXIV", 2024),
])
def test_from_roman(s, expected):
    assert from_roman(s) == expected


@pytest.mark.parametrize("n", [1, 4, 9, 58, 1994, 2024, 3999])
def test_round_trip(n):
    assert from_roman(to_roman(n)) == n


def test_to_roman_rejects_out_of_range():
    with pytest.raises(ValueError):
        to_roman(0)
    with pytest.raises(ValueError):
        to_roman(4000)
