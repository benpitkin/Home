"""Tag half-hours, build daily totals and simulate extra battery capacity."""

import math

import pandas as pd


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def has_rates(df):
    return "import_rate_p" in df and df["import_rate_p"].notna().mean() > 0.9


def check_config(cfg, df=None):
    """Exit with a clear message if the tariff or module cost still needs filling in.

    Tariff values are only required when the data has no rates from the Octopus account.
    """
    t, b = cfg["tariff"], cfg["battery"]
    missing = []
    if df is None or not has_rates(df):
        missing = [k for k in ("offpeak_rate_p", "peak_rate_p") if not t.get(k)]
        if not t.get("offpeak_windows"):
            missing.append("offpeak_windows")
    if (df is None or "export_rate_p" not in df) and not t.get("export_rate_p"):
        missing.append("export_rate_p")
    if not b.get("module_cost_gbp"):
        missing.append("module_cost_gbp")
    if missing:
        raise SystemExit("Fill in config.toml before analysing. Missing: " + ", ".join(missing))


def tag_halfhours(df, cfg):
    """Add is_offpeak, the rate columns, and battery_day: the day each half-hour belongs to.

    With rates from the Octopus account, a half-hour is off-peak when its rate is within
    0.5p of that day's cheapest rate; otherwise the config windows are used.

    If the off-peak window starts in the evening (e.g. 23:30), the cycle "charge
    overnight, discharge through the following day" is labelled with the date of
    the following day, so each day's off-peak charge precedes its peak usage.
    """
    t = cfg["tariff"]
    # Wall-clock time in the UK, so 23:30 means 23:30 in both GMT and BST.
    local = df.index.tz_localize(None)
    mins = pd.Series(local.hour * 60 + local.minute, index=df.index)
    out = df.copy()

    if has_rates(df):
        rate = df["import_rate_p"].ffill().bfill()
        day_min = rate.groupby(local.normalize()).transform("min")
        offpeak = rate <= day_min + 0.5
        out["import_rate_p"] = rate
        # Off-peak start = the most common clock time at which a cheap run begins.
        starts = mins[offpeak & ~offpeak.shift(1, fill_value=True)]
        first_start = int(starts.mode().iloc[0]) if len(starts) else 0
    else:
        windows = [(_minutes(a), _minutes(b)) for a, b in t["offpeak_windows"]]
        offpeak = pd.Series(False, index=df.index)
        for start, end in windows:
            if start < end:
                offpeak |= (mins >= start) & (mins < end)
            else:  # window crosses midnight
                offpeak |= (mins >= start) | (mins < end)
        out["import_rate_p"] = (offpeak.map({True: t["offpeak_rate_p"], False: t["peak_rate_p"]})).astype(float)
        first_start = windows[0][0]

    if "export_rate_p" in df and df["export_rate_p"].notna().any():
        out["export_rate_p"] = df["export_rate_p"].ffill().bfill()
    else:
        out["export_rate_p"] = float(t["export_rate_p"])

    shift = pd.Timedelta(minutes=24 * 60 - first_start) if first_start >= 12 * 60 else pd.Timedelta(0)
    out["is_offpeak"] = offpeak.values
    out["battery_day"] = (local + shift).normalize()
    return out


def offpeak_windows(hh):
    """Describe the off-peak windows actually seen, e.g. ['23:30-05:30'], for the report."""
    local = hh.index.tz_localize(None)
    mins = pd.Series(local.hour * 60 + local.minute, index=hh.index)
    op = hh["is_offpeak"]
    starts = mins[op & ~op.shift(1, fill_value=True)].mode()
    ends = mins[~op & op.shift(1, fill_value=False)].mode()
    fmt = lambda m: f"{int(m) // 60:02d}:{int(m) % 60:02d}"  # noqa: E731
    return [f"{fmt(a)}–{fmt(b)}" for a, b in zip(starts, ends)]


def daily_totals(hh, cfg):
    cap_hh = cfg["battery"]["inverter_kw"] * 0.5      # max kWh the inverter can move in a half-hour
    flag = cfg["analysis"]["capped_halfhour_kwh"]
    peak = ~hh["is_offpeak"]
    imp = hh["import_kwh"]

    parts = pd.DataFrame({
        "peak_import_kwh": imp.where(peak, 0.0),
        "offpeak_import_kwh": imp.where(~peak, 0.0),
        "export_kwh": hh["export_kwh"],
        # Peak import a bigger battery could actually displace, half-hour by half-hour,
        # given the 6 kW discharge limit; the rest is power-capped, not capacity-limited.
        "peak_shiftable_kwh": imp.clip(upper=cap_hh).where(peak, 0.0),
        "peak_over_cap_kwh": (imp - cap_hh).clip(lower=0.0).where(peak, 0.0),
        "capped_halfhours": (imp > flag).astype(int),
        "capped_halfhours_peak": ((imp > flag) & peak).astype(int),
        "offpeak_hours": (~peak).astype(float) * 0.5,
        "halfhours": 1,
    })
    key = hh["battery_day"]

    if has_battery_data(hh):
        # With Sigen history, only peak import while the battery was empty can be fixed by
        # more capacity, and only up to the power the inverter wasn't already delivering.
        # Import while it still had charge was the power limit, the reserve, or a load the
        # battery doesn't feed, so it stays in peak_over_cap_kwh-style "not fixable" import.
        reserve = battery_reserve(hh)
        empty = hh["soc_min_pct"] <= reserve
        headroom = (cap_hh - hh["discharge_kwh"].fillna(0.0)).clip(lower=0.0)
        known = hh["soc_min_pct"].notna()
        sigen_shift = imp.clip(upper=headroom).where(peak & empty, 0.0)
        parts["peak_shiftable_octopus_kwh"] = parts["peak_shiftable_kwh"]
        parts["peak_shiftable_kwh"] = sigen_shift.where(known, parts["peak_shiftable_kwh"])
        parts["peak_while_charged_kwh"] = imp.where(peak & known & ~empty, 0.0)
        parts["peak_over_cap_kwh"] = (imp.where(peak, 0.0) - parts["peak_shiftable_kwh"]).clip(lower=0.0)
        parts["battery_halfhours"] = known.astype(int)

    daily = parts.groupby(key).sum()
    # The day's rates. Peak is weighted by the import a battery could displace, so it
    # reflects the price actually avoided on tariffs with more than two rates.
    r = hh["import_rate_p"]
    w = parts["peak_shiftable_kwh"]
    peak_simple = r.where(peak).groupby(key).mean()
    peak_weighted = (r * w).groupby(key).sum() / w.groupby(key).sum()
    daily["peak_rate_p"] = peak_weighted.fillna(peak_simple).fillna(r.groupby(key).max())
    daily["offpeak_rate_p"] = r.where(~peak).groupby(key).mean().fillna(r.groupby(key).min())
    daily["export_rate_p"] = hh["export_rate_p"].groupby(key).mean()
    if has_battery_data(hh):
        # Charge level when the off-peak window ends: is the battery filling up overnight?
        last_offpeak = hh["is_offpeak"] & ~hh["is_offpeak"].shift(-1, fill_value=False)
        daily["morning_soc_pct"] = hh["soc_end_pct"].where(last_offpeak).groupby(key).max()
    daily.index.name = "day"
    return daily


def has_battery_data(hh):
    return "soc_min_pct" in hh and hh["soc_min_pct"].notna().any()


def battery_reserve(hh):
    """Charge level treated as "empty": just above the lowest level the battery runs down to."""
    return float(hh["soc_min_pct"].quantile(0.01)) + 3.0


def simulate(daily, cfg):
    """Add shifted kWh and £ saved columns for each extra-capacity scenario."""
    b = cfg["battery"]
    eff = b["round_trip_efficiency"]
    peak, offpeak, export = daily["peak_rate_p"], daily["offpeak_rate_p"], daily["export_rate_p"]

    # Value of each kWh delivered from the extra capacity during peak (p/kWh), per day.
    # Storing 1 kWh costs the input energy, and only `eff` of it comes back out,
    # so both sources are divided by efficiency.
    value_grid = (peak - offpeak / eff).clip(lower=0.0)
    value_solar = (peak - export / eff).clip(lower=0.0)
    solar_first = value_solar > value_grid

    # Energy the inverter can put into the extra capacity off-peak, after recharging
    # the existing battery, expressed as delivered kWh.
    grid_room = ((b["inverter_kw"] * daily["offpeak_hours"] - b["existing_kwh"]).clip(lower=0.0)) * eff
    # Exported solar that could have been stored instead, as delivered kWh.
    solar_room = daily["export_kwh"] * eff

    out = daily.copy()
    out["value_grid_p"] = value_grid
    out["value_solar_p"] = value_solar
    for kwh in b["scenarios_kwh"]:
        shift = daily["peak_shiftable_kwh"].clip(upper=kwh)
        g1 = pd.concat([shift, grid_room], axis=1).min(axis=1)
        s1 = pd.concat([shift - g1, solar_room], axis=1).min(axis=1)
        s2 = pd.concat([shift, solar_room], axis=1).min(axis=1)
        g2 = pd.concat([shift - s2, grid_room], axis=1).min(axis=1)
        grid = g2.where(solar_first, g1)
        solar = s2.where(solar_first, s1)
        out[f"grid_shift_{kwh}"] = grid
        out[f"solar_shift_{kwh}"] = solar
        out[f"shifted_{kwh}"] = grid + solar
        out[f"saving_gbp_{kwh}"] = (grid * value_grid + solar * value_solar) / 100
    # Import-weighted average values, for the report.
    w = daily["peak_shiftable_kwh"].where(daily["peak_shiftable_kwh"] > 0, 1e-9)
    return out, float((value_grid * w).sum() / w.sum()), float((value_solar * w).sum() / w.sum())


def monthly_table(sim, cfg):
    scenarios = cfg["battery"]["scenarios_kwh"]
    month = sim.index.to_period("M")
    g = sim.groupby(month)
    table = pd.DataFrame({
        "days": g.size(),
        "avg_daily_peak_import_kwh": g["peak_import_kwh"].mean(),
        "avg_daily_export_kwh": g["export_kwh"].mean(),
        "avg_daily_offpeak_import_kwh": g["offpeak_import_kwh"].mean(),
        "capped_peak_halfhours": g["capped_halfhours_peak"].sum(),
    })
    for kwh in scenarios:
        table[f"+{kwh}kWh_shifted_kwh"] = g[f"shifted_{kwh}"].sum()
        table[f"+{kwh}kWh_saved_gbp"] = g[f"saving_gbp_{kwh}"].sum()
    table.index.name = "month"
    return table


def annual_results(sim, cfg):
    b = cfg["battery"]
    n_days = len(sim)
    scale = 365 / n_days if n_days else 0.0
    rows = []
    for kwh in b["scenarios_kwh"]:
        modules = math.ceil(kwh / b["module_kwh"] - 1e-9)
        cost = modules * b["module_cost_gbp"]
        saving = sim[f"saving_gbp_{kwh}"].sum() * scale
        shifted = sim[f"shifted_{kwh}"].sum() * scale
        # Days on which the extra capacity, not demand or power, was the limit.
        full_days = (sim["peak_shiftable_kwh"] >= kwh).mean()
        rows.append({
            "scenario": f"+{kwh} kWh",
            "extra_kwh": kwh,
            "modules": modules,
            "cost_gbp": cost,
            "annual_shifted_kwh": shifted,
            "annual_saving_gbp": saving,
            "payback_years": cost / saving if saving > 0 else float("inf"),
            "days_capacity_fully_used_pct": 100 * full_days,
        })
    return pd.DataFrame(rows).set_index("scenario")
