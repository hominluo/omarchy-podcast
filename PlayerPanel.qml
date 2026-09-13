import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "components"
import "Model.js" as Model

// The now-playing dropdown under the bar icon (a qs.Ui.Panel).
//
// Owned by BarWidget.qml, which injects `bar`, `settings`, `anchorItem`,
// `hostWidget` and `service`. Everything visible here reads from the service;
// every action is a call on it. One keyboard cursor is shared with the mouse
// (the CursorSurface contract): the root owns cursorActive / focusSection /
// selectedIndex, rows and buttons bind `hasCursor` to it, and hovering moves
// it rather than painting its own highlight.
Panel {
  id: root
  moduleName: "io.github.hominluo.podcast"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var service: null

  readonly property var barIdentity: hostWidget || root
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property color faint: Qt.darker(foreground, 1.9)
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property var player: service ? service.player : null
  readonly property bool connected: service ? service.connected : false
  readonly property bool hasEpisode: service ? service.hasEpisode : false
  readonly property bool playing: service ? service.playing : false
  readonly property string title: service ? service.title : ""
  readonly property string podcastTitle: service ? service.podcastTitle : ""
  readonly property string artwork: service ? service.artwork : ""
  readonly property var chapters: service ? service.chapters : []
  readonly property int chapterIndex: Model.chapterIndexAt(chapters, clock.position)
  readonly property string chapterTitle: chapterIndex >= 0 && chapters[chapterIndex] ? String(chapters[chapterIndex].title || "") : ""
  readonly property int skipBack: Number(setting("skipBack", 15)) || 15
  readonly property int skipForward: Number(setting("skipForward", 30)) || 30
  readonly property int sleepDefault: Number(setting("sleepTimerDefault", 30)) || 30
  readonly property bool sleeping: !!player && player.sleep && player.sleep.mode !== "off"

  // Up Next rows shown here; the full list lives in the browse window.
  readonly property int queueLimit: 6
  readonly property var queueRows: opened && service ? (service.queue || []).slice(0, queueLimit) : []
  readonly property int queueTotal: service ? (service.queue || []).length : 0

  property string notice: ""
  property string noticeLevel: "info"

  implicitWidth: 0
  implicitHeight: 0

  // ---- cursor model ------------------------------------------------------
  property bool cursorActive: false
  property string focusSection: "seek"
  property int selectedIndex: 0

  readonly property var transportActions: transport.actions
  readonly property var toggleActions: ["skipSilence", "voiceBoost"]
  readonly property var footerActions: ["browse", "refresh"]

  readonly property var visibleSections: {
    var list = []
    if (hasEpisode) list.push("hero", "seek", "transport", "toggles")
    else list.push("hero")
    if (queueRows.length > 0) list.push("queue")
    list.push("footer")
    return list
  }

  function sectionCount(section) {
    if (section === "transport") return transportActions.length
    if (section === "toggles") return toggleActions.length
    if (section === "footer") return footerActions.length
    if (section === "queue") return queueRows.length
    return 1
  }

  function sectionIsRow(section) {
    return section === "transport" || section === "toggles" || section === "footer"
  }

  function setCursor(section, index) {
    cursorActive = true
    focusSection = section
    selectedIndex = index
  }

  function moveCursor(dy) {
    var sections = visibleSections
    var sIdx = sections.indexOf(focusSection)
    if (sIdx < 0) { focusSection = sections[0]; selectedIndex = 0; return }
    if (focusSection === "queue") {
      var next = selectedIndex + dy
      if (next >= 0 && next < queueRows.length) { selectedIndex = next; return }
    }
    var target = sIdx + dy
    if (target < 0 || target >= sections.length) return
    focusSection = sections[target]
    selectedIndex = focusSection === "queue" && dy < 0 ? queueRows.length - 1 : 0
    if (sectionIsRow(focusSection)) selectedIndex = Math.min(selectedIndex, sectionCount(focusSection) - 1)
  }

  function moveCursorH(dx) {
    if (sectionIsRow(focusSection)) {
      selectedIndex = Math.max(0, Math.min(sectionCount(focusSection) - 1, selectedIndex + dx))
      return
    }
    if (!service || !hasEpisode) return
    service.seekRelative(dx < 0 ? -skipBack : skipForward)
  }

  function activateCursor() {
    if (!service) return
    switch (focusSection) {
      case "hero": if (hasEpisode) root.browse("nowPlaying"); else root.browse("library"); return
      case "seek": service.togglePause(); return
      case "transport": root.runTransport(transportActions[selectedIndex]); return
      case "toggles": root.runToggle(toggleActions[selectedIndex]); return
      case "queue":
        var row = queueRows[selectedIndex]
        if (row) service.play(row.id)
        return
      case "footer": root.runFooter(footerActions[selectedIndex]); return
    }
  }

  function runTransport(action) { transport.run(action) }

  function runToggle(action) {
    if (!service) return
    if (action === "skipSilence") service.toggleSkipSilence()
    else if (action === "voiceBoost") service.toggleVoiceBoost()
  }

  function runFooter(action) {
    if (action === "browse") root.browse("library")
    else if (action === "refresh") root.refresh()
  }

  function cycleSpeed(direction) { transport.cycleSpeed(direction) }
  function cycleSleep() { transport.cycleSleep() }

  // Tab/Shift+Tab walk to the neighbouring bar panel, the host way.
  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function") {
      if (root.bar.switchPanelFrom(root.barIdentity, direction)) return
    }
    root.close()
  }

  function browse(view) {
    root.close()
    if (service) service.browse(view, hasEpisode && view === "nowPlaying" ? { episodeId: player.episodeId } : null)
  }

  function refresh() {
    if (!service) return
    service.refresh()
    root.showNotice("Refreshing feeds…", "info")
  }

  function showNotice(text, level) {
    root.notice = String(text || "")
    root.noticeLevel = level || "info"
    noticeTimer.restart()
  }

  function ensureCursorVisible(item) {
    if (!item || !flick) return
    var maxY = Math.max(0, flick.contentHeight - flick.height)
    if (maxY <= 0) { flick.contentY = 0; return }
    var margin = Style.space(6)
    var point = item.mapToItem(flick.contentItem, 0, 0)
    var top = point.y
    var bottom = top + item.height
    if (top < flick.contentY + margin) flick.contentY = Math.max(0, top - margin)
    else if (bottom > flick.contentY + flick.height - margin) flick.contentY = Math.min(maxY, bottom + margin - flick.height)
  }

  onOpenedChanged: {
    if (opened) {
      cursorActive = false
      focusSection = hasEpisode ? "seek" : "hero"
      selectedIndex = 0
      Qt.callLater(function() { if (flick) flick.contentY = 0 })
    }
  }

  onVisibleSectionsChanged: {
    if (visibleSections.indexOf(focusSection) < 0) { focusSection = visibleSections[0]; selectedIndex = 0 }
    if (selectedIndex >= sectionCount(focusSection)) selectedIndex = Math.max(0, sectionCount(focusSection) - 1)
  }

  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onNotice(payload) {
      if (!payload || !root.opened) return
      root.showNotice(payload.text, payload.level)
    }
  }

  Timer {
    id: noticeTimer
    interval: 4000
    onTriggered: root.notice = ""
  }

  PlaybackClock {
    id: clock
    service: root.service
    running: root.opened
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      property bool enterPending: false
      onReturnRequested: enterPending = true
      onActivateRequested: {
        if (enterPending) { enterPending = false; if (root.cursorActive) root.activateCursor(); else if (root.service) root.service.togglePause() }
        else if (root.service) root.service.togglePause()
      }
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.moveCursor(dy)
        else root.moveCursorH(dx)
      }
      onDeleteRequested: {
        if (root.focusSection === "queue" && root.service) {
          var row = root.queueRows[root.selectedIndex]
          if (row) root.service.queueRemove(row.id)
        }
      }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (!root.service) return
        switch (t) {
          case "s": root.cycleSpeed(1); break
          case "S": root.cycleSpeed(-1); break
          case "z": case "Z": root.cycleSleep(); break
          case "b": case "B": root.browse("library"); break
          case "t": case "T": if (root.hasEpisode) root.browse("nowPlaying"); break
          case "r": case "R": root.refresh(); break
          case "n": case "N": root.service.next(); break
          case "m": case "M": if (root.hasEpisode) root.service.markPlayed(root.player.episodeId, true); break
          case "v": case "V": root.service.toggleVoiceBoost(); break
          case "q": case "Q": root.browse("upNext"); break
          case "i": case "I": root.browse("inbox"); break
          case "/": root.browse("discover"); break
          case ",": root.service.seekRelative(-root.skipBack); break
          case ".": root.service.seekRelative(root.skipForward); break
        }
      }

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: flick.width
          spacing: Style.space(12)

          // ---------- Hero: artwork · title · podcast · chapter ----------
          CursorSurface {
            id: heroRow
            width: parent.width
            implicitHeight: heroContent.implicitHeight + Style.spacing.rowPaddingX
            hasCursor: root.cursorActive && root.focusSection === "hero"
            foreground: root.foreground
            onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(heroRow)

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onEntered: root.setCursor("hero", 0)
              onClicked: root.activateCursor()
            }

            RowLayout {
              id: heroContent
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(8)
              anchors.rightMargin: Style.space(8)
              spacing: Style.space(12)

              Artwork {
                size: Style.space(64)
                source: root.artwork
                foreground: root.foreground
                Layout.alignment: Qt.AlignVCenter
              }

              Column {
                Layout.fillWidth: true
                spacing: Style.spacing.xxs

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  text: root.hasEpisode ? root.title : (root.connected ? "Nothing playing" : (root.service ? "Starting the podcast engine…" : "Podcast"))
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.subtitle
                  font.bold: root.hasEpisode
                  wrapMode: Text.WordWrap
                  maximumLineCount: 2
                  elide: Text.ElideRight
                  renderType: Text.NativeRendering
                }

                Text {
                  width: parent.width
                  visible: text !== ""
                  textFormat: Text.PlainText
                  text: root.hasEpisode ? root.podcastTitle : (root.connected ? "Pick an episode from the library, or press b." : "")
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                  maximumLineCount: 1
                  renderType: Text.NativeRendering
                }

                Text {
                  width: parent.width
                  visible: root.hasEpisode && root.chapterTitle !== ""
                  textFormat: Text.PlainText
                  text: root.chapterIndex >= 0 ? ("CHAPTER " + (root.chapterIndex + 1) + "  ·  " + root.chapterTitle).toUpperCase() : ""
                  color: root.faint
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  elide: Text.ElideRight
                  maximumLineCount: 1
                  renderType: Text.NativeRendering
                }
              }
            }
          }

          // ---------- Seek ----------
          CursorSurface {
            id: seekRow
            visible: root.hasEpisode
            width: parent.width
            implicitHeight: seekBar.implicitHeight + Style.spacing.md * 2
            hasCursor: root.cursorActive && root.focusSection === "seek"
            outline: true
            foreground: root.foreground
            onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(seekRow)

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              acceptedButtons: Qt.NoButton
              onEntered: root.setCursor("seek", 0)
            }

            SeekBar {
              id: seekBar
              bar: root.bar
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(10)
              anchors.rightMargin: Style.space(10)
              position: clock.position
              duration: root.player ? Number(root.player.duration) || 0 : 0
              chapters: root.chapters
              step: root.skipBack
              onSeekRequested: function(seconds) { if (root.service) root.service.seek(seconds) }
            }
          }

          // ---------- Transport ----------
          Item {
            visible: root.hasEpisode
            width: parent.width
            implicitHeight: transport.implicitHeight

            TransportRow {
              id: transport
              anchors.horizontalCenter: parent.horizontalCenter
              service: root.service
              foreground: root.foreground
              fontFamily: root.fontFamily
              hasCursor: root.cursorActive && root.focusSection === "transport"
              cursorIndex: root.selectedIndex
              onHovered: function(index) { root.setCursor("transport", index) }
            }
          }

          // ---------- Toggles ----------
          Item {
            visible: root.hasEpisode
            width: parent.width
            implicitHeight: toggleRow.implicitHeight

            Row {
              id: toggleRow
              anchors.horizontalCenter: parent.horizontalCenter
              spacing: Style.space(6)

              Repeater {
                model: root.toggleActions

                Button {
                  required property string modelData
                  required property int index
                  readonly property bool on: root.player ? (modelData === "skipSilence" ? root.player.skipSilence === true : root.player.voiceBoost === true) : false
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  hasCursor: root.cursorActive && root.focusSection === "toggles" && root.selectedIndex === index
                  selected: on
                  bordered: true
                  iconText: modelData === "skipSilence" ? "󰎊" : "󰗋"
                  text: modelData === "skipSilence" ? "Skip silence" : "Voice boost"
                  tooltipText: modelData === "skipSilence"
                    ? "Speed through pauses without changing the words"
                    : "Even out quiet and loud voices  (v)"
                  onHovered: function(isHovered) { if (isHovered) root.setCursor("toggles", index) }
                  onClicked: root.runToggle(modelData)
                }
              }
            }
          }

          // ---------- Up Next ----------
          Column {
            visible: root.queueRows.length > 0
            width: parent.width
            spacing: Style.space(6)

            PanelSeparator { foreground: root.foreground }

            Item {
              width: parent.width
              implicitHeight: upNextHeader.implicitHeight

              PanelSectionHeader {
                id: upNextHeader
                text: "UP NEXT"
                foreground: root.foreground
                fontFamily: root.fontFamily
              }

              Text {
                anchors.right: parent.right
                anchors.baseline: upNextHeader.baseline
                textFormat: Text.PlainText
                text: root.queueTotal + (root.queueTotal === 1 ? " episode" : " episodes")
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                renderType: Text.NativeRendering
              }
            }

            Repeater {
              model: root.queueRows

              EpisodeRow {
                id: queueRow
                required property var modelData
                required property int index
                width: parent.width
                episode: modelData
                foreground: root.foreground
                fontFamily: root.fontFamily
                artworkSize: Style.space(28)
                compact: true
                hasCursor: root.cursorActive && root.focusSection === "queue" && root.selectedIndex === index
                onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(queueRow)
                onHovered: root.setCursor("queue", index)
                onClicked: function(mouse) {
                  if (!root.service) return
                  if (mouse.button === Qt.MiddleButton) root.service.queueRemove(modelData.id)
                  else root.service.play(modelData.id)
                }
              }
            }

            Button {
              visible: root.queueTotal > root.queueRows.length
              anchors.horizontalCenter: parent.horizontalCenter
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              text: "+" + (root.queueTotal - root.queueRows.length) + " more in Up Next"
              onClicked: root.browse("upNext")
            }
          }

          // ---------- Footer ----------
          Column {
            width: parent.width
            spacing: Style.space(8)

            PanelSeparator { foreground: root.foreground }

            RowLayout {
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: root.footerActions

                Button {
                  required property string modelData
                  required property int index
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  hasCursor: root.cursorActive && root.focusSection === "footer" && root.selectedIndex === index
                  bordered: true
                  iconText: modelData === "browse" ? "󰌱" : "󰑐"
                  iconSpinning: modelData === "refresh" && root.service && root.service.jobs && root.service.jobs.refreshing === true
                  text: modelData === "browse" ? "Browse library" : "Refresh"
                  tooltipText: modelData === "browse" ? "Open the podcast library  (b)" : "Check every feed for new episodes  (r)"
                  Layout.fillWidth: true
                  Layout.preferredWidth: 1
                  Layout.minimumWidth: 0
                  onHovered: function(isHovered) { if (isHovered) root.setCursor("footer", index) }
                  onClicked: root.runFooter(modelData)
                }
              }
            }

            Text {
              width: parent.width
              visible: text !== ""
              textFormat: Text.PlainText
              text: {
                if (root.notice !== "") return Model.elide(root.notice, 120)
                if (root.service && !root.connected) return "Connecting to the podcast engine…"
                if (root.service && root.service.engine && root.service.engine.mpv === "unavailable") return "mpv is not available; playback is paused."
                return ""
              }
              color: root.noticeLevel === "error" || root.noticeLevel === "warn" ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
              maximumLineCount: 2
              elide: Text.ElideRight
              renderType: Text.NativeRendering
            }
          }
        }
      }
    }
  }
}
