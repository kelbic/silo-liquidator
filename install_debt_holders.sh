#!/usr/bin/env bash
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: Morpho-бот"; exit 1; }
[ -e "$DIR/chain/morpho.py" ] && { echo "СТОП: Morpho-файлы"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py"; exit 1; }
[ -f "$DIR/analysis/oracle_check.py" ] || { echo "СТОП: нет oracle_check.py"; exit 1; }
grep -q "SEL_GETCONFIG" "$DIR/analysis/oracle_check.py" || { echo "СТОП: oracle_check.py без SEL_GETCONFIG — обнови его"; exit 1; }
cat > "$DIR/analysis/debt_holders.py" << 'FILE_EOF'
#!/usr/bin/env python3
"""debt_holders.py — полный охват держателей debtShareToken (не только из --days окна). (read-only)

Фаза 1 (этот файл сейчас): достать адрес debtShareToken + найти блок деплоя бинпоиском —
узнать РЕАЛЬНЫЙ масштаб задачи (сколько блоков сканировать), прежде чем строить сам скан
Transfer-логов. open_positions.py слеп к позициям старше --days окна; это устраняет слепоту
полностью, но полный скан истории может быть большим — сначала измеряем, не гадаем.

Переиспользует get_silo_config из oracle_check.py (та же ConfigData-цепочка, что и solvencyOracle,
другое поле структуры) и паттерн бинпоиска find_block_at_ts из contestation.py (адаптирован под
поиск по КОДУ контракта, не по времени).

Запуск:
  python3 -m analysis.debt_holders --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC
from analysis.oracle_check import get_silo_config, SEL_GETCONFIG, _addr_from_word

IDX_DEBT_SHARE_TOKEN = 6  # поле #6 в ConfigData (см. read_fee.py / STATE.md §2) — рядом с
                          # IDX_SOLVENCY_ORACLE=7 в oracle_check.py, тот же getConfig(silo) вызов


def get_debt_share_token(rpc: RPC, config: str, silo: str) -> str | None:
    """Тот же вызов, что get_solvency_oracle в oracle_check.py, другое поле структуры (6, не 7)."""
    data = SEL_GETCONFIG + silo[2:].lower().rjust(64, "0")
    a = _addr_from_word(rpc.eth_call(config, data), IDX_DEBT_SHARE_TOKEN)
    if a is None:
        return None
    return a if int(a, 16) != 0 else "0x0"


def find_deployment_block(rpc: RPC, address: str, hi_block: int) -> int:
    """Наименьший блок, где eth_getCode(address) уже непустой. Тот же бинпоиск, что find_block_at_ts
    в contestation.py, но по НАЛИЧИЮ КОДА, не по timestamp — код монотонен (появляется один раз,
    дальше не исчезает для живого используемого контракта), бинпоиск корректен."""
    lo, hi = 0, hi_block
    while lo < hi:
        mid = (lo + hi) // 2
        code = rpc.call("eth_getCode", [address, hex(mid)]) or "0x"
        if code == "0x":
            lo = mid + 1
        else:
            hi = mid
    return lo


def main():
    ap = argparse.ArgumentParser(description="Фаза 1: адрес debtShareToken + блок деплоя (масштаб задачи)")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", required=True)
    a = ap.parse_args()
    silo = a.silo.lower()
    rpc = RPC(a.rpc)

    config = get_silo_config(rpc, silo)
    if not config:
        return print(f"config() на {silo} не прочитан")
    token = get_debt_share_token(rpc, config, silo)
    if not token or token == "0x0":
        return print(f"debtShareToken не найден для {silo}")
    print(f"debtShareToken: {token}")

    tip = rpc.block_number()
    dep_block = find_deployment_block(rpc, token, tip)
    span = tip - dep_block
    print(f"блок деплоя: {dep_block}  (текущий tip: {tip})")
    print(f"диапазон для скана Transfer-истории: {span:,} блоков")
    print(f"\nЭто ТОЛЬКО измерение масштаба — сам скан Transfer-логов ещё не запущен.")


if __name__ == "__main__":
    main()
FILE_EOF
cd "$DIR"
python3 -m py_compile analysis/debt_holders.py && echo "[OK] py_compile"
python3 -c "import analysis.debt_holders" && echo "[OK] реальный импорт"
python3 - << 'PY_TEST'
import analysis.debt_holders as dh
DEPLOY_AT = 12345
class F:
    def call(self, method, params):
        block = int(params[1], 16)
        return "0x6080604052" if block >= DEPLOY_AT else "0x"
found = dh.find_deployment_block(F(), "0xtoken", 99999)
assert found == DEPLOY_AT
class F0:
    def call(self, method, params): return "0x6080604052"
assert dh.find_deployment_block(F0(), "0xtoken", 1000) == 0
class FHi:
    def call(self, method, params):
        block = int(params[1], 16)
        return "0x6080604052" if block >= 1000 else "0x"
assert dh.find_deployment_block(FHi(), "0xtoken", 1000) == 1000
def word_addr(tag): return "0" * 24 + tag.lower().ljust(40, "0")
def word_uint(n): return hex(n)[2:].rjust(64, "0")
words = [word_uint(0)] * 6 + [word_addr("deadbeef")] + [word_uint(0)] * 10
ret = "0x" + "".join(words)
class FCfg:
    def eth_call(self, to, data):
        assert data.startswith(dh.SEL_GETCONFIG)
        return ret
result = dh.get_debt_share_token(FCfg(), "0xconfig", "0xsilo")
assert result == "0x" + word_addr("deadbeef")[24:]
print("[OK] бинпоиск (3 случая: середина/генезис/граница) + поле 6 ConfigData — прошли")
PY_TEST
echo ">> debt_holders.py (фаза 1: измерение масштаба). Запуск:"
echo "   python3 -m analysis.debt_holders --rpc https://rpc.soniclabs.com --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3"
