"""The exception taxonomy.

This enum is doing three jobs at once, which is deliberate: it is the routing
key out of Tier 1, the label set the evaluation scores against, and the
vocabulary the agent's Finding must speak. Keeping them one type means a new
class cannot be added to the router without also appearing in the eval.
"""

from __future__ import annotations

from enum import StrEnum


class ExceptionClass(StrEnum):
    MISSING_IN_2B = "MISSING_IN_2B"
    MISSING_IN_BOOKS = "MISSING_IN_BOOKS"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    GSTIN_MISMATCH = "GSTIN_MISMATCH"
    DUPLICATE = "DUPLICATE"
    CANCELLED_IRN = "CANCELLED_IRN"
    CREDIT_NOTE_UNLINKED = "CREDIT_NOTE_UNLINKED"
    RCM = "RCM"
    TIME_BARRED = "TIME_BARRED"


class Tier(StrEnum):
    DETERMINISTIC = "TIER_1"
    INVESTIGATION = "TIER_2"
    RECOVERY = "TIER_3"


class ImsAction(StrEnum):
    """The only actions the IMS accepts, plus the absence of one.

    NO_ACTION is not a fourth option offered by the portal -- it is what
    happens when nothing is submitted, and it is treated as ACCEPT at the
    cut-off. It is modelled explicitly so that "we did nothing" can never be
    confused with a decision someone made.
    """

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    PENDING = "PENDING"
    NO_ACTION = "NO_ACTION"


# Which tier owns each class. Tier 1 classes are closed by rule; the rest are
# routed onward. Measured in the tiering ablation rather than assumed -- if the
# agent turns out to win on a class listed here, the routing table moves.
DEFAULT_ROUTING: dict[ExceptionClass, Tier] = {
    ExceptionClass.MISSING_IN_2B: Tier.RECOVERY,
    ExceptionClass.MISSING_IN_BOOKS: Tier.INVESTIGATION,
    ExceptionClass.AMOUNT_MISMATCH: Tier.INVESTIGATION,
    ExceptionClass.GSTIN_MISMATCH: Tier.INVESTIGATION,
    ExceptionClass.CREDIT_NOTE_UNLINKED: Tier.INVESTIGATION,
    ExceptionClass.DUPLICATE: Tier.DETERMINISTIC,
    ExceptionClass.CANCELLED_IRN: Tier.DETERMINISTIC,
    ExceptionClass.RCM: Tier.DETERMINISTIC,
    ExceptionClass.TIME_BARRED: Tier.DETERMINISTIC,
}
