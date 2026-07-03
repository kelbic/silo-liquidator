#!/usr/bin/env bash
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: Morpho-бот"; exit 1; }
[ -e "$DIR/chain/morpho.py" ] && { echo "СТОП: Morpho-файлы"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py"; exit 1; }
cat > "$DIR/analysis/debt_shares.py" << 'FILE_EOF'
#!/usr/bin/env python3
"""debt_shares.py — СЫРОЙ баланс debt-долей (не сконвертированный в активы). (read-only)

Зачем: maxRepay() = balanceOf(debtShareToken) -> convertToAssets(...) — конвертация долей в активы
МОЖЕТ округлить крошечную ненулевую долю до 0 активов (нет ни одной целой минимальной единицы токена).
Но getDebtSilo() (внутри getConfigsForSolvency, что реально решает LTV-путь) смотрит СЫРОЙ balanceOf,
не сконвертированный. Если maxRepay=0, а LTV≠0 (наш случай на ОБЕИХ сторонах пары) — это ровно тот
зазор: пыльная ненулевая доля, видимая одним путём и невидимая (округлённая) другим.

Вызов ПРЯМО на силосе (Silo.maxRepayShares(address), НЕ через SiloLens — другая конвенция: один
аргумент, не (silo, borrower)).

Запуск:
  python3 -m analysis.debt_shares --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 --borrower 0x...
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC

SEL_MAX_REPAY_SHARES = "0x29d6509a"  # maxRepayShares(address) — keccak-сверено


def get_raw_shares(rpc: RPC, silo: str, borrower: str) -> int:
    data = SEL_MAX_REPAY_SHARES + borrower[2:].lower().rjust(64, "0")
    ret = rpc.eth_call(silo, data)
    return int(ret, 16)


def main():
    ap = argparse.ArgumentParser(description="Сырой баланс debt-долей (не активов) на конкретном силосе")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", required=True)
    ap.add_argument("--borrower", required=True)
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    shares = get_raw_shares(rpc, a.silo.lower(), a.borrower.lower())
    print(f"силос {a.silo}, заёмщик {a.borrower}:")
    print(f"  сырые debt-доли (maxRepayShares): {shares}")
    if shares == 0:
        print(f"  → ноль И в долях — на ЭТОМ силосе долга действительно нет никакого, не только пыль")
    else:
        print(f"  → ЕСТЬ ненулевые доли, но maxRepay (активы) показал 0 — это и есть round-to-zero пыль")


if __name__ == "__main__":
    main()
FILE_EOF
cd "$DIR"
python3 -m py_compile analysis/debt_shares.py && echo "[OK] py_compile"
python3 -c "import analysis.debt_shares" && echo "[OK] реальный импорт"
python3 - << 'PY_TEST'
import analysis.debt_shares as ds
assert ds.SEL_MAX_REPAY_SHARES == "0x29d6509a"
class F:
    def eth_call(s,to,data):
        assert data.startswith(ds.SEL_MAX_REPAY_SHARES)
        return "0x"+hex(7)[2:].rjust(64,"0")
assert ds.get_raw_shares(F(),"0xsilo","0xb")==7
class F0:
    def eth_call(s,to,data): return "0x"+"0"*64
assert ds.get_raw_shares(F0(),"0xsilo","0xb")==0
print("[OK] get_raw_shares: ненулевой и нулевой случаи — прошли")
PY_TEST
echo ">> debt_shares.py готов. Запуск:"
echo "   python3 -m analysis.debt_shares --rpc https://rpc.soniclabs.com --silo <силос> --borrower <адрес>"
