"""Fetch Octopus data and work out whether an extra battery module pays for itself.

    python run.py            # fetch the last 12 months, then analyse
    python run.py --no-fetch # re-analyse the saved data/octopus_halfhourly.csv
"""

import argparse
import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv

from battery import analyse, fetch, report, sigen

ROOT = Path(__file__).parent
DATA = ROOT / "data" / "octopus_halfhourly.csv"
SIGEN_DATA = ROOT / "data" / "sigen_5min.csv"
SIGEN_CACHE = ROOT / "data" / "sigen"
OUT = ROOT / "output"


def add_battery_data(hh, fetch_new):
    """Join Sigen battery history if a login is configured or a saved copy exists."""
    load_dotenv()
    if fetch_new and os.getenv("SIGEN_USERNAME"):
        days = sorted({f"{d:%Y-%m-%d}" for d in hh.index.tz_localize(None).normalize()})
        sig = sigen.fetch(days, SIGEN_CACHE, SIGEN_DATA)
    elif SIGEN_DATA.exists():
        sig = sigen.load(SIGEN_DATA)
    else:
        return hh

    batt = sigen.to_halfhours(sig, hh.index)
    out = hh.join(batt)
    ratio, corr = sigen.check_alignment(out)
    if ratio is not None and ratio < 0.01:  # Sigen reported watts, not kilowatts
        for col in ("discharge_kwh", "sigen_from_grid_kwh"):
            out[col] /= 1000
        ratio, corr = sigen.check_alignment(out)
    if ratio is not None:
        print(f"Sigen vs Octopus grid import: ratio {ratio:.2f}, correlation {corr:.2f} (expect both near 1)")
        if corr < 0.7:
            print("  Warning: Sigen data doesn't line up well with the meter; battery-based results may be off.")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-fetch", action="store_true", help="use the saved CSV instead of calling the API")
    parser.add_argument("--config", default=ROOT / "config.toml", type=Path)
    args = parser.parse_args()

    cfg = tomllib.loads(args.config.read_text())
    if not cfg["battery"].get("module_cost_gbp"):  # fail fast, before spending time on the API
        raise SystemExit("Set module_cost_gbp in config.toml first.")

    if args.no_fetch:
        if not DATA.exists():
            raise SystemExit(f"{DATA} not found; run without --no-fetch first.")
        hh = fetch.load(DATA)
    else:
        hh = fetch.fetch(DATA, months=cfg["analysis"]["months"])

    analyse.check_config(cfg, hh)
    hh = add_battery_data(hh, fetch_new=not args.no_fetch)
    hh = analyse.tag_halfhours(hh, cfg)
    daily = analyse.daily_totals(hh, cfg)
    # Drop part days at either end (e.g. a cycle that starts at 23:30 on the last day).
    expected = daily["halfhours"].median()
    partial = daily["halfhours"] < expected - 2
    if partial.any():
        print(f"Skipping {int(partial.sum())} incomplete day(s) (missing half-hours).")
    daily = daily[~partial]
    # Days on a single-rate tariff have no peak window, so there's nothing to shift.
    # Leaving them in would dilute the annual figures, so analyse time-of-use days only.
    flat = daily["offpeak_hours"] * 2 >= daily["halfhours"]
    if flat.any() and not flat.all():
        print(f"Skipping {int(flat.sum())} day(s) on a single-rate tariff "
              f"(up to {daily.index[flat].max():%d %b %Y}); annual figures are scaled from the rest.")
        daily = daily[~flat]

    sim, value_grid, value_solar = analyse.simulate(daily, cfg)
    monthly = analyse.monthly_table(sim, cfg)
    annual = analyse.annual_results(sim, cfg)
    lines = report.verdict(sim, annual, cfg, value_grid, value_solar)

    OUT.mkdir(exist_ok=True)
    capped = hh[hh["import_kwh"] > cfg["analysis"]["capped_halfhour_kwh"]]
    capped[["import_kwh", "export_kwh", "is_offpeak"]].to_csv(OUT / "capped_halfhours.csv")
    sim.to_csv(OUT / "daily_analysis.csv")
    monthly.to_csv(OUT / "monthly_summary.csv", float_format="%.3f")
    annual.to_csv(OUT / "annual_summary.csv", float_format="%.2f")
    report.chart(sim, cfg, OUT / "daily_peak_import.png")
    windows = analyse.offpeak_windows(hh)
    report.dashboard(sim, annual, lines, cfg, windows, OUT / "dashboard.html")
    summary = report.format_summary(monthly, annual, lines, cfg, sim, windows)
    (OUT / "summary.txt").write_text(summary + "\n")

    print()
    print(summary)
    print(f"\nFiles written to {OUT}/ — open {OUT / 'dashboard.html'} in a browser to explore.")


if __name__ == "__main__":
    main()
