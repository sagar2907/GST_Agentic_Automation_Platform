"""Interval estimates for the proportions this project reports.

Every headline number here is a proportion measured on a few dozen to a few
hundred cases, so reporting it as a bare percentage would overstate what was
learned. The samples are small enough that the choice of interval actually
matters, which is why this is a module rather than a one-line formula inline.

The normal approximation (p +/- 1.96*sqrt(p(1-p)/n)) is the obvious choice and
the wrong one here. It degrades badly exactly where this project lives -- small
n and proportions near 0 or 1 -- and it produces bounds outside [0, 1], so an
agent that resolved 18 of 18 cases gets an interval of [1.0, 1.0], claiming
certainty from eighteen observations. Wilson's interval stays inside the unit
range and keeps its nominal coverage at these sample sizes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Z_95 = 1.959963984540054


@dataclass(frozen=True, slots=True)
class Proportion:
    """A measured rate with an interval and the sample size behind it."""

    successes: int
    trials: int
    low: float
    high: float

    @property
    def point(self) -> float:
        return self.successes / self.trials if self.trials else 0.0

    @property
    def half_width(self) -> float:
        return (self.high - self.low) / 2

    def render(self) -> str:
        if not self.trials:
            return "not measured (n=0)"
        return f"{self.point:.1%} [{self.low:.1%}, {self.high:.1%}] n={self.trials}"

    def as_dict(self) -> dict[str, float | int]:
        return {
            "successes": self.successes,
            "trials": self.trials,
            "point": round(self.point, 4),
            "ci_low": round(self.low, 4),
            "ci_high": round(self.high, 4),
        }


def wilson(successes: int, trials: int, z: float = Z_95) -> Proportion:
    """Wilson score interval for a binomial proportion.

    Returns a zero-width interval only when there is nothing to measure; a
    caller should treat ``trials == 0`` as "not measured" rather than as 0%.
    """
    if trials <= 0:
        return Proportion(0, 0, 0.0, 0.0)
    phat = successes / trials
    denominator = 1 + z**2 / trials
    centre = (phat + z**2 / (2 * trials)) / denominator
    spread = z * math.sqrt(phat * (1 - phat) / trials + z**2 / (4 * trials**2)) / denominator
    return Proportion(successes, trials, max(0.0, centre - spread), min(1.0, centre + spread))


def mean_or_none(values: list[float]) -> float | None:
    """Mean, or None when there is nothing to average.

    Returning None rather than 0.0 is deliberate. A zero would flow into a
    report as a measured value; None forces the caller to print "not measured",
    which is the honest rendering of an empty sample.
    """
    return sum(values) / len(values) if values else None


def difference_is_significant(left: Proportion, right: Proportion) -> bool:
    """Do two Wilson intervals fail to overlap?

    A deliberately conservative test: non-overlapping intervals imply a
    significant difference, but overlapping ones do not imply the absence of
    one. Used only to decide whether a comparison may be stated as a finding
    or must be reported as inconclusive.
    """
    if not left.trials or not right.trials:
        return False
    return left.high < right.low or right.high < left.low
