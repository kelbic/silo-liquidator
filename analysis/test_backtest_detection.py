#!/usr/bin/env python3
"""Офлайн-тесты чистой логики backtest_detection.py — без RPC. Колбэк is_solvent_at инъецируется
синтетическими профилями платёжеспособности, включая осцилляцию и same-block.

Запуск:  python3 -m analysis.test_backtest_detection
"""
from analysis.backtest_detection import cluster_episodes, walk_back_insolvency, model_check, summarize

USDC = 10 ** 6
WS = 10 ** 18
FEE = 65 * 10 ** 15  # 6.5%


def mk_solv(insolvent_from: int, insolvent_to: int):
    """Профиль: неплатёжеспособен на [insolvent_from, insolvent_to], платёжеспособен вне."""
    return lambda b: not (insolvent_from <= b <= insolvent_to)


def test_walk_back_simple():
    # ликвидация в блоке 1000, неплатёжеспособен с 990 — lag должен быть ровно 10
    r = walk_back_insolvency(mk_solv(990, 1005), liq_block=1000)
    assert r["lag"] == 10 and r["first_insolvent"] == 990 and not r["same_block"], r


def test_walk_back_lag_one():
    # стал ликвидируем ровно в предыдущем блоке — lag=1 (минимально ловимый polling'ом)
    r = walk_back_insolvency(mk_solv(999, 1005), liq_block=1000)
    assert r["lag"] == 1 and r["first_insolvent"] == 999, r


def test_walk_back_same_block():
    # платёжеспособен на L-1 (оракул и ликвидация в одном блоке) — same_block, lag=0
    r = walk_back_insolvency(mk_solv(1000, 1000), liq_block=1000)
    assert r["same_block"] and r["lag"] == 0, r


def test_walk_back_oscillation_takes_final_run():
    # осцилляция: [900..910] неплатёжеспособен, [911..979] платёжеспособен, [980..1005] снова нет.
    # Должен найти границу ФИНАЛЬНОГО забега (980), не раннего (900).
    def solv(b):
        return not (900 <= b <= 910 or 980 <= b <= 1005)
    r = walk_back_insolvency(solv, liq_block=1000)
    assert r["first_insolvent"] == 980 and r["lag"] == 20, r


def test_walk_back_long_run_truncated():
    # забег длиннее max_lookback — lag возвращается как НИЖНЯЯ ОЦЕНКА с флагом truncated.
    # При удвоении глубочайший зонд перед капом = max_lookback/2, значит гарантия ≥ max_lookback/2+1.
    r = walk_back_insolvency(mk_solv(0, 10_000_000), liq_block=1_000_000, max_lookback=1000)
    assert r["truncated"] and r["lag"] > 1000 // 2, r


def test_walk_back_call_budget():
    # doubling+bisect: даже lag ~100k — десятки вызовов, не тысячи (RPC-бюджет)
    r = walk_back_insolvency(mk_solv(900_000, 1_000_005), liq_block=1_000_000)
    assert r["lag"] == 100_000 and r["calls"] < 60, r["calls"]


def _ev(borrower, block, repay, withdraw, idx=0, liquidator="0xliq"):
    return {"borrower": borrower, "block": block, "log_index": idx, "liquidator": liquidator,
            "repay_raw": repay, "withdraw_raw": withdraw}


def test_cluster_episodes_gap_split():
    evs = [_ev("0xa", 100, 10, 11), _ev("0xa", 150, 20, 21),      # каскад 1 (разрыв 50 < 1000)
           _ev("0xa", 5000, 30, 31),                                # каскад 2 (разрыв 4850 > 1000)
           _ev("0xb", 120, 40, 41)]                                 # другой заёмщик
    eps = cluster_episodes(evs)
    assert len(eps) == 3, eps
    a1 = next(e for e in eps if e["borrower"] == "0xa" and e["first_block"] == 100)
    assert a1["n_events"] == 2 and a1["repay_raw_total"] == 30 and a1["withdraw_raw_total"] == 32
    assert a1["repay_raw_first"] == 10  # первое событие эпизода, не сумма
    a2 = next(e for e in eps if e["first_block"] == 5000)
    assert a2["n_events"] == 1 and a2["repay_raw_total"] == 30


def test_model_check_math():
    # repay $1000; изъято 1065 wS при цене S=$1.00 → seized $1065, реализованный gross $65;
    # предсказанный gross = 1000×6.5% = $65 → ratio 1.0
    m = model_check(1000 * USDC, 1065 * WS, int(1.00e8), FEE, 6, 18)
    assert abs(m["ratio"] - 1.0) < 1e-6, m
    # цена S=$0.50 при том же withdraw → seized $532.5, gross -$467.5 → ratio отрицательный
    m2 = model_check(1000 * USDC, 1065 * WS, int(0.50e8), FEE, 6, 18)
    assert m2["ratio"] < 0, m2


def test_summarize_criteria():
    rows = [
        {"lag": 5, "same_block": False, "model": {"ratio": 1.05}},
        {"lag": 1, "same_block": False, "model": {"ratio": 0.85}},
        {"lag": 0, "same_block": True,  "model": {"ratio": 1.30}},   # same-block: отдельная категория
        {"lag": 12, "same_block": False, "model": None},              # без модели — не в статистике C
    ]
    s = summarize(rows)
    assert s["n"] == 4 and s["same_block"] == 1 and s["non_same"] == 3
    assert s["catchable"] == 3           # все не-same-block имеют lag≥1
    assert s["model_n"] == 3 and s["model_within20"] == 2  # 1.05, 0.85 в ±20%; 1.30 — нет
    assert s["lags_sorted"] == [1, 5, 12]


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} тестов прошли.")


if __name__ == "__main__":
    run()
