#!/usr/bin/env python3
"""verify_pool.py — ПОСТРОЧНЫЙ аудит repaid одного силоса. Проверка $-числа ДО того, как ему верить. (read-only)

Зачем: несущая находка «настоящий приз на 0x322e1d53, ~$397k/мес» держится на сумме repaid×price. Сумма может
быть раздута артефактом (неверные decimals долгового токена ИЛИ битая цена DeFiLlama на части событий) — и с
агрегата это НЕ отличить от реального. Подпись тревоги уже в логе: на одном силосе/токене чек 0xccd487 = $1359,
а 0x0094c5 = $79 (×17 разброс). Это либо правда (крупный игрок vs спамер копеек), либо кривой decimals/цена.

Что печатает (всё сырое, глазами проверяемо):
  • долговой токен силоса: адрес, symbol, decimals (из eth_call), и цену DeFiLlama этого токена;
  • N крупнейших ликвидаций ПОШТУЧНО: block, tx, repay_raw (hex→int), размер = raw/10^dec, USD, победитель;
  • гистограмму по величине USD-чека (сколько ликвидаций в каждом порядке величины) — артефакт обычно
    кучкуется в один нереальный порядок;
  • сверку: сумма построчных USD должна совпасть с repaid по силосу из pool_size.

Красные флаги, которые ты ищешь глазами:
  • размеры «ровные» гигантские (10^7, 10^8) при копеечной цене → почти наверняка decimals не тот;
  • цена DeFiLlama сильно != $1 для стейбла (USDC ~1.00) → битая цена;
  • пара «китов» даёт весь пул, остальное копейки → приз не потоковый, а от 1–2 событий.

Запуск:
  python3 -m analysis.verify_pool --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 --days 30
"""
from __future__ import annotations
import argparse
import sys
from collections import Counter, defaultdict

from analysis.contestation import (
    RPC, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts,
    silo_token_meta, llama_prices,
)


def order_of_magnitude(usd: float) -> int:
    """Порядок величины USD-чека (floor log10); для None/0 → -99."""
    import math
    if not usd or usd <= 0:
        return -99
    return int(math.floor(math.log10(usd)))


def histogram_by_oom(events: list) -> dict:
    """{порядок_величины: count} по USD-чеку — артефакт кучкуется в один нереальный порядок."""
    h = Counter()
    for e in events:
        h[order_of_magnitude(e.get("usd"))] += 1
    return h


def audit(rpc: RPC, silo: str, chain: str, days: float, topn: int) -> dict:
    """Собрать построчные данные по силосу. Возвращает dict с meta/price/events/hist/sum."""
    silo = silo.lower()
    tip = rpc.block_number()
    frm = find_block_at_ts(rpc, rpc.block_ts(tip) - int(days * 86400), tip)
    logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000)
    events = [e for e in (decode_liquidation_log(l) for l in logs) if e]
    seen, uniq = set(), []
    for e in events:
        k = (e["tx"], e["log_index"])
        if k not in seen:
            seen.add(k); uniq.append(e)
    target = [e for e in uniq if e["silo"].lower() == silo]
    meta_cache = {}
    meta = silo_token_meta(rpc, silo, meta_cache)
    prices = llama_prices(chain, {meta["token"]} if meta.get("token") else set())
    px = prices.get((meta.get("token") or "").lower())
    for e in target:
        e["repay"] = e["repay_raw"] / (10 ** meta["decimals"])
        e["usd"] = (e["repay"] * px) if px else None
    target.sort(key=lambda e: (e["usd"] if e["usd"] is not None else -1), reverse=True)
    total_usd = sum(e["usd"] for e in target if e["usd"] is not None)
    return {"meta": meta, "price": px, "events": target, "count": len(target),
            "hist": histogram_by_oom(target), "total_usd": total_usd,
            "unpriced": sum(1 for e in target if e["usd"] is None)}


def main():
    ap = argparse.ArgumentParser(description="Построчный аудит repaid одного силоса Silo")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", required=True)
    ap.add_argument("--chain", default="sonic")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--top", type=int, default=15, help="сколько крупнейших ликвидаций печатать поштучно")
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    sys.stderr.write(f"аудит силоса {a.silo[:12]}… за {a.days:g}д…\n")
    r = audit(rpc, a.silo, a.chain, a.days, a.top)
    m = r["meta"]

    print("=" * 78)
    print(f"  VERIFY POOL — построчный аудит {a.silo}")
    print("=" * 78)
    print(f"долговой токен: {m.get('token')}  symbol={m.get('symbol')}  decimals={m.get('decimals')}")
    print(f"цена DeFiLlama этого токена: {r['price']}  "
          f"{'⚠ для стейбла ожидается ~1.00' if r['price'] and abs(r['price']-1.0) > 0.1 else ''}")
    print(f"ликвидаций на силосе: {r['count']}  (без цены: {r['unpriced']})")
    print(f"сумма построчных USD: ${r['total_usd']:,.0f}  (должна совпасть с repaid силоса из pool_size)")

    print(f"\n──── {a.top} КРУПНЕЙШИХ ЛИКВИДАЦИЙ ПОШТУЧНО ────")
    print(f"  {'block':>10s} {'repay_raw':>26s} {'размер':>16s} {'USD':>12s}  победитель")
    for e in r["events"][: a.top]:
        raw = e["repay_raw"]
        print(f"  {e['block']:>10d} {raw:>26d} {e['repay']:>16,.2f} "
              f"${(e['usd'] or 0):>11,.2f}  {e['liquidator']}")
    if r["events"]:
        sm = r["events"][-1] if len(r["events"]) <= a.top else r["events"][a.top]
        print(f"  … (медиана/хвост мельче; крупнейший ${r['events'][0]['usd'] or 0:,.0f}, "
              f"смотри распределение ниже)")

    print(f"\n──── РАСПРЕДЕЛЕНИЕ ПО ПОРЯДКУ ВЕЛИЧИНЫ ЧЕКА (артефакт кучкуется в один порядок) ────")
    for oom in sorted(r["hist"].keys(), reverse=True):
        cnt = r["hist"][oom]
        if oom == -99:
            print(f"  без цены:            {cnt:5d}")
        else:
            lo, hi = 10 ** oom, 10 ** (oom + 1)
            bar = "█" * min(50, cnt)
            print(f"  ${lo:>10,.0f}–${hi:<12,.0f} {cnt:5d}  {bar}")

    # концентрация: сколько дают топ-события
    priced = [e for e in r["events"] if e["usd"] is not None]
    if priced and r["total_usd"] > 0:
        top5 = sum(e["usd"] for e in priced[:5])
        print(f"\n──── КОНЦЕНТРАЦИЯ ────")
        print(f"  топ-5 ликвидаций = ${top5:,.0f} = {top5/r['total_usd']*100:.0f}% всего пула силоса")
        print(f"  → если высоко: 'пул' это 1–5 событий, не поток; спам-бот в остальное время берёт копейки.")

    print("\n" + "=" * 78)
    print("ГЛАЗАМИ: размеры 'ровные' 10^7/10^8 при цене~$1 → decimals не тот (repaid раздут на порядки).")
    print("Цена стейбла != ~1.00 → битая цена DeFiLlama. Пара китов = весь пул → приз не потоковый.")
    print("Только если чеки правдоподобны и распределены — '$397k приз' установлен, а не предъявлен.")


if __name__ == "__main__":
    main()
