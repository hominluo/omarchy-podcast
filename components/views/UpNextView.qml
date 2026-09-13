import QtQuick
import qs.Commons
import qs.Ui
import ".."
import "../../Model.js" as Model

// The listening queue, in order. J/K move the episode under the cursor.
Item {
  id: root
  required property var browser
  readonly property var service: browser ? browser.service : null
  readonly property var items: service ? (service.queue || []) : []
  readonly property int totalSeconds: {
    var sum = 0
    for (var i = 0; i < items.length; i++) sum += Math.max(0, (Number(items[i].duration) || 0) - (Number(items[i].position) || 0))
    return sum
  }
  property bool confirmOpen: false

  function moveCurrent(delta) {
    var episode = body.current
    if (!episode || !service) return
    var index = body.list.cursorIndex + delta
    if (index < 0 || index >= items.length) return
    service.queueMove(episode.id, index)
    body.list.cursorIndex = index
  }

  function handleKey(event) {
    if (confirmOpen) { confirm.handleKey(event); return true }
    return body.handleKey(event)
  }
  function handleEscape() {
    if (confirmOpen) { confirmOpen = false; return true }
    return false
  }

  ListWithDetail {
    id: body
    anchors.fill: parent
    browser: root.browser
    title: "Up Next"
    subtitle: root.items.length === 0 ? "Episodes you add to Up Next play in this order when one ends."
             : root.items.length + (root.items.length === 1 ? " episode" : " episodes") + "  ·  " + Model.formatDuration(root.totalSeconds) + " to go"
    items: root.items
    emptyText: "Up Next is empty. Press a on an episode to add it."
    extraKeys: function(event) {
      if (event.text === "J") { root.moveCurrent(1); return true }
      if (event.text === "K") { root.moveCurrent(-1); return true }
      if (event.text === "x" && body.current) { service.queueRemove(body.current.id); return true }
      if (event.text === "X" && root.items.length > 0) { root.confirmOpen = true; return true }
      return false
    }
    onActivated: function(episode) { if (service) service.play(episode.id) }

    Button {
      foreground: browser.foreground
      fontFamily: browser.fontFamily
      iconText: "󰐊"
      text: "Play all"
      visible: root.items.length > 0
      tooltipText: "Start from the top"
      onClicked: if (service && root.items.length > 0) service.play(root.items[0].id)
    }

    Button {
      foreground: browser.foreground
      fontFamily: browser.fontFamily
      iconText: "󰩹"
      text: "Clear"
      visible: root.items.length > 0
      tooltipText: "Empty Up Next  (X)"
      onClicked: root.confirmOpen = true
    }
  }

  ConfirmDialog {
    id: confirm
    anchors.fill: parent
    z: 10
    opened: root.confirmOpen
    message: "Remove every episode from Up Next?"
    confirmText: "Clear"
    background: browser.background
    foreground: browser.foreground
    scrim: browser.scrim
    selectedBackground: browser.selectedBackground
    selectedText: browser.selectedText
    fontFamily: browser.fontFamily
    cornerRadius: browser.cornerRadius
    onCanceled: { root.confirmOpen = false; browser.refocus() }
    onConfirmed: { root.confirmOpen = false; if (service) service.queueClear(); browser.refocus() }
  }
}
