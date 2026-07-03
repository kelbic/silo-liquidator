#!/usr/bin/env python3
"""market_value.py — недостающий ЗНАМЕНАТЕЛЬ EV: бонус-пул $/мес по каждому рынку. (read-only)

market_survey.py дал АКТИВНОСТЬ (кол-во ликвидаций) и доступ (gated/open). Но EV живёт на ДЕНЬГАХ:
бонус-пул/мес = repaid$/мес × liquidationFee(залоговая сторона). Этот тул считает его на цепочке:
repaid-объём в токенах долга × цена токена (USD) × комиссия. Плюс концентрация победителей (кто уже сидит).

Цены [измерено] где есть фид: USDC=1, wS/S — из живого S/USD-агрегатора; иначе помечается '?' (не гадаем).
Комиссия — с ЗАЛОГОВОЙ стороны пары (она ценит премию, §read_fee).

Критично помнить при чтении: «concentration ≤72%» ≠ «polling-контестабелен». Рынок может иметь ≤72%
концентрацию, но быть same-block-гонкой (0x7e88ae5e) — polling его не берёт. Контестабельность по lag
меряет backtest_detection.py, не этот тул. Здесь — только деньги и концентрация.

Запуск:
  python3 -m analysis.market_value --rpc https://rpc.soniclabs.com --days 30 --top 15
"""
from __future__ import annotations
import argparse
import sys
from collections import defaultdict

from analysis.contestation import RPC, RpcError, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts
from analysis.read_fee import SEL_CONFIG, SEL_GETSILOS, SEL_GETCONFIG, parse_config, _word

USDC = "0x29219dd400f2bf60e5a23d13be72b486d4038894"
WS = "0x039e2fb66102314ce7b64ce5ce3e5183bc94ad38"
S_USD_AGG = "0xc76dfb89ff298145b417d221b2c747d84952e01d"  # S/USD агрегатор (§2), decimals 8
SEL_LATEST_ROUND = "0xfeaf968c"
SEL_DECIMALS = "0x313ce567"
SEL_ASSET = "0x38d52e0f"


def bonus_pool_usd(repaid_token: float, price_usd: float, liq_fee: float) -> float:
    """Чистая функция (юнит-тестируемо): бонус-пул = repaid$ × комиссия. repaid_token — объём в токенах,
    price_usd — цена токена, liq_fee — доля (0.065 = 6.5%). Возврат $/за_период."""
    return repaid_token * price_usd * liq_fee


def s_price(rpc: RPC) -> float:
    r = rpc.eth_call(S_USD_AGG, SEL_LATEST_ROUND)
    return int(r[2:][64:128], 16) / 1e8


def liq_fee_collateral(rpc: RPC, debt_silo: str) -> float:
    config = "0x" + _word(rpc.eth_call(debt_silo, SEL_CONFIG), 0)[24:]
    s = rpc.eth_call(config, SEL_GETSILOS)
    s0, s1 = "0x" + _word(s, 0)[24:], "0x" + _word(s, 1)[24:]
    coll = s1 if s0.lower() == debt_silo else s0
    return parse_config(rpc.eth_call(config, SEL_GETCONFIG + coll[2:].rjust(64, "0")))["liquidationFee"] / 1e18


def main():
    ap = argparse.ArgumentParser(description="Бонус-пул $/мес по рынкам (знаменатель EV)")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    tip = rpc.block_number()
    frm = find_block_at_ts(rpc, rpc.block_ts(tip) - int(a.days * 86400), tip)
    sys.stderr.write(f"LiquidationCall всех силосов за {a.days:.0f}д...\n")
    logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000)
    evs = [e for e in (decode_liquidation_log(l) for l in logs) if e]
    by = defaultdict(list)
    for e in evs:
        by[e["silo"].lower()].append(e)

    S = s_price(rpc)
    scale = 30.0 / a.days  # к месяцу
    sys.stderr.write(f"[измерено] S/USD = {S:.4f}\n\n")

    print(f"{'силос':14s} {'liq':>4s} {'conc':>5s} {'repaid/мес':>16s} {'fee':>6s} {'бонус-пул$/мес':>15s}")
    rows = []
    for silo, es in sorted(by.items(), key=lambda kv: -len(kv[1])):
        w = defaultdict(int)
        for e in es:
            w[e["liquidator"].lower()] += 1
        conc = max(w.values()) / len(es)
        try:
            debt = "0x" + rpc.eth_call(silo, SEL_ASSET)[-40:]
            dec = int(rpc.eth_call(debt, SEL_DECIMALS), 16)
            vol = sum(e["repay_raw"] for e in es) / 10 ** dec * scale
            fee = liq_fee_collateral(rpc, silo)
        except (RpcError, RuntimeError):
            continue
        if debt == USDC:
            price, ptag = 1.0, ""
        elif debt == WS:
            price, ptag = S, ""
        else:
            price, ptag = None, "?"
        if price is None:
            print(f"{silo[:14]} {len(es):>4d} {conc:>4.0%} {vol:>14,.2f}т {fee:>5.1%}  цена {debt[:8]}? — н/д")
            continue
        pool = bonus_pool_usd(vol, price, fee)
        rows.append((silo, len(es), conc, vol * price, fee, pool))
        print(f"{silo[:14]} {len(es):>4d} {conc:>4.0%} ${vol*price:>14,.0f} {fee:>5.1%}  ${pool:>13,.0f}")

    print("\nНапоминание: conc≤72% ≠ polling-контестабелен (может быть same-block, см. backtest_detection.py).")
    print("Бонус-пул — ВАЛОВЫЙ потолок ДО доли захвата, нетто-маржи и издержек инфраструктуры.")


if __name__ == "__main__":
    main()
