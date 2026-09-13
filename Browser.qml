import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "components"
import "Model.js" as Model

// The library window: a launcher-style modal over the desktop.
//
// Declared as the plugin's `overlay`, so the host mounts it once (keepLoaded)
// and calls open(payloadJson) / close(). It is the same surface every other
// summoned Omarchy window uses — full-screen layer-shell, dimmed scrim,
// centred card, exclusive keyboard focus — painted with the [menu] theme
// tokens so a theme that styles the launcher styles this too.
//
// Layout: a sidebar of views on the left, one view in the main pane, and an
// optional detail pane the view can open on the right. Navigation is a stack
// (`history`) so Escape and Backspace walk back before closing.
Item {
  id: root

  // Injected by the host.
  property string omarchyPath: ""
  property var shell: null
  property var manifest: null
  property var service: null

  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : "io.github.hominluo.podcast"

  property bool opened: false
  property string view: "library"
  property var viewArgs: ({})
  property var history: []
  property string pane: "main"          // "sidebar" | "main" | "detail"
  property bool editing: false          // a text field has focus; keys go to it
  property bool helpOpen: false
  property bool detailOpen: false

  // ---- theme ---------------------------------------------------------------
  readonly property color background: Color.menu.background
  readonly property color foreground: Color.menu.text
  readonly property color border: Color.menu.border
  readonly property color scrim: Color.menu.scrim
  readonly property color selectedBackground: Color.menu.selectedBackground
  readonly property color selectedText: Color.menu.selectedText
  readonly property color accent: Color.accent
  readonly property color urgent: Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property color faint: Qt.darker(foreground, 1.9)
  readonly property var borderSpec: Border.surfaceSpec("menu", "border", border, Math.max(1, Style.space(2)))
  readonly property int cornerRadius: Style.cornerRadius
  readonly property string fontFamily: Style.font.menuFamily
  readonly property int contentMargin: Style.spacing.panelPadding
  readonly property int cardWidth: Math.min(Style.space(1180), Math.round(panel.width * 0.86))
  readonly property int cardHeight: Math.min(Style.space(760), Math.round(panel.height * 0.84))
  readonly property int sidebarWidth: Style.space(190)
  readonly property int detailWidth: Math.round((cardWidth - sidebarWidth) * 0.4)

  // ---- service shortcuts ---------------------------------------------------
  readonly property bool connected: service ? service.connected : false
  readonly property bool hasEpisode: service ? service.hasEpisode : false
  readonly property var player: service ? service.player : null

  // ---- sidebar -------------------------------------------------------------
  readonly property var sidebarItems: {
    var items = []
    if (hasEpisode) items.push({ view: "nowPlaying", label: "Now Playing", glyph: "󰐊", count: 0 })
    items.push({ view: "library", label: "Library", glyph: "󰌱", count: service ? (service.library || []).length : 0 })
    items.push({ view: "upNext", label: "Up Next", glyph: "󰐑", count: service ? (service.queue || []).length : 0 })
    items.push({ view: "inbox", label: "Inbox", glyph: "󰚇", count: service && service.inbox ? Number(service.inbox.count) || 0 : 0 })
    items.push({ view: "downloads", label: "Downloads", glyph: "󰇚", count: service ? (service.downloads || []).length : 0 })
    items.push({ view: "discover", label: "Discover", glyph: "󰍉", count: 0 })
    items.push({ view: "settings", label: "Settings", glyph: "󰒓", count: 0 })
    return items
  }
  readonly property int sidebarIndex: {
    var key = view === "podcast" || view === "episode" ? "library" : view
    for (var i = 0; i < sidebarItems.length; i++) if (sidebarItems[i].view === key) return i
    return -1
  }
  property int sidebarCursor: 0

  // ---- host contract -------------------------------------------------------
  function open(payloadJson) {
    var payload = {}
    try { payload = JSON.parse(String(payloadJson || "{}")) || {} } catch (e) { payload = {} }
    var target = payload.view ? String(payload.view) : ""
    if (target === "transcript") target = "nowPlaying"
    root.helpOpen = false
    root.editing = false
    root.opened = true
    if (target !== "" && (target !== view || payload.episodeId || payload.podcastId)) {
      root.navigate(target, payload, root.history.length > 0)
    } else if (target === "") {
      // A plain reopen keeps the view where it was; only views that cannot
      // stand without their arguments fall back to the library.
      root.history = []
      if (view === "nowPlaying" && !hasEpisode) root.view = "library"
      else if (view === "podcast" && !(root.viewArgs && root.viewArgs.podcastId)) root.view = "library"
      // Episode-scoped arguments do not outlive the window: Now Playing
      // follows the player again on reopen.
      if (root.view !== "podcast") root.viewArgs = {}
    }
    root.refocus()
  }

  // A view that stops editing hands the keyboard back to the catcher, unless
  // Tab already moved the focus to another control in the form.
  onEditingChanged: {
    if (editing || !opened) return
    Qt.callLater(function() {
      var current = keyCatcher.Window.activeFocusItem
      if (root.opened && !root.editing && !(current && current !== keyCatcher && current.activeFocusOnTab)) keyCatcher.forceActiveFocus()
    })
  }

  function close() {
    root.opened = false
    root.editing = false
    root.helpOpen = false
  }

  function dismiss() {
    root.close()
    if (root.shell && typeof root.shell.hide === "function") root.shell.hide(root.pluginId)
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  function ping() { return "ok" }

  // ---- navigation ----------------------------------------------------------
  function navigate(next, args, push) {
    if (push !== false && root.opened) {
      var stack = root.history.slice()
      stack.push({ view: root.view, args: root.viewArgs })
      root.history = stack.slice(-20)
    }
    root.editing = false
    root.view = String(next)
    root.viewArgs = args || {}
    root.detailOpen = false
    root.pane = "main"
    root.refocus()
  }

  function back() {
    if (root.history.length === 0) return false
    var stack = root.history.slice()
    var previous = stack.pop()
    root.history = stack
    root.view = previous.view
    root.viewArgs = previous.args || {}
    root.detailOpen = false
    root.pane = "main"
    root.refocus()
    return true
  }

  function showSidebarItem(index) {
    var item = root.sidebarItems[index]
    if (item) root.navigate(item.view, {}, true)
  }

  // Views that want a text field focused set `editing` first; the catcher
  // only takes the keyboard back when nobody is typing.
  function refocus() {
    Qt.callLater(function() { if (root.opened && !root.editing) keyCatcher.forceActiveFocus() })
  }

  function handleEscape() {
    if (root.helpOpen) { root.helpOpen = false; return }
    if (root.editing) { root.refocus(); root.editing = false; return }
    var current = viewLoader.item
    if (current && typeof current.handleEscape === "function" && current.handleEscape()) return
    if (root.detailOpen) { root.detailOpen = false; root.pane = "main"; return }
    if (root.back()) return
    root.dismiss()
  }

  function handleKey(event) {
    if (root.editing) {
      if (event.key === Qt.Key_Escape) { root.handleEscape(); event.accepted = true }
      return
    }
    if (root.helpOpen) {
      if (event.key === Qt.Key_Escape || event.text === "?" || event.key === Qt.Key_Return) root.helpOpen = false
      event.accepted = true
      return
    }
    if (event.key === Qt.Key_Escape) { root.handleEscape(); event.accepted = true; return }

    // The active view sees keys first, then the sidebar and global chords.
    var current = viewLoader.item
    if (root.pane !== "sidebar" && current && typeof current.handleKey === "function" && current.handleKey(event)) {
      event.accepted = true
      return
    }

    if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
      root.cyclePane((event.modifiers & Qt.ShiftModifier) || event.key === Qt.Key_Backtab ? -1 : 1)
      event.accepted = true
      return
    }
    if (event.text === "?") { root.helpOpen = true; event.accepted = true; return }
    if (event.key === Qt.Key_Backspace && !(event.modifiers & Qt.ControlModifier)) { root.back(); event.accepted = true; return }
    if (event.key === Qt.Key_Space && root.service) { root.service.togglePause(); event.accepted = true; return }

    var digit = event.text && event.text.length === 1 ? parseInt(event.text, 10) : NaN
    if (!isNaN(digit) && digit >= 1 && digit <= root.sidebarItems.length) {
      root.showSidebarItem(digit - 1)
      event.accepted = true
      return
    }

    if (root.pane === "sidebar") {
      if (event.key === Qt.Key_Down || event.text === "j") { root.sidebarCursor = Math.min(root.sidebarItems.length - 1, root.sidebarCursor + 1); event.accepted = true; return }
      if (event.key === Qt.Key_Up || event.text === "k") { root.sidebarCursor = Math.max(0, root.sidebarCursor - 1); event.accepted = true; return }
      if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.text === "l" || event.key === Qt.Key_Right) {
        root.showSidebarItem(root.sidebarCursor); event.accepted = true; return
      }
    }

    // Global playback chords (available from any view).
    if (root.service) {
      if (event.text === "[") { root.service.setCurrentSpeed(Model.nextSpeed(root.player.baseSpeed || 1, -1)); event.accepted = true; return }
      if (event.text === "]") { root.service.setCurrentSpeed(Model.nextSpeed(root.player.baseSpeed || 1, 1)); event.accepted = true; return }
      if (event.text === ",") { root.service.seekRelative(-(Number(root.service.setting("skipBack", 15)) || 15)); event.accepted = true; return }
      if (event.text === ".") { root.service.seekRelative(Number(root.service.setting("skipForward", 30)) || 30); event.accepted = true; return }
    }
  }

  function cyclePane(direction) {
    var panes = ["sidebar", "main"]
    if (root.detailOpen) panes.push("detail")
    var index = panes.indexOf(root.pane)
    if (index < 0) index = 1
    root.pane = panes[(index + direction + panes.length) % panes.length]
    if (root.pane === "sidebar" && root.sidebarIndex >= 0) root.sidebarCursor = root.sidebarIndex
  }

  function viewSource(name) {
    switch (name) {
      case "library": return "components/views/LibraryView.qml"
      case "podcast": return "components/views/PodcastView.qml"
      case "nowPlaying": return "components/views/NowPlayingView.qml"
      case "upNext": return "components/views/UpNextView.qml"
      case "inbox": return "components/views/InboxView.qml"
      case "downloads": return "components/views/DownloadsView.qml"
      case "discover": return "components/views/DiscoverView.qml"
      case "settings": return "components/views/SettingsView.qml"
      default: return "components/views/LibraryView.qml"
    }
  }

  onViewChanged: viewLoader.load()
  onOpenedChanged: if (opened && !viewLoader.item) viewLoader.load()

  // ---- window --------------------------------------------------------------
  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-podcast"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: root.opened ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: root.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    BorderSurface {
      id: card
      width: root.cardWidth
      height: root.cardHeight
      radius: root.cornerRadius
      anchors.centerIn: parent
      color: root.background
      borderSpec: root.borderSpec
      padding: 0

      MouseArea { anchors.fill: parent; onClicked: {} }

      Item {
        id: keyCatcher
        anchors.fill: parent
        focus: true
        Keys.priority: Keys.BeforeItem

        // Embedded fields pull focus; reclaim it unless one is meant to have it.
        onActiveFocusChanged: {
          if (!activeFocus && root.opened && !root.editing) Qt.callLater(function() { if (root.opened && !root.editing) keyCatcher.forceActiveFocus() })
        }
        Keys.onPressed: function(event) { root.handleKey(event) }

        Row {
          anchors.fill: parent
          anchors.topMargin: card.contentTopInset
          anchors.leftMargin: card.contentLeftInset
          anchors.rightMargin: card.contentRightInset
          anchors.bottomMargin: card.contentBottomInset
          spacing: 0

          // ---------- Sidebar ----------
          Item {
            id: sidebar
            width: root.sidebarWidth
            height: parent.height

            Column {
              anchors.fill: parent
              anchors.margins: Style.spacing.md
              spacing: Style.spacing.xxs

              Item {
                width: parent.width
                height: Style.space(44)

                Row {
                  anchors.left: parent.left
                  anchors.leftMargin: Style.space(10)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(8)

                  Text {
                    textFormat: Text.PlainText
                    text: "󰦔"
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.heading
                    renderType: Text.NativeRendering
                    anchors.verticalCenter: parent.verticalCenter
                  }
                  Text {
                    textFormat: Text.PlainText
                    text: "Podcast"
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.title
                    font.bold: true
                    renderType: Text.NativeRendering
                    anchors.verticalCenter: parent.verticalCenter
                  }
                }
              }

              Repeater {
                model: root.sidebarItems

                SidebarItem {
                  required property var modelData
                  required property int index
                  width: parent.width
                  glyph: modelData.glyph
                  label: modelData.label
                  count: modelData.count
                  shortcut: String(index + 1)
                  foreground: root.foreground
                  accent: root.accent
                  fontFamily: root.fontFamily
                  current: root.sidebarIndex === index
                  hasCursor: root.pane === "sidebar" && root.sidebarCursor === index
                  onHovered: { root.pane = "sidebar"; root.sidebarCursor = index }
                  onClicked: root.showSidebarItem(index)
                }
              }

              Item { width: 1; height: Style.space(8) }
            }

            // Engine state at the foot of the sidebar.
            Column {
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.bottom: parent.bottom
              anchors.margins: Style.space(14)
              spacing: Style.spacing.xxs

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: root.connected ? "" : (root.service ? "Starting engine…" : "Engine unavailable")
                visible: text !== ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
                renderType: Text.NativeRendering
              }
              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: "? for keys"
                color: root.faint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                renderType: Text.NativeRendering
              }
            }
          }

          Rectangle {
            width: Math.max(1, Style.normalBorderWidth)
            height: parent.height
            color: Util.alpha(root.border, 0.28)
          }

          // ---------- Main pane ----------
          Item {
            id: main
            width: parent.width - sidebar.width - Math.max(1, Style.normalBorderWidth)
            height: parent.height

            Loader {
              id: viewLoader
              anchors.fill: parent
              anchors.margins: root.contentMargin
              asynchronous: false

              function load() {
                var url = Qt.resolvedUrl(root.viewSource(root.view))
                viewLoader.setSource(url, { browser: root })
              }

              onStatusChanged: {
                if (status === Loader.Error) console.warn("podcast: view failed to load:", root.view, viewLoader.sourceComponent ? viewLoader.sourceComponent.errorString() : "")
              }
            }
          }
        }

        KeyHelp {
          anchors.fill: parent
          opened: root.helpOpen
          view: root.view
          foreground: root.foreground
          background: root.background
          borderSpec: root.borderSpec
          scrim: root.scrim
          fontFamily: root.fontFamily
          cornerRadius: root.cornerRadius
          onDismissed: root.helpOpen = false
        }
      }
    }
  }
}
