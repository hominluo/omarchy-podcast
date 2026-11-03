import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// Back · play/pause · forward · speed · sleep. Shared by the dropdown and the
// Now Playing view; the host owns the cursor and tells us which button has it.
Item {
  id: root

  property var service: null
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property bool hasCursor: false
  property int cursorIndex: 0
  property bool large: false

  signal hovered(int index)

  readonly property var player: service ? service.player : null
  readonly property bool playing: service ? service.playing : false
  readonly property int skipBack: service ? Number(service.setting("skipBack", 15)) || 15 : 15
  readonly property int skipForward: service ? Number(service.setting("skipForward", 30)) || 30 : 30
  readonly property int sleepDefault: service ? Number(service.setting("sleepTimerDefault", 30)) || 30 : 30
  readonly property bool sleeping: !!player && !!player.sleep && player.sleep.mode !== "off"
  readonly property var actions: ["back", "playPause", "forward", "speed", "sleep"]
  readonly property int count: actions.length

  // One height for the whole row: five controls of five different heights
  // read as five separate boxes. The play button is only wider, not taller,
  // so it stays the centre of the row without breaking the line.
  readonly property real controlHeight: Style.space(root.large ? 44 : 34)
  readonly property real playWidth: root.controlHeight + Style.space(root.large ? 14 : 10)

  implicitWidth: row.implicitWidth
  implicitHeight: row.implicitHeight

  function run(action) {
    if (!service) return
    switch (action) {
      case "back": service.seekRelative(-skipBack); break
      case "playPause": service.togglePause(); break
      case "forward": service.seekRelative(skipForward); break
      case "speed": cycleSpeed(1); break
      case "sleep": cycleSleep(); break
    }
  }

  function runIndex(index) { run(actions[index]) }

  function cycleSpeed(direction) {
    if (!service || !player) return
    service.setCurrentSpeed(Model.nextSpeed(player.baseSpeed || player.speed, direction))
  }

  // Off -> default minutes -> 45 -> 60 -> end of episode -> end of chapter -> off.
  function cycleSleep() {
    if (!service || !player) return
    var sleep = player.sleep || { mode: "off" }
    var left = 0
    if (sleep.mode === "off") { service.setSleepTimer("minutes", sleepDefault); return }
    if (sleep.mode === "minutes") {
      left = Math.round(((Number(sleep.endsAt) || 0) * 1000 - Date.now()) / 60000)
      if (left < 45) { service.setSleepTimer("minutes", 45); return }
      if (left < 60) { service.setSleepTimer("minutes", 60); return }
      service.setSleepTimer("episode"); return
    }
    if (sleep.mode === "episode") { service.setSleepTimer((service.chapters || []).length > 0 ? "chapter" : "off"); return }
    service.setSleepTimer("off")
  }

  Row {
    id: row
    spacing: Style.space(root.large ? 10 : 8)

    Repeater {
      model: root.actions

      Button {
        required property string modelData
        required property int index
        readonly property bool isPlay: modelData === "playPause"
        readonly property bool isSpeed: modelData === "speed"
        readonly property bool isSleep: modelData === "sleep"

        foreground: root.foreground
        fontFamily: root.fontFamily
        hasCursor: root.hasCursor && root.cursorIndex === index
        selected: (isSleep && root.sleeping) || (isSpeed && root.player && Math.abs((root.player.baseSpeed || 1) - 1) > 0.001)
        bordered: true
        // Square icon buttons, a slightly wider play button, and a width that
        // still grows when a label needs the room (a custom skip of 45 s, a
        // speed of 1.25×).
        height: root.controlHeight
        width: Math.max(isPlay ? root.playWidth : root.controlHeight, implicitWidth)
        horizontalPadding: Style.space(root.large ? 14 : 10)
        verticalPadding: 0
        iconSize: root.large && isPlay ? Style.font.display : Style.font.iconLarge
        fontSize: root.large ? Style.font.body : Style.font.bodySmall
        iconText: {
          if (modelData === "back") return root.skipBack === 15 ? "󱥆" : (root.skipBack === 30 ? "󰶖" : "󰑟")
          if (modelData === "forward") return root.skipForward === 30 ? "󰴆" : (root.skipForward === 15 ? "󱤺" : "󰈑")
          if (isPlay) return root.playing ? "󰏤" : "󰐊"
          if (isSpeed) return ""
          return "󰒲"
        }
        text: {
          if (modelData === "back") return root.skipBack === 15 || root.skipBack === 30 ? "" : String(root.skipBack)
          if (modelData === "forward") return root.skipForward === 30 || root.skipForward === 15 ? "" : String(root.skipForward)
          if (isSpeed) return root.player ? Model.formatSpeed(root.player.baseSpeed || root.player.speed) : "1×"
          if (isSleep && root.sleeping) return Model.sleepLabel(root.player.sleep)
          return ""
        }
        tooltipText: {
          if (modelData === "back") return "Back " + root.skipBack + " s  (h, ←)"
          if (modelData === "forward") return "Forward " + root.skipForward + " s  (l, →)"
          if (isPlay) return (root.playing ? "Pause" : "Play") + "  (Space)"
          if (isSpeed) return "Playback speed: click for faster, right-click for slower  (s / S)"
          return root.sleeping ? "Sleep timer: " + Model.sleepLabel(root.player.sleep) + "  (z, right-click to cancel)" : "Sleep timer  (z)"
        }
        onHovered: function(isHovered) { if (isHovered) root.hovered(index) }
        onClicked: root.run(modelData)
        onRightClicked: {
          if (isSpeed) root.cycleSpeed(-1)
          else if (isSleep && root.service) root.service.setSleepTimer("off")
        }
      }
    }
  }
}
