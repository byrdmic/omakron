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
