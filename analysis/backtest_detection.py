#!/usr/bin/env python3
"""backtest_detection.py — бэктест критериев live-ready (B: успеваем ли, C: верна ли модель прибыли)
на РЕАЛЬНЫХ исторических ликвидациях. (read-only, исторические eth_call на публичном RPC)

Зачем: критерий live-ready в STATE.md §7 требует ДАННЫХ, не ощущений:
  (B) на ≥5 реальных ликвидируемых событий детектор выдал бы кандидата не позже блока инкумбента ≥3/5;
  (C) предсказание модели прибыли в пределах ±20% от реализованного.
Этот харнесс их измеряет. Заодно он гоняет ветку «ЛИКВИДИРУЕМ» на реальных исторических состояниях
(isSolvent=0 на живом стейте) — то, что smoke-тест на здоровом рынке проверить не мог.

Метод по каждому эпизоду (заёмщик B, первый LiquidationCall блока L):
  1. isSolvent(B) @ L-1: если 1 — инкумбент взял позицию В ТОМ ЖЕ блоке, где она стала ликвидируемой
     (оракул-апдейт и ликвидация в одном блоке). lag=0, block-polling это НЕ бьёт в принципе —
     считаем отдельной категорией SAME-BLOCK, не смешиваем с «опоздали».
  2. Иначе — walk-back: экспоненциальное удвоение назад до первого платёжеспособного блока, затем
     бинпоиск границы. Находим E = первый блок ФИНАЛЬНОГО непрерывного забега неплатёжеспособности
     (цена могла осциллировать раньше — нас интересует именно забег, закончившийся ликвидацией).
     lag = L - E: сколько блоков позиция ПРОСТОЯЛА ликвидируемой до захвата. lag≥1 ⇒ детектор с
     1-block-polling успел бы не позже инкумбента.
  3. Покрытие: был ли B в event-driven watchlist к моменту E (Borrow/LiquidationCall на нашем силосе
     до E в окне покрытия)? Нет — данные в пользу разового бэкафилла (STATE.md §7 п.1).
  4. Модель (C): предсказанная валовая премия = repay_actual × liquidationFee vs реализованная =
     стоимость изъятого залога (withdraw_raw × цена S/USD оракула @ L) − repay_actual. Слиппедж свопа
     сюда не входит (это gross-калибровка; route-A-маржа — отдельный трек getRoundData по инкумбенту).

Чистая логика (walk-back, кластеризация эпизодов, сверка модели) — функции с инъецируемыми
колбэками, юнит-тестируемы офлайн без RPC (test_backtest_detection.py).

Запуск:
  python3 -m analysis.backtest_detection --rpc https://rpc.soniclabs.com \
      --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 --days 45 --max-episodes 10
"""
from __future__ import annotations
import argparse
import sys

from analysis.contestation import RPC, RpcError, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts
from analysis.borrower_health import SILO_LENS_SONIC, SEL_IS_SOLVENT, _addr_pad
from analysis.live_detector import resolve_market, get_max_liquidation, gross_premium_debt
from analysis.open_positions import TOPIC0_BORROW, decode_borrow_owner

SEL_LATEST_ROUND = "0xfeaf968c"  # latestRoundData() — keccak-сверен офлайн
EPISODE_GAP_BLOCKS = 1000  # события одного заёмщика ближе этого = один каскад-эпизод, не два


# ---------------------------------------------------------------------------
# Чистая логика — без RPC (юнит-тестируемо)
# ---------------------------------------------------------------------------

def cluster_episodes(events: list[dict]) -> list[dict]:
    """События LiquidationCall → эпизоды: по заёмщику, разрыв > EPISODE_GAP_BLOCKS = новый эпизод.
    Эпизод несёт ПЕРВОЕ событие (блок входа инкумбента) + суммарные repay/withdraw всех его событий."""
    by_borrower: dict[str, list[dict]] = {}
    for e in sorted(events, key=lambda x: (x["borrower"], x["block"], x["log_index"])):
        by_borrower.setdefault(e["borrower"], []).append(e)
    episodes = []
    for b, evs in by_borrower.items():
        cur = None
        for e in evs:
            if cur is None or e["block"] - cur["last_block"] > EPISODE_GAP_BLOCKS:
                if cur:
                    episodes.append(cur)
                cur = {"borrower": b, "first_block": e["block"], "last_block": e["block"],
                       "first_liquidator": e["liquidator"], "n_events": 1,
                       "repay_raw_total": e["repay_raw"], "withdraw_raw_total": e["withdraw_raw"],
                       "repay_raw_first": e["repay_raw"], "withdraw_raw_first": e["withdraw_raw"],
                       "events": [(e["block"], e["repay_raw"], e["withdraw_raw"])]}
            else:
                cur["last_block"] = e["block"]
                cur["n_events"] += 1
                cur["repay_raw_total"] += e["repay_raw"]
                cur["withdraw_raw_total"] += e["withdraw_raw"]
                cur["events"].append((e["block"], e["repay_raw"], e["withdraw_raw"]))
        if cur:
            episodes.append(cur)
    episodes.sort(key=lambda x: x["first_block"])
    return episodes


def walk_back_insolvency(is_solvent_at, liq_block: int, max_lookback: int = 200_000) -> dict:
    """Первый блок E ФИНАЛЬНОГО непрерывного забега неплатёжеспособности, закончившегося ликвидацией
    в блоке liq_block. is_solvent_at(block)->bool — инъецируемый колбэк (RPC или синтетика в тестах).

    Возврат: {'lag': L-E, 'first_insolvent': E, 'same_block': bool, 'calls': n, 'truncated': bool}.
    same_block=True — заёмщик платёжеспособен на L-1 (оракул+ликвидация в одном блоке), lag=0.
    truncated=True — забег длиннее max_lookback, lag = нижняя оценка (для критерия B этого достаточно:
    'успели бы' уже доказано)."""
    calls = 0

    def solv(b):
        nonlocal calls
        calls += 1
        return is_solvent_at(b)

    if solv(liq_block - 1):
        return {"lag": 0, "first_insolvent": liq_block, "same_block": True, "calls": calls, "truncated": False}
    # экспоненциально назад до первого платёжеспособного
    step = 1
    last_insolvent = liq_block - 1
    while True:
        probe = liq_block - 1 - step
        if step > max_lookback or probe < 1:
            return {"lag": liq_block - last_insolvent, "first_insolvent": last_insolvent,
                    "same_block": False, "calls": calls, "truncated": True}
        if solv(probe):
            lo_solvent, hi_insolvent = probe, last_insolvent
            break
        last_insolvent = probe
        step *= 2
    # бинпоиск границы: lo платёжеспособен, hi неплатёжеспособен, ищем первый неплатёжеспособный
    while hi_insolvent - lo_solvent > 1:
        mid = (lo_solvent + hi_insolvent) // 2
        if solv(mid):
            lo_solvent = mid
        else:
            hi_insolvent = mid
    return {"lag": liq_block - hi_insolvent, "first_insolvent": hi_insolvent,
            "same_block": False, "calls": calls, "truncated": False}


def model_check(repay_raw: int, withdraw_raw: int, price_e8: int, liq_fee_wei: int,
                debt_decimals: int, coll_decimals: int) -> dict:
    """Калибровка (C), валовый уровень: предсказание = repay×fee vs реализация = залог×цена − repay.
    Всё в USD (долг USDC ⇒ 1 raw-unit=1e-6 USD; цена оракула e8). Возврат с ratio реализ/предсказ."""
    repay_usd = repay_raw / 10 ** debt_decimals
    seized_usd = withdraw_raw / 10 ** coll_decimals * price_e8 / 1e8
    realized_gross = seized_usd - repay_usd
    predicted_gross = gross_premium_debt(repay_raw, liq_fee_wei) / 10 ** debt_decimals
    ratio = realized_gross / predicted_gross if predicted_gross > 0 else float("nan")
    return {"repay_usd": repay_usd, "seized_usd": seized_usd,
            "realized_gross_usd": realized_gross, "predicted_gross_usd": predicted_gross, "ratio": ratio}


def model_check_events(events: list[tuple[int, int, int]], price_at, liq_fee_wei: int,
                       debt_decimals: int, coll_decimals: int) -> dict:
    """Калибровка (C) PER-EVENT: каждое событие каскада оценивается ценой оракула СВОЕГО блока
    (v1 ценила весь эпизод ценой первого блока — на падающих каскадах это завышало seized и ratio).
    price_at(block)->int(e8) — инъецируемый колбэк (RPC или синтетика в тестах); кэш по блоку —
    события одного блока не дёргают цену дважды.

    Возврат в форме model_check + n_price_calls (контроль RPC-бюджета)."""
    cache: dict[int, int] = {}

    def price(b: int) -> int:
        if b not in cache:
            cache[b] = price_at(b)
        return cache[b]

    repay_usd = seized_usd = 0.0
    repay_raw_total = 0
    for block, repay_raw, withdraw_raw in events:
        repay_usd += repay_raw / 10 ** debt_decimals
        seized_usd += withdraw_raw / 10 ** coll_decimals * price(block) / 1e8
        repay_raw_total += repay_raw
    realized_gross = seized_usd - repay_usd
    predicted_gross = gross_premium_debt(repay_raw_total, liq_fee_wei) / 10 ** debt_decimals
    ratio = realized_gross / predicted_gross if predicted_gross > 0 else float("nan")
    return {"repay_usd": repay_usd, "seized_usd": seized_usd,
            "realized_gross_usd": realized_gross, "predicted_gross_usd": predicted_gross,
            "ratio": ratio, "n_price_calls": len(cache)}


def summarize(rows: list[dict]) -> dict:
    """Агрегат по критериям B/C. B: доля эпизодов с lag≥1 (успели бы с block-polling) среди не-same-block
    + same-block отдельной строкой. C: медиана ratio и доля |ratio-1|≤0.2."""
    lags = [r["lag"] for r in rows if not r["same_block"]]
    same = sum(1 for r in rows if r["same_block"])
    catchable = sum(1 for l in lags if l >= 1)
    ratios = sorted(r["model"]["ratio"] for r in rows if r["model"] and r["model"]["ratio"] == r["model"]["ratio"])
    within = sum(1 for x in ratios if abs(x - 1) <= 0.20)
    med = ratios[len(ratios) // 2] if ratios else float("nan")
    return {"n": len(rows), "same_block": same, "catchable": catchable, "non_same": len(lags),
            "lag_min": min(lags) if lags else None, "lag_max": max(lags) if lags else None,
            "lags_sorted": sorted(lags), "model_n": len(ratios), "model_within20": within, "model_median_ratio": med}


# ---------------------------------------------------------------------------
# RPC-обвязка
# ---------------------------------------------------------------------------

def resolve_aggregator(rpc: RPC, collateral_silo: str) -> str:
    """ЖИВОЙ резолв S/USD-агрегатора (не хардкод — урок: рукописный хвост адреса дал 0-байтовые ответы):
    залоговый силос → config → getConfig(силос).solvencyOracle → oracleConfig()→getConfig().primaryAggregator.
    Тот же путь, что в oracle_check.py (переиспользуем read_chainlink_config)."""
    from analysis.oracle_check import read_chainlink_config
    from analysis.read_fee import SEL_CONFIG, SEL_GETCONFIG, parse_config, _word
    config = "0x" + _word(rpc.eth_call(collateral_silo, SEL_CONFIG), 0)[24:]
    cfg = parse_config(rpc.eth_call(config, SEL_GETCONFIG + collateral_silo[2:].rjust(64, "0")))
    oracle = cfg["solvencyOracle"]
    cc = read_chainlink_config(rpc, oracle)
    if not cc:
        raise RuntimeError(f"не смог прочитать ChainlinkV3-конфиг у оракула {oracle}")
    return cc["primary_aggregator"]


def make_is_solvent_at(rpc: RPC, silo: str, borrower: str):
    args = SEL_IS_SOLVENT + _addr_pad(silo) + _addr_pad(borrower)

    def is_solvent_at(block: int) -> bool:
        ret = rpc.call("eth_call", [{"to": SILO_LENS_SONIC, "data": args}, hex(block)])
        return int(ret, 16) == 1
    return is_solvent_at


def oracle_price_e8(rpc: RPC, aggregator: str, block: int) -> int:
    """latestRoundData().answer на историческом блоке (int, e8)."""
    ret = rpc.call("eth_call", [{"to": aggregator, "data": SEL_LATEST_ROUND}, hex(block)])
    h = ret[2:]
    if len(h) < 320:
        raise RpcError(f"latestRoundData вернул {len(h)//2} байт — не 5 слов")
    answer = int(h[64:128], 16)
    if answer >= 2 ** 255:  # int256 отрицательный — не бывает у цены, но честно декодируем
        answer -= 2 ** 256
    return answer


def get_max_liquidation_at(rpc: RPC, hook: str, borrower: str, block: int) -> tuple[int, int, bool]:
    """maxLiquidation на ИСТОРИЧЕСКОМ блоке (get_max_liquidation из live_detector — только latest)."""
    ret = rpc.call("eth_call", [{"to": hook, "data": "0xbd02d848" + _addr_pad(borrower)}, hex(block)])
    h = ret[2:] if ret.startswith("0x") else ret
    if len(h) < 192:
        return 0, 0, False
    return int(h[0:64], 16), int(h[64:128], 16), int(h[128:192], 16) != 0


def main():
    ap = argparse.ArgumentParser(description="Бэктест критериев B/C на реальных исторических ликвидациях")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", required=True)
    ap.add_argument("--days", type=float, default=45.0, help="окно ликвидаций для анализа")
    ap.add_argument("--coverage-days", type=float, default=30.0, help="окно event-охвата ПЕРЕД эпизодом")
    ap.add_argument("--max-episodes", type=int, default=10, help="кап эпизодов (RPC-бюджет)")
    ap.add_argument("--min-repay-usd", type=float, default=50.0, help="отсечь пылевые эпизоды")
    ap.add_argument("--aggregator", default=None, help="дефолт: живой резолв через solvencyOracle залоговой стороны")
    a = ap.parse_args()

    rpc = RPC(a.rpc)
    silo = a.silo.lower()
    mkt = resolve_market(rpc, silo)
    dec, cdec = mkt["debt_decimals"], mkt["coll_decimals"]
    aggregator = a.aggregator or resolve_aggregator(rpc, mkt["collateral_silo"])
    sys.stderr.write(f"S/USD агрегатор (живой резолв): {aggregator}\n")

    tip = rpc.block_number()
    frm = find_block_at_ts(rpc, rpc.block_ts(tip) - int(a.days * 86400), tip)
    sys.stderr.write(f"Ликвидации на {silo} за {a.days:.0f}д (блоки {frm}..{tip}):\n")
    logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000)
    events = [e for e in (decode_liquidation_log(l) for l in logs) if e and e["silo"].lower() == silo]
    episodes = cluster_episodes(events)
    sys.stderr.write(f"событий: {len(events)} → эпизодов: {len(episodes)}\n")

    # пылевой фильтр + кап (свежие первыми — ближе к текущему состоянию рынка)
    min_repay_raw = int(a.min_repay_usd * 10 ** dec)
    episodes = [e for e in episodes if e["repay_raw_total"] >= min_repay_raw]
    episodes = sorted(episodes, key=lambda x: -x["first_block"])[:a.max_episodes]
    sys.stderr.write(f"после фильтра ≥${a.min_repay_usd:.0f} и капа {a.max_episodes}: {len(episodes)}\n\n")
    if not episodes:
        return print("Эпизодов для бэктеста нет — расширь --days или снизь --min-repay-usd.")

    # Borrow-события для проверки покрытия: одно окно на всех (дешевле, чем по эпизоду)
    cov_from = find_block_at_ts(rpc, rpc.block_ts(min(e["first_block"] for e in episodes))
                                - int(a.coverage_days * 86400), tip)
    sys.stderr.write(f"Borrow-события для покрытия (блоки {cov_from}..{tip}):\n")
    borrow_logs = fetch_liquidation_logs(rpc, cov_from, tip, chunk=10_000, topic0=TOPIC0_BORROW)
    borrow_blocks: dict[str, list[int]] = {}
    for l in borrow_logs:
        if (l.get("address") or "").lower() == silo:
            o = decode_borrow_owner(l)
            if o:
                borrow_blocks.setdefault(o, []).append(int(l["blockNumber"], 16))
    liq_blocks_hist: dict[str, list[int]] = {}
    for e in events:
        liq_blocks_hist.setdefault(e["borrower"], []).append(e["block"])

    rows = []
    for ep in episodes:
        b, L = ep["borrower"], ep["first_block"]
        wb = walk_back_insolvency(make_is_solvent_at(rpc, silo, b), L)
        E = wb["first_insolvent"]
        # покрытие: любое НАШЕ событие заёмщика (Borrow или предыдущая ликвидация) строго до E
        covered = any(x < E for x in borrow_blocks.get(b, [])) or any(x < E for x in liq_blocks_hist.get(b, []))
        # модель (C) per-event: каждое событие каскада — по цене оракула СВОЕГО блока.
        # v1 (вся сумма по цене первого блока) считается рядом на каскадах — видно артефакт агрегации.
        try:
            model = model_check_events(ep["events"], lambda blk: oracle_price_e8(rpc, aggregator, blk),
                                       mkt["liq_fee_wei"], dec, cdec)
            model_v1 = None
            if ep["n_events"] > 1:
                model_v1 = model_check(ep["repay_raw_total"], ep["withdraw_raw_total"],
                                       oracle_price_e8(rpc, aggregator, L), mkt["liq_fee_wei"], dec, cdec)
        except (RpcError, RuntimeError) as ex:
            sys.stderr.write(f"  {b[:12]}… оракул@{L}: {str(ex)[:80]} — без модели\n")
            model, model_v1 = None, None
        # предсказание maxLiquidation на L-1 vs факт первого события (что видел бы детектор)
        try:
            _, pred_repay, _ = get_max_liquidation_at(rpc, mkt["hook"], b, L - 1)
        except (RpcError, RuntimeError):
            pred_repay = None
        rows.append({"borrower": b, "liq_block": L, "first_insolvent": E, "lag": wb["lag"],
                     "same_block": wb["same_block"], "truncated": wb["truncated"], "rpc_calls": wb["calls"],
                     "covered": covered, "n_events": ep["n_events"], "model": model,
                     "pred_repay_raw": pred_repay, "first_repay_raw": ep["repay_raw_first"],
                     "liquidator": ep["first_liquidator"]})
        lagtxt = "SAME-BLOCK (block-polling не бьёт)" if wb["same_block"] else \
                 f"lag={wb['lag']}{'+ (обрезан)' if wb['truncated'] else ''} блоков"
        mtxt = f"gross реализ/предсказ={model['ratio']:.2f}" if model else "модель: н/д"
        if model and model_v1:
            mtxt += f" (v1-однаяцена={model_v1['ratio']:.2f}, цен запрошено {model['n_price_calls']})"
        ptxt = (f"maxLiq@L-1={pred_repay/10**dec:,.0f} vs факт={ep['repay_raw_first']/10**dec:,.0f}"
                if pred_repay is not None else "maxLiq: н/д")
        sys.stderr.write(f"  {b}  L={L}  {lagtxt}  охвачен={'ДА' if covered else 'НЕТ'}  "
                         f"события={ep['n_events']}  {mtxt}  {ptxt}\n")

    s = summarize(rows)
    print("\n================ КРИТЕРИИ LIVE-READY (STATE.md §7) ================")
    print(f"эпизодов: {s['n']}  (same-block: {s['same_block']}, с ненулевым окном: {s['non_same']})")
    if s["non_same"]:
        print(f"(B) успели бы с 1-block-polling (lag≥1): {s['catchable']}/{s['non_same']}  "
              f"lag min/max: {s['lag_min']}/{s['lag_max']}  все lag: {s['lags_sorted']}")
    if s["same_block"]:
        print(f"    same-block захватов: {s['same_block']} — против них нужен oracle-frontrun/mempool, не polling")
    if s["model_n"]:
        print(f"(C) модель gross: в ±20% — {s['model_within20']}/{s['model_n']}, медиана ratio {s['model_median_ratio']:.2f}")
    print("(A) fork-replay — отдельная задача, этим харнессом не покрывается")
    uncovered = [r["borrower"] for r in rows if not r["covered"]]
    if uncovered:
        print(f"⚠ вне event-охвата ({len(uncovered)}): {', '.join(x[:12]+'…' for x in uncovered)} — данные в пользу бэкафилла §7.1")


if __name__ == "__main__":
    main()
