#!/usr/bin/env python3
"""borrower_health.py — HF/LTV одного заёмщика через КАНОНИЧЕСКИЙ SiloLens. Первый кирпич paper-детектора.

Зачем: детектору нужна платёжеспособность заёмщика — но переизобретать формулу Silo (нормализация цены,
доли токенов, конвертация) рискованно (мы уже 20+ раз ловили себя на "не тот объект"/"не та формула").
SiloLens — публичный, развёрнутый Silo'ом контракт с готовыми getter'ами (isSolvent/getUserLTV/getUserLT),
делающими ТУ ЖЕ математику, что и сам протокол при принятии решения о ликвидируемости. Используем его
напрямую вместо своей реализации — это canonical source, не наша интерпретация.

Адрес SiloLens на Sonic — из деплой-записи самого Silo (silo-core/deployments/sonic/SiloLens.sol.json
в их репо), не угадан и не найден на стороннем сайте.

v3 (ревью): футер раньше печатал margin+'(здоров)' БЕЗУСЛОВНО, даже после того как выше уже было сказано
'ПОЗИЦИЯ ЗАКРЫТА, не здоров с запасом' — прямое внутреннее противоречие вывода. Теперь margin/статус
печатается ТОЛЬКО когда данные согласованы (debt>0 И LTV/LT ненулевые); иначе — 'НЕТ ПОЗИЦИИ' или
'ДАННЫЕ НЕДОСТОВЕРНЫ', без ложного '(здоров)'.

Запуск (пример: борровер из реальной ликвидации на 0x322e1d53):
  python3 -m analysis.borrower_health --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 --borrower 0x...
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC

SILO_LENS_SONIC = "0xeA5359C6AbA077Bcd19AB5F7CcB8245AAc45687B"  # из деплой-записи Silo, не угадан

SEL_IS_SOLVENT = "0x590630f0"   # isSolvent(address,address)   — keccak-сверено
SEL_USER_LTV   = "0x43afdad2"   # getUserLTV(address,address)  — keccak-сверено
SEL_USER_LT    = "0x1fe02269"   # getUserLT(address,address)   — keccak-сверено
SEL_DEBT_BAL   = "0xd9bd0ea2"   # debtBalanceOfUnderlying(address,address) — keccak-сверено, НЕЗАВИСИМЫЙ
                                 # путь (_silo.maxRepay(_borrower) внутри) — не тот же код, что LTV/LT.
                                 # Нужен, чтобы отличить 'LTV=0/LT=0 потому что позиция закрыта' от
                                 # 'что-то не так с LTV/LT-путём конкретно'.


def _addr_pad(addr: str) -> str:
    return addr[2:].lower().rjust(64, "0")


def get_borrower_health(rpc: RPC, silo: str, borrower: str) -> dict:
    """Три канонических значения одним набором вызовов: solvent (bool), LTV (1e18=100%), LT (1e18=100%).
    Плюс debt_raw (независимый путь) — отличает 'позиция закрыта' от 'проблема в LTV/LT-пути'."""
    args = _addr_pad(silo) + _addr_pad(borrower)
    solvent_ret = rpc.eth_call(SILO_LENS_SONIC, SEL_IS_SOLVENT + args)
    ltv_ret = rpc.eth_call(SILO_LENS_SONIC, SEL_USER_LTV + args)
    lt_ret = rpc.eth_call(SILO_LENS_SONIC, SEL_USER_LT + args)
    debt_ret = rpc.eth_call(SILO_LENS_SONIC, SEL_DEBT_BAL + args)
    solvent = int(solvent_ret, 16) == 1
    ltv = int(ltv_ret, 16)
    lt = int(lt_ret, 16)
    debt_raw = int(debt_ret, 16)
    return {"solvent": solvent, "ltv": ltv, "lt": lt, "debt_raw": debt_raw,
            "ltv_pct": ltv / 1e18 * 100, "lt_pct": lt / 1e18 * 100}


def main():
    ap = argparse.ArgumentParser(description="HF/LTV заёмщика через канонический SiloLens")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", required=True)
    ap.add_argument("--borrower", required=True)
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    h = get_borrower_health(rpc, a.silo.lower(), a.borrower.lower())
    print(f"SiloLens ({SILO_LENS_SONIC}) на силосе {a.silo}, заёмщик {a.borrower}:")
    print(f"  isSolvent: {h['solvent']}")
    print(f"  LTV:       {h['ltv_pct']:.2f}%  (raw {h['ltv']})")
    print(f"  LT:        {h['lt_pct']:.2f}%  (raw {h['lt']}, порог ликвидируемости этого заёмщика)")
    print(f"  долг (независимый путь, maxRepay): {h['debt_raw']} raw")
    if h["debt_raw"] == 0:
        if h["ltv"] == 0 and h["lt"] == 0:
            print(f"  ⚠ долг=0, LTV=LT=0 — согласованно. ПОЗИЦИЯ ЗАКРЫТА, не 'здоров с запасом'.")
            print(f"  Для детектора: такого заёмщика нечего мониторить — он не может быть ликвидирован,")
            print(f"  пока не откроет новую позицию.")
        else:
            print(f"  ⚠ долг=0, но LTV={h['ltv_pct']:.2f}%/LT={h['lt_pct']:.2f}% ненулевые — НЕСОГЛАСОВАННО,")
            print(f"  разбирать отдельно, не доверять этим числам.")
        print(f"  статус: НЕТ ПОЗИЦИИ")
        return
    if h["ltv"] == 0 and h["lt"] == 0:
        print(f"  ⚠ долг={h['debt_raw']}>0, но LTV=LT=0 — НЕСОГЛАСОВАННО. Проблема в LTV/LT-пути")
        print(f"  конкретно (не в том, что позиции нет) — разбирать отдельно, не доверять этому числу.")
        print(f"  статус: ДАННЫЕ НЕДОСТОВЕРНЫ")
        return
    margin = h["lt_pct"] - h["ltv_pct"]
    print(f"  запас до LT: {margin:+.2f} п.п.  {'(здоров)' if h['solvent'] else '(ЛИКВИДИРУЕМ — LTV >= LT)'}")


if __name__ == "__main__":
    main()
