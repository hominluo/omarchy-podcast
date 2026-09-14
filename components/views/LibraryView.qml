import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import ".."
import "../../Model.js" as Model

// Every podcast you follow, as a grid of covers. `/` filters by title.
Item {
  id: root
  required property var browser
  readonly property var service: browser ? browser.service : null

  property string filter: ""
  property bool searching: false
  property int cursorIndex: 0
  property bool cursorActive: false
  property bool confirmOpen: false

  readonly property var podcasts: {
    var all = service ? (service.library || []) : []
    var q = filter.trim().toLowerCase()
    if (q === "") return all
    var out = []
    for (var i = 0; i < all.length; i++) {
      var p = all[i]
      if ((String(p.title || "") + " " + String(p.author || "")).toLowerCase().indexOf(q) !== -1) out.push(p)
    }
    return out
  }
  readonly property var current: cursorIndex >= 0 && cursorIndex < podcasts.length ? podcasts[cursorIndex] : null
  readonly property int columns: Math.max(1, Math.floor(grid.width / grid.cellWidth))

  function openPodcast(podcast) {
    if (podcast) browser.navigate("podcast", { podcastId: podcast.id }, true)
  }

  function moveCursor(delta) {
    if (podcasts.length === 0) return
    if (!cursorActive) { cursorActive = true; return }
    cursorIndex = Math.max(0, Math.min(podcasts.length - 1, cursorIndex + delta))
    grid.positionViewAtIndex(cursorIndex, GridView.Contain)
  }

  function startSearch() {
    searching = true
    browser.editing = true
    Qt.callLater(function() { searchField.forceActiveFocus(); searchField.selectAll() })
  }

  function endSearch(clear) {
    if (clear) { filter = ""; searchField.text = "" }
    searching = filter !== ""
    browser.editing = false
    browser.refocus()
  }

  function handleEscape() {
    if (confirmOpen) { confirmOpen = false; return true }
    if (searching || filter !== "") { endSearch(true); return true }
    return false
  }

  function handleKey(event) {
    if (confirmOpen) { if (confirm.handleKey(event)) return true; return true }
    if (event.text === "/") { startSearch(); return true }
    if (event.key === Qt.Key_Down || event.text === "j") { moveCursor(columns); return true }
    if (event.key === Qt.Key_Up || event.text === "k") { moveCursor(-columns); return true }
    if (event.key === Qt.Key_Right || event.text === "l") { moveCursor(1); return true }
    if (event.key === Qt.Key_Left || event.text === "h") {
      if (cursorActive && cursorIndex % columns === 0) { browser.pane = "sidebar"; return true }
      moveCursor(-1); return true
    }
    if (event.key === Qt.Key_Home) { cursorActive = true; cursorIndex = 0; grid.positionViewAtIndex(0, GridView.Contain); return true }
    if (event.key === Qt.Key_End) { cursorActive = true; cursorIndex = podcasts.length - 1; grid.positionViewAtIndex(cursorIndex, GridView.Contain); return true }
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      if (!cursorActive) { cursorActive = true; return true }
      openPodcast(current); return true
    }
    if (event.text === "r") { if (service) { service.refresh(); } return true }
    if (event.text === "s" && cursorActive && current) { confirmOpen = true; return true }
    return false
  }

  Column {
    anchors.fill: parent
    spacing: Style.space(12)

    ViewHeader {
      width: parent.width
      title: "Library"
      subtitle: {
        var n = service ? (service.library || []).length : 0
        if (n === 0) return "Nothing yet — Browse has the charts, search by name, or paste a feed URL."
        var fresh = 0
        for (var i = 0; i < service.library.length; i++) fresh += Number(service.library[i].counts.new) || 0
        return n + (n === 1 ? " podcast" : " podcasts") + (fresh > 0 ? "  ·  " + fresh + " new" : "")
          + (service.jobs && service.jobs.refreshing ? "  ·  refreshing " + service.jobs.refreshDone + "/" + service.jobs.refreshTotal : "")
      }
      foreground: browser.foreground
      fontFamily: browser.fontFamily

      TextField {
        id: searchField
        visible: root.searching || root.filter !== ""
        width: Style.space(220)
        placeholderText: "Filter podcasts"
        foreground: browser.foreground
        onTextChanged: { root.filter = text; root.cursorIndex = 0 }
        onActiveFocusChanged: if (!activeFocus && root.searching) root.endSearch(false)
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) { root.endSearch(true); event.accepted = true }
          else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Down) { root.endSearch(false); root.cursorActive = true; event.accepted = true }
        }
      }

      Button {
        foreground: browser.foreground
        fontFamily: browser.fontFamily
        iconText: "󰍉"
        visible: !searchField.visible
        tooltipText: "Filter  (/)"
        onClicked: root.startSearch()
      }

      Button {
        foreground: browser.foreground
        fontFamily: browser.fontFamily
        iconText: "󰑐"
        iconSpinning: service && service.jobs && service.jobs.refreshing === true
        tooltipText: "Refresh all feeds  (r)"
        onClicked: if (service) service.refresh()
      }

      Button {
        foreground: browser.foreground
        fontFamily: browser.fontFamily
        iconText: "󰐕"
        text: "Add"
        tooltipText: "Find or add a podcast"
        onClicked: browser.navigate("discover", {}, true)
      }
    }

    // `podcasts` is a fresh array on every library broadcast, which rebuilds
    // the grid at the top; the settled position is put back (see EpisodeList).
    property real _settledY: 0
    property bool _swapping: false
    function _captureY() { if (!_swapping) _settledY = grid.contentY }

    GridView {
      id: grid
      width: parent.width
      height: parent.height - y
      clip: true
      // The cursor is ours; the view must not scroll on its own currentIndex.
      highlightFollowsCurrentItem: false
      keyNavigationEnabled: false
      onContentYChanged: Qt.callLater(parent._captureY)
      onModelChanged: {
        var y = parent._settledY
        parent._swapping = true
        Qt.callLater(function() {
          if (y > 0 && grid.contentHeight > grid.height && y <= grid.contentHeight - grid.height) grid.contentY = y
          parent._settledY = grid.contentY
          parent._swapping = false
        })
      }
      model: root.podcasts
      cellWidth: Style.space(128)
      cellHeight: Style.space(160)
      boundsBehavior: Flickable.StopAtBounds
      cacheBuffer: Style.space(400)
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

      delegate: Item {
        required property var modelData
        required property int index
        width: grid.cellWidth
        height: grid.cellHeight

        PodcastTile {
          anchors.centerIn: parent
          width: grid.cellWidth - Style.space(8)
          height: grid.cellHeight - Style.space(8)
          podcast: modelData
          foreground: browser.foreground
          fontFamily: browser.fontFamily
          hasCursor: browser.pane === "main" && root.cursorActive && root.cursorIndex === index
          onHovered: { root.cursorActive = true; root.cursorIndex = index; browser.pane = "main" }
          onClicked: root.openPodcast(modelData)
        }
      }
    }
  }

  Text {
    anchors.centerIn: parent
    visible: root.podcasts.length === 0
    textFormat: Text.PlainText
    text: root.filter !== "" ? "No podcast matches “" + root.filter + "”" : "Press 1 (or click Browse) to find your first podcast."
    color: browser.dim
    font.family: browser.fontFamily
    font.pixelSize: Style.font.body
    renderType: Text.NativeRendering
  }

  ConfirmDialog {
    id: confirm
    anchors.fill: parent
    z: 10
    opened: root.confirmOpen
    message: root.current ? "Unsubscribe from “" + Model.elide(root.current.title, 40) + "”?" : ""
    confirmText: "Unsubscribe"
    background: browser.background
    foreground: browser.foreground
    scrim: browser.scrim
    selectedBackground: browser.selectedBackground
    selectedText: browser.selectedText
    fontFamily: browser.fontFamily
    cornerRadius: browser.cornerRadius
    onCanceled: { root.confirmOpen = false; browser.refocus() }
    onConfirmed: {
      if (root.current && service) service.unsubscribe(root.current.id, false)
      root.confirmOpen = false
      browser.refocus()
    }
  }
}
