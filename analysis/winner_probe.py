#!/usr/bin/env python3
"""winner_probe.py — ОБГОНЯЕМОСТЬ конкретного победителя на его ЖИРНЫХ (>порог) победах. (read-only)

v6 (ревью доки-30): 'реальный бидер' (tip>10g) пропускает НОГУ СВЯЗКИ (0xa2712025 бидит высоко сам —
это не спам-бот, а флеш-кредитор, подтверждённый decompose). Сплит и idx-тест пересчитаны ДВАЖДЫ:
как было (полная популяция) и с исключением pipeline_addrs (= distinct_to ∪ distinct_from ЭТОГО ЖЕ
прогона — не список руками). Если после исключения n=0 — внешней конкуренции в данных нет.

Запуск:
  python3 -m analysis.winner_probe --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 \
      --winner 0xccd487e01e9df6932f656b53668f58005f604417 --min-usd 100 --days 30
"""
from __future__ import annotations
import argparse
import statistics
import sys

from analysis.contestation import (
    RPC, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts,
    silo_token_meta, llama_prices,
)
from analysis.latency_probe import tip_of, classify_block, _hx

HOOK = "0x6aafd9dd424541885fd79c06fda96929cfd512f9"


def hook_direct_calls(block: dict, exclude_hash: str) -> list:
    base_fee = _hx(block.get("baseFeePerGas", "0x0"))
    out = []
    for t in block.get("transactions") or []:
        to = (t.get("to") or "").lower()
        h = t.get("hash") or ""
        if to == HOOK.lower() and h.lower() != exclude_hash.lower():
            out.append({"from": (t.get("from") or "").lower(), "tip": tip_of(t, base_fee), "hash": h})
    return out


def main():
    ap = argparse.ArgumentParser(description="Обгоняемость победителя на его жирных победах")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", required=True)
    ap.add_argument("--winner", required=True, help="адрес победителя для разреза")
    ap.add_argument("--chain", default="sonic")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--min-usd", type=float, default=100.0, help="порог 'жирной' победы")
    ap.add_argument("--max-blocks", type=int, default=250)
    a = ap.parse_args()
    silo = a.silo.lower()
    winner = a.winner.lower()
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
    known = {e["liquidator"].lower() for e in uniq}

    meta = {}
    m = silo_token_meta(rpc, silo, meta)
    px = llama_prices(a.chain, {m["token"]} if m.get("token") else set()).get((m.get("token") or "").lower())
    target = [e for e in uniq if e["silo"].lower() == silo]
    for e in target:
        e["repay"] = e["repay_raw"] / (10 ** m["decimals"])
        e["usd"] = (e["repay"] * px) if px else None

    all_wins = [e for e in target if e["liquidator"].lower() == winner]
    fat = [e for e in all_wins if e["usd"] is not None and e["usd"] >= a.min_usd]
    sys.stderr.write(f"побед {winner[:10]}… всего {len(all_wins)}, жирных (>${a.min_usd:g}): {len(fat)}\n")
    if not fat:
        sys.exit(f"Нет жирных побед этого адреса на этом силосе за окно — проверь --winner/--min-usd/--days.")

    status_cache = {}
    def status_of(h):
        if h in status_cache:
            return status_cache[h]
        r = rpc.call("eth_getTransactionReceipt", [h]) or {}
        st = _hx(r.get("status", "0x1"))
        status_cache[h] = st
        return st

    block_cache = {}
    def get_block(n):
        if n in block_cache:
            return block_cache[n]
        b = rpc.call("eth_getBlockByNumber", [hex(n), True]) or {}
        block_cache[n] = b
        return b

    rows = []
    winner_to_hook_direct = 0
    hidden_hook_callers = 0
    for e in fat[: a.max_blocks]:
        blk = get_block(e["block"])
        cb = classify_block(blk, e["tx"], known, status_of)
        win_tx = next((t for t in (blk.get("transactions") or []) if (t.get("hash") or "").lower() == e["tx"].lower()), None)
        win_to = (win_tx.get("to") or "").lower() if win_tx else None
        win_from = (win_tx.get("from") or "").lower() if win_tx else None
        if win_to == HOOK.lower():
            winner_to_hook_direct += 1
        hidden = [c for c in hook_direct_calls(blk, e["tx"]) if c["from"] not in known]
        if hidden:
            hidden_hook_callers += 1
        rows.append((e, cb, win_to, win_from, hidden))

    idxs = [cb["winner_index"] for _, cb, _, _, _ in rows if cb["winner_index"] is not None]
    tips_gwei = [(cb["winner_tip"] or 0) / 1e9 for _, cb, _, _, _ in rows]
    contested = [(e, cb) for e, cb, _, _, _ in rows if cb["contested"]]
    uncontested = [(e, cb) for e, cb, _, _, _ in rows if not cb["contested"]]
    distinct_to = sorted({wt for _, _, wt, _, _ in rows if wt})
    distinct_from = sorted({wf for _, _, _, wf, _ in rows if wf})
    # ревью (доки-30): distinct_to/from ЭТОГО ЖЕ прогона — адреса, структурно завязанные на СОБСТВЕННЫЙ
    # tx победителя (кто его вызывает, куда он сам шлёт). Не список вручную — берём то, что тул уже нашёл.
    pipeline_addrs = set(distinct_to) | set(distinct_from)

    print("=" * 78)
    print(f"  WINNER PROBE — {winner} на {silo[:12]}…, порог >${a.min_usd:g}")
    print("=" * 78)
    print(f"жирных побед разобрано: {len(rows)} из {len(fat)} найденных (всего побед адреса: {len(all_wins)})")
    print(f"позиция в блоке (idx): медиана {statistics.median(idxs) if idxs else '—'}, "
          f"мин {min(idxs) if idxs else '—'}, макс {max(idxs) if idxs else '—'}")
    print(f"tip победителя (gwei): медиана {statistics.median(tips_gwei):.1f}" if tips_gwei else "tip: —")
    print(f"\nсо-блоковый соперник ЕСТЬ: {len(contested)}/{len(rows)} "
          f"({len(contested)/len(rows)*100:.0f}%)   НЕТ (побеждает в одиночку): {len(uncontested)}/{len(rows)}")
    if contested:
        top_bidder_any = 0
        top_bidder_rev = 0
        divergent = []
        for e, cb in contested:
            rev_tips = [c["tip"] for c in cb["competitors"] if c["reverted"]]
            is_top_any = cb["winner_is_top_bidder"]
            is_top_rev = (not rev_tips) or (cb["winner_tip"] or 0) >= max(rev_tips)
            top_bidder_any += is_top_any
            top_bidder_rev += is_top_rev
            if is_top_any != is_top_rev:
                divergent.append((e, cb, is_top_any, is_top_rev))
        print(f"из них tip ≥ МАКС среди ВСЕХ известных соперников (rev+ok, может включать шум с других позиций): "
              f"{top_bidder_any}/{len(contested)} ({top_bidder_any/len(contested)*100:.0f}%)")
        print(f"из них tip ≥ МАКС среди ТОЛЬКО РЕВЕРТНУВШИХ рядом (сильнее — соперник реально проиграл ЭТУ гонку): "
              f"{top_bidder_rev}/{len(contested)} ({top_bidder_rev/len(contested)*100:.0f}%)")
        if divergent:
            print(f"  РАСХОЖДЕНИЕ ANY vs REV на {len(divergent)} блок(ах) — тут 'ok'-сосед реально сдвинул оценку:")
            for e, cb, ia, ir in divergent:
                ok_noise = [c for c in cb["competitors"] if not c["reverted"]]
                noise_str = ", ".join(f"{c['to'][:10]}…{c['tip']/1e9:.0f}g" for c in ok_noise) if ok_noise else "—"
                print(f"    блок {e['block']} (${e['usd']:.0f}): ANY={'top' if ia else 'не-top'} "
                      f"REV={'top' if ir else 'не-top'} | 'ok'-сосед(и) с шумным tip: {noise_str}")
        else:
            print(f"  Расхождений ANY/REV НЕТ на видимых {len(contested)} блоках — 'ok'-шум в ЭТОМ прогоне")
            print(f"  ни разу не поменял классификацию (эффект синтетически доказан, но здесь не проявился).")

        BID_FLOOR = 10 * 1e9
        gt = eq = lt = 0
        for e, cb in contested:
            real_bidder_tips = [c["tip"] for c in cb["competitors"] if c["reverted"] and c["tip"] > BID_FLOOR]
            if not real_bidder_tips:
                continue
            m = max(real_bidder_tips)
            wt = cb["winner_tip"] or 0
            if wt > m: gt += 1
            elif wt == m: eq += 1
            else: lt += 1
        n_real = gt + eq + lt
        print(f"\nСПЛИТ >/=/< против РЕАЛЬНОГО бидера (tip>10g, не константный 5g-неигрок), n={n_real} блоков:")
        print(f"  строго БОЛЬШЕ (перебил): {gt}/{n_real}   РАВНО (сравнялся, не перебил): {eq}/{n_real}   "
              f"строго МЕНЬШЕ (проиграл по tip, но выиграл ликвидацию): {lt}/{n_real}")
        if n_real and gt == 0:
            print(f"  ⚠ 0 строгих перебитий реального бидера на видимых данных — 'ставка решает' НЕ подтверждена.")

        gt2 = eq2 = lt2 = 0
        for e, cb in contested:
            real_bidder_tips = [c["tip"] for c in cb["competitors"]
                                if c["reverted"] and c["tip"] > BID_FLOOR and c["to"] not in pipeline_addrs]
            if not real_bidder_tips:
                continue
            m = max(real_bidder_tips)
            wt = cb["winner_tip"] or 0
            if wt > m: gt2 += 1
            elif wt == m: eq2 += 1
            else: lt2 += 1
        n_ext = gt2 + eq2 + lt2
        print(f"\nТОТ ЖЕ СПЛИТ, ИСКЛЮЧАЯ pipeline_addrs ({len(pipeline_addrs)} адресов из distinct_to/from ВЫШЕ), "
              f"n={n_ext}:")
        if n_ext == 0:
            print(f"  ⚠ 0 блоков остаётся — ВЕСЬ сигнал 'реальный бидер' в исходном сплите был против ноги")
            print(f"  связки, не внешнего участника. Внешней равно-tip конкуренции в данных НЕТ.")
        else:
            print(f"  строго БОЛЬШЕ: {gt2}/{n_ext}   РАВНО: {eq2}/{n_ext}   строго МЕНЬШЕ: {lt2}/{n_ext}")
            print(f"  ⚠ Это {n_ext} блок(ов) — небольшая выборка, но это ВНЕШНИЙ сигнал, не отфильтрован.")

        idx_ahead = idx_behind = 0
        idx_examples = []
        for e, cb in contested:
            win_idx = cb["winner_index"]
            if win_idx is None:
                continue
            for c in cb["competitors"]:
                if c["reverted"] and c["tip"] == (cb["winner_tip"] or 0) and "idx" in c:
                    if c["idx"] < win_idx:
                        idx_ahead += 1
                    elif c["idx"] > win_idx:
                        idx_behind += 1
                    idx_examples.append((e["block"], win_idx, c["idx"]))
        if idx_ahead + idx_behind:
            print(f"\nСРЕДИ РЕВЕРТНУВШИХ С ТОЧНО РАВНЫМ tip: их idx РАНЬШЕ победителя (не про приход) "
                  f"{idx_ahead}, ПОЗЖЕ (согласуется с FCFS-в-класса) {idx_behind}")
            print(f"  ⚠ idx — позиция в СОБРАННОМ блоке, не порядок прихода к секвенсеру; и неизвестно, целился")
            print(f"  ли реверт в ТУ ЖЕ позицию (calldata не декодируется) — сигнал, не доказательство.")

        idx_ahead_ext = idx_behind_ext = 0
        for e, cb in contested:
            win_idx = cb["winner_index"]
            if win_idx is None:
                continue
            for c in cb["competitors"]:
                if (c["reverted"] and c["tip"] == (cb["winner_tip"] or 0) and "idx" in c
                        and c["to"] not in pipeline_addrs):
                    if c["idx"] < win_idx:
                        idx_ahead_ext += 1
                    elif c["idx"] > win_idx:
                        idx_behind_ext += 1
        n_idx_ext = idx_ahead_ext + idx_behind_ext
        print(f"\nТО ЖЕ, ИСКЛЮЧАЯ pipeline_addrs: РАНЬШЕ {idx_ahead_ext}, ПОЗЖЕ {idx_behind_ext} (n={n_idx_ext})")
        if n_idx_ext == 0:
            print(f"  ⚠ 0 — весь equal-tip сигнал в исходном idx-тесте был против ноги связки. Внешнего")
            print(f"  equal-tip реверта в данных НЕТ — 'скорость vs аукцион' из истории неразрешимо (ревью).")

    print(f"\nАрхитектура победителя — СЫРЫЕ адреса, БЕЗ интерпретации (ревью: 'обёртка' была выводом без")
    print(f"предъявления; это может быть automation-registry, а не bespoke MEV-контракт):")
    print(f"  distinct tx.to   ({len(distinct_to)}): {', '.join(distinct_to)}")
    print(f"  distinct tx.from ({len(distinct_from)}): {', '.join(distinct_from)}")
    print(f"  tx.to==ХУК напрямую в {winner_to_hook_direct}/{len(rows)} побед (0 = НЕ через хук напрямую).")
    print(f"  Если tx.to выше — известный тебе адрес (реестр автоматизации/протокольный кипер), это меняет")
    print(f"  класс противника радикально. Сверь на Sonicscan — фетч оттуда у меня заблокирован.")
    if hidden_hook_callers:
        print(f"⚠ НАЙДЕНЫ прямые вызовы хука с адресов ВНЕ known_liquidators в {hidden_hook_callers}/{len(rows)} "
              f"блоков — это невидимые для competitors-фильтра участники (см. примеры ниже, если есть).")
    else:
        print(f"Прямых вызовов хука с НЕизвестных адресов не найдено. НО (ревью): и competitors-фильтр, и этот")
        print(f"скан слепы к ОДНОМУ классу — боту, который НИ РАЗУ нигде не выиграл (нет emit → нет в")
        print(f"known_liquidators), ЕСЛИ он тоже через обёртку (tx.to = его контракт, не хук). А раз победитель")
        print(f"САМ так делает ({winner_to_hook_direct}/{len(rows)} прямых вызовов) — вероятно, так же делал бы и")
        print(f"новый игрок. Этот 'ноль' почти НЕ информативен ни в какую сторону. Полное закрытие — трассировка")
        print(f"внутренних вызовов (debug_traceBlockByNumber, не публичный RPC) — вне этого разреза.")

    if contested:
        print(f"\n──── ПРИМЕРЫ: жирные победы С со-блоковым соперником ────")
        for e, cb in contested[:8]:
            rev_tips_ex = [c["tip"] for c in cb["competitors"] if c["reverted"]]
            is_top_rev_ex = (not rev_tips_ex) or (cb["winner_tip"] or 0) >= max(rev_tips_ex)
            tag = f"[ANY={'top' if cb['winner_is_top_bidder'] else 'не-top'} REV={'top' if is_top_rev_ex else 'не-top'}]"
            comps = ", ".join(f"{c['to'][:10]}…{'✗rev' if c['reverted'] else 'ok'}(tip {c['tip']/1e9:.0f}g)"
                              for c in cb["competitors"])
            print(f"  блок {e['block']} (${e['usd']:.0f}) {tag}: idx {cb['winner_index']}/{cb['n_tx']} "
                  f"tip {(cb['winner_tip'] or 0)/1e9:.0f}g | соперники: {comps}")
    if uncontested:
        print(f"\n──── ПРИМЕРЫ: жирные победы БЕЗ соперника (один в блоке) ────")
        for e, cb in uncontested[:5]:
            print(f"  блок {e['block']} (${e['usd']:.0f}): idx {cb['winner_index']}/{cb['n_tx']} "
                  f"tip {(cb['winner_tip'] or 0)/1e9:.0f}g")

    print("\n" + "=" * 78)
    print("ФАКТЫ БЕЗ ИНТЕРПРЕТАЦИИ:")
    print("  • со-соперник есть/нет — сырой факт присутствия, НЕ прокси обгоняемости.")
    print("  • top-bidder-среди-ревертнувших — прокси 'аукцион vs нет', но ТОЛЬКО среди ВИДИМЫХ (известных)")
    print("    участников; невидимый более высокий бид не попадёт в расчёт.")
    print("  • Оба pipeline-исключённых пересчёта выше — самая честная версия на сегодня.")


if __name__ == "__main__":
    main()
