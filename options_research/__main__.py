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

    args = ap.parse_args()
    cfg = Config()
    if args.db:
        cfg.db_path = args.db
    conn = db.connect(cfg.db_path)

    if args.cmd == "init":
        print(f"database ready at {cfg.db_path}")

    elif args.cmd == "collect":
        from .collector import Collector
        primary, cross = _sources(cfg)
        c = Collector(conn, primary, cfg, cross_source=cross)
        if args.once:
            ts = cal.utcnow_iso()
            for sym in cfg.symbols:
                c.snap_underlying(sym, ts)
                n = c.snap_chain(sym, ts)
                print(f"{sym}: {n} contracts @ {ts}")
        else:
            c.run_loop()

    elif args.cmd == "run":
        from .collector import Collector
        from .shadow import run_cycle
        primary, cross = _sources(cfg)
        c = Collector(conn, primary, cfg, cross_source=cross)
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

    elif args.cmd == "validate":
        from .validator import assign_splits, evaluate_all, SplitFrozenError
        try:
            assign_splits(conn)
        except (ValueError, SplitFrozenError) as e:
            sys.exit(str(e))
        for r in evaluate_all(conn):
            t = r["test"]
            print(f"{r['strategy']} {r['version']}: {r['verdict']} — "
                  f"test n={t['n']}, net ${t['pnl']}; {r['notes']}")

    elif args.cmd == "report":
        from .reporter import build_report, recent_trades
        print(build_report(conn))
        if args.trades:
            print()
            print(recent_trades(conn))


if __name__ == "__main__":
    main()
