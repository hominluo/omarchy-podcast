// Pure helpers shared by every QML file in the plugin. No QML imports, no
// state: everything here is a function of its arguments, which is what lets
// tests/model.test.js run it under node.

var SPEEDS = [1, 1.25, 1.5, 1.75, 2]

function pad2(n) {
  return (n < 10 ? "0" : "") + n
}

// 75 -> "1:15", 3725 -> "1:02:05". Negative or missing -> "0:00".
function formatTime(seconds) {
  var total = Math.max(0, Math.floor(Number(seconds) || 0))
  var h = Math.floor(total / 3600)
  var m = Math.floor((total % 3600) / 60)
  var s = total % 60
  if (h > 0) return h + ":" + pad2(m) + ":" + pad2(s)
  return m + ":" + pad2(s)
}

// Rounded human duration for lists: "42 min", "1 h 05 min", "< 1 min".
function formatDuration(seconds) {
  var total = Math.max(0, Math.round(Number(seconds) || 0))
  if (total === 0) return ""
  if (total < 60) return "< 1 min"
  var h = Math.floor(total / 3600)
  var m = Math.round((total % 3600) / 60)
  if (m === 60) { h += 1; m = 0 }
  if (h > 0) return h + " h" + (m > 0 ? " " + pad2(m) + " min" : "")
  return m + " min"
}

// Time left in an episode at 1x: "38 min left".
function remainingText(position, duration) {
  var left = Math.max(0, (Number(duration) || 0) - (Number(position) || 0))
  if (!duration) return ""
  var text = formatDuration(left)
  return text === "" ? "" : text + " left"
}

// "Today", "Yesterday", "3 days ago", "Mar 4", "Mar 4, 2025". `nowMs` is a
// parameter so tests are deterministic.
function formatRelativeDate(epochSeconds, nowMs) {
  var t = Number(epochSeconds) || 0
  if (t <= 0) return ""
  var now = nowMs === undefined ? Date.now() : nowMs
  var date = new Date(t * 1000)
  var today = new Date(now)
  var startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()
  var dayMs = 86400000
  var diffDays = Math.floor((startOfToday - new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()) / dayMs)
  if (diffDays <= 0) return "Today"
  if (diffDays === 1) return "Yesterday"
  if (diffDays < 7) return diffDays + " days ago"
  var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
  var text = months[date.getMonth()] + " " + date.getDate()
  if (date.getFullYear() !== today.getFullYear()) text += ", " + date.getFullYear()
  return text
}

function elide(text, max) {
  var value = String(text || "")
  if (value.length <= max) return value
  return value.substring(0, Math.max(0, max - 1)).replace(/\s+$/, "") + "…"
}

// Binary search over items sorted by startTime: index of the last item whose
// startTime <= t, or -1.
function indexAtTime(items, t) {
  if (!items || items.length === 0) return -1
  var time = Number(t) || 0
  var lo = 0, hi = items.length - 1, found = -1
  while (lo <= hi) {
    var mid = (lo + hi) >> 1
    var start = Number(items[mid].startTime) || 0
    if (start <= time) { found = mid; lo = mid + 1 } else { hi = mid - 1 }
  }
  return found
}

function chapterIndexAt(chapters, t) {
  return indexAtTime(chapters, t)
}

// Plain text on its way into a RichText Text: the four characters that would
// otherwise be read as markup.
function escapeHtml(text) {
  return String(text || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")
}

// Transcript segments also carry endTime; between segments (a pause) the
// previous one stays current so the highlight does not flicker off.
function segmentIndexAt(segments, t) {
  return indexAtTime(segments, t)
}

function nextSpeed(current, direction) {
  var speed = Number(current) || 1
  var dir = direction < 0 ? -1 : 1
  // Snap to the ring: first entry strictly beyond the current value.
  if (dir > 0) {
    for (var i = 0; i < SPEEDS.length; i++) if (SPEEDS[i] > speed + 0.001) return SPEEDS[i]
    return SPEEDS[0]
  }
  for (var j = SPEEDS.length - 1; j >= 0; j--) if (SPEEDS[j] < speed - 0.001) return SPEEDS[j]
  return SPEEDS[SPEEDS.length - 1]
}

function formatSpeed(speed) {
  var value = Number(speed) || 1
  var text = (Math.round(value * 100) / 100).toString()
  return text + "×"
}

// Sleep timer label from the `sleep` slice: {mode, endsAt(ms epoch)}.
function sleepLabel(sleep, nowMs) {
  if (!sleep || sleep.mode === "off") return ""
  if (sleep.mode === "episode") return "end of episode"
  if (sleep.mode === "chapter") return "end of chapter"
  var now = nowMs === undefined ? Date.now() : nowMs
  var left = Math.max(0, Math.round(((Number(sleep.endsAt) || 0) * 1000 - now) / 60000))
  return left <= 0 ? "< 1 min" : left + " min"
}

// Case-insensitive substring filter over title/podcast/notes text.
function filterEpisodes(items, query) {
  var q = String(query || "").trim().toLowerCase()
  if (q === "") return items || []
  var out = []
  for (var i = 0; i < (items || []).length; i++) {
    var item = items[i]
    var hay = (String(item.title || "") + " " + String(item.podcastTitle || "") + " " + String(item.notesText || "")).toLowerCase()
    if (hay.indexOf(q) !== -1) out.push(item)
  }
  return out
}

// Indexes of transcript segments containing `query` (case-insensitive).
function searchMatches(segments, query) {
  var q = String(query || "").trim().toLowerCase()
  var out = []
  if (q === "") return out
  for (var i = 0; i < (segments || []).length; i++) {
    var body = String(segments[i].body || "").toLowerCase()
    if (body.indexOf(q) !== -1) out.push(i)
  }
  return out
}

// Replace or append `item` in `items` by id; returns a new array so QML
// bindings re-evaluate.
function patchById(items, item) {
  var out = []
  var replaced = false
  for (var i = 0; i < (items || []).length; i++) {
    if (items[i] && item && items[i].id === item.id) { out.push(item); replaced = true }
    else out.push(items[i])
  }
  if (!replaced && item) out.push(item)
  return out
}

function removeById(items, id) {
  var out = []
  for (var i = 0; i < (items || []).length; i++) if (!items[i] || items[i].id !== id) out.push(items[i])
  return out
}

// Defensive second pass over show notes the daemon already sanitized. Qt's
// rich text is small, so this only has to keep the allowed subset and neuter
// anything that could fetch or run: images, scripts, styles, event handlers.
function sanitizeShowNotes(html) {
  var text = String(html || "")
  text = text.replace(/<\s*(script|style|iframe|object|embed|svg|noscript|head|title)[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "")
  text = text.replace(/<\s*img[^>]*>/gi, "")
  text = text.replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "")
  text = text.replace(/href\s*=\s*("|')\s*javascript:[^"']*\1/gi, 'href="#"')
  return text
}

function stripTags(html) {
  return String(html || "").replace(/<[^>]*>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/\s+/g, " ").trim()
}

// Percent done for a progress hairline, 0..1.
function progressFraction(position, duration) {
  var d = Number(duration) || 0
  if (d <= 0) return 0
  return Math.max(0, Math.min(1, (Number(position) || 0) / d))
}

// Human bytes for download rows.
function formatBytes(bytes) {
  var value = Number(bytes) || 0
  if (value < 1024) return value + " B"
  var units = ["KB", "MB", "GB", "TB"]
  var i = -1
  do { value /= 1024; i++ } while (value >= 1024 && i < units.length - 1)
  return (value >= 10 ? Math.round(value) : Math.round(value * 10) / 10) + " " + units[i]
}

if (typeof module !== "undefined") {
  module.exports = {
    SPEEDS: SPEEDS, formatTime: formatTime, formatDuration: formatDuration, remainingText: remainingText,
    formatRelativeDate: formatRelativeDate, elide: elide, indexAtTime: indexAtTime, chapterIndexAt: chapterIndexAt,
    segmentIndexAt: segmentIndexAt, nextSpeed: nextSpeed, formatSpeed: formatSpeed, sleepLabel: sleepLabel,
    filterEpisodes: filterEpisodes, searchMatches: searchMatches, patchById: patchById, removeById: removeById,
    sanitizeShowNotes: sanitizeShowNotes, stripTags: stripTags, escapeHtml: escapeHtml, progressFraction: progressFraction, formatBytes: formatBytes,
  }
}
