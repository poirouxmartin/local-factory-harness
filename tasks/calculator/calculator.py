"""Testbed subject for the Loop Engineering harness.

Contains an INTENTIONAL bug so the autonomous edit -> test -> observe loop has a
concrete failing signal to fix. See todo.md.
"""


class Calculator:
    def add(self, a, b):
        return a + b

    def subtract(self, a, b):
        return a - b

    def multiply(self, a, b):
        # BUG (intentional): multiplication implemented as addition.
        # The loop's job is to replace `a + b` with `a * b`.
        return a + b

    def divide(self, a, b):
        if b == 0:
            raise ZeroDivisionError("division by zero")
        return a / b
