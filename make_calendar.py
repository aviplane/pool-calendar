"""Scrape Harvard and MIT pool schedules and write open swim hours to pool.ics.

Harvard: MAC and Blodgett pool hours from the Harvard Rec facility hours page.
MIT: Z Center and Alumni Pool, shown as open whenever at least MIT_MIN_LANES
lanes across the facility are marked "Open Rec Swim".
"""
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser

URL = "https://recreation.gocrimson.com/sports/2021/5/14/facility-hours.aspx"
POOLS = ["MAC Pool", "Blodgett Pool"]
LOCATIONS = {"MAC Pool": "Malkin Athletic Center, 39 Holyoke St, Cambridge, MA",
             "Blodgett Pool": "Blodgett Pool, 65 N Harvard St, Boston, MA"}
TZ = "America/New_York"

MIT_MIN_LANES = 3  # facility is "open" when at least this many lanes are rec swim
MIT_WEEKS = 4
MIT_FACILITIES = [
    # (name, schedule url, location, {data-pool: sub-pool label})
    ("Z Center", "https://www.mitrecsports.com/pool-schedule-z/",
     "MIT Zesiger Center, 120 Vassar St, Cambridge, MA",
     {"competition-pool": "Competition", "dive-well-pool": "Dive Well", "teaching-pool": "Teaching"}),
    ("Alumni Pool", "https://www.mitrecsports.com/pool-schedule-aw/",
     "MIT Alumni Pool, 32 Vassar St, Cambridge, MA",
     {"alumni-pool-gallery": "Lap", "alumni-teaching-pool": "Teaching"}),
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


class TableParser(HTMLParser):
    """Collects every table as rows of cells; each cell is a list of paragraph strings."""

    def __init__(self):
        super().__init__()
        self.tables, self.row, self.cell, self.para = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell, self.para = [], []
        elif tag in ("p", "br") and self.cell is not None:
            self._flush_para()

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self._flush_para()
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.tables:
                self.tables[-1].append(self.row)
            self.row = None
        elif tag == "p" and self.cell is not None:
            self._flush_para()

    def handle_data(self, data):
        if self.para is not None:
            self.para.append(data)

    def _flush_para(self):
        text = " ".join("".join(self.para).replace("\xa0", " ").split())
        if text:
            self.cell.append(text)
        self.para = []


def find_pool_table(tables):
    for t in tables:
        header = [" ".join(c) for c in t[0]] if t else []
        if all(any(p in h for h in header) for p in POOLS):
            return t, [next(i for i, h in enumerate(header) if p in h) for p in POOLS]
    raise RuntimeError("Pool table not found -- page layout may have changed")


TIME_RE = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|noon)?"
RANGE_RE = re.compile(TIME_RE + r"\s*[-–]\s*" + TIME_RE, re.I)


def to_time(h, m, ap):
    h, m, ap = int(h), int(m or 0), (ap or "").lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h, m


def parse_ranges(text):
    out = []
    for h1, m1, a1, h2, m2, a2 in RANGE_RE.findall(text):
        a1 = a1 or a2  # "10-11am"
        out.append((to_time(h1, m1, a1), to_time(h2, m2, a2)))
    return out


def infer_date(m, d, today):
    candidates = [date(today.year + dy, m, d) for dy in (-1, 0, 1)]
    return min(candidates, key=lambda c: abs((c - today).days))


def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")


def build_events(table, cols, today):
    events = []
    for row in table[1:]:
        if not row or not row[0]:
            continue
        md = re.search(r"\d{1,2}/\d{1,2}", " ".join(row[0]))
        if not md:
            continue
        day = infer_date(*map(int, md.group().split("/")), today)
        for pool, ci in zip(POOLS, cols):
            if ci >= len(row) or not row[ci]:
                continue
            hours, notes = row[ci][0], row[ci][1:]
            short = pool.replace(" Pool", "")
            uid_base = f"harvard-{day:%Y%m%d}-{short.lower()}"
            ranges = parse_ranges(hours)
            if "closed" in hours.lower() and not ranges:
                events.append(dict(uid=f"{uid_base}-closed", day=day, allday=True,
                                   summary=f"{short} Pool CLOSED", desc="\n".join(notes),
                                   location=LOCATIONS[pool], source=URL))
                continue
            for i, ((h1, m1), (h2, m2)) in enumerate(ranges):
                start = datetime(day.year, day.month, day.day, h1, m1)
                end = datetime(day.year, day.month, day.day, h2, m2)
                if end <= start:
                    end += timedelta(days=1)
                events.append(dict(uid=f"{uid_base}-{i}", start=start, end=end,
                                   summary=f"{short} Pool" + (" ⚠️" if notes else ""),
                                   desc="\n".join([f"Hours: {hours}"] + notes),
                                   location=LOCATIONS[pool], source=URL))
    return events


# One output calendar per facility, keyed by the UID facility slug.
CALENDARS = [
    ("mac.ics", "MAC Pool (Harvard)", "harvard-mac"),
    ("blodgett.ics", "Blodgett Pool (Harvard)", "harvard-blodgett"),
    ("mit-zcenter.ics", "Z Center Pool (MIT)", "mit-zcenter"),
    ("mit-alumni.ics", "Alumni Pool (MIT)", "mit-alumnipool"),
]
UID_KEY_RE = re.compile(r"^(harvard|mit)-\d{8}-([a-z]+)")


def uid_key(uid):
    m = UID_KEY_RE.match(uid)
    return f"{m.group(1)}-{m.group(2)}" if m else ""


def to_ics(events, calname):
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//harvard-pool-calendar//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{calname}",
             f"X-WR-TIMEZONE:{TZ}", "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
             "X-PUBLISHED-TTL:PT6H", *VTIMEZONE.splitlines()]
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}@harvard-pool-calendar", f"DTSTAMP:{stamp}"]
        if e.get("allday"):
            lines += [f"DTSTART;VALUE=DATE:{e['day']:%Y%m%d}",
                      f"DTEND;VALUE=DATE:{e['day'] + timedelta(days=1):%Y%m%d}"]
        else:
            lines += [f"DTSTART;TZID={TZ}:{e['start']:%Y%m%dT%H%M%S}",
                      f"DTEND;TZID={TZ}:{e['end']:%Y%m%dT%H%M%S}"]
        lines += [f"SUMMARY:{esc(e['summary'])}", f"LOCATION:{esc(e['location'])}",
                  f"DESCRIPTION:{esc(e['desc'] + chr(10) + chr(10) + 'Source: ' + e['source'])}",
                  "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")


def harvard_events(today):
    p = TableParser()
    p.feed(fetch(URL))
    table, cols = find_pool_table(p.tables)
    return build_events(table, cols, today)


MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
LI_RE = re.compile(r"<li\b([^>]*\bdata-time-start=[^>]*)>")


def attr(tag, name):
    m = re.search(rf'{name}="([^"]*)"', tag)
    return unescape(m.group(1)) if m else ""


def hhmm(s):
    return int(s[:2]) * 60 + int(s[2:])


def parse_mit_week(html, subpools, today):
    """Return {date: {subpool_label: [lane_open_minutes(set), ...]}} for one week page."""
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    titles = dict(re.findall(r'<h3 class="daytitle[^"]*" id="(\w+)-title">([^<]+)<', html))
    days = {}
    for chunk in re.split(r'class="pool-schedule-table[^"]*"\s*id="', html)[1:]:
        dayid = chunk[:chunk.index('"')]
        m = re.search(r"(\w+) (\d{1,2})", titles.get(dayid, "").split(",")[-1])
        if not m or m.group(1).lower() not in MONTHS:
            continue
        day = infer_date(MONTHS[m.group(1).lower()], int(m.group(2)), today)
        pools = {}
        for ls in re.split(r'class="lane-schedule" data-pool="', chunk)[1:]:
            label = subpools.get(ls[:ls.index('"')])
            if not label:
                continue
            lanes = []
            for lane in ls.split('<ul class="pool-classes">')[1:]:
                open_min = set()
                for tag in LI_RE.findall(lane.split("</ul>")[0]):
                    if attr(tag, "data-tool-title").strip().lower() == "open rec swim":
                        open_min.update(range(hhmm(attr(tag, "data-time-start")),
                                              hhmm(attr(tag, "data-time-end"))))
                lanes.append(open_min)
            pools.setdefault(label, []).extend(lanes)
        if pools:
            days[day] = pools
    return days


def fmt_time(minute):
    h, m = divmod(minute, 60)
    return f"{(h - 1) % 12 + 1}{':%02d' % m if m else ''}{'am' if h < 12 or h == 24 else 'pm'}"


def mit_events(today):
    events = []
    for name, url, location, subpools in MIT_FACILITIES:
        days = {}
        for week in range(1, MIT_WEEKS + 1):
            try:
                days.update(parse_mit_week(fetch(f"{url}?week={week}"), subpools, today))
            except Exception as e:
                if week == 1:
                    raise
                print(f"{name} week {week} failed: {e}", file=sys.stderr)
            time.sleep(1)
        if not days:
            raise RuntimeError(f"No MIT schedule parsed for {name}")
        for day, pools in sorted(days.items()):
            # counts[minute] = (lanes open per sub-pool)
            counts = [tuple(sum(minute in lane for lane in lanes) for lanes in pools.values())
                      for minute in range(24 * 60)]
            minute = 0
            while minute < 24 * 60:
                if sum(counts[minute]) < MIT_MIN_LANES:
                    minute += 1
                    continue
                start = minute
                while minute < 24 * 60 and sum(counts[minute]) >= MIT_MIN_LANES:
                    minute += 1
                segs, s = [], start
                for t in range(start + 1, minute + 1):
                    if t == minute or counts[t] != counts[s]:
                        detail = ", ".join(f"{lbl} {n}" for lbl, n in zip(pools, counts[s]))
                        segs.append(f"{fmt_time(s)}-{fmt_time(t)}: {sum(counts[s])} lanes ({detail})")
                        s = t
                totals = [sum(c) for c in counts[start:minute]]
                lo, hi = min(totals), max(totals)
                lanes_txt = f"{lo} lanes" if lo == hi else f"{lo}-{hi} lanes"
                d0 = datetime(day.year, day.month, day.day)
                events.append(dict(
                    uid=f"mit-{day:%Y%m%d}-{name.lower().replace(' ', '')}-{start}",
                    start=d0 + timedelta(minutes=start), end=d0 + timedelta(minutes=minute),
                    summary=f"MIT {name} rec swim ({lanes_txt})", location=location,
                    source=f"{url}?week=1", desc="Open Rec Swim lanes:\n" + "\n".join(segs)))
    return events


def old_events(path, prefix):
    """{uid: VEVENT block} for UIDs with the given prefix from a previous calendar file."""
    if not os.path.exists(path):
        return {}
    text = open(path, encoding="utf-8").read()
    blocks = re.findall(r"BEGIN:VEVENT\r?\n.*?END:VEVENT", text, flags=re.S)
    out = {}
    for b in blocks:
        m = re.search(r"^UID:(\S+?)@", b, flags=re.M)
        if m and m.group(1).startswith(prefix + "-"):
            out[m.group(1)] = b
    return out


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "."
    today = date.today()
    events, kept, ok = [], {}, 0
    for prefix, source in (("harvard", harvard_events), ("mit", mit_events)):
        try:
            evs = source(today)
            if not evs:
                raise RuntimeError("no events parsed")
            events += evs
            ok += 1
            print(f"{prefix}: {len(evs)} events")
        except BaseException as e:
            old = {}
            for filename, _, _ in CALENDARS:
                old.update(old_events(os.path.join(outdir, filename), prefix))
            kept.update(old)
            print(f"{prefix} FAILED ({e!r}); keeping {len(old)} previous events", file=sys.stderr)
    if not ok:
        sys.exit("All sources failed -- refusing to overwrite calendar")
    for filename, calname, key in CALENDARS:
        evs = [e for e in events if uid_key(e["uid"]) == key]
        old = [b for uid, b in kept.items() if uid_key(uid) == key]
        ics = to_ics(evs, calname)
        if old:
            ics = ics.replace("END:VCALENDAR", "\r\n".join(old) + "\r\nEND:VCALENDAR")
        with open(os.path.join(outdir, filename), "w", encoding="utf-8", newline="") as f:
            f.write(ics)
        print(f"Wrote {len(evs) + len(old)} events to {filename}")


if __name__ == "__main__":
    main()
