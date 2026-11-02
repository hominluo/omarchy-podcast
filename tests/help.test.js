const test = require("node:test")
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")

const ROOT = path.join(__dirname, "..")

function read(relative) {
  return fs.readFileSync(path.join(ROOT, relative), "utf8")
}

test("now-playing help matches the keys the view handles", () => {
  const help = read("components/KeyHelp.qml")
  const nowPlaying = read("components/views/NowPlayingView.qml")
  const table = help.split("\n").find((line) => line.includes('case "nowPlaying"'))
  assert.ok(table, "KeyHelp has a nowPlaying row")
  // n opens Notes; N is the previous match inside a transcript.
  assert.match(table, /\["n", "Notes/)
  assert.match(table, /\["N", "Previous transcript match"\]/)
  // t/v/x/m are handled by the view and must be advertised.
  for (const key of ["t", "v", "x", "m"]) {
    assert.ok(table.includes(`["${key}", `), `help mentions ${key}`)
    assert.ok(nowPlaying.includes(`case "${key}"`), `view handles ${key}`)
  }
  assert.match(nowPlaying, /case "n":.*tab = "notes"/)
})

test("ListWithDetail does not shadow its list alias with an id", () => {
  const source = read("components/ListWithDetail.qml")
  assert.match(source, /property alias list: episodeList/)
  assert.ok(!/id:\s*list\b/.test(source))
})
