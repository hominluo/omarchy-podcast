import QtQuick
import qs.Commons
import qs.Ui
import ".."
import "../../Model.js" as Model

// The queue. (placeholder until the view lands)
Item {
  id: root
  required property var browser
  readonly property var service: browser ? browser.service : null

  // The browser asks the view first; return true when the key was used.
  function handleKey(event) { return false }
  // Esc ladder hook: return true when something view-local was closed.
  function handleEscape() { return false }

  ViewHeader {
    id: header
    width: parent.width
    title: "UpNext"
    subtitle: "The queue."
    foreground: browser.foreground
    fontFamily: browser.fontFamily
  }

  Text {
    anchors.top: header.bottom
    anchors.topMargin: Style.space(24)
    textFormat: Text.PlainText
    text: "Nothing here yet."
    color: browser.dim
    font.family: browser.fontFamily
    font.pixelSize: Style.font.body
    renderType: Text.NativeRendering
  }
}
