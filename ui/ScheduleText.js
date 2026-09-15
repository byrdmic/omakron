// Plain words for a five-field cron expression, and 12-hour clock times.
// Anything the editor did not write is shown as the raw expression.
.pragma library

var DAY_NAMES = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday", "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday"}
var SHORT_NAMES = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun"}

// 9, 0 -> "9:00 AM"; 13, 30 -> "1:30 PM"
function clock(hour, minute) {
  var h = hour % 12
  if (h === 0) h = 12
  return h + ":" + (minute < 10 ? "0" + minute : minute) + " " + (hour < 12 ? "AM" : "PM")
}

// "2026-09-14T09:00:00-04:00" -> "Mon 14 Sep, 9:00 AM"; the wall-clock part is used as written
function localMoment(iso) {
  var d = new Date(iso.slice(0, 19))
  if (isNaN(d)) return iso
  return Qt.formatDateTime(d, "ddd d MMM") + ", " + clock(d.getHours(), d.getMinutes())
}

function describe(cron) {
  if (!cron) return ""
  var f = cron.trim().split(/\s+/)
  if (f.length !== 5) return cron
  var minute = f[0], hour = f[1], dom = f[2], month = f[3], dow = f[4]
  if (dom !== "*" || month !== "*") return cron
  if (hour === "*" && /^\d+$/.test(minute)) return minute === "0" ? "Every hour" : "Every hour at :" + (minute.length < 2 ? "0" + minute : minute)
  if (!/^\d+$/.test(minute) || !/^\d+$/.test(hour)) return cron
  var at = clock(Number(hour), Number(minute))
  if (dow === "*") return "Daily at " + at
  if (dow === "1-5") return "Weekdays at " + at
  if (dow === "0,6" || dow === "6,0") return "Weekends at " + at
  var days = dow.split(",")
  if (days.length === 1 && DAY_NAMES[days[0]]) return DAY_NAMES[days[0]] + "s at " + at
  var names = []
  for (var i = 0; i < days.length; i++) {
    if (!SHORT_NAMES[days[i]]) return cron
    names.push(SHORT_NAMES[days[i]])
  }
  return names.join(", ") + " at " + at
}

// ---- run history wording -------------------------------------------------

var OPEN = {"queued": true, "claimed": true, "running": true}
var WORDS = {queued: "Queued", claimed: "Starting", running: "Running", succeeded: "Succeeded", failed: "Failed",
  timed_out: "Timed out", canceled: "Canceled", interrupted: "Interrupted", skipped: "Skipped"}

function isOpen(status) { return OPEN[status] === true }
function statusWord(status) { return WORDS[status] || status || "" }

// A UTC instant -> "Mon 15 Sep, 12:41 PM" in this machine's zone
function moment(iso) {
  if (!iso) return ""
  var d = new Date(iso)
  if (isNaN(d)) return iso
  return Qt.formatDateTime(d, "ddd d MMM") + ", " + clock(d.getHours(), d.getMinutes())
}

// "just now", "4 min ago", "3 h ago", "yesterday", or the moment
function relative(iso, nowMs) {
  if (!iso) return ""
  var d = new Date(iso)
  if (isNaN(d)) return iso
  var s = Math.max(0, Math.round(((nowMs || Date.now()) - d.getTime()) / 1000))
  if (s < 60) return "just now"
  if (s < 3600) return Math.round(s / 60) + " min ago"
  if (s < 86400) return Math.round(s / 3600) + " h ago"
  if (s < 172800) return "yesterday"
  return moment(iso)
}

// A UTC instant ahead of now -> "in under a minute", "in 3 min", "in 2 h 15 min",
// "tomorrow, 9:00 AM", or the moment
function until(iso, nowMs) {
  if (!iso) return ""
  var d = new Date(iso)
  if (isNaN(d)) return iso
  var now = new Date(nowMs || Date.now())
  var s = Math.round((d.getTime() - now.getTime()) / 1000)
  if (s < 60) return "in under a minute"
  if (s < 3600) return "in " + Math.round(s / 60) + " min"
  if (s < 86400) {
    var h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60)
    if (m === 60) { h += 1; m = 0 }
    return "in " + h + " h" + (m > 0 ? " " + m + " min" : "")
  }
  var tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)
  if (d.getFullYear() === tomorrow.getFullYear() && d.getMonth() === tomorrow.getMonth() && d.getDate() === tomorrow.getDate())
    return "tomorrow, " + clock(d.getHours(), d.getMinutes())
  return moment(iso)
}

// "Next run: in 3 min" for a cron routine whose schedule is on, else ""
function nextRun(routine, nowMs) {
  if (!routine || routine.schedule_kind !== "cron" || !routine.enabled || !routine.next_run_at) return ""
  return "Next run: " + until(routine.next_run_at, nowMs)
}

// seconds -> "12 s", "4 min 12 s", "1 h 5 min"
function duration(seconds) {
  if (seconds === null || seconds === undefined) return ""
  var s = Math.round(seconds)
  if (s < 60) return s + " s"
  if (s < 3600) return Math.floor(s / 60) + " min " + (s % 60) + " s"
  return Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min"
}

// One line about a routine's most recent run
function lastRun(run, nowMs) {
  if (!run) return "Never run"
  if (isOpen(run.status)) return statusWord(run.status) + "…"
  return statusWord(run.status) + " · " + relative(run.ended_at || run.enqueued_at, nowMs)
}
