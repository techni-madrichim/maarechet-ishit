"""Scan the school timetable once and build every instructor's personal page data.

Usage: python3 tracker/build.py [--offline] [--morning]

Output (served by GitHub Pages from docs/):
  docs/index.html           the app; the visitor picks their name once
  docs/data/staff.json      who can be picked
  docs/data/<id>.json       one instructor's lessons, changes and journal
"""

import argparse
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import (diff, dm, fill, homeroom_line, load_json, morning_summary, notify_ntfy,  # noqa: E402
                 todays_changes, write_json)
from scrape import ISRAEL_TZ, collect, fetch_agenda, homeroom_info, scrape  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
MAX_CHANGELOG = 150
HISTORY_DAYS = 90
WEEKDAYS = {"ראשון": 0, "שני": 1, "שלישי": 2, "רביעי": 3, "חמישי": 4, "שישי": 5,
            "א": 0, "ב": 1, "ג": 2, "ד": 3, "ה": 4, "ו": 5}


def person_id(person):
    return hashlib.sha1((person.get("name") or person["as_given"]).encode("utf-8")).hexdigest()[:10]


def pin_hash(pid, pin):
    return hashlib.sha256(f"{pid}:{pin}".encode()).hexdigest()


def ntfy_topic(person):
    """Per-person topic derived from a secret, so it can't be guessed from the public repository."""
    salt = os.environ.get("NTFY_SALT", "")
    if not salt:
        return None
    digest = hmac.new(salt.encode(), (person.get("name") or person["as_given"]).encode("utf-8"), hashlib.sha256)
    return "techni-" + digest.hexdigest()[:20]


HEADERS = {"name": "שם המדריך", "type": "סוג", "day": "יום קבוע", "date": "או תאריך חד-פעמי",
           "from": "משעה", "to": "עד שעה", "subject": "נושא / תלמיד", "class": "כיתה", "room": "חדר",
           "notes": "הערות"}


def sheet_csv_url(sheet_id, tab):
    return (f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet="
            + urllib.parse.quote(tab))


def read_sheet_tab(tab, must_have):
    """Rows of one tab of the management sheet as dicts keyed by the header row, or None if unreachable.

    Tries the main sheet, then the older one. A tab is accepted only if its header has `must_have`:
    Google returns the first tab when a tab name doesn't exist, which must not be read as data.
    """
    for sheet in [CONFIG.get("sheet_id"), CONFIG.get("fallback_sheet_id")]:
        if not sheet:
            continue
        try:
            req = urllib.request.Request(sheet_csv_url(sheet, tab), headers={"User-Agent": "Mozilla/5.0 (lesson-tracker)"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                table = list(csv.reader(io.StringIO(resp.read().decode("utf-8-sig"))))
        except Exception as e:  # a broken sheet must not stop the timetable update
            print(f"sheet {sheet[:8]}… tab '{tab}' not loaded: {e}")
            continue
        header = [h.strip() for h in table[0]] if table else []
        if must_have not in header:
            print(f"sheet {sheet[:8]}… has no tab '{tab}'")
            continue
        return [{h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(header) if h} for row in table[1:]]
    return None


def role_label(role):
    """The staff call coordinators "ראש צוות", whatever the sheet says."""
    return re.sub(r"^רכז(ת)?\s+", "ראש צוות ", role.strip())


def class_key(class_name):
    """"ט' - 3", "ט'3" and "ט 3" all -> "ט3"."""
    return re.sub(r"[^א-ת0-9]", "", class_name)


def grade_of(class_name):
    """"יא'- 2" -> "יא", "י - 6" -> "י"."""
    return re.sub(r"[^א-ת]", "", class_name.split("-")[0])


def site_teachers(raw):
    names = set()
    for _, cell in [e for entries in raw["slots"].values() for e in entries] + raw["regular"]:
        for l in cell["lessons"]:
            names.update(t.strip() for t in l["teacher"].split(",") if t.strip())
    return names


def load_staff(raw):
    """People and teams from the sheet's staff tab, falling back to staff.json."""
    local = json.loads((ROOT / "staff.json").read_text(encoding="utf-8"))
    rows = read_sheet_tab(CONFIG.get("staff_tab", "אנשי צוות"), "שם (כמו בשחף: משפחה ואז פרטי)")
    if not rows:
        return local["staff"], local.get("teams", {})
    known = site_teachers(raw) if raw else set()
    by_tokens = {frozenset(t.split()): t for t in known}
    staff, teams = [], {}
    for r in rows:
        typed = " ".join(r.get("שם (כמו בשחף: משפחה ואז פרטי)", "").split())
        if not typed or r.get("פעיל", "כן") == "לא":
            continue
        # Exact name as on the site, or the same words in another order (e.g. first name first).
        name = typed if typed in known else by_tokens.get(frozenset(typed.split()))
        if raw and not name:
            print(f"note: '{typed}' has no lessons on the school site")
        homerooms = [h for h in [r.get("כיתת חינוך", ""), r.get("כיתת חינוך נוספת", "")] if h]
        code = r.get("קוד", "").strip()
        person = {"name": name, "as_given": typed, "display": name or typed, "code": code,
                  "roles": [role_label(x) for x in r.get("תפקיד", "").split(",") if x.strip()]}
        if homerooms:
            person["homeroom"], person["homeroom_extra"] = homerooms[0], homerooms[1:]
        staff.append(person)
        team = r.get("צוות", "")
        if team:
            t = teams.setdefault(team, {"lead": None, "members": []})
            t["members"].append(person["display"])
            if r.get("ראש צוות") == "כן":
                t["lead"] = person["display"]
    for r in read_sheet_tab(CONFIG.get("teams_tab", "צוותים"), "שם הצוות") or []:
        if r.get("שם הצוות") in teams:
            teams[r["שם הצוות"]]["grades"] = [g.strip(" '\"") for g in r.get("שכבות", "").split(",") if g.strip()]
            teams[r["שם הצוות"]]["commander"] = " ".join(r.get("מפקד", "").split())
    print(f"staff from sheet: {len(staff)} people, {len(teams)} teams")
    return staff, teams


def load_extras(teams=None):
    """Extra lessons the school site doesn't list (tutoring, mentoring...), from the Google Sheet and/or extras.json.
    A row whose name is a team ("צוות ט") goes to everyone in that team and to its commander (a team meeting)."""
    rows = list(load_json(ROOT / "extras.json", []))
    for r in read_sheet_tab(CONFIG.get("extras_sheet_tab", "תוספות"), HEADERS["name"]) or []:
        row = {k: r.get(v, "") for k, v in HEADERS.items()}
        if row["name"]:
            rows.append(row)
    by_name = {}
    for r in rows:
        nums = [int(n) for n in re.findall(r"\d+", f"{r.get('from', '')} {r.get('to', '')} {r.get('hours', '')}")]
        kind, subject = r.get("type", ""), r.get("subject", "")
        what = subject if kind and subject.startswith(kind) else " · ".join(x for x in [kind, subject] if x)
        if not nums or not what or "דוגמה" in (r.get("notes") or ""):  # example rows stay in the sheet only
            continue
        extra = {"hours": list(range(min(nums), max(nums) + 1)), "subject": what,
                 "classes": [r["class"]] if r.get("class") else [], "rooms": [r["room"]] if r.get("room") else []}
        m = re.match(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", r.get("date", "") or "")
        if m:
            year = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
            extra["date"] = f"{year}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        elif (r.get("day") or "").replace("יום", "").strip(" '\"") in WEEKDAYS:
            extra["weekday"] = WEEKDAYS[r["day"].replace("יום", "").strip(" '\"")]
        else:
            continue
        # Keyed by the set of name words, so "מאי כהן" and "כהן מאי" are the same person.
        team = (teams or {}).get(" ".join(r["name"].split()))
        who = (team["members"] + ([team["commander"]] if team.get("commander") else [])) if team else [r["name"]]
        for n in dict.fromkeys(who):
            by_name.setdefault(frozenset(n.split()), []).append(extra)
    return by_name


def add_grade_agenda(pid, grades, raw, now):
    """A commander's "exams and events" tab shows every exam and event of the team's grades."""
    path = DOCS / "data" / f"{pid}.json"
    data = load_json(path, {})
    if not data:
        return
    today = now.date().isoformat()
    agenda = {}
    for c in raw["classes"]:
        if grade_of(c["name"]) not in grades:
            continue
        for tab, kind in (("exams", "מבחן"), ("events", "אירוע")):
            try:
                items = fetch_agenda(c["id"], tab, kind)
            except Exception as err:  # noqa: BLE001 - one class failing must not stop the build
                print(f"note: agenda of {c['name']} not read ({err})")
                continue
            for it in items:
                if it["date"] and it["date"] < today:
                    continue
                e = agenda.setdefault((it["kind"], it["date"], it["title"], tuple(it["hours"] or ())), {**it, "my_classes": [], "my_lessons": []})
                if c["name"] not in e["my_classes"]:
                    e["my_classes"].append(c["name"])
    data["grade_agenda"] = sorted(agenda.values(), key=lambda a: (a["date"] or "", a["hours"] or [0], a["title"]))
    write_json(path, data)


def build_person(person, raw, extras, now, offline, morning):
    pid = person_id(person)
    path = DOCS / "data" / f"{pid}.json"
    saved = load_json(path, {})
    previous = saved.get("latest", {})
    changelog = saved.get("changelog", [])
    history = {d["date"]: d for d in saved.get("history", [])}
    today = now.date().isoformat()
    name = person.get("name")
    display = person.get("display") or name or " ".join(reversed(person["as_given"].split(" ", 1)))
    homerooms = [h for h in [person.get("homeroom")] + person.get("homeroom_extra", []) if h]

    if offline:
        if not previous:
            return None
        latest, found = previous, []
    else:
        keys = {frozenset(display.split()), frozenset(person["as_given"].split())}
        mine = [e for k in keys for e in extras.get(k, [])]
        # Classes typed by hand ("ט'3", "ט 3") are written as the site writes them ("ט' - 3").
        site_class = {class_key(c["name"]): c["name"] for c in raw["classes"]}
        mine = [{**e, "classes": [site_class.get(class_key(c), c) for c in e["classes"]]} for e in mine]
        latest = scrape(name or "\u0000", raw=raw, extra_lessons=mine, homeroom=homerooms[0] if homerooms else None)
        latest["homerooms"] = [homeroom_info(raw, h) for h in homerooms]
        # The page's "end of day" table: the homeroom classes, or for a team lead every class of their grades.
        grades = person.get("lead_grades", [])
        shown = [c["name"] for c in raw["classes"] if grade_of(c["name"]) in grades] if grades else []
        latest["class_end"] = {}
        for hr in latest["homerooms"] + [homeroom_info(raw, c) for c in shown if c not in homerooms]:
            for day, info in hr["days"].items():
                latest["class_end"].setdefault(day, []).append(
                    {"class": hr["class"], "now": info.get("last_lesson"), "regular": info.get("regular_end")})
        for rows in latest["class_end"].values():
            rows.sort(key=lambda r: (len(grade_of(r["class"])), r["class"]))
        found = diff(previous, latest, today) if previous else []
        for i, hr in enumerate(latest["homerooms"][1:], start=1):
            old = (previous.get("homerooms") or [None] * (i + 1))[i] if previous else None
            if old:
                found += diff({"lessons": [], "days": previous.get("days", []), "homeroom": old, "class_changes": []},
                              {"lessons": [], "days": latest["days"], "homeroom": hr, "class_changes": []}, today)
        for f in found:
            f["detected_at"] = latest["scraped_at"]
        changelog = (found + changelog)[:MAX_CHANGELOG]
        for d in latest["days"]:
            history[d["date"]] = {**d, "recorded_at": latest["scraped_at"],
                                  "lessons": [l for l in latest["lessons"] if l["date"] == d["date"]]}
        cutoff = (now.date() - timedelta(days=HISTORY_DAYS)).isoformat()
        history = {k: v for k, v in history.items() if k >= cutoff}

    # Notifications: one morning summary a day, later only changes that land on today.
    summary_sent = saved.get("summary_sent", "")
    today_found = todays_changes(found, today)
    title = text = None
    if not offline and morning and summary_sent != today:
        text = morning_summary(latest, today)
        for hr in latest.get("homerooms", [])[1:]:
            if text:
                text += "\n" + "\n".join(homeroom_line(hr, today))
        title = "☀️ היום שלך"
    elif today_found:
        title, text = "⚠️ שינוי במערכת להיום", "\n".join(f"• {f['kind']}: {fill(f['text'])}" for f in today_found[:12])
    topic = ntfy_topic(person)
    sent = ""
    if text and topic:
        sent = "sent" if notify_ntfy(topic, title, text, CONFIG.get("site_url", "")) else "FAILED"
        if sent == "sent" and title.startswith("☀️"):
            summary_sent = today  # only once a day, and only once it really went out

    write_json(path, {
        "teacher": display, "source": CONFIG["site"], "roles": person.get("roles", []), "api": CONFIG.get("api_url", ""), "vapid": CONFIG.get("vapid_public", ""),
        "ends_title": ("שעת סיום · שכבה " + ", ".join(person["lead_grades"])) if person.get("lead_grades")
        else ("שעת סיום · " + ("הכיתות שלי" if len(homerooms) > 1 else "הכיתה שלי") if homerooms else ""),
        "summary_sent": summary_sent, "latest": latest, "changelog": changelog,
        "history": [history[k] for k in sorted(history)],
    })
    active = sum(1 for l in latest["lessons"] if l.get("status") != "cancelled")
    print(f"{display}: {active} lessons, {len(found)} changes ({len(today_found)} today)"
          + (f", notification {sent}" if sent else ""))
    entry = {"id": pid, "name": display, "roles": person.get("roles", []), "homerooms": homerooms}
    pin = pin_hash(pid, person["code"]) if person.get("code") else CONFIG.get("pin_hashes", {}).get(display)
    if pin:
        entry["pin"] = pin  # sha256("<id>:<code>"); the page asks for the code before showing the person
    return entry


LUNCH_ROLES = ["לוקח לחד\"א", "נשאר בשכבה"]
DUTY_GROUP = {"יג": "מכללה", "יד": "מכללה"}  # grades 13 and 14 share one yard-duty list


def duty_group(grade):
    return DUTY_GROUP.get(grade, grade)



def build_duty(raw, now, people, team_defs):
    """Yard duty per grade: one free team member for every period the grade is in class, and after
    period 6 the lunch break with two roles. Kept stable through the day: someone who stays free keeps
    the slot; a slot whose person became busy is handed to the free member with the fewest duties."""
    today = now.date().isoformat()
    path = DOCS / "data" / "duty.json"
    prev = load_json(path, {}).get("days", {})
    busy, teaching, absent = {}, {}, {}
    for person in people:
        data = load_json(DOCS / "data" / f"{person['id']}.json", {})
        for l in data.get("latest", {}).get("lessons", []):
            if l.get("status") != "cancelled" and l.get("hour") is not None:
                busy.setdefault(person["name"], set()).add((l["date"], l["hour"]))
                teaching.setdefault(person["name"], {}).setdefault((l["date"], l["hour"]), set()).update(class_key(c) for c in l.get("classes", []))
            # Someone stands in for them in a lesson that day (שמירות, a course, sick...): away all day.
            for c in l.get("changes", []):
                m = re.match(r"\s*(.+?)\s*<-", c.get("text", ""))
                if "מילוי מקום" in (c.get("type") or "") and m and m.group(1).strip() == person["name"]:
                    absent.setdefault(l["date"], set()).add(person["name"])
    known = {p["name"] for p in people}
    # Only today's list: it is made on the first run of the morning and kept up to date through the day.
    dates = [today] if today in raw["days"] and now.weekday() in (6, 0, 1, 2, 3) else []
    grade_hours = {}
    for (date, hour), entries in raw["slots"].items():
        if hour is None:
            continue
        for name, cell in entries:
            # The grade's school day: every hour up to its last one gets a yard-duty slot, whatever
            # happens in it (lessons, exams, events, cancellations).
            if cell["lessons"] or cell.get("changes") or cell.get("removed"):
                grade_hours.setdefault((date, duty_group(grade_of(name))), set()).add(hour)
    # The fairness table: every duty since the start of this month counts, not only today's.
    hist_path = DOCS / "data" / "duty_history.json"
    hist = load_json(hist_path, {}).get("days", {})
    month = today[:7]
    count, last = {}, {}
    rel_count, rel_last = {}, {}  # class releases have their own fairness table
    for d, day in hist.items():
        if d[:7] == month and d < today:
            for g in day.values():
                for r in g.get("releases", []):
                    for c in r["people"]:
                        if r.get("why") == "פנוי":
                            rel_count[c] = rel_count.get(c, 0) + 1
                            rel_last[c] = max(rel_last.get(c, ""), d)
    for d, day in hist.items():
        if d[:7] == month and d < today:
            for g in day.values():
                for s in g["slots"]:
                    for c in s["people"]:
                        count[c] = count.get(c, 0) + 1
                        last[c] = max(last.get(c, ""), d)
    days = {}
    edits = duty_edits(today, prev.get(today, {}))

    def pick(cands, date, key, taken):
        # fewest duties this month first, then whoever had one longest ago
        free = [c for c in cands if c not in taken]
        free.sort(key=lambda c: (count.get(c, 0), last.get(c, ""), hashlib.sha1(f"{c}|{date}|{key}".encode()).hexdigest()))
        return free[0] if free else None

    for date in dates:
        day_out, taken_at = {}, {}
        for (grade, h, role), e in edits.items():  # hand-made choices come first
            for c in e["people"]:
                taken_at.setdefault(h, set()).add(c)
        for team, t in team_defs.items():
            members = [m for m in t.get("members", []) if m in known]
            away = absent.get(date, set())
            if not members:
                continue
            for grade in dict.fromkeys(duty_group(g) for g in t.get("grades", [])):
                hours = sorted(grade_hours.get((date, grade), ()))
                if not hours:
                    continue
                old = {(s["hour"], s.get("role", "")): s["people"] for s in prev.get(date, {}).get(grade, {}).get("slots", [])}
                slots = []
                wanted = [(h, "") for h in range(1, max(hours) + 1)]
                if max(hours) >= 7:
                    wanted.append((6.5, LUNCH_ROLES[0]))
                    wanted.append((6.5, LUNCH_ROLES[1]))
                free_hours = {m: sum(1 for h, r in wanted if not r and (date, h) not in busy.get(m, set())) for m in members}
                for h, role in wanted:
                    taken = taken_at.setdefault(h, set())
                    e = edits.get((grade, h, role))
                    if e:
                        chosen = list(e["people"])
                        for c in chosen:
                            count[c] = count.get(c, 0) + 1
                            last[c] = date
                        slot = {"hour": h, "people": chosen, "manual": True, "note": e.get("note", ""), "by": e.get("by", "")}
                        if role:
                            slot["role"] = role
                        slots.append(slot)
                        continue
                    if role:
                        cands = [m for m in members if m not in away]  # the whole team is out of class during the break
                    else:
                        cands = [m for m in members if (date, h) not in busy.get(m, set()) and m not in away]
                    keep = [x for x in old.get((h, role), []) if x in cands and x not in taken]
                    chosen = keep[:1] or [x for x in [pick(cands, date, f"{grade}{h}{role}", taken)] if x]
                    for c in chosen:
                        taken.add(c)
                        count[c] = count.get(c, 0) + 1
                        last[c] = date
                    slot = {"hour": h, "people": chosen}
                    if role:
                        slot["role"] = role
                    slots.append(slot)
                releases = class_releases(raw, date, grade, team, members, busy, teaching, away, people,
                                          prev.get(date, {}).get(grade, {}).get("releases", []), rel_count, rel_last)
                day_out[grade] = {"team": team, "slots": sorted(slots, key=lambda x: x["hour"]), "releases": releases,
                                  "hours": sum(1 for h, r in wanted if not r), "free": free_hours, "members": members,
                                  "free_at": {str(h): [m for m in members if (date, h) not in busy.get(m, set()) and m not in away] for h, r in wanted if not r},
                                  "away": sorted(away & set(members))}
        days[date] = day_out
    write_json(path, {"updated": now.isoformat(timespec="minutes"), "lunch_roles": LUNCH_ROLES,
                      "days": {d: {g: {"team": v["team"], "slots": v["slots"], "members": v["members"], "free_at": v["free_at"], "releases": v["releases"], "away": v["away"]}
                                   for g, v in day.items()} for d, day in days.items()}})
    # Keep about two months of days; the monthly reports below are kept for good.
    hist.update(days)
    keep_from = (now.date().replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
    hist = {d: v for d, v in sorted(hist.items()) if d >= keep_from}
    write_json(hist_path, {"updated": now.isoformat(timespec="minutes"), "days": hist})
    build_duty_reports(hist, team_defs, known, now)
    print(f"duty lists for {len(dates)} days")


PERIOD_END = {0: "08:10", 1: "08:55", 2: "09:40", 3: "10:40", 4: "11:25", 5: "12:15", 6: "13:00",
              7: "14:25", 8: "15:10", 9: "16:00", 10: "16:45"}


def class_releases(raw, date, grade, team, members, busy, teaching, away, people, old, rel_count, rel_last):
    """Who releases each class of the grade at the end of its day: the homeroom instructor when free
    (or teaching that very class then); otherwise a free member of the grade's own team, the one
    with the fewest releases this month, who isn't releasing another class at the same time.
    Being on yard duty then is fine. Only the grade's team: never an instructor of another grade."""
    homeroom = {}
    for p in people:
        if p["name"] not in members:
            continue
        for h in p.get("homerooms", []):
            homeroom.setdefault(class_key(h), p["name"])
    kept = {r["class"]: r for r in old}
    rows, taken = [], {}
    classes = [c["name"] for c in raw["classes"] if duty_group(grade_of(c["name"])) == grade]
    for cls in sorted(classes, key=lambda c: (len(grade_of(c)), c)):
        info = homeroom_info(raw, cls)["days"].get(date) or {}
        end = info.get("end") or info.get("last_lesson")
        if end:
            # A level group (one teacher out of several) dropped in the last period with no stand-in:
            # that group leaves a period earlier than the rest of the class.
            subbed = " ".join(c["text"] for c in info.get("changes", []) if c["hour"] == end["hour"] and "מילוי מקום" in c["type"])
            early = [{"subject": r["subject"], "teacher": r["teacher"], "end": PERIOD_END.get(end["hour"] - 1, "")}
                     for r in info.get("removed", []) if r["hour"] == end["hour"] and r["teacher"] and r["teacher"] not in subbed]
            rows.append({"class": cls, "hour": end["hour"], "end": end["end"], "homeroom": homeroom.get(class_key(cls), ""),
                         "regular": (info.get("regular_end") or {}).get("end", ""), "early": early})

    def free(m, r):
        if m in away:
            return False
        return (date, r["hour"]) not in busy.get(m, set()) or class_key(r["class"]) in teaching.get(m, {}).get((date, r["hour"]), set())
    # First every homeroom instructor who can release their own class...
    for r in rows:
        if r["homeroom"] and free(r["homeroom"], r):
            r.update(people=[r["homeroom"]], why="מחנך")
            taken.setdefault(r["hour"], set()).add(r["homeroom"])
    # ...then a free team member for the rest.
    for r in rows:
        if "people" in r:
            continue
        cands = [m for m in members if free(m, r) and m not in taken.get(r["hour"], set())]
        prev = kept.get(r["class"], {})
        keep = [x for x in prev.get("people", []) if prev.get("why") == "פנוי" and x in cands]
        if keep:
            pick = keep[0]
        else:
            cands.sort(key=lambda c: (rel_count.get(c, 0), rel_last.get(c, ""), hashlib.sha1(f"{c}|{date}|{r['class']}".encode()).hexdigest()))
            pick = cands[0] if cands else None
            if pick:
                rel_count[pick] = rel_count.get(pick, 0) + 1
                rel_last[pick] = date
        r.update(people=[pick] if pick else [], why="פנוי",
                 note=("מדריך/ת הכיתה נעדר/ת היום" if r["homeroom"] in away else "מדריך/ת הכיתה בשיעור" if r["homeroom"] else "אין מדריך/ת כיתה רשום/ה"))
        if pick:
            taken.setdefault(r["hour"], set()).add(pick)
    out = rows
    return out


def duty_edits(today, prev_day):
    """Yard duty changed by hand in the site, from the engine. If the engine can't be reached, the hand-made
    slots already in duty.json are kept as they are."""
    out = {}
    try:
        if not CONFIG.get("api_url"):
            raise ValueError("no engine")
        req = urllib.request.Request(CONFIG["api_url"], data=json.dumps({"action": "duty_get", "date": today}).encode(),
                                     method="POST", headers={"Content-Type": "text/plain"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        if not r.get("ok"):
            raise ValueError(r.get("error"))
        for e in r["edits"]:
            e["people"] = [x.strip() for x in str(e.get("person") or "").split("|") if x.strip()][:2]
            out[(duty_group(e["grade"]), float(e["hour"]) if float(e["hour"]) % 1 else int(e["hour"]), e.get("role", ""))] = e
    except Exception as err:  # noqa: BLE001
        print(f"duty edits: engine not reached ({err}), keeping the earlier ones")
        for grade, g in prev_day.items():
            for s in g.get("slots", []):
                if s.get("manual"):
                    out[(grade, s["hour"], s.get("role", ""))] = {"people": s["people"][:2], "note": s.get("note", ""), "by": s.get("by", "")}
    if out:
        print(f"duty edits: {len(out)}")
    return out


def build_duty_reports(hist, team_defs, known, now):
    """How many yard duties each instructor did, per team and month (data/duty_reports.json)."""
    path = DOCS / "data" / "duty_reports.json"
    months = load_json(path, {}).get("months", {})
    fresh = {}
    for d, day in sorted(hist.items()):
        m = d[:7]
        for grade, g in day.items():
            team = fresh.setdefault(m, {}).setdefault(g["team"], {"days": set(), "people": {}})
            team["days"].add(d)
            people = team["people"]
            for name in [x for x in team_defs.get(g["team"], {}).get("members", []) if x in known]:
                people.setdefault(name, {"total": 0, "hours": 0, "lunch": {r: 0 for r in LUNCH_ROLES}, "free": 0, "possible": 0})
            for name, n in (g.get("free") or {}).items():
                if name in people:
                    people[name]["free"] += n
                    people[name]["possible"] += g.get("hours", 0)
            for s in g["slots"]:
                for name in s["people"]:
                    p = people.setdefault(name, {"total": 0, "hours": 0, "lunch": {r: 0 for r in LUNCH_ROLES}, "free": 0, "possible": 0})
                    p["total"] += 1
                    if s.get("role"):
                        p["lunch"][s["role"]] = p["lunch"].get(s["role"], 0) + 1
                    else:
                        p["hours"] += 1
    for m, teams in fresh.items():
        months[m] = {"teams": {t: {"days": len(v["days"]), "people": v["people"]} for t, v in teams.items()}}
    write_json(path, {"updated": now.isoformat(timespec="minutes"), "lunch_roles": LUNCH_ROLES,
                      "months": dict(sorted(months.items(), reverse=True))})


def class_id(class_name):
    return hashlib.sha1(class_name.encode("utf-8")).hexdigest()[:8]


def build_classes(raw, now):
    """One small file per class (its lessons, changes and end times) plus an index, for the "classes" tab."""
    days = sorted(raw["days"].values(), key=lambda d: d["date"])
    regular = {}
    for name, cell in raw["regular"]:
        regular[(name, cell["date"], cell["hour"])] = cell
    index = []
    for c in raw["classes"]:
        name = c["name"]
        slots = {}
        for (date, hour), entries in raw["slots"].items():
            for n, cell in entries:
                if n == name and hour is not None:
                    slots[(date, hour)] = {"date": date, "hour": hour, "start": cell["start"], "end": cell["end"],
                                           "lessons": cell["lessons"], "changes": cell["changes"], "removed": []}
        for (n, date, hour), cell in regular.items():
            if n != name or hour is None:
                continue
            cur = slots.get((date, hour))
            kept = {(l["subject"], l["teacher"]) for l in (cur["lessons"] if cur else [])}
            gone = [l for l in cell["lessons"] if (l["subject"], l["teacher"]) not in kept]
            if gone:
                slot = slots.setdefault((date, hour), {"date": date, "hour": hour, "start": cell["start"], "end": cell["end"],
                                                       "lessons": [], "changes": [], "removed": []})
                slot["removed"].extend(gone)
        info = homeroom_info(raw, name)["days"]
        ends = {d: {"now": v.get("last_lesson"), "regular": v.get("regular_end")} for d, v in info.items()}
        cid = class_id(name)
        write_json(DOCS / "data" / "class" / f"{cid}.json", {
            "class": name, "updated": now.isoformat(timespec="minutes"), "days": days, "ends": ends,
            "slots": [slots[k] for k in sorted(slots)],
        })
        index.append({"id": cid, "name": name, "grade": grade_of(name)})
    write_json(DOCS / "data" / "classes.json", {"updated": now.isoformat(timespec="minutes"), "classes": index})
    print(f"built {len(index)} class pages")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="rebuild the site from saved data without scanning")
    ap.add_argument("--morning", action="store_true", help="send the daily summary (default before 08:00)")
    args = ap.parse_args()

    now = datetime.now(ISRAEL_TZ)
    raw, extras = None, {}
    if not args.offline:
        raw = collect(now=now)
        if not raw["classes"] or not raw["days"]:
            sys.exit("scan returned no classes or days; keeping the previous data")
    staff, team_defs = load_staff(raw)
    if raw:
        extras = load_extras(team_defs)
    for t in team_defs.values():
        for person in staff:
            shown = person.get("display") or person.get("name") or " ".join(reversed(person["as_given"].split(" ", 1)))
            if shown == t.get("lead"):
                person["lead_grades"] = person.get("lead_grades", []) + t.get("grades", [])
    if raw:
        build_classes(raw, now)
    morning = args.morning or 5 <= now.hour < 8
    people = [p for p in (build_person(s, raw, extras, now, args.offline, morning) for s in staff) if p]
    # Team commanders aren't in the staff tab, but many also teach: build their timetable too, for the
    # first tab of their board.
    known = site_teachers(raw) if raw else set()
    by_tokens = {frozenset(t.split()): t for t in known}
    shown = {p["name"]: p["id"] for p in people}
    commander_ids = {}
    for t in team_defs.values():
        c = t.get("commander")
        if c and c in shown and raw:  # a commander who is also on the staff list: their page is already built
            add_grade_agenda(shown[c], t.get("grades", []), raw, now)
        if not c or c in shown or c in commander_ids:
            continue
        site_name = c if c in known else by_tokens.get(frozenset(c.split()))
        entry = build_person({"name": site_name, "as_given": c, "display": c, "code": "", "roles": ["מפקד"]},
                             raw, extras, now, args.offline, False)
        if entry:
            commander_ids[c] = entry["id"]
            if raw:
                add_grade_agenda(entry["id"], t.get("grades", []), raw, now)
    if people:
        by_name = {p["name"]: p["id"] for p in people}
        teams = []
        for team_name, t in team_defs.items():
            members = [{"id": by_name[m], "name": m} for m in t["members"] if m in by_name]
            if t.get("lead") in by_name:
                teams.append({"name": team_name, "lead": by_name[t["lead"]], "members": members,
                              "commander": t.get("commander", ""), "grades": t.get("grades", []),
                              "commander_id": commander_ids.get(t.get("commander", "")) or by_name.get(t.get("commander", ""), "")})
        write_json(DOCS / "data" / "staff.json",
                   {"updated": now.isoformat(timespec="minutes"), "people": people, "teams": teams, "api": CONFIG.get("api_url", ""),
                    "cmd_hash": CONFIG.get("commander_hash", "")})
        if raw:
            build_duty(raw, now, people, team_defs)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text((ROOT / "web" / "index.html").read_text(encoding="utf-8"), encoding="utf-8")
    for extra in ("sw.js", "manifest.webmanifest", "icon-192.png", "icon-512.png"):
        if (ROOT / "web" / extra).exists():
            (DOCS / extra).write_bytes((ROOT / "web" / extra).read_bytes())
    (DOCS / ".nojekyll").write_text("")
    print(f"built {len(people)} pages at {now.isoformat(timespec='minutes')}")


if __name__ == "__main__":
    main()
