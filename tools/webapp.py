"""Local web platform for the Polymarket Earnings Edge tool.

A thin Flask layer over the existing deterministic tools — no logic lives here,
it just exposes predict / backtest / forward over HTTP and serves one page.
Run: `python -m tools.webapp` (or `python -m tools.cli serve`) then open
http://127.0.0.1:8000. Everything else in tools/ stays the engine; this is UI.
"""
from __future__ import annotations

import os
import traceback
from pathlib import Path
from typing import Any, Dict

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from tools import backtest as bt, forward_test, predict as predict_mod, report
from tools.config import ROOT, TMP_DIR

WEB_DIR = ROOT / "web"
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # cap uploads at 10MB (anti-DoS)

# Only accept requests addressed to localhost. Blocks DNS-rebinding attacks where a
# malicious website resolves a hostname to 127.0.0.1 and tries to reach this server
# from your browser. Combined with the loopback bind, the app is only reachable by you.
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


@app.before_request
def _host_guard():
    host = (request.host or "").rsplit(":", 1)[0]
    if host not in _ALLOWED_HOSTS:
        return jsonify({"error": "forbidden host"}), 403


def _safe_snapshot_path(p):
    """Only allow snapshot files that live under .tmp (uploads). Blocks a crafted
    request from reading arbitrary files on disk (e.g. /etc/passwd) via snapshot_path."""
    if not p:
        return None
    real = os.path.realpath(str(p))
    if real.startswith(os.path.realpath(str(TMP_DIR)) + os.sep) and os.path.isfile(real):
        return real
    return None


def _rows_for(result: Dict[str, Any]):
    """Reuse the CLI report layout so web + terminal show identical fields."""
    return [[label, extract(result)] for label, extract in report.ROWS]


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/api/edge_status")
def api_edge_status():
    """Show the weekly auto-scan output: any tradeable edges caught (EDGE_FOUND.txt) and
    the landscape history (scan_landscape.csv). Read-only, no fabrication — empty when the
    scanner has genuinely found nothing."""
    out = ROOT / "output"
    ef = out / "EDGE_FOUND.txt"
    land = out / "scan_landscape.csv"
    edges = ef.read_text().strip().splitlines() if ef.exists() else []
    rows = []
    if land.exists():
        lines = land.read_text().strip().splitlines()
        hdr = lines[0].split(",") if lines else []
        for ln in lines[1:][-12:]:  # last 12 weekly snapshots
            rows.append(dict(zip(hdr, ln.split(","))))
    # Milestone flags written by the daily self-improve job.
    fdone = out / "FORWARD_DONE.txt"
    fit = out / "FIT_SMG_READY.txt"
    pot_f = out / "POTENTIAL_EDGE.txt"
    potential = pot_f.read_text().strip().splitlines() if pot_f.exists() else []
    dm = out / "daily_metrics.csv"
    last_daily = None
    if dm.exists():
        dl = dm.read_text().strip().splitlines()
        if len(dl) > 1:
            last_daily = dict(zip(dl[0].split(","), dl[-1].split(",")))
    try:
        from tools import edge_book
        book = edge_book.active()
    except Exception:
        book = {"real": [], "potential": [], "n_real": 0, "n_potential": 0}
    try:
        from tools.forward_test import edge_guard
        guard = edge_guard()
    except Exception:
        guard = {"tradeable_now": False, "warning": "", "flagged_pnl_per_1": None}
    try:
        from tools.forward_test import strike_edge_status
        strike_status = strike_edge_status()
    except Exception:
        strike_status = {"verdict": "n/a", "resolved_n": 0, "proven": False}
    conf = out / "EDGE_CONFIRMED.txt"
    disp = out / "EDGE_DISPROVEN.txt"
    fade_f = out / "FADE_BEAT.txt"
    fade = fade_f.read_text().strip().splitlines() if fade_f.exists() else []
    fade_proven_f = out / "FADE_PROVEN.txt"
    return jsonify({"edges": edges, "landscape": rows, "has_edge": bool(edges),
                    "potential": potential, "has_potential": bool(potential),
                    "fade_beat": fade, "has_fade": bool(fade),
                    "edge_book": book, "guard": guard, "strike_edge": strike_status,
                    "edge_confirmed": conf.read_text().strip() if conf.exists() else None,
                    "edge_disproven": disp.read_text().strip() if disp.exists() else None,
                    "fade_proven": fade_proven_f.read_text().strip() if fade_proven_f.exists() else None,
                    "forward_done": fdone.read_text().strip() if fdone.exists() else None,
                    "fit_ready": fit.read_text().strip() if fit.exists() else None,
                    "last_daily": last_daily})


@app.get("/api/edge_book")
def api_edge_book():
    """The persistent Edge Book: all still-active real + potential edges seen."""
    from tools import edge_book
    return jsonify(edge_book.active())


@app.post("/api/upload")
def upload():
    """Save an uploaded TradingView OHLCV CSV; return its server path."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "no file"}), 400
    name = secure_filename(f.filename) or "snapshot.csv"
    dest = TMP_DIR / f"upload_{name}"
    f.save(dest)
    return jsonify({"path": str(dest)})


@app.post("/api/pm_search")
def api_pm_search():
    """Suggest Polymarket markets for a ticker (resolve to company name, then search)."""
    d = request.get_json(force=True) or {}
    ticker = (d.get("ticker") or "").strip()
    if not ticker:
        return jsonify({"candidates": []})
    try:
        from tools import finnhub_client as fh
        from tools.polymarket_client import search_markets
        resolved = fh.resolve_symbol(ticker)
        name = (resolved or {}).get("description", "").split()[0] if resolved else ticker
        seen, cands = set(), []
        for q in (f"{name} earnings", name, ticker):
            for c in search_markets(q, limit=8):
                if c["slug"] in seen:
                    continue
                seen.add(c["slug"])
                cands.append(c)
        cands = [c for c in cands if not c["closed"]] or cands  # open first
        cands.sort(key=lambda c: (not c["earnings_like"], -(c["volume"] or 0)))  # global rank
        return jsonify({"company": name, "candidates": cands[:10]})
    except Exception as e:
        return jsonify({"error": str(e), "candidates": []}), 500


@app.post("/api/find_earnings_date")
def api_find_earnings_date():
    """Web-scan the next earnings date for a ticker (free sources)."""
    d = request.get_json(force=True) or {}
    ticker = (d.get("ticker") or "").strip()
    if not ticker:
        return jsonify({})
    try:
        from tools import finnhub_client as fh, market_data as md
        nxt = fh.get_next_earnings(ticker) or {}
        if nxt.get("date"):
            return jsonify({"date": nxt["date"], "source": "finnhub"})
        return jsonify(md.find_next_earnings_date(ticker) or {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/predict")
def api_predict():
    d = request.get_json(force=True) or {}
    ticker = (d.get("ticker") or "").strip()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    threshold = d.get("threshold")
    try:
        threshold = float(threshold) if threshold not in (None, "", False) else None
    except (TypeError, ValueError):
        threshold = None
    outcome = d.get("outcome") or "eps_beat"
    pm_url = d.get("polymarket_url") or None
    # AUTO-ATTACH: if the user didn't give a market, find the matching live Polymarket market
    # for this ticker+outcome so the edge + link are ALREADY there (no manual step).
    auto_market = None
    if not pm_url:
        try:
            from tools.scanner import _family
            from tools.polymarket_client import search_markets, infer_outcome
            fam = _family(outcome)
            from tools import finnhub_client as fh
            resolved = fh.resolve_symbol(ticker)
            name = (resolved or {}).get("description", "").split()[0] if resolved else ticker
            best = None
            for q in (f"{name} earnings", f"{ticker} earnings", ticker):
                for c in search_markets(q, limit=8):
                    if c.get("closed"):
                        continue
                    tk = None
                    import re as _re
                    m = _re.search(r"\(([A-Z]{1,5})\)", c.get("question", "") or "")
                    tk = m.group(1) if m else None
                    if tk != ticker.upper():
                        continue
                    cfam = _family((c.get("inferred") or {}).get("outcome"))
                    if fam and cfam != fam:
                        continue
                    if best is None or (c.get("volume") or 0) > (best.get("volume") or 0):
                        best = c
                if best:
                    break
            if best:
                pm_url = best.get("slug")
                auto_market = {"slug": best.get("slug"), "url": best.get("url"),
                               "question": best.get("question")}
        except Exception:
            pass
    try:
        result = predict_mod.predict(
            ticker,
            outcome=outcome,
            threshold=threshold,
            polymarket_url=pm_url,
            snapshot_path=_safe_snapshot_path(d.get("snapshot_path")),
            earnings_date_override=(d.get("earnings_date") or None),
        )
        logged = False
        if "error" not in result and result.get("model_prob") is not None:
            from tools import edge_log
            logged = edge_log.log_edge(result)  # auto-log into the forward-test vault
        return jsonify({"rows": _rows_for(result), "raw": result, "logged": logged})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.post("/api/backtest")
def api_backtest():
    d = request.get_json(force=True) or {}
    tickers = d.get("tickers") or None
    if isinstance(tickers, str):
        tickers = [t.strip().upper() for t in tickers.replace(",", " ").split() if t.strip()] or None
    try:
        quarters = int(d.get("quarters") or 8)
        result = bt.backtest(tickers, max_quarters=quarters)
        report_path = bt.write_report(result, quarters) if d.get("report") else None
        result.pop("rows", None)  # drop bulky per-event rows from the response
        result["report_path"] = report_path
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.post("/api/forward")
def api_forward():
    try:
        return jsonify(forward_test.score_log())
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.post("/api/forward_test")
def api_forward_test():
    """Per-event evaluation over a date range [since, until], plus the live vault."""
    d = request.get_json(force=True) or {}
    since = (d.get("since") or "").strip() or None
    until = (d.get("until") or "").strip() or None
    ticker = (d.get("ticker") or "").strip()
    basket = bool(d.get("basket"))
    confident_only = bool(d.get("confident_only"))
    tickers = None if basket else ([ticker] if ticker else None)
    try:
        res = bt.backtest(tickers, since=since, until=until)
        rows = res.get("rows", [])
        # Confident-subset accuracy: where the model says it CAN be confident (SCAR>=45).
        conf = [r for r in rows if (r.get("predictability") or 0) >= 45]
        def _acc(rs):
            return round(sum(1 for r in rs if r.get("pred_beat") == r.get("actual_beat")) / len(rs), 3) if rs else None
        confident = {"n": len(conf), "accuracy": _acc(conf),
                     "all_n": len(rows), "all_accuracy": _acc(rows)}
        shown = conf if confident_only else rows
        events = [{
            "date": r.get("period"), "ticker": r.get("symbol"),
            "prob": r.get("prob"), "pred_beat": r.get("pred_beat"),
            "actual_beat": r.get("actual_beat"),
            "eps_estimate": r.get("eps_estimate"), "eps_actual": r.get("eps_actual"),
            "surprise_pct": r.get("surprise_pct"), "predictability": r.get("predictability"),
            "correct": r.get("pred_beat") == r.get("actual_beat"),
            "diagnosis": bt.diagnose(r),
        } for r in shown]
        events.sort(key=lambda e: e["date"] or "", reverse=True)
        vault = forward_test.score_log().get("summary", {})
        return jsonify({"since": since, "until": until, "metrics": res["metrics_current"],
                        "confident": confident, "events": events[:200], "vault": vault})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.post("/api/reaction_test")
def api_reaction_test():
    """Validate the post-earnings MOVE model (Q3) on realized returns."""
    d = request.get_json(force=True) or {}
    ticker = (d.get("ticker") or "").strip()
    basket = bool(d.get("basket"))
    since = (d.get("since") or "").strip() or None
    tickers = None if basket else ([ticker] if ticker else None)
    try:
        return jsonify(bt.reaction_backtest(tickers, since=since))
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.post("/api/scar")
def api_scar():
    """SCAR 'kills' = predictability 0-100 (how confident/effective the read is) for a ticker."""
    d = request.get_json(force=True) or {}
    tk = (d.get("ticker") or "").strip()
    if not tk:
        return jsonify({"error": "ticker required"}), 400
    try:
        r = predict_mod.predict(tk, outcome="eps_beat")
        if "error" in r:
            return jsonify({"error": r["error"]}), 200
        return jsonify({"ticker": r["ticker"], "kills": r.get("scar_predictability"),
                        "flag": r.get("scar_flag"), "reasons": r.get("scar_reasons", []),
                        "coherence": r.get("scar_conviction"),
                        "acceleration": r.get("scar_acceleration")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/scan")
def api_scan():
    """Scan live Polymarket markets, rank by edge, auto-log to the vault."""
    try:
        from tools import scanner
        d = request.get_json(force=True) or {}
        thr = d.get("threshold")
        try:
            thr = float(thr) if thr not in (None, "", False) else None
        except (TypeError, ValueError):
            thr = None
        result = scanner.scan(limit=int(d.get("limit") or 12),
                              outcome=(d.get("outcome") or None), threshold=thr)
        try:
            from tools.forward_test import edge_guard
            result["guard"] = edge_guard()
        except Exception:
            pass
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.post("/api/scar_tune")
def api_scar_tune():
    """Recalibrate SCAR's predictability threshold from the just-run backtest: find the
    cutoff where the model's calls are most reliable (with enough support), and save it.
    Data-driven tuning of ONE scalar — not a logic change. In-sample, so flagged as such."""
    d = request.get_json(force=True) or {}
    since = (d.get("since") or "").strip() or None
    until = (d.get("until") or "").strip() or None
    ticker = (d.get("ticker") or "").strip()
    basket = bool(d.get("basket"))
    tickers = None if basket else ([ticker] if ticker else None)
    try:
        from tools import scar
        rows = bt.backtest(tickers, since=since, until=until).get("rows", [])
        ev = [(r.get("predictability"), r.get("pred_beat") == r.get("actual_beat"))
              for r in rows if r.get("predictability") is not None]
        if len(ev) < 40:
            return jsonify({"error": f"need ≥40 events to recalibrate (have {len(ev)}). "
                                     "Widen the lookback or use the basket."}), 200
        n = len(ev)
        baseline_acc = round(sum(1 for _, c in ev if c) / n, 3)
        best = None  # (threshold, high_acc, high_n)
        for t in range(20, 71, 5):
            hi = [c for p, c in ev if p >= t]
            if len(hi) < max(25, int(0.25 * n)):   # require real support, no cherry-pick
                continue
            acc = sum(1 for c in hi if c) / len(hi)
            if best is None or acc > best[1] or (acc == best[1] and len(hi) > best[2]):
                best = (t, acc, len(hi))
        if not best:
            return jsonify({"error": "not enough confident events to recalibrate safely."}), 200
        old = scar.predictability_threshold()
        scar.save_calibration({"threshold": best[0], "events": n,
                               "high_n": best[2], "high_accuracy": round(best[1], 3),
                               "overall_accuracy": baseline_acc, "in_sample": True})
        return jsonify({"old_threshold": old, "new_threshold": best[0], "events": n,
                        "high_n": best[2], "high_accuracy": round(best[1], 3),
                        "overall_accuracy": baseline_acc})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.post("/api/forward_log")
def api_forward_log():
    """Forward test: log TODAY's prediction into the library (held pending until
    its earnings date), then return the full library (resolved + pending)."""
    d = request.get_json(force=True) or {}
    ticker = (d.get("ticker") or "").strip()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    try:
        threshold = d.get("threshold")
        threshold = float(threshold) if threshold not in (None, "", False) else None
    except (TypeError, ValueError):
        threshold = None
    try:
        from tools import edge_log
        result = predict_mod.predict(ticker, outcome=d.get("outcome") or "eps_beat",
                                     threshold=threshold,
                                     polymarket_url=(d.get("polymarket_url") or None))
        logged = False
        if "error" not in result and result.get("model_prob") is not None:
            logged = edge_log.log_edge(result, notes="forward")
        lib = forward_test.score_log()
        return jsonify({"logged": logged, "entry": {
            "ticker": result.get("ticker"), "outcome": result.get("outcome"),
            "earnings_date": result.get("earnings_date"),
            "model_prob": result.get("model_prob"), "error": result.get("error"),
        }, "library": lib})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("⚠ WARNING: serving on a non-loopback host exposes this app to your network. "
              "Only do this on a trusted network. Default 127.0.0.1 is local-only.")
    print(f"Polymarket Earnings Edge — open http://127.0.0.1:{port}  (local-only)")
    app.run(host=host, port=port, threaded=True, debug=False)  # debug MUST stay off (RCE risk)


if __name__ == "__main__":
    serve()


# ---------------------------------------------------------------------------
# FORECAST SHEET (option C, 2026-11-05) — ship the forecast, not the bet.
#
# The calibration advantage is this project's one asset that passes its own test:
# model Brier 0.1544 vs market 0.1674 on the same resolved bets, out of sample. The
# money edge does not pass, and on current variance would need ~2,400 favorite-zone
# bets to settle. So this endpoint serves the FORECAST as the product, with the
# scorecard that backs it and the limits that bound it.
#
# The claim is "we forecast earnings outcomes better than the crowd". The claim is
# NOT "this makes money". Both must be visible in the same response, because the
# whole failure mode of this project has been letting the first imply the second.
# ---------------------------------------------------------------------------
@app.post("/api/forecast_sheet")
def api_forecast_sheet():
    try:
        from tools import forward_test, scanner
        d = request.get_json(force=True) or {}
        limit = int(d.get("limit") or 20)

        # OFFLINE scorecard. score_log() resolves pending rows, i.e. makes API calls —
        # rendering a page with it got this endpoint rate-limited and 500ing after 67
        # seconds. Resolution is the daily job's work, never a page load's.
        sc = forward_test.scorecard()
        guard = forward_test.edge_guard()

        scorecard = {
            "n_resolved_bets": sc.get("bets"),
            "model_brier": sc.get("model_brier"),
            "market_brier": sc.get("market_brier"),
            "model_beats_market": sc.get("model_beats_market"),
            "model_accuracy": sc.get("model_accuracy"),
            # The honest other half, carried in the same object on purpose.
            "realized_pnl_per_$1": sc.get("realized_pnl_per_$1"),
            "pnl_ci95": sc.get("pnl_ci95"),
            "pnl_verdict": sc.get("pnl_verdict"),
            "bets_needed_to_confirm": sc.get("n_needed"),
            "money_edge_proven": bool(guard.get("tradeable_now")),
        }

        rows = []
        try:
            scan = scanner.scan(limit=limit)
            for r in scan.get("rows", []):
                rows.append({
                    "ticker": r.get("ticker"),
                    "question": r.get("question"),
                    "earnings_date": r.get("earnings_date"),
                    "outcome": r.get("outcome"),
                    "our_probability": r.get("model_prob"),
                    "market_probability": r.get("market_prob"),
                    "confidence": r.get("confidence"),
                    "scar": r.get("predictability"),
                    "eps_basis": r.get("eps_basis"),
                    "evidence": (r.get("evidence") or [])[:6],
                    "risks": (r.get("risks") or [])[:4],
                    "url": r.get("url"),
                    # Carried so a reader can never mistake a forecast for a trade idea.
                    "tradeable": bool(r.get("trustworthy")),
                    "not_tradeable_because": (r.get("fill_reason") or
                                              ("GAAP contract — we cannot price it"
                                               if (r.get("eps_basis") == "gaap") else "")),
                })
            rows.sort(key=lambda x: (x.get("earnings_date") or "9999",
                                     -(x.get("scar") or 0)))
        except Exception as e:
            rows = []
            scorecard["scan_error"] = str(e)

        return jsonify({
            "scorecard": scorecard,
            "rows": rows,
            "claim": ("These are calibrated probabilities for upcoming earnings outcomes. "
                      "On resolved bets our forecasts score a better Brier than the market "
                      "price does — that is the claim, and it is measured, not asserted."),
            "limits": [
                "This is a FORECAST, not a trade recommendation. Being better calibrated "
                "than the market is not the same as making money against it.",
                "Our realised P&L is not positive and is not powered: at current payoff "
                "variance a real edge would need roughly 2,400 favorite-zone bets to "
                "confirm. Nothing here is proven profitable.",
                "Probabilities answer 'will the company beat ADJUSTED (non-GAAP) "
                "consensus'. Contracts resolving on GAAP EPS are a different question and "
                "are marked as unpriceable.",
                "Most live markets cannot be filled at any size worth trading.",
            ],
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.post("/api/maker")
def api_maker():
    """Paper market-making: quote sheet, fills, and the adverse-selection read."""
    try:
        from tools import maker
        d = request.get_json(force=True) or {}
        if d.get("refresh"):
            return jsonify(maker.daily(limit=int(d.get("limit") or 25)))
        return jsonify({"score": maker.score_quotes(maker._resolve),
                        "sheet": [], "quotes_built": 0})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
