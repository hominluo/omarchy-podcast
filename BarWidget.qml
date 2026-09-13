import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// The bar cell: a podcast glyph, the episode title scrolling past while
// something plays, and the host for the now-playing dropdown (Panel.qml).
//
// One of these exists per monitor. It owns nothing but its own pixels; state
// and the daemon connection live in Service.qml, reached through
// bar.shell.serviceFor(). Left click opens the dropdown, middle click
// play/pauses, right click opens the library window, and the wheel seeks.
BarWidget {
  id: root
  moduleName: "io.github.hominluo.podcast"

  property var service: null

  function bindService() {
    if (root.service || !root.bar || !root.bar.shell || typeof root.bar.shell.serviceFor !== "function") return
    var found = root.bar.shell.serviceFor(root.moduleName)
    if (found) root.service = found
  }

  function pushSettings() {
    if (root.service && typeof root.service.configure === "function") root.service.configure(root.settings)
  }

  onBarChanged: { bindService(); pushSettings(); injectPanel() }
  onSettingsChanged: { pushSettings(); injectPanel() }
  onServiceChanged: { pushSettings(); injectPanel() }

  // The service may not exist yet when the bar builds its widgets; it does a
  // moment later. Stop asking once it is there.
  Timer {
    interval: 250
    repeat: true
    running: root.service === null
    onTriggered: root.bindService()
  }

  readonly property var player: service ? service.player : null
  readonly property bool hasEpisode: service ? service.hasEpisode : false
  readonly property bool playing: service ? service.playing : false
  readonly property string title: service ? service.title : ""
  readonly property string podcastTitle: service ? service.podcastTitle : ""
  readonly property real progress: player ? Model.progressFraction(player.pos, player.duration) : 0
  readonly property int maxLabelWidth: Style.space(Number(setting("maxLabelWidth", 160)) || 0)
  readonly property bool showTitle: setting("showTitle", true) === true && maxLabelWidth > 0
  readonly property bool barProgress: setting("barProgress", false) === true
  readonly property string wheelAction: String(setting("wheelAction", "seek"))

  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // ---- dropdown contract (Bar.findPanelWidget needs opened/open/close) ----
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  readonly property real openPanelIndicatorWidth: row.implicitWidth
  readonly property real openPanelIndicatorHeight: Style.bar.iconSlot

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
    if ("service" in target) target.service = root.service
  }

  function browse() {
    root.close()
    if (root.service && typeof root.service.browse === "function") root.service.browse("")
    else if (root.bar && root.bar.shell && typeof root.bar.shell.summon === "function") root.bar.shell.summon(root.moduleName, "{}")
  }

  implicitWidth: vertical ? barSize : Math.round(row.implicitWidth + Style.spaceReal(14))
  implicitHeight: vertical ? Style.bar.iconSlot : barSize

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("PlayerPanel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  // The dropdown's own IPC target. `omarchy-shell shell toggle <id>` reaches
  // the browse window instead, because this plugin also declares an overlay.
  IpcHandler {
    target: root.moduleName + ".panel"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    fixedWidth: root.vertical ? -1 : root.implicitWidth
    fixedHeight: root.vertical ? root.implicitHeight : -1
    tooltipText: root.hasEpisode
      ? root.title + (root.podcastTitle ? " — " + root.podcastTitle : "")
        + "\n" + Model.formatTime(root.player.pos) + " / " + Model.formatTime(root.player.duration)
      : (root.service && root.service.connected ? "Podcast" : "Podcast — starting…")

    onPressed: function(b) {
      if (b === Qt.MiddleButton) {
        if (root.service) root.service.togglePause()
      } else if (b === Qt.RightButton) {
        root.browse()
      } else {
        root.toggle()
      }
    }

    property real wheelAcc: 0
    onWheelMoved: function(delta) {
      if (root.wheelAction !== "seek" || !root.service || !root.hasEpisode) return
      var wheel = Util.wheelSteps(button.wheelAcc, delta)
      button.wheelAcc = wheel.remainder
      if (wheel.steps !== 0) root.service.seekRelative(wheel.steps > 0 ? 15 : -15)
    }

    Row {
      id: row
      anchors.centerIn: parent
      spacing: Style.space(6)

      OpticalGlyph {
        width: Style.bar.iconCanvas
        height: Style.bar.iconCanvas
        anchors.verticalCenter: parent.verticalCenter
        text: "󰦔"   // nf-md-podcast
        fontFamily: root.fontFamily
        fontSize: Style.bar.iconFont
        color: root.foreground
        opacity: root.hasEpisode && !root.playing ? 0.6 : 1.0
        Behavior on opacity { NumberAnimation { duration: 160 } }
      }

      Item {
        id: clipBox
        visible: !root.vertical && root.showTitle && root.playing && label.text !== ""
        width: visible ? Math.min(root.maxLabelWidth, label.implicitWidth) : 0
        height: label.implicitHeight
        clip: true
        anchors.verticalCenter: parent.verticalCenter

        Text {
          id: label
          textFormat: Text.PlainText
          text: root.title + (root.podcastTitle ? "  ·  " + root.podcastTitle : "")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          renderType: Text.NativeRendering
          anchors.verticalCenter: parent.verticalCenter

          readonly property bool needsScroll: implicitWidth > clipBox.width
          onNeedsScrollChanged: if (!needsScroll) x = 0

          NumberAnimation on x {
            running: label.needsScroll && clipBox.visible && !root.opened
            loops: Animation.Infinite
            duration: Math.max(6000, label.implicitWidth * 25)
            from: clipBox.width
            to: -label.implicitWidth
            easing.type: Easing.Linear
          }
        }
      }
    }

    // Optional two-pixel progress line along the bar edge.
    Rectangle {
      visible: root.barProgress && root.hasEpisode && !root.opened
      color: Util.alpha(root.foreground, 0.35)
      anchors.left: parent.left
      anchors.bottom: root.vertical ? undefined : parent.bottom
      anchors.top: root.vertical ? parent.top : undefined
      width: root.vertical ? 2 : Math.round(parent.width * root.progress)
      height: root.vertical ? Math.round(parent.height * root.progress) : 2
    }
  }
}
