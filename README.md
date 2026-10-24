# Home battery expansion analysis

Pulls 12 months of half-hourly import/export data from the Octopus Energy API and works out
whether adding battery capacity (+5, +8, +10 kWh) to the SigenStor system pays for itself.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env        # add your API key and account number
# edit config.toml: set module_cost_gbp
python run.py               # fetch + analyse
python run.py --no-fetch    # re-analyse the saved CSV without calling the API
```

With `ACCOUNT_NUMBER` set, the script reads your MPANs, meter serials and tariff history from
the Octopus account and fetches the actual unit rate for every half-hour, including price changes
and tariff switches. Off-peak is then any half-hour within 0.5p of that day's cheapest rate. Without
it, set the MPANs/serial in `.env` and the tariff in `config.toml`.

Open `output/dashboard.html` in a browser for the interactive version. Drag the capacity
slider, change the module cost or try different rates, and the savings, payback and charts
recalculate. It's a single offline file with your data embedded and Chart.js inlined from
`battery/vendor/`, so treat it as private.

Outputs go to `output/` (`dashboard.html`, `summary.txt`, `monthly_summary.csv`, `annual_summary.csv`,
`daily_analysis.csv`, `capped_halfhours.csv`, `daily_peak_import.png`). Raw data is saved to
`data/octopus_halfhourly.csv`. `.env`, `data/` and `output/` are git-ignored.

### Sigen battery data (optional)

Set `SIGEN_USERNAME` and `SIGEN_PASSWORD` (your mySigen app login) in `.env` or the environment.
`run.py` then downloads 5-minute battery history from Sigen Cloud into `data/sigen/`, one file per
day, so a re-run resumes where it stopped. With it, the model:

- counts only peak import made **while the battery was empty** as fixable by more capacity
  (import while it still had charge is the power limit, the reserve, or a load it doesn't feed);
- checks whether the battery is **full at the end of each off-peak window** in winter, and estimates
  what fixing the charge settings would save before you spend anything.

On each run it prints how well Sigen's grid import matches the Octopus meter, which catches unit
or clock problems. The Sigen Cloud API is undocumented and was worked out from the app by the
community, so it may change without notice.

## How the model works

- **Time:** every half-hour is held in UK local time (Europe/London), so off-peak windows follow
  the clock through BST/GMT changes. The CSV stores timestamps with their offset, so the repeated
  01:00 hour in October stays distinct.
- **Day:** if the off-peak window starts in the evening (e.g. 23:30), a "day" runs from that start to
  the next one and is labelled with the following date, so each night's charge pairs with the next
  day's usage.
- **Peak import** is the shortfall the existing battery didn't cover.
- **Power cap:** the inverter can deliver at most 6 kW, i.e. 3 kWh per half-hour. Only
  `min(import, 3 kWh)` of each peak half-hour counts as shiftable. Anything above that can't be
  covered however big the battery is. Half-hours importing more than 3 kWh are flagged.
- **Shifted kWh per day** = `min(extra capacity, shiftable peak import)`, also capped by what 6 kW
  can recharge in the off-peak window after refilling the existing 10.76 kWh.
- **Rates:** valued day by day using that day's rates (peak weighted by when you imported).
- **Value per kWh shifted:** grid-charged = `peak − off-peak ÷ 0.9`; solar-charged =
  `peak − export ÷ 0.9`, limited to that day's exported kWh × 0.9. The cheaper source is used
  first. With normal Octopus rates (export above off-peak), grid charging wins and solar only tops up.
- **Cost:** `ceil(extra kWh ÷ module_kwh) × module_cost_gbp`, so +8 and +10 kWh each need two
  5 kWh modules. Annual figures are scaled to 365 days.

## Limits

Octopus data shows only net grid flows, not battery state. So the model can't tell a battery that
was empty from one that was already discharging at full power, and it treats every sub-6 kW peak
import as coverable. Savings are therefore an upper bound. It also assumes the battery may
discharge into the Tesla. If your setup blocks that, peak-time car charging isn't really shiftable.

On Intelligent Octopus Go, extra smart-charge slots outside the off-peak window are billed at the
off-peak rate. The REST API doesn't expose them, so car charging in those slots is counted as
peak here. That overstates both peak import and the savings.
