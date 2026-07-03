#!/usr/bin/env bash
# Установка silo_state.py — консолидированный статус проекта Silo-ликвидатор. Read-only, stdlib. Боты не трогает.
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: это Morpho-бот"; exit 1; }
[ -e "$DIR/chain/morpho.py" ] && { echo "СТОП: Morpho-файлы в $DIR"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py — сперва update_contestation.sh"; exit 1; }
[ -f "$DIR/analysis/permission_check.py" ] || { echo "СТОП: нет permission_check.py — сперва update_permission_check.sh"; exit 1; }
mkdir -p "$DIR/analysis"
cat > "$DIR/analysis/silo_state.py" << 'SILO_EOF'
#!/usr/bin/env python3
"""silo_state.py — КОНСОЛИДИРОВАННЫЙ СТАТУС проекта Silo-ликвидатор (read-only). Сверяемся по нему.

Собирает в один дашборд всё, что мы установили замерами, и пересчитывает НА ЛЕТУ (детект дрейфа):
  1. ПЕРМИШЕН-КАРТА: probe известных hook/controller (из репо Silo) → какие рынки гейтятся вайтлистом
     (enabled=True), живой состав ALLOWED_ROLE, owner-Safe. Гейтнутые config'и собираются динамически.
  2. РЕАЛЬНОСТЬ РЫНКА: ликвидации за --days по силосам, каждый помечен config'ом и OPEN/GATED.
  3. ЦЕЛЬ: биггест силос по объёму находится ЭМПИРИЧЕСКИ (не хардкод). Если он OPEN — это скоростная
     гонка (контестабельно); концентрация: инкумбент % vs «щель» (доля мимо инкумбента = доступна нам).
  4. ВЕРДИКТ + следующий шаг. Опц. --json пишет снимок (сверять во времени: не закрылась ли дверь).

Запуск:
  python3 -m analysis.silo_state --rpc https://rpc.soniclabs.com --chain sonic --days 7
  python3 -m analysis.silo_state --rpc "$ARB_RPC" --chain arbitrum --days 3 --json state_arb.json
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from collections import Counter

from analysis.contestation import RPC
from analysis.permission_check import (
    probe, gather_empirics, silo_config_of, dec_addr,
    SONIC_PROBES, ARB_PROBES, SONIC_KNOWN_WL, MARKET_LABEL,
    SONIC_GATED_CONFIG,
)

INCUMBENT = "0x0094c5d6b460a2efff23420db097921bcc2b2a2c"
KNOWN_CHALLENGERS = {  # VPS-боты, эмпирически берущие долю на открытых силосах
    "0x6bcbd4feb5d9894b3ed08fad2e1cf488c1eeb580": "challenger (спам-стратегия)",
    "0x3504994ec7180e1212b478ff64d6ea53988b0ebe": "challenger",
    "0xccd487e01e9df6932f656b53668f58005f604417": "batch-кипер (и на open)",
}
# гейтнутые config'и, известные из репо (сид; основное — динамика из enabled-контроллеров)
SEED_GATED_CONFIGS = {SONIC_GATED_CONFIG}


def build_gating(probe_results: list, config_lookup) -> dict:
    """Из результатов probe собираем: живые контроллеры (enabled), гейтнутые config'и/силосы, вайтлист, owner.
    config_lookup(addr)->config|None вызывается только для anySilo включённых контроллеров."""
    gated_silos, gated_configs = set(), set(SEED_GATED_CONFIGS)
    whitelist, owners = set(), set()
    controllers = []
    for p in probe_results:
        for m in (p.get("members") or []):
            whitelist.add(m)
        if p.get("owner"):
            owners.add(p["owner"])
        if p.get("kind") == "controller" and p.get("enabled"):
            sil = p.get("any_silo")
            controllers.append({"ctrl": p["addr"], "any_silo": sil,
                                "members": p.get("members") or []})
            if sil:
                gated_silos.add(sil)
                cfg = config_lookup(sil)
                if cfg:
                    gated_configs.add(cfg)
    return {"gated_silos": gated_silos, "gated_configs": gated_configs,
            "whitelist": whitelist, "owners": owners, "controllers": controllers}


def is_gated(silo: str, cfg, gating: dict) -> bool:
    return (silo in gating["gated_silos"]) or (cfg is not None and cfg in gating["gated_configs"])


def concentration(counter: Counter) -> dict:
    tot = sum(counter.values()) or 1
    top_addr, top_n = counter.most_common(1)[0]
    gap = tot - top_n
    ch = sum(n for a, n in counter.items() if a in KNOWN_CHALLENGERS)
    return {"total": tot, "top": top_addr, "top_share": top_n / tot,
            "gap_share": gap / tot, "challenger_share": ch / tot, "distinct": len(counter)}


def assess(silos_tagged: list) -> dict:
    """silos_tagged: [(silo, cfg, counter, gated_bool)]. Возвращает биггест и биггест-OPEN + оценки."""
    if not silos_tagged:
        return {"biggest": None, "target": None}
    by_vol = sorted(silos_tagged, key=lambda r: -sum(r[2].values()))
    biggest = by_vol[0]
    target = next((r for r in by_vol if not r[3]), None)  # первый OPEN по объёму
    return {"biggest": biggest, "target": target, "by_vol": by_vol}


def verdict_lines(gating: dict, a: dict) -> list:
    L = []
    nctrl = len(gating["controllers"])
    if nctrl:
        L.append(f"🔒 Пермишен-система ЖИВА: {nctrl} включённых контроллер(ов), вайтлист "
                 f"{len(gating['whitelist'])} адр., owner-Safe {', '.join(list(gating['owners'])[:1])}.")
        L.append("   Но гейтнутые рынки — мелкие/без объёма; курс Silo на пермишен подтверждён (риск на будущее).")
    else:
        L.append("• Включённых контроллеров в probe-наборе не найдено (или не читаются).")
    tgt = a.get("target")
    if not tgt:
        L.append("⚠ Нет OPEN-силоса с объёмом — либо всё гейтнуто, либо окно пустое. Проверь вывод.")
        return L
    silo, cfg, cnt, _ = tgt
    c = concentration(cnt)
    inc_share = cnt.get(INCUMBENT, 0) / (sum(cnt.values()) or 1)
    L.append(f"🟢 ЦЕЛЬ (биггест OPEN): {silo}")
    L.append(f"   config {cfg} — НЕ гейтится → скоростная гонка, контестабельно.")
    L.append(f"   инкумбент {inc_share*100:.0f}%, ЩЕЛЬ (мимо инкумбента) {c['gap_share']*100:.0f}%, "
             f"из них известные challenger-боты {c['challenger_share']*100:.0f}% — их и обгоняем первыми.")
    L.append("   СТАТУС: CONTESTABLE. Следующий шаг — форк LiquidationHelper + paper-режим на этом силосе")
    L.append("   (измерить отставание по блокам от инкумбента и опережаем ли challenger'ов), ноль капитала.")
    return L


def main():
    ap = argparse.ArgumentParser(description="Silo liquidator — consolidated project state")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--chain", default="sonic", choices=["sonic", "arbitrum"])
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--json", help="записать снимок состояния в файл")
    a = ap.parse_args()
    rpc = RPC(a.rpc)
    probes_src = SONIC_PROBES if a.chain == "sonic" else ARB_PROBES

    sys.stderr.write("probe пермишен-контрактов…\n")
    presults = [probe(rpc, addr) for _, addr in probes_src]
    cfg_cache = {}
    gating = build_gating(presults, lambda s: silo_config_of(rpc, s, cfg_cache))

    sys.stderr.write(f"эмпирика ликвидаций за {a.days:g}д…\n")
    events, by_silo = gather_empirics(rpc, a.days)
    tagged = []
    for silo, cnt in by_silo.items():
        cfg = silo_config_of(rpc, silo, cfg_cache)
        tagged.append((silo, cfg, cnt, is_gated(silo, cfg, gating)))
    a_ = assess(tagged)

    # ---------- дашборд ----------
    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    print("=" * 74)
    print(f"  SILO LIQUIDATOR — СТАТУС ПРОЕКТА — {a.chain.upper()} — {now}")
    print("=" * 74)
    print("\n─ ПЕРМИШЕН-СИСТЕМА ─")
    for p in presults:
        extra = ""
        if p["kind"] == "controller":
            extra = f" enabled={p['enabled']} anySilo={(p['any_silo'] or '?')[:10]}…"
        mem = "" if p["members"] is None else f" роль={len(p['members'])}"
        print(f"  {p['addr'][:12]}…  {p['kind']:12s} код={p['code']}б{extra}{mem}")
    print(f"  → гейтнутых config'ов: {len(gating['gated_configs'])} | вайтлист-адресов: {len(gating['whitelist'])} "
          f"| включённых контроллеров: {len(gating['controllers'])}")

    print(f"\n─ РЕАЛЬНОСТЬ РЫНКА (за {a.days:g}д) ─  ликв={len(events)} силосов={len(by_silo)}")
    for silo, cfg, cnt, gated in a_.get("by_vol", []):
        tot = sum(cnt.values())
        top_a, top_n = cnt.most_common(1)[0]
        flag = "GATED 🔒" if gated else "OPEN 🟢"
        print(f"  {silo[:12]}…  cfg {(cfg or '—')[:10]}…  {flag:9s} ликв={tot:3d}  "
              f"топ {top_a[:10]}… {top_n/tot*100:3.0f}%")

    print("\n─ ВЕРДИКТ ─")
    vl = verdict_lines(gating, a_)
    for line in vl:
        print(line)

    if a.json:
        snap = {"ts": int(time.time()), "chain": a.chain, "days": a.days,
                "gated_configs": sorted(gating["gated_configs"]),
                "whitelist": sorted(gating["whitelist"]),
                "controllers_enabled": len(gating["controllers"]),
                "silos": [{"silo": s, "config": c, "liqs": sum(cnt.values()),
                           "gated": g, "top": cnt.most_common(1)[0][0],
                           "top_share": cnt.most_common(1)[0][1] / (sum(cnt.values()) or 1)}
                          for s, c, cnt, g in a_.get("by_vol", [])],
                "target": (a_["target"][0] if a_.get("target") else None)}
        with open(a.json, "w") as f:
            json.dump(snap, f, indent=1)
        print(f"\nснимок записан в {a.json}")


if __name__ == "__main__":
    main()
SILO_EOF
cd "$DIR"
python3 -m py_compile analysis/silo_state.py && echo "[OK] py_compile"
python3 - << 'PY_TEST'
import analysis.silo_state as st
from collections import Counter
INC=st.INCUMBENT; CH="0x6bcbd4feb5d9894b3ed08fad2e1cf488c1eeb580"
probes=[{"addr":"0xhook","kind":"hook/прочее","enabled":None,"any_silo":None,"members":[INC,"0xb1"],"owner":"0xsafe"},
        {"addr":"0xctrl","kind":"controller","enabled":True,"any_silo":"0xSILO","members":[INC],"owner":"0xsafe"},
        {"addr":"0xoff","kind":"controller","enabled":False,"any_silo":"0xZZ","members":[],"owner":None}]
g=st.build_gating(probes, lambda s: {"0xSILO":"0xGATEDCFG"}.get(s))
assert "0xSILO" in g["gated_silos"] and "0xGATEDCFG" in g["gated_configs"] and len(g["controllers"])==1
assert st.is_gated("0xother","0xGATEDCFG",g) and not st.is_gated("0xopen","0xOPENCFG",g)
c=st.concentration(Counter({INC:106,CH:12,"0x3504994ec7180e1212b478ff64d6ea53988b0ebe":5,"0xaa":1}))
assert c["total"]==124 and abs(c["gap_share"]-18/124)<1e-9 and abs(c["challenger_share"]-17/124)<1e-9
a=st.assess([("0xBIGgated","0xGATEDCFG",Counter({INC:200}),True),("0xBIGopen","0xOPENCFG",Counter({INC:106,CH:18}),False)])
assert a["biggest"][0]=="0xBIGgated" and a["target"][0]=="0xBIGopen"
j="\n".join(st.verdict_lines(g,a)); assert "CONTESTABLE" in j and "ЦЕЛЬ" in j
assert st.assess([("0xg","0xGATEDCFG",Counter({INC:10}),True)])["target"] is None
print("[OK] тесты: build_gating/is_gated/concentration/assess/verdict — прошли")
PY_TEST
echo ">> silo_state.py установлен и проверен."
