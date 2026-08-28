# HGC module-production day-schedule figures

Parametric version of the one-day resource-lane Gantt (stations × time, colored
batch bars, prior-day WIP hatched, staffing step chart). The schedule is
**computed** from the assumptions in a scenario YAML by a greedy forward
scheduler that honors station capacity, crew/people constraints, and job
dependencies — change a cycle time and the whole day reflows.

## Regenerating

```sh
# the working scenarios (6 / 5 / 4 batches per day):
~/python-environments/webplot/bin/python make_schedule_figure.py scenario_6batches.yaml
~/python-environments/webplot/bin/python make_schedule_figure.py scenario_5batches.yaml
~/python-environments/webplot/bin/python make_schedule_figure.py scenario_4batches.yaml
# reference reproduction + staffing experiment:
~/python-environments/webplot/bin/python make_schedule_figure.py schedule_config_baseline.yaml
~/python-environments/webplot/bin/python make_schedule_figure.py scenario_3people.yaml
```

(needs `yaml` + `playwright` for the PNG export — both live in the `webplot`
environment; the SVG needs nothing beyond `yaml`.)

Each run prints the computed schedule (hatched carryover marked `*`) plus the
jobs that spilled past the staffed window, and writes `<scenario>.svg` and
`<scenario>.png`.

## Making a new scenario (3 lines)

```sh
cp schedule_config_baseline.yaml scenario_myidea.yaml
$EDITOR scenario_myidea.yaml     # tweak cycle_times_min / crews / batches / carryover
~/python-environments/webplot/bin/python make_schedule_figure.py scenario_myidea.yaml
```

One file = one scenario. Everything the figure shows is an assumption in that
file: production window, batch list + colors, per-station cycle times (per
batch/side), crews (headcount + shift, which stations they run,
`people_per_job`), the flow template for today's batches, the prior-day WIP
(`carryover`) entering the morning queues, handoff markers, and the displayed
staffing steps.

### Batches per day

`num_batches: N` in the config keeps the first N of the `batches` list and
automatically drops carryover entries for the removed batches (the retained
carryover still follows the baseline day's pattern). Quick look without editing
a file:

```sh
~/python-environments/webplot/bin/python make_schedule_figure.py \
    schedule_config_baseline.yaml --num-batches 5 -o quick_5batches
```

`scenario_5batches.yaml` / `scenario_4batches.yaml` are ready-made: 5 batches
almost fills the day (3 jobs spill vs 9 in baseline); 4 batches clears
completely — wirebonder and encap go idle by ~17:15 with zero spill.

### Multiple shifts per crew

A crew can list `shifts:` instead of a single `shift:` — each window with its
own headcount. Coverage is the union of the windows, so back-to-back shifts
give continuous coverage (a job may span the changeover); a gap between
windows leaves the stations unstaffed in between. In `style: bars` mode each
shift gets its own presence bars.

```yaml
crews:
  assembly:
    shifts:
      - {people: 2, from: "08:00", to: "16:00"}   # day shift
      - {people: 2, from: "16:00", to: "20:00"}   # evening shift
```

### Lunch / breaks

`breaks:` is a list of windows during which people are unavailable. It can sit
on a crew (applies to every shift window) or on an individual shift window
(applies to those people only):

```yaml
crews:
  assembly:
    shifts:
      - {people: 2, from: "08:00", to: "16:00", breaks: [["13:00", "14:00"]]}
      - {people: 1, from: "13:00", to: "20:00"}    # covers during the day-shift lunch
  wirebond: {people: 1, shift: ["08:00", "21:00"], breaks: [["12:00", "12:30"]]}
```

Scheduling uses true availability: a job needs its `people_per_job` available
at every moment, so work continues through a break if enough people from an
overlapping shift cover it, and stops (schedule reflows) if availability drops
below what the job needs. The staffed band is whited out only where nobody at
all is available; in `style: bars` mode each person's breaks are drawn as gray
hatched notches in their presence bar. To show a dip in the classic `steps`
histogram, add matching step entries by hand.

### "People on shift" as per-person bars

`style: bars` under `staffing:` replaces the step-chart histogram with one
presence bar per person, derived from the crews; the bar is labeled with the
crew name, and any crew `breaks:` are drawn as gray hatched notches inside
the bar. `staffing.steps` is ignored in
this mode, and the "peak N at once" sublabel is computed from the crews unless
given explicitly. Omit `style` (or use `steps`) for the classic histogram.

```yaml
staffing:
  style: bars
  label: People on shift
```

### Station downtime

An entry in the `carryover:` list with `break:` instead of a batch/pass blocks
that station for N minutes, scheduled like a job at its queue position (drawn
as a gray hatched block):

```yaml
carryover:
  - {station: wirebonder, batch: Batch 2, pass: F}
  - {station: wirebonder, break: 60, label: maintenance}   # 1 h downtime after F2
```

Options: `at: "11:00"` pins it to a clock time instead of the queue position;
`people: 1` if the maintenance also occupies a crew member (default 0 — it can
even sit inside a lunch break); `priority: late` queues it after today's work.

## Feeding cycle times from a Google Sheet

`--cycle-csv` overrides cycle times from a flat CSV (export a sheet tab with
columns `cycle,minutes,batch`; empty batch = default for all batches):

```sh
~/python-environments/webplot/bin/python make_schedule_figure.py \
    schedule_config_baseline.yaml --cycle-csv cycle_times_example.csv -o baseline_sheet
```

## Files

- `make_schedule_figure.py` — scheduler + pure-SVG renderer (+ PNG via headless chromium)
- `schedule_config_baseline.yaml` — reproduces the reference figure
  (Mon 8:00–18:30, 6 batches, peak 5 people); calibrated by measuring the
  reference image, matches it bar-for-bar
- `baseline.svg` / `baseline.png` — rendered baseline
- `scenario_3people.yaml` + renders — same day with peak staffing of 3
  (gantry crew of 2 + one shared tech for OGP/wirebonder/encap/test):
  downstream stations serialize, the test queue never runs, 33 jobs spill
- `scenario_6batches.yaml` + renders — the working scenario: 6 batches on an
  extended 8:00–20:00 day, merged Gantry/OGP row, two-shift assembly crew with
  per-shift lunch breaks, dedicated wirebond/encap techs
- `scenario_5batches.yaml` / `scenario_4batches.yaml` + renders — baseline day
  with the `num_batches` knob at 5 / 4
- `cycle_times_example.csv` — sheet-friendly cycle-time override template
- `reference-figure.png` — the original figure the baseline was calibrated to

## Scheduler semantics (short version)

- Today's batches run the `today_flow` chain (gantry → OGP, gantry → backside
  wirebond → backside encap), drawn **solid**; `carryover` lists prior-day WIP
  entering the morning queues, drawn **hatched**.
- Greedy forward scheduling: at any moment the highest-priority ready job that
  fits (free lane, enough crew members free for its whole duration, inside its
  crew's shift, before its optional `start_by` cutoff) is started. Priority =
  carryover first, then today's work, then `priority: late` entries; ties by
  listing order.
- Jobs that can't finish inside the staffed window spill to the next day and
  are reported on stdout (in steady state they are exactly the next morning's
  `carryover`).
