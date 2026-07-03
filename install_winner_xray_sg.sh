#!/usr/bin/env bash
# Установка winner_xray_sg.py (Arbitrum x-ray через сабграф). Read-only, stdlib. Боты не трогает.
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: это Morpho-бот"; exit 1; }
[ -e "$DIR/chain/morpho.py" ] && { echo "СТОП: Morpho-файлы в $DIR"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py — прогони install_contestation.sh"; exit 1; }
[ -f "$DIR/analysis/winner_xray.py" ]  || { echo "СТОП: нет winner_xray.py — прогони install_winner_xray.sh"; exit 1; }
mkdir -p "$DIR/analysis"
cat > "$DIR/analysis/winner_xray_sg.py" << 'SILO_EOF'
#!/usr/bin/env python3
"""winner_xray_sg.py — Arbitrum x-ray ЧЕРЕЗ САБГРАФ (обходит 10-блочный лимит eth_getLogs).
Источник: Silo Arbitrum subgraph (Messari). Liquidate.liquidator=победитель, .liquidatee=заёмщик,
hash/blockNumber/timestamp/amountUSD. Timeboost добираем поштучно через eth_getTransactionReceipt (Alchemy).
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from collections import Counter, defaultdict

from analysis.contestation import RPC, RpcError, winner_stats
from analysis.winner_xray import get_receipt, position_bucket, _require_rpc

ARBITRUM_SILO_SUBGRAPH_ID = "2ufoztRpybsgogPVW6j9NTn1JmBWFYPKbP7pAabizADU"
DEFAULT_GATEWAY = "https://gateway.thegraph.com/api/{key}/subgraphs/id/{sid}"

LIQ_QUERY = """
query($first:Int!,$lt:BigInt!){
  liquidates(first:$first, orderBy:timestamp, orderDirection:desc, where:{timestamp_lt:$lt}){
    hash blockNumber timestamp amountUSD profitUSD
    liquidator{id} liquidatee{id} asset{symbol} market{id}
  }
}"""

INTROSPECT_QUERY = 'query{ __type(name:"Query"){ fields{ name } } }'


def graphql(url: str, query: str, variables: dict = None, timeout: float = 40.0) -> dict:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json", "User-Agent": "silo-xray/1.0"})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                obj = json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:300]
            except Exception:  # noqa
                pass
            if e.code in (401, 403):
                sys.exit(f"Сабграф: HTTP {e.code} — ключ The Graph отвергнут или нет доступа. {body}")
            last = e
            time.sleep(0.8 * (attempt + 1))
            continue
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last = e
            time.sleep(0.8 * (attempt + 1))
            continue
        if obj.get("errors"):
            raise RpcError(f"GraphQL errors: {json.dumps(obj['errors'])[:400]}")
        return obj.get("data") or {}
    raise RuntimeError(f"Сабграф не отвечает после ретраев: {last}")


def diagnose_schema(url: str, err: Exception):
    """Если основной запрос упал — интроспектируем корень Query и подсказываем реальные имена полей."""
    sys.stderr.write(f"\nОсновной запрос к сабграфу не прошёл: {err}\n")
    try:
        data = graphql(url, INTROSPECT_QUERY)
        fields = [f["name"] for f in (data.get("__type") or {}).get("fields", [])]
        liq = [f for f in fields if "liquidat" in f.lower()]
        sys.stderr.write(f"Поля корня Query, похожие на ликвидации: {liq or '—'}\n")
        sys.stderr.write("Если имя не 'liquidates' — пришли этот список, поправлю запрос под схему.\n")
    except Exception as e:  # noqa
        sys.stderr.write(f"Интроспекция тоже не удалась: {e}\n")


def fetch_liquidations(url: str, limit: int) -> list:
    """Последние `limit` Liquidate по убыванию времени; курсор по timestamp_lt (обход лимита first/skip)."""
    out = []
    cursor = str(2 ** 63)  # заведомо больше любого ts
    while len(out) < limit:
        want = min(1000, limit - len(out))
        try:
            data = graphql(url, LIQ_QUERY, {"first": want, "lt": cursor})
        except RpcError as e:
            diagnose_schema(url, e)
            raise
        batch = data.get("liquidates") or []
        if not batch:
            break
        out.extend(batch)
        cursor = str(int(batch[-1]["timestamp"]))
        sys.stderr.write(f"\r  сабграф: ликвидаций получено {len(out)}   ")
        sys.stderr.flush()
        if len(batch) < want:
            break
    sys.stderr.write("\n")
    return out


def block_txcount(rpc: RPC, num: int, cache: dict) -> int:
    if num in cache:
        return cache[num]
    b = rpc.call("eth_getBlockByNumber", [hex(num), False]) or {}
    n = len(b.get("transactions") or []) or 1
    cache[num] = n
    return n


def _usd(x) -> float:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _winner(e: dict) -> str:
    return ((e.get("liquidator") or {}).get("id") or "").lower()


def main():
    ap = argparse.ArgumentParser(description="Silo Arbitrum winner x-ray via subgraph + Alchemy receipts")
    ap.add_argument("--subgraph-url", help="полный URL сабграфа с ключом (кнопка Query в Explorer)")
    ap.add_argument("--graph-key", help="ключ The Graph (если не задан --subgraph-url)")
    ap.add_argument("--subgraph-id", default=ARBITRUM_SILO_SUBGRAPH_ID)
    ap.add_argument("--rpc", required=True, help="Alchemy Arbitrum RPC (для receipts/timeboosted)")
    ap.add_argument("--limit", type=int, default=1000, help="сколько последних ликвидаций для концентрации")
    ap.add_argument("--sample", type=int, default=150, help="для скольких свежих читать receipts/timeboosted")
    ap.add_argument("--min-usd", type=float, default=0.0)
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args()

    if a.subgraph_url:
        sg_url = a.subgraph_url
    elif a.graph_key:
        sg_url = DEFAULT_GATEWAY.format(key=a.graph_key, sid=a.subgraph_id)
    else:
        sys.exit("Нужен --graph-key ИЛИ --subgraph-url. Ключ: thegraph.com/studio → API Keys.")

    _require_rpc(a.rpc)
    rpc = RPC(a.rpc)

    sys.stderr.write("тяну ликвидации из сабграфа Silo Arbitrum…\n")
    liqs = fetch_liquidations(sg_url, a.limit)
    if not liqs:
        sys.exit("Сабграф вернул 0 ликвидаций. Проверь ключ/URL/сеть (Arbitrum subgraph id) "
                 "или сабграф пуст за индексированный период.")

    for e in liqs:
        e["_usd"] = _usd(e.get("amountUSD"))
    if a.min_usd > 0:
        big = [e for e in liqs if e["_usd"] is None or e["_usd"] >= a.min_usd]
    else:
        big = liqs

    ws = winner_stats([_winner(e) for e in big if _winner(e)])
    incumbent = ws["ranked"][0][0] if ws["ranked"] else None

    big.sort(key=lambda e: int(e["blockNumber"]), reverse=True)
    sample = big[:a.sample]
    sys.stderr.write(f"вскрываю timeboosted по {len(sample)} свежим победам "
                     f"(инкумбент {incumbent[:10] if incumbent else '?'}…)\n")

    per = defaultdict(lambda: {"n": 0, "tb_true": 0, "tb_false": 0, "tb_none": 0, "pos": Counter(), "gas": 0.0})
    bcache = {}
    for i, e in enumerate(sample, 1):
        sys.stderr.write(f"\r  receipt {i}/{len(sample)}   ")
        sys.stderr.flush()
        w = _winner(e)
        rc = get_receipt(rpc, e["hash"])
        ntx = block_txcount(rpc, int(e["blockNumber"]), bcache)
        d = per[w]
        d["n"] += 1
        if rc["timeboosted"] is True:
            d["tb_true"] += 1
        elif rc["timeboosted"] is False:
            d["tb_false"] += 1
        else:
            d["tb_none"] += 1
        d["pos"][position_bucket(rc["index"], ntx)] += 1
        d["gas"] += rc["gas_used"] * rc["eff_price"] / 1e18
    sys.stderr.write("\n")

    tb_seen = any((d["tb_true"] + d["tb_false"]) > 0 for d in per.values())
    priced = [e["_usd"] for e in big if e["_usd"] is not None]

    print("\n" + "=" * 72)
    print(f"  WINNER X-RAY (сабграф) — Silo V2 — ARBITRUM — выборка {len(big)} ликв, receipts {len(sample)}")
    print("=" * 72)
    if priced:
        print(f"Ликвидаций с ценой: {len(priced)} на ${sum(priced):,.0f} (amountUSD из сабграфа)")

    print("\n──── КОНЦЕНТРАЦИЯ ПОБЕДИТЕЛЕЙ (открыт ли хвост) ────")
    print(f"Разных ликвидаторов: {ws['distinct']}   |   всего побед: {ws['total']}")
    print(f"Доля топ-2:            {ws['top_n_share']*100:5.1f}%   (высокая → заперт)")
    print(f"Доля 'случайных' (<=2): {ws['occasional_share']*100:5.1f}%   (высокая → есть щель)")
    print(f"\n  Топ-{a.top}:")
    for i, (addr, n) in enumerate(ws["ranked"][:a.top], 1):
        print(f"   {i:2d}. {addr}  {n:4d}  ({n/ws['total']*100:4.1f}%)")

    if not tb_seen:
        print("\n⚠  RPC не отдаёт поле `timeboosted` в receipt (нужна нода Nitro посвежее: Alchemy/QuickNode).")
        print("   Концентрация выше валидна, но развилку деньги/физика без флага не решить.")

    print("\n──── TIMEBOOST по ботам (свежая выборка) ────")
    order = sorted(per.items(), key=lambda kv: kv[1]["n"], reverse=True)[:a.top]
    for addr, d in order:
        n = d["n"]
        tb_pct = f"{d['tb_true']/n*100:.0f}%" if (tb_seen and n) else "—"
        posdist = ", ".join(f"{k}:{v}" for k, v in d["pos"].most_common())
        tag = " ◀ ИНКУМБЕНТ" if addr == incumbent else ""
        print(f"  {addr[:12]}…  n={n:3d}  timeboosted={tb_pct:>4} ({d['tb_true']}/{n})  газ~{d['gas']/n if n else 0:.6f}ETH{tag}")
        print(f"       позиция в блоке: {posdist}")

    print("\n" + "=" * 72)
    print("  ВЕРДИКТ")
    print("=" * 72)
    top2, occ = ws["top_n_share"], ws["occasional_share"]
    if ws["total"] < 20:
        print("⚠  Мало данных (<20) — подними --limit.")
    open_tail = (occ >= 0.4 or top2 < 0.5)
    if open_tail:
        print(f"🟢 ХВОСТ ОТКРЫТ (топ-2 {top2*100:.0f}%, случайных {occ*100:.0f}%): в отличие от Sonic есть щель.")
        print("   Вот это и есть сценарий, ради которого стоило мерить Arbitrum. Дальше — экономика входа.")
    else:
        print(f"🔒 ХВОСТ ЗАПЕРТ (топ-2 {top2*100:.0f}%, случайных {occ*100:.0f}%), как и на Sonic.")
        inc = per.get(incumbent)
        if tb_seen and inc and inc["n"]:
            sh = inc["tb_true"] / inc["n"]
            if sh >= 0.6:
                print(f"   💰 ДЕНЬГИ: инкумбент timeboosted на {sh*100:.0f}% → выигрывает Timeboost-аукционом, не железом.")
                print("      Физика не стена. Но express lane на >90% у Wintermute/Selini, ставки → величине MEV,")
                print("      ~22% timeboosted ревертят. Перебивать их соло за приз, который аукцион и съедает, — почти")
                print("      наверняка минус. Прежде чем строить — считаем: MEV топ-силоса за раунд vs цена бида.")
            elif sh <= 0.15:
                print(f"   ⚙  ФИЗИКА: инкумбент timeboosted лишь на {sh*100:.0f}% → co-location/FCFS.")
                print("      Стена физическая: он ближе к секвенсеру. Соло не обгоняем. Вывод как по Sonic —")
                print("      Silo-ликвидатор не строим, Morpho-на-Base остаётся лучшим носителем.")
            else:
                print(f"   🟡 СМЕШАННО: инкумбент timeboosted на {sh*100:.0f}% — и аукцион, и латентность.")
        else:
            print("   (нет флага timeboosted — см. предупреждение; развилку деньги/физика пока не закрыть.)")
    print("\nПримечание: сабграф видит только успешные ликвидации (реверты соперников не индексируются).")


if __name__ == "__main__":
    main()
SILO_EOF
cd "$DIR"
python3 -m py_compile analysis/winner_xray_sg.py && echo "[OK] py_compile"
python3 - << 'PY_TEST'
import analysis.winner_xray_sg as sg
from analysis.winner_xray_sg import fetch_liquidations, _usd, _winner, block_txcount
from analysis.contestation import winner_stats
from analysis.winner_xray import get_receipt
assert _usd("12.5")==12.5 and _usd(None) is None and _usd("x") is None
assert _winner({"liquidator":{"id":"0xABC"}})=="0xabc" and _winner({})==""
BASE=[{"hash":f"0x{i:x}","blockNumber":str(1_000_000+(3000-i)),"timestamp":str(3000-i),
       "amountUSD":(None if i%50==0 else str(i*10)),"liquidator":{"id":"0xb" if i%3==0 else "0xa"},
       "liquidatee":{"id":"0xd"},"asset":{"symbol":"USDC"},"market":{"id":"0xm"}} for i in range(1,2501)]
calls=[]
def fake(url,q,variables=None,timeout=40):
    calls.append(dict(variables)); lt=int(variables["lt"]); first=min(variables["first"],1000)
    flt=sorted([e for e in BASE if int(e["timestamp"])<lt],key=lambda e:int(e["timestamp"]),reverse=True)
    return {"liquidates":flt[:first]}
sg.graphql=fake
liqs=fetch_liquidations("http://x",2500)
ts=[int(e["timestamp"]) for e in liqs]
assert len(liqs)==2500 and ts==sorted(ts,reverse=True) and len(set(e["hash"] for e in liqs))==2500 and len(calls)==3
ws=winner_stats([_winner(e) for e in liqs]); assert ws["distinct"]==2 and ws["ranked"][0][0]=="0xa"
class M:
    def __init__(s): s.b={7:{"transactions":["a","b","c"]}}
    def call(s,m,p):
        if m=="eth_getTransactionReceipt":
            tb={"0x1":True,"0x2":False}.get(p[0],None)
            return {"transactionIndex":"0x1","status":"0x1","gasUsed":"0x30d40","effectiveGasPrice":"0x5f5e100","timeboosted":tb}
        return s.b[int(p[0],16)]
m=M(); c={}; assert block_txcount(m,7,c)==3 and block_txcount(m,7,c)==3
agg=[0,0,0]
for h in ("0x1","0x2","0x9"):
    tb=get_receipt(m,h)["timeboosted"]; agg[0]+=tb is True; agg[1]+=tb is False; agg[2]+=tb is None
assert agg==[1,1,1]
print("[OK] тесты: пагинация сабграфа + парсинг + концентрация + receipts/timeboosted — прошли")
PY_TEST
echo ">> winner_xray_sg.py установлен и проверен."
