"""Forward-test vault: every prediction is logged to output/edge_log.csv.

This is the data vault that keeps the forward test "in play". Each prediction
(with or without a Polymarket market) is recorded the moment it's made; after the
earnings date passes, forward_test.py resolves the EPS outcome and scores model
vs market. Re-running the same ticker/outcome/earnings-date updates the existing
unresolved row in place (no duplicates).
"""
from __future__ import annotations

import csv
from datetime import datetime
from typing import Any, Dict, List

from tools.config import EDGE_LOG_PATH

FIELDS = [
    "logged_at", "ticker", "outcome", "threshold", "earnings_date",
    "model_prob", "market_prob", "net_edge", "side", "confidence",
    "scar_pred", "smg_psi", "smg_stake", "smg_live", "pump_score", "pump_dir",
    "edge_tilt", "market_aware_prob", "lambda_scar",
    "contract_strike", "contract_prob", "strike_gap",
    # critique #2 — immutable earnings-event ID + POINT-IN-TIME consensus snapshot: what consensus
    # (source/fiscal period) the prediction was made against, so resolution scores the frozen
    # target, never a re-fetched/revised one.
    "earnings_event_id", "consensus_eps_used", "consensus_source", "fiscal_year", "fiscal_quarter",
    # EXECUTION CONTEXT (added 2026-11-03). Without these the vault cannot tell a bet that
    # could actually be filled from one that could not, so its P&L is fiction: the
    # 2026-10-30 "EDGE CONFIRMED" rested on $1 "fills" at q=0.030 in books with no size.
    # Frozen at log time (the book moves), so resolution scores the price we could really
    # have traded at, and the pre-registered strike_gap_v1 entry gate becomes verifiable.
    "yes_bid", "yes_ask", "book_spread", "depth_usd", "max_usd", "executable_edge",
    "expected_profit_usd",
    # Which EPS measure the CONTRACT resolves on. 'gaap' means our (adjusted) model is
    # answering a different question, so those rows must not be scored as model-vs-market.
    "eps_basis",
    "recommendation", "resolved", "actual_outcome", "notes",
]


def _exec_ctx(market: Dict[str, Any], edge: Dict[str, Any]) -> Dict[str, Any]:
    """Freeze the order-book state at the moment the bet is logged."""
    return {
        "yes_bid": market.get("best_bid"),
        "yes_ask": market.get("best_ask"),
        "book_spread": market.get("book_spread") if market.get("book_spread") is not None
                       else market.get("spread"),
        "depth_usd": market.get("depth_usd"),
        "max_usd": edge.get("max_tradeable_usd"),
        "executable_edge": edge.get("executable_edge"),
        # expected $ profit at the size the book can actually absorb — the statistic
        # selection now ranks on, so the vault records what it was ranked on.
        "expected_profit_usd": edge.get("expected_profit_usd"),
    }


def _basis(result: Dict[str, Any]) -> Dict[str, Any]:
    return {"eps_basis": result.get("contract_basis")}


def log_strike_edge(result: Dict[str, Any], notes: str = "strikegap-fwd") -> bool:
    """Log a STRIKE-GAP forward bet (pre-registered hypothesis strike_gap_v1) as a DISTINCT row
    (outcome 'eps_strike', so it never collides with the consensus-beat log). model_prob = the
    recalibrated contract_prob; side/net = the structural strike edge; threshold = the frozen
    strike, which drives strike-based resolution in forward_test. Returns True if written."""
    se = result.get("strike_edge") or {}
    if not se or se.get("side") not in ("YES", "NO") or result.get("contract_strike") is None:
        return False
    market = result.get("market") or {}
    row = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "ticker": result.get("ticker"), "outcome": "eps_strike",
        "threshold": result.get("contract_strike"),
        "earnings_date": result.get("earnings_date"),
        "model_prob": result.get("contract_prob"),
        "market_prob": market.get("yes_price"),
        "net_edge": se.get("net_edge"), "side": se.get("side"),
        "confidence": result.get("confidence"),
        "scar_pred": result.get("scar_predictability"),
        "contract_strike": result.get("contract_strike"),
        "contract_prob": result.get("contract_prob"),
        "strike_gap": result.get("strike_gap"),
        "earnings_event_id": result.get("earnings_event_id"),
        "consensus_eps_used": result.get("consensus_eps"),
        "consensus_source": result.get("consensus_source"),
        "fiscal_year": result.get("fiscal_year"), "fiscal_quarter": result.get("fiscal_quarter"),
        **_exec_ctx(market, se), **_basis(result),
        "recommendation": se.get("recommendation"), "resolved": "", "actual_outcome": "",
        "notes": (notes + (" | candidate" if se.get("structural_candidate") else " | watch")),
    }
    rows = _read()
    for i, r in enumerate(rows):
        if _key(r) == _key(row):
            if not r.get("resolved"):
                rows[i] = row
                _write(rows)
                return True
            return False
    rows.append(row)
    _write(rows)
    return True


def _read() -> List[Dict[str, Any]]:
    if not EDGE_LOG_PATH.exists():
        return []
    with open(EDGE_LOG_PATH, newline="") as fh:
        return list(csv.DictReader(fh))


def _write(rows: List[Dict[str, Any]]) -> None:
    with open(EDGE_LOG_PATH, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def _key(r: Dict[str, Any]):
    # normalize None -> "" so an in-memory dateless row (None) matches its CSV round-trip ("")
    return (r.get("ticker"), r.get("outcome"), str(r.get("earnings_date") or ""))


def log_edge(result: Dict[str, Any], notes: str = "") -> bool:
    """Record a prediction into the vault. Logs even without a market (market_prob
    blank) so EPS outcomes can still be forward-scored. Updates an existing
    unresolved row for the same ticker/outcome/earnings-date instead of duplicating.
    Returns True if written."""
    if result.get("model_prob") is None:
        return False
    market = result.get("market") or {}
    edge = result.get("edge") or {}
    row = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "ticker": result.get("ticker"),
        "outcome": result.get("outcome"),
        "threshold": result.get("threshold"),
        "earnings_date": result.get("earnings_date"),
        "model_prob": result.get("model_prob"),
        "market_prob": market.get("yes_price"),
        "net_edge": edge.get("net_edge"),
        "side": edge.get("side"),
        "confidence": result.get("confidence"),
        "scar_pred": result.get("scar_predictability"),
        "smg_psi": (result.get("smg") or {}).get("psi"),
        "smg_stake": (result.get("smg") or {}).get("stake_fraction"),
        "smg_live": (result.get("smg") or {}).get("live"),
        "pump_score": (result.get("pump") or {}).get("pump"),
        "pump_dir": (result.get("pump") or {}).get("direction"),
        "edge_tilt": result.get("edge_tilt"),
        "market_aware_prob": result.get("market_aware_prob"),
        "lambda_scar": result.get("lambda_scar"),
        "contract_strike": result.get("contract_strike"),
        "contract_prob": result.get("contract_prob"),
        "strike_gap": result.get("strike_gap"),
        "earnings_event_id": result.get("earnings_event_id"),
        "consensus_eps_used": result.get("consensus_eps"),
        "consensus_source": result.get("consensus_source"),
        "fiscal_year": result.get("fiscal_year"),
        "fiscal_quarter": result.get("fiscal_quarter"),
        **_exec_ctx(market, edge), **_basis(result),
        "recommendation": edge.get("recommendation"),
        "resolved": "",
        "actual_outcome": "",
        "notes": notes,
    }
    rows = _read()
    for i, r in enumerate(rows):
        if _key(r) == _key(row):
            if not r.get("resolved"):
                rows[i] = row      # refresh the unresolved entry in place
                _write(rows)
                return True
            return False           # event already resolved -> never append a duplicate
    rows.append(row)
    _write(rows)
    return True
