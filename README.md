# Home battery expansion analysis

Pulls 12 months of half-hourly import/export data from the Octopus Energy API and works out
whether adding battery capacity (+5, +8, +10 kWh) to the SigenStor system pays for itself.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env        # add your API key, MPANs and meter serial
# edit config.toml: tariff times/rates and the module cost
python run.py               # fetch + analyse
python run.py --no-fetch    # re-analyse the saved CSV without calling the API
```

Outputs go to `output/` (`summary.txt`, `monthly_summary.csv`, `annual_summary.csv`,
`daily_analysis.csv`, `capped_halfhours.csv`, `daily_peak_import.png`). Raw data is saved to
`data/octopus_halfhourly.csv`. `.env`, `data/` and `output/` are git-ignored.

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
