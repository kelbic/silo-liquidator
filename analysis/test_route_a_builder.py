#!/usr/bin/env python3
"""Офлайн-тест билдера route-A калдаты — байт-в-байт против эталона, снятого cast'ом (и это ТА ЖЕ калдата,
что дала 10.155 USDC в проходящем fork-replay). Без RPC, только stdlib.

Запуск:  python3 -m analysis.test_route_a_builder
"""
from analysis.route_a_builder import encode_swap_calldata, SEL_SWAP

POOL = "0x324963c267C354c7660Ce8CA3F5f167E05649970"
WS = "0x039e2fb66102314ce7b64ce5ce3e5183bc94ad38"
# эталон: `cast calldata 'swap(address,address,bool)' <POOL> <WS> true` — и он же строится в
# SiloLiquidatorFork.t.sol (abi.encodeWithSelector(V3SwapAdapter.swap.selector, pool, coll, true)).
CAST_REF = ("0xc1813380"
            "000000000000000000000000324963c267c354c7660ce8ca3f5f167e05649970"
            "000000000000000000000000039e2fb66102314ce7b64ce5ce3e5183bc94ad38"
            "0000000000000000000000000000000000000000000000000000000000000001")


def test_selector_matches():
    assert SEL_SWAP == "0xc1813380", SEL_SWAP  # swap(address,address,bool)


def test_calldata_byte_matches_cast_zeroForOne_true():
    out = encode_swap_calldata(POOL, WS, True)
    assert out.lower() == CAST_REF.lower(), f"\n got {out}\n ref {CAST_REF}"


def test_zeroForOne_false_flips_last_word():
    out = encode_swap_calldata(POOL, WS, False)
    assert out.lower() == CAST_REF.lower()[:-64] + "0" * 64
    assert out.lower().endswith("0" * 64)


def test_shape_selector_plus_three_words():
    out = encode_swap_calldata(POOL, WS, True)
    assert (len(out) - 2) // 2 == 4 + 3 * 32  # selector(4) + 3 слова(96) = 100 байт


def test_addresses_lowercased_and_padded():
    out = encode_swap_calldata(POOL, WS, True)
    # pool в слове 1, tokenIn в слове 2, оба левым-zero-pad до 32 байт, в нижнем регистре
    w1 = out[10:10 + 64]
    w2 = out[10 + 64:10 + 128]
    assert w1 == POOL[2:].lower().rjust(64, "0")
    assert w2 == WS[2:].lower().rjust(64, "0")


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} тестов прошли.")


if __name__ == "__main__":
    run()
