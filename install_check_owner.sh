#!/usr/bin/env bash
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: Morpho-бот"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py"; exit 1; }
cat > "$DIR/analysis/check_owner.py" << 'FILE_EOF'
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
FILE_EOF
cd "$DIR"
python3 -m py_compile analysis/check_owner.py && echo "[OK] py_compile"
python3 - << 'PY_TEST'
import analysis.check_owner as co
def word(h): return "0x"+"0"*24+h
class F:
    def __init__(s,r): s.r=r
    def eth_call(s,to,data): return s.r.get(to)
rpc=F({"A":word("ab"*20),"B":word("ab"*20)})
oa=co.get_owner(rpc,"A"); ob=co.get_owner(rpc,"B")
assert oa==ob=="0x"+"ab"*20
class R:
    def eth_call(s,to,data):
        from analysis.contestation import RpcError
        raise RpcError("revert")
assert co.get_owner(R(),"X") is None
assert co.SEL_OWNER=="0x8da5cb5b"
print("[OK] get_owner (совпадение/revert) + селектор сверен — прошли")
PY_TEST
echo ">> check_owner.py. Запуск:"
echo "   python3 -m analysis.check_owner --rpc https://rpc.soniclabs.com --a 0xccd487e01e9df6932f656b53668f58005f604417 --b 0xa2712025f69c1f1538f7428269e998f9777d7c96"
