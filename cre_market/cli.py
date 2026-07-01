"""Command-line interface.

Examples:

    # single address, government sources only
    cre-market --address "123 Main St, Houston, TX 77002" -o houston.xlsx

    # with CoStar exports overriding rent + existing home sales
    cre-market --address "123 Main St, Houston, TX 77002" \\
        --costar-rent rent_export.csv --costar-home-sales sales_export.xlsx \\
        -o houston.xlsx --plot

    # batch: CSV with columns address[,costar_rent,costar_home_sales,costar_new_home_sales]
    cre-market --input properties.csv -o portfolio.xlsx
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys

from . import config
from .cache import CachedSession
from .pipeline import run_address
from .report import write_workbook

log = logging.getLogger("cre_market")

COSTAR_KINDS = ("rent", "home_sales", "new_home_sales")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cre-market",
        description="CRE market research: government-data-first metrics by radius.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    src = p.add_argument_group("input")
    src.add_argument("--address", action="append", default=[],
                     help="property address (repeatable)")
    src.add_argument("--input", metavar="CSV",
                     help="batch CSV: address[,costar_rent,costar_home_sales,"
                          "costar_new_home_sales] (file-path columns optional)")
    for kind in COSTAR_KINDS:
        src.add_argument(f"--costar-{kind.replace('_', '-')}", metavar="FILE",
                         dest=f"costar_{kind}",
                         help=f"CoStar {kind.replace('_', ' ')} export "
                              "(single --address runs only)")
    out = p.add_argument_group("output")
    out.add_argument("-o", "--output", default="market_research.xlsx",
                     help="output Excel workbook")
    out.add_argument("--plot", action="store_true",
                     help="save a radius/block-group validation map PNG per address")
    opts = p.add_argument_group("options")
    opts.add_argument("--radii", default="30,15,5",
                      help="comma-separated radii in miles")
    opts.add_argument("--lookback", type=int, default=config.DEFAULT_LOOKBACK_YEARS,
                      help="growth lookback in years")
    opts.add_argument("--acs-year", type=int, default=None,
                      help="ACS 5-year vintage (default: newest available)")
    opts.add_argument("--cache-dir", default=config.DEFAULT_CACHE_DIR)
    opts.add_argument("--no-cache", action="store_true",
                      help="bypass the local API response cache")
    opts.add_argument("-v", "--verbose", action="store_true")
    return p


def load_batch(path: str) -> list[dict]:
    jobs = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in reader.fieldnames or []}
        addr_col = cols.get("address") or (reader.fieldnames or [None])[0]
        if not addr_col:
            raise SystemExit(f"{path}: no columns found")
        base_dir = os.path.dirname(os.path.abspath(path))
        for row in reader:
            address = (row.get(addr_col) or "").strip()
            if not address:
                continue
            costar = {}
            for kind in COSTAR_KINDS:
                col = cols.get(f"costar_{kind}")
                val = (row.get(col) or "").strip() if col else ""
                if val:
                    costar[kind] = val if os.path.isabs(val) else os.path.join(base_dir, val)
            jobs.append({"address": address, "costar": costar})
    return jobs


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")

    jobs: list[dict] = []
    if args.input:
        jobs.extend(load_batch(args.input))
    for addr in args.address:
        costar = {k: getattr(args, f"costar_{k}") for k in COSTAR_KINDS
                  if getattr(args, f"costar_{k}")}
        jobs.append({"address": addr, "costar": costar})
    if not jobs:
        print("error: provide --address or --input", file=sys.stderr)
        return 2
    if len(args.address) > 1 and any(getattr(args, f"costar_{k}") for k in COSTAR_KINDS):
        print("error: --costar-* flags only work with a single --address; "
              "use --input CSV for batches", file=sys.stderr)
        return 2

    radii = tuple(float(r) for r in args.radii.split(","))
    census_key = config.census_api_key()
    hud_key = config.hud_api_key()
    if not census_key:
        log.warning("CENSUS_API_KEY not set: ACS metrics (1-5) will be skipped. "
                    "Register free at https://api.census.gov/data/key_signup.html")
    if not hud_key:
        log.warning("HUD_API_KEY not set: HUD FMR rent metric will be skipped "
                    "unless a CoStar rent export is provided. Register free at "
                    "https://www.huduser.gov/portal/dataset/fmr-api.html")

    session = CachedSession(cache_dir=args.cache_dir, enabled=not args.no_cache)
    results = []
    for job in jobs:
        log.info("processing: %s", job["address"])
        res = run_address(session, job["address"], radii, args.lookback,
                          census_key, hud_key, args.acs_year,
                          costar_files=job["costar"])
        results.append(res)
        if args.plot and not res.error and res.block_groups_current:
            from .plotting import plot_address
            safe = "".join(ch if ch.isalnum() else "_" for ch in job["address"])[:60]
            png = os.path.splitext(args.output)[0] + f"_{safe}_map.png"
            plot_address(res, radii, png)
            log.info("wrote map: %s", png)

    write_workbook(args.output, results, radii, args.lookback)
    log.info("wrote workbook: %s", args.output)
    n_err = sum(1 for r in results if r.error)
    if n_err:
        log.warning("%d of %d addresses failed; see Summary sheet", n_err, len(results))
    return 0 if n_err < len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
