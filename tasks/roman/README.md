# Loop Demo — Roman Numeral Converter

A small, self-contained project for exercising the Loop Engineering harness.
It has one intentional bug. The loop's job: make every test pass.

## Files
- `roman.py` — `to_roman` (correct) + `from_roman` (buggy) + a CLI.
- `test_roman.py` — the spec that pins correct behavior.
- `todo.md` — the single task for the loop.

## How to run it by hand

```
cd C:\Users\me\ai-factory\loop-demo
python roman.py to 1994        # convert int  -> roman
python roman.py from MCMXCIV   # convert roman -> int
python -m pytest -q            # run the test suite
```
(Use `py -3 ...` instead of `python ...` in a terminal opened before the PATH fix.)

## The bug
`from_roman` ignores subtractive notation — it just sums each symbol, so a
smaller numeral before a larger one is added instead of subtracted.

## Expected results

### ❌ BEFORE the fix (current state)
| Command | Output now (buggy) | Correct |
|---------|--------------------|---------|
| `python roman.py from IV` | `6` | `4` |
| `python roman.py from IX` | `11` | `9` |
| `python roman.py from MCMXCIV` | `2216` | `1994` |
| `python roman.py to 1994` | `MCMXCIV` ✅ | `MCMXCIV` |

`pytest` before the fix: **several `test_from_roman` / `test_round_trip` cases FAIL.**

### ✅ AFTER the fix (definition of done)
- `python roman.py from MCMXCIV` prints `1994`
- `python roman.py from IV` prints `4`
- `python -m pytest -q` reports **all tests passed** (0 failed)

## The fix (reference — for grading the loop, don't peek if testing the model)
Rewrite `from_roman` to subtract when a symbol's value is less than the value
of the symbol to its right, e.g.:

```python
def from_roman(s):
    s = s.upper()
    total = 0
    prev = 0
    for ch in reversed(s):
        val = _VALUES[ch]
        total += val if val >= prev else -val
        prev = val
    return total
```
