"""Configuration. Tolerances are business decisions, so they are configuration.

Thresholds that carry legal meaning are versioned and date-stamped rather than
hardcoded as constants, because the rules genuinely change: IMS moved from
launch to mandatory hard-blocks between 2024 and 2026. A ruleset that cannot
say which vintage it implements cannot be audited after the fact.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class MatchTolerances(BaseSettings):
    """Fuzzy-match tolerances. The client owns these numbers, not the engine."""

    absolute_tax_paise: Decimal = Field(
        default=Decimal("1.00"),
        description="Rupee-level rounding slack applied to total tax.",
    )
    relative_tax_fraction: Decimal = Field(
        default=Decimal("0.005"),
        description="Proportional slack on total tax, capped by absolute_tax_cap.",
    )
    absolute_tax_cap: Decimal = Field(
        default=Decimal("100.00"),
        description=(
            "Hard ceiling on the proportional allowance, so a large invoice "
            "cannot quietly absorb a large absolute difference."
        ),
    )
    date_window_days: int = 30
    number_similarity_floor: float = 0.82


class PolicyThresholds(BaseSettings):
    """Statutory thresholds. Versioned; verify against current official sources."""

    ruleset_version: str = "2026.07"
    drc01c_absolute_inr: Decimal = Decimal("100000.00")
    drc01c_relative_fraction: Decimal = Decimal("0.20")
    section_16_4_cutoff_month: int = 11
    section_16_4_cutoff_day: int = 30
    ims_cutoff_day: int = 14
    auto_accept_value_ceiling: Decimal = Field(
        default=Decimal("25000.00"),
        description=(
            "Accepts at or below this tax value may proceed without a human; "
            "every reject and every larger accept enters the review queue."
        ),
    )


class AgentBudgets(BaseSettings):
    """Hard bounds on Tier 2. Unbounded agents are how projects die."""

    max_steps: int = 8
    max_wall_clock_seconds: float = 120.0
    max_tokens_per_case: int = 60_000
    loop_repeat_threshold: int = 2


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_prefix="GSTRECON_",
        extra="ignore",
    )

    database_url: str = "postgresql://gstrecon:gstrecon@localhost:5433/gstrecon"
    llm_mode: str = "fake"
    cache_dir: Path = REPO_ROOT / "data" / "llm_cache"
    results_dir: Path = REPO_ROOT / "results"
    random_seed: int = 20260726

    tolerances: MatchTolerances = Field(default_factory=MatchTolerances)
    policy: PolicyThresholds = Field(default_factory=PolicyThresholds)
    budgets: AgentBudgets = Field(default_factory=AgentBudgets)


def load_settings() -> Settings:
    return Settings()


def load_provider_credentials(env_file: Path | None = None) -> list[str]:
    """Copy provider API keys from the gitignored .env into the process env.

    Deliberately separate from ``Settings``. Keys are not modelled as settings
    fields so that they cannot appear in a config dump, a repr, a log line or a
    serialised experiment manifest -- all of which are things this project
    writes to disk and commits.

    Returns the names of the variables that were populated, never the values.
    """
    path = env_file or (REPO_ROOT / ".env")
    if not path.exists():
        return []
    populated: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip("'\"")
        if name.endswith("_API_KEY") and value and name not in os.environ:
            os.environ[name] = value
            populated.append(name)
    return populated
