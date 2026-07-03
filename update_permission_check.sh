#!/usr/bin/env bash
# Обновление permission_check.py → v2 (generic probe, верные адреса hook/controller, разметка рынков).
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: это Morpho-бот"; exit 1; }
[ -e "$DIR/chain/morpho.py" ] && { echo "СТОП: Morpho-файлы в $DIR"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py"; exit 1; }
cat > "$DIR/analysis/permission_check.py" << 'SILO_EOF'
#!/usr/bin/env python3
"""permission_check.py v2 — ончейн-правда о вайтлист-стене Silo на Sonic/Arbitrum. (read-only)

Уточнённая карта (из v3_markets + Safe-батчей + broadcast-журналов репо Silo):
  • БОЛЬШОЙ рынок Sonic (97% денег): id 20, config 0x062a36bb…, пара wS/USDC, хук LEGACY
    (0x6aafd9dd…, owner-Safe 0x7461d8c0…). Деплой PermissionedLiquidationController на него УПАЛ
    (exitCode 1) — legacy-хук механизм не поддерживает → рынок структурно НЕ гейтится этой системой.
  • Гейтируемый рынок: id 3001, config 0x180a8ff4…, пара wS/USSD, хук SiloHookV2 = 0xebb2677d…
    (owner-Safe 0xe8e8041c…). Его контроллер = 0x03058027… (Set-Gauge-батч: setGauge → setEnabled(true)
    → 4×grantRole ALLOWED_ROLE). Отдельный батч 13-мая грантит роль ещё и НА САМОМ хуке (3 адреса).
  ⇒ v1 этого скрипта ошибочно звал permisionedData() на ХУКЕ — отсюда «не читается». Исправлено:
    generic-probe различает контроллер/хук по факту, читает живой состав роли у обоих.

Что печатает:
  1. Для каждого probe-адреса: есть ли код; контроллер (enabled/anySilo→config) или хук; живой ALLOWED_ROLE.
  2. Эмпирика (Sonic): силосы за --days, помечены config'ом (БОЛЬШОЙ wS/USDC vs гейтируемый wS/USSD vs прочие),
     полная таблица победителей большого силоса — кто забирает 15% мимо инкумбента.
  3. Вердикт по фактам.

Запуск:
  python3 -m analysis.permission_check --rpc https://rpc.soniclabs.com --chain sonic --days 7
  python3 -m analysis.permission_check --rpc "$ARB_RPC" --chain arbitrum      # state-only probe
"""
from __future__ import annotations
import argparse
import sys
from collections import defaultdict, Counter

from analysis.contestation import RPC, RpcError, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts

ALLOWED_ROLE = "0xd5dc6b389d0dd5687ab5bd9338f760ebeaff2d2852a93a9a9ebaebbfefc763ac"  # keccak("ALLOWED_ROLE")
SEL_PERM_DATA   = "0x421f5b13"  # permisionedData() (контроллер)
SEL_CONFIG      = "0x79502c55"  # ISilo.config()
SEL_ROLE_GETTER = "0xd32f7154"  # ALLOWED_ROLE()
SEL_MEMBER_CNT  = "0xca15c873"  # getRoleMemberCount(bytes32)
SEL_MEMBER_AT   = "0x9010d07c"  # getRoleMember(bytes32,uint256)
SEL_OWNER       = "0x8da5cb5b"  # owner()

# ---- Sonic: карта из репо Silo ----
SONIC_BIG_CONFIG   = "0x062a36bbe0306c2fd7aecdf25843291fbab96ad2"  # wS/USDC, БОЛЬШОЙ, legacy-хук
SONIC_GATED_CONFIG = "0x180a8ff4c343b6eea2ff0feb2dcc92a360bf7025"  # wS/USSD, SiloHookV2
SONIC_PROBES = [
    ("hook wS/USSD (SiloHookV2)",  "0xebb2677d5a5ff045b7e34f514cc2f353932833a3"),
    ("controller wS/USSD",         "0x03058027fd20dbf4ebf04ecdd63683b32542cbf4"),
    ("hook wS/USDC (legacy)",      "0x6aafd9dd424541885fd79c06fda96929cfd512f9"),
]
SONIC_KNOWN_WL = {  # адреса из Safe-батчей (для тегов; живой состав читаем с чейна)
    "0x0094c5d6b460a2efff23420db097921bcc2b2a2c": "инкумбент",
    "0x08a52ec31e0e981bbd64082224185e420d3f9849": "батч",
    "0xb49329bf1d95d51681f4e4f644eb37f58e398abd": "батч",
    "0xccd487e01e9df6932f656b53668f58005f604417": "батч/№4",
    "0x1ff60e85852ac73cd05b69a8b6641fc24a3fc011": "hook-батч 13.05",
    "0xc04f84a02cc65f14f4e8c982a7a467ee88c5311e": "hook-батч 13.05",
    "0xd3ec1026c9f911e201de4d52a667dc10bc3754d7": "hook-батч 13.05",
}
MARKET_LABEL = {SONIC_BIG_CONFIG: "БОЛЬШОЙ wS/USDC (legacy-хук, не гейтится)",
                SONIC_GATED_CONFIG: "wS/USSD (гейтируемый)"}

# ---- Arbitrum: адреса из вайтлист-батчей (возможно, ХУКИ) + инстансы из broadcast run-latest ----
ARB_PROBES = [
    ("батч", "0x15d3ebe0317cd2f0db0a6f4780c29c43c93a1003"), ("батч", "0x2c7d9b9823bbcc63e52de90db6f3fb10679789ae"),
    ("батч", "0x95626c763a2c9fdf3e8a4e6644f2b9bcdb67097a"), ("батч", "0xaf45c4f4b0239a20157eda4069c283cb8c7d6af2"),
    ("батч", "0xf3c69312f020eaaa9e0a60295549820a77b5cab9"), ("батч", "0x2174557e5ed2e8256284a5df42a91b21db6313a9"),
    ("батч", "0x97dc5ea7a2d98599534655535e101e2f35bbcd69"),
    ("broadcast", "0x02d6d1d6c534a8031ef442d20e3c9ccd0e47b8a5"), ("broadcast", "0xca31cdb2a1ef693279774176fa2b38adb600082e"),
    ("broadcast", "0x97c881098038bb7876f9bfe8ab2d7560e6df86da"), ("broadcast", "0x0ad6a039fe5a659cd3a430319316cea4f35aa88e"),
]
ARB_TOP_WINNERS = ["0x879c2a2f7e4071ebdc971e508885d4a8cdeaf227","0x823de6b63f9cb010cbb58951c90eea30bf02bd36",
                   "0xc8c2ab457ecb26ea47018490ffa0b2cb2646a7ef","0x4b2b41dfbf77d3fd332a4d572706373376675b69",
                   "0x0665609124cc2a958cf0ed582ee132076243b6da"]


def pad32(h: str) -> str:
    h = h.lower().replace("0x", "")
    return "0" * (64 - len(h)) + h


def call(rpc: RPC, to: str, data: str):
    try:
        r = rpc.eth_call(to, data)
        return r if r and r != "0x" else None
    except (RpcError, RuntimeError):
        return None


def dec_addr(word: str) -> str:
    return "0x" + word[-40:].lower()


def get_code_size(rpc: RPC, addr: str) -> int:
    try:
        code = rpc.call("eth_getCode", [addr, "latest"]) or "0x"
        return max(0, (len(code) - 2) // 2)
    except (RpcError, RuntimeError):
        return -1  # RPC-проблема, не «нет кода»


def role_members(rpc: RPC, addr: str) -> list:
    role_ret = call(rpc, addr, SEL_ROLE_GETTER)
    role = ("0x" + role_ret[2:66]) if role_ret and len(role_ret) >= 66 else ALLOWED_ROLE
    cnt_ret = call(rpc, addr, SEL_MEMBER_CNT + pad32(role))
    if cnt_ret is None:
        return None
    members = []
    for i in range(min(int(cnt_ret, 16), 32)):
        m = call(rpc, addr, SEL_MEMBER_AT + pad32(role) + pad32(hex(i)))
        if m:
            members.append(dec_addr(m))
    return members


def probe(rpc: RPC, addr: str) -> dict:
    """Generic: код? контроллер (permisionedData) или хук/прочее; живой состав ALLOWED_ROLE; owner."""
    out = {"addr": addr, "code": get_code_size(rpc, addr), "kind": "?", "enabled": None,
           "any_silo": None, "members": None, "owner": None}
    if out["code"] <= 0:
        out["kind"] = "нет кода" if out["code"] == 0 else "RPC-ошибка"
        return out
    pd = call(rpc, addr, SEL_PERM_DATA)
    if pd and len(pd) >= 2 + 64 * 3:
        h = pd[2:]
        out["kind"] = "controller"
        out["any_silo"] = dec_addr(h[0:64])
        out["enabled"] = int(h[64:128], 16) != 0
    else:
        out["kind"] = "hook/прочее"
    own = call(rpc, addr, SEL_OWNER)
    out["owner"] = dec_addr(own) if own else None
    out["members"] = role_members(rpc, addr)
    return out


def silo_config_of(rpc: RPC, silo: str, cache: dict):
    if silo in cache:
        return cache[silo]
    ret = call(rpc, silo, SEL_CONFIG)
    cfg = dec_addr(ret) if ret else None
    cache[silo] = cfg
    return cfg


def gather_empirics(rpc: RPC, days: float):
    tip = rpc.block_number()
    frm = find_block_at_ts(rpc, rpc.block_ts(tip) - int(days * 86400), tip)
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


def fmt_probe(p: dict) -> str:
    base = f"код={p['code']}б  вид={p['kind']}"
    if p["kind"] == "controller":
        base += f"  enabled={p['enabled']}  anySilo={(p['any_silo'] or '?')[:12]}…"
    if p["owner"]:
        base += f"  owner={p['owner'][:12]}…"
    return base


def print_members(members, indent="     "):
    if members is None:
        print(indent + "роль не читается")
        return
    print(indent + f"живой ALLOWED_ROLE: {len(members)} адр.")
    for m in members:
        tag = SONIC_KNOWN_WL.get(m, "")
        print(indent + f"  {m}" + (f"   ({tag})" if tag else ""))


def main():
    ap = argparse.ArgumentParser(description="Silo permissioned-liquidation on-chain check v2")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--chain", default="sonic", choices=["sonic", "arbitrum"])
    ap.add_argument("--days", type=float, default=7.0)
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    if a.chain == "sonic":
        print("=" * 74)
        print("  PERMISSION CHECK v2 — Silo V2 — SONIC")
        print("=" * 74)
        probes = {}
        for label, addr in SONIC_PROBES:
            p = probe(rpc, addr)
            probes[addr] = p
            print(f"[{label}] {addr[:12]}…  {fmt_probe(p)}")
            print_members(p["members"])
        ctrl = probes.get("0x03058027fd20dbf4ebf04ecdd63683b32542cbf4", {})
        hook = probes.get("0xebb2677d5a5ff045b7e34f514cc2f353932833a3", {})

        sys.stderr.write(f"\nэмпирика: ликвидации за {a.days:g}д…\n")
        events, by_silo = gather_empirics(rpc, a.days)
        cfg_cache = {}
        print(f"\nликвидаций за окно: {len(events)}  |  силосов: {len(by_silo)}")
        big_rows = []
        for silo, winners in sorted(by_silo.items(), key=lambda kv: -sum(kv[1].values())):
            cfg = silo_config_of(rpc, silo, cfg_cache)
            label = MARKET_LABEL.get(cfg, "прочий")
            tot = sum(winners.values())
            print(f"\n  silo {silo}  config={(cfg or 'не читается')[:12]}…  [{label}]  ликв={tot}")
            for w, n in winners.most_common(8):
                tag = SONIC_KNOWN_WL.get(w, "")
                print(f"     {w}  {n:4d} ({n/tot*100:4.1f}%)" + (f"  [{tag}]" if tag else ""))
            if cfg == SONIC_BIG_CONFIG:
                big_rows.append((silo, winners))

        print("\n" + "=" * 74)
        print("  ВЕРДИКТ")
        print("=" * 74)
        gate_on = bool(ctrl.get("enabled")) or bool(hook.get("members"))
        print(f"• Гейтируемый рынок wS/USSD: controller enabled={ctrl.get('enabled')}, "
              f"роль(контроллер)={len(ctrl.get('members') or []) if ctrl.get('members') is not None else '—'}, "
              f"роль(хук)={len(hook.get('members') or []) if hook.get('members') is not None else '—'} "
              f"→ вайтлист-механизм {'ЖИВ' if gate_on else 'не активен'} (рынок мелкий, ликвидаций за окно почти нет).")
        if big_rows:
            silo, winners = big_rows[0]
            tot = sum(winners.values())
            inc = winners.get("0x0094c5d6b460a2efff23420db097921bcc2b2a2c", 0)
            others = tot - inc
            print(f"• БОЛЬШОЙ wS/USDC: хук LEGACY → пермишен-система НЕ применяется. Это НАСТОЯЩАЯ гонка.")
            print(f"  Инкумбент {inc}/{tot} ({inc/tot*100:.0f}%), мимо него утекает {others} ({others/tot*100:.0f}%) — таблица выше.")
            print(f"  ⇒ Скоростной контест на большом силосе ЖИВ; paper-план осмыслен. Дверь-вайтлист остаётся")
            print(f"    запасной (курс Silo подтверждён), но для главного приза сейчас не нужна.")
        else:
            print("• Большой wS/USDC не идентифицирован по config за окно — пришли вывод, сверю адреса.")
    else:
        print("=" * 74)
        print("  PERMISSION CHECK v2 — ARBITRUM — probe контроллеров/хуков (state-only)")
        print("=" * 74)
        any_live = False
        for src, addr in ARB_PROBES:
            p = probe(rpc, addr)
            print(f"[{src}] {addr[:12]}…  {fmt_probe(p)}")
            if p["members"]:
                any_live = True
                print_members(p["members"])
                for w in ARB_TOP_WINNERS:
                    if w in (p["members"] or []):
                        print(f"       наш топ-победитель {w[:12]}… ∈ роли")
        print("\nИтог: " + ("часть адресов живая — сверь, какие рынки они держат (anySilo→config)." if any_live
              else "живых вайтлистов не видно — измеренная гонка на Arbitrum реальна."))


if __name__ == "__main__":
    main()
SILO_EOF
cd "$DIR"
python3 -m py_compile analysis/permission_check.py && echo "[OK] py_compile"
python3 - << 'PY_TEST'
import analysis.permission_check as pc
W=lambda a:"0"*24+a[2:].lower(); ROLE=pc.ALLOWED_ROLE[2:]
CTRL="0x03058027fd20dbf4ebf04ecdd63683b32542cbf4"; HOOK="0xebb2677d5a5ff045b7e34f514cc2f353932833a3"
class Mock:
    def __init__(s):
        s.m={("code",HOOK):"0x"+"60"*400,("code",CTRL):"0x"+"60"*300,("code","0x"+"aa"*20):"0x"}
        s.m[(HOOK,pc.SEL_ROLE_GETTER)]="0x"+ROLE
        s.m[(HOOK,pc.SEL_MEMBER_CNT+ROLE)]="0x"+"0"*63+"2"
        s.m[(HOOK,pc.SEL_MEMBER_AT+ROLE+"0"*64)]="0x"+W("0x1ff60e85852ac73cd05b69a8b6641fc24a3fc011")
        s.m[(HOOK,pc.SEL_MEMBER_AT+ROLE+"0"*63+"1")]="0x"+W("0x0094c5d6b460a2efff23420db097921bcc2b2a2c")
        s.m[(HOOK,pc.SEL_OWNER)]="0x"+W("0xe8e8041cb5e3158a0829a19e014ca1cf91098554")
        s.m[(CTRL,pc.SEL_PERM_DATA)]="0x"+W("0x"+"11"*20)+"0"*63+"1"+"0"*64
        s.m[(CTRL,pc.SEL_ROLE_GETTER)]="0x"+ROLE
        s.m[(CTRL,pc.SEL_MEMBER_CNT+ROLE)]="0x"+"0"*63+"1"
        s.m[(CTRL,pc.SEL_MEMBER_AT+ROLE+"0"*64)]="0x"+W("0x0094c5d6b460a2efff23420db097921bcc2b2a2c")
        s.m[("0x"+"22"*20,pc.SEL_CONFIG)]="0x"+W(pc.SONIC_BIG_CONFIG)
    def call(s,m,p):
        if m=="eth_getCode": return s.m[("code",p[0])]
        raise pc.RpcError("nope")
    def eth_call(s,to,data):
        v=s.m.get((to,data))
        if v is None: raise pc.RpcError("revert")
        return v
m=Mock()
ph=pc.probe(m,HOOK); assert ph["kind"]=="hook/прочее" and len(ph["members"])==2
pctl=pc.probe(m,CTRL); assert pctl["kind"]=="controller" and pctl["enabled"] is True
assert pc.probe(m,"0x"+"aa"*20)["kind"]=="нет кода"
c={}; assert pc.silo_config_of(m,"0x"+"22"*20,c)==pc.SONIC_BIG_CONFIG
print("[OK] тесты v2: probe hook/controller/нет-кода + config-разметка — прошли")
PY_TEST
echo ">> permission_check.py v2 установлен."
