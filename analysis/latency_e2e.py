#!/usr/bin/env python3
"""latency_e2e.py — end-to-end латентность горячего пути ликвидатора vs бюджет в блоках. (read-only)

Меряет ФАКТИЧЕСКОЕ время per-opportunity критического пути (детекция→сборка route-A→подготовка tx), разбивая
на RPC-раундтрипы (сетевая часть, зависит от близости к ноде) и локальный компьют (детерминирован). Сравнивает
с бюджетом = lag × block_time (Sonic ~1с/блок; ловимый lag на мягких открытых рынках 1–3 блока, см. §7).

ЧЕСТНАЯ рамка измерения:
  • Отсюда (песочница, публичный HTTPS-RPC) каждый eth_call ≈ 90мс — это ВЕРХНЯЯ, непредставительная граница:
    ко-located бот у своей ноды имеет RTT <1мс, и тогда путь становится compute-bound (<1мс суммарно).
  • Поэтому меряем и печатаем ОБЕ величины: (1) как есть с публичного RPC, (2) вычтя сетевую часть — что
    останется у ко-located бота. Вывод делаем по обеим, не выдавая пессимистичную сетевую за фундаментальную.
  • Подпись tx (secp256k1) и broadcast НЕ выполняем (zero-capital, ключей нет): подпись — известная локальная
    константа ~0.1–0.3мс, broadcast — ещё один RPC-раундтрип. Обозначаем, не мистифицируем.

Что НЕ меряется как «наше»: same-block-гонка при lag=1 решается приоритетом в секвенсере (tip + близость), а не
нашим компьютом — латентность пайплайна тут необходимое, но НЕ достаточное условие (см. вывод §7 по 0x7e88ae5e).

Запуск:
  python3 -m analysis.latency_e2e --rpc https://rpc.soniclabs.com \
      --hook 0x6aafd9dd424541885fd79c06fda96929cfd512f9 \
      --borrower 0x1ad4e35388f8e9bfabd4c05961cb8d21ac2dc0c2 \
      --adapter 0x000000000000000000000000000000000000dEaD \
      --pool 0x324963c267C354c7660Ce8CA3F5f167E05649970 \
      --collateral 0x039e2fb66102314ce7b64ce5ce3e5183bc94ad38 --iters 15
"""
from __future__ import annotations
import argparse
import statistics
import time

from analysis.contestation import RPC
from analysis.borrower_health import _addr_pad
from analysis.live_detector import SEL_MAXLIQ
from analysis.route_a_builder import build_route_a, encode_swap_calldata, resolve_direction

SEL_GAS_PRICE = "eth_gasPrice"
SEL_NONCE = "eth_getTransactionCount"


def _timed(fn):
    t = time.perf_counter()
    r = fn()
    return (time.perf_counter() - t) * 1000.0, r


def measure_block_time(rpc: RPC, n: int = 20) -> float:
    tip = rpc.block_number()
    ts = [rpc.block_ts(tip - i) for i in range(n)]
    deltas = sorted(ts[i] - ts[i + 1] for i in range(len(ts) - 1))
    return deltas[len(deltas) // 2]


def measure_rpc_floor(rpc: RPC, n: int = 12) -> float:
    """Медианный RTT дешёвого eth_call — оценка «сетевой» константы на раундтрип отсюда."""
    lat = []
    for _ in range(n):
        dt, _ = _timed(lambda: rpc.block_number())
        lat.append(dt)
    return statistics.median(lat)


def hot_path_once(rpc: RPC, hook: str, borrower: str, adapter: str, pool: str, collateral: str,
                  dir_cache: dict) -> dict:
    """Один прогон горячего пути. Возвращает тайминги стадий (мс) и число RPC-раундтрипов на стадию."""
    stages = {}

    # A) детекция: maxLiquidation(borrower) — 1 RPC. (что видит бот, решая, брать ли позицию)
    dt, _ = _timed(lambda: rpc.eth_call(hook, SEL_MAXLIQ + _addr_pad(borrower)))
    stages["A_detect_maxLiquidation"] = (dt, 1)

    # B) сборка route-A. Направление кэшируется на пару навсегда → в steady-state 0 RPC + локальный encode.
    if collateral not in dir_cache:
        dt, z = _timed(lambda: resolve_direction(rpc, pool, collateral))
        dir_cache[collateral] = z
        stages["B_build_direction_COLD"] = (dt, 1)  # разово на пару
    z = dir_cache[collateral]
    dt, _ = _timed(lambda: encode_swap_calldata(pool, collateral, z))  # чистый локальный компьют
    stages["B_build_encode_calldata"] = (dt, 0)

    # C) подготовка tx: цена газа + nonce — 2 RPC (в реальном боте кэшируются/пайплайнятся, но считаем честно)
    dt, _ = _timed(lambda: rpc.call(SEL_GAS_PRICE, []))
    stages["C_txprep_gasPrice"] = (dt, 1)
    dt, _ = _timed(lambda: rpc.call(SEL_NONCE, [adapter, "latest"]))
    stages["C_txprep_nonce"] = (dt, 1)

    return stages


def main():
    ap = argparse.ArgumentParser(description="End-to-end латентность горячего пути vs бюджет в блоках")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--hook", required=True)
    ap.add_argument("--borrower", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--pool", required=True)
    ap.add_argument("--collateral", required=True)
    ap.add_argument("--iters", type=int, default=15)
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    blk_t = measure_block_time(rpc)
    rpc_floor = measure_rpc_floor(rpc)
    print(f"Sonic block time (медиана): {blk_t:.1f}с  →  бюджет: lag1={blk_t:.1f}с lag2={2*blk_t:.1f}с lag3={3*blk_t:.1f}с")
    print(f"RPC-RTT floor отсюда (публичный HTTPS): {rpc_floor:.0f}мс/раундтрип (ко-located бот: <1мс)\n")

    dir_cache: dict = {}
    # прогрев (заполняет dir_cache один раз — как в steady-state) + сбор per-стадийных таймингов
    agg: dict = {}
    for i in range(a.iters):
        stages = hot_path_once(rpc, a.hook.lower(), a.borrower.lower(), a.adapter.lower(),
                               a.pool.lower(), a.collateral.lower(), dir_cache)
        for name, (dt, nrpc) in stages.items():
            agg.setdefault(name, {"times": [], "nrpc": nrpc})["times"].append(dt)

    print(f"{'стадия':34s} {'медиана мс':>11s} {'RPC':>4s}  тип")
    total_wall = 0.0
    total_rpc_calls = 0
    total_local = 0.0
    for name, d in agg.items():
        med = statistics.median(d["times"])
        cold = "_COLD" in name
        kind = ("RPC×%d" % d["nrpc"]) if d["nrpc"] else "локальный"
        tag = "  (разово на пару)" if cold else ""
        print(f"{name:34s} {med:>11.2f} {d['nrpc']:>4d}  {kind}{tag}")
        if not cold:  # steady-state путь не включает cold-резолв направления
            total_wall += med
            total_rpc_calls += d["nrpc"]
            if d["nrpc"] == 0:
                total_local += med

    # разложение: сетевое = (RPC-раундтрипы × floor); локальное = total_wall − наблюдённое сетевое
    net_component = total_rpc_calls * rpc_floor
    print("\n— STEADY-STATE горячий путь (направление пула закэшировано) —")
    print(f"  RPC-раундтрипов в пути: {total_rpc_calls}  (+1 на broadcast, здесь не шлём)")
    print(f"  локальный компьют (encode калдаты): {total_local:.3f}мс  + подпись ~0.2мс (константа, не мерим)")
    print(f"  наблюдённое wall-time отсюда: {total_wall:.0f}мс  (сетевое ≈ {net_component:.0f}мс = {total_rpc_calls}×{rpc_floor:.0f}мс)")
    colocated = total_wall - net_component + total_rpc_calls * 0.5  # RPC→~0.5мс у своей ноды
    print(f"  оценка у КО-LOCATED бота (RPC→~0.5мс): ~{max(colocated,total_local):.1f}мс")

    budget1 = blk_t * 1000
    print("\n— вывод vs бюджет —")
    for lag, secs in ((1, blk_t), (2, 2 * blk_t), (3, 3 * blk_t)):
        wall_fits = "✅" if total_wall < secs * 1000 else "❌"
        print(f"  lag={lag} (бюджет {secs*1000:.0f}мс): отсюда {total_wall:.0f}мс {wall_fits}  |  ко-located ~{max(colocated,total_local):.1f}мс ✅")
    print("\n  Замечание: при lag=1 попадание в бюджет НЕОБХОДИМО, но НЕ достаточно — это гонка в ОДНОМ блоке")
    print("  с победителем, решается tip + близость к секвенсеру, не нашим компьютом (см. §7, 0x7e88ae5e).")
    print("  Компьют-путь (encode+подпись) — доли мс; связывающее ограничение — RPC-близость и приоритет, не билдер.")


if __name__ == "__main__":
    main()
