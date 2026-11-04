"""Tests for the two-pass fund classifier.

Verifies that real AMFI scheme_category strings + scheme names map to the
correct internal benchmark bucket. The classifier is consulted in
backend/server.py before running the FQS pipeline; an incorrect bucket means
the min-WM benchmark and equity-vs-non-equity weight branch are wrong.
"""
import pytest

from category_map import classify_fund


@pytest.mark.parametrize("name,amfi_cat,expected", [
    # Canonical equity buckets — AMFI category is authoritative.
    ("Nippon India Small Cap Fund",     "Equity Scheme - Small Cap Fund",        "Small Cap"),
    ("HDFC Mid-Cap Opportunities Fund", "Equity Scheme - Mid Cap Fund",          "Mid Cap"),
    ("Mirae Asset Large & Midcap Fund", "Equity Scheme - Large & Mid Cap Fund",  "Large Cap"),
    ("Axis Bluechip Fund",              "Equity Scheme - Large Cap Fund",        "Large Cap"),
    ("Kotak Multicap Fund",             "Equity Scheme - Multi Cap Fund",        "Multi Cap"),
    ("Parag Parikh Flexi Cap Fund",     "Equity Scheme - Flexi Cap Fund",        "Flexi Cap"),
    ("Mirae Asset Tax Saver Fund",      "Equity Scheme - ELSS",                  "ELSS"),
    # Equity sub-types without a dedicated bucket → Flexi Cap default.
    ("ICICI Pru Value Discovery Fund",  "Equity Scheme - Value Fund",            "Flexi Cap"),
    ("SBI Contra Fund",                 "Equity Scheme - Contra Fund",           "Flexi Cap"),
    ("ICICI Pru Focused Equity Fund",   "Equity Scheme - Focused Fund",          "Flexi Cap"),
    ("ICICI Prudential Technology Fund","Equity Scheme - Sectoral/Thematic",     "Flexi Cap"),
    ("ICICI Pru Dividend Yield Equity", "Equity Scheme - Dividend Yield Fund",   "Large Cap"),

    # Other Scheme — Index / ETF / Gold / FoF.
    ("Motilal Oswal S&P 500 Index Fund","Other Scheme - Index Funds",            "Index Fund"),
    ("Nippon India ETF Nifty BeES",     "Other Scheme - ETFs",                   "Index Fund"),
    ("ICICI Pru Gold ETF",              "Other Scheme - ETFs - Gold",            "Gold/Silver"),
    ("Motilal Oswal Nasdaq 100 FoF",    "Other Scheme - FoFs (Overseas)",        "Index Fund"),
    ("Axis Gold Fund",                  "Other Scheme - FoFs (Domestic)",        "Gold/Silver"),
    ("HDFC Nifty 50 Index FoF",         "Other Scheme - FoFs (Domestic)",        "Index Fund"),

    # Hybrid sub-categories — arbitrage is debt-like, others are hybrid.
    ("DSP Arbitrage Fund",              "Hybrid Scheme - Arbitrage Fund",        "Debt"),
    ("ICICI Pru Equity Savings Fund",   "Hybrid Scheme - Equity Savings",        "Hybrid"),
    ("HDFC Balanced Advantage Fund",    "Hybrid Scheme - Balanced Advantage Fund","Hybrid"),
    ("ICICI Pru Multi Asset Fund",      "Hybrid Scheme - Multi Asset Allocation","Hybrid"),

    # Solution-oriented.
    ("HDFC Retirement Savings Fund",    "Solution Oriented Scheme - Retirement Fund", "Hybrid"),
    ("HDFC Children's Gift Fund",       "Solution Oriented Scheme - Children's Fund", "Hybrid"),

    # Debt sub-categories — every variant must hit Debt.
    ("ICICI Pru Liquid Fund",           "Debt Scheme - Liquid Fund",             "Debt"),
    ("SBI Magnum Gilt Fund",            "Debt Scheme - Gilt Fund",               "Debt"),
    ("HDFC Corporate Bond Fund",        "Debt Scheme - Corporate Bond Fund",     "Debt"),
    ("Aditya Birla Banking & PSU Debt", "Debt Scheme - Banking and PSU Fund",    "Debt"),
    ("Nippon Floating Rate Fund",       "Debt Scheme - Floater Fund",            "Debt"),
    ("Axis Overnight Fund",             "Debt Scheme - Overnight Fund",          "Debt"),
    ("HDFC Short Duration",             "Debt Scheme - Short Duration Fund",     "Debt"),
    ("ICICI Pru Credit Risk Fund",      "Debt Scheme - Credit Risk Fund",        "Debt"),
])
def test_classify_with_amfi_category(name, amfi_cat, expected):
    assert classify_fund(name, amfi_cat) == expected


@pytest.mark.parametrize("name,expected", [
    # Legacy / missing AMFI category — must classify from the name alone.
    ("HDFC Small Cap Fund",             "Small Cap"),
    ("ICICI Pru Bluechip Equity Fund",  "Large Cap"),
    ("UTI Nifty 50 Index Fund",         "Index Fund"),
    ("Kotak Gold Fund",                 "Gold/Silver"),
    ("HDFC Balanced Advantage Fund",    "Hybrid"),
    ("Axis Liquid Fund",                "Debt"),
    ("Anonymous Equity Fund",           "Misc Fund"),   # default fallback (DEFAULT_CATEGORY)
])
def test_classify_without_amfi_category(name, expected):
    assert classify_fund(name, "") == expected
