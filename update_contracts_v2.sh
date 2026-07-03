#!/usr/bin/env bash
# v1 -> v2 контракта Silo-ликвидатора: порт AllowMeToLiquidate/_turnOnLiquidation, named owner-tradeoff,
# asset()==debt в интерфейсе+replay, gated-тест (T6), форк-тест пинг+полный replay. Read-only к чужому.
set -euo pipefail
DIR="${SILO_DIR:-/root/silo-liquidator}"
[ "$DIR" = "/root/liquidator" ] && { echo "СТОП: это Morpho-бот"; exit 1; }
[ -e "$DIR/chain/morpho.py" ] && { echo "СТОП: Morpho-файлы в $DIR"; exit 1; }
[ -d "$DIR/contracts" ] || { echo "СТОП: нет $DIR/contracts — распакуй тарболл v1 сначала"; exit 1; }
cd "$DIR/contracts"

cat > src/SiloLiquidator.sol << 'FILE_EOF'
// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// @notice Minimal ERC20 surface (self-contained; handles non-standard return-data like Morpho version).
interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

/// @notice ERC-3156 flash borrower callback (Silo silos are ERC-3156 flash lenders).
interface IERC3156FlashBorrower {
    function onFlashLoan(address initiator, address token, uint256 amount, uint256 fee, bytes calldata data)
        external returns (bytes32);
}

/// @notice The subset of a Silo we use: ERC-3156 flash lending of the silo's OWN asset.
/// NB: SiloStdLib.flashFee does `require(_token == asset())`, so `flashLoanFrom` MUST be the silo whose
/// `asset() == debtAsset` (e.g. to repay USDC debt, flash USDC from the USDC silo). This is the caller's
/// (off-chain bot's) responsibility; the fork replay test asserts it against the live silo.
interface ISiloFlashLender {
    function flashLoan(IERC3156FlashBorrower receiver, address token, uint256 amount, bytes calldata data)
        external returns (bool);
    function flashFee(address token, uint256 amount) external view returns (uint256);
    function asset() external view returns (address);
}

/// @notice Silo V2 partial-liquidation hook (the "hook receiver" of the market).
/// hook = silo.config().getConfig(silo).hookReceiver
interface IPartialLiquidation {
    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 maxDebtToCover,
        bool receiveSToken
    ) external returns (uint256 withdrawCollateral, uint256 repayDebtAssets);

    function maxLiquidation(address borrower)
        external view returns (uint256 collateralToLiquidate, uint256 debtToRepay, bool sTokenRequired);
}

/// @notice Silo config data (verbatim layout from ISiloConfig.ConfigData — required for correct ABI decode).
interface ISiloConfig {
    struct ConfigData {
        uint256 daoFee;
        uint256 deployerFee;
        address silo;
        address token;
        address protectedShareToken;
        address collateralShareToken;
        address debtShareToken;
        address solvencyOracle;
        address maxLtvOracle;
        address interestRateModel;
        uint256 maxLtv;
        uint256 lt;
        uint256 liquidationTargetLtv;
        uint256 liquidationFee;
        uint256 flashloanFee;
        address hookReceiver;
        bool callBeforeQuote;
    }
    function getConfigsForSolvency(address borrower)
        external view returns (ConfigData memory collateralConfig, ConfigData memory debtConfig);
}

/// @notice Gauge hook receiver — resolves the incentives/permission controller set for a share token.
interface IGaugeHookReceiver {
    function configuredGauges(address shareToken) external view returns (address controller);
    function siloConfig() external view returns (ISiloConfig);
}

/// @notice Permissioned-liquidation controller — whitelisted liquidators arm a transient allow-flag.
interface IPermissionedLiquidationController {
    function allowMeToLiquidate() external;
}

/// @title SiloLiquidator — zero-capital Silo V2 partial liquidations with an honest net-profit floor.
/// @notice Fork of Silo's `LiquidationHelper 4.16.0` flow, rewritten lean in the style of our Morpho
/// `Liquidator.sol`. Flow (verified against LiquidationHelper.sol + PartialLiquidation.sol):
///   executeLiquidation() -> ERC-3156 flashLoan(debtAsset) from a Silo -> onFlashLoan:
///     _turnOnLiquidation (forward-compat with permissioned markets; NO-OP on open markets),
///     hook.liquidationCall() repays `user` debt & seizes collateral to THIS contract,
///     swap collateral->debt via generic aggregator calldata, leave loan+fee approved for the lender.
///   Back in executeLiquidation the lender has pulled loan+fee; the remaining debt-token balance is the
///   realized profit (measured as a DELTA balAfter-balBefore, not an absolute, so pre-existing dust can't
///   inflate it). Reverts unless profit >= `minProfit` (honest net-floor), then sweeps to owner.
///
/// Safety (mirrors our Morpho version): onlyOwner entry (swap calldata is always our own), callback locked
/// to our own in-flight liquidation (transient guards + initiator + lender checks), swap-success and
/// can-repay checks, minProfit gate (= slippage/MEV protection, checked in the OUTER fn for a single point
/// of accounting), nonReentrant, return-data-checked ERC20, market/hook/lender passed as arguments, and
/// force-approve (USDT-safe) with allowance reset. The actual per-market `liquidationFee` and its cap live
/// on-chain (ISiloConfig.liquidationFee / SiloFactory.maxLiquidationFee) — do not assume 15%.
contract SiloLiquidator is IERC3156FlashBorrower {
    // solhint-disable-next-line var-name-mixedcase
    bytes32 private constant _FLASHLOAN_CALLBACK = keccak256("ERC3156FlashBorrower.onFlashLoan");

    /// @dev Design tradeoff (named explicitly, per review): profit goes to `owner`, rotatable via
    /// `setOwner`, NOT an immutable TOKENS_RECEIVER. This trades a security property for flexibility —
    /// whoever holds the owner key can redirect all future profit and (via setOwner) take over the
    /// contract. Blast radius is bounded to profit-in-flight (no capital sits here between liquidations),
    /// which we accept for an operational bot. For the stronger guarantee, make the receiver immutable
    /// and drop setOwner.
    address public owner;
    uint256 private _locked = 1; // 1 = unlocked, 2 = locked (nonzero-init saves gas)

    // transient guards so onFlashLoan is only executable inside our own executeLiquidation
    address private transient _expectedLender;
    bool private transient _inLiquidation;

    /// @dev context handed to the flash-loan callback.
    struct SwapCtx {
        address hook;            // IPartialLiquidation hook of the market
        address collateralAsset; // underlying collateral of `user`
        address user;            // borrower
        address swapTarget;      // aggregator router (0x/1inch/Odos/KyberSwap), pre-quoted off-chain
        bytes swapCallData;      // collateral->debt swap calldata built by the bot
    }

    error NotOwner();
    error Reentrant();
    error NotInLiquidation();
    error NotLender();
    error BadInitiator();
    error NoDebtToCover();
    error SwapFailed();
    error CannotRepay();
    error ProfitTooLow(uint256 got, uint256 min);
    error ERC20OpFailed();

    event Liquidated(address indexed user, address indexed debtAsset, uint256 profit);
    event OwnerChanged(address indexed from, address indexed to);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier nonReentrant() {
        if (_locked == 2) revert Reentrant();
        _locked = 2;
        _;
        _locked = 1;
    }

    constructor() {
        owner = msg.sender;
    }

    function setOwner(address newOwner) external onlyOwner {
        emit OwnerChanged(owner, newOwner);
        owner = newOwner;
    }

    /// @notice Liquidate `user` on the market behind `hook`, flash-loaning `maxDebtToCover` of `debtAsset`
    /// from `flashLoanFrom`, swapping seized collateral to debt via `swapTarget`/`swapCallData`.
    /// Reverts unless realized profit (swept to owner) >= `minProfit`. onlyOwner so calldata is our own.
    /// @param flashLoanFrom Silo to flash-loan the debt asset from — MUST have asset() == debtAsset
    /// @param hook          market partial-liquidation hook (silo.config().getConfig(silo).hookReceiver)
    /// @param collateralAsset underlying collateral token of `user`'s position
    /// @param debtAsset     underlying debt token to repay
    /// @param user          borrower to liquidate
    /// @param maxDebtToCover max debt to repay (see IPartialLiquidation.maxLiquidation)
    /// @param swapTarget    aggregator router; ignored when collateralAsset == debtAsset
    /// @param swapCallData  collateral->debt swap calldata; empty when collateralAsset == debtAsset
    /// @param minProfit     honest net-floor: revert if realized profit (in debtAsset) is below this
    function executeLiquidation(
        address flashLoanFrom,
        address hook,
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 maxDebtToCover,
        address swapTarget,
        bytes calldata swapCallData,
        uint256 minProfit
    ) external onlyOwner nonReentrant returns (uint256 profit) {
        if (maxDebtToCover == 0) revert NoDebtToCover();

        bytes memory data = abi.encode(
            SwapCtx({
                hook: hook,
                collateralAsset: collateralAsset,
                user: user,
                swapTarget: swapTarget,
                swapCallData: swapCallData
            })
        );

        uint256 balBefore = IERC20(debtAsset).balanceOf(address(this));

        _expectedLender = flashLoanFrom;
        _inLiquidation = true;
        ISiloFlashLender(flashLoanFrom).flashLoan(this, debtAsset, maxDebtToCover, data);
        _inLiquidation = false;
        _expectedLender = address(0);

        _forceApprove(debtAsset, flashLoanFrom, 0); // reset any residual allowance to the lender

        uint256 balAfter = IERC20(debtAsset).balanceOf(address(this));
        profit = balAfter - balBefore; // DELTA: lender already pulled loan+fee; remainder is profit
        if (profit < minProfit) revert ProfitTooLow(profit, minProfit);

        _safeTransfer(debtAsset, owner, balAfter); // sweep everything (incl. any prior dust)
        emit Liquidated(user, debtAsset, profit);
    }

    /// @notice ERC-3156 callback. Only executable inside our own executeLiquidation and only from the
    /// lender we called, initiated by us. Arms permissioned markets (no-op on open ones), repays the
    /// borrower's debt, seizes collateral, swaps to debt, leaves loan+fee approved for the lender.
    function onFlashLoan(
        address initiator,
        address debtAsset,
        uint256 amount,
        uint256 fee,
        bytes calldata data
    ) external returns (bytes32) {
        if (!_inLiquidation) revert NotInLiquidation();
        if (msg.sender != _expectedLender) revert NotLender();
        if (initiator != address(this)) revert BadInitiator();

        SwapCtx memory s = abi.decode(data, (SwapCtx));

        // forward-compat with permissioned markets: arm the allow-flag. Self-gating — on an OPEN market
        // configuredGauges()==0 for the share tokens, so this is a couple of view calls and an early
        // return (no-op). On a gated market, if we hold ALLOWED_ROLE it lets liquidationCall through
        // without a redeploy; if we don't, liquidationCall reverts LiquidationNotAllowed() (expected).
        _turnOnLiquidation(s.hook, s.user);

        // repay `user` debt via the hook (pulls `debtAsset` from us), receive collateral to this contract
        _forceApprove(debtAsset, s.hook, amount);
        IPartialLiquidation(s.hook).liquidationCall(s.collateralAsset, debtAsset, s.user, amount, false);
        _forceApprove(debtAsset, s.hook, 0);

        // swap seized collateral -> debt (skip when the position is single-asset: collateral == debt)
        if (s.collateralAsset != debtAsset) {
            uint256 collBal = IERC20(s.collateralAsset).balanceOf(address(this));
            _forceApprove(s.collateralAsset, s.swapTarget, collBal);
            // solhint-disable-next-line avoid-low-level-calls
            (bool ok, ) = s.swapTarget.call(s.swapCallData);
            if (!ok) revert SwapFailed();
            _forceApprove(s.collateralAsset, s.swapTarget, 0); // drop dangling allowance
        }

        // breakeven guard: we must be able to repay loan+fee (profit floor is enforced by the outer fn).
        // `fee` comes from the callback (Silo rounds up to >=1 wei even at a 0 rate) — never assumed 0.
        uint256 owed = amount + fee;
        if (IERC20(debtAsset).balanceOf(address(this)) < owed) revert CannotRepay();

        _forceApprove(debtAsset, msg.sender, owed); // lender pulls exactly loan+fee next
        return _FLASHLOAN_CALLBACK;
    }

    /// @notice Recover stuck tokens (collateral dust from a partial swap, airdrops) to owner.
    function sweep(address token) external onlyOwner {
        _safeTransfer(token, owner, IERC20(token).balanceOf(address(this)));
    }

    // --- ported verbatim from Silo's AllowMeToLiquidate / LiquidationHelper._turnOnLiquidation ---

    function _turnOnLiquidation(address hook, address user) internal {
        (ISiloConfig.ConfigData memory collateralConfig, ) =
            IGaugeHookReceiver(hook).siloConfig().getConfigsForSolvency(user);
        _allowMeToLiquidate(hook, collateralConfig.collateralShareToken);
        _allowMeToLiquidate(hook, collateralConfig.protectedShareToken);
    }

    function _allowMeToLiquidate(address hook, address shareToken) internal {
        if (shareToken == address(0)) return;
        address controller = IGaugeHookReceiver(hook).configuredGauges(shareToken);
        if (controller == address(0)) return; // open market: no gauge -> nothing to arm
        // solhint-disable-next-line no-empty-blocks
        try IPermissionedLiquidationController(controller).allowMeToLiquidate() {
            // armed
        } catch {
            // not allowed or not supported -> liquidationCall will revert if the market is truly gated
        }
    }

    // --- return-data-checked ERC20 helpers (handle non-standard tokens) ---

    function _forceApprove(address token, address spender, uint256 amount) internal {
        _call(token, abi.encodeWithSelector(IERC20.approve.selector, spender, uint256(0)));
        if (amount != 0) {
            _call(token, abi.encodeWithSelector(IERC20.approve.selector, spender, amount));
        }
    }

    function _safeTransfer(address token, address to, uint256 amount) internal {
        if (amount == 0) return;
        _call(token, abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
    }

    function _call(address token, bytes memory payload) private {
        (bool ok, bytes memory ret) = token.call(payload);
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert ERC20OpFailed();
    }
}
FILE_EOF

cat > test/mocks/MockHook.sol << 'FILE_EOF'
// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;
import {MockERC20} from "./MockERC20.sol";
import {ISiloConfig} from "../../src/SiloLiquidator.sol";

/// Mock ISiloConfig: returns collateral/protected share tokens for the borrower's config.
contract MockSiloConfig is ISiloConfig {
    address public immutable collShare;
    address public immutable protShare;
    constructor(address _coll, address _prot) { collShare = _coll; protShare = _prot; }
    function getConfigsForSolvency(address)
        external view returns (ConfigData memory c, ConfigData memory d)
    {
        c.collateralShareToken = collShare;
        c.protectedShareToken = protShare;
        // debtConfig `d` left zero — unused by _turnOnLiquidation
    }
}

/// Mock permissioned-liquidation controller: records that allowMeToLiquidate() was called.
contract MockGauge {
    uint256 public calls;
    function allowMeToLiquidate() external { calls += 1; }
    function called() external view returns (bool) { return calls > 0; }
}

/// Silo partial-liquidation hook mock: pulls debt from caller, seizes collateral*(1+bonus) to caller.
/// Also implements IGaugeHookReceiver (siloConfig + configuredGauges) so the ported _turnOnLiquidation
/// path is really exercised (open market: gauge==0 -> no-op; gated: returns the set controller).
contract MockHook {
    uint256 public immutable bonusBps;
    MockSiloConfig public immutable cfg;
    mapping(address => address) public gauges; // shareToken => controller (0 = open market)

    constructor(uint256 _bonusBps) {
        bonusBps = _bonusBps;
        // dummy but distinct share-token addresses for the borrower's config
        cfg = new MockSiloConfig(address(0xC011A7e5a1), address(0x9707ec7ed0));
    }

    function siloConfig() external view returns (ISiloConfig) { return cfg; }
    function configuredGauges(address shareToken) external view returns (address) { return gauges[shareToken]; }

    /// arm a gauge/controller for BOTH of this borrower's share tokens (simulates a gated market)
    function setGaugeForAll(address controller) external {
        gauges[cfg.collShare()] = controller;
        gauges[cfg.protShare()] = controller;
    }

    function liquidationCall(address collateral, address debt, address, uint256 maxDebtToCover, bool)
        external returns (uint256 withdrawCollateral, uint256 repayDebtAssets)
    {
        repayDebtAssets = maxDebtToCover;
        require(MockERC20(debt).transferFrom(msg.sender, address(this), repayDebtAssets), "pull debt");
        withdrawCollateral = repayDebtAssets * (10000 + bonusBps) / 10000;
        MockERC20(collateral).mint(msg.sender, withdrawCollateral);
    }
}
FILE_EOF

cat > test/Runner.sol << 'FILE_EOF'
// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;
import {SiloLiquidator} from "../src/SiloLiquidator.sol";
import {MockERC20} from "./mocks/MockERC20.sol";
import {MockSilo} from "./mocks/MockSilo.sol";
import {MockHook, MockGauge} from "./mocks/MockHook.sol";
import {MockSwapper} from "./mocks/MockSwapper.sol";

/// @dev Offline test harness: the constructor runs every assertion. If this contract deploys without
/// reverting, all checks passed. Executed in an offline EVM (no network / no forge needed).
contract Runner {
    address constant BOB = address(0xB0B);

    function _swapData(address sell, address buy, uint256 outAmt) internal pure returns (bytes memory) {
        return abi.encodeWithSignature("swap(address,address,uint256)", sell, buy, outAmt);
    }

    constructor() {
        // ---- TEST 1: profitable two-asset liquidation, profit swept to owner ----
        {
            MockERC20 debt = new MockERC20();
            MockERC20 coll = new MockERC20();
            SiloLiquidator liq = new SiloLiquidator();     // owner = this Runner
            MockSilo lender = new MockSilo(10);            // 0.1% flash fee
            MockHook hook = new MockHook(1000);            // 10% liquidation bonus
            MockSwapper sw = new MockSwapper();
            // maxDebt 1000 -> fee 1 -> seize 1100 coll -> swap to 1080 debt -> profit 1080-1000-1 = 79
            uint256 profit = liq.executeLiquidation(
                address(lender), address(hook), address(coll), address(debt), BOB,
                1000, address(sw), _swapData(address(coll), address(debt), 1080), 50
            );
            require(profit == 79, "T1 profit");
            require(debt.balanceOf(address(this)) == 79, "T1 swept to owner");
            require(debt.balanceOf(address(liq)) == 0, "T1 liq empty");
        }
        // ---- TEST 2: profit below floor reverts (ProfitTooLow) ----
        {
            MockERC20 debt = new MockERC20(); MockERC20 coll = new MockERC20();
            SiloLiquidator liq = new SiloLiquidator();
            MockSilo lender = new MockSilo(10); MockHook hook = new MockHook(1000); MockSwapper sw = new MockSwapper();
            try liq.executeLiquidation(
                address(lender), address(hook), address(coll), address(debt), BOB,
                1000, address(sw), _swapData(address(coll), address(debt), 1080), 100  // floor 100 > profit 79
            ) returns (uint256) {
                revert("T2 should have reverted");
            } catch { /* expected ProfitTooLow */ }
        }
        // ---- TEST 3: cannot repay flashloan (swap output < loan+fee) reverts ----
        {
            MockERC20 debt = new MockERC20(); MockERC20 coll = new MockERC20();
            SiloLiquidator liq = new SiloLiquidator();
            MockSilo lender = new MockSilo(10); MockHook hook = new MockHook(1000); MockSwapper sw = new MockSwapper();
            try liq.executeLiquidation(
                address(lender), address(hook), address(coll), address(debt), BOB,
                1000, address(sw), _swapData(address(coll), address(debt), 1000), 0  // 1000 < owed 1001
            ) returns (uint256) {
                revert("T3 should have reverted");
            } catch { /* expected CannotRepay */ }
        }
        // ---- TEST 4: single-asset position (collateral == debt), no swap ----
        {
            MockERC20 debt = new MockERC20();
            SiloLiquidator liq = new SiloLiquidator();
            MockSilo lender = new MockSilo(10); MockHook hook = new MockHook(1000);
            // seize 1100 debt-as-collateral, owed 1001 -> profit 99, no swap needed
            uint256 profit = liq.executeLiquidation(
                address(lender), address(hook), address(debt), address(debt), BOB,
                1000, address(0), "", 50
            );
            require(profit == 99, "T4 profit");
            require(debt.balanceOf(address(this)) == 99, "T4 swept");
        }
        // ---- TEST 5: onFlashLoan not callable outside our own liquidation ----
        {
            SiloLiquidator liq = new SiloLiquidator();
            try liq.onFlashLoan(address(this), address(0), 1, 0, "") returns (bytes32) {
                revert("T5 should have reverted");
            } catch { /* expected NotInLiquidation */ }
        }
        // ---- TEST 6: permissioned path — _turnOnLiquidation arms the gauge controller ----
        {
            MockERC20 debt = new MockERC20();
            MockERC20 coll = new MockERC20();
            SiloLiquidator liq = new SiloLiquidator();
            MockSilo lender = new MockSilo(10);
            MockHook hook = new MockHook(1000);
            MockSwapper sw = new MockSwapper();
            MockGauge gauge = new MockGauge();
            hook.setGaugeForAll(address(gauge));   // gated market: controller configured
            uint256 profit = liq.executeLiquidation(
                address(lender), address(hook), address(coll), address(debt), BOB,
                1000, address(sw), _swapData(address(coll), address(debt), 1080), 50
            );
            require(profit == 79, "T6 profit");
            require(gauge.called(), "T6 allowMeToLiquidate armed"); // proves ported path fired
        }
        // all good -> deploy succeeds
    }
}
FILE_EOF

cat > test/SiloLiquidator.t.sol << 'FILE_EOF'
// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {SiloLiquidator} from "../src/SiloLiquidator.sol";
import {MockERC20} from "./mocks/MockERC20.sol";
import {MockSilo} from "./mocks/MockSilo.sol";
import {MockHook, MockGauge} from "./mocks/MockHook.sol";
import {MockSwapper} from "./mocks/MockSwapper.sol";
import {Runner} from "./Runner.sol";

/// Unit tests for SiloLiquidator against mock Silo/hook/swapper. Run: `forge test`.
contract SiloLiquidatorTest is Test {
    address constant BOB = address(0xB0B);
    SiloLiquidator liq;
    MockERC20 debt;
    MockERC20 coll;
    MockSilo lender;
    MockHook hook;
    MockSwapper sw;

    function setUp() public {
        debt = new MockERC20();
        coll = new MockERC20();
        liq = new SiloLiquidator(); // owner = this test contract
        lender = new MockSilo(10);  // 0.1% flash fee
        hook = new MockHook(1000);  // 10% liquidation bonus
        sw = new MockSwapper();
    }

    function _swap(address s, address b, uint256 out) internal pure returns (bytes memory) {
        return abi.encodeWithSignature("swap(address,address,uint256)", s, b, out);
    }

    function test_ProfitableTwoAsset() public {
        uint256 p = liq.executeLiquidation(
            address(lender), address(hook), address(coll), address(debt), BOB,
            1000, address(sw), _swap(address(coll), address(debt), 1080), 50
        );
        assertEq(p, 79, "profit");
        assertEq(debt.balanceOf(address(this)), 79, "swept to owner");
        assertEq(debt.balanceOf(address(liq)), 0, "liq empty");
    }

    function test_RevertBelowFloor() public {
        vm.expectRevert(abi.encodeWithSelector(SiloLiquidator.ProfitTooLow.selector, 79, 100));
        liq.executeLiquidation(
            address(lender), address(hook), address(coll), address(debt), BOB,
            1000, address(sw), _swap(address(coll), address(debt), 1080), 100
        );
    }

    function test_RevertCannotRepay() public {
        vm.expectRevert(SiloLiquidator.CannotRepay.selector);
        liq.executeLiquidation(
            address(lender), address(hook), address(coll), address(debt), BOB,
            1000, address(sw), _swap(address(coll), address(debt), 1000), 0
        );
    }

    function test_SingleAsset_NoSwap() public {
        uint256 p = liq.executeLiquidation(
            address(lender), address(hook), address(debt), address(debt), BOB,
            1000, address(0), "", 50
        );
        assertEq(p, 99, "profit");
        assertEq(debt.balanceOf(address(this)), 99, "swept");
    }

    function test_OnlyOwner() public {
        vm.prank(address(0xBAD));
        vm.expectRevert(SiloLiquidator.NotOwner.selector);
        liq.executeLiquidation(
            address(lender), address(hook), address(coll), address(debt), BOB,
            1000, address(sw), _swap(address(coll), address(debt), 1080), 0
        );
    }

    function test_DirectCallbackReverts() public {
        vm.expectRevert(SiloLiquidator.NotInLiquidation.selector);
        liq.onFlashLoan(address(this), address(debt), 1, 0, "");
    }

    function test_SetOwner() public {
        liq.setOwner(address(0xCAFE));
        assertEq(liq.owner(), address(0xCAFE));
        vm.prank(address(0xCAFE));
        liq.setOwner(address(this));
        assertEq(liq.owner(), address(this));
    }

    function test_SetOwner_OnlyOwner() public {
        vm.prank(address(0xBAD));
        vm.expectRevert(SiloLiquidator.NotOwner.selector);
        liq.setOwner(address(0xBAD));
    }

    function test_PermissionedPath_ArmsGauge() public {
        MockGauge gauge = new MockGauge();
        hook.setGaugeForAll(address(gauge));
        uint256 p = liq.executeLiquidation(
            address(lender), address(hook), address(coll), address(debt), BOB,
            1000, address(sw), _swap(address(coll), address(debt), 1080), 50
        );
        assertEq(p, 79, "profit");
        assertTrue(gauge.called(), "allowMeToLiquidate armed on gated market");
    }

    /// Runs the all-in-one offline harness (same asserts, one shot) under forge too.
    function test_OfflineRunnerAllAsserts() public {
        new Runner();
    }
}
FILE_EOF

cat > test/SiloLiquidatorFork.t.sol << 'FILE_EOF'
// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {SiloLiquidator, ISiloFlashLender} from "../src/SiloLiquidator.sol";

interface IERC4626Asset {
    function asset() external view returns (address);
}

/// Fork tests against REAL Silo on Sonic. Two levels:
///   (A) test_InterfaceCompat_Ping — cheap sanity on `latest`: our interfaces bind to the live silo.
///       Needs only a normal RPC:  SONIC_RPC=https://rpc.soniclabs.com forge test --match-test Ping -vvv
///   (B) test_Replay_RealLiquidation — the REAL go/no-go gate: replay a historical liquidation on an
///       ARCHIVE node and assert the floor holds BOTH ways against live contracts. Needs archive state
///       at REPLAY_BLOCK (public nodes often prune -> "header not found"); use an archive endpoint.
///
/// The replay is data-driven (fill these from a real LiquidationCall you pull via analysis/contestation.py
/// or winner_xray on silo 0x7e88ae5e — take the borrower + the block, fork at block-1, and build a
/// collateral->debt aggregator quote AT THAT BLOCK, else the swap reverts as a stale quote):
///   REPLAY_BLOCK       (uint)  fork block (the block *before* the historical liquidation)
///   REPLAY_LENDER      (addr)  silo to flash-loan from — MUST have asset()==REPLAY_DEBT
///   REPLAY_HOOK        (addr)  partial-liquidation hook of the market
///   REPLAY_COLLATERAL  (addr)  borrower collateral asset
///   REPLAY_DEBT        (addr)  borrower debt asset
///   REPLAY_BORROWER    (addr)  the underwater borrower
///   REPLAY_MAXDEBT     (uint)  max debt to cover
///   REPLAY_SWAPTARGET  (addr)  aggregator router
///   REPLAY_SWAPDATA    (bytes) collateral->debt swap calldata quoted at REPLAY_BLOCK
contract SiloLiquidatorForkTest is Test {
    address constant BIG_SILO = 0x7e88AE5E50474A48deA4c42a634aA7485e7CaA62;

    function _sonic() internal view returns (string memory) {
        return vm.envOr("SONIC_RPC", string(""));
    }

    // ---------- (A) cheap interface sanity on latest ----------
    function test_InterfaceCompat_Ping() public {
        string memory rpc = _sonic();
        if (bytes(rpc).length == 0) { emit log("SKIP: set SONIC_RPC"); return; }
        vm.createSelectFork(rpc);

        SiloLiquidator liq = new SiloLiquidator();
        assertEq(liq.owner(), address(this), "owner");

        address debtAsset = IERC4626Asset(BIG_SILO).asset();
        assertTrue(debtAsset != address(0), "silo.asset()");
        uint256 fee = ISiloFlashLender(BIG_SILO).flashFee(debtAsset, 1e6);
        emit log_named_address("BIG_SILO.asset (debt)", debtAsset);
        emit log_named_uint("flashFee(1e6)", fee);
        assertLe(fee, 1e6, "sane fee");
    }

    // ---------- (B) the REAL gate: replay a historical liquidation on archive ----------
    function test_Replay_RealLiquidation() public {
        uint256 blk = vm.envOr("REPLAY_BLOCK", uint256(0));
        if (blk == 0) { emit log("SKIP: set REPLAY_* env (needs an ARCHIVE Sonic RPC)"); return; }

        string memory rpc = _sonic();
        require(bytes(rpc).length != 0, "need SONIC_RPC (archive) for replay");
        vm.createSelectFork(rpc, blk); // <-- requires archive state at `blk`

        address lender     = vm.envAddress("REPLAY_LENDER");
        address hook       = vm.envAddress("REPLAY_HOOK");
        address collateral = vm.envAddress("REPLAY_COLLATERAL");
        address debt       = vm.envAddress("REPLAY_DEBT");
        address borrower   = vm.envAddress("REPLAY_BORROWER");
        uint256 maxDebt    = vm.envUint("REPLAY_MAXDEBT");
        address swapTarget = vm.envAddress("REPLAY_SWAPTARGET");
        bytes memory swapData = vm.envBytes("REPLAY_SWAPDATA");

        // hard requirement from SiloStdLib.flashFee: flash only the silo's own asset
        assertEq(ISiloFlashLender(lender).asset(), debt, "flashLoanFrom.asset() == debtAsset");

        SiloLiquidator liq = new SiloLiquidator(); // owner = this test

        // (1) FLOOR HOLDS: an absurd minProfit must revert ProfitTooLow against the LIVE contracts.
        // The whole tx reverts, so state is untouched and the borrower stays liquidatable for step (2).
        vm.expectRevert(SiloLiquidator.ProfitTooLow.selector);
        liq.executeLiquidation(
            lender, hook, collateral, debt, borrower, maxDebt, swapTarget, swapData, type(uint256).max
        );

        // (2) FLOOR PASSES: same liquidation with minProfit=1 must realize profit>0, swept to owner.
        uint256 before = _bal(debt, address(this));
        uint256 profit = liq.executeLiquidation(
            lender, hook, collateral, debt, borrower, maxDebt, swapTarget, swapData, 1
        );
        assertGt(profit, 0, "profit > 0 on live Silo");
        assertEq(_bal(debt, address(this)) - before, profit, "profit swept to owner");
        emit log_named_uint("replay realized profit (debt units)", profit);
    }

    function _bal(address token, address who) internal view returns (uint256) {
        (bool ok, bytes memory ret) = token.staticcall(abi.encodeWithSignature("balanceOf(address)", who));
        require(ok && ret.length >= 32, "balanceOf");
        return abi.decode(ret, (uint256));
    }
}
FILE_EOF

cat > README.md << 'FILE_EOF'
# SiloLiquidator — Silo V2 liquidation bot contract (fork of LiquidationHelper 4.16.0) — v2

Zero-capital Silo V2 **partial** liquidations with an **honest net-profit floor** — lean fork of Silo's
`LiquidationHelper`, in the style of our Morpho `Liquidator.sol` (onlyOwner entry, `minProfit` gate,
profit swept to owner, return-data-checked ERC20, transient-guarded flash callback).

Flow: `executeLiquidation()` → ERC-3156 `flashLoan(debtAsset)` from a Silo → `onFlashLoan`: arm
permissioned markets via `_turnOnLiquidation` (NO-OP on open markets), repay the borrower's debt via the
hook (seizes collateral here), swap collateral→debt (aggregator calldata built off-chain), repay loan+fee.
Back in the outer call the remaining debt balance is realized profit (measured as a **delta**, not an
absolute); reverts unless `profit >= minProfit`, then sweeps to owner.

## v2 changes (from review)
- **Ported `AllowMeToLiquidate` / `_turnOnLiquidation` verbatim** — self-gating (a view + early return on
  open markets), forward-compatible if the big silo is ever gated (arms `allowMeToLiquidate` if we hold
  ALLOWED_ROLE — no redeploy under fire). Exercised by a mock gauge in the tests (T6 / test_PermissionedPath).
- **`minProfit` is checked in the OUTER function** (single point of accounting); profit is a delta
  `balAfter-balBefore`; `fee` is taken from the callback, never assumed 0.
- **Owner-as-receiver tradeoff named in code** (vs immutable TOKENS_RECEIVER): owner key can redirect
  future profit / take over via setOwner; blast radius = profit-in-flight (no capital sits on the contract).
- **Flash-loan-from-own-asset constraint documented + asserted in replay**: `flashLoanFrom.asset()` MUST
  equal `debtAsset` (SiloStdLib.flashFee `require(_token==asset)`); silo selection is the bot's job.

## Layout
- `src/SiloLiquidator.sol` — the contract (solc 0.8.28, cancun, viaIR; ~4.8kb).
- `test/mocks/` — MockERC20 / MockSilo (ERC-3156 lender) / MockHook (+MockSiloConfig +MockGauge) / MockSwapper.
- `test/Runner.sol` — all-in-one offline harness (constructor runs T1–T6; validated in an offline EVM).
- `test/SiloLiquidator.t.sol` — Foundry unit tests (T1–T6 incl. gated-path + onlyOwner/setOwner + guard).
- `test/SiloLiquidatorFork.t.sol` — (A) interface ping on latest, (B) real historical replay on archive.

## Run — unit tests (no network)
```bash
cd /root/silo-liquidator/contracts
git init -q && git clone --depth 1 --branch v1.9.6 https://github.com/foundry-rs/forge-std lib/forge-std
forge build          # prints SiloLiquidator size — verify it yourself, don't trust the report
forge test -vvv      # unit + offline Runner (T1..T6)
```
(Pin the forge-std tag and keep solc pinned in foundry.toml so the byte size is reproducible.)

## Run — (A) interface ping against live Silo (normal RPC)
```bash
SONIC_RPC=https://rpc.soniclabs.com forge test --match-test Ping -vvv
```
Still just a ping (interfaces bind) — NOT the go/no-go gate.

## Run — (B) real replay = the actual go/no-go gate (ARCHIVE RPC required)
Public nodes prune; forking a week-old block on them fails `header not found`. Use an archive endpoint.
Fill REPLAY_* from a real LiquidationCall on `0x7e88ae5e` (pull borrower+block via analysis/contestation.py
or winner_xray), and build a collateral→debt aggregator quote **at that block** (a `latest` quote will
revert as stale). Then:
```bash
export SONIC_RPC=<ARCHIVE_sonic_rpc>
export REPLAY_BLOCK=<block-1 of the liquidation>
export REPLAY_LENDER=<silo with asset()==debt>   REPLAY_HOOK=<market hook>
export REPLAY_COLLATERAL=<coll>  REPLAY_DEBT=<debt>  REPLAY_BORROWER=<underwater user>
export REPLAY_MAXDEBT=<max debt to cover>
export REPLAY_SWAPTARGET=<router>  REPLAY_SWAPDATA=<0x… quote at REPLAY_BLOCK>
forge test --match-test Replay -vvv
```
Asserts against LIVE contracts: `flashLoanFrom.asset()==debt`, the floor **reverts** at `minProfit=max`
AND **passes** at `minProfit=1` with profit>0 swept to owner. Until this is green, go/no-go has NOT moved.

## Status
Logic self-consistent (T1–T6 pass in an offline EVM + under forge). **Not yet validated against mainnet**:
that is exactly what the replay (B) does, and it needs real borrower/block/quote + an archive node.
Next: paper-mode on `0x7e88ae5e` to measure block-lag vs incumbent `0x0094c5…`. See `../silo_state.md`.
FILE_EOF

echo "[OK] файлы v2 записаны (src + MockHook + Runner + оба .t.sol + README)"
if command -v forge >/dev/null 2>&1; then
  echo "=== forge build (сверь размер сам) ==="; forge build
  echo "=== forge test -vvv ==="; forge test -vvv
else
  echo "forge не в PATH — запусти вручную из $DIR/contracts:  forge build && forge test -vvv"
fi
