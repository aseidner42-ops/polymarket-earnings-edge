"""SMG — Stake & Market-edge Gauge (formerly "PUMP"). Transparent, NOT a black box.

SMG answers: "vs the Polymarket price, how much EDGE is here net of everything, and how
much should I bet?" It needs a market price q. (The new PUMP, in tools/pump.py, is the
pure-price pre-earnings read that does NOT need Polymarket.)

    Ψ  =  E · w(K) · u(σ̂) · r(V)  −  f_c

  E      gross market-aware edge on the recommended side = |p* − q|   (from the edge
         engine; p* deviates from market price q ONLY on orthogonal signals the market
         underweights. So E already carries the "quality" tilt — SMG does NOT re-apply a
         quality gate, which would double-count.)
  w(K)   SCAR trust gate          = clip(K/100, 0, 1)
  u(σ̂)  CWSF-uncertainty shrink  = 1 / (1 + λ·σ̂/σ̄)
  r(V)   volatility-regime shrink = 1 − γ·max(0, V−0.5)
  f_c    friction (½-spread + fee + uncertainty haircut)  (from ev.py, same numbers)

Ψ is net expected edge per $1 on the recommended side. Stake is FRACTIONAL Kelly on the
shrunk edge, capped by f_max and by real book depth.

HONESTY (config/weights.yaml `smg`): coefficients are PRINCIPLED DEFAULTS from EV/Kelly
math + the literature — NOT fit, because honest fitting needs >=30 RESOLVED forward-vault
bets. Provisional, must be fit on realized P&L later, never hand-tuned. Research only;
SMG never trades.
"""
from __future__ import annotations

from typing import Any, Dict

import json
from tools.config import load_weights, OUTPUT_DIR

_DEFAULTS = {
    "lambda_sigma": 0.04, "sigma_ref": 12.0, "gamma_vol": 0.6, "r_min": 0.3,
    "kelly_fraction": 0.5, "f_max": 0.10, "min_edge": 0.03, "scar_min_K": 45.0,
}
_CALIB = OUTPUT_DIR / "smg_calibration.json"


def _params() -> Dict[str, float]:
    p = dict(_DEFAULTS)
    p.update(load_weights().get("smg", {}) or {})
    # Auto-recalibrated overrides (written by daily_improve, guardrailed). These only tighten
    # the live-edge gates unless forward P&L is PROVEN (CI>0) — never loosen on noise.
    try:
        cal = json.loads(_CALIB.read_text())
        for k in ("min_edge", "scar_min_K"):
            if k in cal:
                p[k] = cal[k]
    except Exception:
        pass
    return p


def save_calibration(payload: Dict[str, Any]) -> None:
    _CALIB.write_text(json.dumps(payload, indent=2))


def recommend_calibration(bets, current_min_edge: float, current_scar_min: float):
    """Recalibrate SMG's live-edge gate from RESOLVED forward bets. bets = list of
    (net_edge, pnl_per_$1). Honest asymmetry to avoid self-fooling on noisy data:
      * LOOSEN min_edge (accept smaller edges) ONLY at a threshold where the bets above it
        are PROVEN profitable — mean P&L 95% CI lower bound > 0 with >=25 bets support.
      * otherwise HOLD, or TIGHTEN (raise min_edge) if the current live bets are losing.
    EMA-smoothed + snapped. Returns dict or None if too few bets."""
    b = [(e, p) for e, p in bets if e is not None and p is not None]
    n = len(b)
    if n < 30:
        return {"min_edge": current_min_edge, "scar_min": current_scar_min,
                "changed": False, "n": n, "reason": f"only {n} bets (<30) — hold"}

    def _ci_low(vals):
        if len(vals) < 2:
            return -9.9
        import statistics as st
        m = st.mean(vals); sd = st.pstdev(vals) if len(vals) > 1 else 0.0
        se = sd / (len(vals) ** 0.5)
        return m - 1.96 * se

    # Find the LOWEST edge threshold whose above-set is proven profitable (CI>0, >=25 bets).
    proven = None
    for cand in (0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10, 0.12):
        sub = [p for e, p in b if e >= cand]
        if len(sub) >= 25 and _ci_low(sub) > 0:
            proven = cand
            break
    cur = current_min_edge
    if proven is not None:
        target = proven                      # proven profitable -> allow (even if looser)
        reason = f"threshold {proven:.0%} proven profitable (CI>0, n>=25)"
    else:
        # No proven profitable gate. If current live bets lose, tighten; else hold.
        live = [p for e, p in b if e >= cur]
        losing = live and (sum(live) / len(live) <= 0)
        target = min(cur + 0.02, 0.15) if losing else cur
        reason = ("no proven profitable gate; current live bets losing -> TIGHTEN"
                  if losing else "no proven profitable gate -> HOLD (conservative)")
    smoothed = round((0.7 * cur + 0.3 * target) / 0.01) * 0.01
    smoothed = max(0.03, min(0.15, smoothed))
    changed = abs(smoothed - cur) >= 0.01
    return {"min_edge": round(smoothed, 3), "scar_min": current_scar_min,
            "changed": changed, "n": n, "reason": reason, "old_min_edge": cur}


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def smg_edge(result: Dict[str, Any]) -> Dict[str, Any]:
    """Compute Ψ + a recommended stake from an assembled predict() result.
    active=False (with a reason) when there is no market price / model probability —
    SMG stays silent rather than inventing an edge."""
    p = _params()
    edge = result.get("edge") or {}
    q = edge.get("market_prob")
    if q is None or result.get("model_prob") is None:
        return {"active": False, "psi": None, "stake_fraction": 0.0, "max_usd": None,
                "side": None, "verdict": "no edge — needs a Polymarket price",
                "live": False, "components": {}, "reasons": ["no market price / model probability"]}

    side = edge.get("side")
    e_gross = edge.get("gross_edge")
    f_c = edge.get("friction") or 0.0
    p_aware = result.get("market_aware_prob")
    if p_aware is None:
        p_aware = result.get("model_prob")
    if e_gross is None:
        e_gross = abs(p_aware - q)

    K = result.get("scar_predictability") or 0.0
    w_K = _clip(K / 100.0, 0.0, 1.0)
    sigma = result.get("cwsf_sigma")
    u_sig = 1.0 / (1.0 + p["lambda_sigma"] * (sigma / p["sigma_ref"])) if sigma is not None else 0.85
    vix = result.get("vix") or {}
    V = vix.get("percentile")
    r_V = max(p["r_min"], 1.0 - p["gamma_vol"] * max(0.0, (V - 0.5))) if V is not None else 1.0

    shrink = w_K * u_sig * r_V
    psi = e_gross * shrink - f_c

    if side == "NO":
        f_full = max(0.0, (q - p_aware)) / q if q > 0 else 0.0
    else:
        f_full = max(0.0, (p_aware - q)) / (1.0 - q) if q < 1 else 0.0
    stake_fraction = _clip(p["kelly_fraction"] * f_full * shrink, 0.0, p["f_max"])
    max_usd = edge.get("max_tradeable_usd")

    reasons, live = [], True
    if psi < p["min_edge"]:
        live = False; reasons.append(f"Ψ {psi:+.1%} below {p['min_edge']:.0%} live-edge threshold")
    if K < p["scar_min_K"]:
        live = False; reasons.append(f"SCAR K={K:.0f} < {p['scar_min_K']:.0f} (low conviction — sit out)")
    if max_usd is not None and max_usd <= 0:
        live = False; reasons.append("book too thin to execute any size at an edge")
    if stake_fraction <= 0:
        live = False; reasons.append("no positive Kelly stake")
    if live:
        sz = f" on up to ~${max_usd:,.0f}" if max_usd else ""
        reasons.append(f"trust×uncertainty×regime shrink = {shrink:.2f}; ½-Kelly stake {stake_fraction:.1%} of bankroll{sz}")
    verdict = (f"LIVE: {side} — Ψ {psi:+.1%}/$1, stake {stake_fraction:.1%} of bankroll"
               if live else f"SKIP — {reasons[0] if reasons else 'no edge'}")

    return {
        "active": True, "psi": round(psi, 4), "stake_fraction": round(stake_fraction, 4),
        "max_usd": max_usd, "side": side, "verdict": verdict, "live": live,
        "components": {"E_gross": round(e_gross, 4), "w_K": round(w_K, 3),
                       "u_sigma": round(u_sig, 3), "r_V": round(r_V, 3),
                       "shrink": round(shrink, 3), "friction": round(f_c, 4),
                       "kelly_full": round(f_full, 4)},
        "reasons": reasons,
        "note": "PROVISIONAL defaults (EV/Kelly + literature); not fit on realized P&L yet "
                "— validate on the forward vault. Research only, never auto-trades.",
    }
