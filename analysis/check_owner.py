#!/usr/bin/env python3
"""check_owner.py — сравнивает owner() двух контрактов. (read-only, один вопрос)

Зачем: 0xa2712025 подтверждён построчно как флеш-кредитор 0xccd487 в ЧЕТЫРЁХ из четырёх проверенных
блоков (decompose). Это экономическая связь — не говорит, общий ли у них КОНТРОЛЬ (один оператор с
двумя путями исполнения) или это два разных участника с коммерческими отношениями. Transfer-логи не
могут различить эти два сценария — след одинаковый в обоих случаях. owner() — прямая проверка контроля,
другая ось данных.

Запуск:
  python3 -m analysis.check_owner --rpc https://rpc.soniclabs.com --a 0xccd487e01e9df6932f656b53668f58005f604417 --b 0xa2712025f69c1f1538f7428269e998f9777d7c96
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC, RpcError

SEL_OWNER = "0x8da5cb5b"  # owner() — сверено с проектным списком селекторов


def get_owner(rpc: RPC, addr: str) -> str | None:
    """eth_call owner() -> адрес или None (нет такой функции / revert)."""
    try:
        ret = rpc.eth_call(addr, SEL_OWNER)
    except RpcError:
        return None
    if not ret or len(ret) < 66:
        return None
    return "0x" + ret[-40:]


def main():
    ap = argparse.ArgumentParser(description="Сравнить owner() двух контрактов")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    owner_a = get_owner(rpc, a.a)
    owner_b = get_owner(rpc, a.b)
    print(f"owner({a.a}) = {owner_a or 'нет owner()/revert'}")
    print(f"owner({a.b}) = {owner_b or 'нет owner()/revert'}")
    if owner_a and owner_b:
        if owner_a.lower() == owner_b.lower():
            print(f"\n>>> ОДИН И ТОТ ЖЕ owner — сильный признак общего контроля (один оператор, два пути).")
        else:
            print(f"\n>>> РАЗНЫЕ owner — гипотезу общего контроля НЕ подтверждает (но и не опровергает —")
            print(f"    могут быть разными EOA одного и того же человека).")
    else:
        print(f"\n>>> Хотя бы один контракт не отвечает на owner() (нет такой функции / другой паттерн) —")
        print(f"    проверка неубедительна ни в ту, ни в другую сторону. Нужен другой признак (deployer и т.п.).")


if __name__ == "__main__":
    main()
