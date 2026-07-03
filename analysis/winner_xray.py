#!/usr/bin/env python3
"""winner_xray.py — вскрываем ЭДЖ инкумбента: он опережает нас за счёт ФИЗИКИ или за счёт ДЕНЕГ?
(read-only, stdlib, строится на проверенных примитивах contestation.py)

Вопрос, который закрывает этот скрипт (продолжение контестации):
  Контестация сказала «хвост заперт, 1-2 бота берут всё». Но ПОЧЕМУ они выигрывают?
    (A) co-location / голая FCFS-латентность  → стена ФИЗИЧЕСКАЯ (матчить близость к секвенсеру);
    (B) Timeboost express lane (Arbitrum)      → стена ЭКОНОМИЧЕСКАЯ (перебить в аукционе/сабслотах).
  Это РАЗНЫЕ стены и разные решения. Различаем их прямым сигналом.

ЧТО МЕРЯЕМ (дёшево, без ретроспективной реконструкции HF):
  1. Timeboost-флаг (Arbitrum). В receipt транзакции Arbitrum есть поле `timeboosted` (true/false).
     Если победы инкумбента ~все timeboosted → он в express lane → это АУКЦИОН, физика ни при чём.
     Если ~ноль → голый FCFS → co-location. (Sonic — не Arbitrum-чейн, Timeboost там НЕТ: N/A.)
  2. Позиция в блоке. transactionIndex победной ликвидации / число tx в блоке. Наверху (контроль
     ордеринга) или размазано. (Ремарка из исследования Timeboost: прибыльный MEV кучкуется в КОНЦЕ
     блока — ликвидация должна сесть ПОСЛЕ оракул-апдейта, так что абсолютная позиция — лишь прокси.)
  3. Реверты соперников (--contenders). В блоках победных ликвидаций ищем tx, адресованные контрактам
     ДРУГИХ известных ботов (из рейтинга контестации), которые зареверлись (status 0) → это проигравшие
     попытки. Их число и сожжённый газ = НАША вероятная себестоимость проигрыша, в цифрах.

Ограничение (честно): это характеристика эджа инкумбента и стоимости входа, а НЕ доказательство, что
мы не сможем. Финально проверяется только paper-режимом. Победитель = контракт-вызыватель liquidationCall.

Запуск (Timeboost-вопрос — ТОЛЬКО Arbitrum, нужен настоящий archive-RPC):
    python3 -m analysis.winner_xray --rpc https://arb-mainnet.g.alchemy.com/v2/KEY --chain arbitrum --days 30 --contenders
    python3 -m analysis.winner_xray --rpc https://rpc.soniclabs.com --chain sonic --days 7   # co-location-профиль
"""
from __future__ import annotations
import argparse
import sys
from collections import Counter, defaultdict

# строимся на протестированных примитивах контестации (файл уже на VPS)
from analysis.contestation import (RPC, RpcError, fetch_liquidation_logs, decode_liquidation_log,
                                   find_block_at_ts, winner_stats, llama_prices, LLAMA_CHAIN)


def _require_rpc(url: str):
    """Отсекаем неподставленный/битый RPC-URL ДО сетевых вызовов — с понятным сообщением."""
    bad = None
    if not url or url.strip() == "":
        bad = "пустой URL"
    elif not url.lower().startswith(("http://", "https://")):
        bad = "URL должен начинаться с http:// или https://"
    else:
        try:
            url.encode("ascii")
        except UnicodeEncodeError:
            bad = "в URL нестандартные символы (похоже, плейсхолдер вроде ТВОЙ_КЛЮЧ не заменён)"
        for ph in ("<", ">", "КЛЮЧ", "ТВОЙ", "your_key", "YOUR_KEY", "KEY_HERE"):
            if ph in url:
                bad = f"в URL остался плейсхолдер '{ph}' — впиши реальный archive-RPC"
                break
    if bad:
        sys.exit(f"НЕВЕРНЫЙ --rpc: {bad}\n"
                 f"Пример: --rpc https://arb-mainnet.g.alchemy.com/v2/<реальный_ключ>")


def hx(x) -> int:
    try:
        return int(x, 16)
    except (TypeError, ValueError):
        return 0


def position_bucket(idx: int, n: int) -> str:
    """Абсолютная позиция tx в блоке → корзина. n = число tx в блоке."""
    if n <= 1:
        return "solo"          # одна tx в блоке — ордеринг не оспаривался
    frac = idx / (n - 1)       # 0.0 = самый верх, 1.0 = самый низ
    if frac <= 0.10:
        return "top"
    if frac <= 0.50:
        return "upper"
    if frac < 0.90:
        return "lower"
    return "end"


def get_receipt(rpc: RPC, txhash: str) -> dict:
    r = rpc.call("eth_getTransactionReceipt", [txhash]) or {}
    return {
        "index": hx(r.get("transactionIndex", "0x0")),
        "status": hx(r.get("status", "0x1")),
        "gas_used": hx(r.get("gasUsed", "0x0")),
        "eff_price": hx(r.get("effectiveGasPrice", "0x0")),
        # Arbitrum: поле может отсутствовать у старых нод/не-arb чейнов → None
        "timeboosted": r.get("timeboosted", None),
        "to": (r.get("to") or "").lower(),
        "from": (r.get("from") or "").lower(),
    }


def get_block_full(rpc: RPC, num: int, cache: dict) -> dict:
    if num in cache:
        return cache[num]
    b = rpc.call("eth_getBlockByNumber", [hex(num), True]) or {}
    cache[num] = b
    return b


def main():
    ap = argparse.ArgumentParser(description="Silo V2 winner x-ray: Timeboost vs co-location")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--chain", default="arbitrum", choices=list(LLAMA_CHAIN.keys()))
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--sample", type=int, default=150, help="сколько последних побед вскрывать (лимит RPC)")
    ap.add_argument("--top", type=int, default=6, help="сколько ботов показать в разбивке")
    ap.add_argument("--contenders", action="store_true", help="искать реверты соперников (дороже по RPC)")
    ap.add_argument("--chunk", type=int, default=50_000)
    a = ap.parse_args()

    _require_rpc(a.rpc)
    rpc = RPC(a.rpc)

    tip = rpc.block_number()
    now_ts = rpc.block_ts(tip)
    target = now_ts - int(a.days * 86400)
    sys.stderr.write(f"tip={tip} → бинпоиск блока {a.days:g}д назад…\n")
    from_block = find_block_at_ts(rpc, target, tip)
    sys.stderr.write(f"окно: блоки {from_block}..{tip}\n")

    logs = fetch_liquidation_logs(rpc, from_block, tip, chunk=a.chunk)
    events = [e for e in (decode_liquidation_log(l) for l in logs) if e]
    seen, uniq = set(), []
    for e in events:
        k = (e["tx"], e["log_index"])
        if k not in seen:
            seen.add(k); uniq.append(e)
    events = uniq
    if not events:
        sys.exit(f"За {a.days:g}д на {a.chain} ликвидаций не найдено — нечего вскрывать "
                 f"(проверь archive-RPC / расширь --days).")

    ws = winner_stats([e["liquidator"] for e in events])
    ranked = ws["ranked"]
    incumbent = ranked[0][0]
    rival_contracts = {addr for addr, _ in ranked[:max(a.top, 4)]}  # набор контрактов известных ботов

    # выборка: последние N побед (свежие = ближе к текущей конфигурации гонки)
    events.sort(key=lambda e: (e["block"], e["log_index"]), reverse=True)
    sample = events[:a.sample]
    sys.stderr.write(f"вскрываю {len(sample)} последних побед из {len(events)} "
                     f"(инкумбент {incumbent[:10]}… = {ranked[0][1]} побед)\n")

    per = defaultdict(lambda: {"n": 0, "tb_true": 0, "tb_false": 0, "tb_none": 0,
                               "pos": Counter(), "gas_native": 0.0})
    block_cache = {}
    contender_revert = Counter()   # rival_addr -> кол-во зареверченных попыток
    contender_gas = defaultdict(float)
    contender_blocks_scanned = 0

    for i, e in enumerate(sample, 1):
        sys.stderr.write(f"\r  receipt {i}/{len(sample)}   "); sys.stderr.flush()
        rc = get_receipt(rpc, e["tx"])
        blk = get_block_full(rpc, e["block"], block_cache)
        ntx = len(blk.get("transactions") or []) or 1
        w = per[e["liquidator"]]
        w["n"] += 1
        if rc["timeboosted"] is True:
            w["tb_true"] += 1
        elif rc["timeboosted"] is False:
            w["tb_false"] += 1
        else:
            w["tb_none"] += 1
        w["pos"][position_bucket(rc["index"], ntx)] += 1
        w["gas_native"] += rc["gas_used"] * rc["eff_price"] / 1e18

        if a.contenders:
            contender_blocks_scanned += 1
            txs = blk.get("transactions") or []
            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                to = (tx.get("to") or "").lower()
                if to in rival_contracts and tx.get("hash", "").lower() != e["tx"].lower():
                    crc = get_receipt(rpc, tx["hash"])
                    if crc["status"] == 0:  # соперник попытался и зареверлся
                        contender_revert[to] += 1
                        contender_gas[to] += crc["gas_used"] * crc["eff_price"] / 1e18

    sys.stderr.write("\n")

    # опционально: цена газ-токена в USD (WETH на arb, wS на sonic) — не критично
    gas_sym = {"arbitrum": "ETH", "ethereum": "ETH", "base": "ETH", "optimism": "ETH", "sonic": "S"}[a.chain]

    # ---------- отчёт ----------
    print("\n" + "=" * 72)
    print(f"  WINNER X-RAY — Silo V2 — {a.chain.upper()} — окно {a.days:g}д — выборка {len(sample)}")
    print("=" * 72)

    tb_field_seen = any((w["tb_true"] + w["tb_false"]) > 0 for w in per.values())
    if a.chain == "arbitrum" and not tb_field_seen:
        print("⚠  RPC не отдаёт поле `timeboosted` в receipt (старая нода?). Возьми ноду Nitro посвежее")
        print("   (Alchemy/QuickNode/dRPC), иначе Timeboost-вопрос не разрешить.")
    if a.chain != "arbitrum":
        print(f"ℹ  {a.chain} — не Arbitrum-чейн: Timeboost отсутствует, гонка = чистый FCFS/co-location.")

    print("\n──── ЭДЖ ПО БОТАМ (свежая выборка) ────")
    order = sorted(per.items(), key=lambda kv: kv[1]["n"], reverse=True)[:a.top]
    for addr, w in order:
        n = w["n"]
        tb = f"{w['tb_true']}/{n} timeboosted" if tb_field_seen else "TB:n/a"
        tb_pct = f"{w['tb_true']/n*100:.0f}%" if (tb_field_seen and n) else "—"
        posdist = ", ".join(f"{k}:{v}" for k, v in w["pos"].most_common())
        gas_avg = w["gas_native"] / n if n else 0
        tag = " ◀ ИНКУМБЕНТ" if addr == incumbent else ""
        print(f"  {addr[:12]}…  n={n:3d}  TB={tb_pct:>4} ({tb})  газ~{gas_avg:.6f}{gas_sym}")
        print(f"       позиция в блоке: {posdist}{tag}")

    if a.contenders:
        print("\n──── РЕВЕРТЫ СОПЕРНИКОВ (наша вероятная себестоимость проигрыша) ────")
        if contender_revert:
            tot_gas = sum(contender_gas.values())
            tot_n = sum(contender_revert.values())
            print(f"В {contender_blocks_scanned} блоках побед найдено {tot_n} зареверченных попыток "
                  f"известных ботов, сожжено ~{tot_gas:.6f} {gas_sym}")
            for addr, cnt in contender_revert.most_common():
                print(f"   {addr[:12]}…  реверты: {cnt}  газ~{contender_gas[addr]:.6f} {gas_sym}")
            print("   (это боты, которые проиграли ту же ликвидацию и заплатили газ — наш пол издержек)")
        else:
            print("Зареверченных попыток известных ботов в блоках побед не найдено.")
            print("Либо соперники не спамят (сабмитят только уверенные), либо реверты вне этих блоков.")

    # ---------- вердикт ----------
    print("\n" + "=" * 72)
    print("  ЧИТАЕМ ВЕРДИКТ: ФИЗИКА или ДЕНЬГИ?")
    print("=" * 72)
    inc = per.get(incumbent)
    if a.chain == "arbitrum" and tb_field_seen and inc and inc["n"]:
        tb_share = inc["tb_true"] / inc["n"]
        if tb_share >= 0.6:
            print(f"💰 ДЕНЬГИ: инкумбент выигрывает через Timeboost express lane ({tb_share*100:.0f}% побед timeboosted).")
            print("   Физика — НЕ стена. Стена экономическая: чтобы контестить, надо перебивать в")
            print("   Timeboost-аукционе (или брать сабслоты у Gattaca/Kairos). Дальше считаем: сколько")
            print("   MEV в целевом силосе за раунд vs цена express-lane бида. Это уже наша поляна.")
        elif tb_share <= 0.15:
            print(f"⚙  ФИЗИКА: инкумбент почти НЕ использует Timeboost ({tb_share*100:.0f}%) → голый FCFS/co-location.")
            print("   Стена физическая: он ближе к секвенсеру. Контест = матчить латентность (свой узел")
            print("   рядом с секвенсером Arbitrum) + backrun по оракул-апдейту. Дорого, но проверяемо.")
        else:
            print(f"🟡 СМЕШАННО: инкумбент timeboosted на {tb_share*100:.0f}% — часть побед через lane, часть через скорость.")
            print("   Значит и аукцион, и латентность в игре. Смотрим позицию в блоке и реверты соперников выше.")
    elif a.chain != "arbitrum":
        # Sonic и пр.: только позиция + реверты как прокси «насколько заперт ордеринг»
        top_pos = inc["pos"].most_common(1)[0][0] if inc and inc["pos"] else "?"
        print(f"⚙  {a.chain}: Timeboost нет — это чистая FCFS-латентность. Инкумбент садится преимущественно")
        print(f"   в корзину '{top_pos}'. Если 'top' — он жёстко контролит ранний ордеринг (co-location);")
        print("   если 'end' — MEV кучкуется в конце блока и позиция менее решающа (есть теоретический зазор).")
        print("   Прямого 'аукционного' рычага здесь нет — контест только матчингом латентности к секвенсеру Sonic.")
    else:
        print("Не хватает данных для вердикта (нет поля timeboosted). См. предупреждение выше про ноду.")

    print("\nСледующий разветвитель:")
    print("  💰 если ДЕНЬГИ → считаем экономику Timeboost-бида в топ-силосе, потом форк LiquidationHelper.")
    print("  ⚙  если ФИЗИКА → честно оцениваем стоимость co-location vs приз; часто вывод «носитель — Morpho/Base».")


if __name__ == "__main__":
    main()
