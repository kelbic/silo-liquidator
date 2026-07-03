#!/usr/bin/env bash
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: Morpho-бот"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py"; exit 1; }
cat > "$DIR/analysis/latency_probe.py" << 'FILE_EOF'
#!/usr/bin/env python3
"""latency_probe.py — РЕТРОСПЕКТИВНЫЙ ЛАТЕНТНОСТНЫЙ БЮДЖЕТ большого силоса Sonic. (read-only, публичный RPC)

Вопрос, от которого зависит весь проект: победа инкумбента — это ЛАТЕНТНОСТНАЯ ГОНКА (успеть первым, выиграть
можно скоростью) или PRIORITY-АУКЦИОН (кто больше дал приоритетной комиссии, выиграть можно только доплатой)?
От ответа зависит, есть ли смысл в живом paper-боте и стоит ли вообще заходить.

Дёшево измеримые сигналы (без archive, без трейсов — только логи + блоки + receipts за неделю):
  1. Позиция победителя в блоке (transactionIndex) и его priority-tip vs медиана блока.
  2. Соперники-ликвидаторы В ТОМ ЖЕ блоке, чьи tx РЕВЕРТНУЛИ (проиграли гонку внутри блока).
     Множество «ликвидаторов» = все distinct liquidator из логов (боты, выигравшие ≥1 раз); их tx ловим
     по tx.to ∈ этому множеству. Реверт соперника в победном блоке = он гонялся и проиграл здесь.
  3. Соперники, ревертнувшие в блоке+1/+2 (пришли на блок-два позже — прямая улика латентностного зазора).

Разбор:
  • contested-rate НИЗКИЙ (соперники редко попадают даже в победный блок, чаще ревертят позже) →
    зазор по латентности ЕСТЬ, более быстрый бот втиснется → БЮДЖЕТ ЕСТЬ, paper-бот оправдан.
  • contested-rate ВЫСОКИЙ + победитель почти всегда с МАКСИМАЛЬНЫМ tip → PRIORITY-АУКЦИОН →
    нужна доплата за приоритет (постоянный расход), не чистая скорость.
  • contested-rate ВЫСОКИЙ, но победитель НЕ всегда с макс tip → упорядочивание НЕ по tip, а по приходу
    (Sonic FCFS) → чистая суб-блочная скоростная гонка, зазор ~0, для удалённого бота очень тяжело.

Запуск (публичный RPC ок, archive НЕ нужен):
  python3 -m analysis.latency_probe --rpc https://rpc.soniclabs.com --days 7
  python3 -m analysis.latency_probe --rpc <rpc> --days 7 --silo 0x7e88ae5e50474a48dea4c42a634aa7485e7caa62
"""
from __future__ import annotations
import argparse
import statistics
import sys
from collections import Counter

from analysis.contestation import (
    RPC, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts,
)

BIG_SILO = "0x7e88ae5e50474a48dea4c42a634aa7485e7caa62"


def _hx(x) -> int:
    try:
        return int(x, 16) if isinstance(x, str) else int(x)
    except (TypeError, ValueError):
        return 0


def tip_of(tx: dict, base_fee: int) -> int:
    """Priority-tip (wei/gas) полученный из объекта tx и baseFee блока — без receipt.
    type-2: min(maxPriorityFee, maxFee-baseFee); legacy: gasPrice-baseFee. Никогда < 0."""
    t = _hx(tx.get("type", "0x0"))
    if t == 2 and tx.get("maxFeePerGas") is not None:
        max_fee = _hx(tx.get("maxFeePerGas"))
        max_prio = _hx(tx.get("maxPriorityFeePerGas", "0x0"))
        return max(0, min(max_prio, max_fee - base_fee))
    gp = _hx(tx.get("gasPrice", "0x0"))
    return max(0, gp - base_fee)


def classify_block(block: dict, winner_txhash: str, known_liquidators: set, status_of) -> dict:
    """Разбор победного блока. status_of(txhash)->int (1/0) вызывается только для соперников-кандидатов.
    Возвращает позицию/tip победителя, список соперников-ликвидаторов в блоке и их реверты."""
    txs = block.get("transactions") or []
    base_fee = _hx(block.get("baseFeePerGas", "0x0"))
    n = len(txs)
    win = next((t for t in txs if (t.get("hash") or "").lower() == winner_txhash.lower()), None)
    win_idx = _hx(win.get("transactionIndex", "0x0")) if win else None
    win_tip = tip_of(win, base_fee) if win else None
    all_tips = [tip_of(t, base_fee) for t in txs] if txs else []
    median_tip = int(statistics.median(all_tips)) if all_tips else 0

    competitors = []
    for t in txs:
        to = (t.get("to") or "").lower()
        h = (t.get("hash") or "")
        if h.lower() == winner_txhash.lower():
            continue
        if to in known_liquidators:  # ликвидатор-бот, но не победитель этого лога
            st = status_of(h)
            competitors.append({"to": to, "tip": tip_of(t, base_fee), "reverted": (st == 0), "hash": h,
                                "idx": _hx(t.get("transactionIndex", "0x0"))})
    contested = any(c["reverted"] for c in competitors)
    # выиграл ли победитель ставкой: его tip — максимальный среди ликвидационных tx блока?
    liq_tips = [c["tip"] for c in competitors] + ([win_tip] if win_tip is not None else [])
    winner_is_top = (win_tip is not None and liq_tips and win_tip >= max(liq_tips))
    return {"n_tx": n, "winner_index": win_idx, "winner_tip": win_tip, "median_tip": median_tip,
            "competitors": competitors, "contested": contested, "winner_is_top_bidder": winner_is_top,
            "base_fee": base_fee}


def verdict(stats: dict) -> list:
    """stats — агрегаты по всем победным блокам. Возвращает строки вердикта."""
    n = stats["blocks"]
    if n == 0:
        return ["Нет победных блоков за окно — расширь --days или проверь силос."]
    contested_rate = stats["contested_blocks"] / n
    top_rate = (stats["winner_top_bidder"] / stats["contested_blocks"]) if stats["contested_blocks"] else 0.0
    late = stats["late_competitor_blocks"]
    L = []
    L.append(f"Победных блоков: {n} | с соперником-ревертом В ТОМ ЖЕ блоке: {stats['contested_blocks']} "
             f"({contested_rate*100:.0f}%) | с соперником в блоке±1..2 позже: {late}")
    L.append(f"Позиция победителя в блоке (медиана index): {stats['median_winner_index']} из "
             f"~{stats['median_block_tx']} tx | tip победителя vs медиана блока: "
             f"×{stats['winner_tip_ratio']:.1f}")
    L.append("")
    if contested_rate < 0.20:
        L.append("🟢 ВЕРДИКТ: ЛАТЕНТНОСТНЫЙ ЗАЗОР ЕСТЬ. Соперники редко попадают даже в победный блок — они")
        L.append("   на блок-два позади. Более быстрый бот способен втиснуться → БЮДЖЕТ ЕСТЬ, живой paper-бот")
        L.append("   оправдан: он померит, попадаешь ли ТЫ в победный блок.")
    elif contested_rate >= 0.20 and top_rate >= 0.70:
        L.append("🔴 ВЕРДИКТ: ПОХОЖЕ НА PRIORITY-АУКЦИОН. В спорных блоках победитель почти всегда с максимальным")
        L.append(f"   priority-tip ({top_rate*100:.0f}%). Выигрыш — доплатой за приоритет, а не скоростью. Вход требует")
        L.append("   переставлять по газу (постоянный расход), латентность вторична. Порог входа выше, чем скорость.")
    else:
        L.append("🟡 ВЕРДИКТ: СУБ-БЛОЧНАЯ СКОРОСТНАЯ ГОНКА. Соперники в блоке есть, но победитель НЕ всегда с макс")
        L.append(f"   tip (top-rate {top_rate*100:.0f}%) — упорядочивание не по ставке, а по приходу (Sonic FCFS).")
        L.append("   Побеждает физически первый в секвенсере. Для удалённого бота зазор ~суб-блок — очень тяжело,")
        L.append("   но это не аукцион: доплата не поможет, поможет только со-локация. Paper покажет твой реальный зазор.")
    L.append("")
    L.append("NB: сигнал строится на соперниках, которые хоть раз выигрывали (их контракты известны из логов);")
    L.append("    чистые вечные-лузеры не видны. И это latency-геометрия, НЕ размер приза (тот — отдельно, §4).")
    return L


def main():
    ap = argparse.ArgumentParser(description="Латентностный бюджет большого силоса Silo (Sonic)")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--silo", default=BIG_SILO, help="целевой силос (по умолч. большой wS/USDC)")
    ap.add_argument("--max-blocks", type=int, default=200, help="ограничение числа победных блоков к разбору")
    a = ap.parse_args()
    a.silo = a.silo.lower()
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
    known = {e["liquidator"].lower() for e in uniq}                 # все ликвидатор-контракты окна
    target = [e for e in uniq if e["silo"].lower() == a.silo]       # только целевой силос
    sys.stderr.write(f"ликвидаций всего {len(uniq)}, на целевом силосе {len(target)}, "
                     f"известных ликвидаторов {len(known)}\n")
    if not target:
        sys.exit("На целевом силосе за окно ликвидаций нет — проверь --silo/--days.")

    # кэш receipt-статусов (только для соперников-кандидатов)
    status_cache = {}
    def status_of(txhash):
        if txhash in status_cache:
            return status_cache[txhash]
        r = rpc.call("eth_getTransactionReceipt", [txhash]) or {}
        st = _hx(r.get("status", "0x1"))
        status_cache[txhash] = st
        return st

    block_cache = {}
    def get_block(num):
        if num in block_cache:
            return block_cache[num]
        b = rpc.call("eth_getBlockByNumber", [hex(num), True]) or {}
        block_cache[num] = b
        return b

    agg = {"blocks": 0, "contested_blocks": 0, "winner_top_bidder": 0, "late_competitor_blocks": 0,
           "win_indices": [], "block_txcounts": [], "tip_ratios": []}
    per_block_rows = []
    for e in target[: a.max_blocks]:
        blk = get_block(e["block"])
        cb = classify_block(blk, e["tx"], known, status_of)
        agg["blocks"] += 1
        agg["block_txcounts"].append(cb["n_tx"])
        if cb["winner_index"] is not None:
            agg["win_indices"].append(cb["winner_index"])
        if cb["winner_tip"] is not None and cb["median_tip"] > 0:
            agg["tip_ratios"].append(cb["winner_tip"] / cb["median_tip"])
        if cb["contested"]:
            agg["contested_blocks"] += 1
            if cb["winner_is_top_bidder"]:
                agg["winner_top_bidder"] += 1
        else:
            # соперник в блоке±1..2 позже? (проксимация латентностного зазора)
            late = False
            for d in (1, 2):
                nb = get_block(e["block"] + d)
                for t in (nb.get("transactions") or []):
                    to = (t.get("to") or "").lower()
                    if to in known and to != e["liquidator"].lower():
                        if status_of(t.get("hash", "")) == 0:
                            late = True; break
                if late:
                    break
            if late:
                agg["late_competitor_blocks"] += 1
        per_block_rows.append((e["block"], e["liquidator"], cb))

    agg["median_winner_index"] = int(statistics.median(agg["win_indices"])) if agg["win_indices"] else -1
    agg["median_block_tx"] = int(statistics.median(agg["block_txcounts"])) if agg["block_txcounts"] else 0
    agg["winner_tip_ratio"] = (statistics.median(agg["tip_ratios"]) if agg["tip_ratios"] else 0.0)

    print("=" * 74)
    print(f"  LATENCY PROBE — большой силос {a.silo[:12]}… — окно {a.days:g}д")
    print("=" * 74)
    winners = Counter(e["liquidator"] for e in target)
    print("победители на силосе:")
    for w, c in winners.most_common(5):
        print(f"   {w}  {c} ({c/len(target)*100:.0f}%)")
    print(f"\nразобрано победных блоков: {agg['blocks']}")
    # несколько примеров спорных блоков
    shown = 0
    for blk, winner, cb in per_block_rows:
        if cb["contested"] and shown < 5:
            comps = ", ".join(f"{c['to'][:8]}…{'✗rev' if c['reverted'] else ''}(tip {c['tip']//10**9}g)"
                              for c in cb["competitors"])
            print(f"  блок {blk}: победитель idx {cb['winner_index']}/{cb['n_tx']} tip {(cb['winner_tip'] or 0)//10**9}gwei"
                  f" | соперники: {comps}")
            shown += 1
    print("\n" + "=" * 74)
    for line in verdict(agg):
        print(line)


if __name__ == "__main__":
    main()
FILE_EOF
cd "$DIR"
python3 -m py_compile analysis/latency_probe.py && echo "[OK] py_compile latency_probe"
python3 - << 'PY_TEST'
import analysis.latency_probe as lp
G=10**9
known={"0xliqwin","0xliqcomp"}
block={"baseFeePerGas":hex(50*G),"transactions":[
  {"hash":"0xaaa","to":"0xliqwin","type":"0x2","maxFeePerGas":hex(60*G),"maxPriorityFeePerGas":hex(5*G),"transactionIndex":"0x1"},
  {"hash":"0xbbb","to":"0xliqcomp","type":"0x2","maxFeePerGas":hex(60*G),"maxPriorityFeePerGas":hex(9*G),"transactionIndex":"0x7"}]}
cb=lp.classify_block(block,"0xaaa",known, lambda h: 0 if h=="0xbbb" else 1)
assert cb["competitors"][0]["idx"]==7 and cb["winner_index"]==1 and cb["contested"] is True
print("[OK] classify_block: idx-поле + прежнее поведение не сломано")
PY_TEST
echo ">> latency_probe.py обновлён (+idx у соперников, аддитивно)."
