"""Pop-up notifications to the phones of people who turned them on in the site.

Runs after build.py in the hourly update. It needs two repository secrets:
  PUSH_TOKEN         the "מפתח התראות" value from the private settings sheet (Apps Script)
  VAPID_PRIVATE_KEY  the private half of the key pair whose public half is config.json's vapid_public
Without them it does nothing.

What is sent, per person, at most once each (a hashed log in docs/data/push_log.json):
  - 07:00–07:59: a morning summary of the day (lessons, yard duty, reminders)
  - any run: changes found in this run that land on today
  - any run after the morning: reminders and yard duty that start within the next hour
  - to team leads and commanders, once a month: the yard-duty report (see report_month)
  - about 15-35 minutes before each lesson (not the first of the day), unless the person turned it off
"""

import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import fill, load_json, write_json  # noqa: E402
from notify_text import changes_message, lesson_message  # noqa: E402
from scrape import ISRAEL_TZ  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
HOURS = {0: "07:25", 1: "08:10", 2: "08:55", 3: "09:55", 4: "10:40", 5: "11:30", 6: "12:15",
         7: "13:40", 8: "14:25", 9: "15:15", 10: "16:00"}
LUNCH = "13:00"


def api(payload):
    req = urllib.request.Request(CONFIG["api_url"], data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def mins(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


BIG_CHANGES = ("מילוי מקום", "לא מתקיים", "נוסף", "ביטול", "כיתת חינוך")


def where(grade):
    return grade if grade == "מכללה" else f"שכבה {grade}'"


def short(name):
    return name.split(" ")[0] if name else name


def commander_messages(team, all_reminders, duty, ids, today, dow, morning):
    """A team's commander: one morning summary, then one message per run with the team's new changes."""
    members = [m["name"] for m in team.get("members", [])]
    changes, fresh = [], []
    for m in team.get("members", []):
        data = load_json(DOCS / "data" / f"{m['id']}.json", {})
        stamp = data.get("latest", {}).get("scraped_at")
        for c in data.get("changelog", []):
            if c.get("date") == today and any(k in (c.get("kind") or "") for k in BIG_CHANGES):
                line = f"{m['name']}: {c.get('kind')} · {fill(c.get('text', ''))}"
                if line not in changes:
                    changes.append(line)
                    if c.get("detected_at") == stamp:
                        fresh.append(line)
    out = []
    if morning:
        parts = []
        groups = list(dict.fromkeys("מכללה" if x in ("יג", "יד") else x for x in team.get("grades", [])))
        for grade in groups:
            g = duty.get(grade)
            if not g:
                continue
            hours = [f"{int(s['hour'])} {short(s['people'][0]) if s['people'] else '—'}" for s in g["slots"] if not s.get("role")]
            lunch = [f"{s['role']} – {s['people'][0] if s['people'] else '—'}" for s in g["slots"] if s.get("role")]
            prefix = f"{where(grade)}: " if len(groups) > 1 else ""
            parts.append("🛡️ תורני חצר: " + prefix + " · ".join(hours))
            if lunch:
                parts.append("🍽️ הפסקה גדולה: " + " · ".join(lunch))
        homeroom = [c.split(": ", 1)[1].replace("כיתת חינוך · ", "") for c in changes if "כיתת חינוך" in c]
        if homeroom:
            parts.append("🏫 שינויים בכיתות: " + " · ".join(dict.fromkeys(homeroom))[:180])
        others = [c for c in changes if "כיתת חינוך" not in c]
        if others:
            parts.append(f"⚠️ {len(others)} שינויים במערכת של הצוות")
        n = sum(1 for r in all_reminders if r.get("team") == team["name"] and (r["date"] == today if r["date"] else r["dow"] == dow))
        if n:
            parts.append(f"📝 {n} תזכורות של הצוות להיום")
        if parts:
            out.append(("cmd-morning", f"👥 {team['name']} · הבוקר", "\n".join(parts)))
    elif fresh:
        body = "\n".join(fresh[:6]) + (f"\nועוד {len(fresh) - 6}" if len(fresh) > 6 else "")
        out.append(("cmd-chg|" + hashlib.sha1("|".join(fresh).encode("utf-8")).hexdigest()[:10],
                    f"⚠️ {team['name']} · {len(fresh)} שינויים חדשים להיום", body))
    return out


MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]


def report_month(now, duty_today):
    """The month whose yard-duty report goes out now, or None: at the last update of the month's last
    school day (from 14:00), or, if that was missed, in the first week of the next month."""
    d = now.date()
    if duty_today and now.hour >= 14:
        nxt = d + timedelta(days=1)
        while nxt.month == d.month and nxt.weekday() not in (6, 0, 1, 2, 3):
            nxt += timedelta(days=1)
        if nxt.month != d.month:
            return d.isoformat()[:7]
    if d.day <= 7:
        return (d.replace(day=1) - timedelta(days=1)).isoformat()[:7]
    return None


def report_message(reports, month, team):
    t = reports.get("months", {}).get(month, {}).get("teams", {}).get(team)
    if not t or not t["people"]:
        return []
    people = sorted(t["people"].items(), key=lambda kv: -kv[1]["total"])
    total = sum(p["total"] for _, p in people)
    if not total:
        return []
    avg = total / len(people)
    hi, lo = people[0][1]["total"], people[-1][1]["total"]
    most = ", ".join(f"{short(n)} {p['total']}" for n, p in people if p["total"] == hi)
    least = ", ".join(f"{short(n)} {p['total']}" for n, p in people if p["total"] == lo)
    name = MONTHS[int(month[5:]) - 1]
    body = f"{total} תורנויות · ממוצע {avg:.0f} · פער מקסימלי {hi - lo}\nהכי הרבה: {most} · הכי מעט: {least}\nהדוח המלא באתר, בלשונית תורני חצר"
    return [(f"report|{month}", f"📊 {team} · דוח תורנויות {name}", body)]


def vapid_sub():
    """The push services want a contact as a bare https origin (no path) or mailto: address."""
    from urllib.parse import urlsplit
    u = urlsplit(CONFIG.get("site_url") or "")
    return f"https://{u.hostname}" if u.hostname else "https://github.com"


def device(name, sub):
    """Who a message went to: the person and this one phone. Someone with two subscriptions (a new phone,
    or the home-screen icon next to Safari) gets every message on each."""
    return name + "#" + hashlib.sha1(str(sub.get("endpoint", "")).encode("utf-8")).hexdigest()[:10]


def sent_key(who, tag, today):
    if tag.startswith("report|"):  # once per month, not once per day
        return "r:" + hashlib.sha1(f"{who}|{tag}".encode("utf-8")).hexdigest()[:16]
    return hashlib.sha1(f"{who}|{today}|{tag}".encode("utf-8")).hexdigest()[:16]


def send_all(webpush, WebPushException, sub, name, messages, log, today, key):
    """Send each message once to this phone; returns (sent, gone) where gone means the phone turned
    notifications off. Keys from before the log was per phone (by name alone) still count as sent."""
    sent, who = 0, device(name, sub)
    for tag, title, body in messages:
        k = sent_key(who, tag, today)
        if k in log or sent_key(name, tag, today) in log:
            continue
        try:
            webpush(subscription_info=sub, data=json.dumps({"title": title, "body": body, "tag": tag[:40], "url": CONFIG.get("site_url", "./")}, ensure_ascii=False),
                    vapid_private_key=key, vapid_claims={"sub": vapid_sub()}, ttl=3600)
            log[k] = today
            sent += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):
                return sent, True
            print(f"notifications: one failed ({code})")
        except Exception as e:  # noqa: BLE001 - one bad message must not stop the others
            print(f"notifications: one failed ({type(e).__name__}: {e})")
    return sent, False


def main():
    token, key = os.environ.get("PUSH_TOKEN", ""), os.environ.get("VAPID_PRIVATE_KEY", "")
    if not (token and key and CONFIG.get("api_url")):
        print("notifications: not set up, skipping")
        return
    from pywebpush import WebPushException, webpush

    now = datetime.now(ISRAEL_TZ)
    today, dow = now.date().isoformat(), (now.weekday() + 1) % 7  # Sunday = 0, as in the site
    feed = api({"action": "push_feed", "token": token})
    if not feed.get("ok"):
        print(f"notifications: feed refused ({feed.get('error')})")
        return
    staff = load_json(DOCS / "data" / "staff.json", {})
    ids = {p["name"]: p["id"] for p in staff.get("people", [])}
    duty = load_json(DOCS / "data" / "duty.json", {}).get("days", {}).get(today, {})
    log_path = DOCS / "data" / "push_log.json"
    log = load_json(log_path, {})
    log = {k: v for k, v in log.items() if v >= (now.date() - timedelta(days=40 if k.startswith("r:") else 3)).isoformat()}
    morning = now.hour == 7
    window = (now.hour * 60 + now.minute, now.hour * 60 + now.minute + 60)
    sent = dropped = 0

    teams = {t["name"]: t for t in staff.get("teams", [])}
    reports = load_json(DOCS / "data" / "duty_reports.json", {})
    rmonth = report_month(now, bool(duty))
    names = {p["id"]: p["name"] for p in staff.get("people", [])}
    lead_of = {names.get(t["lead"]): t["name"] for t in staff.get("teams", [])}
    lesson_off = set(feed.get("lesson_off", []))
    for entry in feed["subs"]:
        name, sub = entry["name"], entry["sub"]
        if name.startswith("מפקד:"):
            team = teams.get(name.split(":", 1)[1])
            messages = commander_messages(team, feed["reminders"], duty, ids, today, dow, morning) if team else []
            if team and rmonth:
                messages += report_message(reports, rmonth, team["name"])
            ok, gone = send_all(webpush, WebPushException, sub, name, messages, log, today, key)
            sent += ok
            if gone:
                api({"action": "push_drop", "token": token, "endpoint": sub.get("endpoint", "")})
                dropped += 1
            continue
        data = load_json(DOCS / "data" / f"{ids.get(name, '')}.json", {})
        latest = data.get("latest", {})
        lessons = [l for l in latest.get("lessons", []) if l.get("date") == today and l.get("status") != "cancelled"]
        rems = [r for r in feed["reminders"] if r["name"] == name and (r["date"] == today if r["date"] else r["dow"] == dow)]
        duties = []
        for grade, g in duty.items():
            for s in g["slots"]:
                if name in s["people"]:
                    duties.append({"grade": grade, "hour": s["hour"], "role": s.get("role", "")})
        messages = []
        if morning:
            parts = []
            if lessons:
                first = min(lessons, key=lambda l: l["hour"])
                parts.append(f"{len({l['hour'] for l in lessons})} שעות הוראה, הראשונה ב-{first['start']} ({first['subject']})")
            else:
                parts.append("אין לך שיעורים היום")
            for d in sorted(duties, key=lambda d: d["hour"]):
                parts.append(f"תורנות: {d['role'] or 'תורן חצר'} · {where(d['grade'])} · " + (f"בהפסקה ({LUNCH})" if d["role"] else f"שעה {d['hour']}"))
            for r in sorted(rems, key=lambda r: r["hour"]):
                parts.append(f"תזכורת בשעה {r['hour']}: {r['text']}")
            messages.append(("morning", "☀️ היום שלך", "\n".join(parts)))
        else:
            for r in rems:
                t = HOURS.get(r["hour"], "")
                if t and window[0] <= mins(t) < window[1]:
                    messages.append((f"rem|{r['id']}", f"⏰ תזכורת · {t}", r["text"]))
            for d in duties:
                t = LUNCH if d["role"] else HOURS.get(d["hour"], "")
                if t and window[0] <= mins(t) < window[1]:
                    what = d["role"] or "תורן חצר"
                    messages.append((f"duty|{d['grade']}|{d['hour']}|{d['role']}", f"🛡️ {what} · {t}", where(d["grade"])))
        # Before each lesson (about 15-35 minutes ahead, as the update runs every 20 minutes): on by
        # default, each instructor can turn it off on their page. Not for the day's first lesson (the
        # morning summary has it) nor for the second half of a double lesson.
        if name not in lesson_off and lessons:
            by_hour = {}
            for l in lessons:
                by_hour.setdefault(l["hour"], []).append(l)
            first = min(by_hour)
            for h, ls in sorted(by_hour.items()):
                l = ls[0]
                if h == first or any(p["subject"] == l["subject"] and p["classes"] == l["classes"] for p in by_hour.get(h - 1, [])):
                    continue
                start = l.get("start") or HOURS.get(h, "")
                left = mins(start) - (now.hour * 60 + now.minute) if start else -1
                if 12 <= left < 38:
                    messages.append((f"lesson|{h}", *lesson_message(l, left, start, name)))
        # Today's changes not sent yet, together in one message (a missed run catches up on the next).
        new = []
        for c in data.get("changelog", []):
            if c.get("date") == today and c.get("kind") != "עודכן":
                k = "c:" + hashlib.sha1(f"{device(name, sub)}|{today}|{c.get('kind')}|{c.get('text')}".encode("utf-8")).hexdigest()[:16]
                old = "c:" + hashlib.sha1(f"{name}|{today}|{c.get('kind')}|{c.get('text')}".encode("utf-8")).hexdigest()[:16]
                item = (c.get("kind"), c.get("text", ""))
                if k not in log and old not in log and item not in [x[1] for x in new]:
                    new.append((k, item))
        if new:
            messages.append(("chg|" + new[0][0], *changes_message([x[1] for x in new], name)))

        if rmonth and name in lead_of:
            messages += report_message(reports, rmonth, lead_of[name])
        ok, gone = send_all(webpush, WebPushException, sub, name, messages, log, today, key)
        sent += ok
        if new and sent_key(device(name, sub), "chg|" + new[0][0], today) in log:
            for k, _ in new:  # these changes went out
                log[k] = today
        if gone:  # the phone turned notifications off
            api({"action": "push_drop", "token": token, "endpoint": sub.get("endpoint", "")})
            dropped += 1
    write_json(log_path, log)
    print(f"notifications: {sent} sent to {len(feed['subs'])} phones, {dropped} removed")


if __name__ == "__main__":
    main()
