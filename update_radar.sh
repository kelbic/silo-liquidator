#!/usr/bin/env bash
# Обновление radar.py: фильтр контестабельных сетей (Ethereum-аукцион исключён по умолчанию).
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: это Morpho-бот"; exit 1; }
[ -e "$DIR/chain/morpho.py" ] && { echo "СТОП: Morpho-файлы в $DIR"; exit 1; }
[ -d "$DIR/radar" ] || { echo "СТОП: нет $DIR/radar — прогони install_radar.sh"; exit 1; }
cat > "$DIR/radar/radar.py" << 'SILO_EOF'
#!/usr/bin/env python3
"""radar.py — РАДАР молодых lending-рынков, где хвост ликвидаций ещё ОТКРЫТ (read-only, stdlib + Telegram).
Раз в сутки: DefiLlama /protocols → фильтр Lending + КОНТЕСТАБЕЛЬНАЯ СЕТЬ + молодой/растущий + пул>порога →
Telegram-алерт, дедуп в SQLite. Ethereum исключён по умолчанию (аукцион). Точную контестабельность даёт x-ray.
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import datetime as dt

LLAMA_PROTOCOLS = "https://api.llama.fi/protocols"
DAY = 86400

# Сети, где соло-контест реалистичен: FCFS-латентность или priority-fee L2, дешёвый газ.
# Ethereum ИСКЛЮЧЁН по умолчанию: публичный mempool + MEV-Boost, профи-билдеры, дорогой газ —
# окно новизны там мгновенно занимают те же searcher'ы, латентностью не отыграть. (--all-chains чтобы вкл.)
CONTESTABLE_CHAINS = {
    "arbitrum", "base", "optimism", "op mainnet", "sonic",
    "blast", "mode", "scroll", "linea", "mantle", "zksync era", "zksync",
    "fraxtal", "ink", "unichain", "soneium", "polygon zkevm", "taiko",
}


def contestable_of(p: dict, allowed: set) -> list:
    """Сети протокола, попадающие в контестабельный набор (lower-case матч)."""
    return [c for c in (p.get("chains") or []) if str(c).lower() in allowed]


def http_get_json(url: str, timeout: float = 60.0):
    req = urllib.request.Request(url, headers={"User-Agent": "silo-radar/1.0", "Accept": "application/json"})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"GET {url} не удался после ретраев: {last}")


def age_days(listed_at) -> float:
    """Возраст протокола в днях по listedAt (unix). None → большой возраст (не считаем молодым)."""
    try:
        if not listed_at:
            return 1e9
        return (time.time() - float(listed_at)) / DAY
    except (TypeError, ValueError):
        return 1e9


def num(x) -> float:
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def estimate_pool_usd(tvl: float, pool_factor: float) -> float:
    """Грубая оценка месячного нетто-пула бонусов ликвидаторов. tvl × factor. НЕ точное число."""
    return max(0.0, tvl) * pool_factor


def is_candidate(p: dict, cfg) -> tuple:
    """Возвращает (bool, pool_usd, reason). Кандидат = категория + контестабельная сеть + (молодой ИЛИ растёт) + TVL>=пол + пул>=порог."""
    cat = (p.get("category") or "")
    if cat not in cfg.categories:
        return (False, 0.0, "категория")
    cc = (p.get("chains") or []) if cfg.all_chains else contestable_of(p, cfg.contestable_chains)
    if not cc:
        return (False, 0.0, "нет контестабельной сети")
    tvl = num(p.get("tvl"))
    if tvl < cfg.min_tvl:
        return (False, 0.0, "мал TVL")
    a = age_days(p.get("listedAt"))
    growth_1m = num(p.get("change_1m"))
    young = a <= cfg.max_age_days
    growing = growth_1m >= cfg.growth_pct
    if not (young or growing):
        return (False, 0.0, "не молодой и не растёт")
    pool = estimate_pool_usd(tvl, cfg.pool_factor)
    if pool < cfg.min_pool:
        return (False, pool, "пул ниже порога")
    why = []
    if young:
        why.append(f"молодой {a:.0f}д")
    if growing:
        why.append(f"+{growth_1m:.0f}%/мес TVL")
    why.append("сеть: " + ",".join(cc[:4]))
    return (True, pool, " · ".join(why))


def telegram_send(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "silo-radar/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            obj = json.loads(r.read())
        return bool(obj.get("ok"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        sys.stderr.write(f"Telegram ошибка: {e}\n")
        return False


def db_init(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, last_ts INTEGER, tvl REAL, pool REAL)")
    con.commit()
    return con


def should_alert(con: sqlite3.Connection, key: str, realert_days: float) -> bool:
    row = con.execute("SELECT last_ts FROM alerts WHERE key=?", (key,)).fetchone()
    if not row:
        return True
    return (time.time() - row[0]) >= realert_days * DAY


def record_alert(con: sqlite3.Connection, key: str, tvl: float, pool: float):
    con.execute("INSERT INTO alerts(key,last_ts,tvl,pool) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET last_ts=excluded.last_ts, tvl=excluded.tvl, pool=excluded.pool",
                (key, int(time.time()), tvl, pool))
    con.commit()


def format_alert(p: dict, pool: float, reason: str) -> str:
    chains = ", ".join(p.get("chains") or [])[:120]
    tvl = num(p.get("tvl"))
    slug = p.get("slug") or p.get("name")
    cat = p.get("category") or "?"
    note = "\n⚠️ CDP: проверь механику ликвидаций (аукцион/stability-pool/soft-liq — часто НЕ контестится)." if cat == "CDP" else ""
    return (f"🛰️ *Радар: молодой {cat}-рынок*\n"
            f"*{p.get('name')}*  ({chains})\n"
            f"TVL ${tvl:,.0f}  |  {reason}\n"
            f"Оценка нетто-пула: *~${pool:,.0f}/мес* (грубо, TVL×factor)\n"
            f"Порог пройден → проверь ХВОСТ (открыт ли):\n"
            f"`# наведи contestation/x-ray на {slug} на нужной сети`\n"
            f"DefiLlama: https://defillama.com/protocol/{slug}{note}")


def main():
    ap = argparse.ArgumentParser(description="Радар молодых lending-рынков с открытым хвостом ликвидаций")
    ap.add_argument("--telegram-token", help="токен Telegram-бота (для алертов)")
    ap.add_argument("--chat-id", help="chat_id получателя")
    ap.add_argument("--db", default="radar.db", help="путь к SQLite дедупа")
    ap.add_argument("--categories", default="Lending", help="через запятую: Lending,CDP (perps по умолч. нет)")
    ap.add_argument("--contestable-chains", default=",".join(sorted(CONTESTABLE_CHAINS)),
                    help="сети, где контест реалистичен (Ethereum по умолч. НЕ входит)")
    ap.add_argument("--all-chains", action="store_true", help="не фильтровать по сети (вкл. Ethereum-аукцион)")
    ap.add_argument("--min-tvl", type=float, default=5_000_000, help="минимальный TVL, $")
    ap.add_argument("--max-age-days", type=float, default=270, help="'молодой' = моложе стольких дней")
    ap.add_argument("--growth-pct", type=float, default=40.0, help="или рост TVL за месяц >= %, чтобы считать кандидатом")
    ap.add_argument("--min-pool", type=float, default=15_000, help="порог оценочного нетто-пула, $/мес")
    ap.add_argument("--pool-factor", type=float, default=0.0002,
                    help="пул ≈ TVL×factor. Дефолт ≈ 0.5%%/мес оборота × 4%% нетто. Калибруй по замерам.")
    ap.add_argument("--realert-days", type=float, default=30, help="не будить один рынок чаще, чем раз в N дн")
    ap.add_argument("--dry-run", action="store_true", help="печать, без Telegram и без записи в БД")
    ap.add_argument("--top", type=int, default=30, help="сколько кандидатов показать в консоли")
    a = ap.parse_args()
    a.categories = {c.strip() for c in a.categories.split(",") if c.strip()}
    a.contestable_chains = {c.strip().lower() for c in a.contestable_chains.split(",") if c.strip()}

    import os
    a.telegram_token = a.telegram_token or os.environ.get("RADAR_TELEGRAM_TOKEN")
    a.chat_id = a.chat_id or os.environ.get("RADAR_CHAT_ID")

    if not a.dry_run and not (a.telegram_token and a.chat_id):
        sys.exit("Нужны --telegram-token и --chat-id (или env RADAR_TELEGRAM_TOKEN/RADAR_CHAT_ID), либо --dry-run.")

    sys.stderr.write("тяну список протоколов с DefiLlama…\n")
    protos = http_get_json(LLAMA_PROTOCOLS)
    if not isinstance(protos, list):
        sys.exit("Неожиданный ответ DefiLlama (ожидался список протоколов).")

    cands = []
    hidden_chain = []
    for p in protos:
        ok, pool, reason = is_candidate(p, a)
        if ok:
            cands.append((p, pool, reason))
        elif reason == "нет контестабельной сети" and not a.all_chains:
            import copy
            a2 = copy.copy(a); a2.all_chains = True
            ok2, pool2, _ = is_candidate(p, a2)
            if ok2:
                hidden_chain.append((p, pool2))
    cands.sort(key=lambda t: t[1], reverse=True)

    print(f"\n=== РАДАР: кандидатов {len(cands)} из {len(protos)} протоколов "
          f"(категории {sorted(a.categories)}, порог пула ${a.min_pool:,.0f}/мес, "
          f"{'ВСЕ сети' if a.all_chains else 'только контестабельные сети'}) ===")
    for p, pool, reason in cands[:a.top]:
        chains = ",".join(p.get("chains") or [])
        print(f"  {p.get('name'):22.22s} {p.get('category'):8.8s} TVL ${num(p.get('tvl')):>13,.0f}  "
              f"пул~${pool:>9,.0f}/мес  [{reason}]")
    if hidden_chain:
        hidden_chain.sort(key=lambda t: t[1], reverse=True)
        print(f"\n  скрыто как неконтестабельные по сети (обычно Ethereum-аукцион): {len(hidden_chain)} "
              f"— показать: --all-chains")
        for p, pool in hidden_chain[:5]:
            print(f"    · {p.get('name'):22.22s} пул~${pool:>9,.0f}/мес  ({','.join(p.get('chains') or [])})")

    if a.dry_run:
        print("\n(dry-run: Telegram и запись в БД пропущены)")
        return

    con = db_init(a.db)
    sent = 0
    for p, pool, reason in cands:
        key = f"{p.get('slug') or p.get('name')}"
        if not should_alert(con, key, a.realert_days):
            continue
        if telegram_send(a.telegram_token, a.chat_id, format_alert(p, pool, reason)):
            record_alert(con, key, num(p.get("tvl")), pool)
            sent += 1
            time.sleep(0.5)
    print(f"\nОтправлено алертов: {sent} (остальные кандидаты уже алертились в пределах {a.realert_days:g}д)")


if __name__ == "__main__":
    main()
SILO_EOF
cd "$DIR/radar"
python3 -m py_compile radar.py && echo "[OK] py_compile"
python3 - << 'PY_TEST'
import types, time, radar
def cfg(**kw):
    b=dict(categories={"Lending"},contestable_chains=set(radar.CONTESTABLE_CHAINS),all_chains=False,
           min_tvl=5_000_000,max_age_days=270,growth_pct=40.0,min_pool=15_000,pool_factor=0.0002); b.update(kw); return types.SimpleNamespace(**b)
now=time.time()
bp={"name":"X","category":"Lending","tvl":100_000_000,"listedAt":now-100*86400,"change_1m":5,"slug":"x"}
ok,pool,why=radar.is_candidate(dict(bp,chains=["Base"]),cfg()); assert ok and pool==20000 and "сеть: Base" in why
assert radar.is_candidate(dict(bp,chains=["Ethereum"]),cfg())[2]=="нет контестабельной сети"
assert radar.is_candidate(dict(bp,chains=["Ethereum","Sonic"]),cfg())[0]
assert radar.is_candidate(dict(bp,chains=["Ethereum"]),cfg(all_chains=True))[0]
assert radar.contestable_of({"chains":["Ethereum","Base","Sonic"]},set(radar.CONTESTABLE_CHAINS))==["Base","Sonic"]
assert not radar.is_candidate(dict(bp,chains=["Base"],tvl=1_000_000),cfg())[0]
con=radar.db_init(":memory:"); assert radar.should_alert(con,"x",30); radar.record_alert(con,"x",1,1); assert not radar.should_alert(con,"x",30)
print("[OK] тесты: фильтр сети (Ethereum режется, мультичейн/--all-chains) + регрессия — прошли")
PY_TEST
echo ">> radar.py обновлён (фильтр сетей). Проверь: python3 radar.py --dry-run"
