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


def _get_all(session, url, params):
    """GET a paginated Octopus endpoint and return every result."""
    rows = []
    while url:
        resp = session.get(url, params=params, timeout=60)
        if resp.status_code == 401:
            raise SystemExit("Octopus API rejected the key (401). Check OCTOPUS_API_KEY.")
        if resp.status_code == 404:
            raise SystemExit(f"Not found (404): {url}")
        resp.raise_for_status()
        body = resp.json()
        rows.extend(body["results"])
        url = body.get("next")
        params = None  # the "next" URL already carries the query string
    return rows


def _iso(ts):
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_meter(session, mpan, serials, period_from, period_to):
    """Return a Series of kWh indexed by UTC interval start.

    Several serials are combined, which covers a meter that was swapped during the year.
    """
    params = {"period_from": _iso(period_from), "period_to": _iso(period_to),
              "page_size": PAGE_SIZE, "order_by": "period"}
    rows = []
    for serial in serials:
        url = f"{API}/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
        rows.extend(_get_all(session, url, dict(params)))

    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    # Octopus returns timestamps with mixed offsets (Z in winter, +01:00 in summer);
    # normalising to UTC first makes every interval unique and unambiguous.
    idx = pd.to_datetime(df["interval_start"], utc=True)
    s = pd.Series(df["consumption"].astype(float).values, index=idx)
    return s[~s.index.duplicated(keep="last")].sort_index()


def _account_meters(session, account):
    """Find import and export meter points (MPAN, serials, agreements) on the account."""
    body = session.get(f"{API}/accounts/{account}/", timeout=60)
    if body.status_code in (401, 403, 404):
        raise SystemExit(f"Could not read account {account} ({body.status_code}). "
                         "Check ACCOUNT_NUMBER (A-XXXXXXXX) and OCTOPUS_API_KEY.")
    body.raise_for_status()
    props = body.json()["properties"]
    # Prefer the property you still live at.
    props = sorted(props, key=lambda p: p.get("moved_out_at") is None, reverse=True)
    found = {}
    for point in props[0]["electricity_meter_points"]:
        kind = "export" if point.get("is_export") else "import"
        found.setdefault(kind, {
            "mpan": point["mpan"],
            "serials": [m["serial_number"] for m in point.get("meters", []) if m.get("serial_number")],
            "agreements": point.get("agreements", []),
        })
    return found


def _rates(session, agreements, index):
    """Unit rate (p/kWh inc VAT) for every half-hour in `index`, from the tariff agreements."""
    rate = pd.Series(float("nan"), index=index)
    for ag in agreements:
        code = ag["tariff_code"]
        ag_from = pd.Timestamp(ag["valid_from"]).tz_convert("UTC")
        ag_to = pd.Timestamp(ag["valid_to"]).tz_convert("UTC") if ag.get("valid_to") else index.max() + pd.Timedelta("30min")
        if ag_to <= index.min() or ag_from > index.max():
            continue
        # E-1R-INTELLI-VAR-24-10-14-A -> product INTELLI-VAR-24-10-14
        product = "-".join(code.split("-")[2:-1])
        url = f"{API}/products/{product}/electricity-tariffs/{code}/standard-unit-rates/"
        rows = _get_all(session, url, {"period_from": _iso(max(ag_from, index.min())),
                                       "period_to": _iso(min(ag_to, index.max() + pd.Timedelta("30min"))),
                                       "page_size": 1500})
        if not rows:
            print(f"  No rates published for {code}")
            continue
        r = pd.DataFrame(rows)
        if "payment_method" in r and (r["payment_method"] == "DIRECT_DEBIT").any():
            r = r[r["payment_method"].isin(["DIRECT_DEBIT"]) | r["payment_method"].isna()]
        r["valid_from"] = pd.to_datetime(r["valid_from"], utc=True)
        r["valid_to"] = pd.to_datetime(r["valid_to"], utc=True)
        r = r.sort_values("valid_from")
        hh = pd.DataFrame({"t": index})
        m = pd.merge_asof(hh, r[["valid_from", "valid_to", "value_inc_vat"]],
                          left_on="t", right_on="valid_from")
        ok = (m["t"] >= ag_from) & (m["t"] < ag_to) & (m["valid_to"].isna() | (m["t"] < m["valid_to"]))
        rate[ok.values] = m.loc[ok, "value_inc_vat"].values
        print(f"  Rates for {code}: {ok.sum():,} half-hours")
    return rate


def fetch(out_path, months=12):
    load_dotenv()
    session = requests.Session()
    session.auth = (_env("OCTOPUS_API_KEY"), "")
    account = os.getenv("ACCOUNT_NUMBER", "").strip()

    meters = _account_meters(session, account) if account else {}
    if account and "import" in meters:
        print(f"Account {account}: import MPAN {meters['import']['mpan']}"
              + (f", export MPAN {meters['export']['mpan']}" if "export" in meters else ", no export MPAN"))
    imp_m = meters.get("import", {})
    exp_m = meters.get("export", {})
    # Values in .env override what the account reports.
    import_mpan = os.getenv("IMPORT_MPAN", "").strip() or imp_m.get("mpan") or _env("IMPORT_MPAN")
    export_mpan = os.getenv("EXPORT_MPAN", "").strip() or exp_m.get("mpan") or _env("EXPORT_MPAN")
    serial = os.getenv("METER_SERIAL", "").strip()
    import_serials = [serial] if serial else imp_m.get("serials") or [_env("METER_SERIAL")]
    export_serial = os.getenv("EXPORT_METER_SERIAL", "").strip() or serial
    export_serials = [export_serial] if export_serial else exp_m.get("serials") or import_serials

    # Whole UK days: from local midnight N months ago up to local midnight today.
    today = pd.Timestamp.now(tz=UK).normalize()
    start = today - pd.DateOffset(months=months)

    print(f"Fetching import  {import_mpan} / {', '.join(import_serials)} ...")
    imp = _fetch_meter(session, import_mpan, import_serials, start, today)
    print(f"Fetching export  {export_mpan} / {', '.join(export_serials)} ...")
    exp = _fetch_meter(session, export_mpan, export_serials, start, today)
    if imp.empty:
        raise SystemExit("No import data returned. Is the meter sending half-hourly readings?")

    df = pd.DataFrame({"import_kwh": imp, "export_kwh": exp})
    df.index.name = "interval_start"
    df["export_kwh"] = df["export_kwh"].fillna(0.0)
    df = df.dropna(subset=["import_kwh"])

    if imp_m.get("agreements"):
        print("Fetching tariff rates ...")
        df["import_rate_p"] = _rates(session, imp_m["agreements"], df.index).values
        if exp_m.get("agreements"):
            df["export_rate_p"] = _rates(session, exp_m["agreements"], df.index).values

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
