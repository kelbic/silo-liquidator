#!/usr/bin/env python3
"""Офлайн юнит-тесты модели прибыли и формы кандидата live_detector.py — без RPC, только stdlib.

Запуск:  python3 -m analysis.test_live_detector
Философия (как у всего трека): проверить арифметику независимо, не поверить коду на слово.
"""
from analysis.live_detector import gross_premium_debt, net_profit_debt, build_candidate

WEI = 10 ** 18
FEE_650 = 65 * WEI // 1000  # 6.5% == 0.065e18 — фактический liquidationFee целевого рынка
USDC = 10 ** 6


def approx(a, b, tol):
    return abs(a - b) <= tol


def test_gross_premium():
    # погашаем 100 USDC долга, премия 6.5% → 6.5 USDC валовой премии
    g = gross_premium_debt(100 * USDC, FEE_650)
    assert approx(g, int(6.5 * USDC), 1), g
    # нулевой долг → нулевая премия
    assert gross_premium_debt(0, FEE_650) == 0


def test_net_no_slippage_no_gas():
    # без слиппеджа и газа нетто должно равняться валовой премии:
    # seized = repay*(1+fee); after=seized; net = seized - repay = repay*fee = gross
    repay = 1000 * USDC
    net = net_profit_debt(repay, FEE_650, slippage_bps=0, gas_cost_debt_raw=0)
    assert net == gross_premium_debt(repay, FEE_650), (net, gross_premium_debt(repay, FEE_650))


def test_net_slippage_eats_premium():
    # 5% слиппедж на seized (≈1.065*repay) съедает больше, чем 6.5% премия → нетто отрицательное
    repay = 1000 * USDC
    net = net_profit_debt(repay, FEE_650, slippage_bps=500, gas_cost_debt_raw=0)
    # seized=1065, after=1065*0.95=1011.75, net=1011.75-1000=+11.75 ... проверим знак и величину
    assert approx(net, int(11.75 * USDC), USDC), net
    # 7% слиппедж → seized*0.93=990.45, net=-9.55 (отрицательно) — премия не покрывает слиппедж
    net7 = net_profit_debt(repay, FEE_650, slippage_bps=700, gas_cost_debt_raw=0)
    assert net7 < 0, net7


def test_net_gas_subtracted():
    repay = 1000 * USDC
    base = net_profit_debt(repay, FEE_650, slippage_bps=50, gas_cost_debt_raw=0)
    with_gas = net_profit_debt(repay, FEE_650, slippage_bps=50, gas_cost_debt_raw=5 * USDC)
    assert base - with_gas == 5 * USDC, (base, with_gas)


def test_monotonic_in_size():
    # больше долг → больше нетто (при тех же ставках) — санити на масштабируемость
    small = net_profit_debt(100 * USDC, FEE_650, 50, USDC)
    big = net_profit_debt(100_000 * USDC, FEE_650, 50, USDC)
    assert big > small > 0, (small, big)


def test_candidate_shape():
    # кандидат обязан нести ровно параметры executeLiquidation, maxDebtToCover из maxLiquidation (не гадаем)
    c = build_candidate(
        borrower="0xborrower", silo="0xsilo", hook="0xhook",
        collateral_asset="0xcoll", debt_asset="0xdebt",
        coll_to_liq_raw=1065 * USDC, debt_to_repay_raw=1000 * USDC, s_token_required=False,
        net_raw=64 * USDC, gross_raw=65 * USDC, debt_decimals=6,
    )
    for k in ("flashLoanFrom", "hook", "collateralAsset", "debtAsset", "user", "maxDebtToCover", "minProfit_suggested"):
        assert k in c, f"кандидат без обязательного поля {k}"
    assert c["flashLoanFrom"] == "0xsilo"           # флеш из нашего долгового силоса (asset==debt)
    assert c["maxDebtToCover"] == 1000 * USDC       # из maxLiquidation, не выдумано
    assert c["user"] == "0xborrower"
    assert c["minProfit_suggested"] == 64 * USDC
    assert approx(c["net_estimate_debt"], 64.0, 0.001)


def test_negative_net_floors_minprofit_at_zero():
    # если нетто отрицательное, предлагаемый minProfit не должен уйти в минус (контракт всё равно ревертнёт)
    c = build_candidate("0xb", "0xs", "0xh", "0xc", "0xd", 0, 100 * USDC, False,
                        net_raw=-5 * USDC, gross_raw=1 * USDC, debt_decimals=6)
    assert c["minProfit_suggested"] == 0, c["minProfit_suggested"]


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} тестов прошли.")


if __name__ == "__main__":
    run()
