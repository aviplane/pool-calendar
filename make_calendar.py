"""Scrape Harvard Rec facility hours and write MAC/Blodgett pool hours to pool.ics."""
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

URL = "https://recreation.gocrimson.com/sports/2021/5/14/facility-hours.aspx"
POOLS = ["MAC Pool", "Blodgett Pool"]
TZ = "America/New_York"

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
    sys.exit("Pool table not found -- page layout may have changed")


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


def infer_date(md, today):
    m, d = map(int, md.split("/"))
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
        day = infer_date(md.group(), today)
        for pool, ci in zip(POOLS, cols):
            if ci >= len(row) or not row[ci]:
                continue
            hours, notes = row[ci][0], row[ci][1:]
            short = pool.replace(" Pool", "")
            uid_base = f"{day:%Y%m%d}-{short.lower()}"
            ranges = parse_ranges(hours)
            if "closed" in hours.lower() and not ranges:
                events.append(dict(uid=f"{uid_base}-closed", day=day, allday=True,
                                   summary=f"{short} Pool CLOSED", desc="\n".join(notes)))
                continue
            for i, ((h1, m1), (h2, m2)) in enumerate(ranges):
                start = datetime(day.year, day.month, day.day, h1, m1)
                end = datetime(day.year, day.month, day.day, h2, m2)
                if end <= start:
                    end += timedelta(days=1)
                events.append(dict(uid=f"{uid_base}-{i}", start=start, end=end,
                                   summary=f"{short} Pool" + (" ⚠️" if notes else ""),
                                   desc="\n".join([f"Hours: {hours}"] + notes)))
    return events


def to_ics(events):
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//harvard-pool-calendar//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Harvard Pool Hours",
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
        lines += [f"SUMMARY:{esc(e['summary'])}", f"LOCATION:{esc(e['summary'].split(' Pool')[0])} Pool",
                  f"DESCRIPTION:{esc(e['desc'] + chr(10) + chr(10) + 'Source: ' + URL)}",
                  "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    p = TableParser()
    p.feed(html)
    table, cols = find_pool_table(p.tables)
    events = build_events(table, cols, date.today())
    if not events:
        sys.exit("No pool events parsed -- refusing to overwrite calendar")
    out = sys.argv[1] if len(sys.argv) > 1 else "pool.ics"
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write(to_ics(events))
    print(f"Wrote {len(events)} events to {out}")


if __name__ == "__main__":
    main()
