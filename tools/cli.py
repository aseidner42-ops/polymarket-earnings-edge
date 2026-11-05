"""Command-line entrypoint for the Polymarket Earnings Edge tool.

Usage (run from the repo root):
  python -m tools.cli predict AAPL --outcome eps_beat -p <polymarket_url> --save
  python -m tools.cli predict AAPL MSFT --outcome revenue_beat --threshold 3
  python -m tools.cli backtest AAPL MSFT NVDA --quarters 8
  python -m tools.cli check        # verify env + connectivity
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow `python tools/cli.py ...` as well as `python -m tools.cli`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUTCOME_HELP = (
    "eps_beat | eps_surprise_over | revenue_beat | revenue_over | stock_up. "
    "Thresholds: eps_beat/eps_surprise_over=% , revenue_beat=% , revenue_over=$ amount."
)


def _cmd_predict(args) -> int:
    from tools import edge_log, predict, report
    results = []
    for tk in args.tickers:
        pm = args.polymarket if len(args.tickers) == 1 else None
        snap = args.snapshot if len(args.tickers) == 1 else None
        res = predict.predict(tk, outcome=args.outcome,
                              threshold=args.threshold, polymarket_url=pm,
                              snapshot_path=snap)
        results.append(res)
        if args.log and "error" not in res:
            edge_log.log_edge(res)
    report.render(results)
    if args.save:
        paths = report.save(results)
        print(f"\nSaved: {paths['csv']}\n       {paths['md']}")
    return 0


def _cmd_backtest(args) -> int:
    from tools.backtest import backtest, write_report
    tickers = args.tickers or None  # None => diverse default basket
    result = backtest(tickers, max_quarters=args.quarters)
    mc, mk = result["metrics_current"], result["metrics_calibrated"]
    if not mc.get("n"):
        print("No usable historical quarters.")
        return 1
    print(json.dumps({
        "events": mc["n"],
        "current": {k: mc[k] for k in ("accuracy", "precision", "recall",
                                       "false_positives", "false_negatives", "brier")},
        "calibrated": {k: mk.get(k) for k in ("accuracy", "brier")},
        "baseline": {"accuracy": mc["baseline_always_beat_accuracy"],
                     "brier": mc["brier_baseline"]},
        "beats_baseline_brier": mk.get("beats_baseline_brier"),
        "signal_analysis": result["signal_analysis"],
        "recommended_weights": result["recommended_weights"],
        "recommended_logistic": result["recommended_logistic"],
    }, indent=2))
    if args.report:
        path = write_report(result, args.quarters)
        print(f"\nReport: {path}")
    return 0


def _cmd_forward(_args) -> int:
    from tools.forward_test import score_log
    print(json.dumps(score_log(), indent=2))
    return 0


def _cmd_scan(args) -> int:
    from tools.scanner import scan, write_landscape
    from datetime import datetime
    result = scan(limit=args.limit)
    stamp = datetime.now().isoformat(timespec="seconds")
    write_landscape(result, stamp)  # weekly landscape history + EDGE_FOUND flag
    n_trade = result.get("tradeable", 0)
    print(f"[{stamp}] scanned {result.get('scanned',0)} markets, {n_trade} tradeable")
    if n_trade:
        print(f"*** TRADEABLE EDGE FOUND ({n_trade}) — see output/EDGE_FOUND.txt ***")
    print(json.dumps(result, indent=2))
    return 0


def _cmd_reaction(args) -> int:
    from tools.backtest import reaction_backtest
    r = reaction_backtest(args.tickers or None)
    r.pop("events", None)
    print(json.dumps(r, indent=2))
    return 0


def _cmd_serve(args) -> int:
    from tools.webapp import serve
    serve(host=args.host, port=args.port)
    return 0


def _cmd_check(_args) -> int:
    from tools.config import get_finnhub_key
    from tools import finnhub_client as fh
    try:
        get_finnhub_key()
        print("FINNHUB_API_KEY: found")
    except Exception as e:
        print(f"FINNHUB_API_KEY: MISSING ({e})")
        return 1
    sym = fh.resolve_symbol("AAPL")
    print(f"Finnhub search: {'ok -> ' + sym['symbol'] if sym else 'FAILED'}")
    return 0 if sym else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="polymarket-earnings-edge",
                                description="Estimate earnings-beat probability and compare to Polymarket.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("predict", help="predict one outcome for one or more tickers")
    pp.add_argument("tickers", nargs="+", help="ticker(s) or company name(s)")
    pp.add_argument("--outcome", default="eps_beat", help=OUTCOME_HELP)
    pp.add_argument("--threshold", type=float, default=None,
                    help="user threshold (% or $ depending on outcome)")
    pp.add_argument("-p", "--polymarket", default=None,
                    help="Polymarket market URL or slug (single-ticker runs only)")
    pp.add_argument("--snapshot", default=None,
                    help="TradingView OHLCV CSV for the Kelly over-extrapolation signal "
                         "(single-ticker runs only)")
    pp.add_argument("--snapshot-timeframe", default=None,
                    help="snapshot lookback: days/weeks/years, e.g. 5y, 180d, 52w "
                         "(informational; guides how the snapshot was pulled)")
    pp.add_argument("--save", action="store_true", help="save CSV + markdown to output/")
    pp.add_argument("--log", action="store_true", help="append edge to output/edge_log.csv")
    pp.set_defaults(func=_cmd_predict)

    pb = sub.add_parser("backtest", help="backtest + calibrate the beat model")
    pb.add_argument("tickers", nargs="*", help="tickers (omit = diverse default basket)")
    pb.add_argument("--quarters", type=int, default=8, help="max quarters per ticker")
    pb.add_argument("--report", action="store_true", help="write markdown report to output/")
    pb.set_defaults(func=_cmd_backtest)

    pc = sub.add_parser("check", help="verify env + API connectivity")
    pc.set_defaults(func=_cmd_check)

    pf = sub.add_parser("forward", help="score logged predictions after earnings resolve")
    pf.set_defaults(func=_cmd_forward)

    psc = sub.add_parser("scan", help="scan live Polymarket markets, rank by edge, auto-log")
    psc.add_argument("--limit", type=int, default=12)
    psc.set_defaults(func=_cmd_scan)

    prx = sub.add_parser("reaction", help="validate the post-earnings move model (Q3)")
    prx.add_argument("tickers", nargs="*")
    prx.set_defaults(func=_cmd_reaction)

    ps = sub.add_parser("serve", help="launch the local web platform")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8000)
    ps.set_defaults(func=_cmd_serve)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
