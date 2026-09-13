import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// The transcript pane. (Filled in with the transcripts milestone.)
Item {
  id: root

  property var browser: null
  property var episode: null
  readonly property var service: browser ? browser.service : null

  function handleKey(event) { return false }
  function handleEscape() { return false }

  Column {
    anchors.centerIn: parent
    spacing: Style.space(10)
    width: Math.min(parent.width - Style.space(40), Style.space(420))

    Text {
      width: parent.width
      textFormat: Text.PlainText
      text: "Transcripts arrive with the next milestone."
      color: browser ? browser.dim : Color.menu.text
      font.family: browser ? browser.fontFamily : Style.font.menuFamily
      font.pixelSize: Style.font.body
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
      renderType: Text.NativeRendering
    }
  }
}
