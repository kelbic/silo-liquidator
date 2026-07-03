#!/usr/bin/env python3
"""oracle_check.py v2 — АДРЕС solvencyOracle + резолв minimal-proxy + ССЫЛКА на исходник. (read-only)

ВАЖНО (по ревью): авто-ярлык push/pull УБРАН. Он был систематически смещён и мог инвертировать решение:
все оракулы Silo реализуют ISiloOracle, поэтому quote()/quoteToken()/beforeQuote() держат И Chainlink-обёртка,
И TWAP-адаптер — набор ВНЕШНИХ селекторов push от pull НЕ отличает. push-vs-pull живёт во ВНУТРЕННЕМ источнике
адаптера (immutable-адрес Chainlink-агрегатора внутри → push; чтение DEX-пула → pull), и виден ТОЛЬКО чтением
исходника адаптера на Sonicscan. Байткод-эвристика тут не работает — тем более что оракулы Silo часто
minimal-proxy (45 байт), где селекторов имплементации нет вовсе.

Что тул делает ЧЕСТНО:
  • config() -> getConfig(silo) -> solvencyOracle (поле #7 ConfigData) — верно и полезно;
  • если оракул = EIP-1167 minimal-proxy (45 байт) → достаёт адрес ИМПЛЕМЕНТАЦИИ из байткода;
  • печатает Sonicscan-ссылки на оракул И на имплементацию — классифицировать push/pull ГЛАЗАМИ по исходнику;
  • auto-target силосов по объёму (без хардкода адресов).
  НЕ выводит вердикт push/pull — только адреса и куда смотреть.

Запуск:
  python3 -m analysis.oracle_check --rpc https://rpc.soniclabs.com --days 30
  python3 -m analysis.oracle_check --rpc <rpc> --silo 0x322e1d5384...
"""
from __future__ import annotations
import argparse
import sys

from analysis.contestation import (
    RPC, RpcError, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts,
    silo_token_meta, llama_prices,
)

SEL_CONFIG    = "0x79502c55"  # ISilo.config() -> ISiloConfig
SEL_GETCONFIG = "0xe48a5f7b"  # ISiloConfig.getConfig(address) -> ConfigData
IDX_SOLVENCY_ORACLE = 7       # поле #7 в ConfigData

SONICSCAN = "https://sonicscan.org/address/{}#code"
EIP1167_PREFIX = "363d3d373d3d3d363d73"
EIP1167_SUFFIX = "5af43d82803e903d91602b57fd5bf3"

MARKER_SELECTORS = {
    "c71ed1e6": ("PUSH", "ChainlinkV3Oracle", "getAggregatorPrice(bool)"),
    "9a6fc8f5": ("PUSH", "Chainlink-agg-like", "getRoundData(uint80)"),
    "869dd80c": ("PUSH", "SupraSValueOracle", "getPriceForToken(bytes32,uint256,uint256)"),
    "9af0a551": ("PULL", "UniswapV3Oracle", "oldestTimestamp()"),
    "0902f1ac": ("PULL", "DEX/reserves", "getReserves()"),
}


def top_silos_by_volume(rpc: RPC, chain: str, days: float, n: int) -> list:
    from collections import defaultdict
    tip = rpc.block_number()
    frm = find_block_at_ts(rpc, rpc.block_ts(tip) - int(days * 86400), tip)
    logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000)
    events = [e for e in (decode_liquidation_log(l) for l in logs) if e]
    seen, uniq = set(), []
    for e in events:
        k = (e["tx"], e["log_index"])
        if k not in seen:
            seen.add(k); uniq.append(e)
    meta_cache = {}
    for s in {e["silo"] for e in uniq}:
        silo_token_meta(rpc, s, meta_cache)
    toks = {m["token"] for m in meta_cache.values() if m.get("token")}
    prices = llama_prices(chain, toks)
    agg = defaultdict(lambda: {"usd": 0.0, "count": 0, "symbol": "?"})
    for e in uniq:
        m = meta_cache.get(e["silo"], {"symbol": "?", "decimals": 18, "token": None})
        row = agg[e["silo"].lower()]
        row["count"] += 1
        row["symbol"] = m["symbol"]
        px = prices.get((m.get("token") or "").lower())
        if px:
            row["usd"] += (e["repay_raw"] / 10 ** m["decimals"]) * px
    have_usd = any(r["usd"] > 0 for r in agg.values())
    key = (lambda kv: kv[1]["usd"]) if have_usd else (lambda kv: kv[1]["count"])
    return [(s, r["symbol"], r["usd"], r["count"]) for s, r in sorted(agg.items(), key=key, reverse=True)[:n]]


def _addr_from_word(ret: str, word_idx: int) -> str | None:
    if not ret or not ret.startswith("0x"):
        return None
    body = ret[2:]
    start = word_idx * 64
    if len(body) < start + 64:
        return None
    return "0x" + body[start:start + 64][24:]


def get_silo_config(rpc: RPC, silo: str) -> str | None:
    try:
        a = _addr_from_word(rpc.eth_call(silo, SEL_CONFIG), 0)
    except RpcError:
        return None
    return a if a and int(a, 16) != 0 else None


def get_solvency_oracle(rpc: RPC, config: str, silo: str) -> str | None:
    data = SEL_GETCONFIG + silo[2:].lower().rjust(64, "0")
    try:
        a = _addr_from_word(rpc.eth_call(config, data), IDX_SOLVENCY_ORACLE)
    except RpcError:
        return None
    if a is None:
        return None
    return a if int(a, 16) != 0 else "0x0"


def resolve_proxy(rpc: RPC, addr: str) -> dict:
    """Если addr — EIP-1167 minimal-proxy, вернуть адрес имплементации. Иначе — сам addr.
    Возвращает {code_len, is_proxy, impl}."""
    try:
        code = rpc.call("eth_getCode", [addr, "latest"]) or "0x"
    except RpcError:
        code = "0x"
    body = code[2:].lower() if code.startswith("0x") else code.lower()
    is_proxy = EIP1167_PREFIX in body and EIP1167_SUFFIX in body
    impl = None
    if is_proxy:
        i = body.find(EIP1167_PREFIX) + len(EIP1167_PREFIX)
        impl_hex = body[i:i + 40]
        if len(impl_hex) == 40:
            impl = "0x" + impl_hex
    return {"code_len": len(body) // 2, "is_proxy": is_proxy, "impl": impl}


def probe_impl_class(rpc: RPC, impl: str) -> dict:
    """Проба КЛАССА имплементации оракула по class-маркерам в байткоде.
    Возвращает {hits:[(kind,name,sig)], guess}. guess — 'PUSH'/'PULL'/'СМЕШАН'/'НЕ ОПОЗНАН' (указатель!)."""
    try:
        code = rpc.call("eth_getCode", [impl, "latest"]) or "0x"
    except RpcError:
        code = "0x"
    body = code[2:].lower() if code.startswith("0x") else code.lower()
    hits = [(kind, name, sig) for s, (kind, name, sig) in MARKER_SELECTORS.items() if s in body]
    kinds = {k for k, _, _ in hits}
    if kinds == {"PUSH"}:
        guess = "PUSH (ончейн-фид)"
    elif kinds == {"PULL"}:
        guess = "PULL (DEX/TWAP)"
    elif "PUSH" in kinds and "PULL" in kinds:
        guess = "СМЕШАН (dual/forwarder — читать исходник)"
    else:
        guess = "НЕ ОПОЗНАН по маркерам — читать исходник"
    return {"hits": hits, "guess": guess}


SEL_ORACLE_CONFIG = "0x324b8d6e"      # oracleConfig() — сверено keccak И embedded PUSH4 в скомпилированном байткоде
SEL_CHAINLINK_CONFIG = "0xc3f909d4"   # ChainlinkV3OracleConfig.getConfig() (БЕЗ аргументов, ДРУГОЙ контракт,
                                       # не путать с SEL_GETCONFIG=ISiloConfig.getConfig(address) выше) — та же двойная сверка


def read_chainlink_config(rpc: RPC, oracle_proxy: str) -> dict | None:
    """proxy.oracleConfig() -> config-адрес -> config.getConfig() -> {primaryAggregator, primaryHeartbeat,
    baseToken, quoteToken, ...}. Прямая проверка агрегатора+heartbeat — НЕ то же самое, что impl-match
    (тот подтверждает КЛАСС кода; этот — какой именно фид и с какой свежестью он реально читает)."""
    cfg_ret = rpc.eth_call(oracle_proxy, SEL_ORACLE_CONFIG)
    if not cfg_ret or int(cfg_ret, 16) == 0:
        return None
    config_addr = "0x" + cfg_ret[-40:]
    ret = rpc.eth_call(config_addr, SEL_CHAINLINK_CONFIG)
    if not ret or len(ret) < 2 + 64 * 10:
        return None
    words = [ret[2 + i * 64: 2 + (i + 1) * 64] for i in range(10)]
    return {
        "config_addr": config_addr,
        "primary_aggregator": "0x" + words[0][-40:],
        "secondary_aggregator": "0x" + words[1][-40:],
        "primary_heartbeat": int(words[2], 16),
        "secondary_heartbeat": int(words[3], 16),
        "base_token": "0x" + words[6][-40:],
        "quote_token": "0x" + words[7][-40:],
    }


def main():
    ap = argparse.ArgumentParser(description="Адрес solvencyOracle + резолв прокси + ссылка на исходник")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", action="append", default=[])
    ap.add_argument("--chain", default="sonic")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--top", type=int, default=3)
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    if a.silo:
        silos = a.silo
    else:
        sys.stderr.write(f"определяю топ-{a.top} силосов по repaid-USD за {a.days:g}д…\n")
        top = top_silos_by_volume(rpc, a.chain, a.days, a.top)
        silos = [s for s, _, _, _ in top]
        print("Топ-силосы по объёму (repaid$/окно) — цель ИЗ ДАННЫХ:")
        for s, sym, usd, cnt in top:
            print(f"   {s}  {sym}  ${usd:,.0f}  ({cnt} ликв)")
        print()

    print("=" * 78)
    print("  ORACLE CHECK v2 — адрес solvencyOracle + имплементация (push/pull ГЛАЗАМИ по исходнику)")
    print("=" * 78)
    print("push/pull НЕ определяется автоматически: все Silo-оракулы держат ISiloOracle-селекторы.")
    print("Читай ИСХОДНИК адаптера: внутри Chainlink-агрегатор (latestRoundData) → PUSH (ончейн-фид);")
    print("чтение DEX-пула / TWAP (observe/slot0/consult) → PULL (офчейн-собираемая цена).\n")

    for silo in silos:
        silo = silo.lower()
        print(f"── силос {silo}")
        cfg = get_silo_config(rpc, silo)
        if not cfg:
            print("   config() не прочитан — пропуск\n"); continue
        oracle = get_solvency_oracle(rpc, cfg, silo)
        if oracle is None:
            print(f"   config {cfg} — solvencyOracle не декодирован\n"); continue
        if oracle == "0x0":
            print(f"   config {cfg} — solvencyOracle 0x0 (нет отдельного оракула)\n"); continue
        pr = resolve_proxy(rpc, oracle)
        print(f"   config:        {cfg}")
        print(f"   solvencyOracle:{oracle}  ({pr['code_len']} байт{', EIP-1167 proxy' if pr['is_proxy'] else ''})")
        print(f"     исходник:    {SONICSCAN.format(oracle)}")
        if pr["is_proxy"] and pr["impl"]:
            ip = resolve_proxy(rpc, pr["impl"])
            print(f"   ИМПЛЕМЕНТАЦИЯ: {pr['impl']}  ({ip['code_len']} байт)  ← ЧИТАТЬ ЭТОТ исходник для push/pull")
            print(f"     исходник:    {SONICSCAN.format(pr['impl'])}")
            cls = probe_impl_class(rpc, pr["impl"])
            if cls["hits"]:
                marks = ", ".join(f"{name}({sig})" for _, name, sig in cls["hits"])
                print(f"     class-маркеры: {marks}")
            print(f"     догадка (УКАЗАТЕЛЬ, не вердикт): {cls['guess']}")
            cc = read_chainlink_config(rpc, oracle)
            if cc:
                print(f"     oracleConfig:  {cc['config_addr']}")
                print(f"     primaryAggregator:   {cc['primary_aggregator']}  heartbeat={cc['primary_heartbeat']}с")
                print(f"     secondaryAggregator: {cc['secondary_aggregator']}  heartbeat={cc['secondary_heartbeat']}с")
                print(f"     base/quote token:    {cc['base_token']} / {cc['quote_token']}")
                print(f"     ⚠ ПРЯМАЯ проверка (не impl-match): именно ЭТОТ агрегатор и heartbeat")
                print(f"     реально используются данным силосом — не 'вероятно тот же класс'.")
        elif not pr["is_proxy"]:
            cls = probe_impl_class(rpc, oracle)
            if cls["hits"]:
                marks = ", ".join(f"{name}({sig})" for _, name, sig in cls["hits"])
                print(f"   class-маркеры: {marks}")
            print(f"   догадка (УКАЗАТЕЛЬ, не вердикт): {cls['guess']}")
        print()

    print("=" * 78)
    print("ЧТО ИСКАТЬ В ИСХОДНИКЕ ИМПЛЕМЕНТАЦИИ (решает досрочное закрытие):")
    print("  PULL/TWAP (DEX-пул, observe/consult, нет immutable Chainlink-агрегатора) → слепой спам")
    print("    (0x6bcbd4, 2% hit) = структурный потолок бота без офчейн-фида; 'кресло' наследует его → проект под вопросом.")
    print("  PUSH (immutable Chainlink AggregatorV3, latestRoundData) → момент ликвидируемости виден в стейте;")
    print("    тогда решает НЕ это, а value-раскладка и контестабельность реального силоса.")


if __name__ == "__main__":
    main()
