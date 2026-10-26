"""Exact-cent arithmetic and invalid-input tests, without external services."""

from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR, localcontext
import json

import pytest

from tools.budget_tools import calculate_total, remaining_budget, validate_budget


def test_total_and_non_mutation():
    items = [{"name": "Venue", "price": 1500}, {"name": "Catering", "price": 2000},
             {"name": "Security", "price": 500}]
    before = deepcopy(items)
    result = calculate_total(items)
    assert result["total"] == "4000.00" and result["currency"] == "EUR"
    assert result["items"][0] == {"name": "Venue", "price": "1500.00"}
    assert items == before
    json.dumps(result, allow_nan=False)


def test_decimal_arithmetic_and_round_each_input():
    assert calculate_total([{"name": "A", "price": 0.1}, {"name": "B", "price": 0.2}])["total"] == "0.30"
    assert calculate_total([{"name": "A", "price": "0.005"}, {"name": "B", "price": Decimal("0.005")}])["total"] == "0.02"
    assert calculate_total([{"name": "A", "price": 2.675}])["total"] == "2.68"
    assert remaining_budget("1.00", "0.995")["remaining_budget"] == "0.00"
    assert calculate_total([{"name": "A", "price": "-0"}])["total"] == "0.00"


def test_precision_policy_is_independent_of_global_decimal_context():
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_FLOOR
        assert calculate_total([{"name": "A", "price": "1234.565"}])["total"] == "1234.57"
        assert remaining_budget("1234.57", "1000")["remaining_budget"] == "234.57"


@pytest.mark.parametrize("spent,remaining,exceeded", [(80, "20.00", False), (100, "0.00", False), (125, "-25.00", True)])
def test_remaining_budget(spent, remaining, exceeded):
    assert remaining_budget(100, spent) == {
        "total_budget": "100.00", "expenses": f"{spent:.2f}", "remaining_budget": remaining,
        "budget_exceeded": exceeded, "currency": "EUR",
    }


def test_allocations_preserved_and_over_budget_reported():
    source = {"Venue": "60.005", "Catering": "50"}
    result = validate_budget(100, source)
    assert result["allocations"] == {"Venue": "60.01", "Catering": "50.00"}
    assert result["total_allocations"] == "110.01"
    assert result["remaining_budget"] == "-10.01"
    assert result["budget_exceeded"] and not result["valid"]
    assert source == {"Venue": "60.005", "Catering": "50"}
    assert validate_budget("0.3", {"A": "0.1", "B": "0.2"})["valid"]


def test_empty_totals_and_allocations():
    assert calculate_total([])["total"] == "0.00"
    result = validate_budget(100, {})
    assert result["total_allocations"] == "0.00" and result["remaining_budget"] == "100.00"
    assert result["valid"]


@pytest.mark.parametrize("amount", [None, True, False, [], {}, "", "EUR 10", "1_000", "1,20", -1,
                                    "-0.001", float("nan"), float("inf"), "NaN", "Infinity",
                                    Decimal("NaN"), "1e1000", "private-value"])
def test_invalid_monetary_values(amount):
    for operation in [lambda: calculate_total([{"name": "A", "price": amount}]),
                      lambda: remaining_budget(amount, 0), lambda: remaining_budget(10, amount),
                      lambda: validate_budget(10, {"A": amount})]:
        with pytest.raises(ValueError) as failure:
            operation()
        assert "private-value" not in str(failure.value)


@pytest.mark.parametrize("items", [None, {}, [None], [{}], [{"name": "A"}], [{"price": 1}],
                                   [{"name": "", "price": 1}], [{"name": "A", "price": 1, "quantity": 2}]])
def test_invalid_items_do_not_silently_ignore_missing_prices_or_quantities(items):
    with pytest.raises(ValueError):
        calculate_total(items)


@pytest.mark.parametrize("allocations", [None, [], {"": 1}, {1: 2}])
def test_invalid_allocations(allocations):
    with pytest.raises(ValueError):
        validate_budget(100, allocations)


def test_total_overflow_is_not_silently_rounded():
    with pytest.raises(ValueError, match="precision"):
        calculate_total([{"name": "A", "price": "99999999999999999999999999.99"},
                         {"name": "B", "price": "0.01"}])
