#!/usr/bin/env python3
"""market_survey.py — есть ли ОТКРЫТЫЕ Silo-рынки Sonic с живым хвостом ликвидаций? (read-only)

Мотив: fork-replay доказал, что целевой `0x322e1d53` пермишенован (вайтлист из 4, см. STATE.md ⭐).
Стратегическая развилка — вайтлистинг vs пивот на открытые рынки. Этот тул отвечает на дешёвый
диагностический вопрос под второй путь: перебирает ВСЕ силосы с реальными LiquidationCall за окно и
для каждого гоняет permission_gate_check — GATED (вайтлист) или OPEN (ликвидировать может любой).

Метод: LiquidationCall по topic0 без фильтра по силосу → группировка по event.silo (долговой силос:
Silo зовёт ISilo(debtConfig.silo).repay) → метрики активности (кол-во, распредление победителей, объём)
→ для каждого силоса резолв ЗАЛОГОВОГО гейджа и чтение ALLOWED_ROLE. Переиспользует всё готовое.

Открытый рынок с живым хвостом = кандидат под стратегию (пивот). Если ВСЕ активные рынки gated —
вывод: гейтинг это норма зрелых пар, и радар имеет смысл только на совсем свежих деплоях (до появления
первых ликвидаций/вайтлиста).

Запуск:
  python3 -m analysis.market_survey --rpc https://rpc.soniclabs.com --days 14 --top 15
"""
from __future__ import annotations
import argparse
from collections import defaultdict

from analysis.contestation import RPC, RpcError, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts
from analysis.permission_gate_check import resolve_collateral_gate, read_allowed_role


def main():
    ap = argparse.ArgumentParser(description="Открытые vs пермишенованные Silo-рынки Sonic с ликвидациями")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--top", type=int, default=15, help="сколько самых активных силосов проверить на гейт")
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    tip = rpc.block_number()
    frm = find_block_at_ts(rpc, rpc.block_ts(tip) - int(a.days * 86400), tip)
    print(f"LiquidationCall по ВСЕМ силосам Sonic за {a.days:.0f}д (блоки {frm}..{tip}):")
    logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000)
    events = [e for e in (decode_liquidation_log(l) for l in logs) if e]

    by_silo: dict[str, list] = defaultdict(list)
    for e in events:
        by_silo[e["silo"].lower()].append(e)
    print(f"событий: {len(events)}  различных силосов: {len(by_silo)}\n")

    # ранжируем силосы по кол-ву ликвидаций; проверяем гейт у топ-N
    ranked = sorted(by_silo.items(), key=lambda kv: -len(kv[1]))[:a.top]

    print(f"{'силос':44s} {'#liq':>5s} {'#winners':>9s} {'top-winner доля':>16s}  доступ")
    open_markets = []
    for silo, evs in ranked:
        winners = defaultdict(int)
        for e in evs:
            winners[e["liquidator"].lower()] += 1
        top_share = max(winners.values()) / len(evs)
        # гейт: резолвим залоговый гейдж и читаем ALLOWED_ROLE
        try:
            g = resolve_collateral_gate(rpc, silo)
            role = read_allowed_role(rpc, g["gauge"])
            if role is None:
                access = "🟢 OPEN (нет ALLOWED_ROLE на залоговом гейдже)"
                open_markets.append((silo, len(evs)))
            else:
                access = f"🔒 GATED (вайтлист {role['count']})"
        except (RpcError, RuntimeError) as ex:
            access = f"? не резолвится: {str(ex)[:40]}"
        print(f"{silo:44s} {len(evs):>5d} {len(winners):>9d} {top_share:>15.0%}  {access}")

    print()
    if open_markets:
        print(f"🟢 ОТКРЫТЫХ рынков с ликвидациями: {len(open_markets)} — кандидаты под пивот:")
        for s, n in sorted(open_markets, key=lambda x: -x[1]):
            print(f"    {s}  ({n} ликвидаций за окно)")
    else:
        print("🔒 Среди топ-активных рынков ОТКРЫТЫХ нет — гейтинг похож на норму зрелых пар.")
        print("   Следствие: радар имеет смысл только на СВЕЖИХ деплоях (до первого вайтлиста),")
        print("   либо стратегия требует получения ALLOWED_ROLE у админа рынка.")


if __name__ == "__main__":
    main()
