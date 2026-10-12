// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @title AcbTypes
/// @notice Numeric enums shared by Solidity and Python (spec section 0.2). Never renumber.
library AcbTypes {
    // ---- Grade ----
    uint8 internal constant GRADE_NONE = 0;
    uint8 internal constant GRADE_NEW = 1;
    uint8 internal constant GRADE_D = 2;
    uint8 internal constant GRADE_C = 3;
    uint8 internal constant GRADE_B = 4;
    uint8 internal constant GRADE_A = 5;

    // ---- Grade mask bits (1 << grade) ----
    uint8 internal constant MASK_NONE = 0x01;
    uint8 internal constant MASK_NEW = 0x02;
    uint8 internal constant MASK_D = 0x04;
    uint8 internal constant MASK_C = 0x08;
    uint8 internal constant MASK_B = 0x10;
    uint8 internal constant MASK_A = 0x20;
    /// @dev Every grade except D; reported as the mask when a D payee is blocked outside a tier mask.
    uint8 internal constant MASK_NOT_D = 0x3B;

    // ---- Tier ----
    uint8 internal constant TIER_MAKER_CHECKER = 0;
    uint8 internal constant TIER_SMALL = 1;
    uint8 internal constant TIER_STANDARD = 2;
    uint8 internal constant TIER_EXTENDED = 3;
    uint8 internal constant TIER_MAX = 3;
    uint8 internal constant TIER_FROZEN = 255;

    // ---- Decision (6 reserved, retired CREDIT) ----
    uint8 internal constant DECISION_NONE = 0;
    uint8 internal constant DECISION_PAY = 1;
    uint8 internal constant DECISION_PAY_CAPPED = 2;
    uint8 internal constant DECISION_ESCROW = 3;
    uint8 internal constant DECISION_REFUSE = 4;
    uint8 internal constant DECISION_NEEDS_OWNER_APPROVAL = 5;

    // ---- OrderState (6 reserved, retired CREDIT_DRAW) ----
    uint8 internal constant STATE_NONE = 0;
    uint8 internal constant STATE_PAID = 1;
    uint8 internal constant STATE_ESCROWED = 2;
    uint8 internal constant STATE_RELEASED = 3;
    uint8 internal constant STATE_REFUNDED = 4;
    uint8 internal constant STATE_REFUSED = 5;

    // ---- Outcome ----
    uint8 internal constant OUTCOME_NONE = 0;
    uint8 internal constant OUTCOME_ON_TIME = 1;
    uint8 internal constant OUTCOME_LATE = 2;
    uint8 internal constant OUTCOME_FAILED = 3;

    // ---- ViolationKind ----
    uint8 internal constant VK_LIMIT_PROPOSAL = 1;
    uint8 internal constant VK_DUPLICATE_PROPOSAL = 2;
    uint8 internal constant VK_FROZEN_PROPOSAL = 3;
    uint8 internal constant VK_PROVENANCE_AMOUNT = 4;
    uint8 internal constant VK_PROVENANCE_PAYEE = 5;
    uint8 internal constant VK_INJECTION_FOLLOWED = 6;
    uint8 internal constant VK_PRIVILEGE_ESCALATION = 7;
    uint8 internal constant VK_DRAIN_ATTEMPT = 8;
    uint8 internal constant VK_UNKNOWN_TOOL = 9;
    uint8 internal constant VK_SECRET_EXFILTRATION = 10;
    uint8 internal constant VK_REPEATED_VIOLATIONS = 11;

    // ---- Violation severity ----
    uint8 internal constant SEV_MINOR = 1;
    uint8 internal constant SEV_SERIOUS = 2;
    uint8 internal constant SEV_SEVERE = 3;

    // ---- Reason codes used on-chain (Appendix A) ----
    uint16 internal constant R_OWNER_APPROVED = 2007;
    uint16 internal constant R_REFUND_NON_DELIVERY = 2400;
    uint16 internal constant R_REFUND_RISK_ROLLBACK = 2401;
    uint16 internal constant R_REFUND_TIMEOUT = 2402;
    uint16 internal constant R_REFUND_DISPUTED_AFTER_CLAIM = 2403;
    uint16 internal constant R_DEMOTE_SERIOUS_VIOLATION = 3100;
    uint16 internal constant R_DEMOTE_MINOR_VIOLATIONS = 3101;
    uint16 internal constant R_DEMOTE_OWNER_LIMIT = 3104;
    uint16 internal constant R_FREEZE_SEVERE_VIOLATION = 3200;

    /// @notice Grade rank (spec 0.2): D=0, C=1, NONE=2, NEW=2, B=3, A=4. Used for the upgrade delay.
    function rank(uint8 grade) internal pure returns (uint8) {
        if (grade == GRADE_A) return 4;
        if (grade == GRADE_B) return 3;
        if (grade == GRADE_C) return 1;
        if (grade == GRADE_D) return 0;
        return 2; // NONE, NEW (and any unknown value, treated as unrated)
    }
}
