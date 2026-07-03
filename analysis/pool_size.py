#!/usr/bin/env python3
"""pool_size.py v2 — РАЗМЕР ПРИЗА по VALUE (не count) + разбивка USD по победителям + серия по неделям. (read-only)

v1 совершил задокументированный сбой №1 (wrong population / mixed basis): применял СЧЁТНУЮ долю челленджера
(21% побед) к USD-пулу без единого замера value-share. v2 чинит:
  1. ЦЕЛЬ выбирается по repaid-USD, не по числу ликвидаций и не хардкодом адреса (в v1 «большой» силос
     0x7e88ae5e с пулом $275 был выбран по COUNT — а по деньгам главный приз на 0x322e1d53, ×56 больше).
  2. Раскладка USD ПО ПОБЕДИТЕЛЯМ: repaid$ и средний чек на каждого. Видно, крупнее или мельче чеки
     челленджера среднего — знак value-share, который счётная доля скрывает (каскады могут гнуть в обе стороны).
  3. Серия repaid по НЕДЕЛЯМ: тренд 82→26→19.4 ликв/д означает либо каскадно-раздутую 30-дн базу, либо
     остывающий рынок. Если объём тает — даже плюсовый бакет тающий.
  4. Стоимость: дефолт НЕ $2, а бэнд с платным endpoint (спам через публичный RPC = бан) + пометка про
     key-management (горячий ключ с газом убивает 'без ключа/капитала') и со-теннанси с Morpho-ботом.
  5. НЕ печатает 🟢/🟡/🔴 — вопрос value-share решается ДАННЫМИ этого прогона, а не порогом по счётной доле.

USD — по ТЕКУЩИМ ценам DeFiLlama: оценка размера, не PnL.

Запуск:
  python3 -m analysis.pool_size --rpc https://rpc.soniclabs.com --chain sonic --days 30
  python3 -m analysis.pool_size --rpc <rpc> --chain sonic --days 30 --cost 45
"""
from __future__ import annotations
import argparse
import sys
from collections import Counter, defaultdict

from analysis.contestation import (
    RPC, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts,
    silo_token_meta, llama_prices,
)

MARGINS = {"conserv": 0.004, "mid": 0.039, "optim": 0.069}  # нетто-маржа от repaid (§4)


def scale_monthly(total: float, days: float) -> float:
    return total * (30.0 / days) if days > 0 else 0.0


def group_by_silo(events: list) -> dict:
    """{silo: {repaid_usd, count, unpriced, symbol, winners_count:Counter, winners_usd:dict}}.
    winners_usd — repaid-USD по каждому победителю (VALUE-share, не count)."""
    g = defaultdict(lambda: {"repaid_usd": 0.0, "count": 0, "unpriced": 0, "symbol": "?",
                             "winners_count": Counter(), "winners_usd": defaultdict(float)})
    for e in events:
        s = e["silo"].lower()
        row = g[s]
        row["count"] += 1
        row["symbol"] = e.get("symbol", "?")
        w = e["liquidator"].lower()
        row["winners_count"][w] += 1
        if e.get("usd") is not None:
            row["repaid_usd"] += e["usd"]
            row["winners_usd"][w] += e["usd"]
        else:
            row["unpriced"] += 1
    return g


def winner_value_table(row: dict) -> list:
    """[(winner, wins, repaid_usd, mean_check_usd, count_share, value_share)] по убыванию USD."""
    tot_usd = row["repaid_usd"] or 0.0
    tot_cnt = sum(row["winners_count"].values()) or 1
    out = []
    for w, cnt in row["winners_count"].most_common():
        usd = row["winners_usd"].get(w, 0.0)
        out.append((w, cnt, usd, (usd / cnt if cnt else 0.0),
                    cnt / tot_cnt, (usd / tot_usd if tot_usd else 0.0)))
    out.sort(key=lambda t: t[2], reverse=True)  # по USD
    return out


def weekly_series(events: list, days: float) -> list:
    """[(week_idx, repaid_usd, count)] — repaid по неделям (0 = самая старая), для тренда объёма."""
    priced = [e for e in events if e.get("usd") is not None and "block" in e]
    if not events:
        return []
    blocks = [e["block"] for e in events if "block" in e]
    if not blocks:
        return []
    b_min, b_max = min(blocks), max(blocks)
    span = max(1, b_max - b_min)
    n_weeks = max(1, round(days / 7))
    buckets = defaultdict(lambda: {"usd": 0.0, "count": 0})
    for e in events:
        if "block" not in e:
            continue
        wk = min(n_weeks - 1, int((e["block"] - b_min) / span * n_weeks))
        buckets[wk]["count"] += 1
        if e.get("usd") is not None:
            buckets[wk]["usd"] += e["usd"]
    return [(wk, buckets[wk]["usd"], buckets[wk]["count"]) for wk in range(n_weeks)]


def enrich(rpc: RPC, events: list, chain: str, no_usd: bool):
    meta_cache = {}
    for s in {e["silo"] for e in events}:
        silo_token_meta(rpc, s, meta_cache)
    prices = {}
    if not no_usd:
        toks = {m["token"] for m in meta_cache.values() if m.get("token")}
        prices = llama_prices(chain, toks)
        sys.stderr.write(f"цены DeFiLlama: {len(prices)}/{len(toks)} токенов\n")
    for e in events:
        m = meta_cache.get(e["silo"], {"symbol": "?", "decimals": 18, "token": None})
        e["symbol"] = m["symbol"]
        e["repay"] = e["repay_raw"] / (10 ** m["decimals"])
        px = prices.get((m.get("token") or "").lower())
        e["usd"] = (e["repay"] * px) if px else None


def main():
    ap = argparse.ArgumentParser(description="Размер приза Silo по VALUE + разбивка по победителям + тренд")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--chain", default="sonic")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--silo", default=None, help="цель явно; иначе — силос с наибольшим repaid-USD")
    ap.add_argument("--margin", type=float, default=0.039)
    ap.add_argument("--cost", type=float, default=45.0,
                    help="реалистичная марж. стоимость $/мес (платный RPC; дефолт 45, НЕ 2)")
    ap.add_argument("--no-usd", action="store_true")
    ap.add_argument("--top", type=int, default=6)
    a = ap.parse_args()
    MARGINS["mid"] = a.margin
    rpc = RPC(a.rpc)

    sys.stderr.write(f"тяну ликвидации за {a.days:g}д…\n")
    tip = rpc.block_number()
    frm = find_block_at_ts(rpc, rpc.block_ts(tip) - int(a.days * 86400), tip)
    logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000)
    events = [e for e in (decode_liquidation_log(l) for l in logs) if e]
    seen, uniq = set(), []
    for e in events:
        k = (e["tx"], e["log_index"])
        if k not in seen:
            seen.add(k); uniq.append(e)
    events = uniq
    if not events:
        sys.exit("Ликвидаций за окно нет.")
    enrich(rpc, events, a.chain, a.no_usd)

    g = group_by_silo(events)
    have_usd = any(row["repaid_usd"] > 0 for row in g.values())
    key = (lambda kv: kv[1]["repaid_usd"]) if have_usd else (lambda kv: kv[1]["count"])
    ranked_silos = sorted(g.items(), key=key, reverse=True)

    print("=" * 76)
    print(f"  POOL SIZE v2 (VALUE-share) — Silo {a.chain.upper()} — {a.days:g}д → норм. 30д")
    print("=" * 76)
    if not have_usd:
        print("!! Цен DeFiLlama нет — только родные единицы.\n")
    print(f"{'силос':14s} {'sym':6s} {'ликв':>5s} {'repaid$/30д':>13s} {'пул@%.1f%%$/30д':>15s}" % (a.margin*100,))
    for s, row in ranked_silos[: a.top]:
        rep_m = scale_monthly(row["repaid_usd"], a.days)
        print(f"{s[:12]}… {row['symbol']:6.6s} {row['count']:5d} ${rep_m:>12,.0f} ${rep_m*a.margin:>14,.0f}")
    print(f"\nсуммарный repaid всех силосов (норм.30д): "
          f"${scale_monthly(sum(r['repaid_usd'] for r in g.values()), a.days):,.0f}")

    # цель = по объёму (или явная)
    target = a.silo.lower() if a.silo else (ranked_silos[0][0] if ranked_silos else None)
    if not target or target not in g:
        print("\n⚠ Цель не найдена."); return
    row = g[target]
    rep_m = scale_monthly(row["repaid_usd"], a.days)
    unpriced_frac = row["unpriced"] / row["count"] if row["count"] else 0.0

    print("\n" + "=" * 76)
    print(f"ЦЕЛЬ ПО ОБЪЁМУ: {target}  ({row['symbol']}, {row['count']} ликв/{a.days:g}д)")
    print(f"  repaid ${rep_m:,.0f}/мес | пул нетто @%.1f%%: ${rep_m*a.margin:,.0f}/мес "
          "(бэнд 0.4–6.9%%: $%s–$%s)" % (a.margin*100, f"{rep_m*0.004:,.0f}", f"{rep_m*0.069:,.0f}"))
    if unpriced_frac > 0.1:
        print(f"  NB: {unpriced_frac*100:.0f}% ликвидаций без цены — USD ЗАНИЖЕН")

    # --- РАЗБИВКА ПО ПОБЕДИТЕЛЯМ: count-share vs VALUE-share ---
    print("\n──── ПОБЕДИТЕЛИ: доля по ЧИСЛУ vs доля по ДЕНЬГАМ (это и есть ответ на сбой №1) ────")
    print(f"  {'адрес':44s} {'побед':>5s} {'repaid$':>11s} {'ср.чек$':>9s} {'%счёт':>6s} {'%деньги':>7s}")
    wt = winner_value_table(row)
    mean_all = (rep_m / row["count"]) if row["count"] else 0.0
    for w, cnt, usd, mean_c, cshare, vshare in wt[:8]:
        usd_m = scale_monthly(usd, a.days)
        flag = ""
        if wt.index((w, cnt, usd, mean_c, cshare, vshare)) == 1:  # топ-челленджер по деньгам
            flag = "  ← челленджер"
        print(f"  {w:44s} {cnt:5d} ${usd_m:>10,.0f} ${mean_c:>8,.0f} {cshare*100:5.0f}% {vshare*100:6.0f}%{flag}")
    print(f"  средний чек по силосу: ${mean_all:,.0f}")

    # реалистичный захват — теперь по VALUE-доле челленджера (а не count)
    if len(wt) > 1:
        ch = wt[1]
        ch_vshare = ch[5]
        ch_cshare = ch[4]
        print(f"\n  Топ-челленджер {ch[0][:10]}…: счётная доля {ch_cshare*100:.0f}%, "
              f"ДЕНЕЖНАЯ доля {ch_vshare*100:.0f}%  "
              f"(чек ${ch[3]:,.0f} {'>' if ch[3] > mean_all else '<'} среднего ${mean_all:,.0f})")
        take_v = {k: rep_m * m * ch_vshare for k, m in MARGINS.items()}
        print(f"  Реалистичный удалённый доход (кресло челленджера по ДЕНЬГАМ, ~{ch_vshare*100:.0f}%):")
        print(f"    ${take_v['conserv']:,.0f} … ${take_v['mid']:,.0f} … ${take_v['optim']:,.0f}/мес (маржа 0.4/3.9/6.9%)")
        print(f"    ⚠ кресло НЕ свободно: {ch[0][:10]}… не выходит; вход = внутриблочная гонка с настроенным спамером.")
        # знак нетто при заданной стоимости — по всему диапазону захвата
        print(f"\n  Против марж. стоимости ${a.cost:,.0f}/мес (платный RPC; НЕ $2):")
        for lbl, share in [("челленджер-value", ch_vshare), ("холодные 2%", 0.02)]:
            net_mid = rep_m * a.margin * share - a.cost
            print(f"    {lbl:18s} @3.9%: ${rep_m*a.margin*share:,.0f} − ${a.cost:,.0f} = "
                  f"${net_mid:,.0f}/мес  {'ПЛЮС' if net_mid > 0 else 'МИНУС'}")

    # --- ТРЕНД ПО НЕДЕЛЯМ ---
    print("\n──── ТРЕНД repaid ПО НЕДЕЛЯМ (тает ли рынок) ────")
    ser = weekly_series(events, a.days)
    for wk, usd, cnt in ser:
        bar = "█" * min(40, int(cnt / max(1, max(s[2] for s in ser)) * 40)) if ser else ""
        print(f"  нед {wk}: {cnt:5d} ликв  ${usd:>10,.0f}  {bar}")
    if len(ser) >= 2 and ser[0][2] > 0:
        chg = (ser[-1][2] - ser[0][2]) / ser[0][2] * 100
        print(f"  Δ число ликвидаций первая→последняя неделя: {chg:+.0f}%  "
              f"{'(рынок ОСТЫВАЕТ — пул тающий)' if chg < -25 else '(стабильно)' if abs(chg) <= 25 else '(растёт)'}")

    print("\n" + "=" * 76)
    print("БЕЗ АВТО-ВЕРДИКТА: смотри %деньги челленджера vs %счёт (если ≪ — value-share мал, кресло")
    print("даёт меньше, чем счётная доля обещала), знак нетто против реальной стоимости, и тренд недель.")
    print("Оракул целевого силоса (push/pull) — отдельно: python3 -m analysis.oracle_check --rpc <rpc>")


if __name__ == "__main__":
    main()
