// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title ShitVesting
 * @notice SHIT-20 presale vesting vault for the ShitcoinBtc NEVM sidechain (chain ID 20026).
 *
 *         Presale terms enforced on-chain:
 *           - 12 equal monthly unlocks per beneficiary
 *           - No cliff: the first 1/12 unlocks one month after the schedule start
 *           - Linear: vested(t) = total * min(elapsedMonths, 12) / 12
 *
 *         The contract is ERC-20 compatible under the hood; the user-facing
 *         token standard branding is SHIT-20.
 *
 * @dev Self-contained: no external imports, compiles with solc 0.8.x.
 *      Schedules are non-revocable and non-transferable by design — once the
 *      owner registers a buyer, only time unlocks the funds. The buyer claims
 *      permissionlessly via release(); nobody else can touch their schedule.
 */
interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

contract ShitVesting {
    // ------------------------------------------------------------------------
    // Types & constants
    // ------------------------------------------------------------------------

    struct Schedule {
        uint256 total;    // total SHIT allocated to this beneficiary
        uint256 released; // SHIT already claimed
        uint64  start;    // unix timestamp the 12-month clock starts at
        bool    exists;   // distinguishes "no schedule" from "zero schedule"
    }

    /// @notice Number of equal unlock tranches.
    uint256 public constant PERIODS = 12;
    /// @notice Length of one unlock period (30-day months).
    uint256 public constant PERIOD = 30 days;

    // ------------------------------------------------------------------------
    // State
    // ------------------------------------------------------------------------

    /// @notice The SHIT-20 token being vested.
    IERC20 public immutable token;
    /// @notice Deployer; the only account that may register schedules.
    address public owner;

    mapping(address => Schedule) public schedules;

    // Minimal reentrancy guard (no OZ import needed).
    bool private _locked;

    // ------------------------------------------------------------------------
    // Events
    // ------------------------------------------------------------------------

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event ScheduleRegistered(address indexed beneficiary, uint256 amount, uint64 start);
    event TokensReleased(address indexed beneficiary, uint256 amount);

    // ------------------------------------------------------------------------
    // Modifiers
    // ------------------------------------------------------------------------

    modifier onlyOwner() {
        require(msg.sender == owner, "ShitVesting: caller is not the owner");
        _;
    }

    modifier nonReentrant() {
        require(!_locked, "ShitVesting: reentrant call");
        _locked = true;
        _;
        _locked = false;
    }

    // ------------------------------------------------------------------------
    // Construction
    // ------------------------------------------------------------------------

    /**
     * @param token_ Address of the deployed SHIT-20 token contract.
     */
    constructor(address token_) {
        require(token_ != address(0), "ShitVesting: token is zero address");
        token = IERC20(token_);
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "ShitVesting: new owner is zero address");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    // ------------------------------------------------------------------------
    // Schedule registration (owner only, done once at launch)
    // ------------------------------------------------------------------------

    /**
     * @notice Register (or top up) a buyer's vesting schedule.
     * @dev If the beneficiary already has a schedule, `amount` is ADDED to it
     *      and the original start timestamp is kept. The owner must fund this
     *      contract with enough SHIT to cover all registered schedules.
     * @param beneficiary Buyer address on the NEVM chain.
     * @param amount      SHIT amount (in token base units) to vest.
     * @param start       Unix timestamp the 12-month clock starts at.
     */
    function register(address beneficiary, uint256 amount, uint64 start)
        external
        onlyOwner
    {
        require(beneficiary != address(0), "ShitVesting: beneficiary is zero address");
        require(amount > 0, "ShitVesting: amount is zero");

        Schedule storage s = schedules[beneficiary];
        if (s.exists) {
            // Top-up: keep the original start, grow the total.
            s.total += amount;
        } else {
            s.total = amount;
            s.released = 0;
            s.start = start;
            s.exists = true;
        }
        emit ScheduleRegistered(beneficiary, amount, s.start);
    }

    /**
     * @notice Register many schedules in one transaction (cheaper at launch).
     * @dev Arrays must be parallel; every entry shares the same `start`.
     */
    function registerBatch(
        address[] calldata beneficiaries,
        uint256[] calldata amounts,
        uint64 start
    ) external onlyOwner {
        require(
            beneficiaries.length == amounts.length,
            "ShitVesting: length mismatch"
        );
        for (uint256 i = 0; i < beneficiaries.length; i++) {
            address beneficiary = beneficiaries[i];
            uint256 amount = amounts[i];
            require(beneficiary != address(0), "ShitVesting: beneficiary is zero address");
            require(amount > 0, "ShitVesting: amount is zero");

            Schedule storage s = schedules[beneficiary];
            if (s.exists) {
                s.total += amount;
            } else {
                s.total = amount;
                s.released = 0;
                s.start = start;
                s.exists = true;
            }
            emit ScheduleRegistered(beneficiary, amount, s.start);
        }
    }

    // ------------------------------------------------------------------------
    // Claiming (permissionless)
    // ------------------------------------------------------------------------

    /**
     * @notice SHIT currently claimable by `beneficiary`.
     */
    function releasable(address beneficiary) public view returns (uint256) {
        Schedule storage s = schedules[beneficiary];
        if (!s.exists) return 0;
        if (block.timestamp <= s.start) return 0;

        uint256 elapsed = (block.timestamp - s.start) / PERIOD;
        if (elapsed > PERIODS) elapsed = PERIODS;

        uint256 vested = (s.total * elapsed) / PERIODS;
        return vested - s.released;
    }

    /**
     * @notice Claim all currently unlocked SHIT. Anyone may call, but funds
     *         always go to the schedule's beneficiary (msg.sender).
     */
    function release() external nonReentrant {
        uint256 amount = releasable(msg.sender);
        require(amount > 0, "ShitVesting: nothing releasable");

        schedules[msg.sender].released += amount;
        require(token.transfer(msg.sender, amount), "ShitVesting: transfer failed");
        emit TokensReleased(msg.sender, amount);
    }

    // ------------------------------------------------------------------------
    // Views & safety
    // ------------------------------------------------------------------------

    /// @notice SHIT balance held by this vault (must cover unreleased totals).
    function vaultBalance() external view returns (uint256) {
        return token.balanceOf(address(this));
    }

    /**
     * @notice Rescue tokens that are NOT the SHIT-20 token, sent here by mistake.
     * @dev Cannot touch the vested SHIT: the vesting token itself is excluded.
     */
    function sweep(address token_) external onlyOwner nonReentrant {
        require(token_ != address(token), "ShitVesting: cannot sweep vested token");
        uint256 bal = IERC20(token_).balanceOf(address(this));
        require(bal > 0, "ShitVesting: nothing to sweep");
        require(IERC20(token_).transfer(owner, bal), "ShitVesting: sweep failed");
    }
}
