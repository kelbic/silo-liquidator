#!/usr/bin/env python3
"""open_positions.py — enumerate ТЕКУЩИХ заёмщиков с открытым долгом на силосе. (read-only)

Зачем: recent_borrowers.py находит только тех, кого УЖЕ ликвидировали — детектору нужны ВСЕ текущие
открытые позиции, включая никогда не ликвидировавшихся (здоровых или ещё не пойманных). Событие
LiquidationCall этого не покажет: нужно событие открытия/увеличения долга (Borrow).

Кандидаты — объединение Borrow.owner (topics[3]) И LiquidationCall.borrower (кто-то мог занять давно,
быть частично ликвидирован, и остаться с непогашенным остатком). Дешёвый первый проход — ТОЛЬКО
debtBalanceOfUnderlying (1 RPC-вызов на кандидата) отсеивает закрытые позиции ДО дорогого полного
health-чека (4 вызова) — экономит RPC на кандидатах, которых нет смысла проверять дальше.

Переиспользует get_borrower_health/SEL_DEBT_BAL/_addr_pad из borrower_health.py — не дублирует логику.

Запуск:
  python3 -m analysis.open_positions --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 --days 30
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts, silo_token_meta
from analysis.borrower_health import SILO_LENS_SONIC, SEL_DEBT_BAL, _addr_pad, get_borrower_health
from analysis.debt_shares import get_raw_shares

TOPIC0_BORROW = "0x96558a334f4759f0e7c423d68c84721860bd8fbf94ddc4e55158ecb125ad04b5"  # keccak-сверен


def decode_borrow_owner(log: dict) -> str | None:
    """Borrow(address indexed sender, address indexed receiver, address indexed owner, uint256, uint256)
    — owner это topics[3] (0-based: topics[0]=event sig, [1]=sender, [2]=receiver, [3]=owner)."""
    topics = log.get("topics") or []
    if len(topics) < 4:
        return None
    return "0x" + topics[3][-40:]


def get_debt_only(rpc: RPC, silo: str, borrower: str) -> int:
    """Только debtBalanceOfUnderlying (1 вызов) — дешёвый фильтр перед полным health-чеком."""
    ret = rpc.eth_call(SILO_LENS_SONIC, SEL_DEBT_BAL + _addr_pad(silo) + _addr_pad(borrower))
    return int(ret, 16)


def main():
    ap = argparse.ArgumentParser(description="Все текущие заёмщики с открытым долгом на силосе")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", required=True)
    ap.add_argument("--days", type=float, default=30.0)
    a = ap.parse_args()
    silo = a.silo.lower()
    rpc = RPC(a.rpc)

    tip = rpc.block_number()
    frm = find_block_at_ts(rpc, rpc.block_ts(tip) - int(a.days * 86400), tip)

    print(f"Borrow-события на {silo}:", end=" ")
    borrow_logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000, topic0=TOPIC0_BORROW)
    borrow_owners = {decode_borrow_owner(l) for l in borrow_logs
                     if (l.get("address") or "").lower() == silo and decode_borrow_owner(l)}

    print(f"LiquidationCall-события на {silo}:", end=" ")
    liq_logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000)
    liq_events = [e for e in (decode_liquidation_log(l) for l in liq_logs) if e]
    liq_borrowers = {e["borrower"].lower() for e in liq_events if e["silo"].lower() == silo}

    candidates = borrow_owners | liq_borrowers
    print(f"\nкандидатов всего: {len(candidates)} (Borrow: {len(borrow_owners)}, "
          f"из ликвидаций: {len(liq_borrowers)}, пересечение: {len(borrow_owners & liq_borrowers)})")

    open_positions = []
    for addr in candidates:
        debt = get_debt_only(rpc, silo, addr)
        if debt > 0:
            open_positions.append(addr)
    print(f"с ОТКРЫТЫМ долгом (debt>0) прямо сейчас: {len(open_positions)}/{len(candidates)}\n")

    if not open_positions:
        return print("Открытых позиций не найдено — либо все закрыты, либо окно --days мало.")

    meta = {}
    m_debt = silo_token_meta(rpc, silo, meta)
    rows = [(addr, get_borrower_health(rpc, silo, addr)) for addr in open_positions]
    rows.sort(key=lambda r: r[1]["lt_pct"] - r[1]["ltv_pct"])  # ближе к LT — первым

    print(f"{'заёмщик':44s} {'LTV':>8s} {'LT':>8s} {'запас п.п.':>11s}  {'долг '+m_debt['symbol']:>14s}  статус")
    DUST_THRESHOLD_RAW = 1_000_000  # $1 в raw-единицах USDC (decimals=6) — консервативный порог:
    # на несколько порядков выше шума округления conversion (доли цента), с запасом ниже газовых
    # издержек Sonic. Порог, не точный ==0: debt_raw на границе 1 wei долей МОЖЕТ давать 0 или 1-2
    # raw-единицы от блока к блоку (сдвиг totalSiloAssets/totalShares из-за начисления процентов) —
    # точное сравнение с нулём НЕНАДЁЖНО на этой границе, порог — устойчив.
    for addr, h in rows:
        margin = h["lt_pct"] - h["ltv_pct"]
        debt_amt = h["debt_raw"] / (10 ** m_debt["decimals"])
        if h["debt_raw"] < DUST_THRESHOLD_RAW:
            raw_shares = get_raw_shares(rpc, silo, addr)
            if raw_shares > 0:
                status = f"ПЫЛЬ (${debt_amt:.6f}, {raw_shares} сырых долей — не captureable)"
            else:
                status = "НЕТ ПОЗИЦИИ НА ЭТОМ СИЛОСЕ (0 и в долях, и в активах)"
        elif h["ltv"] == 0 and h["lt"] == 0:
            status = "НЕСОГЛАСОВАНО (debt>0, LTV=LT=0 — не доверять)"
        else:
            status = "ЗДОРОВ" if h["solvent"] else "ЛИКВИДИРУЕМ"
        print(f"{addr:44s} {h['ltv_pct']:>7.2f}% {h['lt_pct']:>7.2f}% {margin:>+10.2f}п  {debt_amt:>14,.4f}  {status}")


if __name__ == "__main__":
    main()
