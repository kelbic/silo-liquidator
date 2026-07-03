#!/usr/bin/env bash
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: Morpho-бот"; exit 1; }
[ -f "$DIR/analysis/contestation.py" ] || { echo "СТОП: нет contestation.py"; exit 1; }
cat > "$DIR/analysis/verify_bytecode.py" << 'FILE_EOF'
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
FILE_EOF
cat > "$DIR/analysis/chainlinkv3_reference.hex" << 'REF_EOF'
608060405234801561000f575f5ffd5b5060043610610060575f3560e01c806313b0be3314610064578063217a4b701461008a578063324b8d6e146100aa578063c4d66de8146100bc578063c71ed1e6146100d1578063f9fa619a146100fb575b5f5ffd5b6100776100723660046106a9565b61010c565b6040519081526020015b60405180910390f35b6100926102dc565b6040516001600160a01b039091168152602001610081565b5f54610092906001600160a01b031681565b6100cf6100ca3660046106d7565b610352565b005b6100e46100df366004610706565b6104a7565b604080519215158352602083019190915201610081565b6100cf6101093660046106d7565b50565b5f8054604080516330fe427560e21b8152905183926001600160a01b03169163c3f909d4916004808301926101409291908290030181865afa158015610154573d5f5f3e3d5ffd5b505050506040513d601f19601f820116820180604052508101906101789190610772565b90508060c001516001600160a01b0316836001600160a01b0316146101b05760405163981a2a2b60e01b815260040160405180910390fd5b6fffffffffffffffffffffffffffffffff8411156101e157604051631df5999960e21b815260040160405180910390fd5b5f5f6101f4835f01518460400151610557565b91509150816102155760405162bfc92160e01b815260040160405180910390fd5b82610100015161025d57610233868285608001518660a001516105e2565b9350835f03610255576040516301a7e28b60e61b815260040160405180910390fd5b5050506102d6565b5f5f61027185602001518660600151610557565b915091508161029357604051637c5ab47160e01b815260040160405180910390fd5b6102ae88848388608001518960a001518a610120015161061c565b9550855f036102d0576040516301a7e28b60e61b815260040160405180910390fd5b50505050505b92915050565b5f8054604080516330fe427560e21b8152905183926001600160a01b03169163c3f909d4916004808301926101409291908290030181865afa158015610324573d5f5f3e3d5ffd5b505050506040513d601f19601f820116820180604052508101906103489190610772565b60e0015192915050565b7ff0c57e16840df040f15088dc2f81fe391c3923bec73e23a9662efc9c229c6a008054600160401b810460ff16159067ffffffffffffffff165f811580156103975750825b90505f8267ffffffffffffffff1660011480156103b35750303b155b9050811580156103c1575080155b156103df5760405163f92ee8a960e01b815260040160405180910390fd5b845467ffffffffffffffff19166001178555831561040957845460ff60401b1916600160401b1785555b5f80546001600160a01b0319166001600160a01b0388169081179091556040519081527f077847d1fadf50041a730385b3d6b2de1cdeb5e078cb933fb091003e7f10e07a9060200160405180910390a1831561049f57845460ff60401b19168555604051600181527fc7f505b2f371ae2175ee4913f4499e1f2633a7b5936321eed1cdaeb6115181d29060200160405180910390a15b505050505050565b5f5f5f5f5f9054906101000a90046001600160a01b03166001600160a01b031663c3f909d46040518163ffffffff1660e01b815260040161014060405180830381865afa1580156104fa573d5f5f3e3d5ffd5b505050506040513d601f19601f8201168201806040525081019061051e9190610772565b90508361053c5761053781602001518260600151610557565b61054d565b61054d815f01518260400151610557565b9250925050915091565b5f5f5f846001600160a01b031663feaf968c6040518163ffffffff1660e01b815260040160a060405180830381865afa158015610596573d5f5f3e3d5ffd5b505050506040513d601f19601f820116820180604052508101906105ba9190610839565b5050509150505f8113156105d3576001925090506105db565b5f5f92509250505b9250929050565b5f815f036106035782848602816105fb576105fb610887565b049050610614565b848402610610838261089b565b9150505b949350505050565b8585025f83900361065e578161063b57610636858261089b565b610645565b61064585826108be565b905083818161065657610656610887565b04905061068b565b610668838261089b565b90508161067e57610679858261089b565b610688565b61068885826108be565b90505b9695505050505050565b6001600160a01b0381168114610109575f5ffd5b5f5f604083850312156106ba575f5ffd5b8235915060208301356106cc81610695565b809150509250929050565b5f602082840312156106e7575f5ffd5b81356106f281610695565b9392505050565b8015158114610109575f5ffd5b5f60208284031215610716575f5ffd5b81356106f2816106f9565b604051610140810167ffffffffffffffff8111828210171561075157634e487b7160e01b5f52604160045260245ffd5b60405290565b805161076281610695565b919050565b8051610762816106f9565b5f610140828403128015610784575f5ffd5b5061078d610721565b61079683610757565b81526107a460208401610757565b602082015260408381015190820152606080840151908201526080808401519082015260a080840151908201526107dd60c08401610757565b60c08201526107ee60e08401610757565b60e08201526108006101008401610767565b6101008201526108136101208401610767565b6101208201529392505050565b805169ffffffffffffffffffff81168114610762575f5ffd5b5f5f5f5f5f60a0868803121561084d575f5ffd5b61085686610820565b6020870151604088015160608901519297509095509350915061087b60808701610820565b90509295509295909350565b634e487b7160e01b5f52601260045260245ffd5b80820281158282048414176102d657634e487b7160e01b5f52601160045260245ffd5b5f826108d857634e487b7160e01b5f52601260045260245ffd5b50049056fea26469706673582212208756f2e416807b637e231ab6477846e3b0bad4e0ac9ae033b029a994091d01bc64736f6c634300081c0033
REF_EOF
cd "$DIR"
python3 -m py_compile analysis/verify_bytecode.py && echo "[OK] py_compile"
[ -s analysis/chainlinkv3_reference.hex ] && echo "[OK] эталонный hex записан ($(wc -c < analysis/chainlinkv3_reference.hex) байт)"
python3 - << 'PY_TEST'
import analysis.verify_bytecode as vb
a="0x6080604052"+"ab"*100
assert vb.compare_bytecode(a,a)["identical"] is True
base="6080604052"+"cd"*180
d="0x"+base+"aa"*30; r=base+"bb"*30
res=vb.compare_bytecode(d,r)
assert res["identical"] is False and res["first_diff_byte"]==len(base)//2
print("[OK] compare_bytecode: идентично/хвост-расхождение — прошли")
PY_TEST
echo ">> verify_bytecode.py + эталон установлены ОДНИМ скриптом. Запуск:"
echo "   python3 -m analysis.verify_bytecode --rpc https://rpc.soniclabs.com --address 0x2e226f3a140563138570693b17c0a3060726bde8 --reference analysis/chainlinkv3_reference.hex"
