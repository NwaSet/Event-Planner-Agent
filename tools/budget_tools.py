"""Deterministic EUR arithmetic; no allocations, taxes, or fees are inferred.

Policy: accept non-negative finite int/float/Decimal values or decimal strings;
convert via Decimal(str(value)), round EACH input to cents with ROUND_HALF_UP,
then calculate. Monetary outputs are fixed two-decimal strings, preserving exact
cents in JSON. A local 28-digit decimal context bounds supported amounts/totals.
Booleans, negatives, NaN, infinity, and values exceeding this precision fail.

All inputs must already use the same currency and tax basis. A price is the full
line amount, not a unit price; no implicit quantity or surcharge is applied.
"""

from decimal import Context, Decimal, DecimalException, ROUND_HALF_UP, localcontext
import re


_CENT = Decimal("0.01")
_CONTEXT = Context(prec=28, rounding=ROUND_HALF_UP)
_DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _money(value, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{label} must be a finite, non-negative monetary value.")
    text = str(value).strip()
    if not _DECIMAL_TEXT.fullmatch(text):
        raise ValueError(f"{label} must be a valid decimal monetary value.")
    try:
        with localcontext(_CONTEXT):
            amount = Decimal(text)
            if not amount.is_finite() or amount < 0:
                raise ValueError(f"{label} must be finite and non-negative.")
            rounded = amount.quantize(_CENT)
            return rounded.copy_abs() if rounded.is_zero() else rounded
    except DecimalException:
        raise ValueError(f"{label} exceeds the supported monetary precision.") from None


def _format(amount: Decimal) -> str:
    try:
        with localcontext(_CONTEXT):
            return format(amount.quantize(_CENT), ".2f")
    except DecimalException:
        raise ValueError("Calculated amount exceeds the supported monetary precision.") from None


def _sum(amounts) -> Decimal:
    try:
        with localcontext(_CONTEXT):
            return sum(amounts, Decimal("0.00")).quantize(_CENT)
    except DecimalException:
        raise ValueError("Calculated total exceeds the supported monetary precision.") from None


def calculate_total(items: list[dict]) -> dict:
    """Sum full line prices; each item has exactly a non-empty name and price."""
    if not isinstance(items, list):
        raise ValueError("Items must be a list of name/price dictionaries.")
    amounts, breakdown = [], []
    for index, item in enumerate(items):
        if (not isinstance(item, dict) or set(item) != {"name", "price"}
                or not isinstance(item["name"], str) or not item["name"].strip()):
            raise ValueError(f"Item {index} must contain only a non-empty name and a price.")
        amount = _money(item["price"], f"Item {index} price")
        amounts.append(amount)
        breakdown.append({"name": item["name"], "price": _format(amount)})
    return {"total": _format(_sum(amounts)), "currency": "EUR", "items": breakdown}


def remaining_budget(total_budget: float, expenses: float) -> dict:
    """Return the remaining amount and whether expenses exceed the budget."""
    budget, spent = _money(total_budget, "Total budget"), _money(expenses, "Expenses")
    with localcontext(_CONTEXT):
        remaining = budget - spent
    return {"total_budget": _format(budget), "expenses": _format(spent),
            "remaining_budget": _format(remaining), "budget_exceeded": spent > budget, "currency": "EUR"}


def validate_budget(total_budget: float, allocations: dict[str, float]) -> dict:
    """Check caller-proposed allocations without selecting or changing categories."""
    budget = _money(total_budget, "Total budget")
    if not isinstance(allocations, dict):
        raise ValueError("Allocations must map non-empty category names to monetary values.")
    if any(not isinstance(name, str) or not name.strip() for name in allocations):
        raise ValueError("Allocation category names must be non-empty strings.")
    amounts = {name: _money(value, "Allocation amount") for name, value in allocations.items()}
    total = _sum(amounts.values())
    with localcontext(_CONTEXT):
        remaining = budget - total
    return {"total_budget": _format(budget), "total_allocations": _format(total),
            "remaining_budget": _format(remaining), "allocations": {name: _format(value) for name, value in amounts.items()},
            "budget_exceeded": total > budget, "valid": total <= budget, "currency": "EUR"}
