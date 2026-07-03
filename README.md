# Silo Finance V2 Liquidation Bot — Sonic Chain

Research + paper-detector for liquidations on Silo V2 (Sonic). Read-only, zero-capital
throughout: **no transactions, no keys, no external data sends.** Every tool reads the
public RPC (`https://rpc.soniclabs.com`) with Python stdlib only.

Full state, decisions, and the 5-point EV analysis live in [`STATE.md`](STATE.md)
(read the 🏁 verdict and ⭐/⭐⭐ sections first). Working process is in [`CLAUDE.md`](CLAUDE.md).

---

## 🏁 Verdict: technically ready, economically not justified

Both halves are on measured data, not estimates.

**✅ Technically ready (verified):**
- **Detection** works end-to-end — canonical `SiloLens`, event-driven coverage,
  two-sided-pair gate. Paper-traded on two open markets: **0 MISS on both** — we would
  always detect in time; detection was never the bottleneck.
- **Contract** (`SiloLiquidator.sol`) validated by fork-replay against **live** Silo
  contracts: the profit floor reverts at `minProfit=max` and passes at `=1`, realizing
  **10.155 USDC** on a real historical liquidation via a clean direct-pool route
  (slightly beating the incumbent's lossy split route).
- **Off-chain route-A quote builder**: calldata in **0.004 ms**, byte-identical to `cast`
  and to the passing replay. End-to-end hot-path latency **282 ms** on public RPC (≈3 ms
  co-located) — inside the lag≥1 block budget.
- ~40 offline unit tests green.

**❌ Economically not justified (measured):**
- The only valuable market is **gated** `0x322e1d53` (**$25,776/mo** bonus pool) — a
  closed whitelist of 4 liquidators; admin is Silo protocol governance. No access without
  Silo Labs.
- Across all Sonic Silo markets, **no market is {open} ∩ {polling-contestable} ∩ {valuable}.**
  Reliable open-market income ≈ **$13–30/mo** (paper-traded), below even a $15/mo VPS. The
  ~$120/mo seen on `0x4e216c15` is a single whale position — not bankable.
- The bottleneck is **$ pool size + gating of the valuable market**, not code or speed.

**Three paths (user's call):** (a) obtain `ALLOWED_ROLE` via Silo governance — the only
path to real money; (b) accept $13–30/mo as a hobby and go live; (c) stop here.
Recommended for income: **(c)**.

---

## Repo layout

| Path | What |
|---|---|
| `analysis/*.py` | Read-only, stdlib-only tools (detector, backtest, gate check, EV, route-A builder, latency, paper trader). |
| `analysis/test_*.py` | Offline unit tests (no network). |
| `contracts/` | `SiloLiquidator.sol` + Foundry tests incl. the fork-replay. Separate git sub-repo. See [`contracts/README.md`](contracts/README.md). |
| `radar/` | Earlier branch: scan for young/ungated markets (own systemd timer + SQLite). Not part of the final pipeline. |
| `STATE.md` | Full journal: findings, decisions, EV analysis. |
| `CLAUDE.md` | Working process + hard rules. |

Target market (gated): `0x322e1d5384aa4ed66aeca770b95686271de61dc3` (USDC debt / wS collateral).
Open markets referenced: `0x4e216c15697c1392fe59e1014b009505e05810df`,
`0x112380065a2cb73a5a429d9ba7368cc5e8434595`.

---

## Reproduce the findings

All analysis tools are stdlib-only — no install needed. Run from the repo root.

```bash
RPC=https://rpc.soniclabs.com
TARGET=0x322e1d5384aa4ed66aeca770b95686271de61dc3
```

**1. Detection works (current open positions on a market):**
```bash
python3 -m analysis.open_positions --rpc $RPC --silo $TARGET --days 30
python3 -m analysis.live_detector  --rpc $RPC --silo $TARGET --seed-days 30 --once
```

**2. The target market is permissioned (the pivotal finding):**
```bash
python3 -m analysis.permission_gate_check --rpc $RPC --silo $TARGET \
    --liquidator 0xccd487e01e9df6932f656b53668f58005f604417
# -> 🔒 PERMISSIONED, whitelist of 4; the incumbent is in it, a fresh contract is not.
```

**3. Open vs gated across all markets + the $ denominator (EV):**
```bash
python3 -m analysis.market_survey --rpc $RPC --days 14 --top 15   # gated vs open + concentration
python3 -m analysis.market_value  --rpc $RPC --days 30 --top 15   # bonus pool $/mo per market
```

**4. Catchability (block-lag vs real winners) on an open market:**
```bash
python3 -m analysis.backtest_detection --rpc $RPC \
    --silo 0x4e216c15697c1392fe59e1014b009505e05810df --days 30 --max-episodes 15
```

**5. Paper trading — hit/miss ledger vs real winners (no transactions):**
```bash
# USDC-debt market:
python3 -m analysis.paper_trader --rpc $RPC \
    --silo 0x4e216c15697c1392fe59e1014b009505e05810df --days 30 --contested-winrate 0.3
# wS-debt market (pass the measured S price):
python3 -m analysis.paper_trader --rpc $RPC \
    --silo 0x112380065a2cb73a5a429d9ba7368cc5e8434595 --days 45 --debt-price-usd 0.0264
# add --follow to keep logging forward. Ledger -> paper_ledger_<silo6>.jsonl (gitignored).
```

**6. Route-A quote builder + end-to-end latency:**
```bash
python3 -m analysis.route_a_builder --rpc $RPC \
    --adapter 0x000000000000000000000000000000000000dEaD \
    --pool 0x324963c267C354c7660Ce8CA3F5f167E05649970 \
    --collateral 0x039e2fb66102314ce7b64ce5ce3e5183bc94ad38
python3 -m analysis.latency_e2e --rpc $RPC \
    --hook 0x6aafd9dd424541885fd79c06fda96929cfd512f9 \
    --borrower 0x1ad4e35388f8e9bfabd4c05961cb8d21ac2dc0c2 \
    --adapter 0x000000000000000000000000000000000000dEaD \
    --pool 0x324963c267C354c7660Ce8CA3F5f167E05649970 \
    --collateral 0x039e2fb66102314ce7b64ce5ce3e5183bc94ad38
```

**7. Contract go/no-go — fork-replay against live Silo** (needs [Foundry](https://getfoundry.sh)):
```bash
cd contracts
git init -q && git clone --depth 1 --branch v1.9.6 https://github.com/foundry-rs/forge-std lib/forge-std
forge test                                                   # unit T1–T6 (offline)
SONIC_RPC=$RPC forge test --match-test Replay -vv             # fork-replay: both replay tests PASS
#   test_Replay_PermissionedMarket_BlocksOutsider -> fresh liquidator is blocked (the moat)
#   test_Replay_DirectPool_WhenWhitelisted        -> with the role: floor holds both ways, +10.155 USDC
```

---

## Run the tests (the push gate)

```bash
python3 -m analysis.test_live_detector
python3 -m analysis.test_backtest_detection
python3 -m analysis.test_route_a_builder
python3 -m analysis.test_market_value
python3 -m analysis.test_paper_trader
cd contracts && forge test            # when the contract changes
```
All green is the condition for any push (see `CLAUDE.md`).

---

## Hard rules (unchanged all track)

- **No transactions** — read-only / paper only; capital at risk = 0. The contract sends
  no live on-chain transactions in this project (fork-replay/simulation only).
- **No secrets** — no private or API keys in the repo, code, logs, or history.
- **No external data sends** — read the public RPC only.
- Any live/capital step (funded key, deploy, real submission, own node) needs explicit
  approval.
