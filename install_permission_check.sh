#!/usr/bin/env bash
# Установка permission_check.py — ончейн-проверка вайтлист-стены Silo. Read-only, stdlib. Боты не трогает.
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: это Morpho-бот"; exit 1; }
[ -e "$DIR/chain/morpho.py" ] && { echo "СТОП: Morpho-файлы в $DIR"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py — сперва update_contestation.sh"; exit 1; }
mkdir -p "$DIR/analysis"
cat > "$DIR/analysis/permission_check.py" << 'SILO_EOF'
#!/usr/bin/env python3
"""permission_check.py — ГЛАВНАЯ ПРОВЕРКА: заперт ли рынок Silo ВАЙТЛИСТОМ, а не скоростью. (read-only)

Открытие из репо Silo: задача set-permissioned-liquidation (Safe-батчи от 2026-05-12) вешает на рынки
PermissionedLiquidationController (гейдж на collateral share-токене). Механика (из исходника 4.17.0):
  - enabled=false при деплое; включает owner (Safe хука);
  - вайтлистнутый (ALLOWED_ROLE) перед ликвидацией зовёт allowMeToLiquidate() → transient-флаг;
  - при enabled=true ЛЮБОЙ перевод collateral-шар, после которого sender неплатёжеспособен
    (= ликвидация), от НЕ-вайтлистнутого → revert LiquidationNotAllowed().
Т.е. если enabled=true на большом силосе — «инкумбент быстрее» было НЕВЕРНОЙ рамкой: он не быстрее,
он ДОПУЩЕН. 973 реверта челленджера — стук в стену пермишена, а не проигрыш гонки.

НО: Safe-батч в репо — заготовка, не факт исполнения; enabled стартует false. Правду знает только чейн.
Этот скрипт читает её напрямую:
  1. permisionedData() контроллера → enabled + anySilo (какой рынок гейтится).
  2. Живой состав ALLOWED_ROLE (getRoleMemberCount/getRoleMember) — кто реально допущен СЕЙЧАС.
  3. owner() → чей Safe рулит (кому подаваться на вайтлист — единственная реальная дверь).
  4. Эмпирика (Sonic): победители за --days по силосам; для гейтнутого рынка — все ли победители ∈ роли.
Arbitrum-режим: state-only матрица по 7 контроллерам (enabled + членство наших топ-победителей).

Запуск:
  python3 -m analysis.permission_check --rpc https://rpc.soniclabs.com --chain sonic --days 7
  python3 -m analysis.permission_check --rpc "$ARB_RPC" --chain arbitrum          # state-only
"""
from __future__ import annotations
import argparse
import sys
from collections import defaultdict, Counter

from analysis.contestation import RPC, RpcError, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts

# ---- вычислено из исходников Silo (keccak сверен с Safe-батчем) ----
ALLOWED_ROLE = "0xd5dc6b389d0dd5687ab5bd9338f760ebeaff2d2852a93a9a9ebaebbfefc763ac"
SEL_PERM_DATA   = "0x421f5b13"   # permisionedData() -> (anySilo, enabled, isDebt)
SEL_CONFIG      = "0x79502c55"   # ISilo.config()
SEL_HAS_ROLE    = "0x91d14854"
SEL_ROLE_GETTER = "0xd32f7154"
SEL_MEMBER_CNT  = "0xca15c873"
SEL_MEMBER_AT   = "0x9010d07c"
SEL_OWNER       = "0x8da5cb5b"

SONIC_CONTROLLER   = "0xebb2677d5a5ff045b7e34f514cc2f353932833a3"
SONIC_GATED_CONFIG = "0x180a8ff4c343b6eea2ff0feb2dcc92a360bf7025"  # siloConfig рынка из деплой-мэппинга
SONIC_BATCH_WL = ["0x0094c5d6b460a2efff23420db097921bcc2b2a2c","0x08a52ec31e0e981bbd64082224185e420d3f9849",
                  "0xb49329bf1d95d51681f4e4f644eb37f58e398abd","0xccd487e01e9df6932f656b53668f58005f604417"]

ARB_CONTROLLERS = ["0x15d3ebe0317cd2f0db0a6f4780c29c43c93a1003","0x2c7d9b9823bbcc63e52de90db6f3fb10679789ae",
                   "0x95626c763a2c9fdf3e8a4e6644f2b9bcdb67097a","0xaf45c4f4b0239a20157eda4069c283cb8c7d6af2",
                   "0xf3c69312f020eaaa9e0a60295549820a77b5cab9","0x2174557e5ed2e8256284a5df42a91b21db6313a9",
                   "0x97dc5ea7a2d98599534655535e101e2f35bbcd69"]
ARB_BATCH_WL = ["0x1a7f0b5201e3fa97f0ef1146d8d7be8ad7c3860f","0x2ab8d934ffbe1653c9140778beda598ddac7f2e1",
                "0x56b9289dbe2a036e41c2c66b1b0d0346a54a59e2","0xe78e99b5674ff8fed71529f98884ce5b9f897ebf"]
ARB_TOP_WINNERS = ["0x879c2a2f7e4071ebdc971e508885d4a8cdeaf227","0x823de6b63f9cb010cbb58951c90eea30bf02bd36",
                   "0xc8c2ab457ecb26ea47018490ffa0b2cb2646a7ef","0x4b2b41dfbf77d3fd332a4d572706373376675b69",
                   "0x0665609124cc2a958cf0ed582ee132076243b6da"]


def pad32(hexstr: str) -> str:
    h = hexstr.lower().replace("0x", "")
    return "0" * (64 - len(h)) + h


def call(rpc: RPC, to: str, data: str):
    try:
        return rpc.eth_call(to, data)
    except (RpcError, RuntimeError):
        return None


def dec_addr(word_hex: str) -> str:
    return "0x" + word_hex[-40:].lower()


def controller_state(rpc: RPC, ctrl: str) -> dict:
    """permisionedData() → anySilo/enabled/isDebt; owner(); живой состав роли."""
    out = {"ctrl": ctrl, "ok": False, "any_silo": None, "enabled": None, "is_debt": None,
            "owner": None, "members": None}
    ret = call(rpc, ctrl, SEL_PERM_DATA)
    if not ret or len(ret) < 2 + 64 * 3:
        return out
    h = ret[2:]
    out["any_silo"] = dec_addr(h[0:64])
    out["enabled"]  = int(h[64:128], 16) != 0
    out["is_debt"]  = int(h[128:192], 16) != 0
    own = call(rpc, ctrl, SEL_OWNER)
    out["owner"] = dec_addr(own[2:]) if own and own != "0x" else None
    # роль читаем с самого контракта (устойчивее), фолбэк — вычисленная константа
    role_ret = call(rpc, ctrl, SEL_ROLE_GETTER)
    role = ("0x" + role_ret[2:66]) if role_ret and len(role_ret) >= 66 else ALLOWED_ROLE
    cnt_ret = call(rpc, ctrl, SEL_MEMBER_CNT + pad32(role))
    members = []
    if cnt_ret:
        cnt = int(cnt_ret, 16)
        for i in range(min(cnt, 32)):
            m = call(rpc, ctrl, SEL_MEMBER_AT + pad32(role) + pad32(hex(i)))
            if m:
                members.append(dec_addr(m[2:]))
    out["members"] = members
    out["role"] = role
    out["ok"] = True
    return out


def has_role(rpc: RPC, ctrl: str, role: str, addr: str):
    ret = call(rpc, ctrl, SEL_HAS_ROLE + pad32(role) + pad32(addr))
    return None if ret is None else (int(ret, 16) != 0)


def silo_config_of(rpc: RPC, silo: str, cache: dict):
    if silo in cache:
        return cache[silo]
    ret = call(rpc, silo, SEL_CONFIG)
    cfg = dec_addr(ret[2:]) if ret and ret != "0x" else None
    cache[silo] = cfg
    return cfg


def gather_sonic_empirics(rpc: RPC, days: float):
    tip = rpc.block_number()
    target = rpc.block_ts(tip) - int(days * 86400)
    frm = find_block_at_ts(rpc, target, tip)
    logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000)
    events = [e for e in (decode_liquidation_log(l) for l in logs) if e]
    seen, uniq = set(), []
    for e in events:
        k = (e["tx"], e["log_index"])
        if k not in seen:
            seen.add(k); uniq.append(e)
    by_silo = defaultdict(Counter)
    for e in uniq:
        by_silo[e["silo"]][e["liquidator"]] += 1
    return uniq, by_silo


def verdict_sonic(state: dict, gated_winner_sets: dict) -> str:
    if not state["ok"]:
        return "⚠ контроллер не читается — проверь RPC/адрес."
    members = set(state["members"] or [])
    if state["enabled"] is False:
        return ("🟢 enabled=FALSE: пермишен-стена НЕ активна на гейтнутом рынке — гонка там реальная.\n"
                "   Тогда доминирование инкумбента — всё же скорость, и paper-план в силе.")
    lines = []
    if state["enabled"]:
        lines.append("🔒 enabled=TRUE: большой рынок ЗАПЕРТ ВАЙТЛИСТОМ. Это не гонка и не co-location —")
        lines.append("   не-вайтлистнутый физически не может ликвидировать (revert LiquidationNotAllowed).")
        lines.append(f"   Живой вайтлист ({len(members)}): " + ", ".join(m[:10] + "…" for m in sorted(members)))
        outsiders = {}
        for silo, wl_ok, winners in gated_winner_sets.get("gated", []):
            outs = [w for w in winners if w not in members]
            if outs:
                outsiders[silo] = outs
        if gated_winner_sets.get("gated") and not outsiders:
            lines.append("   Эмпирика сходится: ВСЕ победители на гейтнутом рынке ∈ вайтлисту.")
        elif outsiders:
            lines.append("   ⚠ АНОМАЛИЯ: на гейтнутом рынке есть победители вне роли — шли вывод, разберём.")
        lines.append("   ЕДИНСТВЕННАЯ дверь — не скорость, а ДОПУСК: owner (Safe) = " + str(state["owner"]))
        lines.append("   → путь BD: заявка команде Silo на ALLOWED_ROLE (у них уже 4 внешних кипера — прецедент).")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Silo permissioned-liquidation on-chain check")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--chain", default="sonic", choices=["sonic", "arbitrum"])
    ap.add_argument("--days", type=float, default=7.0)
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    if a.chain == "sonic":
        st = controller_state(rpc, SONIC_CONTROLLER)
        print("=" * 72)
        print("  PERMISSION CHECK — Silo V2 — SONIC — контроллер", SONIC_CONTROLLER[:12] + "…")
        print("=" * 72)
        if st["ok"]:
            print(f"enabled={st['enabled']}  anySilo={st['any_silo']}  owner(Safe)={st['owner']}")
            print(f"живой ALLOWED_ROLE ({len(st['members'] or [])}):")
            for m in st["members"] or []:
                tag = "  (инкумбент)" if m == SONIC_BATCH_WL[0] else ("  (из батча)" if m in SONIC_BATCH_WL else "  (НЕ из батча!)")
                print("   ", m, tag)
        else:
            print("⚠ permisionedData() не читается — контроллер не тот/не задеплоен?")

        sys.stderr.write(f"\nэмпирика: тяну ликвидации за {a.days:g}д…\n")
        events, by_silo = gather_sonic_empirics(rpc, a.days)
        cfg_cache = {}
        gated, open_ = [], []
        for silo, winners in sorted(by_silo.items(), key=lambda kv: -sum(kv[1].values())):
            cfg = silo_config_of(rpc, silo, cfg_cache)
            row = (silo, cfg == SONIC_GATED_CONFIG, winners)
            (gated if cfg == SONIC_GATED_CONFIG else open_).append(row)
        print(f"\nликвидаций за окно: {len(events)}  |  силосов: {len(by_silo)}")
        print("\n──── ГЕЙТНУТЫЙ РЫНОК (config " + SONIC_GATED_CONFIG[:10] + "…) ────")
        members = set(st["members"] or [])
        for silo, _, winners in gated:
            tot = sum(winners.values())
            ins = sum(n for w, n in winners.items() if w in members)
            print(f"  silo {silo[:12]}…  ликв={tot}  победителей={len(winners)}  из них в роли: {ins}/{tot}")
            for w, n in winners.most_common(6):
                print(f"     {w[:12]}…  {n:4d}  {'∈ РОЛИ' if w in members else 'вне роли'}")
        if not gated:
            print("  (за окно на этом config ликвидаций не было)")
        print("\n──── ОТКРЫТЫЕ РЫНКИ (остальные силосы) ────")
        for silo, _, winners in open_[:6]:
            tot = sum(winners.values())
            top_w, top_n = winners.most_common(1)[0]
            print(f"  silo {silo[:12]}…  ликв={tot:4d}  топ {top_w[:10]}… {top_n/tot*100:3.0f}%")
        print("\n" + "=" * 72)
        print(verdict_sonic(st, {"gated": gated}))
    else:
        print("=" * 72)
        print("  PERMISSION CHECK — Silo V2 — ARBITRUM — state-only по 7 контроллерам")
        print("=" * 72)
        any_enabled = False
        for ctrl in ARB_CONTROLLERS:
            st = controller_state(rpc, ctrl)
            if not st["ok"]:
                print(f"  {ctrl[:12]}…  не читается (не задеплоен?)")
                continue
            any_enabled |= bool(st["enabled"])
            mem = st["members"] or []
            print(f"  {ctrl[:12]}…  enabled={st['enabled']}  роль: {len(mem)} адр.  anySilo={(st['any_silo'] or '?')[:12]}…")
            for w in ARB_TOP_WINNERS:
                hr = has_role(rpc, ctrl, st.get("role", ALLOWED_ROLE), w)
                if hr:
                    print(f"       наш топ-победитель {w[:12]}… ∈ РОЛИ этого контроллера")
        print("\nИтог: " + ("есть ВКЛЮЧЁННЫЕ контроллеры — часть рынков Arbitrum запирается вайтлистом; наши топ-победители "
                             "дерутся на оставшихся открытых." if any_enabled else
                             "все контроллеры выключены/не читаются — измеренная нами гонка на Arbitrum была реальной."))


if __name__ == "__main__":
    main()
SILO_EOF
cd "$DIR"
python3 -m py_compile analysis/permission_check.py && echo "[OK] py_compile"
python3 - << 'PY_TEST'
import analysis.permission_check as pc
from collections import Counter
W=lambda a:"0"*24+a[2:].lower(); ROLE=pc.ALLOWED_ROLE[2:]
class Mock:
    def __init__(s):
        c=pc.SONIC_CONTROLLER; wl=pc.SONIC_BATCH_WL; s.map={}
        s.map[(c,pc.SEL_PERM_DATA)]="0x"+W("0x"+"11"*20)+"0"*63+"1"+"0"*64
        s.map[(c,pc.SEL_OWNER)]="0x"+W("0xe8e8041cb5e3158a0829a19e014ca1cf91098554")
        s.map[(c,pc.SEL_ROLE_GETTER)]="0x"+ROLE
        s.map[(c,pc.SEL_MEMBER_CNT+ROLE)]="0x"+"0"*63+"2"
        s.map[(c,pc.SEL_MEMBER_AT+ROLE+"0"*64)]="0x"+W(wl[0])
        s.map[(c,pc.SEL_MEMBER_AT+ROLE+"0"*63+"1")]="0x"+W(wl[3])
        s.map[(c,pc.SEL_HAS_ROLE+ROLE+W(wl[0]))]="0x"+"0"*63+"1"
        s.map[("0x"+"11"*20,pc.SEL_CONFIG)]="0x"+W(pc.SONIC_GATED_CONFIG)
    def eth_call(s,to,data):
        v=s.map.get((to.lower(),data))
        if v is None: raise pc.RpcError("revert")
        return v
m=Mock(); st=pc.controller_state(m,pc.SONIC_CONTROLLER)
assert st["ok"] and st["enabled"] is True and st["members"]==[pc.SONIC_BATCH_WL[0],pc.SONIC_BATCH_WL[3]]
assert pc.has_role(m,pc.SONIC_CONTROLLER,pc.ALLOWED_ROLE,pc.SONIC_BATCH_WL[0]) is True
c={}; assert pc.silo_config_of(m,"0x"+"11"*20,c)==pc.SONIC_GATED_CONFIG
v=pc.verdict_sonic(dict(st,members=[pc.SONIC_BATCH_WL[0]]),{"gated":[("0x"+"11"*20,True,Counter({pc.SONIC_BATCH_WL[0]:10}))]})
assert "ЗАПЕРТ ВАЙТЛИСТОМ" in v
assert "НЕ активна" in pc.verdict_sonic(dict(st,enabled=False),{})
print("[OK] тесты: state/роль/config/вердикты — прошли")
PY_TEST
echo ">> permission_check.py установлен и проверен."
