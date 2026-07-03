#!/usr/bin/env python3
"""Офлайн-тест чистой функции бонус-пула — без RPC. python3 -m analysis.test_market_value"""
from analysis.market_value import bonus_pool_usd


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_usdc_market():
    # 396,559 USDC repaid × 6.5% = $25,776 (gated 0x322e1d53)
    assert approx(bonus_pool_usd(396559, 1.0, 0.065), 25776.335, 0.01)


def test_ws_market_tiny():
    # 26,269 wS × $0.0264 × 3.5% = ~$24 (soft 0x11238006) — знаменатель EV
    p = bonus_pool_usd(26269, 0.0264, 0.035)
    assert approx(p, 24.27, 0.1), p


def test_zero_volume():
    assert bonus_pool_usd(0, 1.0, 0.065) == 0


def test_scales_linearly():
    base = bonus_pool_usd(1000, 0.5, 0.05)
    assert approx(bonus_pool_usd(2000, 0.5, 0.05), 2 * base)
    assert approx(bonus_pool_usd(1000, 1.0, 0.05), 2 * base)


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t(); print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} тестов прошли.")


if __name__ == "__main__":
    run()
