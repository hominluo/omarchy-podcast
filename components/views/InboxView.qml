import QtQuick
import qs.Commons
import qs.Ui
import ".."
import "../../Model.js" as Model

// New episodes waiting for a decision: play, queue, or archive.
Item {
  id: root
  required property var browser
  readonly property var service: browser ? browser.service : null

  property var items: []
  property int count: 0

  function reload() {
    if (!service) return
    service.requestInbox(0, 200, function(ok, result) {
      if (!ok || !result) return
      root.items = result.items || []
      root.count = Number(result.count) || 0
    })
  }

  function archiveAll() {
    if (!service || items.length === 0) return
    var ids = []
    for (var i = 0; i < items.length; i++) ids.push(items[i].id)
    service.setEpisodeState(ids, "archived")
  }

  function queueAll() {
    if (!service || items.length === 0) return
    var ids = []
    for (var i = 0; i < items.length; i++) ids.push(items[i].id)
    service.queueAdd(ids, "last")
  }

  function handleKey(event) { return body.handleKey(event) }
  function handleEscape() { return false }

  Component.onCompleted: reload()

  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onInboxChanged() { root.reload() }
    function onEvent(name, data) {
      if (name === "episode" && data) {
        var next = Model.patchById(root.items, data)
        if (next.length === root.items.length) root.items = next
      }
    }
  }

  ListWithDetail {
    id: body
    anchors.fill: parent
    browser: root.browser
    title: "Inbox"
    subtitle: root.count === 0 ? "New episodes land here. Play them, add them to Up Next, or archive them (e)."
             : root.count + (root.count === 1 ? " new episode" : " new episodes")
    items: root.items
    emptyText: "Inbox zero. New episodes appear here as feeds refresh."
    extraKeys: function(event) {
      if (event.text === "E") { root.archiveAll(); return true }
      if (event.text === "Q") { root.queueAll(); return true }
      return false
    }
    onActivated: function(episode) { body.openDetail(episode) }

    Button {
      foreground: browser.foreground
      fontFamily: browser.fontFamily
      iconText: "󰐒"
      text: "Queue all"
      tooltipText: "Add every inbox episode to Up Next  (Q)"
      visible: root.items.length > 0
      onClicked: root.queueAll()
    }

    Button {
      foreground: browser.foreground
      fontFamily: browser.fontFamily
      iconText: "󱈎"
      text: "Archive all"
      tooltipText: "Clear the inbox without playing  (E)"
      visible: root.items.length > 0
      onClicked: root.archiveAll()
    }
  }
}
