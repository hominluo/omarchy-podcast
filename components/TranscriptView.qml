import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// The transcript of one episode, following the audio.
//
// Segments live in a ListModel that only ever appends, so a transcription
// streaming in never rebuilds the rows already on screen. While `following`
// the row for the current position is centred; any scroll, wheel or cursor
// move hands control to the reader until `f` or "Jump to now".
Item {
  id: root

  property var browser: null
  property int episodeId: 0
  readonly property var service: browser ? browser.service : null
  readonly property color foreground: browser ? browser.foreground : Color.menu.text
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property string fontFamily: browser ? browser.fontFamily : Style.font.menuFamily
  readonly property bool isCurrent: !!service && !!service.player && service.player.episodeId === episodeId

  property string status: "loading"     // loading | none | queued | partial | complete | error
  property string source: ""
  property string language: ""
  property bool timed: true
  property int percent: 0
  property bool canTranscribe: false
  property int estimateSec: 0
  property string reason: ""
  property string error: ""
  property string stage: ""
  property var segments: []

  property bool following: true
  property int currentIndex: -1
  property bool searching: false
  property string query: ""
  property var matches: []
  property int matchIndex: -1
  property int cursorIndex: -1
  property bool cursorActive: false
  property int requestSerial: 0

  function load() {
    if (!service || episodeId <= 0) { status = "none"; return }
    var serial = ++requestSerial
    var wanted = episodeId
    status = "loading"
    service.requestTranscript(wanted, "paragraphs", function(ok, result) {
      if (serial !== root.requestSerial || wanted !== root.episodeId) return
      if (!ok) { root.status = "error"; root.error = result && result.message ? String(result.message) : "Could not load the transcript"; return }
      root.apply(result)
    })
  }

  function apply(result) {
    root.status = String(result.status || "none")
    root.source = String(result.source || "")
    root.language = String(result.language || "")
    root.timed = result.timed !== false
    root.percent = Number(result.percent) || 0
    root.canTranscribe = result.canTranscribe === true
    root.estimateSec = Number(result.estimateSec) || 0
    root.reason = String(result.reason || "")
    root.error = String(result.error || "")
    root.setSegments(result.segments || [])
  }

  function setSegments(list) {
    rows.clear()
    var out = []
    for (var i = 0; i < list.length; i++) { rows.append(list[i]); out.push(list[i]) }
    root.segments = out
    root.currentIndex = -1
    root.setQuery(root.query)
  }

  function appendSegments(list) {
    if (!list || list.length === 0) return
    var out = root.segments.slice()
    for (var i = 0; i < list.length; i++) { rows.append(list[i]); out.push(list[i]) }
    root.segments = out
  }

  function startTranscription() {
    if (!service || episodeId <= 0) return
    service.transcribe(episodeId, function(ok, result) {
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not start"; root.status = "error"; return }
      root.status = result && result.status === "complete" ? "complete" : "queued"
      root.percent = 0
      root.stage = "queued"
      if (root.status === "complete") root.load()
    })
  }

  function cancelTranscription() {
    if (service && episodeId > 0) service.cancelTranscription(episodeId)
  }

  function jumpToNow() {
    following = true
    cursorActive = false
    if (currentIndex >= 0) list.positionViewAtIndex(currentIndex, ListView.Center)
  }

  function seekTo(index) {
    var seg = root.segments[index]
    if (!seg || !service) return
    if (!isCurrent) service.play(episodeId, Number(seg.startTime) || 0)
    else service.seek(Number(seg.startTime) || 0)
    following = true
  }

  function setQuery(text) {
    query = String(text || "")
    matches = Model.searchMatches(segments, query)
    matchIndex = matches.length ? 0 : -1
    if (matchIndex >= 0) revealMatch()
  }

  function nextMatch(direction) {
    if (matches.length === 0) return
    matchIndex = (matchIndex + direction + matches.length) % matches.length
    revealMatch()
  }

  function revealMatch() {
    if (matchIndex < 0) return
    following = false
    cursorActive = true
    cursorIndex = matches[matchIndex]
    list.positionViewAtIndex(cursorIndex, ListView.Center)
  }

  function startSearch() {
    searching = true
    if (browser) browser.editing = true
    Qt.callLater(function() { searchField.forceActiveFocus(); searchField.selectAll() })
  }

  function endSearch(clear) {
    if (clear) { searchField.text = ""; setQuery("") }
    searching = query !== ""
    if (browser) { browser.editing = false; browser.refocus() }
  }

  function moveCursor(delta) {
    if (segments.length === 0) return
    following = false
    if (!cursorActive) { cursorActive = true; cursorIndex = currentIndex >= 0 ? currentIndex : 0 }
    else cursorIndex = Math.max(0, Math.min(segments.length - 1, cursorIndex + delta))
    list.positionViewAtIndex(cursorIndex, ListView.Contain)
  }

  function handleEscape() {
    if (searching || query !== "") { endSearch(true); return true }
    if (!following && isCurrent) { jumpToNow(); return true }
    return false
  }

  function handleKey(event) {
    if (event.text === "/") { startSearch(); return true }
    if (event.text === "n") { nextMatch(1); return true }
    if (event.text === "N") { nextMatch(-1); return true }
    if (event.text === "f") { jumpToNow(); return true }
    if (event.key === Qt.Key_Down || event.text === "j") { moveCursor(1); return true }
    if (event.key === Qt.Key_Up || event.text === "k") { moveCursor(-1); return true }
    if (event.key === Qt.Key_PageDown) { moveCursor(8); return true }
    if (event.key === Qt.Key_PageUp) { moveCursor(-8); return true }
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      if (root.status === "none" && root.canTranscribe) { startTranscription(); return true }
      if (cursorActive && cursorIndex >= 0) { seekTo(cursorIndex); return true }
      return false
    }
    return false
  }

  onEpisodeIdChanged: { root.query = ""; root.following = true; root.cursorActive = false; load() }
  Component.onCompleted: load()

  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onEvent(name, data) {
      if (name !== "transcript-progress" || !data || Number(data.episodeId) !== root.episodeId) return
      root.stage = String(data.stage || "")
      root.percent = Number(data.percent) || 0
      if (data.language) root.language = String(data.language)
      if (data.stage === "transcribing") {
        if (root.status !== "partial") { root.status = "partial"; if (root.segments.length === 0) rows.clear() }
        root.appendSegments(data.segments || [])
      } else if (data.stage === "done") {
        root.status = "complete"
        root.load()
      } else if (data.stage === "error") {
        root.status = "error"
        root.error = String(data.error || "Transcription failed")
      } else {
        root.status = "queued"
      }
    }
  }

  // Follow the audio.
  PlaybackClock {
    id: clock
    service: root.service
    running: root.visible && root.isCurrent && (!root.browser || root.browser.opened)
  }

  onSegmentsChanged: updateCurrent()
  Connections {
    target: clock
    function onPositionChanged() { root.updateCurrent() }
  }

  function updateCurrent() {
    if (!isCurrent || !timed) return
    var index = Model.segmentIndexAt(segments, clock.position)
    if (index === currentIndex) return
    currentIndex = index
    if (following && index >= 0 && !list.moving) list.positionViewAtIndex(index, ListView.Center)
  }

  ListModel { id: rows }

  Column {
    anchors.fill: parent
    spacing: Style.space(8)

    // ---------- header ----------
    Item {
      width: parent.width
      height: Math.max(Style.spacing.controlHeight, headerColumn.implicitHeight)

      Column {
        id: headerColumn
        anchors.left: parent.left
        anchors.right: searchField.visible ? matchCount.left : parent.right
        anchors.rightMargin: Style.space(10)
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.spacing.xxs

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: {
            switch (root.status) {
              case "loading": return "Loading transcript…"
              case "queued": return root.stage === "fetching" ? "Fetching the audio…" : (root.stage === "converting" ? "Preparing the audio…" : "Waiting to transcribe…")
              case "partial": return "Transcribing…  " + root.percent + "%"
              case "complete": return (root.source === "whisper" ? "Transcribed locally" : "Transcript from the feed") + (root.language ? "  ·  " + root.language.toUpperCase() : "") + (root.timed ? "" : "  ·  no timestamps")
              case "error": return "Transcript unavailable"
              default: return root.canTranscribe ? "No transcript in the feed" : "No transcript"
            }
          }
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          elide: Text.ElideRight
          renderType: Text.NativeRendering
        }

        Rectangle {
          visible: root.status === "partial" || (root.status === "queued" && root.percent > 0)
          width: parent.width
          height: Style.spacing.xxs
          radius: height / 2
          color: Util.alpha(root.foreground, 0.12)
          Rectangle {
            height: parent.height
            width: Math.round(parent.width * Math.min(1, root.percent / 100))
            radius: height / 2
            color: Style.selectedStateColor(root.foreground, Color.accent)
            Behavior on width { NumberAnimation { duration: 240 } }
          }
        }
      }

      Text {
        id: matchCount
        visible: searchField.visible && root.query !== ""
        anchors.right: searchField.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        width: visible ? implicitWidth : 0
        textFormat: Text.PlainText
        text: root.matches.length > 0 ? (root.matchIndex + 1) + " / " + root.matches.length : "no matches"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        renderType: Text.NativeRendering
      }

      TextField {
        id: searchField
        visible: root.searching || root.query !== ""
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(220)
        placeholderText: "Search transcript"
        foreground: root.foreground
        onTextChanged: root.setQuery(text)
        onActiveFocusChanged: if (!activeFocus && root.searching) root.endSearch(false)
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) { root.endSearch(true); event.accepted = true }
          else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { root.nextMatch(event.modifiers & Qt.ShiftModifier ? -1 : 1); root.endSearch(false); event.accepted = true }
          else if (event.key === Qt.Key_Down) { root.endSearch(false); event.accepted = true }
        }
      }
    }

    // ---------- body ----------
    Item {
      width: parent.width
      height: parent.height - y

      ListView {
        id: list
        anchors.fill: parent
        visible: root.segments.length > 0
        clip: true
        model: rows
        spacing: Style.spacing.xxs
        boundsBehavior: Flickable.StopAtBounds
        cacheBuffer: Style.space(800)
        reuseItems: true
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        onMovementStarted: root.following = false

        WheelHandler {
          onWheel: root.following = false
        }

        delegate: CursorSurface {
          id: row
          required property int index
          required property real startTime
          required property string body
          required property string speaker
          width: list.width - Style.space(10)
          implicitHeight: content.implicitHeight + Style.spacing.lg
          foreground: root.foreground
          current: index === root.currentIndex
          hasCursor: root.cursorActive && index === root.cursorIndex
          opacity: root.matches.length > 0 && root.matches.indexOf(index) === -1 ? 0.45 : 1

          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: root.timed ? Qt.PointingHandCursor : Qt.ArrowCursor
            onEntered: { root.cursorActive = true; root.cursorIndex = index }
            onClicked: if (root.timed) root.seekTo(index)
          }

          Row {
            id: content
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.space(10)
            anchors.rightMargin: Style.space(10)
            spacing: Style.space(10)

            Text {
              textFormat: Text.PlainText
              text: root.timed ? Model.formatTime(startTime) : ""
              visible: root.timed
              color: index === root.currentIndex ? root.foreground : Qt.darker(root.foreground, 1.7)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              width: Style.space(52)
              anchors.top: parent.top
              anchors.topMargin: Style.space(2)
              renderType: Text.NativeRendering
            }

            Column {
              width: parent.width - (root.timed ? Style.space(52) + parent.spacing : 0)
              spacing: Style.spacing.xxs

              Text {
                visible: speaker !== ""
                textFormat: Text.PlainText
                text: speaker
                color: Qt.darker(root.foreground, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                renderType: Text.NativeRendering
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: body
                color: index === root.currentIndex ? root.foreground : Qt.darker(root.foreground, 1.2)
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                wrapMode: Text.WordWrap
                lineHeight: 1.2
                renderType: Text.NativeRendering
              }
            }
          }
        }
      }

      // Empty / call-to-action states.
      Column {
        anchors.centerIn: parent
        visible: root.segments.length === 0
        width: Math.min(parent.width - Style.space(40), Style.space(440))
        spacing: Style.space(12)

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: {
            switch (root.status) {
              case "loading": return "Loading…"
              case "queued": return root.stage === "fetching" ? "Fetching the audio first…" : (root.stage === "converting" ? "Preparing the audio…" : "Queued for transcription.")
              case "partial": return "Transcribing… the first lines appear in a moment."
              case "error": return root.error !== "" ? root.error : "Transcript unavailable."
              default:
                if (root.canTranscribe) return "This episode ships no transcript. Transcribe it here with whisper.cpp"
                  + (root.estimateSec > 0 ? " — about " + Model.formatDuration(root.estimateSec) : "") + "."
                return root.reason !== "" ? root.reason : "This episode has no transcript."
            }
          }
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
          renderType: Text.NativeRendering
        }

        Button {
          visible: root.status === "none" && root.canTranscribe
          anchors.horizontalCenter: parent.horizontalCenter
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          iconText: "󰍬"
          text: "Transcribe"
          tooltipText: "Run whisper.cpp on this machine  (Enter)"
          onClicked: root.startTranscription()
        }

        Button {
          visible: root.status === "queued" || root.status === "partial"
          anchors.horizontalCenter: parent.horizontalCenter
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          iconText: "󰅙"
          text: "Cancel"
          onClicked: root.cancelTranscription()
        }

        Button {
          visible: root.status === "error" && root.canTranscribe
          anchors.horizontalCenter: parent.horizontalCenter
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          iconText: "󰑐"
          text: "Try again"
          onClicked: root.startTranscription()
        }
      }

      Button {
        visible: root.segments.length > 0 && !root.following && root.isCurrent && root.timed
        anchors.bottom: parent.bottom
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottomMargin: Style.space(10)
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.bodySmall
        bordered: true
        iconText: "󰐊"
        text: "Jump to now"
        tooltipText: "Follow the audio again  (f)"
        onClicked: root.jumpToNow()
      }
    }
  }
}
