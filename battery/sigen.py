"""Download 5-minute battery history from Sigen Cloud (the API the mySigen app uses).

There is no official public documentation for this API. Endpoints and the password
encryption follow community clients (e.g. github.com/paulczar/sigen-mcp), so expect
it to change without notice.
"""

import base64
import json
import os
import time

import pandas as pd
import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from dotenv import load_dotenv

UK = "Europe/London"
REGIONS = {
    "eu": "https://api-eu.sigencloud.com",
    "aus": "https://api-aus.sigencloud.com",
    "apac": "https://api-apac.sigencloud.com",
    "us": "https://api-us.sigencloud.com",
    "cn": "https://api-cn.sigencloud.com",
}
# The app encrypts the password with this fixed AES key/IV before sending it.
_AES_KEY = b"sigensigensigenp"
FIELDS = {
    "batSoc": "soc_pct",
    "esChargePower": "charge_kw",
    "esDischargePower": "discharge_kw",
    "pvTotalPower": "pv_kw",
    "loadPower": "load_kw",
    "fromGridPower": "from_grid_kw",
    "toGridPower": "to_grid_kw",
}


def _encrypt(password):
    padder = padding.PKCS7(128).padder()
    data = padder.update(password.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_KEY)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def _unwrap(resp, what):
    if resp.status_code in (424, 429):
        raise _RateLimited(what)
    if resp.status_code in (401, 403):
        raise SystemExit(f"Sigen rejected the login for {what} ({resp.status_code}). Check SIGEN_USERNAME/SIGEN_PASSWORD.")
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") not in (None, 0):
        raise _ApiError(body.get("code"), body.get("msg"), what)
    data = body.get("data")
    # Some endpoints wrap the payload as a JSON string.
    return json.loads(data) if isinstance(data, str) and data[:1] in "{[" else data


class _RateLimited(Exception):
    pass


class _ApiError(Exception):
    def __init__(self, code, msg, what):
        super().__init__(f"Sigen API error {code} for {what}: {msg}")
        self.code = code


class SigenClient:
    def __init__(self, username, password, region="eu"):
        self.base = REGIONS[region]
        self.username, self.password = username, password
        self.s = requests.Session()
        self.app_token = None   # token for the app endpoints (station lookup)
        self.nb_token = None    # token for the /openapi endpoints (history)

    def _app_login(self):
        resp = self.s.post(
            f"{self.base}/auth/oauth/token",
            headers={"Authorization": "Basic " + base64.b64encode(b"sigen:sigen").decode()},
            data={"username": self.username, "password": _encrypt(self.password), "grant_type": "password"},
            timeout=30,
        )
        self.app_token = _unwrap(resp, "app login")["access_token"]

    def _nb_login(self):
        resp = self.s.post(f"{self.base}/openapi/auth/login/password",
                           json={"username": self.username, "password": _encrypt(self.password)}, timeout=30)
        self.nb_token = _unwrap(resp, "openapi login")["accessToken"]

    def station_code(self):
        if not self.app_token:
            self._app_login()
        resp = self.s.get(f"{self.base}/device/owner/station/home",
                          headers={"Authorization": f"Bearer {self.app_token}"}, timeout=30)
        data = _unwrap(resp, "station lookup")
        return str(data.get("stationCode") or data["stationId"])

    def history(self, code, day):
        if not self.nb_token:
            self._nb_login()
        resp = self.s.get(f"{self.base}/openapi/systems/{code}/history",
                          params={"date": day, "level": "day"},
                          headers={"Authorization": f"Bearer {self.nb_token}"}, timeout=60)
        return _unwrap(resp, f"history {day}")


def fetch(days, cache_dir, out_path, pause=1.0):
    """Download each day's 5-minute history, caching one JSON file per day so reruns resume."""
    load_dotenv()
    user, pwd = os.getenv("SIGEN_USERNAME", "").strip(), os.getenv("SIGEN_PASSWORD", "")
    if not user or not pwd:
        raise SystemExit("Set SIGEN_USERNAME and SIGEN_PASSWORD (in .env or the environment) to fetch Sigen data.")
    client = SigenClient(user, pwd, os.getenv("SIGEN_REGION", "eu").strip() or "eu")
    code = os.getenv("SIGEN_STATION_CODE", "").strip() or client.station_code()
    print(f"Sigen station {code}: fetching {len(days)} days of 5-minute history ...")

    cache_dir.mkdir(parents=True, exist_ok=True)
    fetched = skipped = 0
    for day in days:
        path = cache_dir / f"{day}.json"
        if path.exists():
            continue
        for attempt in range(5):
            try:
                data = client.history(code, day)
                break
            except _RateLimited:
                wait = 60 * (attempt + 1)
                print(f"  rate limited; waiting {wait}s ...")
                time.sleep(wait)
            except _ApiError as e:
                if e.code == 1201:  # no access for this date, e.g. before installation
                    data = {"itemList": []}
                    skipped += 1
                    break
                raise
        else:
            raise SystemExit("Sigen kept rate-limiting; run again later and it will resume.")
        path.write_text(json.dumps(data))
        fetched += 1
        if fetched % 25 == 0:
            print(f"  {fetched} days fetched ...")
        time.sleep(pause)
    print(f"Sigen: {fetched} new day(s) fetched, {skipped} with no data.")
    return build_csv(cache_dir, out_path)


def build_csv(cache_dir, out_path):
    rows = []
    for path in sorted(cache_dir.glob("*.json")):
        items = json.loads(path.read_text()).get("itemList") or []
        rows.extend(items)
    if not rows:
        raise SystemExit("Sigen returned no history.")
    df = pd.DataFrame(rows)
    df = df[["dataTime"] + [k for k in FIELDS if k in df]].rename(columns=FIELDS)
    # dataTime is local wall-clock time; the repeated October hour is resolved by order.
    t = pd.to_datetime(df.pop("dataTime"))
    df.index = t.dt.tz_localize(UK, ambiguous="infer", nonexistent="NaT")
    df = df[df.index.notna()].sort_index()
    df = df[~df.index.duplicated()].apply(pd.to_numeric, errors="coerce")
    df.index.name = "time"
    out = df.copy()
    out.index = out.index.map(lambda ts: ts.isoformat()).rename("time")
    out.to_csv(out_path)
    print(f"Saved {len(df):,} Sigen readings to {out_path}")
    return df


def load(path):
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(UK)
    return df.set_index("time").sort_index()


def to_halfhours(sig, hh_index):
    """Half-hourly battery state aligned to the Octopus intervals.

    soc_min_pct is the lowest charge level seen in the half-hour; discharge_kwh is the
    energy the battery delivered in it (power readings are averaged over the slot).
    """
    utc = sig.tz_convert("UTC")
    g = utc.resample("30min")
    out = pd.DataFrame({
        "soc_min_pct": g["soc_pct"].min(),
        "soc_end_pct": g["soc_pct"].last(),
        "discharge_kwh": g["discharge_kw"].mean() * 0.5 if "discharge_kw" in utc else float("nan"),
        "sigen_from_grid_kwh": g["from_grid_kw"].mean() * 0.5 if "from_grid_kw" in utc else float("nan"),
    })
    out.index = out.index.tz_convert(UK)
    return out.reindex(hh_index)


def check_alignment(hh):
    """Compare Sigen's grid import with the Octopus meter, to catch unit or clock problems."""
    both = hh[["import_kwh", "sigen_from_grid_kwh"]].dropna()
    both = both[both["import_kwh"] > 0.2]
    if len(both) < 100:
        return None, None
    ratio = both["import_kwh"].sum() / both["sigen_from_grid_kwh"].sum() if both["sigen_from_grid_kwh"].sum() else None
    corr = both["import_kwh"].corr(both["sigen_from_grid_kwh"])
    return ratio, corr
