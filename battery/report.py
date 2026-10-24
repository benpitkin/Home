"""Verdict text, chart and files written to output/."""

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

# Text inks and series colours (light theme; the PNG is a static, light figure).
INK, INK_2, INK_3 = "#0b0b0b", "#52514e", "#8a8984"
BLUE, ORANGE = "#2a78d6", "#eb6834"
GRID = "#e6e5e1"


def verdict(sim, annual, cfg, value_grid, value_solar):
    b = cfg["battery"]
    cap_kw = b["inverter_kw"]
    peak_total = sim["peak_import_kwh"].sum()
    over_cap = sim["peak_over_cap_kwh"].sum()
    over_share = over_cap / peak_total if peak_total else 0.0
    capped_hh = int(sim["capped_halfhours_peak"].sum())
    median_short = sim["peak_shiftable_kwh"].median()

    best = annual["payback_years"].idxmin()
    best_row = annual.loc[best]
    lines = []

    if best_row["annual_saving_gbp"] <= 0:
        lines.append("Not worth it: the extra capacity would barely be used, so it saves almost nothing.")
    else:
        yrs = best_row["payback_years"]
        if yrs <= 7:
            call = "Worth it"
        elif yrs <= 10:
            call = "Borderline"
        else:
            call = "Not worth it on savings alone"
        lines.append(
            f"{call}. Best option is {best} ({int(best_row['modules'])} module(s), "
            f"£{best_row['cost_gbp']:,.0f}): about £{best_row['annual_saving_gbp']:,.0f} a year, "
            f"paying back in {yrs:.1f} years."
        )

    # Is more capacity than the first module worth it?
    first = annual.iloc[0]
    for name, row in annual.iloc[1:].iterrows():
        extra_cost = row["cost_gbp"] - first["cost_gbp"]
        extra_save = row["annual_saving_gbp"] - first["annual_saving_gbp"]
        if extra_cost > 0:
            marg = extra_cost / extra_save if extra_save > 0 else float("inf")
            marg_txt = f"{marg:.1f} years" if marg != float("inf") else "never"
            lines.append(
                f"Going from {first.name} to {name} adds £{extra_save:,.0f}/yr for £{extra_cost:,.0f} more "
                f"(marginal payback {marg_txt})."
            )

    lines.append(
        f"On a typical day the battery could have displaced {median_short:.1f} kWh more peak import; "
        f"a +{annual.iloc[0]['extra_kwh']:g} kWh module would be completely used on "
        f"{first['days_capacity_fully_used_pct']:.0f}% of days."
    )

    if over_share >= 0.25:
        lines.append(
            f"The inverter's {cap_kw:g} kW limit IS a real constraint: {over_share:.0%} of your peak import "
            f"({over_cap:,.0f} kWh across {capped_hh} half-hours) came while drawing over {cap_kw:g} kW, "
            "which no extra capacity can cover. That is usually the Tesla or heat pump running in peak hours, "
            "so moving those loads off-peak will save more than another module."
        )
    elif over_share >= 0.10:
        lines.append(
            f"The {cap_kw:g} kW limit matters a little: {over_share:.0%} of peak import ({over_cap:,.0f} kWh, "
            f"{capped_hh} half-hours) was above what the inverter can supply."
        )
    else:
        lines.append(
            f"The {cap_kw:g} kW limit is not the main constraint: only {over_share:.0%} of peak import "
            f"({capped_hh} half-hours) was above what the inverter can supply. Capacity is the limit."
        )

    lines.append(
        f"Each kWh shifted is worth {value_grid:.1f}p if grid-charged off-peak, "
        f"{value_solar:.1f}p if charged from solar you'd otherwise export."
    )
    return lines


def chart(sim, cfg, path):
    b = cfg["battery"]
    days = sim.index
    shiftable = sim["peak_shiftable_kwh"]
    over = sim["peak_over_cap_kwh"]

    fig, ax = plt.subplots(figsize=(12, 5), dpi=150)
    ax.bar(days, shiftable, width=0.8, color=BLUE, label=f"Peak import within {b['inverter_kw']:g} kW")
    ax.bar(days, over, width=0.8, bottom=shiftable, color=ORANGE,
           label=f"Peak import above {b['inverter_kw']:g} kW (power-capped)")

    for kwh in b["scenarios_kwh"]:
        ax.axhline(kwh, color=INK_2, linewidth=1, linestyle=(0, (4, 3)), zorder=3)
        ax.annotate(f"+{kwh} kWh", xy=(days[-1], kwh), xytext=(6, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK_2, annotation_clip=False)

    ax.set_title(f"Daily peak-time import — {cfg['tariff']['name']}", loc="left", color=INK, fontsize=13, pad=28)
    ax.set_ylabel("kWh per day", color=INK_2)
    ax.margins(x=0.01)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(INK_3)
    ax.tick_params(colors=INK_2, length=0)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, fontsize=9,
              labelcolor=INK_2, borderaxespad=0.3, handlelength=1.2)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def format_summary(monthly, annual, verdict_lines, cfg, sim):
    t = cfg["tariff"]
    scen = cfg["battery"]["scenarios_kwh"]
    windows = ", ".join(f"{a}–{b}" for a, b in t["offpeak_windows"])
    out = [
        f"Tariff: {t['name']}  |  off-peak {windows} @ {t['offpeak_rate_p']}p  |  "
        f"peak {t['peak_rate_p']}p  |  export {t['export_rate_p']}p",
        f"Data: {sim.index.min():%d %b %Y} to {sim.index.max():%d %b %Y} ({len(sim)} days)",
        "",
        "MONTHLY (averages per day; shifted kWh and £ are monthly totals)",
    ]
    m = pd.DataFrame(index=monthly.index.astype(str))
    m["Peak imp"] = monthly["avg_daily_peak_import_kwh"].map("{:.1f}".format)
    m["Export"] = monthly["avg_daily_export_kwh"].map("{:.1f}".format)
    m[">6kW hh"] = monthly["capped_peak_halfhours"]
    for kwh in scen:
        m[f"+{kwh} kWh"] = monthly[f"+{kwh}kWh_shifted_kwh"].map("{:.0f}".format)
        m[f"+{kwh} £"] = monthly[f"+{kwh}kWh_saved_gbp"].map("£{:.2f}".format)
    out.append(m.to_string())
    out += ["", "ANNUAL"]
    a = pd.DataFrame(index=annual.index)
    a["Modules"] = annual["modules"]
    a["Cost"] = annual["cost_gbp"].map("£{:,.0f}".format)
    a["kWh shifted/yr"] = annual["annual_shifted_kwh"].map("{:,.0f}".format)
    a["Saving/yr"] = annual["annual_saving_gbp"].map("£{:,.0f}".format)
    a["Payback"] = annual["payback_years"].map(lambda y: "never" if y == float("inf") else f"{y:.1f} yrs")
    a["Days full"] = annual["days_capacity_fully_used_pct"].map("{:.0f}%".format)
    out.append(a.to_string())
    out += ["", "VERDICT"] + [f"- {line}" for line in verdict_lines]
    return "\n".join(out)
