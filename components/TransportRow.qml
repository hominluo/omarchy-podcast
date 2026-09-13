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
    service.setSpeed(Model.nextSpeed(player.baseSpeed || player.speed, direction))
  }

  // Off -> default minutes -> 45 -> 60 -> end of episode -> end of chapter -> off.
  function cycleSleep() {
    if (!service || !player) return
    var sleep = player.sleep || { mode: "off" }
    if (sleep.mode === "off") { service.setSleepTimer("minutes", sleepDefault); return }
    if (sleep.mode === "minutes") {
      var left = Math.round(((Number(sleep.endsAt) || 0) * 1000 - Date.now()) / 60000)
      if (left < 45) { service.setSleepTimer("minutes", 45); return }
      if (left < 60) { service.setSleepTimer("minutes", 60); return }
      service.setSleepTimer("episode"); return
    }
    if (sleep.mode === "episode") { service.setSleepTimer((service.chapters || []).length > 0 ? "chapter" : "off"); return }
    service.setSleepTimer("off")
  }

  Row {
    id: row
    spacing: Style.space(6)

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
        iconSize: isPlay ? (root.large ? Style.font.display : Style.font.iconLarge) : (root.large ? Style.font.iconLarge : Style.font.icon)
        fontSize: root.large ? Style.font.bodySmall : Style.font.caption
        horizontalPadding: isPlay ? Style.space(root.large ? 20 : 14) : Style.spacing.controlPaddingX
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
