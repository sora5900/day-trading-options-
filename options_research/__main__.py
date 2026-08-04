"""CLI. Research mode only — there is no order-placement command and none
may be added (spec §1 hard rule).

  python -m options_research init                 create the database
  python -m options_research collect --once      one snapshot cycle now
  python -m options_research run                 market-hours loop:
                                                 collect → features → detect → grade
  python -m options_research shadow --at TS      run one shadow cycle at a
                                                 historical timestamp (backfill)
  python -m options_research crosscheck          compare sources on ATM contracts
  python -m options_research freeze-split DATE   freeze the train/test cutoff
  python -m options_research validate            assign splits + verdicts
  python -m options_research report              the shadow ledger report
"""

import argparse
import logging
import sys

from . import db, market_calendar as cal
from .config import Config


def _sources(cfg, need_cross=False):
    from .sources.polygon import PolygonSource
    from .sources.tradier import TradierSource
    primary = cross = None
    errs = []
    if cfg.polygon_api_key:
        primary = PolygonSource(cfg.polygon_api_key)
    else:
        errs.append("POLYGON_API_KEY not set")
    if cfg.tradier_token:
        t = TradierSource(cfg.tradier_token, cfg.tradier_base)
        if primary is None:
            primary = t
        else:
            cross = t
    else:
        errs.append("TRADIER_TOKEN not set")
    if primary is None:
        sys.exit("no data source configured: " + "; ".join(errs))
    if need_cross and cross is None:
        sys.exit("cross-check needs BOTH sources configured: " + "; ".join(errs))
    return primary, cross


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="options_research")
    ap.add_argument("--db", default=None, help="database path override")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    p = sub.add_parser("probe")
    p.add_argument("--source", choices=("polygon", "tradier", "yahoo"),
                   default="polygon")
    p.add_argument("--sample", action="store_true",
                   help="dump one RAW contract from the chain snapshot, to "
                        "see exactly which fields the plan returns")
    p = sub.add_parser("collect")
    p.add_argument("--once", action="store_true")
    sub.add_parser("run")
    p = sub.add_parser("shadow")
    p.add_argument("--at", required=True,
                   help="decision timestamp, e.g. 2026-07-30T14:35:00Z")
    sub.add_parser("crosscheck")
    p = sub.add_parser("freeze-split")
    p.add_argument("cutoff", help="first TEST day, YYYY-MM-DD (frozen forever)")
    sub.add_parser("validate")
    p = sub.add_parser("report")
    p.add_argument("--trades", action="store_true", help="recent trades table")
    p = sub.add_parser("backfill", help="historical 1-min bars (free tier)")
    p.add_argument("--symbols", default="SPY,QQQ")
    p.add_argument("--years", type=float, default=2.0)
    p.add_argument("--rate", type=int, default=5,
                   help="requests per minute allowed by the plan")
    p.add_argument("--discover", action="store_true",
                   help="probe how far back this plan actually serves")

    p = sub.add_parser("momentum", help="H3-P1 stage 1 (+ stage 2 hurdle)")
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--option-friction", type=float, default=None,
                   help="round-trip option friction as a fraction of premium; "
                        "omit to use C2's measured value if available")
    p.add_argument("--leverage", type=float, default=20.0)

    p = sub.add_parser("freshness",
                       help="measure whether a source's quotes are live and "
                            "how far behind they are")
    p.add_argument("--source", choices=("yahoo", "tradier", "polygon"),
                   default="yahoo")
    p.add_argument("--seconds", type=int, default=120)
    p.add_argument("--symbol", default="SPY")

    sub.add_parser("manifest")
    sub.add_parser("controls")
    p = sub.add_parser("replay")
    p.add_argument("trade_id", type=int)

    args = ap.parse_args()
    cfg = Config()
    if args.db:
        cfg.db_path = args.db
    conn = db.connect(cfg.db_path)

    if args.cmd == "init":
        print(f"database ready at {cfg.db_path}")

    elif args.cmd == "probe":
        from .collector import Collector
        from .sources.polygon import PolygonSource
        from .sources.tradier import TradierSource
        if args.source == "yahoo":
            from .sources.yahoo import YahooSource, YahooError
            try:
                src = YahooSource()
            except YahooError as e:
                sys.exit(str(e))
        elif args.source == "polygon":
            if not cfg.polygon_api_key:
                sys.exit("POLYGON_API_KEY is not set")
            src = PolygonSource(cfg.polygon_api_key)
        else:
            if not cfg.tradier_token:
                sys.exit("TRADIER_TOKEN is not set")
            src = TradierSource(cfg.tradier_token, cfg.tradier_base)
        if args.sample:
            import json as _json
            from .sources.polygon import PolygonError
            print("RAW chain-snapshot contract, exactly as the API returned "
                  "it.\nUse this to see which blocks the plan populates "
                  "(last_quote, greeks, day, ...).\n" + "=" * 74)
            try:
                j = src._get("/v3/snapshot/options/SPY", {"limit": 3})
                results = j.get("results") or []
                if not results:
                    print("no results returned")
                for c in results[:2]:
                    print(_json.dumps(c, indent=2)[:2500])
                    print("-" * 74)
                print("top-level keys present on contract 1:",
                      sorted(results[0].keys()) if results else [])
            except PolygonError as e:
                print(f"HTTP {e.status_code}: {e.body[:400]}")
            print("=" * 74)
            print("\nAlso testing the standalone options quote endpoints:")
            for path, label in (
                    ("/v3/quotes/O:SPY260807C00760000", "historical NBBO"),
                    ("/v2/last/nbbo/O:SPY260807C00760000", "last NBBO")):
                try:
                    r = src._get(path, {"limit": 1})
                    print(f"  {label:18s} OK  keys={sorted(r.keys())}")
                except PolygonError as e:
                    print(f"  {label:18s} HTTP {e.status_code} "
                          f"{e.body[:160]}")
            return

        report = Collector(conn, src, cfg).probe()
        print(report.render())
        xsp_chain = report.by_id("xsp_options")
        xsp_under = report.by_id("xsp_underlying")
        arm_b = (xsp_chain and xsp_chain.status.value == "PASS"
                 and xsp_under and xsp_under.status.value == "PASS")
        print(f"\nH1 arm B (XSP cash-settled comparator): "
              f"{'AVAILABLE' if arm_b else 'NOT AVAILABLE — H1 runs SPY-only'}")
        sys.exit(0 if report.can_collect else 2)

    elif args.cmd == "collect":
        from .collector import Collector
        primary, cross = _sources(cfg)
        c = Collector(conn, primary, cfg, cross_source=cross)
        c.require_capability()
        if args.once:
            for sym in cfg.symbols:
                c.snap_underlying(sym)
                print(f"{sym}: {c.snap_chain(sym)} contracts")
        else:
            c.run_loop()

    elif args.cmd == "run":
        from .collector import Collector
        from .shadow import run_cycle
        primary, cross = _sources(cfg)
        c = Collector(conn, primary, cfg, cross_source=cross)
        c.require_capability()
        print("shadow research loop — paper trades only, no orders exist here")
        c.run_loop(on_cycle=lambda ts: run_cycle(conn, ts, cfg))

    elif args.cmd == "shadow":
        from .shadow import run_cycle
        run_cycle(conn, args.at, cfg)
        print(f"shadow cycle done at {args.at}")

    elif args.cmd == "crosscheck":
        from .collector import Collector
        primary, cross = _sources(cfg, need_cross=True)
        c = Collector(conn, primary, cfg, cross_source=cross)
        for sym in cfg.symbols:
            c.cross_check(sym)
        print("cross-check complete; divergences (if any) are in data_quality")

    elif args.cmd == "freeze-split":
        from .validator import assign_splits
        res = assign_splits(conn, args.cutoff)
        print(f"split frozen at {res['cutoff']}: "
              f"+{res['assigned_train']} train, +{res['assigned_test']} test rows")

    elif args.cmd == "backfill":
        from datetime import date, timedelta
        from . import backfill as bf
        from .sources.polygon import PolygonSource
        if not cfg.polygon_api_key:
            sys.exit("POLYGON_API_KEY is not set")
        src = PolygonSource(cfg.polygon_api_key)
        src.resolve_base()
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        limiter = bf.RateLimiter(args.rate)
        if args.discover:
            for sym in symbols:
                earliest = bf.discover_lookback(src, sym, limiter)
                print(f"{sym}: earliest 1-min bars available ~ {earliest}")
            return
        end = date.today()
        start = end - timedelta(days=int(365 * args.years))
        print(f"backfilling {symbols} {start} .. {end} at {args.rate} req/min "
              f"(~1 call per month of history)")
        totals = bf.run(conn, src, symbols, start, end, args.rate)
        for sym in symbols:
            print(f"  {sym}: {totals.get(sym, 0):,} bars stored — "
                  f"{bf.coverage(conn, sym)}")

    elif args.cmd == "momentum":
        from .analysis import intraday_momentum as im
        s1 = im.run(conn, args.symbol)
        friction = args.option_friction
        if friction is None:
            from .validator import measured_friction
            fr = measured_friction(conn)
            if fr and fr.get("avg_spread_pct"):
                # round trip: spread paid on entry and again on exit
                friction = fr["avg_spread_pct"] * 2
        s2 = (im.stage2_hurdle(s1, friction, args.leverage)
              if (friction and "error" not in s1) else None)
        print(im.render(s1, s2))
        if friction is None and "error" not in s1:
            print("\n(stage 2 skipped: no measured option friction yet. "
                  "Re-run with --option-friction 0.15 to test a 15% "
                  "round-trip assumption, or wait for C2 to measure it.)")

    elif args.cmd == "freshness":
        from . import freshness as fr
        if args.source == "yahoo":
            from .sources.yahoo import YahooSource
            src = YahooSource()
        elif args.source == "tradier":
            from .sources.tradier import TradierSource
            src = TradierSource(cfg.tradier_token, cfg.tradier_base)
        else:
            from .sources.polygon import PolygonSource
            src = PolygonSource(cfg.polygon_api_key)
        u = src.get_underlying(args.symbol)
        if not u or not u.get("last"):
            sys.exit("source returned no underlying price")
        print(f"sampling {args.symbol} twice, {args.seconds}s apart "
              f"(this will pause)...")
        mv = fr.quote_movement(src, args.symbol, u["last"], args.seconds)
        dl = fr.estimate_delay_vs_reference(conn, src, args.symbol)
        print(fr.render(mv, dl))

    elif args.cmd == "manifest":
        from .reporter import manifest_report
        print(manifest_report(conn))

    elif args.cmd == "controls":
        from .validator import check_controls, measured_friction, PipelineHalt
        try:
            rep = check_controls(conn)
        except PipelineHalt as e:
            print("PIPELINE HALT — controls read anomalously:")
            print(f"  {e}")
            sys.exit(3)
        print(f"C1 leakage control: {rep['c1']}")
        print(f"C2 cost control:    {rep['c2']}")
        print(f"measured friction:  {measured_friction(conn)}")
        print("controls healthy")

    elif args.cmd == "replay":
        from .journal import replay, render_replay
        print(render_replay(replay(conn, args.trade_id)))

    elif args.cmd == "validate":
        from .validator import (assign_splits, evaluate_all, check_controls,
                                SplitFrozenError, PipelineHalt)
        try:
            assign_splits(conn)
        except (ValueError, SplitFrozenError) as e:
            sys.exit(str(e))
        # controls gate every verdict: if the instruments are broken, the
        # measurements they produced are not trustworthy
        try:
            check_controls(conn)
        except PipelineHalt as e:
            print("PIPELINE HALT — no verdicts will be issued.")
            sys.exit(f"  {e}")
        for r in evaluate_all(conn):
            t = r["test"]
            print(f"{r['hypothesis']}@{r['version']} "
                  f"[{r['params_hash']}/{r['data_era']}]: {r['verdict']} — "
                  f"test n={t['n']}, EV/trade ${t['ev_per_trade']}, "
                  f"CI [{t['ci_lo']}, {t['ci_hi']}]; {r['notes']}")

    elif args.cmd == "report":
        from .reporter import build_report, recent_trades
        print(build_report(conn))
        if args.trades:
            print()
            print(recent_trades(conn))


if __name__ == "__main__":
    main()
