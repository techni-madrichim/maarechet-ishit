/**
 * "המערכת האישית שלי" — personal reminders, personal codes and the HQ view.
 *
 * A standalone Apps Script project (script.google.com, signed in as the unit), deployed as a web app:
 *   Deploy → New deployment → Web app → Execute as: Me → Who has access: Anyone.
 * The first run creates a separate, private spreadsheet "המערכת האישית — קודים ותזכורות"
 * in the owner's Drive. Codes and reminders live only there, never in the public site.
 */

var SHEET_ID = '11AgQeq267joV3MHYxot3YrSCvdH2UfEf4WvLx5S_Kt4';  // the management sheet
var STAFF_TAB = 'אנשי צוות';
var COL_NAME = 'שם (כמו בשחף: משפחה ואז פרטי)';
var MAX_TEXT = 120;

function doGet() {
  return out({ ok: true, service: 'maarechet-ishit' });
}

function doPost(e) {
  var req;
  try { req = JSON.parse(e.postData.contents); } catch (err) { return out({ ok: false, error: 'bad request' }); }
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    return out(handle(req));
  } catch (err) {
    return out({ ok: false, error: String(err && err.message || err) });
  } finally {
    lock.releaseLock();
  }
}

function handle(req) {
  var a = req.action;
  if (a === 'hq') return hq(req);
  if (a === 'cmd_team' || a === 'cmd_subscribe') return commander(req);
  if (a === 'push_feed' || a === 'push_drop') return pushFeed(req);
  if (a === 'duty_get') return dutyGet(req);
  if (a === 'duty_set' || a === 'duty_clear') return dutySet(req);
  var name = clean(req.name);
  var staff = staffList();
  var me = staff.filter(function (p) { return p.name === name; })[0];
  if (!me) return { ok: false, error: 'unknown name' };
  // Each person's reminders open only on the browsers linked to them. The first browser links freely;
  // another browser (Chrome next to Safari, the home-screen icon, a new phone) needs a one-time code
  // shown on a browser that is already linked. To reset someone, delete their rows in "מכשירים".
  var dev = ensureSheet('מכשירים', ['שם', 'מפתח מכשיר', 'קושר']);
  var mine = dev.getDataRange().getValues().slice(1).filter(function (r) { return String(r[0]) === name; });
  if (a === 'status') return { ok: true, bound: mine.length > 0 };
  if (a === 'bind') {
    if (req.token && mine.some(function (r) { return String(r[1]) === String(req.token); })) return { ok: true, token: req.token };
    if (mine.length) {
      // Another browser: a 6-digit code from a linked browser, or the person's own 8-digit recovery code.
      var cache = CacheService.getScriptCache(), want = cache.get('link:' + name);
      if (!req.link) return { ok: false, error: 'bound elsewhere' };
      var given = String(req.link).replace(/\D/g, ''), tries = Number(cache.get('tries:' + name) || 0);
      var good = tries < 5 && (given.length === 8 ? recoveryOk_(name, given) : (!!want && given === want));
      if (!good) {
        tries += 1;
        if (tries >= 5) cache.remove('link:' + name);
        cache.put('tries:' + name, String(tries), 600);  // 5 wrong tries lock linking for 10 minutes
        Utilities.sleep(800);
        return { ok: false, error: tries >= 5 ? 'too many tries' : 'wrong link code' };
      }
      cache.remove('link:' + name); cache.remove('tries:' + name);
    }
    var token = Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '');
    dev.appendRow([name, token, new Date()]);
    var res = { ok: true, token: token };
    if (!recoveryRow_(name)) res.recovery = newRecovery_(name);  // shown once, on the first link
    return res;
  }
  if (!req.token || !mine.some(function (r) { return String(r[1]) === String(req.token); })) {
    Utilities.sleep(800);
    return { ok: false, error: 'not this phone' };
  }
  if (a === 'link_code') {  // a 6-digit code, good for 10 minutes, to link one more browser
    var code = String(Math.floor(100000 + Math.random() * 900000));
    CacheService.getScriptCache().put('link:' + name, code, 600);
    CacheService.getScriptCache().remove('tries:' + name);
    return { ok: true, code: code };
  }
  if (a === 'recovery_code') return { ok: true, code: newRecovery_(name) };  // a new one replaces the old
  if (a === 'prefs') {  // per-person settings; for now: a notification before each lesson (on unless turned off)
    var pt = ensureSheet('העדפות', ['שם', 'התראה לפני שיעור', 'עודכן']), pr = findRow(pt, 1, name);
    if (typeof req.lesson_alerts === 'boolean') {
      var v = req.lesson_alerts ? 'כן' : 'לא';
      if (pr > 0) pt.getRange(pr, 2, 1, 2).setValues([[v, new Date()]]); else pt.appendRow([name, v, new Date()]);
      return { ok: true, lesson_alerts: req.lesson_alerts };
    }
    return { ok: true, lesson_alerts: !(pr > 0 && String(pt.getRange(pr, 2).getValue()) === 'לא') };
  }
  var rem = sheet('תזכורות');
  if (a === 'list') return { ok: true, reminders: reminders(rem, function (r) { return r.name === name; }) };
  if (a === 'add') {
    var r = req.reminder || {};
    var text = clean(r.text).slice(0, MAX_TEXT);
    var hour = Number(r.hour);
    if (!text || !(hour >= 0 && hour <= 10)) return { ok: false, error: 'bad reminder' };
    var date = /^\d{4}-\d{2}-\d{2}$/.test(r.date || '') ? r.date : '';
    var dow = date ? '' : Number(r.dow);
    if (!date && !(dow >= 0 && dow <= 6)) return { ok: false, error: 'bad reminder' };
    var id = Utilities.getUuid().slice(0, 12);
    rem.appendRow([id, name, me.team, "'" + date, dow, hour, text, new Date()]);
    return { ok: true, id: id };
  }
  if (a === 'delete') {
    var rr = findRow(rem, 1, String(req.id || ''));
    if (rr > 0 && rem.getRange(rr, 2).getValue() === name) rem.deleteRow(rr);
    return { ok: true };
  }
  if (a === 'subscribe') {
    var sub = req.sub || {};
    if (!sub.endpoint || !/^https:\/\//.test(sub.endpoint)) return { ok: false, error: 'bad subscription' };
    var subs = sheet('מנויים');
    var at = findRow(subs, 2, sub.endpoint);
    if (at > 0) subs.deleteRow(at);
    subs.appendRow([name, sub.endpoint, JSON.stringify(sub), new Date()]);
    return { ok: true };
  }
  if (a === 'team') {
    if (!me.lead) return { ok: false, error: 'not a team lead' };
    return { ok: true, team: me.team, reminders: reminders(rem, function (r) { return r.team === me.team && r.name !== name; }) };
  }
  return { ok: false, error: 'unknown action' };
}

/** For the hourly update only: who wants notifications, and the reminders to send. Needs the notification key. */
function pushFeed(req) {
  var key = String(setting('מפתח התראות') || '');
  if (!key || String(req.token || '') !== key) { Utilities.sleep(800); return { ok: false, error: 'wrong token' }; }
  var subs = sheet('מנויים');
  if (req.action === 'push_drop') {
    var at = findRow(subs, 2, String(req.endpoint || ''));
    if (at > 0) subs.deleteRow(at);
    return { ok: true };
  }
  var rows = subs.getDataRange().getValues().slice(1).filter(function (r) { return r[0] && r[2]; });
  var off = ensureSheet('העדפות', ['שם', 'התראה לפני שיעור', 'עודכן']).getDataRange().getValues().slice(1)
    .filter(function (r) { return String(r[1]) === 'לא'; }).map(function (r) { return String(r[0]); });
  return { ok: true, subs: rows.map(function (r) { return { name: r[0], sub: JSON.parse(r[2]) }; }),
           reminders: reminders(sheet('תזכורות'), function () { return true; }), lesson_off: off };
}

/** Administration staff: the code only opens every page; personal reminders stay private. */
function hq(req) {
  var want = String(setting('קוד אנשי מנהלה') || '');
  if (!want || String(req.code || '').trim() !== want) {
    Utilities.sleep(800);
    return { ok: false, error: 'wrong code' };
  }
  return { ok: true };
}

/** A team's commander (above the team lead): the team's reminders, and notifications for the team. */
function commander(req) {
  var want = String(setting('קוד מפקדים') || '');
  if (!want || String(req.code || '').trim() !== want) {
    Utilities.sleep(800);
    return { ok: false, error: 'wrong code' };
  }
  var team = clean(req.team);
  if (!team || !staffList().some(function (p) { return p.team === team; })) return { ok: false, error: 'unknown team' };
  if (req.action === 'cmd_team') {
    return { ok: true, reminders: reminders(sheet('תזכורות'), function (r) { return r.team === team; }) };
  }
  var sub = req.sub || {};
  if (!sub.endpoint || !/^https:\/\//.test(sub.endpoint)) return { ok: false, error: 'bad subscription' };
  var subs = sheet('מנויים');
  var at = findRow(subs, 2, sub.endpoint);
  if (at > 0) subs.deleteRow(at);
  subs.appendRow(['מפקד:' + team, sub.endpoint, JSON.stringify(sub), new Date()]);
  return { ok: true };
}

/** Yard duty edited by hand (team leads, commanders, administration): who, and a short note. */
var DUTY_HEAD = ['תאריך', 'שכבה', 'שעה', 'תפקיד', 'מדריך', 'הערה', 'נערך על ידי', 'מתי'];
var GRADES = ['ט', 'י', 'יא', 'יב', 'יג', 'יד', 'מכללה'];  // 13 and 14 share one list, "מכללה"

function dutyGet(req) {
  var date = String(req.date || '');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return { ok: false, error: 'bad date' };
  try { countVisit(clean(req.who)); } catch (err) {}  // the visit counter must never break the page
  var rows = ensureSheet('תורנויות ידניות', DUTY_HEAD).getDataRange().getDisplayValues().slice(1);
  return { ok: true, edits: rows.filter(function (r) { return r[0] === date; }).map(function (r) {
    return { grade: r[1], hour: Number(r[2]), role: r[3], person: r[4], note: r[5], by: r[6] };
  }) };
}

function dutySet(req) {
  var date = String(req.date || ''), grade = clean(req.grade), hour = Number(req.hour), role = clean(req.role).slice(0, 30);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || GRADES.indexOf(grade) < 0 || !(hour >= 0 && hour <= 12)) return { ok: false, error: 'bad slot' };
  var staff = staffList();
  // Team leads, commanders and administration use their code; any other staff member edits from the
  // phone linked to them, and only the grades of their own team.
  var code = String(req.code || '').trim();
  var ok = code && [setting('קוד מפקדים'), setting('קוד אנשי מנהלה')].some(function (w) { return w && code === String(w); });
  if (!ok && !code && req.name && req.token) {
    var name = clean(req.name), me = staff.filter(function (p) { return p.name === name; })[0];
    var linked = ensureSheet('מכשירים', ['שם', 'מפתח מכשיר', 'קושר']).getDataRange().getValues().slice(1)
      .some(function (r) { return String(r[0]) === name && String(r[1]) === String(req.token); });
    if (!me || !linked) { Utilities.sleep(800); return { ok: false, error: 'not this phone' }; }
    if (teamGrades(me.team).indexOf(grade) < 0) return { ok: false, error: 'not your team' };
    ok = true;
  }
  if (!ok) { Utilities.sleep(800); return { ok: false, error: 'wrong code' }; }
  // one name, or two joined by "|" (the lunch-break roles can take two people)
  var names = String(req.person || '').split('|').map(clean).filter(function (x) { return x; }).slice(0, 2);
  if (names.some(function (n) { return !staff.some(function (p) { return p.name === n; }); })) return { ok: false, error: 'unknown name' };
  var person = names.join('|');
  var tab = ensureSheet('תורנויות ידניות', DUTY_HEAD);
  var rows = tab.getDataRange().getDisplayValues();
  for (var i = rows.length - 1; i >= 1; i--) {
    if (rows[i][0] === date && rows[i][1] === grade && Number(rows[i][2]) === hour && rows[i][3] === role) tab.deleteRow(i + 1);
  }
  if (req.action === 'duty_set') {
    tab.appendRow(["'" + date, grade, "'" + hour, role, person, clean(req.note).slice(0, 40), clean(req.by).slice(0, 40), new Date()]);
  }
  return { ok: true };
}

/** Who opened the site today: one row a day in "כניסות" (date, how many, who), written only when
 *  someone new comes in that day. The names of the day are kept in the script properties. */
function countVisit(who) {
  if (!who || who.length > 40) return;
  var today = Utilities.formatDate(new Date(), 'Asia/Jerusalem', 'yyyy-MM-dd');
  var props = PropertiesService.getScriptProperties(), key = 'visits:' + today;
  var names = JSON.parse(props.getProperty(key) || '[]');
  if (names.indexOf(who) >= 0) return;
  if (!staffList().some(function (p) { return p.name === who; }) && who.indexOf('מפקד ') !== 0) return;
  names.push(who);
  props.setProperty(key, JSON.stringify(names));
  var tab = ensureSheet('כניסות', ['תאריך', 'כמה נכנסו', 'מי']);
  var last = tab.getLastRow();
  var row = last > 1 && tab.getRange(last, 1).getDisplayValue() === today ? last : last + 1;
  tab.getRange(row, 1, 1, 3).setValues([["'" + today, names.length, names.join(', ')]]);
  if (row !== last) {  // a new day: drop the names kept for older days
    props.getKeys().forEach(function (k) { if (k.indexOf('visits:') === 0 && k !== key) props.deleteProperty(k); });
  }
}

// ---- data

function teamGrades(team) {
  var tab = SpreadsheetApp.openById(SHEET_ID).getSheetByName('צוותים');
  if (!tab || !team) return [];
  var rows = tab.getDataRange().getValues(), head = rows.shift();
  var n = head.indexOf('שם הצוות'), g = head.indexOf('שכבות');
  for (var i = 0; i < rows.length; i++) {
    if (String(rows[i][n]).trim() === team) {
      var list = String(rows[i][g] || '').replace(/['"׳״]/g, '').split(/[,\s]+/).filter(function (x) { return x; });
      if (list.indexOf('יג') >= 0 || list.indexOf('יד') >= 0) list.push('מכללה');
      return list;
    }
  }
  return [];
}

function staffList() {
  var tab = SpreadsheetApp.openById(SHEET_ID).getSheetByName(STAFF_TAB);
  var rows = tab.getDataRange().getValues();
  var head = rows.shift();
  var col = function (h) { return head.indexOf(h); };
  var n = col(COL_NAME), t = col('צוות'), l = col('ראש צוות'), act = col('פעיל');
  return rows.filter(function (r) { return r[n] && (act < 0 || String(r[act]).trim() !== 'לא'); }).map(function (r) {
    return { name: clean(r[n]), team: t >= 0 ? String(r[t]).trim() : '', lead: l >= 0 && String(r[l]).trim() === 'כן' };
  });
}

function reminders(rem, keep) {
  var rows = rem.getDataRange().getValues().slice(1);
  return rows.map(function (r) {
    return { id: String(r[0]), name: r[1], team: r[2], date: String(r[3] || ''), dow: r[4] === '' ? null : Number(r[4]), hour: Number(r[5]), text: r[6] };
  }).filter(function (r) { return r.id && keep(r); });
}

function store() {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('STORE_ID');
  if (id) return SpreadsheetApp.openById(id);
  var ss = SpreadsheetApp.create('המערכת האישית — קודים ותזכורות (פרטי)');
  ss.getSheets()[0].setName('מכשירים').appendRow(['שם', 'מפתח מכשיר', 'קושר']);
  ss.insertSheet('תזכורות').appendRow(['מזהה', 'שם', 'צוות', 'תאריך', 'יום בשבוע', 'שעה', 'תזכורת', 'נוצר']);
  ss.insertSheet('הגדרות').appendRow(['קוד אנשי מנהלה', '']);
  ss.getSheetByName('הגדרות').appendRow(['מפתח התראות', Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '')]);
  ss.insertSheet('מנויים').appendRow(['שם', 'כתובת', 'מנוי', 'נוצר']);
  ss.getSheetByName('הגדרות').getRange('A4').setValue('כותבים את קוד אנשי המנהלה בתא B1. רק מי שיודע אותו רואה את כל התזכורות של היחידה.');
  props.setProperty('STORE_ID', ss.getId());
  return ss;
}

function sheet(name) { return store().getSheetByName(name); }

// A personal recovery code links a new browser without a code from a linked one (an iPhone home-screen
// icon that was deleted, a new phone). Only its hash is kept, in "שחזור".
function recoveryHash_(name, code) {
  return Utilities.base64Encode(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, 'recovery:' + name + ':' + code, Utilities.Charset.UTF_8));
}
function recoveryRow_(name) {
  return findRow(ensureSheet('שחזור', ['שם', 'טביעת קוד', 'עודכן']), 1, name);
}
function recoveryOk_(name, code) {
  var r = recoveryRow_(name);
  return r > 0 && String(sheet('שחזור').getRange(r, 2).getValue()) === recoveryHash_(name, code);
}
function newRecovery_(name) {
  var code = String(10000000 + parseInt(Utilities.getUuid().replace(/-/g, '').slice(0, 12), 16) % 90000000);
  var t = ensureSheet('שחזור', ['שם', 'טביעת קוד', 'עודכן']), r = findRow(t, 1, name), row = [name, recoveryHash_(name, code), new Date()];
  if (r > 0) t.getRange(r, 1, 1, 3).setValues([row]); else t.appendRow(row);
  return code;
}

function ensureSheet(name, head) {
  var ss = store(), tab = ss.getSheetByName(name);
  if (!tab) { tab = ss.insertSheet(name); tab.appendRow(head); }
  return tab;
}

function setting(key) {
  var tab = sheet('הגדרות');
  var rows = tab.getDataRange().getValues();
  for (var i = 0; i < rows.length; i++) if (rows[i][0] === key) return rows[i][1];
  tab.appendRow([key, '']);  // a new setting shows up as an empty row, ready to fill in
  return '';
}

function findRow(tab, col, value) {
  var vals = tab.getRange(1, col, Math.max(tab.getLastRow(), 1), 1).getValues();
  for (var i = 1; i < vals.length; i++) if (String(vals[i][0]) === value) return i + 1;
  return -1;
}

function clean(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }

function out(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/** Run once from the editor (▶ setup) to create the private spreadsheet and approve access. */
function setup() {
  var ss = store();
  Logger.log('הגיליון הפרטי מוכן: ' + ss.getUrl());
}
