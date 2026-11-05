"""Strategy-agnostic validation harness — the falsification machinery, extracted.

WHAT THIS IS FOR
----------------
Deciding, honestly, whether a set of resolved bets is evidence of an edge. Nothing here
knows about earnings, Polymarket, or any particular signal: it takes realised payoffs and
returns a verdict you can defend.

It exists because this project produced a confident, wrong answer. On 2026-10-30 the
forward test reported "EDGE CONFIRMED — +0.394/$1 over 351 bets, 95% CI [0.045, 0.744]".
Every number was computed correctly and the conclusion was false. Four separate defects
each would have been enough on their own:

  1. NOT INDEPENDENT — the same earnings print was logged under several outcome rows, so
     one lucky 32:1 winner was counted twice. That inflated the mean AND faked the sample
     size, which is what made the confidence interval look tight.
  2. NOT FILLABLE — the payoffs assumed $1 fills at the mid price in books holding $9.
     A bet you could not have placed is not evidence.
  3. NOT ROBUST — the top 5 bets carried 89% of the total P&L. Remove five rows and the
     edge vanished. A mean that fragile was never a mean.
  4. NOT POWERED — the bar was "30 bets with CI-low > 0". At the payoff variance involved,
     30 bets cannot distinguish a real 2.6% edge from zero, so the test could only ever
     have been passed by luck.

Each gate below is one of those failures, turned into a rule. The ordering matters: a
result must clear ALL of them, and the cheapest checks are also the ones most often
skipped.

GATES
-----
  dedup_by_group     independence — one payoff per underlying event
  executable         fillability  — was there real size at a real price
  fill_price         price realism — pay the touch, never the mid
  robust_stats       bootstrap CI + tail concentration + required sample size
  verdict            the ruling, including UNDERPOWERED and NOT ROBUST states
  Registry           pre-registration — a rule must predate the data that scores it,
                     plus a trial counter for multiple-comparisons correction

WHY BOOTSTRAP, NOT A t-TEST
---------------------------
Binary-contract payoffs are bimodal and heavy-tailed: you lose exactly -1 or win
(1-cost)/cost. A normal approximation understates the uncertainty badly at low contract
prices, which is precisely where the fluke lived. The bootstrap makes no shape assumption.

WHAT THIS CANNOT DO
-------------------
It cannot tell you a strategy works. It can only tell you that a given body of evidence
fails to show that it doesn't — and, far more often, that the evidence is too weak,
too correlated, or too lucky to say anything at all. Used honestly it will mostly return
bad news. That is the point.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Hashable, Iterable, List, Optional, Sequence

import json
import numpy as np

# ---------------------------------------------------------------------------
# Defaults. These are conventions, not laws — pass your own where they don't fit.
# ---------------------------------------------------------------------------
HAIRCUT = 0.02            # fee + slippage charged against every winning payoff
MIN_PRICE = 0.05          # outside [MIN_PRICE, 1-MIN_PRICE] a quote is a phantom, not a price
TAIL_K = 5                # how many top winners the robustness check removes
FAVORITE_COST = 0.50      # a contract costing at least this is a "favorite" purchase
LONGSHOT_COST = 0.10      # below this, empirically, buyers lose >60% (Bürgi-Deng-Whelan 2026)
MIN_BOOK_USD = 25.0       # less size than this and the bet was not placeable
DEFAULT_EFFECT = 0.026    # the effect size to power against, absent a better estimate


# ---------------------------------------------------------------------------
# Gate 3 — price realism
# ---------------------------------------------------------------------------
def contract_cost(side: str, q: float) -> float:
    """Price of the contract actually BOUGHT — what is at risk per unit.

    Buying YES costs q; buying NO costs 1-q. This distinction is not pedantry. A NO bet
    on a market trading at 0.80 costs 0.20: it is a LONGSHOT PURCHASE, not a contrarian
    view on a favorite. Mis-reading that is how 61% of this project's bets turned out to
    be longshot buys while being described as fades.
    """
    return q if side == "YES" else (1.0 - q)


def fill_price(side: str, q: float, bid: Optional[float] = None,
               ask: Optional[float] = None) -> float:
    """The price you would actually pay, not the mid.

    Buying YES lifts the ask; buying NO pays 1-bid (you sell YES at the bid). Scoring at
    the mid hands the strategy half the spread as free alpha on every single bet — a
    silent, systematic overstatement. On an 8-cent book that gift is larger than most
    edges being tested. Falls back to the mid only when no book was recorded.
    """
    if side == "YES":
        return ask if (ask is not None and 0 < ask < 1) else q
    return (1 - bid) if (bid is not None and 0 < bid < 1) else (1 - q)


def payoff(cost: float, won: bool, haircut: float = HAIRCUT) -> float:
    """Realised P&L per $1 staked on a contract bought at `cost`."""
    if not (0 < cost < 1):
        return float("nan")
    return ((1 - cost) / cost - haircut) if won else -1.0


def pnl_per_1(side: str, q: float, outcome: str, haircut: float = HAIRCUT) -> float:
    """Convenience: payoff for taking `side` at market price q, given the outcome."""
    return payoff(contract_cost(side, q), side == outcome, haircut)


# ---------------------------------------------------------------------------
# Gate 2 — fillability
# ---------------------------------------------------------------------------
def executable(max_usd: Any = None, depth_usd: Any = None,
               min_usd: float = MIN_BOOK_USD) -> Optional[bool]:
    """Could this bet have been filled at all?

    Returns None for UNKNOWN — a record with no book snapshot. Unknown is deliberately
    neither True nor False: it must not be counted as evidence, and it must not be
    counted as a failure either. Most historical records are unknown, and pretending
    otherwise in either direction is how fiction enters the ledger.
    """
    def _n(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    a, b = _n(max_usd), _n(depth_usd)
    if a is None and b is None:
        return None
    return (a or 0.0) >= min_usd or (b or 0.0) >= min_usd


# ---------------------------------------------------------------------------
# Gate 1 — independence
# ---------------------------------------------------------------------------
def dedup_by_group(items: Iterable[Dict[str, Any]],
                   key: Callable[[Dict[str, Any]], Hashable],
                   prefer: Optional[Callable[[Dict[str, Any]], float]] = None
                   ) -> List[Dict[str, Any]]:
    """One record per underlying event.

    Several bets on the same underlying event are the same wager wearing different
    labels. Counting them separately inflates the mean and — worse — fakes the sample
    size, which is what tightens a confidence interval around a fluke. `prefer` ranks
    which record to keep when several share a key (lower is kept).
    """
    best: Dict[Hashable, Dict[str, Any]] = {}
    for it in items:
        k = key(it)
        cur = best.get(k)
        if cur is None or (prefer is not None and prefer(it) < prefer(cur)):
            best[k] = it
    return list(best.values())


# ---------------------------------------------------------------------------
# Gate 4 — power
# ---------------------------------------------------------------------------
def required_n(sd: float, effect: float = DEFAULT_EFFECT, z: float = 1.96) -> Optional[int]:
    """Bets needed before a 95% CI could exclude zero, given payoff SD and true effect.

    This is the number a success criterion should be built from. Stating "30 bets and a
    CI above zero" without it is decoration: if the sample cannot resolve a realistic
    effect, then a CI that excludes zero is more likely a tail fluke than a discovery.
    Variance is the whole story — at SD 0.65 you need ~2,400 bets for a 2.6% edge; at
    SD 3.74 you need ~79,500, which is to say you will never know.
    """
    if not sd or not effect or effect <= 0 or sd <= 0:
        return None
    return int(np.ceil((z * sd / effect) ** 2))


# ---------------------------------------------------------------------------
# Gate 3 — tail robustness, and the statistics
# ---------------------------------------------------------------------------
def robust_stats(pnls: Sequence[float], seed: int = 0, tail_k: int = TAIL_K,
                 effect: float = DEFAULT_EFFECT, draws: int = 20000) -> Dict[str, Any]:
    """Mean with a BOOTSTRAP CI, tail concentration, and the sample size required.

    Reports `mean_ex_topK` and `topK_share` because a mean carried entirely by its best
    few wins is not an edge, it is a lottery ticket that happened to hit. In the case
    that prompted this module, the top 5 of 382 bets held 89% of all P&L.
    """
    n = len(pnls)
    if n == 0:
        return {"n": 0}
    a = np.asarray(pnls, dtype=float)
    out: Dict[str, Any] = {
        "n": n,
        "mean": round(float(a.mean()), 3),
        "median": round(float(np.median(a)), 3),
    }
    if n > 1:
        rng = np.random.default_rng(seed)
        boot = rng.choice(a, size=(draws, n), replace=True).mean(axis=1)
        out["ci95"] = [round(float(np.percentile(boot, 2.5)), 3),
                       round(float(np.percentile(boot, 97.5)), 3)]
        out["p_mean_le_0"] = round(float(np.mean(boot <= 0)), 3)
        sd = float(a.std(ddof=1))
        out["sd"] = round(sd, 3)
        out["n_needed"] = required_n(sd, effect)
        out["effect_assumed"] = effect
        if out["mean"] > 0:
            out["n_needed_at_observed"] = required_n(sd, out["mean"])
        out["powered"] = bool(out["n_needed"] and n >= out["n_needed"])
    if n > tail_k:
        top = np.sort(a)[::-1][:tail_k]
        out[f"mean_ex_top{tail_k}"] = round(float((a.sum() - top.sum()) / (n - tail_k)), 3)
        total = float(a.sum())
        out[f"top{tail_k}_share"] = round(float(top.sum() / total), 3) if total else None
    return out


def verdict(stats: Dict[str, Any], min_n: int = 30, tail_k: int = TAIL_K) -> str:
    """The ruling. Deliberately hard to pass and easy to read.

    Order matters: power is checked BEFORE significance, because a significant result on
    an underpowered sample is weaker evidence than no result at all — it is the signature
    of a fluke, not of an edge.
    """
    n = stats.get("n", 0)
    need = stats.get("n_needed")
    if n < min_n:
        extra = f" (and a powered test needs ~{need:,})" if need else ""
        return f"INSUFFICIENT DATA — {n}/{min_n} resolved bets{extra}. No claim yet."
    ci = stats.get("ci95") or [None, None]
    ex = stats.get(f"mean_ex_top{tail_k}")
    if need and n < need and not (ci[1] is not None and ci[1] < 0):
        return (f"UNDERPOWERED — {n} bets, but SD {stats.get('sd')} means ~{need:,} are "
                f"needed to resolve a {stats.get('effect_assumed', DEFAULT_EFFECT):.1%} "
                f"effect. Any 'significant' result at this n is more likely a tail fluke. "
                f"Narrow to lower-variance bets or keep collecting.")
    if ci[0] is not None and ci[0] > 0 and (ex is None or ex > 0):
        return ("EDGE CONFIRMED — positive, significant (bootstrap 95% CI > 0), powered, "
                "and robust to its biggest wins.")
    if ci[0] is not None and ci[0] > 0:
        return (f"NOT ROBUST — CI > 0, but the mean collapses to {ex:+.3f} once the top "
                f"{tail_k} wins are removed. Tail-driven, not an edge.")
    if ci[1] is not None and ci[1] < 0:
        return f"DISPROVEN — significantly negative over {n} bets."
    return f"NO PROVEN EDGE — mean not significantly > 0 over {n} bets."


# ---------------------------------------------------------------------------
# Gate 5 — pre-registration and the trial counter
# ---------------------------------------------------------------------------
class Registry:
    """Pre-registration store: a rule must predate the data that scores it.

    This is the defense against manufacturing an edge by searching. Slice a few hundred
    resolved bets twenty ways and keep the best slice and you will always find something
    that looks significant — at the 5% level you expect about one false positive per
    twenty tests. That is not a hypothetical: a 20-subgroup sweep of this project's vault
    returned four "profitable" slices, none of which meant anything.

    So: a rule is scored only if it was registered BEFORE the bets, only on bets logged
    after its registration date, and only against the success criterion written at
    registration. Finding a better criterion later voids the test.

    `log_trial` keeps the running count of searches. The Deflated Sharpe Ratio and PBO
    both need the number of trials to judge whether the best result clears the expected
    maximum under zero skill; recording that a search happened without recording how many
    is exactly the missing input.
    """

    def __init__(self, path):
        self.path = path
        try:
            self.data = json.loads(open(path).read())
        except Exception:
            self.data = {}
        self.data.setdefault("pre_registered", [])
        self.data.setdefault("trial_log", {"trials": [], "running_total_subgroup_trials": 0})

    def save(self) -> None:
        with open(self.path, "w") as fh:
            json.dump(self.data, fh, indent=2)

    def get(self, hyp_id: str) -> Optional[Dict[str, Any]]:
        return next((h for h in self.data["pre_registered"] if h.get("id") == hyp_id), None)

    def is_registered(self, hyp_id: str) -> bool:
        return self.get(hyp_id) is not None

    def scoring_starts_at(self, hyp_id: str) -> str:
        h = self.get(hyp_id) or {}
        return h.get("scoring_starts_at") or h.get("registered_at") or "9999-99-99"

    def register(self, hyp: Dict[str, Any]) -> None:
        """Add a hypothesis. Requires the fields that make it falsifiable."""
        for field in ("id", "claim", "success_criterion", "kill_criterion",
                      "scoring_starts_at"):
            if not hyp.get(field):
                raise ValueError(f"hypothesis needs '{field}' — an unfalsifiable rule is not "
                                 f"a hypothesis")
        if self.is_registered(hyp["id"]):
            raise ValueError(f"'{hyp['id']}' already registered — amending a live hypothesis "
                             f"voids its test; register a new id instead")
        self.data["pre_registered"].append(hyp)
        self.save()

    def log_trial(self, date: str, n_trials: int, what: str, outcome: str = "") -> int:
        """Record a search. Returns the new running total."""
        tl = self.data["trial_log"]
        tl.setdefault("trials", []).append(
            {"date": date, "n_trials": n_trials, "what": what, "outcome": outcome})
        prev = tl.get("running_total_subgroup_trials") or 0
        tl["running_total_subgroup_trials"] = prev + int(n_trials)
        self.save()
        return tl["running_total_subgroup_trials"]

    @property
    def trials(self) -> int:
        return self.data.get("trial_log", {}).get("running_total_subgroup_trials", 0) or 0

    def deflation_note(self) -> str:
        """Plain-language warning scaled to how much searching has happened."""
        t = self.trials
        if t <= 1:
            return ""
        return (f"{t} search trials recorded. With {t} trials, roughly {t * 0.05:.1f} "
                f"subgroup(s) are expected to look 'significant' at the 5% level by chance "
                f"alone. Any result not pre-registered must be discounted accordingly "
                f"(see Deflated Sharpe Ratio, Bailey & López de Prado).")


# ---------------------------------------------------------------------------
# One-call convenience
# ---------------------------------------------------------------------------
def assess(bets: Sequence[Dict[str, Any]], *, key: Optional[Callable] = None,
           prefer: Optional[Callable] = None, pnl_field: str = "pnl",
           require_executable: bool = True, seed: int = 0,
           effect: float = DEFAULT_EFFECT, min_n: int = 30) -> Dict[str, Any]:
    """Run every gate over a list of bet dicts and return stats + verdict.

    Each bet needs `pnl_field`; optionally `executable` (True/False/None) and whatever
    `key` reads for deduplication. Returns what was excluded and why, because a verdict
    whose exclusions are invisible is not auditable.
    """
    rows = list(bets)
    n_in = len(rows)
    dropped: Dict[str, int] = {}
    if require_executable:
        before = len(rows)
        rows = [b for b in rows if b.get("executable") is True]
        dropped["not_executable_or_unknown"] = before - len(rows)
    if key is not None:
        before = len(rows)
        rows = dedup_by_group(rows, key=key, prefer=prefer)
        dropped["duplicate_events"] = before - len(rows)
    pnls = [b[pnl_field] for b in rows if b.get(pnl_field) is not None]
    st = robust_stats(pnls, seed=seed, effect=effect)
    return {"n_input": n_in, "n_scored": len(pnls), "excluded": dropped,
            "stats": st, "verdict": verdict(st, min_n=min_n)}
