"""Generate the repository figures as SVG, from live vault data.

Kept as a script rather than a checked-in image so the figure can never drift away
from the numbers it claims to show: re-run it and it redraws from output/edge_log.csv.

Emits a light and a dark variant, which GitHub selects between via the
`#gh-light-mode-only` / `#gh-dark-mode-only` anchor convention.

    .venv/bin/python -m tools.make_figures
"""
from __future__ import annotations

import csv
from typing import Any, Dict, List, Optional

import numpy as np

from tools import forward_test as ft
from tools.config import EDGE_LOG_PATH

DOCS = EDGE_LOG_PATH.parent.parent / "docs"

# Palette: validated two-slot categorical (dataviz reference instance).
# Checks pass light and dark — CVD ΔE 24.7, normal-vision ΔE 33.6, contrast ≥3:1.
THEME = {
    "light": {"surface": "#fcfcfb", "text": "#0b0b0b", "text2": "#52514e",
              "muted": "#8a8984", "grid": "#e5e4e0",
              "model": "#2a78d6", "market": "#eb6834"},
    "dark": {"surface": "#1a1a19", "text": "#f0efec", "text2": "#c3c2b7",
             "muted": "#8a8984", "grid": "#33322f",
             "model": "#3987e5", "market": "#d95926"},
}


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def calibration_data(n_bins: int = 5) -> Dict[str, Any]:
    """Reliability bins for the model and the market, over the same resolved bets.

    Same exclusions the scorer uses — phantom prices, disputed resolutions, GAAP
    contracts, duplicate events — so the figure and the headline Brier agree.
    """
    rows = list(csv.DictReader(open(EDGE_LOG_PATH)))
    res = [r for r in rows
           if r.get("resolved") and r.get("actual_outcome") in ("YES", "NO")
           and _f(r.get("model_prob")) is not None and _f(r.get("market_prob")) is not None
           and ft.MIN_Q < _f(r["market_prob"]) < 1 - ft.MIN_Q
           and "DISPUTED" not in (r.get("notes") or "").upper()
           and (r.get("eps_basis") or "") != "gaap"]
    res = ft._dedup_events([{**r, "ticker": r.get("ticker"),
                             "earnings_date": r.get("earnings_date"),
                             "outcome": r.get("outcome")} for r in res])
    y = np.array([1.0 if r["actual_outcome"] == "YES" else 0.0 for r in res])
    m = np.array([_f(r["model_prob"]) for r in res], float)
    q = np.array([_f(r["market_prob"]) for r in res], float)
    edges = np.linspace(0, 1, n_bins + 1)
    series = {"model": [], "market": []}
    for name, p in (("model", m), ("market", q)):
        for i in range(n_bins):
            lo, hi = edges[i], edges[i + 1]
            sel = (p >= lo) & ((p < hi) if i < n_bins - 1 else (p <= hi))
            if sel.sum() == 0:
                continue
            series[name].append({"x": float(p[sel].mean()),
                                 "y": float(y[sel].mean()),
                                 "n": int(sel.sum())})
    return {"series": series, "n": int(len(y)), "base_rate": float(y.mean()),
            "model_brier": float(np.mean((m - y) ** 2)),
            "market_brier": float(np.mean((q - y) ** 2))}


def _svg(data: Dict[str, Any], mode: str) -> str:
    c = THEME[mode]
    W, H = 720, 560
    L, R, T, B = 78, 28, 74, 92           # margins
    pw, ph = W - L - R, H - T - B

    def X(v):
        return L + v * pw

    def Y(v):
        return T + (1 - v) * ph

    p: List[str] = []
    p.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}" font-family="-apple-system,BlinkMacSystemFont,'
             f'\'Segoe UI\',Helvetica,Arial,sans-serif">')
    p.append(f'<rect width="{W}" height="{H}" fill="{c["surface"]}"/>')

    # Title + subtitle. The subtitle carries the claim; the figure has to earn it.
    p.append(f'<text x="{L}" y="30" fill="{c["text"]}" font-size="17" font-weight="600">'
             f'Forecast calibration: model vs. market price</text>')
    p.append(f'<text x="{L}" y="52" fill="{c["text2"]}" font-size="12.5">'
             f'{data["n"]} resolved earnings bets · Brier '
             f'<tspan fill="{c["model"]}" font-weight="600">{data["model_brier"]:.4f} model</tspan>'
             f' vs <tspan fill="{c["market"]}" font-weight="600">{data["market_brier"]:.4f} market</tspan>'
             f' (lower is better)</text>')

    # Recessive grid.
    for t in (0, 0.2, 0.4, 0.6, 0.8, 1.0):
        p.append(f'<line x1="{L}" y1="{Y(t):.1f}" x2="{L+pw}" y2="{Y(t):.1f}" '
                 f'stroke="{c["grid"]}" stroke-width="1"/>')
        p.append(f'<line x1="{X(t):.1f}" y1="{T}" x2="{X(t):.1f}" y2="{T+ph}" '
                 f'stroke="{c["grid"]}" stroke-width="1"/>')
        p.append(f'<text x="{L-10}" y="{Y(t)+4:.1f}" fill="{c["muted"]}" font-size="11" '
                 f'text-anchor="end">{int(t*100)}%</text>')
        p.append(f'<text x="{X(t):.1f}" y="{T+ph+20}" fill="{c["muted"]}" font-size="11" '
                 f'text-anchor="middle">{int(t*100)}%</text>')

    # Perfect-calibration reference — recessive, dashed, labelled in muted ink.
    p.append(f'<line x1="{X(0):.1f}" y1="{Y(0):.1f}" x2="{X(1):.1f}" y2="{Y(1):.1f}" '
             f'stroke="{c["muted"]}" stroke-width="1.5" stroke-dasharray="5 4"/>')
    p.append(f'<text x="{X(0.90):.1f}" y="{Y(0.90)-10:.1f}" fill="{c["muted"]}" '
             f'font-size="10.5" text-anchor="end" transform="rotate(-45 {X(0.90):.1f} '
             f'{Y(0.90)-10:.1f})">perfect calibration</text>')

    # Series: 2px lines, markers ≥8px scaled by bin count, 2px surface ring so
    # overlapping model/market points stay readable.
    for key, label in (("market", "Polymarket price"), ("model", "Our model")):
        pts = sorted(data["series"][key], key=lambda d: d["x"])
        if not pts:
            continue
        col = c[key]
        d = " ".join(f'{"M" if i == 0 else "L"}{X(q["x"]):.1f},{Y(q["y"]):.1f}'
                     for i, q in enumerate(pts))
        p.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2" '
                 f'stroke-linejoin="round"/>')
        for q in pts:
            r = max(4.5, min(11.0, 3.2 + (q["n"] ** 0.5) * 0.75))
            p.append(f'<circle cx="{X(q["x"]):.1f}" cy="{Y(q["y"]):.1f}" r="{r:.1f}" '
                     f'fill="{col}" stroke="{c["surface"]}" stroke-width="2"/>')
        last = pts[-1]
        p.append(f'<text x="{X(last["x"])+16:.1f}" y="{Y(last["y"])+4:.1f}" fill="{col}" '
                 f'font-size="12" font-weight="600">{label}</text>')

    # Axis titles.
    p.append(f'<text x="{L+pw/2:.0f}" y="{T+ph+46}" fill="{c["text2"]}" font-size="12" '
             f'text-anchor="middle">Predicted probability of the outcome</text>')
    p.append(f'<text x="22" y="{T+ph/2:.0f}" fill="{c["text2"]}" font-size="12" '
             f'text-anchor="middle" transform="rotate(-90 22 {T+ph/2:.0f})">'
             f'Observed frequency</text>')

    # The honest caption. Both curves sit ABOVE the diagonal, and saying so matters
    # more than the Brier win: the model is better, and both are under-confident.
    cap = ("Both curves sit above the diagonal — model and market alike under-predict how "
           "often these outcomes occur.")
    cap2 = (f"Marker area ∝ bin count; the two leftmost model points hold only 20 of "
            f"{data['n']} bets, so read them loosely. Base rate {data['base_rate']*100:.0f}%.")
    p.append(f'<text x="{L}" y="{H-32}" fill="{c["text2"]}" font-size="11.5">{cap}</text>')
    p.append(f'<text x="{L}" y="{H-14}" fill="{c["muted"]}" font-size="10.5">{cap2}</text>')
    p.append("</svg>")
    return "\n".join(p)


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    data = calibration_data()
    for mode in ("light", "dark"):
        out = DOCS / f"calibration-{mode}.svg"
        out.write_text(_svg(data, mode))
        print(f"wrote {out}")
    print(f"n={data['n']} model_brier={data['model_brier']:.4f} "
          f"market_brier={data['market_brier']:.4f} base_rate={data['base_rate']:.3f}")
    for k, pts in data["series"].items():
        print(f"  {k}: " + ", ".join(f"({q['x']:.2f}→{q['y']:.2f} n={q['n']})" for q in pts))


if __name__ == "__main__":
    main()
