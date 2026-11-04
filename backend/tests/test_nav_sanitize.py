"""NAV series sanitisation tests (face-value change / unit-jump splicing).

Real case: Invesco India Short Term Fund - Plan B - Daily Dividend
(scheme 105186) — NAV ≈ ₹10 until Apr-2013, ≈ ₹1000 after (face-value
change). Uncorrected, engines read this as a ~100× return and produced a
47.81× WM for a short-term debt fund.
"""
from datetime import date, timedelta

from amfi_client import sanitize_nav_history, parse_nav_history
from category_map import classify_fund


def _series(specs):
    return [{"date": d, "nav": n} for d, n in specs]


def test_face_value_jump_up_is_rebased():
    hist = _series([
        (date(2013, 4, 16), 10.0),
        (date(2013, 4, 17), 10.01),
        (date(2013, 4, 18), 10.02),
        (date(2013, 4, 19), 1002.5),   # ₹10 → ₹1000 face-value change
        (date(2013, 4, 22), 1003.0),
    ])
    out = sanitize_nav_history(hist)
    ratio = 1002.5 / 10.02
    assert out[0]["nav"] == 10.0 * ratio
    assert out[2]["nav"] == 10.02 * ratio          # continuous at the splice
    assert out[3]["nav"] == 1002.5                  # post-jump untouched
    # day-over-day returns now all tiny
    for a, b in zip(out, out[1:]):
        assert 0.9 < b["nav"] / a["nav"] < 1.1


def test_downward_unit_jump_is_rebased():
    hist = _series([
        (date(2020, 1, 1), 1000.0),
        (date(2020, 1, 2), 10.05),   # decimal glitch
        (date(2020, 1, 3), 10.06),
    ])
    out = sanitize_nav_history(hist)
    assert abs(out[0]["nav"] - 1000.0 * (10.05 / 1000.0)) < 1e-9
    assert out[1]["nav"] == 10.05


def test_jump_across_long_gap_untouched():
    """A big move across a multi-month data hole may be real growth."""
    hist = _series([
        (date(2010, 1, 1), 10.0),
        (date(2014, 1, 1), 45.0),   # 4 years later — legit
    ])
    out = sanitize_nav_history(hist)
    assert out[0]["nav"] == 10.0
    assert out[1]["nav"] == 45.0


def test_normal_volatility_untouched():
    d0 = date(2020, 1, 1)
    hist = _series([(d0 + timedelta(days=i), 100.0 * (1 + 0.02 * (-1) ** i)) for i in range(10)])
    out = sanitize_nav_history(hist)
    assert [r["nav"] for r in out] == [100.0 * (1 + 0.02 * (-1) ** i) for i in range(10)]


def test_parse_nav_history_applies_sanitisation():
    payload = {"data": [
        {"date": "19-04-2013", "nav": "1002.5"},
        {"date": "18-04-2013", "nav": "10.02"},
        {"date": "17-04-2013", "nav": "10.01"},
    ]}
    out = parse_nav_history(payload)
    assert out[-1]["nav"] == 1002.5
    assert abs(out[1]["nav"] - 1002.5) < 1.5   # rebased into the new unit


def test_debt_name_fallback_classification():
    """Corrupted AMFI meta (scheme_category='Direct') must still land in Debt
    via the name fallback so the Debt WM sanity ceiling applies."""
    assert classify_fund("Invesco India Short Term Fund- Plan B - Daily Dividend", "Direct") == "Debt"
    assert classify_fund("Aditya Birla Sun Life Ultra Short Term Fund", "") == "Debt"
    # equity names must not be caught
    assert classify_fund("Nippon India Small Cap Fund", "Equity Scheme - Small Cap Fund") == "Small Cap"
