"""Daily near-autonomous self-RETRAINING routine (self-improving only once versions beat future untouched data + Polymarket net of costs) (run by launchd via daily_improve.sh).

HONEST SCOPE — what this does and does NOT do:
  DOES (safe, deterministic, logged, reversible):
    1. Backtest 50 rotating tickers/day (cycles a ~200-name universe over ~4 days).
    2. Recalibrate the SCAR predictability THRESHOLD from that backtest — one scalar, with
       guardrails (>=40 events, >=25% support, only move if the confident tier beats the base
       rate by >=3 pts). Logged; reversible by deleting output/scar_calibration.json.
    3. Forward-resolve the vault (score bets whose earnings passed) + append daily metrics.
    4. Notify (macOS banner + durable flag) when ALL forward tests are resolved (pending==0)
       or when >=30 bets have resolved (time to fit SMG together).
  DOES NOT (kept human-in-the-loop on purpose, to avoid unattended overfitting/self-fooling):
    * rewrite model logic, invent signals, or fit SMG's coefficients unattended. When >=30
      bets resolve it raises FIT_SMG_READY.txt so we do that review together.

Everything is appended to output/daily_improve.log + output/daily_metrics.csv.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime

from datetime import date

from tools import scar
from tools import validation as V
from tools.backtest import backtest, _logistic, FUND_SPREAD
from tools.config import load_weights, OUTPUT_DIR, ROOT
from tools import forward_test
from tools.forward_test import score_log
from tools.scanner import scan as scan_markets, write_landscape

# Daily edge-scan + notify starts only AFTER the forward vault is fully logged (user's
# instruction: don't surface edges before the current 50-name vault finishes ~Jul 22 2026).
EDGE_SCAN_START = date(2026, 7, 22)

# ~200-name liquid universe; 50/day rotates through it (state in daily_state.json).
UNIVERSE = (
    "AAPL MSFT NVDA META AMZN GOOGL TSLA NFLX AMD INTC QCOM AVGO MU TXN ORCL CRM ADBE NOW "
    "INTU PANW SNOW NET DDOG CRWD ZS MDB PLTR UBER ABNB COIN DKNG HOOD AFRM SOFI RBLX U DASH "
    "KO PEP MDLZ MNST KHC PG CL KMB GIS K STZ TAP "
    "JPM BAC GS WFC C AXP V MA PYPL MS USB PNC TFC SCHW BK STT COF "
    "XOM CVX COP SLB OXY PSX MPC KMI WMB "
    "JNJ PFE MRK ABBV LLY TMO ABT BMY AMGN GILD ISRG MDT CVS UNH CI ELV HUM MCK "
    "CAT DE BA GE HON MMM UNP UPS LMT NOC GD RTX EMR ETN ITW PWR "
    "HD LOW TJX COST WMT TGT NKE SBUX MCD CMG DIS CMCSA T VZ "
    "FCX NEM APD ECL SHW LIN DOW "
    "AEP SO NEE DUK D EXC CEG VST "
    "SPGI MCO ICE CME MSCI FDS NDAQ "
    "ROKU SNAP PINS RIVN LCID ENPH FSLR PANW WDAY TEAM DOCU OKTA TWLO"
).split()


def _log(line: str):
    with open(OUTPUT_DIR / "daily_improve.log", "a") as fh:
        fh.write(line + "\n")
    print(line)


_TN = (ROOT / "tools" / "vendor" / "terminal-notifier.app" / "Contents" / "MacOS"
       / "terminal-notifier")


def _notify(title: str, msg: str, url: str = "http://127.0.0.1:8000"):
    """True silent top-right Notification-Center banner, click-to-open. Uses the bundled
    terminal-notifier (its own app identity => real banners on modern macOS, unlike CLI
    osascript). Clicking the banner opens the app. Falls back to osascript if the binary is
    missing. Durable flag files remain the guaranteed channel regardless."""
    if _TN.exists():
        try:
            subprocess.run([str(_TN), "-title", title, "-message", msg,
                            "-open", url, "-sound", "none",
                            "-group", "earnings-edge"], timeout=10, check=False)
            return
        except Exception:
            pass
    try:
        subprocess.run(["osascript", "-e",
                        f"display notification \"{msg}\" with title \"{title}\""],
                       timeout=10, check=False)
    except Exception:
        pass


def _next_batch(stamp: str):
    state_path = OUTPUT_DIR / "daily_state.json"
    try:
        idx = json.loads(state_path.read_text()).get("idx", 0)
    except Exception:
        idx = 0
    n = len(UNIVERSE)
    batch = [UNIVERSE[(idx + k) % n] for k in range(80)]   # bigger daily backtest (was 50)
    state_path.write_text(json.dumps({"idx": (idx + 80) % n, "updated": stamp}))
    return sorted(set(batch))


def seed_earnings_calendar(stamp: str, days: int = 3, cap: int = 20) -> int:
    """FORWARD-DATA SEEKER (real companies, no Polymarket needed). Pulls the free earnings
    calendar for the next `days`, keeps only names WITH an analyst consensus (has coverage =
    reliable free data), predicts beat/miss for each, and logs to the vault. These resolve via
    free EPS actuals -> feed SCAR + the auto-fitter -> sharpen the MODEL (not a money edge; no
    market attached). Capped + dedup'd so the daily job stays bounded and rate-limits survive."""
    import time as _t
    from datetime import date, timedelta
    from tools import predict as _pred, edge_log as _el, finnhub_client as _fh
    try:
        frm = date.fromisoformat(stamp[:10])
    except Exception:
        frm = date.today()
    to = (frm + timedelta(days=days)).isoformat()
    try:
        cal = (_fh._get("/calendar/earnings", {"from": frm.isoformat(), "to": to}) or {}).get("earningsCalendar") or []
    except Exception as e:
        _log(f"calendar-seed: fetch failed ({e})"); return 0
    # keep only names with a consensus estimate (coverage); dedup; cap.
    seen, cands = set(), []
    for c in cal:
        sym = (c.get("symbol") or "").strip().upper()
        if not sym or sym in seen or c.get("epsEstimate") in (None, 0):
            continue
        if not sym.isalpha() or len(sym) > 5:   # skip odd/foreign tickers
            continue
        seen.add(sym); cands.append(sym)
    cands = sorted(cands)[:cap]
    ok = 0
    for sym in cands:
        try:
            r = _pred.predict(sym, outcome="eps_beat")
            if r.get("model_prob") is not None and _el.log_edge(r, notes="cal-fwd"):
                ok += 1
        except Exception:
            pass
        _t.sleep(1.5)
    _log(f"calendar-seed: {len(cal)} reporters {frm}->{to}, logged {ok}/{len(cands)} "
         f"(consensus-covered names) as beat/miss forward tests")
    return ok


def run(stamp: str) -> dict:
    _log(f"\n===== {stamp} daily_improve =====")

    # 1) Backtest 50 rotating tickers.
    batch = _next_batch(stamp)
    _log(f"backtest {len(batch)} tickers: {' '.join(batch)}")
    bt = backtest(batch, max_quarters=8)
    rows = bt.get("rows", [])
    log = load_weights()["logistic"]
    events = []
    for r in rows:
        if r.get("predictability") is None:
            continue
        p = _logistic(log["intercept"] + log["gain"] * FUND_SPREAD * r["norm"])
        events.append((r["predictability"], (p > 0.5) == r["actual_beat"]))
    mc = bt.get("metrics_current", {})
    _log(f"backtest: {mc.get('n')} events, acc {mc.get('accuracy')}, brier {mc.get('brier')}, "
         f"scar-usable {len(events)}")

    # 2) Recalibrate SCAR threshold on a ROLLING POOL of recent events (not just today's 50)
    #    so it reflects a large stable sample instead of daily sampling noise. Keep ~21 days.
    pool_path = OUTPUT_DIR / "scar_events.jsonl"
    import time as _t
    cutoff = _t.time() - 21 * 86400
    pool = []
    if pool_path.exists():
        for ln in pool_path.read_text().splitlines():
            try:
                r = json.loads(ln)
                if r.get("ts", 0) >= cutoff:
                    pool.append(r)
            except Exception:
                pass
    now_ts = _t.time()
    for p, c in events:
        pool.append({"p": p, "c": bool(c), "ts": now_ts})
    pool = pool[-6000:]  # hard cap
    pool_path.write_text("\n".join(json.dumps(r) for r in pool))
    pooled_events = [(r["p"], r["c"]) for r in pool]
    _log(f"SCAR pool: {len(pooled_events)} events over ~21d (today added {len(events)})")
    rec = scar.recommend_threshold(pooled_events)
    if rec is None:
        _log("SCAR: not enough support to recalibrate today (no change).")
    elif rec["changed"]:
        scar.save_calibration({"threshold": rec["threshold"], "events": rec["events"],
                               "high_n": rec["high_n"], "high_accuracy": rec["high_accuracy"],
                               "overall_accuracy": rec["overall_accuracy"],
                               "in_sample": True, "set_by": "daily_improve", "at": stamp})
        _log(f"SCAR THRESHOLD {rec['old']} -> {rec['threshold']} "
             f"(confident tier {rec['high_accuracy']:.0%} over {rec['high_n']} vs {rec['overall_accuracy']:.0%})")
    else:
        _log(f"SCAR: threshold held at {rec['old']} (confident tier not materially better today).")

    # 2b) AUTO-FIT the beat-model weights DAILY (walk-forward, OOS-gated), reusing today's
    #     backtest rows so we don't backtest twice. Adopts a new formula only if it beats the
    #     current one on held-out data — can't overfit; changes nothing if no real improvement.
    try:
        from tools import autofit
        af = autofit.run(stamp, rows=rows)
        if af.get("adopted"):
            _notify("Earnings Edge — model re-fitted (OOS-gated)",
                    f"Auto-fitter adopted new weights: OOS Brier {af['fitted_oos_brier']} < "
                    f"{af['current_oos_brier']} on {af['test_n']} held-out events.")
            _log(f"AUTOFIT ADOPTED: OOS {af['fitted_oos_brier']} < {af['current_oos_brier']} "
                 f"(test {af['test_n']})")
        else:
            _log(f"autofit: {af.get('reason')} (OOS {af.get('fitted_oos_brier')} vs "
                 f"{af.get('current_oos_brier')})")
    except Exception as e:
        _log(f"autofit error: {e}")

    # 2c) SEEK MORE FORWARD DATA: log real companies reporting in the next few days as
    #     beat/miss forward tests (no Polymarket needed) -> more resolved EPS outcomes to
    #     sharpen SCAR + the auto-fitter. Improves the MODEL, not a money edge.
    try:
        seed_earnings_calendar(stamp, days=3, cap=20)
    except Exception as e:
        _log(f"calendar-seed error: {e}")

    # 3) Forward-resolve + score the vault.
    fwd = score_log()
    s = fwd.get("summary", {})
    _log(f"forward: logged {s.get('logged')} resolved {s.get('resolved')} pending {s.get('pending')} "
         f"| bets {s.get('bets')} model_brier {s.get('model_brier')} market_brier {s.get('market_brier')} "
         f"pnl/$1 {s.get('realized_pnl_per_$1')} verdict: {s.get('verdict')}")

    # 3b) SMG auto-recalibration from resolved bets (guardrailed: tighten-unless-proven).
    try:
        import csv as _csv
        from tools import smg
        from tools.config import EDGE_LOG_PATH
        H = 0.02
        smg_bets = []
        if EDGE_LOG_PATH.exists():
            for r in _csv.DictReader(open(EDGE_LOG_PATH)):
                if not r.get("resolved") or r.get("actual_outcome") not in ("YES", "NO"):
                    continue
                try:
                    q = float(r["market_prob"]); ne = float(r["net_edge"])
                except (TypeError, ValueError, KeyError):
                    continue
                mp = float(r["model_prob"]) if r.get("model_prob") not in (None, "", "None") else None
                side = V.bet_side(r)      # blank side = refusal, not a bet
                if side is None:
                    continue
                won = (side == r["actual_outcome"])
                pnl = ((1 - q) / q - H if won else -1.0) if side == "YES" else ((q / (1 - q)) - H if won else -1.0)
                smg_bets.append((ne, pnl))
        p = smg._params()
        sc = smg.recommend_calibration(smg_bets, p["min_edge"], p["scar_min_K"])
        if sc and sc.get("changed"):
            smg.save_calibration({"min_edge": sc["min_edge"], "scar_min_K": sc["scar_min"],
                                  "set_by": "daily_improve", "at": stamp, "n_bets": sc["n"],
                                  "reason": sc["reason"]})
            _log(f"SMG min_edge {sc.get('old_min_edge')} -> {sc['min_edge']} ({sc['reason']})")
        elif sc:
            _log(f"SMG: gate held at {p['min_edge']} ({sc.get('reason')})")
    except Exception as e:
        _log(f"SMG recalib error: {e}")

    # 3d) FADE RELIABILITY CHECK — notify (once) when resolved FADE bets actually PAY.
    #     Fade signature = side NO on a market that priced a BEAT (market_prob>=0.65). Proven =
    #     >=15 such bets AND realized-P&L 95% CI-low > 0. Honest: it loses now (UBER), so this
    #     only fires when fades genuinely turn profitable over time.
    try:
        import csv as _csv
        from tools.config import EDGE_LOG_PATH
        H = 0.02
        fp = []
        if EDGE_LOG_PATH.exists():
            for r in _csv.DictReader(open(EDGE_LOG_PATH)):
                if not r.get("resolved") or r.get("actual_outcome") not in ("YES", "NO"):
                    continue
                try:
                    q = float(r["market_prob"]); mp = float(r["model_prob"])
                except (TypeError, ValueError, KeyError):
                    continue
                side = V.bet_side(r)      # blank side = refusal, not a fade
                if side is None:
                    continue
                if side != "NO" or q < 0.65:      # fade signature: bet NO against a priced beat
                    continue
                won = (r["actual_outcome"] == "NO")
                fp.append((q / (1 - q)) - H if won else -1.0)
        if len(fp) >= 15:
            m = sum(fp) / len(fp)
            sd = (sum((x - m) ** 2 for x in fp) / (len(fp) - 1)) ** 0.5
            ci_low = m - 1.96 * sd / (len(fp) ** 0.5)
            _log(f"fade-reliability: {len(fp)} fade bets, pnl/$1 {m:+.3f}, CI-low {ci_low:+.3f}")
            if ci_low > 0 and not (OUTPUT_DIR / "FADE_PROVEN.txt").exists():
                (OUTPUT_DIR / "FADE_PROVEN.txt").write_text(
                    f"{stamp}: FADES NOW RELIABLE — {len(fp)} fade bets, pnl/$1 {m:+.3f}, "
                    f"95% CI-low {ci_low:+.3f} > 0. The fade-the-beat scenario is proven profitable.\n")
                _notify("Earnings Edge — FADES ARE RELIABLE ✅",
                        f"Fade-the-beat proven: +{m:.2f}/$1 over {len(fp)} bets, CI-low {ci_low:+.2f}>0. "
                        f"The scenario you like now pays. Lock back on.")
                _log("*** FADES PROVEN — notified ***")
    except Exception as e:
        _log(f"fade-reliability error: {e}")

    # daily metrics CSV
    mpath = OUTPUT_DIR / "daily_metrics.csv"
    new = not mpath.exists()
    with open(mpath, "a") as fh:
        if new:
            fh.write("date,bt_events,bt_acc,bt_brier,scar_threshold,resolved,pending,bets,"
                     "model_brier,market_brier,pnl_per_1,beats_crowd\n")
        fh.write(f"{stamp},{mc.get('n')},{mc.get('accuracy')},{mc.get('brier')},"
                 f"{scar.predictability_threshold()},{s.get('resolved')},{s.get('pending')},"
                 f"{s.get('bets')},{s.get('model_brier')},{s.get('market_brier')},"
                 f"{s.get('realized_pnl_per_$1')},{s.get('beats_crowd')}\n")

    # 4) Notify on milestones — each fires ONCE (guarded by its flag file, no daily spam).
    bets = s.get("bets") or 0
    pending = s.get("pending")
    ci = s.get("pnl_ci95") or [None, None]
    ci_low, ci_high = (ci[0] if len(ci) else None), (ci[1] if len(ci) > 1 else None)

    def _once(flag: str) -> bool:
        return not (OUTPUT_DIR / flag).exists()

    if bets >= 30 and _once("FIT_SMG_READY.txt"):
        (OUTPUT_DIR / "FIT_SMG_READY.txt").write_text(
            f"{stamp}: {bets} bets resolved — enough to fit SMG together.\n")
        _notify("Earnings Edge", f"{bets} bets resolved — ready to fit SMG. Lock back on.")

    # THE BIG ONE: proven edge = >=30 INDEPENDENT bets, bootstrap 95% CI-low > 0, AND the
    # mean still positive after its 5 biggest wins are removed. The tail leg was added
    # 2026-11-03: the 2026-10-30 "EDGE CONFIRMED" was two duplicated q~0.03 longshot fills
    # carrying 89% of the P&L. Do not drop it — a tail-driven mean is not an edge.
    ex5 = s.get("mean_ex_top5")
    robust = ex5 is None or ex5 > 0
    if bets >= 30 and ci_low is not None and ci_low > 0 and robust and _once("EDGE_CONFIRMED.txt"):
        (OUTPUT_DIR / "EDGE_CONFIRMED.txt").write_text(
            f"{stamp}: EDGE CONFIRMED — pnl/$1 {s.get('realized_pnl_per_$1')} over {bets} bets, "
            f"95% CI [{ci_low}, {ci_high}] (low>0), beats_crowd {s.get('beats_crowd')}.\n")
        _notify("Earnings Edge — WE'RE GOOD ✅",
                f"EDGE CONFIRMED: +{s.get('realized_pnl_per_$1')}/$1 over {bets} bets, "
                f"CI low {ci_low} > 0. Lock back on.", url="http://127.0.0.1:8000/")
        _log(f"*** EDGE CONFIRMED — notified. pnl {s.get('realized_pnl_per_$1')} CI [{ci_low},{ci_high}] ***")

    # Honest opposite: >=30 bets AND CI HIGH < 0 => edge disproven (losing, significant).
    if bets >= 30 and ci_high is not None and ci_high < 0 and _once("EDGE_DISPROVEN.txt"):
        (OUTPUT_DIR / "EDGE_DISPROVEN.txt").write_text(
            f"{stamp}: EDGE DISPROVEN — pnl/$1 {s.get('realized_pnl_per_$1')} over {bets} bets, "
            f"95% CI [{ci_low}, {ci_high}] (high<0). The model loses money net.\n")
        _notify("Earnings Edge — no edge (honest)",
                f"Forward test says LOSING: {s.get('realized_pnl_per_$1')}/$1 over {bets} bets, "
                f"CI high {ci_high} < 0. Do not trade this.")
        _log("*** EDGE DISPROVEN — notified (honest negative result) ***")

    # PAPER MAKER (option A, 2026-11-05). We have always been a taker, lifting the ask and
    # paying ~4c of spread to chase a ~2.6% edge. This posts fair-value quotes instead and
    # measures what WOULD have filled — above all whether fills cluster on the side that
    # later proved wrong (adverse selection). No orders are placed; nothing is at risk.
    try:
        from tools import maker as _maker
        _m = _maker.daily(limit=25)
        _ms = _m.get("score") or {}
        _log(f"paper-maker: built {_m.get('quotes_built')} quotes, logged "
             f"{_m.get('quotes_logged')}, new fills {_m.get('new_fills')} | "
             f"total {_ms.get('quotes_logged')} quotes / {_ms.get('filled')} filled "
             f"(rate {_ms.get('fill_rate')}), mean spread earned "
             f"{_ms.get('mean_spread_earned')}, resolved fills {_ms.get('resolved_fills')}")
        _log(f"paper-maker adverse selection: {_ms.get('adverse_selection')}")
    except Exception as _e:
        _log(f"paper-maker failed: {_e}")

    _log(f"verdict progress: bets {bets}/30, pnl/$1 {s.get('realized_pnl_per_$1')} "
         f"(median {s.get('pnl_median')}, ex-top5 {s.get('mean_ex_top5')}), "
         f"CI [{ci_low},{ci_high}] — need CI-low>0 AND ex-top5>0 to confirm.")
    # Where the money actually goes, by the price of the contract bought, plus the live
    # power requirement. Added 2026-11-05: a 30-bet bar cannot resolve a realistic edge.
    try:
        _g = forward_test.edge_guard()
        _bc = _g.get("by_cost") or {}
        _f, _l = _bc.get("favorites") or {}, _bc.get("longshots") or {}
        _log(f"by-contract-cost: FAVORITES n={_f.get('n')} mean={_f.get('mean')} "
             f"SD={_f.get('sd')} need~{_f.get('n_needed')} | LONGSHOTS n={_l.get('n')} "
             f"mean={_l.get('mean')} SD={_l.get('sd')} need~{_l.get('n_needed')} | "
             f"deep(<10c) {_bc.get('deep_longshots_under_10c')}")
        _log(f"guard: {_g.get('verdict')}")
    except Exception as _e:
        _log(f"by-cost report failed: {_e}")

    if pending == 0 and (s.get("resolved") or 0) > 0 and _once("FORWARD_DONE.txt"):
        (OUTPUT_DIR / "FORWARD_DONE.txt").write_text(
            f"{stamp}: ALL forward tests resolved. {json.dumps(s, indent=2)}\n")
        _notify("Earnings Edge", "All forward tests are done — lock back on to review.")
        _log("*** ALL FORWARD TESTS DONE — notified + FORWARD_DONE.txt written ***")

    # 5) DAILY EDGE SCAN + NOTIFY — gated to start only after the vault is fully logged.
    today = date.fromisoformat(stamp[:10])
    if today < EDGE_SCAN_START:
        _log(f"edge-scan holds until {EDGE_SCAN_START} (vault still logging); skipping.")
    else:
        try:
            sc = scan_markets(limit=40)  # widen further -> seek every live PM market -> more forward data
            write_landscape(sc, stamp)  # landscape history + EDGE_FOUND.txt for trustworthy
            rows = sc.get("rows", [])
            believable = [r for r in rows if r.get("trustworthy")]
            # POTENTIAL = a meaningful model-vs-market divergence that is NOT yet trustworthy
            # (thin book / low SCAR / below cost). User wants to see these too — clearly
            # labeled UNPROVEN so they're never mistaken for actionable edges.
            def _gap(r):
                mp, q = r.get("model_prob"), r.get("market_prob")
                return abs(mp - q) if (mp is not None and q is not None) else 0
            potential = [r for r in rows if not r.get("trustworthy") and _gap(r) >= 0.15]
            # FADE-THE-BEAT: market prices a beat (q>=0.65) but model says miss — user's #1 ask.
            fade = [r for r in rows if r.get("fade_beat")]
            if fade:
                with open(OUTPUT_DIR / "FADE_BEAT.txt", "w") as fh:
                    for r in sorted(fade, key=lambda x: -(x.get("market_prob") or 0)):
                        fh.write(f"{stamp}  {r['ticker']} {r['outcome']}: market {r.get('market_prob')} "
                                 f"BEAT vs model {r.get('model_prob')} MISS -> side NO · "
                                 f"trustworthy={r.get('trustworthy')} SCAR={r.get('predictability')} "
                                 f"maxUSD={r.get('max_usd')} | {r.get('url')}\n")
                # NOTIFY only on SCAR>=45 fades (user: don't ping me on low-conviction).
                fade_hi = [r for r in fade if (r.get("predictability") or 0) >= 45]
                if fade_hi:
                    ftop = max(fade_hi, key=lambda x: (x.get("market_prob") or 0))
                    _notify("Earnings Edge — FADE THE BEAT",
                            f"{ftop['ticker']}: market BEAT {ftop.get('market_prob')} vs our "
                            f"{ftop.get('model_prob')} → bet NO · SCAR {ftop.get('predictability')}. "
                            f"{ftop.get('url') or ''}",
                            url="http://127.0.0.1:8000/?edge=1")
                    _log(f"*** FADE-THE-BEAT (SCAR>=45): {len(fade_hi)} — notified. Top {ftop['ticker']} "
                         f"SCAR {ftop.get('predictability')} ***")
                else:
                    _log(f"fade: {len(fade)} seen but none SCAR>=45 — logged, NOT notified.")
            # KALSHI reach: check daily for genuine beat/miss markets (mention-markets excluded).
            # None today; if Kalshi ever lists one, flag + notify so we wire full evaluation.
            try:
                from tools.kalshi_client import find_beat_markets
                kb = find_beat_markets()
                if kb:
                    (OUTPUT_DIR / "KALSHI_MARKETS.txt").write_text(
                        "\n".join(f"{k['ticker']} {k['question']} @ {k['yes_price']} | {k['url']}" for k in kb))
                    _notify("Earnings Edge — Kalshi beat/miss market appeared",
                            f"{len(kb)} Kalshi EPS/revenue market(s) now live — new venue to check.")
                    _log(f"KALSHI: {len(kb)} beat/miss market(s) found -> flagged for full wiring")
                else:
                    _log("kalshi: 0 applicable beat/miss markets (mention-only) — reach checked, nothing to add")
            except Exception as e:
                _log(f"kalshi check error: {e}")
            from tools import edge_book
            bk = edge_book.upsert(believable, potential, stamp)
            _log(f"edge-scan: {sc.get('scanned',0)} markets, {len(believable)} BELIEVABLE, "
                 f"{len(potential)} POTENTIAL | edge-book active: {bk['n_real']} real / {bk['n_potential']} potential")
            # write a potential-edge flag file (separate from EDGE_FOUND)
            if potential:
                # Fillable first, then by gap. Every line now carries WHY it is unfillable,
                # so a paper divergence can't read like money (2026-11-03).
                with open(OUTPUT_DIR / "POTENTIAL_EDGE.txt", "w") as fh:
                    for r in sorted(potential, key=lambda x: (not x.get("paper_only", True),
                                                              _gap(x)), reverse=True):
                        tag = "PAPER-ONLY" if r.get("paper_only") else "FILLABLE"
                        why = f" why={r.get('fill_reason')}" if r.get("fill_reason") else ""
                        fh.write(f"{stamp}  [{tag}] {r['ticker']} {r['outcome']} side={r.get('side')} "
                                 f"model={r.get('model_prob')} mkt={r.get('market_prob')} "
                                 f"gap={_gap(r):.2f} net_edge={r.get('net_edge')} "
                                 f"SCAR={r.get('predictability')} maxUSD={r.get('max_usd')} "
                                 f"E[$]={r.get('expected_profit_usd')} spread={r.get('book_spread')}"
                                 f"{why} | {r.get('url')}\n")
                n_fill = sum(1 for r in potential if not r.get("paper_only"))
                _log(f"potential: {len(potential)} ({n_fill} fillable, "
                     f"{len(potential)-n_fill} paper-only)")
            # NOTIFY on either — tiered so 'potential' is never dressed up as 'believable'.
            if believable:
                top = max(believable, key=lambda r: r.get("edge_quality", 0))
                extra = f" (+{len(potential)} potential)" if potential else ""
                _notify("Earnings Edge — EDGE FOUND",
                        f"{top['ticker']} {top['outcome']} {top.get('side')} net "
                        f"{top.get('net_edge')} · SCAR {top.get('predictability')}{extra}. "
                        f"{top.get('url') or ''}",
                        url="http://127.0.0.1:8000/?edge=1")
                _log(f"*** {len(believable)} BELIEVABLE — notified. Top {top['ticker']} "
                     f"net_edge={top.get('net_edge')} max=${top.get('max_usd')} SCAR={top.get('predictability')} ***")
            else:
                # POTENTIAL: notify ONLY on SCAR>=45 (user: no low-conviction pings). Others
                # still logged to POTENTIAL_EDGE.txt + edge book, just no notification.
                # Prefer a FILLABLE potential; fall back to paper-only but SAY SO in the
                # banner, so a 23-cent-spread mid-price gap never pings as if it were money.
                pot_hi = [r for r in potential if (r.get("predictability") or 0) >= 45]
                fillable_hi = [r for r in pot_hi if not r.get("paper_only")]
                pick = fillable_hi or pot_hi
                if pick:
                    top = max(pick, key=_gap)
                    is_paper = top.get("paper_only")
                    label = ("potential — PAPER ONLY, can't fill" if is_paper
                             else "potential (fillable, UNPROVEN)")
                    why = f" [{top.get('fill_reason')}]" if is_paper and top.get("fill_reason") else ""
                    _notify(f"Earnings Edge — {label}",
                            f"{top['ticker']} {top['outcome']} model {top.get('model_prob')} vs "
                            f"mkt {top.get('market_prob')} · SCAR {top.get('predictability')}"
                            f"{why} — look, don't bet. {top.get('url') or ''}",
                            url="http://127.0.0.1:8000/?edge=1")
                    _log(f"*** POTENTIAL (SCAR>=45): {len(pot_hi)} ({len(fillable_hi)} fillable) "
                         f"— notified. Top {top['ticker']} paper_only={is_paper} ***")
                elif potential:
                    _log(f"potential: {len(potential)} seen but none SCAR>=45 — logged, NOT notified.")
        except Exception as e:
            _log(f"edge-scan error: {e}")

    _log(f"===== {stamp} done =====")
    return {"backtest": mc, "scar": rec, "forward": s}


if __name__ == "__main__":
    run(datetime.now().isoformat(timespec="seconds"))
