const test = require("node:test")
const assert = require("node:assert/strict")
const M = require("../Model.js")

test("formatTime", () => {
  assert.equal(M.formatTime(0), "0:00")
  assert.equal(M.formatTime(75), "1:15")
  assert.equal(M.formatTime(3725), "1:02:05")
  assert.equal(M.formatTime(-4), "0:00")
})

test("formatDuration and remaining", () => {
  assert.equal(M.formatDuration(42 * 60), "42 min")
  assert.equal(M.formatDuration(3900), "1 h 05 min")
  assert.equal(M.formatDuration(20), "< 1 min")
  assert.equal(M.remainingText(600, 3000), "40 min left")
  assert.equal(M.remainingText(0, 0), "")
})

test("formatRelativeDate", () => {
  const now = new Date(2026, 8, 12, 12, 0, 0).getTime()
  const day = 86400
  const at = (daysAgo) => (now / 1000) - daysAgo * day
  assert.equal(M.formatRelativeDate(at(0), now), "Today")
  assert.equal(M.formatRelativeDate(at(1), now), "Yesterday")
  assert.equal(M.formatRelativeDate(at(3), now), "3 days ago")
  assert.equal(M.formatRelativeDate(at(30), now), "Aug 13")
  assert.equal(M.formatRelativeDate(at(400), now), "Aug 8, 2025")
  assert.equal(M.formatRelativeDate(0, now), "")
})

test("indexAtTime binary search", () => {
  const items = [{ startTime: 0 }, { startTime: 10 }, { startTime: 20.5 }]
  assert.equal(M.indexAtTime(items, -1), -1)
  assert.equal(M.indexAtTime(items, 0), 0)
  assert.equal(M.indexAtTime(items, 9.99), 0)
  assert.equal(M.indexAtTime(items, 10), 1)
  assert.equal(M.indexAtTime(items, 99), 2)
  assert.equal(M.indexAtTime([], 5), -1)
})

test("speed ring", () => {
  assert.equal(M.nextSpeed(1, 1), 1.25)
  assert.equal(M.nextSpeed(2, 1), 1)
  assert.equal(M.nextSpeed(1, -1), 2)
  assert.equal(M.nextSpeed(1.3, 1), 1.5)
  assert.equal(M.nextSpeed(1.3, -1), 1.25)
  assert.equal(M.formatSpeed(1.5), "1.5×")
  assert.equal(M.formatSpeed(1), "1×")
})

test("sleepLabel", () => {
  const now = 1_000_000_000
  assert.equal(M.sleepLabel({ mode: "off" }, now), "")
  assert.equal(M.sleepLabel({ mode: "episode" }, now), "end of episode")
  assert.equal(M.sleepLabel({ mode: "minutes", endsAt: now / 1000 + 20 * 60 }, now), "20 min")
})

test("searchMatches and filterEpisodes", () => {
  const segs = [{ body: "Hello world" }, { body: "nothing" }, { body: "WORLD peace" }]
  assert.deepEqual(M.searchMatches(segs, "world"), [0, 2])
  assert.deepEqual(M.searchMatches(segs, ""), [])
  const eps = [{ title: "Alpha", podcastTitle: "Show" }, { title: "Beta", podcastTitle: "Other" }]
  assert.equal(M.filterEpisodes(eps, "show").length, 1)
  assert.equal(M.filterEpisodes(eps, "").length, 2)
})

test("patchById / removeById return new arrays", () => {
  const items = [{ id: 1, a: 1 }, { id: 2, a: 2 }]
  const patched = M.patchById(items, { id: 2, a: 9 })
  assert.notEqual(patched, items)
  assert.equal(patched[1].a, 9)
  assert.equal(M.patchById(items, { id: 3 }).length, 3)
  assert.equal(M.removeById(items, 1).length, 1)
})

test("sanitizeShowNotes strips the dangerous parts", () => {
  const dirty = '<p onclick="x()">Hi <img src="http://t/x.png"><a href="javascript:evil()">l</a></p><script>bad()</script><style>p{}</style>'
  const clean = M.sanitizeShowNotes(dirty)
  assert.ok(!/<img/i.test(clean))
  assert.ok(!/<script/i.test(clean))
  assert.ok(!/<style/i.test(clean))
  assert.ok(!/onclick/i.test(clean))
  assert.ok(!/javascript:/i.test(clean))
  assert.ok(/<p>Hi/.test(clean))
})

test("escapeHtml neutralises markup", () => {
  assert.equal(M.escapeHtml('<b>"x" & y</b>'), "&lt;b&gt;&quot;x&quot; &amp; y&lt;/b&gt;")
  assert.equal(M.escapeHtml(null), "")
})

test("canonicalFeedUrl folds twins like the daemon", () => {
  assert.equal(M.canonicalFeedUrl("http://Feeds.Example.com/show/"), "https://feeds.example.com/show")
  assert.equal(M.canonicalFeedUrl("https://feeds.example.com/show?x=1#frag"), "https://feeds.example.com/show?x=1")
  assert.equal(M.canonicalFeedUrl("https://host"), "https://host/")
  assert.equal(M.canonicalFeedUrl("not a url"), "not a url")
})

test("formatBytes", () => {
  assert.equal(M.formatBytes(512), "512 B")
  assert.equal(M.formatBytes(1536), "1.5 KB")
  assert.equal(M.formatBytes(45 * 1024 * 1024), "45 MB")
})
