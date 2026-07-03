#!/usr/bin/env bash
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: Morpho-бот"; exit 1; }
[ -f "$DIR/analysis/oracle_check.py" ] || { echo "СТОП: нет oracle_check.py (нужен для resolve_proxy)"; exit 1; }
cat > "$DIR/analysis/check_router.py" << 'FILE_EOF'
#!/usr/bin/env python3
"""check_router.py — резолвит прокси + размер байткода произвольного адреса. (read-only, один вопрос)

Зачем: маршрут A (0x8f10b468) не даёт burn, маршрут B (0x3a5d6a7a) теряет 2.27-4.18% залога (ревью,
проверено построчно на 3 блоках). Вопрос «свой хендкод или публичный агрегатор» решает объём работы
по свопу — но публичный/приватный это вопрос УЗНАВАНИЯ (имя, ABI на Sonicscan), не автоматики. Здесь —
только механическая часть: прокси или нет, размер кода (крошечный proxy vs содержательный контракт).

Переиспользует resolve_proxy() из oracle_check.py (та же EIP-1167-логика, уже проверена) — не дублирует.

Запуск:
  python3 -m analysis.check_router --rpc https://rpc.soniclabs.com --address 0x8f10b468b06c6fd214b65f87778827f7d113f996
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC
from analysis.oracle_check import resolve_proxy, SONICSCAN


def main():
    ap = argparse.ArgumentParser(description="Резолв прокси + размер кода произвольного адреса")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--address", required=True)
    a = ap.parse_args()
    rpc = RPC(a.rpc)
    addr = a.address.lower()

    r = resolve_proxy(rpc, addr)
    print(f"адрес:        {addr}  ({r['code_len']} байт{', EIP-1167 proxy' if r['is_proxy'] else ''})")
    print(f"  исходник:   {SONICSCAN.format(addr)}")
    target = addr
    if r["is_proxy"] and r["impl"]:
        ri = resolve_proxy(rpc, r["impl"])
        print(f"импликация:   {r['impl']}  ({ri['code_len']} байт)  ← ЧИТАТЬ ЭТОТ исходник")
        print(f"  исходник:   {SONICSCAN.format(r['impl'])}")
        target = r["impl"]
    print(f"\nЭто говорит только: прокси или нет, сколько байт. 'Публичный агрегатор или приватный")
    print(f"контракт' — вопрос узнавания (имя/ABI). Открой {SONICSCAN.format(target)} глазами:")
    print(f"  • верифицирован + узнаваемое имя (роутер известного DEX/агрегатора) → публичный,")
    print(f"    интеграция дешёвая.")
    print(f"  • не верифицирован / нестандартный ABI → вероятно приватный контракт оператора,")
    print(f"    маршрут придётся повторять самим (пулы маршрута A уже известны из decompose).")


if __name__ == "__main__":
    main()
FILE_EOF
cd "$DIR"
python3 -m py_compile analysis/check_router.py && echo "[OK] py_compile"
python3 - << 'PY_TEST'
import analysis.check_router as cr
import analysis.oracle_check as oc
assert cr.resolve_proxy is oc.resolve_proxy and cr.SONICSCAN == oc.SONICSCAN
class F:
    def __init__(s,c): s.c=c
    def call(s,m,p): return s.c
r=oc.resolve_proxy(F("0x608060405234801561001057600080fd5b50"),"0xrouter")
assert r["is_proxy"] is False
proxy="0x"+oc.EIP1167_PREFIX+("ab"*20)+oc.EIP1167_SUFFIX
r2=oc.resolve_proxy(F(proxy),"0xrouter")
assert r2["is_proxy"] is True and r2["impl"]=="0x"+"ab"*20
print("[OK] check_router переиспользует resolve_proxy — прошли")
PY_TEST
echo ">> check_router.py. Запуск на роутере маршрута A:"
echo "   python3 -m analysis.check_router --rpc https://rpc.soniclabs.com --address 0x8f10b468b06c6fd214b65f87778827f7d113f996"
