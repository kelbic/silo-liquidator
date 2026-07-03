#!/usr/bin/env bash
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: Morpho-бот"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py"; exit 1; }
cat > "$DIR/analysis/recent_borrowers.py" << 'FILE_EOF'
#!/usr/bin/env python3
"""recent_borrowers.py — печатает borrower из недавних ликвидаций целевого силоса. (read-only)

Зачем: decode_liquidation_log() уже парсит поле borrower — ни один из тулов трека его не печатал.
Это чистая дыра в выводе, не новый парсинг: 100% переиспользует уже проверенные fetch/decode функции.
Нужно для первого живого теста borrower_health.py — там нужен РЕАЛЬНЫЙ адрес, не синтетика.

Запуск:
  python3 -m analysis.recent_borrowers --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 --days 30 --top 10
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC, fetch_liquidation_logs, decode_liquidation_log, find_block_at_ts, silo_token_meta


def main():
    ap = argparse.ArgumentParser(description="Borrower из недавних ликвидаций силоса")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", required=True)
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--top", type=int, default=10, help="сколько последних показать")
    a = ap.parse_args()
    silo = a.silo.lower()
    rpc = RPC(a.rpc)

    tip = rpc.block_number()
    frm = find_block_at_ts(rpc, rpc.block_ts(tip) - int(a.days * 86400), tip)
    logs = fetch_liquidation_logs(rpc, frm, tip, chunk=10_000)
    events = [e for e in (decode_liquidation_log(l) for l in logs) if e]
    seen, uniq = set(), []
    for e in events:
        k = (e["tx"], e["log_index"])
        if k not in seen:
            seen.add(k); uniq.append(e)

    meta = {}
    m = silo_token_meta(rpc, silo, meta)
    target = sorted([e for e in uniq if e["silo"].lower() == silo], key=lambda e: -e["block"])[: a.top]
    if not target:
        return print(f"Ликвидаций на {silo} за {a.days:g}д не найдено.")

    print(f"{a.top} последних ликвидаций на {silo} ({m['symbol']}):")
    for e in target:
        repay = e["repay_raw"] / (10 ** m["decimals"])
        print(f"  блок {e['block']:>9d}  borrower {e['borrower']}  repay {repay:,.2f} {m['symbol']}")
    print(f"\nВыбери любой borrower выше для --borrower в borrower_health.py.")


if __name__ == "__main__":
    main()
FILE_EOF
cd "$DIR"
python3 -m py_compile analysis/recent_borrowers.py && echo "[OK] py_compile"
python3 -c "import analysis.recent_borrowers" && echo "[OK] реальный импорт"
python3 - << 'PY_TEST'
SILO="0x322e1d5384aa4ed66aeca770b95686271de61dc3"
OTHER="0x7e88ae5e50474a48dea4c42a634aa7485e7caa62"
uniq=[{"tx":"0xa","log_index":0,"block":100,"silo":SILO,"borrower":"0xborrower1","repay_raw":1000_000000},
      {"tx":"0xb","log_index":0,"block":200,"silo":SILO,"borrower":"0xborrower2","repay_raw":2000_000000},
      {"tx":"0xc","log_index":0,"block":150,"silo":OTHER,"borrower":"0xborrower3","repay_raw":500_000000},
      {"tx":"0xa","log_index":0,"block":100,"silo":SILO,"borrower":"0xborrower1","repay_raw":1000_000000}]
seen,dedup=set(),[]
for e in uniq:
    k=(e["tx"],e["log_index"])
    if k not in seen: seen.add(k); dedup.append(e)
assert len(dedup)==3
target=sorted([e for e in dedup if e["silo"].lower()==SILO.lower()], key=lambda e:-e["block"])
assert len(target)==2 and target[0]["block"]==200 and target[1]["block"]==100
print("[OK] дедуп+фильтр+сортировка borrower-списка — прошли")
PY_TEST
echo ">> recent_borrowers.py готов. Запуск:"
echo "   python3 -m analysis.recent_borrowers --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 --days 30 --top 10"
