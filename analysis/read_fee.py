#!/usr/bin/env python3
"""read_fee.py v2 — liquidationFee ОБЕИХ сторон пары силосов + полный 17-полевой дамп. (read-only)

v1 дал «fee=0», и это был артефакт ДВУХ багов:
  1. OFF-BY-ONE: v1 читал слово 14 (flashloanFee), а liquidationFee — слово 13.
     Прочитанный «ноль» был flashloanFee (он и должен быть 0). Тест v1 был самосогласован
     с тем же неверным индексом — зелёные моки не доказали соответствие ABI.
  2. НЕ ТА СТОРОНА ПАРЫ: премию ликвидатора ценит collateralConfig.liquidationFee
     (PartialLiquidation.sol:83), а в событие пишется debtConfig.silo (:165).
     Наши 1238 ликвидаций сгруппированы по event.silo=0x322e1d53 → это ДОЛГОВАЯ сторона;
     премию ценит СОСЕДНИЙ (залоговый) силос, который v1 не читал.

v2: getSilos() на конфиге → оба силоса пары → getConfig каждого → ВСЕ 17 полей с ярлыками.
Валидация рамки (после неё числам на слове 13 можно верить):
  • эхо silo (слово 2) == запрошенный силос;
  • maxLtv (слово 10) и lt (слово 11) — правдоподобные проценты (0.3–0.99e18).

Запуск:
  python3 -m analysis.read_fee --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC, silo_token_meta

SEL_CONFIG    = "0x79502c55"  # ISilo.config() -> ISiloConfig
SEL_GETSILOS  = "0xaecc90cb"  # ISiloConfig.getSilos() -> (silo0, silo1)   keccak-сверен
SEL_GETCONFIG = "0xe48a5f7b"  # ISiloConfig.getConfig(address) -> ConfigData

# ConfigData: ровно 17 полей, порядок сверен с ISiloConfig.sol (0-based)
FIELDS = [
    ("daoFee", "pct"), ("deployerFee", "pct"), ("silo", "addr"), ("token", "addr"),
    ("protectedShareToken", "addr"), ("collateralShareToken", "addr"), ("debtShareToken", "addr"),
    ("solvencyOracle", "addr"), ("maxLtvOracle", "addr"), ("interestRateModel", "addr"),
    ("maxLtv", "pct"), ("lt", "pct"), ("liquidationTargetLtv", "pct"),
    ("liquidationFee", "pct"), ("flashloanFee", "pct"), ("hookReceiver", "addr"), ("callBeforeQuote", "bool"),
]
IDX = {name: i for i, (name, _) in enumerate(FIELDS)}
assert IDX["liquidationFee"] == 13 and IDX["flashloanFee"] == 14  # регресс off-by-one v1


def _word(ret: str, i: int) -> str:
    body = ret[2:] if ret.startswith("0x") else ret
    return body[i * 64:(i + 1) * 64]


def parse_config(ret: str) -> dict:
    """ABI-ответ getConfig -> {имя: значение} по FIELDS. addr → '0x…', pct/bool → int."""
    out = {}
    for name, kind in FIELDS:
        w = _word(ret, IDX[name])
        out[name] = ("0x" + w[24:]) if kind == "addr" else int(w, 16)
    return out


def frame_check(cfg: dict, expected_silo: str) -> list:
    """Валидация индексной рамки: эхо silo + правдоподобие lt/maxLtv. Возвращает заметки."""
    notes = []
    ok_echo = cfg["silo"].lower() == expected_silo.lower()
    notes.append(("✓" if ok_echo else "⚠") + f" эхо silo (слово 2) {'совпало' if ok_echo else 'НЕ совпало — рамка сдвинута!'}")
    for f in ("maxLtv", "lt"):
        v = cfg[f]
        ok = 0.3e18 <= v <= 0.99e18
        notes.append(("✓" if ok else "⚠") + f" {f} = {v/1e18*100:.1f}% " + ("правдоподобен" if ok else "НЕправдоподобен — рамке не верить!"))
    return notes


def fmt(name: str, kind: str, v) -> str:
    if kind == "addr":
        return v
    if kind == "bool":
        return str(bool(v))
    return f"{v}  ({v/1e18*100:.2f}%)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", default="0x322e1d5384aa4ed66aeca770b95686271de61dc3",
                    help="ДОЛГОВОЙ силос (event.silo измеренных ликвидаций)")
    a = ap.parse_args()
    rpc = RPC(a.rpc)
    debt = a.silo.lower()

    config = "0x" + _word(rpc.eth_call(debt, SEL_CONFIG), 0)[24:]
    silos_ret = rpc.eth_call(config, SEL_GETSILOS)
    s0, s1 = "0x" + _word(silos_ret, 0)[24:], "0x" + _word(silos_ret, 1)[24:]
    if debt not in (s0, s1):
        print(f"⚠ getSilos() вернул {s0}, {s1} — запрошенный {debt} не в паре. Стоп.")
        return
    sibling = s1 if s0 == debt else s0

    meta = {}
    sides = [
        (debt,    "ДОЛГОВАЯ сторона (event.silo наших 1238 ликвидаций)"),
        (sibling, "ЗАЛОГОВАЯ сторона — ЕЁ liquidationFee ценит премию НАШИХ ликвидаций (хук :83)"),
    ]
    fees = {}
    print(f"config пары: {config}\nсилосы пары: {s0} | {s1}\n")
    for addr, label in sides:
        m = silo_token_meta(rpc, addr, meta)
        ret = rpc.eth_call(config, SEL_GETCONFIG + addr[2:].rjust(64, "0"))
        cfg = parse_config(ret)
        fees[addr] = cfg["liquidationFee"]
        print("=" * 78)
        print(f"{label}\n  силос {addr}   актив: {m.get('symbol')} (decimals={m.get('decimals')}, token {m.get('token')})")
        for note in frame_check(cfg, addr):
            print("  " + note)
        print("  " + "-" * 74)
        for name, kind in FIELDS:
            mark = "  ◄◄◄ ПРЕМИЯ" if (name == "liquidationFee") else ("  (v1 читал ЭТО)" if name == "flashloanFee" else "")
            print(f"  [{IDX[name]:2d}] {name:22s} {fmt(name, kind, cfg[name])}{mark}")
        print()

    print("=" * 78)
    print("ИТОГ:")
    print(f"  liquidationFee долговой стороны:  {fees[debt]/1e18*100:.2f}%  (в наших ликвидациях НЕ участвует)")
    print(f"  liquidationFee залоговой стороны: {fees[sibling]/1e18*100:.2f}%  ← ВАЛОВАЯ ПРЕМИЯ наших 1238 ликвидаций")
    print("  (v1-й «0.00%» был flashloanFee(слово 14) долговой стороны — дважды нерелевантен)")


if __name__ == "__main__":
    main()
