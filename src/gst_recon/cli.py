"""Command line entry point.

Two commands. ``reconcile`` runs Tier 1 over a generated cycle and reports the
match rates and the exception taxonomy; it needs no model and no key.
``investigate`` routes the hard residue through the Tier 2 agent, offline by
default and against live providers only when explicitly asked.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import date

from gst_recon.agents.investigate import investigate
from gst_recon.agents.tools import ToolSurface
from gst_recon.config import AgentBudgets, load_provider_credentials, load_settings
from gst_recon.data.generator import DISTRIBUTION_MIX, HARD_MIX, generate
from gst_recon.llm import build_router
from gst_recon.matching.engine import reconcile
from gst_recon.policy.gate import assess_drc01c, days_to_cutoff, decide

BASE_DATE = date(2026, 7, 14)


def _force_utf8_stdout() -> None:
    """Model output is not ASCII, and the Windows console default is not UTF-8.

    A rationale containing a non-breaking hyphen crashed a reporting script
    with a UnicodeEncodeError on cp1252 -- the analysis was fine, the printing
    was not. Anything that renders model text has to say what encoding it
    wants rather than inherit the console's.
    """
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def _cycle(seed: int, clean_pairs: int, mix, near_miss: int = 0):
    return generate(
        seed=seed,
        period="07-2026",
        base_date=BASE_DATE,
        clean_pairs=clean_pairs,
        injections=mix,
        near_miss_pairs=near_miss,
    )


def command_reconcile(args: argparse.Namespace) -> int:
    settings = load_settings()
    dataset = _cycle(args.seed, args.clean_pairs, DISTRIBUTION_MIX, near_miss=args.near_miss)
    result = reconcile(
        dataset.books,
        dataset.portal,
        tolerances=settings.tolerances,
        policy=settings.policy,
        as_of=BASE_DATE,
    )

    print(f"period                {dataset.period}")
    print(f"ruleset               {settings.policy.ruleset_version}")
    print(f"documents             {result.total_documents}")
    print(f"exact matches         {result.exact_count:>6}  ({result.exact_rate:.2%})")
    print(f"fuzzy matches         {result.fuzzy_count:>6}  ({result.fuzzy_rate:.2%})")
    print(f"exceptions            {len(result.exceptions):>6}")
    print(f"days to IMS cut-off   {days_to_cutoff(BASE_DATE, settings.policy)}")
    print()
    print(f"{'exception class':24} {'count':>6}")
    for cls, count in sorted(result.by_class().items(), key=lambda row: -row[1]):
        print(f"{cls.value:24} {count:>6}")

    claimed = sum(line.tax.total_tax for line in dataset.books)
    available = sum(line.tax.total_tax for line in dataset.portal)
    exposure = assess_drc01c(claimed, available, settings.policy)
    print()
    print(f"claimed ITC (books)   {exposure.claimed_itc}")
    print(f"available ITC (2B)    {exposure.available_itc}")
    print(f"excess                {exposure.excess}")
    print(f"DRC-01C threshold     {exposure.threshold}")
    print(f"intimation projected  {exposure.fires}")
    return 0


def command_investigate(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.mode == "live":
        loaded = load_provider_credentials()
        if not loaded:
            print("no provider credentials found in .env; refusing to run live", file=sys.stderr)
            return 2
        print(f"credentials loaded: {', '.join(loaded)}")

    dataset = _cycle(args.seed, 120, HARD_MIX)
    result = reconcile(
        dataset.books,
        dataset.portal,
        tolerances=settings.tolerances,
        policy=settings.policy,
        as_of=BASE_DATE,
    )
    router = build_router(settings.cache_dir, mode=args.mode)
    surface = ToolSurface(dataset, settings.tolerances)
    budgets = AgentBudgets(max_steps=args.max_steps)

    exceptions = result.exceptions[: args.limit]
    print(f"mode {args.mode}  shards {[shard.model for shard in router.shards]}")
    print()
    header = f"{'exception':22} {'class':22} {'steps':>5} {'conf':>5} {'action':>8} {'human':>6}"
    print(header)
    print("-" * len(header))

    resolved = 0
    for exception in exceptions:
        trajectory = investigate(exception, surface, router, budgets)
        decision = decide(exception, trajectory.finding, settings.policy)
        resolved += trajectory.resolved
        confidence = f"{trajectory.finding.confidence:.2f}" if trajectory.finding else "   -"
        print(
            f"{exception.exception_id[:21]:22} {exception.exception_class.value:22} "
            f"{len(trajectory.steps):>5} {confidence:>5} {decision.action.value:>8} "
            f"{decision.requires_human!s:>6}"
        )
        if args.verbose:
            print(f"    probes    {' -> '.join(trajectory.tool_sequence) or '(none)'}")
            print(f"    outcome   {trajectory.terminated_because}")
            if trajectory.finding:
                print(f"    rationale {trajectory.finding.rationale}")
                for item in trajectory.finding.evidence:
                    print(f"    evidence  [{item.tool_call_id}] {item.claim}")

    print("-" * len(header))
    print(f"resolved {resolved}/{len(exceptions)}")
    print(json.dumps(router.stats(), indent=2))
    return 0


def command_experiment(args: argparse.Namespace) -> int:
    from gst_recon.experiments.harness import Experiments  # noqa: PLC0415 -- heavy import

    settings = load_settings()
    if args.mode == "live":
        loaded = load_provider_credentials()
        if not loaded:
            print("no provider credentials found in .env; refusing to run live", file=sys.stderr)
            return 2

    experiments = Experiments(settings, mode=args.mode, results_dir=settings.results_dir)
    suffix = "" if args.mode == "fake" else "_live"

    if args.which in ("ablation", "all"):
        rows = [arm.as_row() for arm in experiments.tiering_ablation()]
        path = experiments.write_csv(f"tiering_ablation{suffix}", rows)
        print(f"wrote {path}")
        for row in rows:
            print(
                f"  {row['arm']:20} resolved {row['resolved']:>4}/{row['exceptions']:<4} "
                f"[{row['ci_low']:.1%}, {row['ci_high']:.1%}]  requests {row['llm_requests']:>5}  "
                f"per resolution {row['requests_per_resolution']}"
            )

    if args.which in ("budget", "all"):
        rows = experiments.step_budget_curve()
        print(f"wrote {experiments.write_csv(f'step_budget_curve{suffix}', rows)}")
        for row in rows:
            print(
                f"  budget {row['budget']:>2}  resolved {row['successes']:>3}/{row['trials']:<3} "
                f"[{row['ci_low']:.3f}, {row['ci_high']:.3f}]  probes {row['probes_spent']}"
            )

    if args.which in ("memory", "all"):
        rows = experiments.memory_curve(cycles=args.cycles)
        print(f"wrote {experiments.write_csv(f'memory_curve{suffix}', rows)}")
        for row in rows:
            print(
                f"  cycle {row['cycle']}  precedents {row['precedents_available']:>4}  "
                f"hit rate {row['precedent_hit_rate']}  mean probes {row['mean_probes']}"
            )

    if args.which in ("quality", "all") and args.mode == "live":
        scores = experiments.trajectory_scores(limit=args.cases)
        print(f"wrote {experiments.write_json('trajectory_scores_live', scores)}")
        print(json.dumps(scores, indent=2))
        rows = experiments.stp_curve(limit=args.cases)
        print(f"wrote {experiments.write_csv('stp_curve_live', rows)}")

    if args.which in ("consistency", "all"):
        payload = experiments.consistency(cases=args.cases, repeats=args.repeats)
        print(f"wrote {experiments.write_json(f'consistency{suffix}', payload)}")
        print(json.dumps({k: v for k, v in payload.items() if k != "per_case"}, indent=2))

    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(prog="gst-recon", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    recon = sub.add_parser("reconcile", help="run Tier 1 over a generated cycle")
    recon.add_argument("--seed", type=int, default=20260726)
    recon.add_argument("--clean-pairs", type=int, default=1800)
    recon.add_argument("--near-miss", type=int, default=120)
    recon.set_defaults(func=command_reconcile)

    agent = sub.add_parser("investigate", help="route hard exceptions through Tier 2")
    agent.add_argument("--mode", choices=("fake", "live"), default="fake")
    agent.add_argument("--seed", type=int, default=4242)
    agent.add_argument("--limit", type=int, default=6)
    agent.add_argument("--max-steps", type=int, default=6)
    agent.add_argument("--verbose", action="store_true")
    agent.set_defaults(func=command_investigate)

    exp = sub.add_parser("experiment", help="run an experiment and write results/")
    exp.add_argument(
        "which",
        choices=("ablation", "budget", "memory", "consistency", "all"),
        default="all",
        nargs="?",
    )
    exp.add_argument("--mode", choices=("fake", "live"), default="fake")
    exp.add_argument("--cycles", type=int, default=3)
    exp.add_argument("--cases", type=int, default=8)
    exp.add_argument("--repeats", type=int, default=3)
    exp.set_defaults(func=command_experiment)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
