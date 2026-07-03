#!/usr/bin/env python3
"""paper_trader.py — PAPER-режим детектора на рынке: логирует ПОПАДАНИЯ/ПРОМАХИ vs реальных победителей.
(read-only, НИ ОДНОЙ транзакции — только чтение цепочки и бумажный леджер.)

Что делает: для каждой РЕАЛЬНОЙ ликвидации на рынке восстанавливает, когда МЫ бы её задетектили
(polling: первый неплатёжеспособный блок E через walk-back), и сравнивает с блоком победителя L:
  • lag = L − E ≥ 2  → HIT       — мы бы подали tx в блок ≤ L, обгоняем победителя (polling берёт)
  • lag = 1          → CONTESTED — детект на L−1, но наш tx в том же блоке L, что победитель: гонка по tip
  • lag = 0          → MISS      — same-block (оракул+ликвидация в одном блоке), polling опаздывает всегда
Оценивает НЕТТО, что мы бы забрали на HIT (route-A: repay×liqFee − газ), ведёт бумажный P&L и леджер.

НЕ бизнес-совет и НЕ гарантия: HIT = «polling успел бы по блокам». Реальная победа на CONTESTED зависит
от tip/латентности (не моделируется). Инкумбент может подтянуться (см. STATE.md ⭐⭐ оговорки). Захват на
CONTESTED задаётся флагом --contested-winrate как ДОПУЩЕНИЕ, по умолчанию 0 (консервативно не считаем).

Режимы:
  ретро (по умолчанию): классифицирует все ликвидации за --days, пишет леджер, печатает сводку.
  --follow: после ретро-засева крутится вперёд, дополняя леджер по мере новых ликвидаций.

Запуск:
  python3 -m analysis.paper_trader --rpc https://rpc.soniclabs.com \
      --silo 0x4e216c15697c1392fe59e1014b009505e05810df --days 30
  python3 -m analysis.paper_trader --rpc ... --silo 0x4e216c15... --follow --poll 5
"""
from __future__ import annotations
import argparse
import json
import sys
import time

from analysis.contestation import RPC, RpcError, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts
from analysis.live_detector import resolve_market, gross_premium_debt
from analysis.backtest_detection import cluster_episodes, walk_back_insolvency, make_is_solvent_at

HIT, CONTESTED, MISS = "HIT", "CONTESTED", "MISS"


def classify(lag: int, same_block: bool) -> str:
    """Чистая классификация исхода polling-бота против реального победителя. Юнит-тестируемо."""
    if same_block or lag <= 0:
        return MISS
    if lag == 1:
        return CONTESTED
    return HIT


def episode_net_usd(repay_raw: int, liq_fee: float, debt_decimals: int, debt_price_usd: float,
                    gas_usd: float) -> float:
    """НЕТТО route-A на эпизоде: валовая премия (repay×fee) в $ минус газ. Слиппедж на малых кусках
    пренебрежим (STATE.md ⭐⭐), поэтому нетто≈валовому−газ. Чистая функция."""
    gross_usd = gross_premium_debt(repay_raw, int(liq_fee * 10 ** 18)) / 10 ** debt_decimals * debt_price_usd
    return gross_usd - gas_usd


def tally(ledger: list[dict], contested_winrate: float) -> dict:
    """Сводка бумажного P&L. captured$ = сумма нетто по HIT + доля --contested-winrate по CONTESTED."""
    n = {HIT: 0, CONTESTED: 0, MISS: 0}
    captured = 0.0
    hit_pnl = 0.0
    contested_pnl = 0.0
    for r in ledger:
        n[r["class"]] += 1
        if r["class"] == HIT:
            hit_pnl += max(0.0, r["net_usd"])
        elif r["class"] == CONTESTED:
            contested_pnl += max(0.0, r["net_usd"]) * contested_winrate
    captured = hit_pnl + contested_pnl
    total = sum(n.values())
    return {"n": n, "total": total,
            "hit_rate": n[HIT] / total if total else 0.0,
            "contested_rate": n[CONTESTED] / total if total else 0.0,
            "miss_rate": n[MISS] / total if total else 0.0,
            "captured_usd": captured, "hit_pnl": hit_pnl, "contested_pnl": contested_pnl}


def classify_episode(rpc: RPC, mkt: dict, ep: dict, price_usd: float, gas_usd: float) -> dict:
    """Один эпизод → запись леджера с классом и нетто."""
    silo = mkt["debt_silo"]
    b, L = ep["borrower"], ep["first_block"]
    wb = walk_back_insolvency(make_is_solvent_at(rpc, silo, b), L)
    cls = classify(wb["lag"], wb["same_block"])
    net = episode_net_usd(ep["repay_raw_total"], mkt["liq_fee_wei"] / 1e18, mkt["debt_decimals"], price_usd, gas_usd)
    return {"borrower": b, "liq_block": L, "first_insolvent": wb["first_insolvent"], "lag": wb["lag"],
            "same_block": wb["same_block"], "class": cls, "winner": ep["first_liquidator"],
            "n_events": ep["n_events"], "repay_raw": ep["repay_raw_total"], "net_usd": round(net, 4)}


def print_row(r: dict, dec: int, sym: str):
    mark = {HIT: "✅ HIT", CONTESTED: "🟡 CONTESTED", MISS: "❌ MISS"}[r["class"]]
    lag = "same-block" if r["same_block"] else f"lag={r['lag']}"
    sys.stderr.write(f"  {mark:14s} {r['borrower'][:12]} L={r['liq_block']} {lag:11s} "
                     f"repay={r['repay_raw']/10**dec:,.2f}{sym} нетто≈${r['net_usd']:.3f} winner={r['winner'][:10]}\n")


def load_ledger(path: str) -> list[dict]:
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


def append_ledger(path: str, rows: list[dict]):
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def print_summary(ledger: list[dict], mkt: dict, contested_winrate: float, days: float):
    t = tally(ledger, contested_winrate)
    print("\n================ БУМАЖНЫЙ P&L (paper) ================")
    print(f"рынок {mkt['debt_silo']}  ({mkt['debt_symbol']}-долг / {mkt['coll_symbol']}-залог)")
    print(f"эпизодов: {t['total']}  |  ✅HIT {t['n'][HIT]} ({t['hit_rate']:.0%})  "
          f"🟡CONTESTED {t['n'][CONTESTED]} ({t['contested_rate']:.0%})  ❌MISS {t['n'][MISS]} ({t['miss_rate']:.0%})")
    print(f"бумажный захват$: HIT ${t['hit_pnl']:.2f} + CONTESTED@{contested_winrate:.0%} ${t['contested_pnl']:.2f} "
          f"= ${t['captured_usd']:.2f} за {days:.0f}д")
    if days:
        print(f"  → экстраполяция ${t['captured_usd']*30/days:.2f}/мес  [ДОПУЩЕНИЕ: HIT-исходы, contested-winrate={contested_winrate:.0%}]")
    print("  Напоминание: HIT = polling успел бы ПО БЛОКАМ; реальная победа зависит от tip/латентности и апатии инкумбента.")


def main():
    ap = argparse.ArgumentParser(description="Paper-режим детектора: hit/miss vs реальных победителей")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", default="0x4e216c15697c1392fe59e1014b009505e05810df")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--min-repay-usd", type=float, default=1.0, help="отсечь пыль")
    ap.add_argument("--gas-usd", type=float, default=0.002, help="газ на ликвидацию, $ (Sonic дёшев)")
    ap.add_argument("--debt-price-usd", type=float, default=1.0, help="цена долгового токена (USDC=1)")
    ap.add_argument("--contested-winrate", type=float, default=0.0, help="[ДОП] доля CONTESTED, что мы бы взяли")
    ap.add_argument("--ledger", default=None, help="путь к леджеру (по умолчанию paper_ledger_<silo6>.jsonl)")
    ap.add_argument("--follow", action="store_true", help="после ретро — крутиться вперёд")
    ap.add_argument("--poll", type=float, default=10.0)
    a = ap.parse_args()

    rpc = RPC(a.rpc)
    silo = a.silo.lower()
    mkt = resolve_market(rpc, silo)
    dec, sym = mkt["debt_decimals"], mkt["debt_symbol"]
    ledger_path = a.ledger or f"paper_ledger_{silo[2:8]}.jsonl"
    min_repay_raw = int(a.min_repay_usd * 10 ** dec)

    sys.stderr.write(f"PAPER на {silo} ({sym}-долг/{mkt['coll_symbol']}-залог), liqFee={mkt['liq_fee_wei']/1e18*100:.2f}%, "
                     f"газ=${a.gas_usd}, леджер={ledger_path}\n")

    tip = rpc.block_number()
    frm = find_block_at_ts(rpc, rpc.block_ts(tip) - int(a.days * 86400), tip)
    logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000)
    events = [e for e in (decode_liquidation_log(l) for l in logs) if e and e["silo"].lower() == silo]
    episodes = [e for e in cluster_episodes(events) if e["repay_raw_total"] >= min_repay_raw]
    sys.stderr.write(f"ретро: {len(events)} событий → {len(episodes)} эпизодов ≥${a.min_repay_usd:.0f}\n")

    seen = {(r["borrower"], r["liq_block"]) for r in load_ledger(ledger_path)}
    fresh = []
    for ep in sorted(episodes, key=lambda x: x["first_block"]):
        key = (ep["borrower"], ep["first_block"])
        if key in seen:
            continue
        try:
            row = classify_episode(rpc, mkt, ep, a.debt_price_usd, a.gas_usd)
        except (RpcError, RuntimeError) as ex:
            sys.stderr.write(f"  пропуск {ep['borrower'][:12]}: {str(ex)[:60]}\n")
            continue
        fresh.append(row)
        seen.add(key)
        print_row(row, dec, sym)
    append_ledger(ledger_path, fresh)
    print_summary(load_ledger(ledger_path), mkt, a.contested_winrate, a.days)

    if not a.follow:
        return
    sys.stderr.write(f"\n[follow] опрос каждые {a.poll}с, дополняю леджер по новым ликвидациям…\n")
    last = tip
    while True:
        try:
            time.sleep(a.poll)
            now = rpc.block_number()
            if now <= last:
                continue
            nlogs = fetch_liquidation_logs(rpc, last + 1, now, chunk=10_000)
            nevs = [e for e in (decode_liquidation_log(l) for l in nlogs) if e and e["silo"].lower() == silo]
            for ep in cluster_episodes(nevs):
                if ep["repay_raw_total"] < min_repay_raw:
                    continue
                key = (ep["borrower"], ep["first_block"])
                if key in seen:
                    continue
                row = classify_episode(rpc, mkt, ep, a.debt_price_usd, a.gas_usd)
                append_ledger(ledger_path, [row]); seen.add(key)
                print_row(row, dec, sym)
            last = now
        except (RpcError, RuntimeError) as ex:
            sys.stderr.write(f"  RPC-сбой, продолжаю: {str(ex)[:60]}\n")
        except KeyboardInterrupt:
            print_summary(load_ledger(ledger_path), mkt, a.contested_winrate, a.days)
            return


if __name__ == "__main__":
    main()
