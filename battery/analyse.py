"""Tag half-hours, build daily totals and simulate extra battery capacity."""

import math

import pandas as pd


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def check_config(cfg):
    t, b = cfg["tariff"], cfg["battery"]
    missing = [k for k in ("offpeak_rate_p", "peak_rate_p", "export_rate_p") if not t.get(k)]
    if not t.get("offpeak_windows"):
        missing.append("offpeak_windows")
    if not b.get("module_cost_gbp"):
        missing.append("module_cost_gbp")
    if missing:
        raise SystemExit("Fill in config.toml before analysing. Missing: " + ", ".join(missing))


def tag_halfhours(df, cfg):
    """Add is_offpeak, and battery_day: the day each half-hour belongs to.

    If the off-peak window starts in the evening (e.g. 23:30), the cycle "charge
    overnight, discharge through the following day" is labelled with the date of
    the following day, so each day's off-peak charge precedes its peak usage.
    """
    windows = [(_minutes(a), _minutes(b)) for a, b in cfg["tariff"]["offpeak_windows"]]
    # Wall-clock time in the UK, so 23:30 means 23:30 in both GMT and BST.
    local = df.index.tz_localize(None)
    mins = local.hour * 60 + local.minute

    offpeak = pd.Series(False, index=df.index)
    for start, end in windows:
        if start < end:
            offpeak |= (mins >= start) & (mins < end)
        else:  # window crosses midnight
            offpeak |= (mins >= start) | (mins < end)

    first_start = windows[0][0]
    shift = pd.Timedelta(minutes=24 * 60 - first_start) if first_start >= 12 * 60 else pd.Timedelta(0)

    out = df.copy()
    out["is_offpeak"] = offpeak.values
    out["battery_day"] = (local + shift).normalize()
    return out


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
    daily = parts.groupby(hh["battery_day"]).sum()
    daily.index.name = "day"
    return daily


def simulate(daily, cfg):
    """Add shifted kWh and £ saved columns for each extra-capacity scenario."""
    t, b = cfg["tariff"], cfg["battery"]
    eff = b["round_trip_efficiency"]
    peak, offpeak, export = t["peak_rate_p"], t["offpeak_rate_p"], t["export_rate_p"]

    # Value of each kWh delivered from the extra capacity during peak (p/kWh).
    # Storing 1 kWh costs the input energy, and only `eff` of it comes back out,
    # so both sources are divided by efficiency.
    value_grid = max(peak - offpeak / eff, 0.0)
    value_solar = max(peak - export / eff, 0.0)
    solar_first = value_solar > value_grid

    # Energy the inverter can put into the extra capacity off-peak, after recharging
    # the existing battery, expressed as delivered kWh.
    grid_room = ((b["inverter_kw"] * daily["offpeak_hours"] - b["existing_kwh"]).clip(lower=0.0)) * eff
    # Exported solar that could have been stored instead, as delivered kWh.
    solar_room = daily["export_kwh"] * eff

    out = daily.copy()
    for kwh in b["scenarios_kwh"]:
        shift = daily["peak_shiftable_kwh"].clip(upper=kwh)
        if solar_first:
            solar = pd.concat([shift, solar_room], axis=1).min(axis=1)
            grid = pd.concat([shift - solar, grid_room], axis=1).min(axis=1)
        else:
            grid = pd.concat([shift, grid_room], axis=1).min(axis=1)
            solar = pd.concat([shift - grid, solar_room], axis=1).min(axis=1)
        out[f"grid_shift_{kwh}"] = grid
        out[f"solar_shift_{kwh}"] = solar
        out[f"shifted_{kwh}"] = grid + solar
        out[f"saving_gbp_{kwh}"] = (grid * value_grid + solar * value_solar) / 100
    return out, value_grid, value_solar


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
