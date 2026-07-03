#!/usr/bin/env python3
"""verify_bytecode.py — сверяет РАЗВЁРНУТЫЙ байткод адреса с локально скомпилированным эталоном. (read-only)

Зачем: impl-match (прокси резолвится в тот же адрес 0x2e226f3a) — это совпадение АДРЕСА, не кода. Максимальная
проверка без доверия Sonicscan: скомпилировать исходник ChainlinkV3Oracle теми же настройками, что использует
сам Silo (Standard JSON Input из flattened/silo_oracles/arbitrum_one/, optimizer=200/cancun/viaIR=false — сверено
с foundry.toml профиля [profile.oracles]), и сравнить байт-в-байт с тем, что реально развёрнуто.

Эталон — /home/claude/work/oracle-verify/runtime.hex (скомпилирован из Arbitrum-артефакта Silo; Sonic может
использовать тот же код на другой цепи, но это ПРЕДПОЛОЖЕНИЕ — сравнение само покажет, верно ли оно).

Запуск:
  python3 -m analysis.verify_bytecode --rpc https://rpc.soniclabs.com --address 0x2e226f3a140563138570693b17c0a3060726bde8 --reference /path/to/runtime.hex
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC


def compare_bytecode(deployed: str, reference: str) -> dict:
    """Сравнивает два hex-байткода. Возвращает длины, совпадение целиком, и точку первого расхождения
    (если есть) — чтобы отличить 'разошлись только метаданные в хвосте' от 'разный код в середине'."""
    d = deployed[2:] if deployed.startswith("0x") else deployed
    r = reference[2:] if reference.startswith("0x") else reference
    d, r = d.lower().strip(), r.lower().strip()
    identical = (d == r)
    first_diff = None
    for i in range(min(len(d), len(r))):
        if d[i] != r[i]:
            first_diff = i // 2  # байты, не hex-символы
            break
    return {"deployed_len": len(d) // 2, "reference_len": len(r) // 2,
            "identical": identical, "first_diff_byte": first_diff}


def main():
    ap = argparse.ArgumentParser(description="Сверка развёрнутого байткода с локальной компиляцией")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--address", required=True)
    ap.add_argument("--reference", required=True, help="путь к файлу с эталонным hex (без 0x или с ним)")
    a = ap.parse_args()
    rpc = RPC(a.rpc)

    deployed = rpc.call("eth_getCode", [a.address, "latest"]) or "0x"
    reference = open(a.reference).read().strip()

    r = compare_bytecode(deployed, reference)
    print(f"развёрнуто:  {r['deployed_len']} байт   ({a.address})")
    print(f"эталон:      {r['reference_len']} байт   (локальная компиляция)")
    if r["identical"]:
        print("\n>>> БАЙТ-В-БАЙТ ИДЕНТИЧНО. Это максимальное доступное подтверждение без доверия")
        print("    верификатору — не 'вероятно', а byte-exact match с известным исходником.")
    elif r["first_diff_byte"] is not None:
        tail_only = r["first_diff_byte"] >= min(r["deployed_len"], r["reference_len"]) - 60
        print(f"\n>>> РАСХОДИТСЯ с байта {r['first_diff_byte']} из ~{min(r['deployed_len'], r['reference_len'])}.")
        if tail_only:
            print(f"    Расхождение в последних ~60 байтах — похоже на ХВОСТ МЕТАДАННЫХ (CBOR/IPFS-хэш),")
            print(f"    который меняется от билд-окружения, но НЕ от логики контракта. Код, вероятно, тот же.")
        else:
            print(f"    Расхождение НЕ в хвосте — это может быть реальная разница в коде, не просто метаданные.")
    else:
        print("\n>>> Один байткод — префикс другого (разная длина, до общей части совпадают) — необычно,")
        print("    смотреть руками.")


if __name__ == "__main__":
    main()
