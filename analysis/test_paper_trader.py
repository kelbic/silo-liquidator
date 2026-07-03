#!/usr/bin/env python3
"""Офлайн-тесты чистой логики paper_trader — без RPC. python3 -m analysis.test_paper_trader"""
from analysis.paper_trader import classify, episode_net_usd, tally, HIT, CONTESTED, MISS

USDC = 10 ** 6


def test_classify():
    assert classify(0, True) == MISS      # same-block
    assert classify(0, False) == MISS     # lag 0
    assert classify(1, False) == CONTESTED
    assert classify(2, False) == HIT
    assert classify(24, False) == HIT


def test_episode_net():
    # repay 1000 USDC, fee 6.5%, gas $0.002 → net = 65 - 0.002
    net = episode_net_usd(1000 * USDC, 0.065, 6, 1.0, 0.002)
    assert abs(net - (65.0 - 0.002)) < 1e-6, net
    # tiny chunk 10 USDC → net = 0.65 - 0.002
    net2 = episode_net_usd(10 * USDC, 0.065, 6, 1.0, 0.002)
    assert abs(net2 - (0.65 - 0.002)) < 1e-6, net2


def test_tally_counts_and_pnl():
    led = [
        {"class": HIT, "net_usd": 5.0},
        {"class": HIT, "net_usd": 3.0},
        {"class": CONTESTED, "net_usd": 4.0},
        {"class": MISS, "net_usd": 9.0},
    ]
    t = tally(led, contested_winrate=0.0)
    assert t["n"] == {HIT: 2, CONTESTED: 1, MISS: 1} and t["total"] == 4
    assert abs(t["hit_pnl"] - 8.0) < 1e-9
    assert t["contested_pnl"] == 0.0                     # winrate 0 → contested не считаем
    assert abs(t["captured_usd"] - 8.0) < 1e-9
    assert abs(t["hit_rate"] - 0.5) < 1e-9

    t2 = tally(led, contested_winrate=0.5)
    assert abs(t2["contested_pnl"] - 2.0) < 1e-9         # 4.0 × 0.5
    assert abs(t2["captured_usd"] - 10.0) < 1e-9         # HIT 8 + contested 2


def test_tally_ignores_negative_net():
    led = [{"class": HIT, "net_usd": -1.0}, {"class": HIT, "net_usd": 5.0}]
    t = tally(led, 0.0)
    assert abs(t["hit_pnl"] - 5.0) < 1e-9                # отрицательный нетто не добавляем


def test_empty_ledger():
    t = tally([], 0.0)
    assert t["total"] == 0 and t["captured_usd"] == 0.0 and t["hit_rate"] == 0.0


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t(); print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} тестов прошли.")


if __name__ == "__main__":
    run()
