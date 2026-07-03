#!/usr/bin/env python3
"""live_detector.py — «мозг» конкурентного пайплайна: непрерывная детекция ликвидируемых позиций. (read-only)

Собирает вместе три уже проверенных кирпича (borrower_health / open_positions / debt_shares) в
живой цикл: держит watchlist текущих заёмщиков с долгом на НАШЕМ силосе, дешёво обновляет их здоровье
на каждом новом блоке, а при пересечении LTV≥LT — считает ЧЕСТНУЮ оценку нетто-прибыли через канонический
`maxLiquidation` хука и печатает КАНДИДАТА ровно в той форме, что принимает `SiloLiquidator.executeLiquidation`
(flashLoanFrom/hook/collateralAsset/debtAsset/user/maxDebtToCover/minProfit). Это замыкает детекцию→исполнение,
не совершая ни одной транзакции — детектор только читает.

Охват заёмщиков — event-driven (осознанное решение, §7 STATE.md): watchlist засевается разовым скан-окном
(`--seed-days`), дальше пополняется ЖИВЫМИ событиями Borrow на силосе. Позиция, уходящая под воду, к этому
моменту УЖЕ эмитила Borrow (иначе долга бы не было) — живой охват ловит новый риск без скана 69.5M блоков
истории Transfer. LiquidationCall в том же окне — сигнал, что кто-то (вероятно инкумбент) забрал позицию
РАНЬШЕ нас: логируем как проигранную гонку и обновляем долг заёмщика.

Готча двусторонних пар (§2 STATE.md) обработана: watchlist гейтится `debtBalanceOfUnderlying(НАШ_силос,·)>порог`
(get_debt_only), поэтому у всех отслеживаемых долг именно USDC на нашей стороне — направление, которое умеет
исполнять наш USDC-flashloan-ликвидатор. `maxLiquidation` резолвит стороны сам (getConfigsForSolvency внутри).

Всё read-only, только stdlib, ключей/капитала нет. Модель прибыли — в чистых функциях (юнит-тестируемо офлайн).

Запуск:
  # один проход (для теста/крона): засеять из окна, проверить всех, напечатать кандидатов, выйти
  python3 -m analysis.live_detector --rpc https://rpc.soniclabs.com \
      --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 --seed-days 14 --once

  # демон: тот же засев, дальше опрос новых блоков каждые --poll секунд
  python3 -m analysis.live_detector --rpc https://rpc.soniclabs.com \
      --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 --seed-days 14 --poll 10 --min-profit-usd 20
"""
from __future__ import annotations
import argparse
import json
import sys
import time

from analysis.contestation import (
    RPC, RpcError, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts, silo_token_meta,
)
from analysis.borrower_health import SEL_DEBT_BAL, SILO_LENS_SONIC, _addr_pad, get_borrower_health
from analysis.open_positions import TOPIC0_BORROW, decode_borrow_owner, get_debt_only
from analysis.read_fee import SEL_CONFIG, SEL_GETSILOS, SEL_GETCONFIG, parse_config, _word

SEL_MAXLIQ = "0xbd02d848"  # maxLiquidation(address) -> (collateralToLiquidate, debtToRepay, sTokenRequired)
                           # keccak-сверен офлайн; эмпирически подтверждён на живом хуке (healthy → все нули)

# Порог «есть ли реальная позиция» в raw-единицах ДОЛГОВОГО токена. Дефолт под USDC (decimals=6): $1.
# Тот же принцип, что в open_positions.py — порог, не ==0 (округление конверсии долей нестабильно у границы).
DUST_THRESHOLD_RAW_DEFAULT = 1_000_000


# ---------------------------------------------------------------------------
# Модель прибыли — чистые функции, без RPC (юнит-тестируемо офлайн)
# ---------------------------------------------------------------------------

def gross_premium_debt(debt_to_repay_raw: int, liq_fee_wei: int) -> int:
    """Валовая премия ликвидатора в единицах ДОЛГОВОГО токена.

    Протокол отдаёт залог стоимостью debtToRepay*(1+liqFee) за погашение debtToRepay долга. Премия (то,
    что сверх возврата) = debtToRepay * liqFee. liq_fee_wei — доля в 1e18 (6.5% == 0.065e18)."""
    return debt_to_repay_raw * liq_fee_wei // 10 ** 18


def net_profit_debt(debt_to_repay_raw: int, liq_fee_wei: int, slippage_bps: int, gas_cost_debt_raw: int) -> int:
    """ЧЕСТНАЯ оценка нетто в единицах долгового токена (та же философия, что minProfit-пол контракта).

      seized_value ≈ debtToRepay*(1+liqFee)         (стоимость изъятого залога в долговых единицах)
      после свопа обратно в долг теряем slippage:    seized_value*(1 - slippage)
      нетто = seized_value*(1-slippage) - debtToRepay - gas
    flashloanFee = 0% (замерено), поэтому в costs не входит. slippage_bps — б.п. (100 = 1%)."""
    seized_value = debt_to_repay_raw * (10 ** 18 + liq_fee_wei) // 10 ** 18
    after_swap = seized_value * (10_000 - slippage_bps) // 10_000
    return after_swap - debt_to_repay_raw - gas_cost_debt_raw


def build_candidate(borrower: str, silo: str, hook: str, collateral_asset: str, debt_asset: str,
                    coll_to_liq_raw: int, debt_to_repay_raw: int, s_token_required: bool,
                    net_raw: int, gross_raw: int, debt_decimals: int) -> dict:
    """Кандидат в ТОЧНОЙ форме входа SiloLiquidator.executeLiquidation (плюс поля оценки прибыли).

    flashLoanFrom = наш долговой силос: у него asset()==debtAsset (SiloStdLib.flashFee требует _token==asset),
    то есть флеш-кредит USDC берём из USDC-силоса — ровно то, что ассёртит fork-replay контракта."""
    return {
        # --- ровно параметры executeLiquidation ---
        "flashLoanFrom": silo,          # asset()==debtAsset (инвариант контракта)
        "hook": hook,
        "collateralAsset": collateral_asset,
        "debtAsset": debt_asset,
        "user": borrower,
        "maxDebtToCover": debt_to_repay_raw,   # из maxLiquidation, не гадаем
        "minProfit_suggested": max(0, net_raw),  # честный пол; исполнитель выставит свой
        # --- контекст оценки (не идёт в контракт, для лога/решения) ---
        "collateralToLiquidate": coll_to_liq_raw,
        "sTokenRequired": s_token_required,
        "gross_premium_raw": gross_raw,
        "net_estimate_raw": net_raw,
        "net_estimate_debt": net_raw / 10 ** debt_decimals,
    }


# ---------------------------------------------------------------------------
# Резолв рынка (один раз на старте) — пересчитывается заново => детект дрейфа
# ---------------------------------------------------------------------------

def resolve_market(rpc: RPC, debt_silo: str) -> dict:
    """config → getSilos → сосед; hook и liquidationFee ЗАЛОГОВОЙ стороны (она ценит премию, §read_fee).
    Плюс токены/decimals обеих сторон. Всё живым чтением, не хардкод — если протокол сменит параметр, увидим."""
    config = "0x" + _word(rpc.eth_call(debt_silo, SEL_CONFIG), 0)[24:]
    silos_ret = rpc.eth_call(config, SEL_GETSILOS)
    s0, s1 = "0x" + _word(silos_ret, 0)[24:], "0x" + _word(silos_ret, 1)[24:]
    if debt_silo not in (s0, s1):
        raise RuntimeError(f"getSilos() вернул {s0},{s1} — запрошенный {debt_silo} не в паре. Стоп.")
    collateral_silo = s1 if s0 == debt_silo else s0
    cfg_debt = parse_config(rpc.eth_call(config, SEL_GETCONFIG + debt_silo[2:].rjust(64, "0")))
    cfg_coll = parse_config(rpc.eth_call(config, SEL_GETCONFIG + collateral_silo[2:].rjust(64, "0")))
    if cfg_debt["silo"].lower() != debt_silo:  # эхо-проверка рамки ABI (как frame_check в read_fee)
        raise RuntimeError(f"эхо silo не совпало ({cfg_debt['silo']} != {debt_silo}) — ABI-рамка сдвинута. Стоп.")

    meta: dict = {}
    m_debt = silo_token_meta(rpc, debt_silo, meta)
    m_coll = silo_token_meta(rpc, collateral_silo, meta)
    return {
        "config": config,
        "debt_silo": debt_silo,
        "collateral_silo": collateral_silo,
        "hook": cfg_debt["hookReceiver"],
        "liq_fee_wei": cfg_coll["liquidationFee"],  # залоговая сторона ценит нашу премию
        "lt_wei": cfg_debt["lt"],
        "debt_token": m_debt["token"], "debt_symbol": m_debt["symbol"], "debt_decimals": m_debt["decimals"],
        "coll_token": m_coll["token"], "coll_symbol": m_coll["symbol"], "coll_decimals": m_coll["decimals"],
    }


def get_max_liquidation(rpc: RPC, hook: str, borrower: str) -> tuple[int, int, bool]:
    """maxLiquidation(borrower) на хуке: (collateralToLiquidate, debtToRepay, sTokenRequired).
    Хук сам резолвит стороны через getConfigsForSolvency — возвращает нули, если заёмщик не ликвидируем."""
    ret = rpc.eth_call(hook, SEL_MAXLIQ + _addr_pad(borrower))
    h = ret[2:] if ret.startswith("0x") else ret
    if len(h) < 192:
        return 0, 0, False
    return int(h[0:64], 16), int(h[64:128], 16), int(h[128:192], 16) != 0


# ---------------------------------------------------------------------------
# Засев watchlist и живой опрос
# ---------------------------------------------------------------------------

def seed_watchlist(rpc: RPC, silo: str, from_block: int, to_block: int, dust_raw: int) -> set:
    """Разовый засев: кандидаты = Borrow.owner ∪ LiquidationCall.borrower в окне, отфильтровано по
    debtBalanceOfUnderlying(НАШ силос,·) > dust_raw. Точь-в-точь логика open_positions, переиспользуем."""
    borrow_logs = fetch_liquidation_logs(rpc, from_block, to_block, chunk=10_000, topic0=TOPIC0_BORROW)
    owners = {decode_borrow_owner(l) for l in borrow_logs
              if (l.get("address") or "").lower() == silo and decode_borrow_owner(l)}
    liq_logs = fetch_liquidation_logs(rpc, from_block, to_block, chunk=10_000)
    liq_borrowers = {e["borrower"].lower() for e in (decode_liquidation_log(l) for l in liq_logs)
                     if e and e["silo"].lower() == silo}
    candidates = owners | liq_borrowers
    watch = set()
    for addr in candidates:
        if get_debt_only(rpc, silo, addr) > dust_raw:
            watch.add(addr)
    sys.stderr.write(f"\nзасев: {len(candidates)} кандидатов из событий → {len(watch)} с открытым долгом (>{dust_raw} raw)\n")
    return watch


def ingest_events(rpc: RPC, silo: str, from_block: int, to_block: int, watch: set, dust_raw: int) -> list:
    """Инкремент по новым блокам: добавить новых Borrow.owner в watchlist; вернуть LiquidationCall на нашем
    силосе (проигранные/чужие гонки) для лога. Возвращает список decoded LiquidationCall-событий на силосе."""
    borrow_logs = fetch_liquidation_logs(rpc, from_block, to_block, chunk=10_000, topic0=TOPIC0_BORROW)
    for l in borrow_logs:
        if (l.get("address") or "").lower() == silo:
            o = decode_borrow_owner(l)
            if o and o not in watch and get_debt_only(rpc, silo, o) > dust_raw:
                watch.add(o)
    liq_logs = fetch_liquidation_logs(rpc, from_block, to_block, chunk=10_000)
    return [e for e in (decode_liquidation_log(l) for l in liq_logs) if e and e["silo"].lower() == silo]


def scan_once(rpc: RPC, mkt: dict, watch: set, dust_raw: int, slippage_bps: int,
              gas_cost_debt_raw: int, min_profit_raw: int, emit_json: bool) -> list:
    """Один проход по watchlist: обновить здоровье, для ликвидируемых посчитать нетто и напечатать кандидата.
    Возвращает список кандидатов (dict), прошедших порог min_profit_raw. Заодно чистит закрытые позиции."""
    silo = mkt["debt_silo"]
    dec = mkt["debt_decimals"]
    candidates = []
    closed = set()
    ranked = []
    for addr in list(watch):
        h = get_borrower_health(rpc, silo, addr)
        if h["debt_raw"] < dust_raw:
            closed.add(addr)  # позиция закрыта/ушла в пыль — снять с наблюдения
            continue
        ranked.append((addr, h))
    ranked.sort(key=lambda r: r[1]["lt_pct"] - r[1]["ltv_pct"])  # ближе к LT — первым

    for addr, h in ranked:
        margin = h["lt_pct"] - h["ltv_pct"]
        debt_amt = h["debt_raw"] / 10 ** dec
        if h["solvent"]:
            sys.stderr.write(f"  {addr}  LTV {h['ltv_pct']:6.2f}% / LT {h['lt_pct']:.2f}%  "
                             f"запас {margin:+6.2f}пп  долг {debt_amt:,.2f} {mkt['debt_symbol']}  ЗДОРОВ\n")
            continue
        # ликвидируем — канонический maxLiquidation на хуке
        coll_raw, debt_to_repay, s_req = get_max_liquidation(rpc, mkt["hook"], addr)
        if debt_to_repay == 0:
            sys.stderr.write(f"  {addr}  !solvent, но maxLiquidation=0 (долг на другой стороне пары?) — пропуск\n")
            continue
        gross = gross_premium_debt(debt_to_repay, mkt["liq_fee_wei"])
        net = net_profit_debt(debt_to_repay, mkt["liq_fee_wei"], slippage_bps, gas_cost_debt_raw)
        cand = build_candidate(addr, silo, mkt["hook"], mkt["coll_token"], mkt["debt_token"],
                               coll_raw, debt_to_repay, s_req, net, gross, dec)
        mark = "✔ КАНДИДАТ" if net >= min_profit_raw else "✗ ниже порога"
        sys.stderr.write(f"  {addr}  ЛИКВИДИРУЕМ  LTV {h['ltv_pct']:.2f}% ≥ LT {h['lt_pct']:.2f}%  "
                         f"repay {debt_to_repay/10**dec:,.2f}  нетто≈{net/10**dec:,.2f} {mkt['debt_symbol']}  {mark}\n")
        if net >= min_profit_raw:
            candidates.append(cand)
            if emit_json:
                print(json.dumps(cand))
    watch -= closed
    if closed:
        sys.stderr.write(f"  снято с наблюдения (закрыто/пыль): {len(closed)}\n")
    return candidates


def main():
    ap = argparse.ArgumentParser(description="Живой детектор ликвидируемых позиций (read-only, мозг пайплайна)")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", required=True, help="ДОЛГОВОЙ силос (asset==debt), напр. USDC-силос")
    ap.add_argument("--seed-days", type=float, default=14.0, help="окно засева watchlist из событий")
    ap.add_argument("--poll", type=float, default=0.0, help="секунд между опросами; 0 = разовый проход")
    ap.add_argument("--once", action="store_true", help="один проход и выход (эквивалент --poll 0)")
    ap.add_argument("--min-profit-usd", type=float, default=10.0, help="порог нетто для печати кандидата")
    ap.add_argument("--slippage-bps", type=int, default=50, help="ожидаемый слиппедж свопа залог→долг, б.п.")
    ap.add_argument("--gas-cost-usd", type=float, default=1.0, help="оценка газа за ликвидацию, USD")
    ap.add_argument("--dust-usd", type=float, default=1.0, help="порог 'есть позиция' по долгу, USD")
    ap.add_argument("--json", action="store_true", help="печатать кандидатов как JSON-строки в stdout")
    a = ap.parse_args()

    rpc = RPC(a.rpc)
    silo = a.silo.lower()
    mkt = resolve_market(rpc, silo)
    dec = mkt["debt_decimals"]
    # пороги в USD → raw. Для стейбл-долга (USDC) 1 unit ≈ $1; для не-стейбла это приближение.
    scale = 10 ** dec
    dust_raw = int(a.dust_usd * scale)
    min_profit_raw = int(a.min_profit_usd * scale)
    gas_cost_debt_raw = int(a.gas_cost_usd * scale)

    sys.stderr.write(
        f"РЫНОК:\n  config {mkt['config']}\n  долговой силос {silo}  ({mkt['debt_symbol']}, dec={dec})\n"
        f"  залоговый силос {mkt['collateral_silo']}  ({mkt['coll_symbol']})\n  hook {mkt['hook']}\n"
        f"  liquidationFee (залог. сторона) {mkt['liq_fee_wei']/1e18*100:.2f}%   LT {mkt['lt_wei']/1e18*100:.2f}%\n"
        f"  slippage {a.slippage_bps}бп, gas ${a.gas_cost_usd}, порог нетто ${a.min_profit_usd}, dust ${a.dust_usd}\n\n"
    )

    tip = rpc.block_number()
    seed_from = find_block_at_ts(rpc, rpc.block_ts(tip) - int(a.seed_days * 86400), tip)
    watch = seed_watchlist(rpc, silo, seed_from, tip, dust_raw)
    last_block = tip

    def run_scan():
        sys.stderr.write(f"\n[скан @ блок {last_block}] watchlist={len(watch)}\n")
        cands = scan_once(rpc, mkt, watch, dust_raw, a.slippage_bps, gas_cost_debt_raw, min_profit_raw, a.json)
        if cands:
            sys.stderr.write(f"  ⇒ {len(cands)} кандидат(ов) выше порога, готовых к исполнению\n")
        else:
            sys.stderr.write("  ⇒ живых captureable-возможностей нет в этом проходе\n")
        return cands

    run_scan()
    if a.once or a.poll <= 0:
        return

    while True:
        try:
            time.sleep(a.poll)
            tip = rpc.block_number()
            if tip <= last_block:
                continue
            liqs = ingest_events(rpc, silo, last_block + 1, tip, watch, dust_raw)
            for e in liqs:  # чужая ликвидация на нашем силосе — проигранная гонка, логируем
                if e["liquidator"].lower() not in ("",):
                    sys.stderr.write(
                        f"  ⚠ ЧУЖАЯ ЛИКВИДАЦИЯ блок {e['block']}: liquidator {e['liquidator']} забрал "
                        f"{e['borrower']} (repay {e['repay_raw']/10**dec:,.2f} {mkt['debt_symbol']}) — гонка проиграна\n")
            last_block = tip
            run_scan()
        except (RpcError, RuntimeError) as ex:
            sys.stderr.write(f"  RPC-сбой в цикле, продолжаю: {ex}\n")
            continue
        except KeyboardInterrupt:
            sys.stderr.write("\nостановлено пользователем.\n")
            return


if __name__ == "__main__":
    main()
