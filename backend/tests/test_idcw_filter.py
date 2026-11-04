"""IDCW / dividend-option exclusion tests."""
import pytest

from amfi_client import is_idcw_scheme


@pytest.mark.parametrize("name", [
    "NIPPON INDIA SMALL CAP FUND - IDCW Option",
    "Invesco India Short Term Fund- Plan B - Daily Dividend",
    "HDFC Top 100 Fund - Regular Plan - Dividend",
    "SBI Bluechip Fund - Regular Plan - Dividend Payout",
    "Axis Long Term Equity Fund - Dividend Reinvestment",
    "Kotak Bond Fund - Payout of Income Distribution cum capital wdrl option",
    "ICICI Prudential Liquid Fund - Weekly Dividend",
    "UTI Flexi Cap Fund - Regular Plan - IDCW",
])
def test_idcw_variants_excluded(name):
    assert is_idcw_scheme(name) is True


@pytest.mark.parametrize("name", [
    "NIPPON INDIA SMALL CAP FUND - Growth Plan - Growth Option",
    "Kotak-Small Cap Fund - Growth",
    "Parag Parikh Flexi Cap Fund - Regular Plan - Growth",
    # "Dividend Yield" is an equity CATEGORY, not a payout option — keep.
    "ICICI Prudential Dividend Yield Equity Fund - Growth",
    "UTI Dividend Yield Fund - Growth Plan",
    "Aditya Birla Sun Life Dividend Yield Fund - Growth",
    "Canara Robeco Large and Mid Cap Fund - Regular Plan - Growth",
])
def test_growth_and_dividend_yield_kept(name):
    assert is_idcw_scheme(name) is False


def test_empty_and_none():
    assert is_idcw_scheme(None) is False
    assert is_idcw_scheme("") is False


def test_dividend_yield_idcw_variant_still_excluded():
    # Dividend-Yield category fund but IDCW option → must be excluded.
    assert is_idcw_scheme("UTI Dividend Yield Fund - IDCW") is True
    assert is_idcw_scheme("ICICI Pru Dividend Yield Equity Fund - Dividend Payout") is True
