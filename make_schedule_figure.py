#!/usr/bin/env python
"""Parametric HGC module-production day-schedule figure.

Reads a scenario config (YAML), computes the day's schedule with a greedy
forward scheduler (station capacity + crew/people constraints), and renders a
resource-lane Gantt figure (SVG + PNG) in the style of the reference figure.

Usage:
    make_schedule_figure.py schedule_config_baseline.yaml
    make_schedule_figure.py scenario_3people.yaml -o myname
    make_schedule_figure.py schedule_config_baseline.yaml --cycle-csv cycles.csv

The schedule is *computed*, not hardcoded: changing a cycle time, a crew
shift, or the carryover list in the config reflows the whole day.
"""

import argparse
import csv
import heapq
import os
import re
import sys

import yaml


# ----------------------------------------------------------------------------
# time helpers
# ----------------------------------------------------------------------------

def tmin(s):
    """'HH:MM' -> minutes from midnight."""
    h, m = str(s).split(':')
    return int(h) * 60 + int(m)


def fmt(t):
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def fmt12(t, with_min=None):
    """12-hour label like the reference axis: 8, 10, 12, 2, 4, 6, 6:30."""
    h, m = int(t) // 60, int(t) % 60
    h12 = h % 12 or 12
    if m or with_min:
        return f"{h12}:{m:02d}"
    return str(h12)


def ampm(t):
    h, m = int(t) // 60, int(t) % 60
    suf = 'AM' if h < 12 else 'PM'
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suf}"


# ----------------------------------------------------------------------------
# color helpers
# ----------------------------------------------------------------------------

def hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(c):
    return '#%02X%02X%02X' % tuple(max(0, min(255, int(round(v)))) for v in c)


def darken(hexcolor, f=0.87):
    """Accent shade used for hatching and module tick marks."""
    return rgb_to_hex(v * f for v in hex_to_rgb(hexcolor))


# ----------------------------------------------------------------------------
# model
# ----------------------------------------------------------------------------

class Job:
    def __init__(self, station, batch, pass_, dur, prio, seq, hatched, label):
        self.station = station          # station id
        self.batch = batch              # batch name
        self.pass_ = pass_              # e.g. 'assemble', 'ogp', 'B', 'F', 'test'
        self.dur = dur                  # minutes
        self.prio = prio                # 0 carryover, 1 today, 2 late
        self.seq = seq                  # tiebreak: queue listing order
        self.hatched = hatched          # prior-day WIP
        self.label = label              # text drawn on the bar
        self.start_by = None            # latest allowed start time (minutes)
        self.at = None                  # exact required start time (minutes)
        self.people = None              # crew people needed (None -> station default)
        self.after_specs = []           # unresolved dependency specs
        self.preds = []                 # resolved Job list
        self.start = None
        self.end = None

    def key(self):
        return (self.station, self.batch, self.pass_, self.hatched)

    def __repr__(self):
        w = f"{fmt(self.start)}-{fmt(self.end)}" if self.start is not None else 'SPILLED'
        return f"<{self.station} {self.label} {w}>"


def crew_windows(crew):
    """Normalize a crew's staffing to [(people, from_min, to_min, breaks), ...].

    Either a single window:      {people: 2, shift: ["08:00", "18:30"]}
    or multiple shift windows:   {shifts: [{people: 2, from: "08:00", to: "14:00"},
                                           {people: 2, from: "14:00", to: "20:00"}]}
    `breaks: [["12:00","12:30"], ...]` may sit on the crew (applies to every
    window) or on an individual shift window (applies to those people only).
    During a break that window's people count as unavailable; people from an
    overlapping window keep working.
    """
    def bmins(obj):
        return [(tmin(b[0]), tmin(b[1])) for b in obj.get('breaks', [])]

    crew_brks = bmins(crew)
    if 'shifts' in crew:
        return [(int(w.get('people', crew.get('people', 1))),
                 tmin(w['from']), tmin(w['to']), crew_brks + bmins(w))
                for w in crew['shifts']]
    return [(int(crew['people']), tmin(crew['shift'][0]),
             tmin(crew['shift'][1]), crew_brks)]


def batch_short(name, cfg_batch):
    if 'short' in cfg_batch:
        return str(cfg_batch['short'])
    m = re.search(r'(\d+)\s*$', name)
    return m.group(1) if m else name[:2]


def cycle_minutes(cfg, key, batch):
    table = cfg['cycle_times_min']
    if key not in table:
        sys.exit(f"config error: cycle_times_min has no entry '{key}'")
    v = table[key]
    if isinstance(v, dict):
        return float(v.get(batch, v['default']))
    return float(v)


def bar_label(station_cfg, pass_, batch, short):
    style = station_cfg.get('label_style', 'batch')
    if style == 'batch':
        return batch
    if style == 'pass':          # 'F4', 'B4'
        return f"{pass_}{short}"
    if style == 'initial':       # 'T4'
        return f"{station_cfg.get('label_initial', pass_[0].upper())}{short}"
    return batch


def build_jobs(cfg):
    """Create today's flow jobs + carryover WIP jobs, resolve dependencies."""
    stations = {s['id']: s for s in cfg['stations']}
    batches = [b['name'] for b in cfg['batches']]
    shorts = {b['name']: batch_short(b['name'], b) for b in cfg['batches']}

    jobs = []
    index = {}   # (station, batch, pass) -> Job

    def add(job):
        if job.key() in index:
            sys.exit(f"config error: duplicate job {job.key()}")
        index[job.key()] = job
        jobs.append(job)

    # carryover WIP (hatched), listed order = queue order
    for i, c in enumerate(cfg.get('carryover', [])):
        st = stations[c['station']]
        if 'break' in c:
            # scheduled downtime: occupies the station for `break` minutes at
            # this queue position (or at a fixed `at:` time), needs no people
            prio = 2 if c.get('priority') == 'late' else 0
            j = Job(st['id'], None, 'down', float(c['break']), prio, i, False,
                    str(c.get('label', 'down')))
            j.people = int(c.get('people', 0))
            if 'at' in c:
                j.at = tmin(c['at'])
                j.start_by = j.at
            if 'after' in c:
                j.after_specs.append(c['after'])
            jobs.append(j)
            continue
        batch = c['batch']
        pass_ = str(c['pass'])
        key = st['cycle_keys'][pass_]
        prio = 2 if c.get('priority') == 'late' else 0
        j = Job(st['id'], batch, pass_, cycle_minutes(cfg, key, batch),
                prio, i, True, bar_label(st, pass_, batch, shorts.get(batch, '?')))
        if 'start_by' in c:
            j.start_by = tmin(c['start_by'])
        if 'after' in c:
            j.after_specs.append(c['after'])
        add(j)

    # today's batches (solid), flow template applied in batch order
    for bi, batch in enumerate(batches):
        for fi, step in enumerate(cfg['today_flow']):
            if 'batches' in step and batch not in step['batches']:
                continue
            st = stations[step['station']]
            pass_ = str(step['pass'])
            key = st['cycle_keys'][pass_]
            j = Job(st['id'], batch, pass_, cycle_minutes(cfg, key, batch),
                    1, 1000 + bi * 20 + fi, False,
                    bar_label(st, pass_, batch, shorts[batch]))
            if 'start_by' in step:
                j.start_by = tmin(step['start_by'])
            if 'after' in step:
                j.after_specs.append({'station': step['after'], 'today': True})
            add(j)

    # resolve dependencies
    for j in jobs:
        for spec in j.after_specs:
            if isinstance(spec, str):
                spec = {'station': spec}
            batch = spec.get('batch', j.batch)
            st = spec['station']
            if spec.get('today'):
                # today's job for this batch at that station (unique by flow)
                cands = [x for x in jobs if x.station == st and x.batch == batch
                         and not x.hatched]
            elif 'pass' in spec:
                cands = [x for x in jobs if x.station == st and x.batch == batch
                         and x.pass_ == str(spec['pass'])]
            else:
                cands = [x for x in jobs if x.station == st and x.batch == batch]
            if len(cands) > 1:
                # same-generation match wins (carryover -> carryover, today -> today)
                same = [x for x in cands if x.hatched == j.hatched]
                if len(same) == 1:
                    cands = same
            if len(cands) != 1:
                sys.exit(f"config error: dependency {spec} of {j.key()} "
                         f"matches {len(cands)} jobs (need exactly 1)")
            j.preds.append(cands[0])
    return jobs


# ----------------------------------------------------------------------------
# greedy forward scheduler
# ----------------------------------------------------------------------------

def schedule(cfg, jobs):
    day0, day1 = tmin(cfg['day']['start']), tmin(cfg['day']['end'])
    stations = {s['id']: s for s in cfg['stations']}
    crews = cfg['crews']

    def crew_wins(name):
        return [(p, max(day0, a), min(day1, b), brks)
                for p, a, b, brks in crew_windows(crews[name])
                if min(day1, b) > max(day0, a)]

    def avail(name, t):
        """People available at time t: on shift and not on a break."""
        return sum(p for p, a, b, brks in crew_wins(name)
                   if a <= t < b and not any(b0 <= t < b1 for b0, b1 in brks))

    placed = []  # scheduled jobs

    def can_place(j, t):
        st = stations[j.station]
        s, e = t, t + j.dur
        if s < day0 or e > day1:
            return False
        if j.start_by is not None and t > j.start_by:
            return False
        # station lanes
        lanes = st.get('lanes', 1)
        same = [x for x in placed if x.station == j.station
                and x.start < e and x.end > s]
        pts = sorted({s} | {x.start for x in same if s <= x.start < e})
        for p in pts:
            if sum(1 for x in same if x.start <= p < x.end) + 1 > lanes:
                return False
        # crew people available over the whole job (shift windows may change)
        def ppl(x):
            if x.people is not None:
                return x.people
            return stations[x.station].get('people_per_job', 1)

        cname = st['crew']
        ppj = ppl(j)
        crew_sts = [k for k, v in stations.items() if v['crew'] == cname]
        cjobs = [x for x in placed if x.station in crew_sts
                 and x.start < e and x.end > s]
        pts = {s} | {x.start for x in cjobs if s <= x.start < e}
        for _, a, b, brks in crew_wins(cname):
            pts |= {w for w in (a, b) if s < w < e}
            for b0, b1 in brks:
                pts |= {w for w in (b0, b1) if s < w < e}
        for p in sorted(pts):
            used = sum(ppl(x) for x in cjobs if x.start <= p < x.end)
            if used + ppj > avail(cname, p):
                return False
        return True

    def ready(j, t):
        return (j.start is None
                and (j.at is None or t >= j.at)
                and all(p.start is not None and p.end <= t for p in j.preds))

    events = [day0]
    events += [j.at for j in jobs if j.at is not None]
    for c in crews:
        for _, a, b, brks in crew_wins(c):
            events.append(a)                        # each shift's arrival
            events += [b1 for _, b1 in brks]        # work resumes after a break
    heap = sorted(set(events))
    heapq.heapify(heap)
    seen = set(heap)

    while heap:
        t = heapq.heappop(heap)
        if t > day1:
            break
        progress = True
        while progress:
            progress = False
            # all ready jobs across stations, arbitrated by queue priority
            cands = sorted((j for j in jobs if ready(j, t)),
                           key=lambda j: (j.prio, j.seq))
            for j in cands:
                if can_place(j, t):
                    j.start, j.end = t, t + j.dur
                    placed.append(j)
                    if j.end not in seen:
                        seen.add(j.end)
                        heapq.heappush(heap, j.end)
                    progress = True
                    break  # re-evaluate from the top

    spilled = [j for j in jobs if j.start is None]
    return spilled


# ----------------------------------------------------------------------------
# SVG renderer
# ----------------------------------------------------------------------------

GRAY_BAND = '#F2F2F2'
INK = '#1F2937'
MUTE = '#9CA3AF'
MUTE2 = '#6B7280'
GRID = '#E7E7E7'
STAFF_FILL = '#CFE4FC'
STAFF_EDGE = '#7BAED5'

# geometry calibrated to the reference image (px)
X0 = 173.0
PPH = 172.19          # px per hour
Y0 = 76.0             # first station band top
BAND_H = 49.0
PITCH = 57.5
BAR_H = 27.0
BAR_DY = 11.0
BAR_RX = 7.0
BAR_GAP = 1.5         # horizontal inset so adjacent bars show a seam
PX_PER_PERSON = 15.4


def est_w(text, size=12.0):
    return len(text) * size * 0.53


def render_svg(cfg, jobs):
    day0, day1 = tmin(cfg['day']['start']), tmin(cfg['day']['end'])
    stations = cfg['stations']
    crews = cfg['crews']
    colors = {b['name']: b['color'] for b in cfg['batches']}
    accents = {n: darken(c) for n, c in colors.items()}
    nmod = int(cfg.get('modules_per_batch', 4))

    def X(t):
        return X0 + (t - day0) / 60.0 * PPH

    x_end = X(day1)

    def crew_wins(name):
        """Merged staffed coverage intervals of a crew, clipped to the day."""
        ivs = sorted([max(day0, a), min(day1, b)]
                     for _, a, b, _brks in crew_windows(crews[name]))
        merged = []
        for a, b in ivs:
            if b <= a:
                continue
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return merged

    def zero_avail(name):
        """Intervals inside the crew's coverage where nobody is available."""
        wins = [(p, max(day0, a), min(day1, b), brks)
                for p, a, b, brks in crew_windows(crews[name])
                if min(day1, b) > max(day0, a)]
        times = set()
        for _, a, b, brks in wins:
            times |= {a, b}
            times |= {t for bb in brks for t in bb}
        times = sorted(t for t in times if day0 <= t <= day1)
        out = []
        for t0, t1 in zip(times, times[1:]):
            mid = (t0 + t1) / 2
            cov = any(a <= mid < b for _, a, b, _x in wins)
            av = sum(p for p, a, b, brks in wins if a <= mid < b
                     and not any(b0 <= mid < b1 for b0, b1 in brks))
            if cov and av == 0:
                if out and out[-1][1] == t0:
                    out[-1][1] = t1
                else:
                    out.append([t0, t1])
        return out

    style = cfg['staffing'].get('style', 'steps')
    staff_top_gap = 22.0
    last_band_bot = Y0 + (len(stations) - 1) * PITCH + BAND_H
    staff_top = last_band_bot + staff_top_gap
    if style == 'bars':
        # one presence bar per person per shift window, derived from the crews
        LANE_P, LANE_H = 19.0, 14.0
        lanes = []
        for cname, c in crews.items():
            for p, a, b, brks in crew_windows(c):
                a, b = max(day0, a), min(day1, b)
                if b <= a:
                    continue
                for _ in range(p):
                    lanes.append((cname, a, b, brks))
        staff_base = staff_top + 4 + len(lanes) * LANE_P
        allw = [(p, max(day0, a), min(day1, b)) for n in crews
                for p, a, b, _brks in crew_windows(crews[n])]
        starts = {a for _, a, b in allw if b > a}
        peak = max(sum(p for p, a, b in allw if a <= t < b) for t in starts)
    else:
        steps = cfg['staffing']['steps']
        peak = max(s['people'] for s in steps)
        staff_base = staff_top + peak * PX_PER_PERSON
    legend_y = staff_base + 52.0
    W = x_end + 19.0
    H = legend_y + 18.0

    svg = []
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
        f'viewBox="0 0 {W:.0f} {H:.0f}">')
    svg.append("""<style>
 text{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif}
 .title{font-size:15px;font-weight:600;fill:#111827}
 .subtitle{font-size:13px;fill:#6B7280}
 .tick{font-size:13px;fill:#9CA3AF}
 .stlabel{font-size:13.5px;fill:#111827;font-weight:500}
 .stsub{font-size:12px;fill:#9CA3AF}
 .barlabel{font-size:13px;fill:#1F2937;font-weight:500}
 .barcode{font-size:12.5px;fill:#1F2937;font-weight:600}
 .handoff{font-size:12px;fill:#9CA3AF}
 .steplabel{font-size:12px;fill:#374151;font-weight:600}
 .legend{font-size:12px;fill:#4B5563}
 .legendb{font-size:10.5px;fill:#374151;font-weight:700}
</style>""")
    svg.append(f'<rect width="{W:.0f}" height="{H:.0f}" fill="#FFFFFF"/>')

    # hatch patterns
    svg.append('<defs>')
    for i, b in enumerate(cfg['batches']):
        fill, acc = colors[b['name']], accents[b['name']]
        svg.append(
            f'<pattern id="h{i}" width="8" height="8" patternUnits="userSpaceOnUse" '
            f'patternTransform="rotate(45)">'
            f'<rect width="8" height="8" fill="{fill}"/>'
            f'<line x1="1.5" y1="0" x2="1.5" y2="8" stroke="{acc}" stroke-width="3"/>'
            f'</pattern>')
    hatch_id = {b['name']: f'h{i}' for i, b in enumerate(cfg['batches'])}
    # gray hatch for crew breaks in the staffing bars
    svg.append('<pattern id="hbreak" width="6" height="6" '
               'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
               '<rect width="6" height="6" fill="#FFFFFF"/>'
               '<line x1="1" y1="0" x2="1" y2="6" stroke="#D8DBDF" '
               'stroke-width="2.5"/></pattern>')
    svg.append('</defs>')

    # title + subtitle
    cx = (X0 + x_end) / 2
    svg.append(f'<text class="title" x="{cx:.0f}" y="16" text-anchor="middle">'
               f'{cfg["title"]}</text>')
    sub = cfg.get('subtitle') or f'{ampm(day0)}–{ampm(day1)} production window'
    svg.append(f'<text class="subtitle" x="{cx:.0f}" y="33" text-anchor="middle">'
               f'{sub}</text>')
    if cfg.get('note'):
        svg.append(f'<text class="handoff" x="{x_end:.0f}" y="16" '
                   f'text-anchor="end">{cfg["note"]}</text>')

    # gridlines + tick labels (every tick_hours hours, plus the window end)
    step = int(cfg.get('tick_hours', 1) * 60)
    ticks = []
    h = (day0 + step - 1) // step * step
    while h < day1:
        ticks.append((h, fmt12(h)))
        h += step
    ticks.append((day1, fmt12(day1, with_min=(day1 % 60 != 0))))
    if day0 not in [t for t, _ in ticks]:
        ticks.insert(0, (day0, fmt12(day0, with_min=(day0 % 60 != 0))))
    for t, lab in ticks:
        x = X(t)
        svg.append(f'<line x1="{x:.1f}" y1="64" x2="{x:.1f}" y2="{staff_base:.1f}" '
                   f'stroke="{GRID}" stroke-width="1"/>')
        svg.append(f'<text class="tick" x="{x:.0f}" y="54" text-anchor="middle">{lab}</text>')

    # station bands + labels
    band_y = {}
    for i, st in enumerate(stations):
        y = Y0 + i * PITCH
        band_y[st['id']] = y
        for w0, w1 in crew_wins(st['crew']):
            svg.append(f'<rect x="{X(w0):.1f}" y="{y:.1f}" width="{X(w1)-X(w0):.1f}" '
                       f'height="{BAND_H}" rx="6" fill="{GRAY_BAND}"/>')
        for z0, z1 in zero_avail(st['crew']):
            svg.append(f'<rect x="{X(z0):.1f}" y="{y:.1f}" '
                       f'width="{X(z1)-X(z0):.1f}" height="{BAND_H}" '
                       f'fill="#FFFFFF"/>')
        yc = y + BAND_H / 2
        svg.append(f'<text class="stlabel" x="10" y="{yc-2:.0f}">{st["label"]}</text>')
        if st.get('sublabel'):
            svg.append(f'<text class="stsub" x="10" y="{yc+14:.0f}">{st["sublabel"]}</text>')

    # bars
    for j in jobs:
        if j.start is None:
            continue
        y = band_y[j.station] + BAR_DY
        x0b, x1b = X(j.start) + BAR_GAP, X(j.end) - BAR_GAP
        wbar = max(x1b - x0b, 2.0)
        if j.batch not in colors:
            # station downtime block
            svg.append(f'<rect x="{x0b:.1f}" y="{y:.1f}" width="{wbar:.1f}" '
                       f'height="{BAR_H}" rx="{BAR_RX}" fill="url(#hbreak)" '
                       f'stroke="#C3C8CE" stroke-width="1" '
                       f'stroke-dasharray="2 2.5"/>')
            if wbar > est_w(j.label, 12) + 6:
                svg.append(f'<text class="legend" x="{(x0b+x1b)/2:.1f}" '
                           f'y="{y+BAR_H/2+4.5:.1f}" text-anchor="middle">'
                           f'{j.label}</text>')
            continue
        fill = f'url(#{hatch_id[j.batch]})' if j.hatched else colors[j.batch]
        svg.append(f'<rect x="{x0b:.1f}" y="{y:.1f}" width="{wbar:.1f}" '
                   f'height="{BAR_H}" rx="{BAR_RX}" fill="{fill}"/>')
        st = next(s for s in stations if s['id'] == j.station)
        if st.get('module_ticks') and nmod > 1:
            acc = accents[j.batch]
            for k in range(1, nmod):
                xt = x0b + wbar * k / nmod
                svg.append(f'<line x1="{xt:.1f}" y1="{y+1.5:.1f}" x2="{xt:.1f}" '
                           f'y2="{y+BAR_H-1.5:.1f}" stroke="{acc}" stroke-width="1.8"/>')
        cls = 'barlabel' if st.get('label_style', 'batch') == 'batch' else 'barcode'
        if wbar > est_w(j.label, 13) + 6:
            svg.append(f'<text class="{cls}" x="{(x0b+x1b)/2:.1f}" y="{y+BAR_H/2+4.5:.1f}" '
                       f'text-anchor="middle">{j.label}</text>')

    # handoff markers
    ho = cfg.get('handoff')
    if ho:
        xh = X(tmin(ho['time']))
        for sid in ho['stations']:
            y = band_y[sid]
            svg.append(f'<line x1="{xh:.1f}" y1="{y-2:.1f}" x2="{xh:.1f}" '
                       f'y2="{y+BAND_H+12:.1f}" stroke="#B6BBC2" stroke-width="1.5" '
                       f'stroke-dasharray="2 3.5"/>')
            svg.append(f'<text class="handoff" x="{xh+7:.1f}" y="{y-6:.1f}">handoff</text>')

    # staffing: per-person presence bars, or the classic step chart
    if style == 'bars':
        for k, (cname, s0, s1, brks) in enumerate(lanes):
            y = staff_top + 4 + k * LANE_P
            segs = [(s0, s1)]
            for b0, b1 in brks:
                nxt = []
                for a, b in segs:
                    if a < b0:
                        nxt.append((a, min(b, b0)))
                    if b > b1:
                        nxt.append((max(a, b1), b))
                segs = nxt
            for si, (a, b) in enumerate(segs):
                svg.append(f'<rect x="{X(a):.1f}" y="{y:.1f}" '
                           f'width="{X(b)-X(a):.1f}" height="{LANE_H}" rx="4" '
                           f'fill="{STAFF_FILL}" stroke="{STAFF_EDGE}" '
                           f'stroke-width="1"/>')
                if si == 0:
                    svg.append(f'<text class="legend" x="{X(a)+8:.1f}" '
                               f'y="{y+LANE_H/2+4:.1f}">{cname}</text>')
            # breaks drawn as hatched notches inside the shift
            for b0, b1 in brks:
                bb0, bb1 = max(b0, s0), min(b1, s1)
                if bb1 > bb0:
                    svg.append(f'<rect x="{X(bb0):.1f}" y="{y:.1f}" '
                               f'width="{X(bb1)-X(bb0):.1f}" height="{LANE_H}" '
                               f'rx="4" fill="url(#hbreak)" '
                               f'stroke="#C3C8CE" stroke-width="1" '
                               f'stroke-dasharray="2 2.5"/>')
    else:
        pts = []
        for i, s in enumerate(steps):
            t0 = tmin(s['from'])
            t1 = tmin(steps[i + 1]['from']) if i + 1 < len(steps) else day1
            pts.append((t0, t1, s['people']))
        path = f'M {X(pts[0][0]):.1f} {staff_base:.1f} '
        for (t0, t1, n) in pts:
            ytop = staff_base - n * PX_PER_PERSON
            path += f'L {X(t0):.1f} {ytop:.1f} L {X(t1):.1f} {ytop:.1f} '
        path += f'L {X(pts[-1][1]):.1f} {staff_base:.1f} Z'
        svg.append(f'<path d="{path}" fill="{STAFF_FILL}" stroke="{STAFF_EDGE}" '
                   f'stroke-width="1.2"/>')
        for (t0, t1, n) in pts:
            ytop = staff_base - n * PX_PER_PERSON
            svg.append(f'<text class="steplabel" x="{X(t0)+5:.1f}" y="{ytop+13:.1f}">{n}</text>')
    lab = cfg['staffing'].get('label', 'People on shift')
    sublab = cfg['staffing'].get('sublabel', f'peak {peak} at once')
    yc = (staff_top + staff_base) / 2
    svg.append(f'<text class="stlabel" x="10" y="{yc-2:.0f}">{lab}</text>')
    svg.append(f'<text class="stsub" x="10" y="{yc+14:.0f}">{sublab}</text>')

    # legend — batches on the left
    x = 8.0
    ly = legend_y
    for b in cfg['batches']:
        svg.append(f'<rect x="{x:.1f}" y="{ly-10:.1f}" width="12" height="12" rx="3" '
                   f'fill="{colors[b["name"]]}"/>')
        svg.append(f'<text class="legend" x="{x+17:.1f}" y="{ly:.1f}">{b["name"]}</text>')
        x += 17 + est_w(b['name']) + 18

    # legend — semantics on the right
    first_batch = cfg['batches'][4]['name'] if len(cfg['batches']) > 4 \
        else cfg['batches'][0]['name']
    items = []   # (draw_fn(x)->None, width)

    def chip(fill):
        def draw(x):
            svg.append(f'<rect x="{x:.1f}" y="{ly-10:.1f}" width="12" height="12" rx="3" '
                       f'fill="{fill}"/>')
        return draw, 12

    def boxletter(ch):
        def draw(x):
            svg.append(f'<rect x="{x:.1f}" y="{ly-11:.1f}" width="14" height="14" rx="3" '
                       f'fill="#EFEFEF"/>')
            svg.append(f'<text class="legendb" x="{x+7:.1f}" y="{ly:.1f}" '
                       f'text-anchor="middle">{ch}</text>')
        return draw, 14

    def minibar():
        def draw(x):
            svg.append(f'<rect x="{x:.1f}" y="{ly-10:.1f}" width="20" height="12" rx="3" '
                       f'fill="{colors[cfg["batches"][0]["name"]]}"/>')
            acc = accents[cfg['batches'][0]['name']]
            for k in (1, 2, 3):
                svg.append(f'<line x1="{x+20*k/4:.1f}" y1="{ly-9:.1f}" '
                           f'x2="{x+20*k/4:.1f}" y2="{ly+1:.1f}" stroke="{acc}" '
                           f'stroke-width="1.5"/>')
        return draw, 20

    def hatchchip():
        def draw(x):
            svg.append(f'<rect x="{x:.1f}" y="{ly-10:.1f}" width="12" height="12" rx="3" '
                       f'fill="url(#{hatch_id[first_batch]})"/>')
        return draw, 12

    legend_right = [
        (chip('#EFEFEF'), 'staffed shift'),
        (hatchchip(), 'prior-day / older WIP'),
        (boxletter('B'), 'backside'),
        (boxletter('F'), 'frontside'),
        (minibar(), f'{"four" if nmod == 4 else nmod} sequential modules'),
    ]
    total = sum(w + 5 + est_w(txt) + 16 for (fn, w), txt in legend_right) - 16
    x = W - 10 - total
    for (fn, w), txt in legend_right:
        fn(x)
        svg.append(f'<text class="legend" x="{x+w+5:.1f}" y="{ly:.1f}">{txt}</text>')
        x += w + 5 + est_w(txt) + 16

    svg.append('</svg>')
    return '\n'.join(svg), W, H


# ----------------------------------------------------------------------------
# PNG export
# ----------------------------------------------------------------------------

def svg_to_png(svg_path, png_path, w, h, scale=2):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        import subprocess
        print('playwright not available; falling back to ImageMagick', file=sys.stderr)
        subprocess.run(['magick', '-density', str(96 * scale), svg_path, png_path],
                       check=True)
        return
    html_path = svg_path + '.tmp.html'
    with open(svg_path) as f:
        svg_text = f.read()
    with open(html_path, 'w') as f:
        f.write(f"<!doctype html><html><head><meta charset='utf-8'></head>"
                f"<body style='margin:0;background:#fff'>{svg_text}</body></html>")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                viewport={'width': int(w) + 2, 'height': int(h) + 2},
                device_scale_factor=scale)
            page.goto('file://' + os.path.abspath(html_path))
            page.locator('svg').screenshot(path=png_path)
            browser.close()
    finally:
        os.unlink(html_path)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def apply_cycle_csv(cfg, path):
    """Override cycle times from a flat CSV: columns cycle,minutes[,batch].

    A row with an empty batch (or 'default') sets the default for that key —
    sheet-friendly so colleagues can edit assumptions in Google Sheets and
    export as CSV.
    """
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            key = row['cycle'].strip()
            minutes = float(row['minutes'])
            batch = (row.get('batch') or '').strip()
            cur = cfg['cycle_times_min'].get(key)
            if not batch or batch.lower() == 'default':
                if isinstance(cur, dict):
                    cur['default'] = minutes
                else:
                    cfg['cycle_times_min'][key] = minutes
            else:
                if not isinstance(cur, dict):
                    cfg['cycle_times_min'][key] = cur = {'default': cur if cur is not None else minutes}
                cur[batch] = minutes


def apply_num_batches(cfg, n):
    """Keep only the first n batches; drop carryover for removed batches."""
    if not n or n >= len(cfg['batches']):
        return
    cfg['batches'] = cfg['batches'][:n]
    keep = {b['name'] for b in cfg['batches']}
    cfg['carryover'] = [c for c in cfg.get('carryover', [])
                        if c['batch'] in keep]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('config', help='scenario YAML file')
    ap.add_argument('-o', '--out', help='output basename (default from config name)')
    ap.add_argument('--cycle-csv', help='flat CSV of cycle-time overrides')
    ap.add_argument('--num-batches', type=int,
                    help='override num_batches from the config')
    ap.add_argument('--no-png', action='store_true', help='skip PNG export')
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    if args.cycle_csv:
        apply_cycle_csv(cfg, args.cycle_csv)
    apply_num_batches(cfg, args.num_batches or cfg.get('num_batches'))

    out = args.out
    if not out:
        out = os.path.splitext(os.path.basename(args.config))[0]
        out = re.sub(r'^schedule_config_', '', out)
    outdir = os.path.dirname(os.path.abspath(args.config))

    jobs = build_jobs(cfg)
    spilled = schedule(cfg, jobs)

    # console summary
    print(f"# {cfg['title']}  ({cfg['day']['start']}-{cfg['day']['end']})")
    for st in cfg['stations']:
        sched = sorted((j for j in jobs if j.station == st['id'] and j.start is not None),
                       key=lambda j: j.start)
        line = '  '.join(f"{j.label}[{fmt(j.start)}-{fmt(j.end)}]"
                         + ('*' if j.hatched else '') for j in sched)
        print(f"{st['label']:24s} {line}")
    if spilled:
        print('spilled to next day (did not fit the staffed window):')
        for j in spilled:
            print(f"  {j.station:14s} {j.label}"
                  + (' (carryover)' if j.hatched else ''))

    svg, w, h = render_svg(cfg, jobs)
    svg_path = os.path.join(outdir, out + '.svg')
    with open(svg_path, 'w') as f:
        f.write(svg)
    print('wrote', svg_path)
    if not args.no_png:
        png_path = os.path.join(outdir, out + '.png')
        svg_to_png(svg_path, png_path, w, h)
        print('wrote', png_path)


if __name__ == '__main__':
    main()
