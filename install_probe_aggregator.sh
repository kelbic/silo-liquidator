#!/usr/bin/env bash
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: Morpho-бот"; exit 1; }
[ -e "$DIR/chain/morpho.py" ] && { echo "СТОП: Morpho-файлы"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py"; exit 1; }
cat > "$DIR/analysis/probe_aggregator.py" << 'FILE_EOF'
#!/usr/bin/env python3
"""probe_aggregator.py — что реально есть на Chainlink-агрегаторе (не угадываем deviation threshold). (read-only)

Зачем: heartbeat агрегатора 0xc76dfb89 прочитан (87001с), но deviation threshold — НЕТ. Прежде чем
строить тул под конкретный геттер, проверяю, что там вообще есть: OCR-агрегаторы Chainlink часто держат
deviation threshold как ОФЧЕЙН-параметр нод-операторов (используется при принятии решения об апдейте),
не ончейн-значение — простого read может не существовать вовсе. Пять кандидатов, все стандартные
Chainlink-конвенции (не угаданные): aggregator() (если 0xc76dfb89 — прокси), typeAndVersion() (класс
контракта), description() (человекочитаемое имя фида — по нему можно свериться с data.chain.link, если
ончейн-чтения нет), decimals(), version(). Переиспользует decode_string_ret из contestation.py.

Запуск:
  python3 -m analysis.probe_aggregator --rpc https://rpc.soniclabs.com --address 0xc76dfb89...
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC, decode_string_ret

CANDIDATES = {
    "0x245a7bfc": ("aggregator()", "address"),
    "0x181f5a77": ("typeAndVersion()", "string"),
    "0x7284e416": ("description()", "string"),
    "0x313ce567": ("decimals()", "uint"),
    "0x54fd4d50": ("version()", "uint"),
}


def probe(rpc: RPC, address: str) -> dict:
    """Пробует каждый селектор, репортит успех/ревёрт и декодированное значение по типу."""
    out = {}
    for sel, (name, kind) in CANDIDATES.items():
        try:
            ret = rpc.eth_call(address, sel)
        except Exception as e:  # noqa
            out[name] = {"ok": False, "error": str(e)[:80]}
            continue
        if not ret or ret == "0x":
            out[name] = {"ok": False, "error": "пустой ответ (вероятно ревёрт)"}
            continue
        if kind == "string":
            out[name] = {"ok": True, "value": decode_string_ret(ret)}
        elif kind == "address":
            out[name] = {"ok": True, "value": "0x" + ret[-40:]}
        else:  # uint
            out[name] = {"ok": True, "value": int(ret, 16)}
    return out


def main():
    ap = argparse.ArgumentParser(description="Что реально есть на Chainlink-агрегаторе")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--address", required=True)
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    results = probe(rpc, a.address.lower())
    print(f"Зонд {a.address} — 5 стандартных Chainlink-селекторов:")
    for name, r in results.items():
        if r["ok"]:
            print(f"  {name:20s} ОТВЕТИЛ: {r['value']}")
        else:
            print(f"  {name:20s} нет ({r['error']})")

    print("\n⚠ Ни один из этих пяти НЕ является deviation threshold напрямую — они только")
    print("  классифицируют контракт (прокси/нет, тип, имя фида). Если aggregator() ответил —")
    print("  0xc76dfb89 прокси, реальная имплементация в его ответе, зонд надо повторить на НЕЙ.")
    print("  Если description() дал имя фида — свериться на data.chain.link по этому имени может")
    print("  быть быстрее, чем искать ончейн-геттер, которого может не быть вовсе.")


if __name__ == "__main__":
    main()
FILE_EOF
cd "$DIR"
python3 -m py_compile analysis/probe_aggregator.py && echo "[OK] py_compile"
python3 -c "import analysis.probe_aggregator" && echo "[OK] реальный импорт"
python3 - << 'PY_TEST'
import analysis.probe_aggregator as pa
addr_ret = "0x" + "0"*24 + "ab"*20
def encode_string(s):
    length = len(s)
    hex_str = s.encode().hex().ljust((length + 31) // 32 * 64, "0")
    return "0x" + "0"*62 + "20" + hex(length)[2:].rjust(64, "0") + hex_str
tv_ret = encode_string("OCR2Aggregator 1.0.0")
dec_ret = "0x" + hex(18)[2:].rjust(64, "0")
responses = {"0x245a7bfc": addr_ret, "0x181f5a77": tv_ret, "0x313ce567": dec_ret}
class F2:
    def eth_call(s, to, data):
        return responses.get(data, "0x")
r = pa.probe(F2(), "0xagg")
assert r["aggregator()"]["ok"] is True and r["aggregator()"]["value"] == "0x" + "ab"*20
assert r["typeAndVersion()"]["ok"] is True and r["typeAndVersion()"]["value"] == "OCR2Aggregator 1.0.0"
assert r["decimals()"]["ok"] is True and r["decimals()"]["value"] == 18
assert r["description()"]["ok"] is False and r["version()"]["ok"] is False
print("[OK] все 3 типа декодирования (address/string/uint) + пустые ответы — прошли")
PY_TEST
echo ">> probe_aggregator.py готов. Возьми адрес агрегатора из свежего вывода oracle_check.py, запусти:"
echo "   python3 -m analysis.probe_aggregator --rpc https://rpc.soniclabs.com --address <primaryAggregator из oracle_check>"
