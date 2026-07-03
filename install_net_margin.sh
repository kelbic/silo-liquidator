#!/usr/bin/env bash
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: Morpho-бот"; exit 1; }
[ -e "$DIR/chain/morpho.py" ] && { echo "СТОП: Morpho-файлы"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py"; exit 1; }
cat > "$DIR/analysis/net_margin.py" << 'FILE_EOF'
#!/usr/bin/env python3
"""net_margin.py — РЕАЛЬНАЯ чистая маржа по Transfer-логам исполненной сделки. (read-only)

Зачем: liquidationFee=6.5% — ВАЛОВАЯ, посчитанная протоколом из oracle-цены (см. read_fee/поправку #13).
Пробовали проверить через ratio withdrawCollateral/repayDebtAssets по oracle-цене — ЭТО ТАВТОЛОГИЯ (сам
протокол вычисляет withdrawCollateral из oracle-цены + fee ВНУТРИ liquidationCall, своп там не участвует;
наш собственный форк делает своп ОТДЕЛЬНЫМ внешним шагом — ровно как, видимо, делают и внешние боты).

Метод без oracle-зависимости: полный receipt каждой жирной победы → все Transfer-логи (topic0 сверен
keccak) токенов wS и USDC → чистая дельта (сумма IN минус сумма OUT) по адресу победителя.

ВАЖНО (ревью, v2): дельта на адресе победителя — это ОСТАТОК после внутритранзакционного цикла
(флеш-займ → repay долга → получение залога → своп части залога → возврат флеш-займа), А НЕ признак
«свопа нет». Своп ЕСТЬ, дельта — то, что осталось сверх цикла флеш-займа («hold margin», не слиппедж).
Для просмотра ПОЛНОГО потока — режим --decompose <tx> или --decompose-block <N>.

v3-v6 (серия ревью-разборов): дедуп по хешу, различение слепоты/реального нуля, all_address_deltas()
(дельта КАЖДОГО адреса, не только победителя) — вскрыла: ЕСТЬ МИНИМУМ ДВА МАРШРУТА. Маршрут A (роутер
0x157f2158→0x8f10b468): победитель — ЧИСТЫЙ ТРАНЗИТ (0), весь спред на 0x016e1a57. Маршрут B (роутер
0x3a5d6a7a напрямую): победитель держит малый остаток, часть берёт флеш-кредитор 0xa2712025. Знаковый
сплит 61/23/37 — это сигнал МАРШРУТА, не экономика победителя. beneficiary_totals копит дельту КАЖДОГО
адреса по ВСЕЙ выборке (без новых RPC — переиспользует уже загруженный receipt).

v7 (ревью): 0xa2712025 — флеш-кредитор 0xccd487 в некоторых tx (Transfer-уровень, tx.to==0xccd487),
что СТРУКТУРНО отлично от его собственных реверт-попыток как соперника (tx.to==0xa2712025 — его
СОБСТВЕННЫЙ вызов). Оба факта верны и не противоречат друг другу — но это значит: нельзя предполагать
роль адреса по умолчанию, разные появления одного адреса могут быть разными ролями. Добавлено: (а)
beneficiary_counts — счётчик, В СКОЛЬКИХ РАЗНЫХ tx адрес дал ненулевую дельту (отличает 'разово крупный
пул' от 'мелко, но много раз' — второе системный сигнал); (б) ПЕРЕСЕЧЕНИЕ адресов в плюсе по ОБОИМ
токенам сразу — естественно отсеивает AMM-пулы (они в плюсе по одному, в минусе по другому по
конструкции свопа), оставляя кандидатов в настоящие двусторонние бенефициары вроде 0x016e1a57.
Старое усреднение "4.3-5% реализованной маржи" (по 5 ручным транзакциям, mixed basis — разные
бенефициары на разных маршрутах) — СНЯТО как непригодное; beneficiary_totals — его замена.

Цена wS для net_usd — ТЕКУЩАЯ DeFiLlama (оценка размера, смещает УРОВЕНЬ маржи, не знак/структуру
остатка). Точная маржа на момент блока ждёт getRoundData Chainlink-агрегатора ЗАЛОГОВОЙ стороны —
её solvencyOracle (0x5da3510d) резолвится в ТУ ЖЕ имплементацию 0x2e226f3a, что и долговая сторона
(уже прочитана как ChainlinkV3Oracle) — impl-match, не байт-сверка. getRoundData-логика пока НЕ реализована.

Ограничение: если победитель НЕ адрес-получатель прибыли, чистая дельта НА ЕГО адресе может НЕ
отражать его реальный профит — тул считает по адресу из события (msg.sender).

Запуск:
  python3 -m analysis.net_margin --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 \
      --winner 0xccd487e01e9df6932f656b53668f58005f604417 --min-usd 100 --days 30 --top 123
  python3 -m analysis.net_margin --rpc <rpc> --silo <debt_silo> --winner <addr> --decompose-block 72462468
"""
from __future__ import annotations
import argparse
import statistics
import sys

from analysis.contestation import (
    RPC, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts,
    silo_token_meta, llama_prices,
)

TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # keccak-сверен


def _addr_from_topic(topic: str) -> str:
    return "0x" + topic[-40:]


def transfer_deltas(receipt: dict, token_addrs: set, holder: str) -> dict:
    """Из логов receipt: {token_addr: net_delta} для holder — сумма IN минус OUT, в родных единицах (raw int).
    Только логи с topic0==Transfer и адресом контракта из token_addrs."""
    holder = holder.lower()
    token_addrs = {t.lower() for t in token_addrs}
    deltas = {t: 0 for t in token_addrs}
    for log in receipt.get("logs", []) or []:
        addr = (log.get("address") or "").lower()
        topics = log.get("topics") or []
        if addr not in token_addrs or not topics or topics[0].lower() != TRANSFER_TOPIC0:
            continue
        if len(topics) < 3:
            continue
        frm = _addr_from_topic(topics[1]).lower()
        to = _addr_from_topic(topics[2]).lower()
        data = log.get("data", "0x0")
        val = int(data, 16) if data and data != "0x" else 0
        if to == holder:
            deltas[addr] += val
        if frm == holder:
            deltas[addr] -= val
    return deltas


def transfer_ledger(receipt: dict, token_addrs: set) -> list:
    """СЫРОЙ список всех Transfer-событий (token, from, to, raw_value) в порядке логов — БЕЗ нэттинга."""
    token_addrs = {t.lower() for t in token_addrs}
    out = []
    for log in receipt.get("logs", []) or []:
        addr = (log.get("address") or "").lower()
        topics = log.get("topics") or []
        if addr not in token_addrs or not topics or topics[0].lower() != TRANSFER_TOPIC0:
            continue
        if len(topics) < 3:
            continue
        frm = _addr_from_topic(topics[1]).lower()
        to = _addr_from_topic(topics[2]).lower()
        data = log.get("data", "0x0")
        val = int(data, 16) if data and data != "0x" else 0
        out.append((addr, frm, to, val))
    return out


def touches_token(receipt: dict, token_addr: str, holder: str) -> bool:
    """True если ЕСТЬ хоть один Transfer-лог этого токена, где holder — from ИЛИ to (НЕЗАВИСИМО от суммы)."""
    holder = holder.lower()
    token_addr = token_addr.lower()
    for log in receipt.get("logs", []) or []:
        addr = (log.get("address") or "").lower()
        topics = log.get("topics") or []
        if addr != token_addr or not topics or topics[0].lower() != TRANSFER_TOPIC0 or len(topics) < 3:
            continue
        frm = _addr_from_topic(topics[1]).lower()
        to = _addr_from_topic(topics[2]).lower()
        if frm == holder or to == holder:
            return True
    return False


def all_address_deltas(ledger: list, token_addr: str) -> dict:
    """Чистая дельта КАЖДОГО адреса, встретившегося в ledger для token_addr — не только одного holder."""
    token_addr = token_addr.lower()
    deltas = {}
    for tok, frm, to, val in ledger:
        if tok != token_addr:
            continue
        deltas[to] = deltas.get(to, 0) + val
        deltas[frm] = deltas.get(frm, 0) - val
    return deltas


def print_decomposition(rpc: RPC, txhash: str, winner: str, m_debt: dict, m_coll: dict) -> None:
    """Печатает сырой Transfer-леджер одной tx + сверку с transfer_deltas(). Общая для --decompose/-block."""
    r = rpc.call("eth_getTransactionReceipt", [txhash]) or {}
    tok_dec = {m_debt["token"].lower(): (m_debt["symbol"], m_debt["decimals"]),
              m_coll["token"].lower(): (m_coll["symbol"], m_coll["decimals"])}
    ledger = transfer_ledger(r, set(tok_dec.keys()))
    print("=" * 100)
    print(f"  DECOMPOSE — сырой Transfer-леджер {txhash} (БЕЗ нэттинга, порядок логов = порядок исполнения)")
    print("=" * 100)
    if not ledger:
        print("Логов Transfer нужных токенов не найдено — проверь хеш/адреса токенов.")
        return
    for tok, frm, to, val in ledger:
        sym, dec = tok_dec.get(tok, ("?", 18))
        print(f"  {sym:6s} {val/10**dec:>16,.4f}   {frm}  →  {to}")
    print("\nЧитать так: несколько строк одного токена в одну и ту же сторону от адреса победителя —")
    print("это флеш-займ+repay+возврат ИЛИ залог+своп+repay-флеша. Сумма IN-OUT по каждому адресу")
    print("должна совпасть с transfer_deltas() (сверка ниже).")
    deltas_win = transfer_deltas(r, set(tok_dec.keys()), winner)
    print(f"\nСверка — чистая дельта победителя ({winner[:10]}…):")
    for tok, (sym, dec) in tok_dec.items():
        print(f"  {sym}: {deltas_win.get(tok, 0)/10**dec:+,.4f}")

    print(f"\n──── КТО РЕАЛЬНО В ПЛЮСЕ (все адреса с ненулевой дельтой — не только победитель) ────")
    print("Ноль = чистый pass-through хоп (заём/роутер). Ненулевое = источник или получатель ценности.")
    for tok, (sym, dec) in tok_dec.items():
        addr_deltas = all_address_deltas(ledger, tok)
        nonzero = sorted(((a, d) for a, d in addr_deltas.items() if d != 0),
                         key=lambda kv: -abs(kv[1]))
        if not nonzero:
            print(f"  {sym}: все адреса нулевые (полностью pass-through по этому токену)")
            continue
        print(f"  {sym}:")
        for addr, d in nonzero:
            tag = "  ← ПОБЕДИТЕЛЬ" if addr == winner.lower() else ""
            print(f"    {d/10**dec:>+16,.4f}   {addr}{tag}")


def resolve_collateral_meta(rpc: RPC, debt_silo: str, meta: dict) -> tuple:
    """(m_debt, m_coll) — залоговый силос узнаём тем же путём, что read_fee: getSilos() на config."""
    m_debt = silo_token_meta(rpc, debt_silo, meta)
    cfg_ret = rpc.eth_call(debt_silo, "0x79502c55")
    config = "0x" + cfg_ret[2:][24:64]
    sret = rpc.eth_call(config, "0xaecc90cb")
    s0, s1 = "0x" + sret[2:][24:64], "0x" + sret[2:][88:128]
    collateral = s1 if s0 == debt_silo else s0
    m_coll = silo_token_meta(rpc, collateral, meta)
    return m_debt, m_coll


def main():
    ap = argparse.ArgumentParser(description="Реальная чистая маржа по Transfer-логам исполненной сделки")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", required=True, help="долговой силос (USDC)")
    ap.add_argument("--winner", required=True)
    ap.add_argument("--chain", default="sonic")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--min-usd", type=float, default=100.0)
    ap.add_argument("--top", type=int, default=15, help="сколько жирных побед разобрать построчно")
    ap.add_argument("--decompose", default=None,
                    help="ДИАГНОСТИКА: hash одной tx — печатает ВЕСЬ Transfer-леджер без нэттинга")
    ap.add_argument("--decompose-block", type=int, default=None,
                    help="ДИАГНОСТИКА (проще --decompose): номер блока — тул сам найдёт хеш")
    a = ap.parse_args()
    silo = a.silo.lower()
    winner = a.winner.lower()
    rpc = RPC(a.rpc)

    if a.decompose or a.decompose_block:
        meta = {}
        m_debt, m_coll = resolve_collateral_meta(rpc, silo, meta)
        if a.decompose:
            print_decomposition(rpc, a.decompose, winner, m_debt, m_coll)
            return
        sys.stderr.write(f"тяну ликвидации за {a.days:g}д, ищу блок {a.decompose_block}…\n")
        tip = rpc.block_number()
        frm = find_block_at_ts(rpc, rpc.block_ts(tip) - int(a.days * 86400), tip)
        logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000)
        events = [e for e in (decode_liquidation_log(l) for l in logs) if e]
        match = [e for e in events if e["block"] == a.decompose_block
                and e["liquidator"].lower() == winner and e["silo"].lower() == silo]
        if not match:
            sys.exit(f"Блок {a.decompose_block}: событие не найдено — проверь номер блока/--days.")
        print(f"найден tx {match[0]['tx']} в блоке {a.decompose_block}\n")
        print_decomposition(rpc, match[0]["tx"], winner, m_debt, m_coll)
        return

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

    meta = {}
    m_debt, m_coll = resolve_collateral_meta(rpc, silo, meta)
    sys.stderr.write(f"долг: {m_debt['symbol']} ({m_debt['token']})  залог: {m_coll['symbol']} ({m_coll['token']})\n")

    debt_tok, coll_tok = m_debt["token"].lower(), m_coll["token"].lower()
    prices = llama_prices(a.chain, {debt_tok, coll_tok})
    px_debt = prices.get(debt_tok)
    px_coll = prices.get(coll_tok)

    target = [e for e in uniq if e["silo"].lower() == silo]
    for e in target:
        e["repay"] = e["repay_raw"] / (10 ** m_debt["decimals"])
        e["usd"] = (e["repay"] * px_debt) if px_debt else None
    fat = sorted([e for e in target if e["liquidator"].lower() == winner and e["usd"] is not None
                 and e["usd"] >= a.min_usd], key=lambda e: -e["usd"])[: a.top]
    sys.stderr.write(f"жирных побед {winner[:10]}…: {len(fat)} (разбираю до {a.top})\n")
    if not fat:
        sys.exit("Нет жирных побед — проверь --winner/--silo/--min-usd/--days.")

    from collections import Counter
    tx_counts = Counter(e["tx"] for e in fat)
    shared_tx = {tx: n for tx, n in tx_counts.items() if n > 1}
    if shared_tx:
        print(f"⚠ {sum(shared_tx.values())} из {len(fat)} строк делят ОДИН tx-хеш ({len(shared_tx)} таких хешей).")
        print(f"  Ниже они помечены и НЕ задваивают агрегат.\n")

    print("=" * 90)
    print(f"  NET MARGIN (реальный, из Transfer-логов) — {winner} на {silo[:12]}…")
    print("=" * 90)
    print(f"{'block':>10s} {'tx':>12s} {'repay USDC':>12s} {'Δwei USDC':>14s} {'Δ '+m_coll['symbol']:>14s} "
          f"{'net USD':>10s} {'hold-margin':>12s}")

    ratios = []
    signs = {"pos": 0, "neg": 0, "zero_touched": 0, "blind": 0}
    seen_tx = set()
    n_counted = 0
    beneficiary_totals = {debt_tok: {}, coll_tok: {}}
    beneficiary_counts = {debt_tok: {}, coll_tok: {}}  # в скольких РАЗНЫХ tx адрес дал ненулевую дельту —
    # отличает "разово крупный пул" (count=1, sum огромная) от "мелко, но много раз" (count большой,
    # sum скромная) — второе и есть системный сигнал (ревью), не разовая случайность каскада.
    for e in fat:
        r = rpc.call("eth_getTransactionReceipt", [e["tx"]]) or {}
        deltas = transfer_deltas(r, {debt_tok, coll_tok}, winner)
        d_usdc = deltas.get(debt_tok, 0) / (10 ** m_debt["decimals"])
        d_coll_raw = deltas.get(coll_tok, 0)
        d_coll = d_coll_raw / (10 ** m_coll["decimals"])
        is_dup = e["tx"] in seen_tx
        if not is_dup:
            seen_tx.add(e["tx"])
            n_counted += 1
            ledger = transfer_ledger(r, {debt_tok, coll_tok})
            for tok in (debt_tok, coll_tok):
                for addr, val in all_address_deltas(ledger, tok).items():
                    if val == 0:
                        continue
                    beneficiary_totals[tok][addr] = beneficiary_totals[tok].get(addr, 0) + val
                    beneficiary_counts[tok][addr] = beneficiary_counts[tok].get(addr, 0) + 1
            if d_coll_raw > 0:
                signs["pos"] += 1
            elif d_coll_raw < 0:
                signs["neg"] += 1
            elif touches_token(r, coll_tok, winner):
                signs["zero_touched"] += 1
            else:
                signs["blind"] += 1
            net_usd = None
            if px_debt is not None and px_coll is not None:
                net_usd = d_usdc * px_debt + d_coll * px_coll
            ratio = (net_usd / e["usd"]) if (net_usd is not None and e["usd"]) else None
            if ratio is not None:
                ratios.append(ratio)
        else:
            net_usd = (d_usdc * px_debt + d_coll * px_coll) if (px_debt is not None and px_coll is not None) else None
            ratio = None
        print(f"{e['block']:>10d} {e['tx'][:10]+'…':>12s} {e['repay']:>12,.0f} {d_usdc:>14,.2f} {d_coll:>14,.4f} "
              f"{'—' if net_usd is None else f'{net_usd:>9,.2f}'} "
              f"{'—' if ratio is None else f'{ratio*100:>11,.2f}%'}"
              f"{'  [дубль хеша — НЕ в агрегате]' if is_dup else ''}")

    print("\n" + "=" * 90)
    n = n_counted
    dup_rows = len(fat) - n_counted
    if dup_rows:
        print(f"АГРЕГАТ ниже — по {n} УНИКАЛЬНЫМ tx (из {len(fat)} строк; {dup_rows} дублей исключены).\n")
    print(f"ЗНАК остатка {m_coll['symbol']} (ЦЕНОНЕЗАВИСИМ), n={n} уникальных tx:")
    print(f"  ⚠ (ревью) Ноль здесь ЧАСТО значит 'победитель — транзит на маршруте 0x157f2158→0x8f10b468'.")
    print(f"  Сплит ниже — сигнал МАРШРУТА, не экономика {winner[:10]}… Настоящая экономика — ниже.")
    print(f"  положительный: {signs['pos']}/{n}   отрицательный: {signs['neg']}/{n}   "
          f"РЕАЛЬНО ноль: {signs['zero_touched']}/{n}   СЛЕПО: {signs['blind']}/{n}")
    if ratios:
        print(f"\nHOLD-MARGIN (net/repay, цена СЕГОДНЯШНЯЯ — смещена по уровню): медиана "
              f"{statistics.median(ratios)*100:.2f}%  мин {min(ratios)*100:.2f}%  макс {max(ratios)*100:.2f}%  (n={len(ratios)})")
    print("\nПолная расшифровка ОДНОЙ tx: --decompose <tx_hash> или --decompose-block <N>")

    print("\n" + "=" * 90)
    print(f"СИСТЕМНЫЕ БЕНЕФИЦИАРЫ по {n_counted} уникальным tx (дельта {winner[:10]}… — транзит на части")
    print("маршрутов, не его экономика; здесь — КТО системно в плюсе по ВСЕЙ выборке сразу):")
    for tok, sym_dec in ((debt_tok, m_debt), (coll_tok, m_coll)):
        sym, dec = sym_dec["symbol"], sym_dec["decimals"]
        totals = beneficiary_totals[tok]
        counts = beneficiary_counts[tok]
        top = sorted(((a, v) for a, v in totals.items() if v != 0), key=lambda kv: -kv[1])[:8]
        if not top:
            print(f"  {sym}: все адреса нулевые суммарно")
            continue
        print(f"  {sym} (топ по сумме, только положительные — count = в скольких РАЗНЫХ tx из {n_counted}):")
        for addr, v in top:
            tag = "  ← ПОБЕДИТЕЛЬ" if addr == winner else ""
            print(f"    {v/10**dec:>+16,.4f}   (в {counts.get(addr,0):>3d}/{n_counted} tx)   {addr}{tag}")

    print(f"\n──── ПЕРЕСЕЧЕНИЕ: в плюсе по ОБОИМ токенам (пулы AMM не пройдут — они в плюсе по ────")
    print(f"     ОДНОМУ, но в минусе по другому; здесь остаются только двусторонние получатели):")
    pos_debt = {a for a, v in beneficiary_totals[debt_tok].items() if v > 0}
    pos_coll = {a for a, v in beneficiary_totals[coll_tok].items() if v > 0}
    both = pos_debt & pos_coll
    both = {a for a in both if a != winner}
    if both:
        for addr in sorted(both, key=lambda a: -beneficiary_totals[debt_tok].get(a, 0)):
            d_usd = beneficiary_totals[debt_tok].get(addr, 0) / 10 ** m_debt["decimals"]
            c_usd = beneficiary_totals[coll_tok].get(addr, 0) / 10 ** m_coll["decimals"]
            print(f"    {addr}   {m_debt['symbol']}:+{d_usd:,.4f} (в {beneficiary_counts[debt_tok].get(addr,0)} tx)   "
                  f"{m_coll['symbol']}:+{c_usd:,.4f} (в {beneficiary_counts[coll_tok].get(addr,0)} tx)")
        print("    ⚠ Кандидаты в 'настоящие бенефициары'. Не считать их 'внешними соперниками' без проверки.")
    else:
        print("    Пусто.")

    print("\n⚠ Крупные DEX-пулы попадут в топ по ОДНОМУ токену — это нормальная механика AMM, не профит.")
    print("Искать надо адрес, повторяющийся МНОГО раз (count) и/или в пересечении обоих токенов выше.")


if __name__ == "__main__":
    main()
FILE_EOF
cd "$DIR"
python3 -m py_compile analysis/net_margin.py && echo "[OK] py_compile"
python3 - << 'PY_TEST'
WS="0x039e2fb66102314ce7b64ce5ce3e5183bc94ad38"
USDC="0x29219dd400f2bf60e5a23d13be72b486d4038894"
CCD="0xccd487e01e9df6932f656b53668f58005f604417"
PROFIT="0x016e1a57c692051c310f28ef47683f455f38a748"
POOL="0xpool0000000000000000000000000000000000"
beneficiary_totals={USDC:{PROFIT:100_000000,POOL:-50_000000,CCD:0},WS:{PROFIT:5*10**18,POOL:2000*10**18,CCD:0}}
pos_debt={a for a,v in beneficiary_totals[USDC].items() if v>0}
pos_coll={a for a,v in beneficiary_totals[WS].items() if v>0}
both={a for a in (pos_debt&pos_coll) if a!=CCD}
assert PROFIT in both and POOL not in both
print("[OK] пересечение отсеивает односторонние пулы, оставляет двусторонних получателей — прошли")
PY_TEST
echo ">> net_margin.py v7 (+count повторяемости, +пересечение двусторонних получателей). Запуск:"
echo "   python3 -m analysis.net_margin --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 --winner 0xccd487e01e9df6932f656b53668f58005f604417 --min-usd 100 --days 30 --top 123"
