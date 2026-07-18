"""Amount-in-words rendering for cheque / payment-voucher printing.

Pure function (no ORM, no request), extracted verbatim from cashbook/views.py.
Behaviour is unchanged; cashbook/views.py re-exports it under its original
private name `_amount_in_words` so the cheque-print views keep calling it as
before.
"""
from decimal import Decimal

_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]


def _under_1000(n):
    w = []
    if n >= 100:
        w.append(_ONES[n // 100] + " hundred")
        n %= 100
    if n >= 20:
        w.append(_TENS[n // 10])
        n %= 10
    if n:
        w.append(_ONES[n])
    return " ".join(w)


def amount_in_words(amount):
    """Render a KES amount in words for cheque printing."""
    amount = Decimal(amount)
    shillings = int(amount)
    cents = int((amount - shillings) * 100)
    if shillings == 0:
        words = "zero"
    else:
        parts, scale = [], [(1_000_000, "million"), (1_000, "thousand"), (1, "")]
        for div, name in scale:
            if shillings >= div:
                chunk = shillings // div
                parts.append(_under_1000(chunk) + (f" {name}" if name else ""))
                shillings %= div
        words = " ".join(p for p in parts if p.strip())
    words = words.strip().capitalize() + " shillings"
    if cents:
        words += f" and {cents} cents"
    return words + " only"
