import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import ".."
import "../../Model.js" as Model

// What is playing, large: artwork, controls, and beside them the transcript,
// the chapters or the show notes.
Item {
  id: root
  required property var browser
  readonly property var service: browser ? browser.service : null

  readonly property var player: service ? service.player : null
  readonly property var nowPlaying: service ? service.nowPlaying : null
  readonly property var episode: nowPlaying ? nowPlaying.episode : null
  readonly property var podcast: nowPlaying ? nowPlaying.podcast : null
  readonly property var chapters: service ? service.chapters : []
  readonly property bool hasEpisode: service ? service.hasEpisode : false
  readonly property int chapterIndex: Model.chapterIndexAt(chapters, clock.position)
  // The transcript pane can show another episode (opened from its detail
  // page) while the player keeps playing what it was playing.
  readonly property int transcriptEpisodeId: browser && browser.viewArgs && browser.viewArgs.episodeId
    ? Number(browser.viewArgs.episodeId) || 0 : (episode ? Number(episode.id) || 0 : 0)

  property string tab: "transcript"     // transcript | chapters | notes
  property var detail: null
  property int transportCursor: 1
  property bool transportActive: false
  property int chapterCursor: 0

  function loadDetail() {
    if (!service || !episode) { detail = null; return }
    var wanted = episode.id
    service.requestEpisode(wanted, function(ok, result) {
      if (ok && result && result.episode && root.episode && root.episode.id === wanted) root.detail = result.episode
    })
  }

  onEpisodeChanged: {
    if (!episode || !detail || detail.id !== episode.id) loadDetail()
  }

  Component.onCompleted: {
    if (browser.viewArgs && browser.viewArgs.transcript) tab = "transcript"
    loadDetail()
  }

  function handleEscape() {
    if (transcript.item && typeof transcript.item.handleEscape === "function" && transcript.item.handleEscape()) return true
    return false
  }

  function handleKey(event) {
    if (!service) return false
    if (tab === "transcript" && transcript.item && typeof transcript.item.handleKey === "function" && transcript.item.handleKey(event)) return true
    switch (event.text) {
      case "s": transport.cycleSpeed(1); return true
      case "S": transport.cycleSpeed(-1); return true
      case "z": transport.cycleSleep(); return true
      case "v": service.toggleVoiceBoost(); return true
      case "x": service.toggleSkipSilence(); return true
      case "c": tab = "chapters"; return true
      case "n": if (tab !== "transcript") { tab = "notes"; return true } break
      case "t": tab = "transcript"; return true
      case "m": if (episode) service.markPlayed(episode.id, true); return true
    }
    if (tab === "chapters" && chapters.length > 0) {
      if (event.key === Qt.Key_Down || event.text === "j") { chapterCursor = Math.min(chapters.length - 1, chapterCursor + 1); return true }
      if (event.key === Qt.Key_Up || event.text === "k") { chapterCursor = Math.max(0, chapterCursor - 1); return true }
      if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { service.seek(Number(chapters[chapterCursor].startTime) || 0); return true }
    }
    if (tab === "notes") {
      if (event.key === Qt.Key_Down || event.text === "j") { notes.scrollBy(Style.space(60)); return true }
      if (event.key === Qt.Key_Up || event.text === "k") { notes.scrollBy(-Style.space(60)); return true }
    }
    if (event.key === Qt.Key_Left || event.text === "h") {
      if (transportActive) { transportCursor = Math.max(0, transportCursor - 1); return true }
      service.seekRelative(-transport.skipBack); return true
    }
    if (event.key === Qt.Key_Right || event.text === "l") {
      if (transportActive) { transportCursor = Math.min(transport.count - 1, transportCursor + 1); return true }
      service.seekRelative(transport.skipForward); return true
    }
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      if (!transportActive) { transportActive = true; return true }
      transport.runIndex(transportCursor); return true
    }
    return false
  }

  PlaybackClock {
    id: clock
    service: root.service
    running: browser.opened && browser.view === "nowPlaying"
  }

  readonly property bool showPlayer: root.hasEpisode || root.transcriptEpisodeId > 0

  Text {
    anchors.centerIn: parent
    visible: !root.showPlayer
    textFormat: Text.PlainText
    text: "Nothing playing. Pick an episode from the library or the inbox."
    color: browser.dim
    font.family: browser.fontFamily
    font.pixelSize: Style.font.body
    renderType: Text.NativeRendering
  }

  Row {
    anchors.fill: parent
    visible: root.showPlayer
    spacing: Style.space(24)

    // ---------- Left: the player ----------
    Column {
      id: playerColumn
      width: Math.round(parent.width * 0.42)
      height: parent.height
      spacing: Style.space(14)

      Artwork {
        size: Math.min(playerColumn.width, Style.space(260))
        source: service ? service.artwork : ""
        foreground: browser.foreground
        glyphSize: Style.space(64)
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: service ? service.title : ""
        color: browser.foreground
        font.family: browser.fontFamily
        font.pixelSize: Style.font.heading
        font.bold: true
        wrapMode: Text.WordWrap
        maximumLineCount: 3
        elide: Text.ElideRight
        renderType: Text.NativeRendering
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: {
          var parts = []
          if (service && service.podcastTitle) parts.push(service.podcastTitle)
          if (root.episode) {
            var date = Model.formatRelativeDate(root.episode.pubDate)
            if (date) parts.push(date)
          }
          return parts.join("  ·  ")
        }
        color: browser.dim
        font.family: browser.fontFamily
        font.pixelSize: Style.font.bodySmall
        elide: Text.ElideRight
        renderType: Text.NativeRendering

        MouseArea {
          anchors.fill: parent
          cursorShape: root.podcast ? Qt.PointingHandCursor : Qt.ArrowCursor
          onClicked: if (root.podcast) browser.navigate("podcast", { podcastId: root.podcast.id }, true)
        }
      }

      Text {
        width: parent.width
        visible: root.chapterIndex >= 0 && root.chapters[root.chapterIndex] && String(root.chapters[root.chapterIndex].title || "") !== ""
        textFormat: Text.PlainText
        text: visible ? ("CHAPTER " + (root.chapterIndex + 1) + "  ·  " + String(root.chapters[root.chapterIndex].title)).toUpperCase() : ""
        color: browser.faint
        font.family: browser.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        elide: Text.ElideRight
        renderType: Text.NativeRendering
      }

      SeekBar {
        width: parent.width
        foreground: browser.foreground
        background: browser.background
        fontFamily: browser.fontFamily
        position: clock.position
        duration: root.player ? Number(root.player.duration) || 0 : 0
        chapters: root.chapters
        step: transport.skipBack
        onSeekRequested: function(seconds) { if (service) service.seek(seconds) }
      }

      TransportRow {
        id: transport
        anchors.horizontalCenter: parent.horizontalCenter
        service: root.service
        foreground: browser.foreground
        fontFamily: browser.fontFamily
        large: true
        hasCursor: root.transportActive && browser.pane === "main"
        cursorIndex: root.transportCursor
        onHovered: function(index) { root.transportActive = true; root.transportCursor = index }
      }

      Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(6)

        Button {
          foreground: browser.foreground
          fontFamily: browser.fontFamily
          fontSize: Style.font.bodySmall
          bordered: true
          selected: root.player && root.player.skipSilence === true
          iconText: "󰎊"
          text: "Skip silence"
          tooltipText: "Speed through pauses without changing the words  (x)"
          onClicked: if (service) service.toggleSkipSilence()
        }
        Button {
          foreground: browser.foreground
          fontFamily: browser.fontFamily
          fontSize: Style.font.bodySmall
          bordered: true
          selected: root.player && root.player.voiceBoost === true
          iconText: "󰗋"
          text: "Voice boost"
          tooltipText: "Even out quiet and loud voices  (v)"
          onClicked: if (service) service.toggleVoiceBoost()
        }
      }

      Text {
        width: parent.width
        visible: root.player && root.player.buffering === true
        textFormat: Text.PlainText
        text: "Buffering…"
        color: browser.dim
        font.family: browser.fontFamily
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignHCenter
        renderType: Text.NativeRendering
      }
    }

    // ---------- Right: transcript / chapters / notes ----------
    Column {
      width: parent.width - playerColumn.width - parent.spacing
      height: parent.height
      spacing: Style.space(10)

      ButtonGroup {
        foreground: browser.foreground
        background: browser.background
        fontFamily: browser.fontFamily
        fontSize: Style.font.bodySmall
        focusable: false
        options: [
          { value: "transcript", label: "Transcript", icon: "󰨖" },
          { value: "chapters", label: "Chapters" + (root.chapters.length ? " (" + root.chapters.length + ")" : ""), icon: "󰉹" },
          { value: "notes", label: "Notes", icon: "󰦪" },
        ]
        value: root.tab
        onChanged: function(value) { root.tab = value }
      }

      Item {
        width: parent.width
        height: parent.height - y

        Loader {
          id: transcript
          anchors.fill: parent
          active: root.tab === "transcript"
          sourceComponent: TranscriptView {
            browser: root.browser
            episodeId: root.transcriptEpisodeId
          }
        }

        ListView {
          id: chapterList
          anchors.fill: parent
          visible: root.tab === "chapters"
          clip: true
          model: root.chapters
          spacing: Style.spacing.xxs
          boundsBehavior: Flickable.StopAtBounds
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

          delegate: CursorSurface {
            required property var modelData
            required property int index
            width: chapterList.width - Style.space(10)
            implicitHeight: chapterRow.implicitHeight + Style.spacing.rowPaddingX
            foreground: browser.foreground
            current: index === root.chapterIndex
            hasCursor: root.tab === "chapters" && browser.pane === "main" && root.chapterCursor === index

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onEntered: root.chapterCursor = index
              onClicked: if (service) service.seek(Number(modelData.startTime) || 0)
            }

            RowLayout {
              id: chapterRow
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(10)
              anchors.rightMargin: Style.space(10)
              spacing: Style.space(12)

              Text {
                textFormat: Text.PlainText
                text: Model.formatTime(modelData.startTime)
                color: browser.dim
                font.family: browser.fontFamily
                font.pixelSize: Style.font.bodySmall
                renderType: Text.NativeRendering
                Layout.preferredWidth: Style.space(56)
              }
              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: String(modelData.title || ("Chapter " + (index + 1)))
                color: browser.foreground
                font.family: browser.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
                renderType: Text.NativeRendering
              }
              Text {
                textFormat: Text.PlainText
                text: modelData.endTime ? Model.formatDuration(modelData.endTime - modelData.startTime) : ""
                color: browser.dim
                font.family: browser.fontFamily
                font.pixelSize: Style.font.caption
                renderType: Text.NativeRendering
              }
            }
          }

          Text {
            anchors.centerIn: parent
            visible: root.chapters.length === 0
            textFormat: Text.PlainText
            text: "This episode has no chapters."
            color: browser.dim
            font.family: browser.fontFamily
            font.pixelSize: Style.font.body
            renderType: Text.NativeRendering
          }
        }

        ShowNotes {
          id: notes
          anchors.fill: parent
          visible: root.tab === "notes"
          html: root.detail && root.detail.notesHtml !== undefined ? String(root.detail.notesHtml) : ""
          foreground: browser.foreground
          fontFamily: browser.fontFamily
        }
      }
    }
  }
}
