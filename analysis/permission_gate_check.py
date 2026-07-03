#!/usr/bin/env python3
"""permission_gate_check.py — ЗАКРЫТ ли рынок вайтлистом ликвидаторов? (read-only, критический гейт)

ГЛАВНОЕ ОТКРЫТИЕ трека (2026-07-03, fork-replay): целевой рынок `0x322e1d53` (USDC/wS) —
**ПЕРМИШЕНОВАННЫЙ**. Изъятие залога при ликвидации проходит через afterTokenTransfer гейта на
**ЗАЛОГОВОМ** share-токене, а `allowMeToLiquidate()` его контроллера закрыт ролью `ALLOWED_ROLE`.
Свежий контракт получает `OnlyAllowedRole()` при попытке взвестись → `LiquidationNotAllowed()` при
изъятии. Ликвидировать может только адрес из вайтлиста.

Почему прежние проверки могли это упустить: гейт сидит на гейдже ЗАЛОГОВОГО силоса
(`0xf55902de`→gauge `0x5c10d4cC`), а события LiquidationCall индексируются под ДОЛГОВЫМ силосом
(`0x322e1d53`→gauge `0x36177720`, другой контроллер). Проверка со стороны долга гейт не видит —
та же готча двусторонних пар, что уже кусала трек (§2 STATE.md). `_turnOnLiquidation` взводит именно
ЗАЛОГОВЫЕ share-токены (там изъятие) — поэтому решает ЗАЛОГОВЫЙ гейт.

Этот тул воспроизводимо проверяет гейт для ЛЮБОГО рынка: резолвит залоговый гейдж, читает членов
ALLOWED_ROLE, и (если задан --liquidator) проверяет, в вайтлисте ли он.

Запуск:
  python3 -m analysis.permission_gate_check --rpc https://rpc.soniclabs.com \
      --silo 0x322e1d5384aa4ed66aeca770b95686271de61dc3 [--liquidator 0x...]
"""
from __future__ import annotations
import argparse

from analysis.contestation import RPC, RpcError
from analysis.read_fee import SEL_CONFIG, SEL_GETSILOS, SEL_GETCONFIG, parse_config, _word
from analysis.borrower_health import _addr_pad

SEL_CONFIGURED_GAUGES = "0xa37d9411"   # GaugeHookReceiver.configuredGauges(address)->address — keccak-сверен офлайн
SEL_ALLOWED_ROLE      = "0xd32f7154"   # ALLOWED_ROLE() (bytes32) — keccak-сверен офлайн
SEL_HAS_ROLE          = "0x91d14854"   # hasRole(bytes32,address)->bool
SEL_ROLE_MEMBER_COUNT = "0xca15c873"   # getRoleMemberCount(bytes32)->uint256
SEL_ROLE_MEMBER       = "0x9010d07c"   # getRoleMember(bytes32,uint256)->address
SEL_GET_ROLE_ADMIN    = "0x248a9ca3"   # getRoleAdmin(bytes32)->bytes32


def resolve_collateral_gate(rpc: RPC, silo: str) -> dict:
    """Для рынка вокруг долгового `silo`: сосед по паре (залоговый силос) → hook → configuredGauges его
    collateralShareToken. Возвращает {hook, collateral_silo, collateral_share, gauge}."""
    config = "0x" + _word(rpc.eth_call(silo, SEL_CONFIG), 0)[24:]
    s = rpc.eth_call(config, SEL_GETSILOS)
    s0, s1 = "0x" + _word(s, 0)[24:], "0x" + _word(s, 1)[24:]
    collateral_silo = s1 if s0.lower() == silo.lower() else s0
    cfg_debt = parse_config(rpc.eth_call(config, SEL_GETCONFIG + silo[2:].rjust(64, "0")))
    cfg_coll = parse_config(rpc.eth_call(config, SEL_GETCONFIG + collateral_silo[2:].rjust(64, "0")))
    hook = cfg_debt["hookReceiver"]
    share = cfg_coll["collateralShareToken"]
    gauge_ret = rpc.eth_call(hook, SEL_CONFIGURED_GAUGES + _addr_pad(share))
    gauge = "0x" + gauge_ret[-40:]
    return {"config": config, "hook": hook, "collateral_silo": collateral_silo,
            "collateral_share": share, "gauge": gauge, "protected_share": cfg_coll["protectedShareToken"]}


def read_allowed_role(rpc: RPC, gauge: str) -> dict | None:
    """Читает ALLOWED_ROLE и список членов. None — если гейдж не гейтит роль (открытый рынок)."""
    if int(gauge, 16) == 0:
        return None
    try:
        role = rpc.eth_call(gauge, SEL_ALLOWED_ROLE)
    except RpcError:
        return None
    if not role or int(role, 16) == 0:
        return None
    try:
        n = int(rpc.eth_call(gauge, SEL_ROLE_MEMBER_COUNT + role[2:]), 16)
    except RpcError:
        n = None
    members = []
    if n is not None:
        for i in range(n):
            try:
                m = rpc.eth_call(gauge, SEL_ROLE_MEMBER + role[2:] + hex(i)[2:].rjust(64, "0"))
                members.append("0x" + m[-40:])
            except RpcError:
                break
    admin_role = None
    try:
        admin_role = rpc.eth_call(gauge, SEL_GET_ROLE_ADMIN + role[2:])
    except RpcError:
        pass
    return {"role": role, "count": n, "members": members, "admin_role": admin_role}


def has_role(rpc: RPC, gauge: str, role: str, account: str) -> bool:
    ret = rpc.eth_call(gauge, SEL_HAS_ROLE + role[2:] + _addr_pad(account))
    return int(ret, 16) == 1


def main():
    ap = argparse.ArgumentParser(description="Закрыт ли рынок вайтлистом ликвидаторов (ALLOWED_ROLE)")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--silo", required=True, help="ДОЛГОВОЙ силос рынка")
    ap.add_argument("--liquidator", default=None, help="проверить, в вайтлисте ли этот адрес")
    a = ap.parse_args()
    rpc = RPC(a.rpc)
    silo = a.silo.lower()

    g = resolve_collateral_gate(rpc, silo)
    print(f"рынок вокруг долгового силоса {silo}")
    print(f"  залоговый силос : {g['collateral_silo']}")
    print(f"  hook            : {g['hook']}")
    print(f"  collateralShare : {g['collateral_share']}")
    print(f"  залоговый gauge : {g['gauge']}  ← через него идёт изъятие, ЕГО роль решает")

    role = read_allowed_role(rpc, g["gauge"])
    if role is None:
        print("\n✅ ОТКРЫТЫЙ РЫНОК: залоговый гейдж не гейтит ALLOWED_ROLE — ликвидировать может любой.")
        return
    print(f"\n🔒 ПЕРМИШЕНОВАННЫЙ РЫНОК")
    print(f"  ALLOWED_ROLE = {role['role']}")
    print(f"  членов в вайтлисте: {role['count']}")
    for i, m in enumerate(role["members"]):
        print(f"    [{i}] {m}")
    print(f"  вывод: ликвидировать может ТОЛЬКО адрес из этого списка. Свежий контракт получит")
    print(f"         OnlyAllowedRole() при arm → LiquidationNotAllowed() при изъятии.")

    if a.liquidator:
        yes = has_role(rpc, g["gauge"], role["role"], a.liquidator.lower())
        print(f"\n  {a.liquidator} в вайтлисте: {'ДА ✅' if yes else 'НЕТ ❌ — не сможет ликвидировать этот рынок'}")


if __name__ == "__main__":
    main()
