"""Short, clear wording for change notifications: one line per change, no date, no long subject names.

A changelog text looks like "2026-11-22 שעה 7 · <subject> · <classes> — <detail>" (see lib.describe);
homeroom and end-time entries are "<classes> · <text>" / "<date> · <class> מסיים ...".
"""

import re

from lib import hours_label

ICON = {"מילוי מקום": "🔄", "לא מתקיים": "❌", "ביטול / שעה חופשית": "❌", "הוסר": "❌", "נוסף": "➕",
        "חזר למערכת": "↩️", "מבחן": "📝", "מבחן במקום השיעור": "📝", "אירוע במקום השיעור": "📝",
        "מבחן חדש": "📝", "אירוע חדש": "📅", "כיתת חינוך": "🏫", "שעת סיום": "🕒", "שינוי חדר": "🚪", "חדר": "🚪"}
LESSON = re.compile(r"^\d{4}-\d\d-\d\d(?: שעה (\d+))? · ([^—]*) · ([^·—]*?)(?: — (.*))?$")
ENDS = re.compile(r"^(.*?) (?:מסיים|מסיימת|מסיימים) (.*?) \((?:קודם|במקום) (.*?)\)$")


def cls(text):
    """ "יד'- 1, י' - 2, יא- 6" -> "יד'1, י'2, יא'6"."""
    return re.sub(r"([א-ת])'?\s*-\s*(\d)", r"\1'\2", text or "").strip()


def join_classes(parts):
    """["ט'1", "ט'2", "ט'3", "יא'2"] -> "ט'1–3, יא'2"."""
    out, run = [], []
    def order(c):
        m = re.match(r"^(.+')(\d+)$", c)
        return (m.group(1), int(m.group(2))) if m else (c, 0)
    for c in sorted(dict.fromkeys(x.strip() for p in parts for x in p.split(",") if x.strip()), key=order) + [None]:
        m = re.match(r"^(.+')(\d+)$", c) if c else None
        if run and m and m.group(1) == run[0][0] and int(m.group(2)) == run[-1][1] + 1:
            run.append((m.group(1), int(m.group(2))))
            continue
        if run:
            out.append(run[0][0] + (f"{run[0][1]}–{run[-1][1]}" if len(run) > 2 else ", ".join(f"{g}{n}" for g, n in run) if len(run) == 2 else str(run[0][1])))
            if len(run) == 2:
                out[-1] = f"{run[0][0]}{run[0][1]}, {run[1][0]}{run[1][1]}"
            run = []
        if m:
            run = [(m.group(1), int(m.group(2)))]
        elif c and c not in out:
            out.append(c)
    return ", ".join(dict.fromkeys(out))


def room(text):
    """ "06 מע' רובוטיקה" -> "06"; a room without a number stays as it is."""
    t = (text or "").strip()
    m = re.match(r"(\d+)\b", t)
    return m.group(1) if m else t


def swap(text, me=None):
    """Several fill-ins ("A <- B 05 ..., C <- D"): the one about this person, else the first."""
    parts = [p for p in re.split(r",\s*(?=[^,]*<-)", text or "") if "<-" in p]
    if len(parts) > 1:
        mine = [p for p in parts if me and me in p]
        return one_swap((mine or parts)[0], me)
    return one_swap(text, me)


def one_swap(text, me=None):
    """ "כץ עדי <- אוחנה אגם 06 מע' רובוטיקה" -> "אוחנה אגם במקום כץ עדי · חדר 06".
    Said to the person themselves: "במקום כץ עדי" when they fill in, "במקומך: אוחנה אגם" when they are replaced."""
    left, sep, right = (text or "").partition("<-")
    if not sep:
        return None
    m = re.match(r"(.*?)\s+(\d.*)$", right.strip())
    who, where = (m.group(1), room(m.group(2))) if m else (right.strip(), "")
    tail = f" · חדר {where}" if where else ""
    if me and who == me:
        return f"במקום {left.strip()}{tail}"
    if me and left.strip() == me:
        return f"במקומך: {who}{tail}"
    return f"{who} במקום {left.strip()}{tail}"


def short(text, n=45):
    text = text.strip()
    return text if len(text) <= n else text[:n - 1].rstrip(" ,·") + "…"


def describe_change(kind, text, me=None):
    """-> (hour or None, classes, what) for one changelog entry."""
    m = LESSON.match(text or "")
    if not m:  # homeroom / end time / new exam: "<date> · <class> ..." or "<class> · <text>"
        t = re.sub(r"^\d{4}-\d\d-\d\d\s*·\s*", "", text or "")
        e = ENDS.match(t)
        if e:  # "ט' - 1 מסיים 15:10 (במקום 16:45)", from the grade or the homeroom; said the same way
            return None, cls(e.group(1)), f"מסיימים {e.group(2)} (במקום {e.group(3)})"
        if kind in ("מבחן חדש", "אירוע חדש"):
            title, _, c = t.rpartition(" · ")
            return None, cls(c), short(title or t)
        c, sep, rest = t.partition(" · ")
        if rest.startswith("לא מתקיים:"):  # homeroom: "לא מתקיים: שעה 2 מתמטיקה, שעה 3 ..."
            hs = sorted({int(x) for x in re.findall(r"שעה (\d+)", rest)})
            if hs:
                rest = ("לא מתקיים בשעה " if len(hs) == 1 else "לא מתקיים בשעות ") + hours_label(hs)
        hs = re.findall(r"שעה (\d+): ([^,]*)", rest)
        if hs and len({t.strip() for _, t in hs}) == 1:  # homeroom "שעה 5: מבחן במקום פיסיקה, שעה 6: ..."
            hours = sorted({int(n) for n, _ in hs})
            if len(hours) == 1:
                return hours[0], cls(c), short(hs[0][1])
            return None, cls(c), short(hs[0][1].strip(), 35) + " ב" + when(hours)
        if rest.startswith(("מבחן", "אירוע")):
            rest = re.split(r"\s*,", rest)[0]
        return (None, cls(c), short(rest)) if sep else (None, "", short(cls(t), 60))
    hour, subject, classes, detail = m.groups()
    hour = int(hour) if hour else None
    if kind in ("מבחן חדש", "אירוע חדש"):
        what = short(subject)
    elif kind == "מילוי מקום":
        what = swap(detail, me) or short(detail or "מילוי מקום")
    elif kind == "נוסף":
        s = swap(subject, me) or swap(detail, me)
        what = "שיעור נוסף" + ((" " if s.startswith("במקום ") else ": ") + s if s else "")
    elif kind in ("לא מתקיים", "ביטול / שעה חופשית"):
        what = "לא מתקיים"
    elif kind == "הוסר":
        what = "בוטל: " + (swap(subject, me) or short(re.split(r",| · ", subject)[0], 40))
    elif kind == "חזר למערכת":
        what = "חזר למערכת"
    elif "מבחן" in kind or "אירוע" in kind:
        what = short((detail or kind).split(",")[0])
    else:
        what = short(f"{kind}: {swap(detail) or detail or ''}")
    return hour, cls(classes), what


SAME = {"ביטול / שעה חופשית": "לא מתקיים", "מבחן במקום השיעור": "מבחן", "אירוע במקום השיעור": "מבחן"}


def group_changes(changes, me=None):
    """[(kind, text)] -> [(kind, hours, classes, what)], joining the same change over consecutive hours."""
    items, seen = [], set()
    for kind, text in changes:
        h, c, what = describe_change(kind, text, me)
        key = (SAME.get(kind, kind), h, c, what)
        if key not in seen:
            seen.add(key)
            items.append((kind, h, c, what))
    # "cancelled" next to a fill-in or an exam in the same lesson says nothing more; drop it
    other = {(h, c) for k, h, c, _ in items if SAME.get(k, k) != "לא מתקיים"}
    items = [x for x in items if not (SAME.get(x[0], x[0]) == "לא מתקיים" and (x[1], x[2]) in other)]
    items = [("שעת סיום" if w.startswith("מסיימים") else k, h, c, w) for k, h, c, w in items]
    items = list(dict.fromkeys(items))
    # in a lesson where this person fills in or is replaced, the other teachers' swaps are noise
    mine = {(h, c) for k, h, c, w in items if k == "מילוי מקום" and (w.startswith("במקום") or w.startswith("במקומך"))}
    items = [x for x in items if not (x[0] == "מילוי מקום" and (x[1], x[2]) in mine and not x[3].startswith(("במקום", "במקומך")))]
    items.sort(key=lambda x: (x[1] is None, x[1] or 0))
    out = []
    for kind, h, c, what in items:
        for g in out:
            if g[0] == kind and g[2] == c and g[3] == what and h is not None and g[1] and (h - 1 in g[1] or h + 1 in g[1]):
                g[1].append(h)
                break
        else:
            out.append([kind, [h] if h is not None else [], c, what])
    # the same change in several classes (a grade ending early) -> one line
    merged = []
    for k, hs, c, w in out:
        for g in merged:
            if g[0] == k and g[1] == sorted(hs) and g[3] == w:
                g[2].append(c)
                break
        else:
            merged.append([k, sorted(hs), [c], w])
    return [(k, hs, join_classes(cs), w) for k, hs, cs, w in merged]


def when(hours):
    if not hours:
        return ""
    return ("שעה " if len(hours) == 1 else "שעות ") + hours_label(hours)


def change_line(kind, hours, classes, what):
    head = " · ".join(x for x in [when(hours), classes] if x)
    return f"{ICON.get(kind, '•')} " + (f"{head}: {what}" if head else what)


def changes_message(changes, me=None, limit=4):
    """[(kind, text)] -> (title, body) for today's changes."""
    groups = group_changes(changes, me)
    if len(groups) == 1:
        kind, hours, classes, what = groups[0]
        label = {"נוסף": "שיעור נוסף", "ביטול / שעה חופשית": "לא מתקיים", "הוסר": "שינוי במערכת",
                 "כיתת חינוך": "בכיתת החינוך שלך", "שעת סיום": "שינוי בשעת הסיום"}.get(kind, kind)
        title = f"{ICON.get(kind, '⚠️')} {label}" + (f" ב{when(hours)}" if hours else "")
        rest = what if what not in (label, kind, "שיעור נוסף") else ""
        if kind == "נוסף" and what.startswith("שיעור נוסף"):
            rest = what[len("שיעור נוסף"):].lstrip(": ")
        return title, " · ".join(x for x in [classes, rest] if x) or what
    lines = [change_line(*g) for g in groups[:limit]]
    if len(groups) > limit:
        lines.append(f"ועוד {len(groups) - limit} · פרטים במערכת")
    return f"⚠️ {len(groups)} שינויים במערכת היום", "\n".join(lines)


def lesson_message(lesson, left, start, me=None):
    """The alert before a lesson: classes, room, and what changed in it."""
    rooms = ", ".join(room(r) for r in lesson.get("rooms", []))
    parts = [cls(", ".join(lesson.get("classes", []))), f"חדר {rooms}" if rooms else ""]
    changes = lesson.get("changes", [])
    mine = [c for c in changes if me and me in c.get("text", "")]
    for c in (mine or changes)[:2]:
        s = swap(c.get("text", ""), me)
        parts.append(f"{ICON.get(c.get('type'), '•')} " + (s.split(' · חדר')[0] if s else short(f"{c.get('type')}: {c.get('text', '')}", 40)))
    return f"📚 בעוד {left} דקות · {start}", " · ".join(p for p in parts if p)
