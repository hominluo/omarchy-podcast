import QtQuick
import qs.Commons
import qs.Ui

// A header, an EpisodeList and the detail pane: the shape Inbox, Up Next,
// Downloads and History share. The host view supplies the items and any
// extra keys.
Item {
  id: root

  property var browser: null
  property string title: ""
  property string subtitle: ""
  property var items: []
  property string emptyText: "Nothing here."
  property bool showPodcast: true
  default property alias headerActions: header.trailing

  // Hosts add their own keys here; return true when handled.
  property var extraKeys: null

  readonly property var service: browser ? browser.service : null
  readonly property alias list: list
  readonly property alias detail: detailPane
  readonly property var current: list.current

  signal activated(var episode)

  function openDetail(episode) {
    if (!episode) return
    detailPane.load(episode)
    browser.detailOpen = true
  }

  function handleKey(event) {
    if (browser.pane === "detail" && browser.detailOpen && detailPane.handleKey(event)) return true
    if (extraKeys && extraKeys(event)) return true
    if (browser.pane === "main") {
      if (list.handleKey(event)) return true
      if (event.text === "l" || event.key === Qt.Key_Right) { if (browser.detailOpen) { browser.pane = "detail"; return true } }
      if (event.text === "h" || event.key === Qt.Key_Left) { browser.pane = "sidebar"; return true }
      if (list.current && event.text !== "" && "paAdmet".indexOf(event.text) !== -1) {
        if (!detailPane.episode || detailPane.episode.id !== list.current.id) openDetail(list.current)
        return detailPane.handleKey(event)
      }
    }
    return false
  }

  Row {
    anchors.fill: parent
    spacing: Style.space(16)

    Item {
      width: browser.detailOpen ? parent.width - browser.detailWidth - parent.spacing : parent.width
      height: parent.height

      Column {
        anchors.fill: parent
        spacing: Style.space(12)

        ViewHeader {
          id: header
          width: parent.width
          title: root.title
          subtitle: root.subtitle
          foreground: browser.foreground
          fontFamily: browser.fontFamily
        }

        EpisodeList {
          id: list
          width: parent.width
          height: parent.height - y
          browser: root.browser
          items: root.items
          showPodcast: root.showPodcast
          active: browser.pane === "main"
          emptyText: root.emptyText
          onActivated: function(episode) { root.activated(episode) }
          onSecondary: function(episode) { if (service) service.queueAdd(episode.id, "last") }
          onCursorMoved: function(episode) { if (browser.detailOpen) detailPane.load(episode) }
        }
      }
    }

    EpisodeDetail {
      id: detailPane
      visible: browser.detailOpen
      width: browser.detailWidth
      height: parent.height
      browser: root.browser
      active: browser.pane === "detail"
    }
  }
}
