import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// The detail pane: one episode with its actions, show notes and chapters.
// Fed a summary straight away and a full record once `episode-get` answers.
Item {
  id: root

  property var browser: null
  property var episode: null          // summary
  property var detail: null           // full record from episode-get
  property bool active: false          // the pane has keyboard focus
  property int actionIndex: 0

  signal closeRequested()

  readonly property var service: browser ? browser.service : null
  readonly property color foreground: browser ? browser.foreground : Color.menu.text
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property string fontFamily: browser ? browser.fontFamily : Style.font.menuFamily
  readonly property var record: detail && episode && detail.id === episode.id ? detail : episode
  readonly property bool isCurrent: !!service && !!episode && service.player && service.player.episodeId === episode.id
  readonly property bool playing: isCurrent && service.playing
  readonly property var chapters: isCurrent ? service.chapters : []
  readonly property string metaLine: {
    if (!record) return ""
    var parts = []
    var date = Model.formatRelativeDate(record.pubDate)
    if (date) parts.push(date)
    if (record.duration) parts.push(Model.formatDuration(record.duration))
    if (record.played) parts.push("Played")
    else if (record.position > 0) parts.push(Model.remainingText(record.position, record.duration))
    if (record.season && record.episodeNumber) parts.push("S" + record.season + "E" + record.episodeNumber)
    else if (record.episodeNumber) parts.push("#" + record.episodeNumber)
    if (record.episodeType && record.episodeType !== "full") parts.push(record.episodeType)
    return parts.join("  ·  ")
  }

  readonly property var actions: {
    if (!record) return []
    var list = []
    list.push({ key: "play", glyph: playing ? "󰏤" : "󰐊", label: playing ? "Pause" : (isCurrent ? "Resume" : "Play"), hint: "p" })
    list.push({ key: "queue", glyph: record.queued ? "󰐑" : "󰐒", label: record.queued ? "In Up Next" : "Up Next", hint: "a" })
    if (record.download === "done") list.push({ key: "download", glyph: "󰆴", label: "Delete file", hint: "d" })
    else if (record.download === "downloading" || record.download === "queued") list.push({ key: "download", glyph: "󰅙", label: "Cancel", hint: "d" })
    else list.push({ key: "download", glyph: "󰇚", label: "Download", hint: "d" })
    list.push({ key: "played", glyph: record.played ? "󰄱" : "󰗠", label: record.played ? "Unplayed" : "Played", hint: "m" })
    list.push({ key: "archive", glyph: record.state === "inbox" ? "󱈎" : "󰚇", label: record.state === "inbox" ? "Archive" : "To inbox", hint: "e" })
    if (record.hasTranscriptSource || record.transcript !== "none" || (service && service.engine && service.engine.whisper && service.engine.whisper.available))
      list.push({ key: "transcript", glyph: "󰨖", label: "Transcript", hint: "t" })
    return list
  }

  function load(summary) {
    root.episode = summary || null
    root.detail = null
    root.actionIndex = 0
    if (notes) notes.contentY = 0
    if (summary && service) {
      var wanted = summary.id
      service.requestEpisode(wanted, function(ok, result) {
        if (ok && result && result.episode && root.episode && root.episode.id === wanted) root.detail = result.episode
      })
    }
  }

  function refresh(summary) {
    if (summary && root.episode && summary.id === root.episode.id) {
      root.episode = summary
      if (root.detail) {
        var merged = {}
        for (var key in root.detail) merged[key] = root.detail[key]
        for (var k in summary) merged[k] = summary[k]
        root.detail = merged
      }
    }
  }

  function run(key) {
    if (!service || !record) return
    switch (key) {
      case "play": if (playing) service.pause(); else service.play(record.id); break
      case "queue": if (record.queued) service.queueRemove(record.id); else service.queueAdd(record.id, "last"); break
      case "playNext": service.queueAdd(record.id, "next"); break
      case "download":
        if (record.download === "done") service.deleteDownload(record.id)
        else if (record.download === "downloading" || record.download === "queued") service.cancelDownload(record.id)
        else service.download(record.id)
        break
      case "played": service.markPlayed(record.id, !record.played); break
      case "archive": service.setEpisodeState(record.id, record.state === "inbox" ? "archived" : "inbox"); break
      case "transcript": if (browser) browser.navigate("nowPlaying", { episodeId: record.id, transcript: true }, true); break
    }
  }

  function handleKey(event) {
    if (!record) return false
    switch (event.text) {
      case "p": run("play"); return true
      case "a": run("queue"); return true
      case "A": run("playNext"); return true
      case "d": run("download"); return true
      case "m": run("played"); return true
      case "e": run("archive"); return true
      case "t": run("transcript"); return true
    }
    if (!active) return false
    if (event.key === Qt.Key_Left || event.text === "h") { actionIndex = Math.max(0, actionIndex - 1); return true }
    if (event.key === Qt.Key_Right || event.text === "l") { actionIndex = Math.min(actions.length - 1, actionIndex + 1); return true }
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { if (actions[actionIndex]) run(actions[actionIndex].key); return true }
    if (event.key === Qt.Key_Down || event.text === "j") { notes.scrollBy(Style.space(60)); return true }
    if (event.key === Qt.Key_Up || event.text === "k") { notes.scrollBy(-Style.space(60)); return true }
    if (event.key === Qt.Key_PageDown) { notes.scrollBy(notes.height * 0.9); return true }
    if (event.key === Qt.Key_PageUp) { notes.scrollBy(-notes.height * 0.9); return true }
    return false
  }

  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onEvent(name, data) { if (name === "episode" && data) root.refresh(data) }
  }

  Column {
    anchors.fill: parent
    spacing: Style.space(12)
    visible: !!root.record

    RowLayout {
      width: parent.width
      spacing: Style.space(12)

      Artwork {
        size: Style.space(84)
        source: root.record ? String(root.record.artwork || "") : ""
        foreground: root.foreground
        Layout.alignment: Qt.AlignTop
      }

      Column {
        Layout.fillWidth: true
        spacing: Style.spacing.xxs

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: root.record ? String(root.record.title || "") : ""
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.title
          font.bold: true
          wrapMode: Text.WordWrap
          maximumLineCount: 3
          elide: Text.ElideRight
          renderType: Text.NativeRendering
        }

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: root.record ? String(root.record.podcastTitle || "") : ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          elide: Text.ElideRight
          renderType: Text.NativeRendering
        }

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: root.metaLine
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          renderType: Text.NativeRendering
        }
      }
    }

    Flow {
      id: actionRow
      width: parent.width
      spacing: Style.space(6)

      Repeater {
        model: root.actions

        Button {
          required property var modelData
          required property int index
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          bordered: true
          hasCursor: root.active && root.actionIndex === index
          selected: modelData.key === "queue" && root.record && root.record.queued === true
          iconText: modelData.glyph
          text: modelData.label
          tooltipText: modelData.label + "  (" + modelData.hint + ")"
          onHovered: function(isHovered) { if (isHovered) { root.actionIndex = index; if (root.browser) root.browser.pane = "detail" } }
          onClicked: root.run(modelData.key)
        }
      }
    }

    PanelSeparator { foreground: root.foreground }

    Column {
      visible: root.chapters.length > 0
      width: parent.width
      spacing: Style.space(4)

      PanelSectionHeader { text: "CHAPTERS"; foreground: root.foreground; fontFamily: root.fontFamily }

      Repeater {
        model: root.chapters.slice(0, 12)

        CursorSurface {
          required property var modelData
          required property int index
          width: parent.width
          implicitHeight: chapterText.implicitHeight + Style.spacing.md
          foreground: root.foreground
          current: root.service && root.service.player && root.service.player.chapter === index

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: if (root.service) root.service.seek(Number(modelData.startTime) || 0)
          }

          Text {
            id: chapterText
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.space(8)
            anchors.rightMargin: Style.space(8)
            textFormat: Text.PlainText
            text: Model.formatTime(modelData.startTime) + "   " + String(modelData.title || "Chapter " + (index + 1))
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
            renderType: Text.NativeRendering
          }
        }
      }

      Text {
        visible: root.chapters.length > 12
        textFormat: Text.PlainText
        text: "+" + (root.chapters.length - 12) + " more in Now Playing"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        renderType: Text.NativeRendering
      }

      PanelSeparator { foreground: root.foreground }
    }

    ShowNotes {
      id: notes
      width: parent.width
      height: parent.height - y
      html: root.detail && root.detail.notesHtml !== undefined ? String(root.detail.notesHtml) : (root.record ? "<p>" + String(root.record.notesText || "") + "</p>" : "")
      foreground: root.foreground
      fontFamily: root.fontFamily
    }
  }
}
