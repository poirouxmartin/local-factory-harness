"""Roman numeral converter — Loop Engineering demo subject.

Two public functions plus a small CLI. `to_roman` is correct.
`from_roman` contains ONE intentional bug: it ignores subtractive notation
(IV, IX, XL, XC, CD, CM) and just sums every symbol. The loop's task is to
fix `from_roman` so the whole suite goes green.

CLI:
    python roman.py to 1994      -> MCMXCIV
    python roman.py from MCMXCIV  -> 1994
"""
import sys

_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

_TABLE = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]


def to_roman(n):
    if not isinstance(n, int) or not (1 <= n <= 3999):
        raise ValueError("n must be an integer in 1..3999")
    out = []
    for value, symbol in _TABLE:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


def from_roman(s):
    s = s.upper()
    total = 0
    for i in range(len(s)):
        if i + 1 < len(s) and _VALUES[s[i]] < _VALUES[s[i+1]]:
            total -= _VALUES[s[i]]
        else:
            total += _VALUES[s[i]]
    return total


def _main(argv):
    if len(argv) != 3 or argv[1] not in ("to", "from"):
        print("usage: python roman.py to <int> | from <ROMAN>", file=sys.stderr)
        return 2
    if argv[1] == "to":
        print(to_roman(int(argv[2])))
    else:
        print(from_roman(argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
