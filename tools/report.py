"""Render prediction results as a readable table and save CSV + markdown.

One column per tested outcome (the spec's layout). Probabilities shown as
percentages; the model-vs-market gap and the recommendation make the edge
obvious without claiming certainty.
"""
from __future__ import annotations

import csv
from datetime import datetime
from typing import Any, Dict, List, Optional

from tabulate import tabulate

from tools.config import OUTPUT_DIR


def _pct(x: Optional[float]) -> str:
    return f"{x:.0%}" if isinstance(x, (int, float)) else "n/a"


def _num(x: Optional[float], fmt: str = ",.2f") -> str:
    return format(x, fmt) if isinstance(x, (int, float)) else "n/a"


# (label, extractor) — order defines the displayed rows.
ROWS = [
    ("Company / Ticker", lambda r: f"{r.get('company','?')} ({r.get('ticker','?')})"),
    ("Earnings date", lambda r: f"{r.get('earnings_date','?')} {r.get('earnings_hour','') or ''}".strip()),
    ("Outcome tested", lambda r: r.get("outcome", "?")),
    ("User threshold", lambda r: _num(r.get("threshold")) if r.get("threshold") is not None else "default"),
    ("Consensus EPS", lambda r: _num(r.get("consensus_eps"))),
    ("Consensus revenue", lambda r: _num(r.get("consensus_revenue"), ",.0f")),
    ("Predicted EPS surprise", lambda r: f"{_num(r.get('predicted_eps_surprise_pct'))}%"),
    ("CWSF surprise ± band", lambda r: r.get("cwsf_surprise_band") or "n/a"),
    ("SCAR predictability", lambda r: (f"{r['scar_predictability']}/100"
                                       + (" ⚠ " + "; ".join(r.get("scar_reasons", [])) if r.get("scar_flag") else " (trust the read)")
                                       if r.get("scar_predictability") is not None else "n/a")),
    ("Predicted rev surprise", lambda r: f"{_num(r.get('predicted_revenue_surprise_pct'))}%"),
    ("Q1 fundamentals-only prob", lambda r: _pct(r.get("fundamental_only_prob"))),
    ("Q1 + price adj (capped)", lambda r: (_pct(r.get("fundamental_beat_prob"))
        + (f"  ({r['market_adjustment_pp']:+.1f}pp price"
           + (f", capped from {r['market_adjustment_raw_pp']:+.1f}" if r.get("market_capped") else "")
           + ")" if r.get("market_adjustment_pp") is not None else ""))),
    ("Q2 priced-in index (0-1)", lambda r: _num(r.get("priced_in_index"), ".2f")),
    ("Kelly ExtRet (over-extrap.)", lambda r: f"{_num(r.get('kelly_ext_ret') and r['kelly_ext_ret']*100, '+.1f')}%"
        if r.get("kelly_ext_ret") is not None else "n/a"),
    ("Model probability", lambda r: _pct(r.get("model_prob"))),
    ("Confidence interval", lambda r: (f"{r['model_prob_ci'][0]:.0%}–{r['model_prob_ci'][1]:.0%}"
                                       if r.get("model_prob_ci") else "n/a")),
    ("Polymarket implied", lambda r: _pct((r.get("market") or {}).get("yes_price"))),
    ("Order book bid/ask", lambda r: (
        f"{(r.get('market') or {}).get('best_bid'):.2f} / {(r.get('market') or {}).get('best_ask'):.2f}"
        if (r.get("market") or {}).get("best_bid") is not None
        and (r.get("market") or {}).get("best_ask") is not None else "n/a")),
    ("Market-aware prob", lambda r: _pct(r.get("market_aware_prob"))),
    ("Edge vs market", lambda r: (f"{r['edge_vs_market']:+.0%}" if r.get("edge_vs_market") is not None else "n/a")),
    ("Edge drivers", lambda r: "\n".join(f"{d['signal']} ({d['contribution']:+.2f})"
                                          for d in r.get("edge_drivers", [])) or "none (agrees with market)"),
    ("Q3 net edge vs market", lambda r: _pct((r.get("edge") or {}).get("net_edge"))),
    ("Confidence", lambda r: r.get("confidence", "n/a")),
    ("Recommendation", lambda r: (r.get("edge") or {}).get("recommendation", "n/a")),
    ("Main evidence", lambda r: "\n".join(r.get("evidence", [])) or "n/a"),
    ("Risks / missing data", lambda r: "\n".join(r.get("risks", [])) or "none"),
]


def render(results: List[Dict[str, Any]]) -> str:
    """Build the table string (one column per result) and print it."""
    results = [r for r in results if "error" not in r] or results
    headers = ["Field"] + [r.get("ticker", "?") for r in results]
    table = [[label] + [extract(r) for r in results] for label, extract in ROWS]
    out = tabulate(table, headers=headers, tablefmt="fancy_grid")
    print(out)
    for r in results:
        if "error" in r:
            print(f"\n[error] {r['error']}")
    print("\nNot financial advice. Probabilities are model estimates, not guarantees.")
    return out


def save(results: List[Dict[str, Any]], basename: str = "prediction") -> Dict[str, str]:
    """Write a flat CSV and a markdown copy of the table to output/."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUTPUT_DIR / f"{basename}_{stamp}.csv"
    md_path = OUTPUT_DIR / f"{basename}_{stamp}.md"

    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["field"] + [r.get("ticker", "?") for r in results])
        for label, extract in ROWS:
            w.writerow([label] + [extract(r).replace("\n", "; ") for r in results])

    headers = ["Field"] + [r.get("ticker", "?") for r in results]
    table = [[label] + [extract(r).replace("\n", "<br>") for r in results]
             for label, extract in ROWS]
    with open(md_path, "w") as fh:
        fh.write(f"# Polymarket Earnings Edge — {stamp}\n\n")
        fh.write(tabulate(table, headers=headers, tablefmt="github"))
        fh.write("\n\n_Not financial advice. Model estimates, not guarantees._\n")

    return {"csv": str(csv_path), "md": str(md_path)}
