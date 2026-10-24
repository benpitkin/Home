"""Download half-hourly import and export readings from the Octopus Energy REST API."""

import os

import pandas as pd
import requests
from dotenv import load_dotenv

API = "https://api.octopus.energy/v1"
UK = "Europe/London"
PAGE_SIZE = 25000  # API maximum; a year of half-hours (~17,520) usually fits in one page


def _env(name, default=None):
    value = os.getenv(name, default)
    if not value:
        raise SystemExit(f"Missing {name} in .env (see .env.example)")
    return value.strip()


def _fetch_meter(session, mpan, serial, period_from, period_to):
    """Return a Series of kWh indexed by UTC interval start, following pagination."""
    url = f"{API}/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
    params = {
        "period_from": period_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period_to": period_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page_size": PAGE_SIZE,
        "order_by": "period",
    }
    rows = []
    while url:
        resp = session.get(url, params=params, timeout=60)
        if resp.status_code == 401:
            raise SystemExit("Octopus API rejected the key (401). Check OCTOPUS_API_KEY.")
        if resp.status_code == 404:
            raise SystemExit(f"Meter not found (404) for MPAN {mpan} / serial {serial}.")
        resp.raise_for_status()
        body = resp.json()
        rows.extend(body["results"])
        url = body.get("next")
        params = None  # the "next" URL already carries the query string

    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    # Octopus returns timestamps with mixed offsets (Z in winter, +01:00 in summer);
    # normalising to UTC first makes every interval unique and unambiguous.
    idx = pd.to_datetime(df["interval_start"], utc=True)
    s = pd.Series(df["consumption"].astype(float).values, index=idx)
    return s[~s.index.duplicated(keep="last")].sort_index()


def fetch(out_path, months=12):
    load_dotenv()
    key = _env("OCTOPUS_API_KEY")
    import_mpan = _env("IMPORT_MPAN")
    export_mpan = _env("EXPORT_MPAN")
    serial = _env("METER_SERIAL")
    export_serial = os.getenv("EXPORT_METER_SERIAL", "").strip() or serial

    # Whole UK days: from local midnight N months ago up to local midnight today.
    today = pd.Timestamp.now(tz=UK).normalize()
    start = today - pd.DateOffset(months=months)
    period_from = start.tz_convert("UTC").to_pydatetime()
    period_to = today.tz_convert("UTC").to_pydatetime()

    session = requests.Session()
    session.auth = (key, "")
    print(f"Fetching import  {import_mpan} / {serial} ...")
    imp = _fetch_meter(session, import_mpan, serial, period_from, period_to)
    print(f"Fetching export  {export_mpan} / {export_serial} ...")
    exp = _fetch_meter(session, export_mpan, export_serial, period_from, period_to)
    if imp.empty:
        raise SystemExit("No import data returned. Is the meter sending half-hourly readings?")

    df = pd.DataFrame({"import_kwh": imp, "export_kwh": exp})
    df.index.name = "interval_start"
    df["export_kwh"] = df["export_kwh"].fillna(0.0)
    df = df.dropna(subset=["import_kwh"])

    # Store in UK local time with its offset (e.g. 2025-10-26T01:00:00+01:00 then
    # 2025-10-26T01:00:00+00:00), which keeps the repeated hour distinct.
    df.index = df.index.tz_convert(UK)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.index = out.index.map(lambda ts: ts.isoformat()).rename("interval_start")
    out.to_csv(out_path)
    print(f"Saved {len(df):,} half-hours to {out_path}")
    return df


def load(path):
    df = pd.read_csv(path)
    df["interval_start"] = pd.to_datetime(df["interval_start"], utc=True).dt.tz_convert(UK)
    return df.set_index("interval_start").sort_index()
