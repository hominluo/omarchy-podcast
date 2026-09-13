import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui

// A keyboard-driven list of EpisodeRow. Owns one cursor; the view that hosts
// it decides when the list has focus (`active`) and what Enter means.
Item {
  id: root

  property var browser: null
  property var items: []
  property bool active: true
  property bool showPodcast: true
  property string emptyText: "Nothing here."
  property string fallbackArtwork: ""
  property int cursorIndex: 0
  property bool cursorActive: false
  property real artworkSize: Style.space(40)

  signal activated(var episode)
  signal secondary(var episode)
  signal cursorMoved(var episode)
  signal reachedEnd()

  readonly property var current: cursorIndex >= 0 && cursorIndex < items.length ? items[cursorIndex] : null
  readonly property color foreground: browser ? browser.foreground : Color.menu.text
  readonly property string fontFamily: browser ? browser.fontFamily : Style.font.menuFamily

  function moveCursor(delta) {
    if (items.length === 0) return
    if (!cursorActive) { cursorActive = true; if (delta > 0 && cursorIndex === 0) { reveal(); return } }
    cursorIndex = Math.max(0, Math.min(items.length - 1, cursorIndex + delta))
    reveal()
  }

  function setCursor(index) {
    cursorActive = true
    cursorIndex = Math.max(0, Math.min(items.length - 1, index))
    reveal()
  }

  function reveal() {
    list.positionViewAtIndex(cursorIndex, ListView.Contain)
    if (cursorIndex >= items.length - 5) root.reachedEnd()
    if (current) root.cursorMoved(current)
  }

  function handleKey(event) {
    if (items.length === 0) return false
    var page = Math.max(1, Math.floor(list.height / Math.max(1, Style.space(52))))
    if (event.key === Qt.Key_Down || event.text === "j") { moveCursor(1); return true }
    if (event.key === Qt.Key_Up || event.text === "k") { moveCursor(-1); return true }
    if (event.key === Qt.Key_PageDown) { moveCursor(page); return true }
    if (event.key === Qt.Key_PageUp) { moveCursor(-page); return true }
    if (event.key === Qt.Key_Home) { setCursor(0); return true }
    if (event.key === Qt.Key_End) { setCursor(items.length - 1); return true }
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      if (!cursorActive) { setCursor(cursorIndex); return true }
      if (current) root.activated(current)
      return true
    }
    return false
  }

  onItemsChanged: {
    if (cursorIndex >= items.length) cursorIndex = Math.max(0, items.length - 1)
  }

  // A patched array is a new model to the ListView, which scrolls back to the
  // top. The last settled position is remembered (deferred, so the reset does
  // not overwrite it) and restored right after the swap.
  property real _settledY: 0
  property bool _swapping: false
  function _captureY() { if (!root._swapping) root._settledY = list.contentY }
  // Hosts call this before handing over a genuinely different list (search,
  // filter) so the old list's offset is not applied to it.
  function resetScroll() { root._settledY = 0 }

  ListView {
    id: list
    anchors.fill: parent
    model: root.items
    clip: true
    onContentYChanged: Qt.callLater(root._captureY)
    onModelChanged: {
      var y = root._settledY
      root._swapping = true
      Qt.callLater(function() {
        if (y > 0 && list.contentHeight > list.height && y <= list.contentHeight - list.height) list.contentY = y
        root._settledY = list.contentY   // follows every swap, restored or not
        root._swapping = false
      })
    }
    spacing: Style.spacing.xxs
    boundsBehavior: Flickable.StopAtBounds
    cacheBuffer: Style.space(600)
    reuseItems: true
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
    onAtYEndChanged: if (atYEnd && root.items.length > 0) root.reachedEnd()

    delegate: EpisodeRow {
      id: row
      required property var modelData
      required property int index
      width: list.width - Style.space(10)
      episode: modelData
      foreground: root.foreground
      fontFamily: root.fontFamily
      showPodcast: root.showPodcast
      artworkSize: root.artworkSize
      fallbackArtwork: root.fallbackArtwork
      hasCursor: root.active && root.cursorActive && root.cursorIndex === index
      current: !!root.browser && !!root.browser.player && root.browser.player.episodeId === modelData.id
      onHovered: { root.cursorActive = true; root.cursorIndex = index; if (root.browser) root.browser.pane = "main" }
      onClicked: function(mouse) {
        root.cursorActive = true
        root.cursorIndex = index
        if (mouse.button === Qt.MiddleButton) root.secondary(modelData)
        else root.activated(modelData)
      }
    }
  }

  Text {
    anchors.centerIn: parent
    visible: root.items.length === 0
    textFormat: Text.PlainText
    text: root.emptyText
    color: Qt.darker(root.foreground, 1.4)
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    horizontalAlignment: Text.AlignHCenter
    wrapMode: Text.WordWrap
    width: Math.min(parent.width - Style.space(40), Style.space(420))
    renderType: Text.NativeRendering
  }
}
