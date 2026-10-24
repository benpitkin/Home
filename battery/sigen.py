"""Download 5-minute battery history from the Sigen Cloud OpenAPI.

Logs in with developer-portal AppKey/AppSecret, or with the mySigen app login. Endpoints
follow community clients (e.g. github.com/schwarzbr0t/sigenergy-ha); Sigenergy's own docs
are behind the developer portal, so expect changes without notice.
"""

import base64
import json
import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv

UK = "Europe/London"
REGIONS = {
    "eu": "https://api-eu.sigencloud.com",
    "aus": "https://api-aus.sigencloud.com",
    "apac": "https://api-apac.sigencloud.com",
    "us": "https://api-us.sigencloud.com",
    "cn": "https://api-cn.sigencloud.com",
}
FIELDS = {
    "batSoc": "soc_pct",
    "esChargePower": "charge_kw",
    "esDischargePower": "discharge_kw",
    "pvTotalPower": "pv_kw",
    "loadPower": "load_kw",
    "fromGridPower": "from_grid_kw",
    "toGridPower": "to_grid_kw",
}


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
    return _maybe_json(data)


def _maybe_json(data):
    # Some endpoints wrap the payload (and fields inside it) as a JSON string.
    return json.loads(data) if isinstance(data, str) and data[:1] in "{[" else data


class _RateLimited(Exception):
    pass


class _ApiError(Exception):
    def __init__(self, code, msg, what):
        super().__init__(f"Sigen API error {code} for {what}: {msg}")
        self.code = code


class SigenClient:
    def __init__(self, username, password, region="eu", app_key="", app_secret=""):
        self.base = REGIONS[region]
        self.username, self.password = username, password
        self.app_key, self.app_secret = app_key, app_secret
        self.s = requests.Session()
        self.token = None

    def _login(self):
        # Sigen allows one login per second; back off briefly if asked to.
        for attempt in range(3):
            try:
                return self._login_once()
            except _ApiError as e:
                if e.code != 2028 or attempt == 2:
                    raise
                time.sleep(2)

    def _login_once(self):
        if self.app_key and self.app_secret:
            # Official developer-portal credentials.
            key = base64.b64encode(f"{self.app_key}:{self.app_secret}".encode()).decode()
            resp = self.s.post(f"{self.base}/openapi/auth/login/key", json={"key": key}, timeout=30)
        else:
            resp = self.s.post(f"{self.base}/openapi/auth/login/password",
                               json={"username": self.username, "password": self.password}, timeout=30)
        self.token = _unwrap(resp, "login")["accessToken"]

    def _get(self, path, params, what):
        if not self.token:
            self._login()
        resp = self.s.get(f"{self.base}{path}", params=params,
                          headers={"Authorization": f"Bearer {self.token}"}, timeout=60)
        return _unwrap(resp, what)

    def station_code(self):
        systems = self._get("/openapi/system", None, "system list") or []
        if not systems:
            raise SystemExit("Sigen returned no systems for this login.")
        return str(systems[0]["systemId"])

    def history(self, code, day):
        data = self._get(f"/openapi/systems/{code}/history",
                         {"systemId": code, "level": "day", "date": day}, f"history {day}") or {}
        data["itemList"] = _maybe_json(data.get("itemList")) or []
        return data


# The history endpoint allows one request per system every 5 minutes; Sigen answers
# faster requests with error 1201 "Access restriction".
PAUSE = 305


def fetch(days, cache_dir, out_path, pause=PAUSE):
    """Download each day's 5-minute history, caching one JSON file per day so reruns resume.

    `days` is fetched in the order given, so put the days that matter most first.
    """
    load_dotenv()
    user, pwd = os.getenv("SIGEN_USERNAME", "").strip(), os.getenv("SIGEN_PASSWORD", "")
    app_key, app_secret = os.getenv("SIGEN_APP_KEY", "").strip(), os.getenv("SIGEN_APP_SECRET", "").strip()
    code = os.getenv("SIGEN_STATION_CODE", "").strip()
    if not (app_key and app_secret) and not (user and pwd):
        raise SystemExit("Set SIGEN_APP_KEY + SIGEN_APP_SECRET, or SIGEN_USERNAME + SIGEN_PASSWORD, to fetch Sigen data.")
    client = SigenClient(user, pwd, os.getenv("SIGEN_REGION", "eu").strip() or "eu", app_key, app_secret)
    code = code or client.station_code()

    cache_dir.mkdir(parents=True, exist_ok=True)
    todo = [d for d in days if not (cache_dir / f"{d}.json").exists()]
    print(f"Sigen station {code}: {len(days) - len(todo)} of {len(days)} days already saved; "
          f"{len(todo)} to fetch (~{len(todo) * pause / 3600:.1f} h at one request per {pause // 60} min). "
          "Stop any time; the next run resumes.")
    for n, day in enumerate(todo, 1):
        for attempt in range(4):
            try:
                data = client.history(code, day)
                break
            except (_RateLimited, _ApiError) as e:
                if isinstance(e, _ApiError) and e.code != 1201:
                    raise
                time.sleep(pause)
        else:
            # Still refused after several spaced-out tries: genuinely unavailable for this date.
            data = {"itemList": []}
        (cache_dir / f"{day}.json").write_text(json.dumps(data))
        print(f"  {n}/{len(todo)} {day}: {len(data['itemList'])} readings", flush=True)
        if n < len(todo):
            time.sleep(pause)
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
    # dataTime is UK wall-clock time, e.g. "20260115 18:05".
    t = pd.to_datetime(df.pop("dataTime"), format="mixed")
    try:  # the repeated October hour, resolved by order where both copies are present
        df.index = t.dt.tz_localize(UK, ambiguous="infer", nonexistent="NaT")
    except Exception:
        df.index = t.dt.tz_localize(UK, ambiguous="NaT", nonexistent="NaT")
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
