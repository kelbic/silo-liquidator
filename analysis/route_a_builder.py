#!/usr/bin/env python3
"""route_a_builder.py — оффчейн-билдер «route A» swap-квоты для SiloLiquidator.executeLiquidation. (read-only)

Что такое route A и зачем свой билдер: инкумбент льёт залог через агрегатор/приватный роутер (маршрут B
теряет 2–4% на burn, §2). Наш путь — ПРЯМОЙ вызов концентрированного пула (UniV3-style) через V3SwapAdapter,
всё ончейн, без агрегаторной квоты (её и нельзя построить на исторический блок — fork-replay это показал).
Билдер производит РОВНО то, что принимает executeLiquidation: `swapTarget` (адаптер) + `swapCallData`.

Ключевое свойство для гонки: калдата route A ДЕТЕРМИНИРОВАНА и строится ЛОКАЛЬНО (после одного дешёвого
чтения token0 пула, кэшируемого на пару навсегда) — никаких HTTP-раундтрипов к агрегатору в горячем пути.
Адаптер сам берёт amountIn из allowance, поэтому в калдате НЕТ ни суммы, ни minOut — нечего пере-quote'ить
между детекцией и сабмишеном. Защита от проскальзывания — ончейн-пол minProfit контракта, не офчейн-квота.

Форма калдаты (сверено с cast и с проходящим fork-replay):
  swapCallData = selector(swap(address,address,bool)) ‖ pool ‖ tokenIn ‖ zeroForOne
  zeroForOne = (tokenIn == pool.token0())   — направление свопа залог→долг

Запуск (демо на паре целевого рынка):
  python3 -m analysis.route_a_builder --rpc https://rpc.soniclabs.com \
      --adapter 0xADAPTER --pool 0x324963c267C354c7660Ce8CA3F5f167E05649970 \
      --collateral 0x039e2fb66102314ce7b64ce5ce3e5183bc94ad38
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC

SEL_SWAP   = "0xc1813380"  # swap(address,address,bool) на V3SwapAdapter — keccak-сверен офлайн (cast sig)
SEL_TOKEN0 = "0x0dfe1681"  # token0() UniV3-пула

# Прим.: билдер НЕ оценивает выход свопа офчейн (спот-цена без слиппеджа врёт, а точный quoter варьируется
# по DEX). Авторитетная проверка прибыльности — ончейн-пол minProfit контракта + fork-replay симуляция
# (SiloLiquidatorFork.t.sol дал 10.155 USDC этой же route-A калдатой). Билдер отвечает за КАЛДАТУ, не за квоту.


def _pad(x: str) -> str:
    return x[2:].lower().rjust(64, "0")


def encode_swap_calldata(pool: str, token_in: str, zero_for_one: bool) -> str:
    """Чистая функция (без RPC): точная калдата для swapTarget=адаптер. Юнит-тестируемо офлайн."""
    return SEL_SWAP + _pad(pool) + _pad(token_in) + ("1".rjust(64, "0") if zero_for_one else "0" * 64)


def resolve_direction(rpc: RPC, pool: str, token_in: str) -> bool:
    """zeroForOne = tokenIn это token0 пула. Одно чтение, кэшируется на пару навсегда (пул неизменен)."""
    token0 = "0x" + rpc.eth_call(pool, SEL_TOKEN0)[-40:]
    return token0.lower() == token_in.lower()


def build_route_a(rpc: RPC, adapter: str, pool: str, collateral_token: str,
                  zero_for_one: bool | None = None) -> dict:
    """Собирает вход swapTarget/swapCallData. zero_for_one можно передать (из кэша) — тогда 0 RPC."""
    z = zero_for_one if zero_for_one is not None else resolve_direction(rpc, pool, collateral_token)
    return {
        "swapTarget": adapter,
        "swapCallData": encode_swap_calldata(pool, collateral_token, z),
        "pool": pool,
        "zeroForOne": z,
    }


def main():
    ap = argparse.ArgumentParser(description="Оффчейн-билдер route-A swap-калдаты для executeLiquidation")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--adapter", required=True, help="адрес V3SwapAdapter (swapTarget)")
    ap.add_argument("--pool", required=True, help="UniV3-style пул залог/долг")
    ap.add_argument("--collateral", required=True, help="токен залога (tokenIn свопа)")
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    q = build_route_a(rpc, a.adapter.lower(), a.pool.lower(), a.collateral.lower())
    print("route-A квота собрана:")
    print(f"  swapTarget (адаптер): {q['swapTarget']}")
    print(f"  pool               : {q['pool']}")
    print(f"  zeroForOne         : {q['zeroForOne']}  (tokenIn == token0)")
    print(f"  swapCallData       : {q['swapCallData']}")
    print(f"  длина калдаты      : {(len(q['swapCallData'])-2)//2} байт (selector+3 слова)")
    print("\n→ передать в executeLiquidation как (swapTarget, swapCallData); adapter возьмёт amountIn из allowance.")


if __name__ == "__main__":
    main()
