#!/usr/bin/env python3
"""Расширенный archive-зонд: НЕ один eth_call, а replay-профиль — сотня+ РАЗНЫХ семантических
исполнений EVM на историческом стейте, разные адреса/селекторы/глубины (кэш одного блока не спасёт).
Считаем успехи/ошибки, ловим missing trie node / state not available."""
import sys, time

from analysis.contestation import RPC, RpcError, TOPIC0_LIQUIDATION_CALL, decode_liquidation_log
from analysis.borrower_health import SILO_LENS_SONIC, SEL_IS_SOLVENT, SEL_USER_LTV, SEL_USER_LT, SEL_DEBT_BAL, _addr_pad
from analysis.live_detector import SEL_MAXLIQ
from analysis.read_fee import SEL_CONFIG, SEL_GETSILOS, SEL_GETCONFIG, _word

rpc = RPC("https://rpc.soniclabs.com")
SILO = "0x322e1d5384aa4ed66aeca770b95686271de61dc3"
COLL_SILO = "0xf55902de87bd80c6a35614b48d7f8b612a083c12"
HOOK = "0x6aafd9dd424541885fd79c06fda96929cfd512f9"
CONFIG = "0x062a36bbe0306c2fd7aecdf25843291fbab96ad2"
BLK = 72462468  # replay-целевой блок (реальная ликвидация)

SEL_BALANCEOF = "0x70a08231"
SEL_TOTALSUPPLY = "0x18160ddd"
SEL_ASSET = "0x38d52e0f"

def call_at(to, data, blk):
    return rpc.call("eth_call", [{"to": to, "data": data}, hex(blk)])

# 1) Набрать РАЗНЫХ заёмщиков из реальных ликвидаций за ~3 дня до целевого блока (их trie-пути точно
#    населены на этом стейте) — это и есть варьирование адресов против кэша.
logs = rpc.call("eth_getLogs", [{"fromBlock": hex(BLK - 250_000), "toBlock": hex(BLK),
                                 "topics": [TOPIC0_LIQUIDATION_CALL]}])
evs = [e for e in (decode_liquidation_log(l) for l in logs) if e]
borrowers = list({e["borrower"] for e in evs})[:20]
liquidators = list({e["liquidator"] for e in evs})[:8]
print(f"заёмщиков для зонда: {len(borrowers)}, ликвидаторов: {len(liquidators)} (из {len(evs)} событий окна)")

ok = 0; fail = []
t0 = time.time()

def probe(label, to, data, blk):
    global ok
    try:
        r = call_at(to, data, blk)
        assert r is not None
        ok += 1
    except (RpcError, RuntimeError, AssertionError) as ex:
        fail.append((label, blk, str(ex)[:160]))

# 2) Replay-профиль на целевом блоке: SiloLens×4 + maxLiquidation на каждом заёмщике
for b in borrowers:
    args = _addr_pad(SILO) + _addr_pad(b)
    for sel, name in [(SEL_IS_SOLVENT, "isSolvent"), (SEL_USER_LTV, "ltv"), (SEL_USER_LT, "lt"), (SEL_DEBT_BAL, "debt")]:
        probe(f"lens.{name}({b[:10]})", SILO_LENS_SONIC, sel + args, BLK)
    probe(f"hook.maxLiq({b[:10]})", HOOK, SEL_MAXLIQ + _addr_pad(b), BLK)

# 3) Конфиг/токены на том же блоке (другие контракты = другие trie-поддеревья)
probe("silo.config", SILO, SEL_CONFIG, BLK)
probe("config.getSilos", CONFIG, SEL_GETSILOS, BLK)
for s in (SILO, COLL_SILO):
    probe(f"config.getConfig({s[:10]})", CONFIG, SEL_GETCONFIG + s[2:].rjust(64, "0"), BLK)
    probe(f"silo.asset({s[:10]})", s, SEL_ASSET, BLK)
    probe(f"silo.totalSupply({s[:10]})", s, SEL_TOTALSUPPLY, BLK)
    for w in liquidators:
        probe(f"silo.balanceOf({w[:10]})", s, SEL_BALANCEOF + _addr_pad(w), BLK)

# 4) Глубокие блоки: прямые вызовы к silo/config (SiloLens мог не существовать на самых старых)
deep = [60_000_000, 40_000_000, 20_000_000, 10_000_000, 6_000_000]
for d in deep:
    probe(f"deep.silo.asset", SILO, SEL_ASSET, d)
    probe(f"deep.silo.totalSupply", SILO, SEL_TOTALSUPPLY, d)
    probe(f"deep.config.getConfig", CONFIG, SEL_GETCONFIG + SILO[2:].rjust(64, "0"), d)
    for b in borrowers[:3]:
        probe(f"deep.silo.balanceOf", SILO, SEL_BALANCEOF + _addr_pad(b), d)

# 5) И SiloLens на средней глубине (полгода-квартал): существовал ли — увидим по ошибке/пустоте
for d in (60_000_000, 40_000_000):
    for b in borrowers[:5]:
        probe(f"mid.lens.isSolvent", SILO_LENS_SONIC, SEL_IS_SOLVENT + _addr_pad(SILO) + _addr_pad(b), d)

dt = time.time() - t0
total = ok + len(fail)
print(f"\nИТОГ: {ok}/{total} eth_call успешны за {dt:.1f}с ({total/dt:.1f} вызовов/с)")
if fail:
    print(f"ОШИБКИ ({len(fail)}):")
    seen = set()
    for label, blk, msg in fail:
        key = (msg[:60],)
        if key not in seen:
            seen.add(key)
            print(f"  @{blk} {label}: {msg}")
else:
    print("ошибок нет — ни одного missing trie node / state not available")
