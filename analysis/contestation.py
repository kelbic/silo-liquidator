#!/usr/bin/env python3
"""contestation.py — есть ли у НАС шанс против других ботов-ликвидаторов на Silo V2? (read-only, stdlib)

Порт метода из kelbic/liquidator (Morpho/Base) на Silo V2. Источник — НЕ сабграф (нужен ключ The Graph
и он может недоиндексировать), а сырые логи события LiquidationCall через eth_getLogs по topic0. Работает
с любого RPC (Sonic по умолчанию), ключей не требует, ноль капитала под риском.

Событие (сигнатура СТАБИЛЬНА от релиза 0.19.0 эпохи запуска на Sonic, янв-2025, до 4.16.0+):
  LiquidationCall(address indexed liquidator, address indexed silo, address indexed borrower,
                  uint256 repayDebtAssets, uint256 withdrawCollateral, bool receiveSToken)
  topic0 = 0x3a84f64446e8eada995aa9da2ddbfcd9b5d5d650503b19f024096d04c05ef2a9
  ПОБЕДИТЕЛЬ = liquidator (topic1) — это контракт-вызыватель liquidationCall. У Silo TOKENS_RECEIVER
  в LiquidationHelper immutable → официальный shared-инстанс шлёт профит одному адресу, поэтому внешние
  searcher'ы деплоят СВОЙ инстанс/контракт. Значит РАЗНЫЕ liquidator-адреса ≈ разные боты. Кластеризуем
  по liquidator, как в Morpho-версии.

ЧТО МЕРЯЕМ (контестабельность хвоста), и чего НЕ мерим (как и в Morpho-версии):
  Мы НЕ можем ретроспективно восстановить нашу скорость реакции (нужна историческая реконструкция HF).
  Но можем измерить, заперт ли хвост одним быстрым ботом или открыт:
    1. Разнообразие победителей — сколько РАЗНЫХ ликвидаторов выигрывают и доля топ-2. Длинный хвост
       СЛУЧАЙНЫХ победителей (по 1-2 победы) — дымящийся ствол: эти ликвидации НЕ снайпил мгновенно
       доминирующий бот → недоминирующий актор (мы) может их забирать.
    2. Кластеризация каскадов — как часто несколько ликвидаций падают в ОДИН блок. Sonic ~суб-секунда,
       поэтому единица каскада — blockNumber, НЕ timestamp. Даже быстрый конкурент берёт лишь столько-то
       за блок; остальное переливается.
    3. Монополизация всплеска — в мульти-ликвидационных блоках всё забирает ОДИН бот или РАЗМАЗАНО по
       нескольким? Размазано == есть место для нас в каскадах (где реальные деньги).

Размер ликвидации: repayDebtAssets в единицах ДОЛГОВОГО токена силоса (topic2 = долговой силос, т.к.
Silo зовёт ISilo(debtConfig.silo).repay). USD-слой опционален (DeFiLlama coins), при недоступности —
отчёт в родных единицах токена + группировка по силосам. Фокус на КРУПНЫХ ликвидациях (>= порога): там
выживает реальный net после слиппеджа/газа/flashloan-fee; пыль нам не интересна (мы её пропускаем).

Запуск (на VPS):
    python3 -m analysis.contestation --rpc https://rpc.soniclabs.com --days 30
    python3 -m analysis.contestation --rpc <arbitrum_rpc> --chain arbitrum --days 30 --min-usd 50
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from collections import Counter, defaultdict

TOPIC0_LIQUIDATION_CALL = "0x3a84f64446e8eada995aa9da2ddbfcd9b5d5d650503b19f024096d04c05ef2a9"

SEL_DECIMALS = "0x313ce567"
SEL_SYMBOL = "0x95d89b41"
SEL_SILO_ASSET = "0x38d52e0f"
SEL_SILO_TOKEN = "0xfc0c546a"

LLAMA_CHAIN = {"sonic": "sonic", "arbitrum": "arbitrum", "ethereum": "ethereum", "base": "base", "optimism": "optimism"}


class RpcError(RuntimeError):
    """JSON-RPC error от узла (детерминированный отказ — ретраи бессмысленны)."""


class RPC:
    def __init__(self, url: str, timeout: float = 30.0):
        self.url = url
        self.timeout = timeout
        self._id = 0

    def call(self, method: str, params: list):
        self._id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=payload,
                                     headers={"Content-Type": "application/json", "User-Agent": "silo-contestation/1.0"})
        last = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    obj = json.loads(r.read())
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "ignore")[:200]
                except Exception:  # noqa
                    pass
                if e.code in (401, 403):
                    raise RpcError(f"{method}: HTTP {e.code} — RPC отверг ключ/доступ "
                                   f"(проверь, что ключ реальный и сеть та самая). {body}")
                if e.code in (400, 413, 414):
                    raise RpcError(f"{method}: HTTP {e.code} (вероятно, слишком большой диапазон/ответ). {body}")
                last = e
                time.sleep(0.7 * (attempt + 1))
                continue
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                last = e
                time.sleep(0.7 * (attempt + 1))
                continue
            err = obj.get("error")
            if err:
                raise RpcError(f"{method}: {err}")
            return obj.get("result")
        raise RuntimeError(f"RPC не отвечает на {method} после ретраев: {last}")

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_ts(self, num: int) -> int:
        b = self.call("eth_getBlockByNumber", [hex(num), False])
        if not b:
            raise RuntimeError(f"нет блока {num}")
        return int(b["timestamp"], 16)

    def eth_call(self, to: str, data: str):
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])


def topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def decode_liquidation_log(log: dict):
    """topics: [topic0, liquidator, silo, borrower]; data: repayDebtAssets|withdrawCollateral|receiveSToken.
    None — если форма лога неожиданная (защита от чужого события с совпавшим topic0)."""
    topics = log.get("topics") or []
    if len(topics) < 4:
        return None
    data = log["data"][2:] if log.get("data", "0x").startswith("0x") else log.get("data", "")
    repay = int(data[0:64], 16) if len(data) >= 64 else 0
    withdraw = int(data[64:128], 16) if len(data) >= 128 else 0
    receive_stoken = (int(data[128:192], 16) != 0) if len(data) >= 192 else False
    return {
        "block": int(log["blockNumber"], 16),
        "tx": log["transactionHash"],
        "log_index": int(log.get("logIndex", "0x0"), 16),
        "liquidator": topic_to_addr(topics[1]),
        "silo": topic_to_addr(topics[2]),
        "borrower": topic_to_addr(topics[3]),
        "repay_raw": repay,
        "withdraw_raw": withdraw,
        "receive_stoken": receive_stoken,
    }


def _hex_to_int(x) -> int:
    try:
        return int(x, 16)
    except (TypeError, ValueError):
        return 0


def decode_string_ret(ret: str) -> str:
    """Декод ABI-строки или bytes32-строки из возврата symbol()."""
    if not ret or ret == "0x":
        return "?"
    h = ret[2:]
    if len(h) >= 128:
        try:
            length = int(h[64:128], 16)
            if 0 < length <= 64:
                raw = bytes.fromhex(h[128:128 + length * 2])
                s = raw.decode("utf-8", "ignore").strip("\x00").strip()
                if s:
                    return s
        except (ValueError, IndexError):
            pass
    try:
        raw = bytes.fromhex(h[:64])
        s = raw.decode("utf-8", "ignore").strip("\x00").strip()
        return s or "?"
    except (ValueError, IndexError):
        return "?"


def find_block_at_ts(rpc: RPC, target_ts: int, hi_block: int) -> int:
    """Наименьший блок с timestamp >= target_ts. Чистый бинпоиск 0..tip (~26 вызовов)."""
    lo, hi = 0, hi_block
    while lo < hi:
        mid = (lo + hi) // 2
        if rpc.block_ts(mid) < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def fetch_liquidation_logs(rpc: RPC, from_block: int, to_block: int, chunk: int = 10_000,
                           topic0: str = TOPIC0_LIQUIDATION_CALL) -> list:
    """eth_getLogs по topic0. Чанкуем и адаптивно дробим при лимитах RPC.
    topic0 опционален (дефолт — LiquidationCall) — переиспользуется для Borrow и т.п."""
    out = []
    start = from_block
    step = chunk
    while start <= to_block:
        end = min(start + step - 1, to_block)
        try:
            logs = rpc.call("eth_getLogs", [{
                "fromBlock": hex(start), "toBlock": hex(end), "topics": [topic0],
            }])
        except RpcError:
            if step > 500:
                step = max(500, step // 2)
                sys.stderr.write(f"\n  RPC отверг диапазон, уменьшаю шаг → {step} блоков\n")
                continue
            raise
        out.extend(logs or [])
        sys.stderr.write(f"\r  логи: блоки {start}-{end}  найдено {len(out)}   ")
        sys.stderr.flush()
        start = end + 1
    sys.stderr.write("\n")
    return out


def silo_token_meta(rpc: RPC, silo: str, cache: dict) -> dict:
    """Долговой токен силоса + symbol/decimals. Кэш по адресу силоса."""
    if silo in cache:
        return cache[silo]
    meta = {"token": None, "symbol": "?", "decimals": 18}
    token = None
    for sel in (SEL_SILO_ASSET, SEL_SILO_TOKEN):
        try:
            ret = rpc.eth_call(silo, sel)
            if ret and ret != "0x" and int(ret, 16) != 0:
                token = "0x" + ret[-40:].lower()
                break
        except RuntimeError:
            continue
    if token:
        meta["token"] = token
        try:
            d = rpc.eth_call(token, SEL_DECIMALS)
            meta["decimals"] = _hex_to_int(d) if d and d != "0x" else 18
            meta["symbol"] = decode_string_ret(rpc.eth_call(token, SEL_SYMBOL))
        except RuntimeError:
            pass
    cache[silo] = meta
    return meta


def llama_prices(chain: str, tokens: set) -> dict:
    """Опционально: цены DeFiLlama coins."""
    ch = LLAMA_CHAIN.get(chain)
    if not ch or not tokens:
        return {}
    ids = ",".join(f"{ch}:{t}" for t in tokens)
    url = f"https://coins.llama.fi/prices/current/{ids}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "silo-contestation/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            obj = json.loads(r.read())
        out = {}
        for k, v in (obj.get("coins") or {}).items():
            addr = k.split(":", 1)[1].lower() if ":" in k else k.lower()
            if isinstance(v, dict) and "price" in v:
                out[addr] = float(v["price"])
        return out
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return {}


def winner_stats(winners: list, top_n: int = 2) -> dict:
    c = Counter(w for w in winners if w)
    total = sum(c.values())
    ranked = c.most_common()
    top = sum(n for _, n in ranked[:top_n])
    occasional = sum(n for _, n in c.items() if n <= 2)
    return {"total": total, "distinct": len(c),
            "top_n_share": top / total if total else 0.0,
            "occasional_share": occasional / total if total else 0.0,
            "ranked": ranked}


def cluster_by_block(items: list) -> dict:
    """items: [(block, winner)]. Группируем по блоку."""
    by_block = defaultdict(list)
    for blk, w in items:
        by_block[blk].append(w)
    sizes = Counter(len(v) for v in by_block.values())
    cascades = {b: ws for b, ws in by_block.items() if len(ws) > 1}
    mono, spread = 0, 0
    liqs_in_cascades = 0
    for ws in cascades.values():
        liqs_in_cascades += len(ws)
        if len(set(ws)) == 1:
            mono += 1
        else:
            spread += 1
    return {"blocks_with_liq": len(by_block), "size_hist": dict(sorted(sizes.items())),
            "cascade_blocks": len(cascades), "cascade_mono": mono, "cascade_spread": spread,
            "liqs_in_cascades": liqs_in_cascades}


def main():
    ap = argparse.ArgumentParser(description="Silo V2 contestation (read-only, raw logs)")
    ap.add_argument("--rpc", required=True, help="URL RPC (Sonic: https://rpc.soniclabs.com)")
    ap.add_argument("--chain", default="sonic", choices=list(LLAMA_CHAIN.keys()))
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--min-usd", type=float, default=0.0, help="порог 'крупной' ликвидации в USD")
    ap.add_argument("--chunk", type=int, default=50_000, help="стартовый размер чанка eth_getLogs")
    ap.add_argument("--no-usd", action="store_true", help="не ходить в DeFiLlama")
    ap.add_argument("--top", type=int, default=15, help="сколько победителей показать")
    a = ap.parse_args()

    rpc = RPC(a.rpc)
    tip = rpc.block_number()
    now_ts = rpc.block_ts(tip)
    target = now_ts - int(a.days * 86400)
    sys.stderr.write(f"tip={tip} ts={now_ts} → бинпоиск блока {a.days:g}д назад…\n")
    from_block = find_block_at_ts(rpc, target, tip)
    span_blocks = tip - from_block
    eff_block_time = (now_ts - rpc.block_ts(from_block)) / span_blocks if span_blocks else 0.0
    sys.stderr.write(f"окно: блоки {from_block}..{tip} ({span_blocks} шт, ~{eff_block_time:.3f}s/блок)\n")

    logs = fetch_liquidation_logs(rpc, from_block, tip, chunk=a.chunk)
    if not logs:
        print("\n=== РЕЗУЛЬТАТ ===")
        print(f"За {a.days:g}д на {a.chain} НЕ найдено ни одной ликвидации Silo V2 (LiquidationCall).")
        print("Возможные причины: узкое окно / низкая волатильность / мало заёмщиков у порога.")
        print("Хвоста ликвидаций может просто не быть в этом окне — расширь --days или проверь другой RPC.")
        return

    events = [e for e in (decode_liquidation_log(l) for l in logs) if e]
    seen, uniq = set(), []
    for e in events:
        k = (e["tx"], e["log_index"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    events = uniq

    meta_cache = {}
    silos = {e["silo"] for e in events}
    sys.stderr.write(f"силосов с ликвидациями: {len(silos)} — тяну token/symbol/decimals…\n")
    for s in silos:
        silo_token_meta(rpc, s, meta_cache)

    prices = {}
    if not a.no_usd:
        toks = {m["token"] for m in meta_cache.values() if m.get("token")}
        prices = llama_prices(a.chain, toks)
        sys.stderr.write(f"цены DeFiLlama: получено {len(prices)}/{len(toks)} токенов\n")

    for e in events:
        m = meta_cache.get(e["silo"], {"symbol": "?", "decimals": 18, "token": None})
        e["symbol"] = m["symbol"]
        e["repay"] = e["repay_raw"] / (10 ** m["decimals"])
        px = prices.get((m.get("token") or "").lower())
        e["usd"] = (e["repay"] * px) if px else None

    have_usd = any(e["usd"] is not None for e in events)
    unpriced = sum(1 for e in events if e["usd"] is None)
    if a.min_usd > 0 and have_usd:
        big = [e for e in events if e["usd"] is None or e["usd"] >= a.min_usd]
    else:
        big = events
        if a.min_usd > 0 and not have_usd:
            sys.stderr.write("!! порог --min-usd задан, но цен нет — показываю ВСЕ ликвидации в родных единицах\n")

    print("\n" + "=" * 72)
    print(f"  КОНТЕСТАЦИЯ Silo V2 — {a.chain.upper()} — окно {a.days:g}д")
    print("=" * 72)
    print(f"Всего ликвидаций (LiquidationCall): {len(events)}")
    if a.min_usd > 0 and have_usd:
        extra = f"  (в т.ч. {unpriced} без цены — ВКЛЮЧЕНЫ, это экзотика хвоста)" if unpriced else ""
        print(f"Из них 'крупных' (>= ${a.min_usd:g}): {len(big)}{extra}")
    if have_usd:
        tot_usd = sum(e["usd"] or 0 for e in big)
        print(f"Суммарный repaid по выборке: ${tot_usd:,.0f}")
        print("(USD по ТЕКУЩИМ ценам DeFiLlama — для окна в недели это аппроксимация размера, не PnL)")

    ws = winner_stats([e["liquidator"] for e in big])
    print("\n──── ПОБЕДИТЕЛИ (кто забирает ликвидации) ────")
    print(f"Разных ликвидаторов: {ws['distinct']}   |   всего побед: {ws['total']}")
    print(f"Доля топ-2 адресов:            {ws['top_n_share']*100:5.1f}%   (высокая → хвост заперт)")
    print(f"Доля 'случайных' (<=2 побед):  {ws['occasional_share']*100:5.1f}%   (высокая → есть щель для нас)")
    print(f"\n  Рейтинг топ-{a.top}:")
    for i, (addr, n) in enumerate(ws["ranked"][:a.top], 1):
        share = n / ws["total"] * 100 if ws["total"] else 0
        print(f"   {i:2d}. {addr}  {n:4d}  ({share:4.1f}%)")

    cl = cluster_by_block([(e["block"], e["liquidator"]) for e in big])
    print("\n──── КАСКАДЫ (мульти-ликвидации в одном блоке) ────")
    print(f"Блоков с ликвидациями: {cl['blocks_with_liq']}")
    print(f"Распределение размера блока (ликвидаций→блоков): {cl['size_hist']}")
    print(f"Каскадных блоков (>1 ликв): {cl['cascade_blocks']}  "
          f"(в них {cl['liqs_in_cascades']} ликвидаций)")
    if cl["cascade_blocks"]:
        mono_share = cl["cascade_mono"] / cl["cascade_blocks"] * 100
        print(f"  из них монополизированы 1 ботом: {cl['cascade_mono']} ({mono_share:.0f}%)  "
              f"| размазаны по >=2: {cl['cascade_spread']}")
        print("  (размазанные каскады == место для нас там, где реальные деньги)")

    by_silo = defaultdict(lambda: {"n": 0, "usd": 0.0, "sym": "?", "winners": Counter()})
    for e in big:
        s = by_silo[e["silo"]]
        s["n"] += 1
        s["usd"] += e["usd"] or 0
        s["sym"] = e["symbol"]
        s["winners"][e["liquidator"]] += 1
    print("\n──── ПО СИЛОСАМ (топ по числу ликвидаций) ────")
    ranked_silos = sorted(by_silo.items(), key=lambda kv: kv[1]["n"], reverse=True)[:a.top]
    for silo, s in ranked_silos:
        top_w, top_n = s["winners"].most_common(1)[0]
        conc = top_n / s["n"] * 100
        usd_str = f"  ${s['usd']:,.0f}" if have_usd else ""
        print(f"   {silo}  debt={s['sym']:8s}  ликв={s['n']:3d}{usd_str}  "
              f"топ-бот {conc:3.0f}% ({top_w[:10]}…)")

    print("\n" + "=" * 72)
    print("  ЧИТАЕМ ВЕРДИКТ")
    print("=" * 72)
    top2 = ws["top_n_share"]
    occ = ws["occasional_share"]
    if ws["total"] < 20:
        print("⚠  Мало данных (<20 ликвидаций) — статистика шумная. Расширь окно --days.")
    if top2 >= 0.8 and occ < 0.2:
        print("🔒 ХВОСТ ЗАПЕРТ: 1-2 бота забирают почти всё, случайных победителей мало.")
        print("   Войти можно только чистой скоростью против доминирующего бота — тяжело.")
    elif occ >= 0.4 or top2 < 0.5:
        print("🟢 ХВОСТ ОТКРЫТ: длинный хвост случайных победителей / низкая концентрация.")
        print("   Эти ликвидации НЕ снайпились мгновенно — недоминирующий актор (мы) может их брать.")
    else:
        print("🟡 СМЕШАННО: есть доминирующие боты, но и заметная доля случайных побед.")
        print("   Реалистично забирать каскады и менее популярные силосы, не лобовую гонку за топ-1.")
    if cl["cascade_blocks"] and cl["cascade_spread"] >= cl["cascade_mono"]:
        print("🟢 Каскады размазаны по нескольким ботам → в всплесках волатильности есть наша доля.")
    print("\nОграничение метода: это контестабельность ХВОСТА, а не наша вероятность победы.")
    print("победы. Историческую скорость нашей реакции ретроспективно не восстановить — она проверяется")
    print("только paper-режимом в проде. Победитель = контракт-вызыватель; разные адреса ≈ разные боты.")


if __name__ == "__main__":
    main()
