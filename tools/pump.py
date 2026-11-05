"""PUMP — Pre-earnings Underlying Move Pressure (PURE PRICE, no Polymarket).

A single transparent equation read off the STOCK alone: how hard, and which way, is the
stock being pushed in the days before the print — measured in units of its OWN expected
move, confirmed by volume. It does NOT need a Polymarket price (that's SMG's job), so —
unlike SMG — PUMP is fully BACKTESTABLE on free price history.

    z      = R_abn / EM                       run-up in units of the expected move
    conv   = clip(1 + β·ṽ , 0.5 , 2.0)        volume confirmation (real positioning?)
    PUMP   = 100 · tanh( z · conv / γ )        signed pressure in [-100, +100]

  R_abn  5-day pre-earnings ABNORMAL return = stock 5d return − index 5d return (yfinance)
  EM     the stock's own expected 5-day move = trailing daily-σ × √5 (market_data.expected_move)
  ṽ      volume buildup = recent vol / baseline − 1, clipped (Beaver 1968)
  β,γ    transparent constants (config `pump`)

Reading (what it's FOR — all pure price):
  * sign(PUMP) = which way money is leaning into the print.
  * |PUMP| = how stretched that lean is vs the stock's normal move (precise, comparable
    across tickers because it's normalized by each stock's own volatility).
  * Kelly 2016 overlay: a large PUMP that AGREES with the firm's recency-weighted past
    announcement returns (ExtRet) = over-extrapolated / likely already priced in → FADE
    risk; a large PUMP with no such history = genuine fresh positioning → FOLLOW.

HONESTY: β,γ are principled defaults, not fit. PUMP is a *measurement* (how stretched is
the run-up), which is exact; whether it PREDICTS the post-earnings move is an empirical
question we BACKTEST (tools/backtest.py logs pump per event) and forward-test. Research only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from tools import market_data as md
from tools.config import load_weights

_DEFAULTS = {
    "beta_vol": 0.3,     # volume-confirmation sensitivity
    "gamma": 1.5,        # squash scale (higher = needs a bigger run-up to saturate)
    "ext_ref": 6.0,      # ExtRet (%) reference for the fade overlay
    "strong": 40.0,      # |PUMP| above this = a notably stretched run-up
}


def _params() -> Dict[str, float]:
    p = dict(_DEFAULTS)
    p.update(load_weights().get("pump", {}) or {})
    return p


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def pump_pressure(symbol: str, asof: Optional[datetime] = None,
                  ext_ret: Optional[float] = None, window: int = 5) -> Dict[str, Any]:
    """Compute PUMP from pure price (point-in-time via asof). ext_ret (Kelly ExtRet, %)
    is optional and only drives the follow/fade overlay. Returns a dict; active=False
    when price history is too thin to measure the run-up honestly."""
    p = _params()
    rs = md.relative_strength(symbol, lookback=window, asof=asof)
    r_abn = rs.get("vs_index")
    em = md.expected_move(symbol, window=window, asof=asof)
    v = md.volume_buildup(symbol, lookback=window, asof=asof)

    if r_abn is None or em is None or em <= 0:
        return {"active": False, "pump": None, "verdict": "not enough price history",
                "components": {}, "reasons": ["missing abnormal return or expected move"]}

    z = r_abn / em
    v_clip = _clip(v if v is not None else 0.0, -0.5, 2.0)
    conv = _clip(1.0 + p["beta_vol"] * v_clip, 0.5, 2.0)
    import math
    pump = 100.0 * math.tanh(z * conv / p["gamma"])

    direction = "UP" if pump > 0 else ("DOWN" if pump < 0 else "FLAT")
    stretched = abs(pump) >= p["strong"]

    # Kelly follow/fade overlay (pure price too): big stretched run-up that agrees with the
    # firm's past over-extrapolation = likely already priced in -> fade risk.
    fade_risk = None
    overlay = "n/a (no ExtRet)"
    if ext_ret is not None:
        agree = (pump > 0 and ext_ret > 0) or (pump < 0 and ext_ret < 0)
        fade_risk = _clip((abs(pump) / 100.0) * (abs(ext_ret) / p["ext_ref"]), 0.0, 1.0) if agree else 0.0
        if stretched and fade_risk >= 0.5:
            overlay = f"FADE risk {fade_risk:.0%} — stretched run-up agrees with past over-extrapolation (Kelly)"
        elif stretched:
            overlay = "FOLLOW — stretched run-up, no over-extrapolation history to fade"
        else:
            overlay = "neutral — run-up within normal range"

    strength = ("STRONG" if abs(pump) >= p["strong"] else
                "moderate" if abs(pump) >= 15 else "mild")
    verdict = f"{direction} pressure {pump:+.0f}/100 ({strength}); {overlay}"

    return {
        "active": True,
        "pump": round(pump, 1),
        "direction": direction,
        "stretched": stretched,
        "fade_risk": (round(fade_risk, 3) if fade_risk is not None else None),
        "verdict": verdict,
        "components": {
            "abn_return_5d": round(r_abn, 4),
            "expected_move_5d": round(em, 4),
            "run_up_in_EMs": round(z, 3),
            "volume_buildup": (round(v, 3) if v is not None else None),
            "conviction": round(conv, 3),
        },
        "reasons": [verdict],
        "note": "Pure-price pre-earnings pressure, normalized by the stock's own expected "
                "move. Measurement is exact; predictive value is being back/forward-tested. "
                "Research only.",
    }
