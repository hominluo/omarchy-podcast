import QtQuick
import qs.Commons
import qs.Ui

// The `?` overlay: two columns of key → action, global first, then whatever
// the current view adds. Same scrim-and-card shape as ConfirmDialog.
Item {
  id: root

  property bool opened: false
  property string view: ""
  property color background: Color.menu.background
  property color foreground: Color.menu.text
  property color scrim: Color.menu.scrim
  property var borderSpec: Border.none()
  property string fontFamily: Style.font.menuFamily
  property int cornerRadius: Style.cornerRadius

  signal dismissed()

  readonly property color dim: Qt.darker(foreground, 1.4)

  readonly property var globalKeys: [
    ["Tab / Shift+Tab", "Move between sidebar, list and detail"],
    ["1–7", "Jump to a sidebar view"],
    ["j k / ↑ ↓", "Move the cursor"],
    ["h l / ← →", "Columns, or seek in Now Playing"],
    ["Enter", "Open or play"],
    ["Space", "Play / pause"],
    ["/", "Search or filter"],
    [", .", "Seek back / forward"],
    ["[ ]", "Slower / faster"],
    ["Backspace", "Back"],
    ["Esc", "Close search → back → close window"],
    ["?", "This help"],
  ]

  readonly property var viewKeys: {
    switch (view) {
      case "library": return [["Enter", "Open podcast"], ["s", "Unsubscribe"], ["r", "Refresh all feeds"]]
      case "podcast": return [["Enter", "Episode details"], ["p", "Play"], ["a", "Add to Up Next"], ["A", "Play next"], ["d", "Download"], ["m", "Mark played"], ["e", "Archive"], ["t", "Transcript"], ["s", "Unsubscribe"]]
      case "nowPlaying": return [["n / N", "Next / previous match"], ["Enter", "Seek to segment"], ["f", "Follow the audio again"], ["c", "Chapters"], ["z", "Sleep timer"], ["s / S", "Speed up / down"]]
      case "upNext": return [["Enter", "Play"], ["x", "Remove"], ["J / K", "Move down / up"], ["X", "Clear Up Next"]]
      case "inbox": return [["Enter / p", "Play"], ["a", "Add to Up Next"], ["A", "Play next"], ["e", "Archive"], ["E", "Archive all"], ["d", "Download"]]
      case "downloads": return [["x", "Cancel or delete"], ["Enter", "Play"]]
      case "discover": return [["Enter", "Show details"], ["s", "Subscribe"], ["H / L", "Previous / next category"], ["/", "Search or paste a feed URL"], ["o", "Import OPML"], ["r", "Reload chart"]]
      case "settings": return [["Enter", "Edit"], ["j k", "Move"]]
      default: return []
    }
  }

  visible: opened

  Rectangle {
    anchors.fill: parent
    color: root.scrim

    MouseArea { anchors.fill: parent; onClicked: root.dismissed() }

    BorderSurface {
      id: card
      width: Math.min(parent.width - Style.space(32), Style.space(720))
      height: Math.min(parent.height - Style.space(32), content.implicitHeight + card.contentTopInset + card.contentBottomInset)
      anchors.centerIn: parent
      color: root.background
      borderSpec: root.borderSpec
      padding: Style.space(22)
      radius: root.cornerRadius

      MouseArea { anchors.fill: parent; onClicked: {} }

      Column {
        id: content
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        spacing: Style.space(16)

        Text {
          textFormat: Text.PlainText
          text: "Keys"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.heading
          font.bold: true
          renderType: Text.NativeRendering
        }

        Row {
          width: parent.width
          spacing: Style.space(28)

          Column {
            width: (parent.width - parent.spacing) / 2
            spacing: Style.space(6)

            PanelSectionHeader { text: "EVERYWHERE"; foreground: root.foreground; fontFamily: root.fontFamily }

            Repeater {
              model: root.globalKeys
              KeyRow { width: parent.width; keys: modelData[0]; action: modelData[1] }
            }
          }

          Column {
            width: (parent.width - parent.spacing) / 2
            spacing: Style.space(6)
            visible: root.viewKeys.length > 0

            PanelSectionHeader { text: "THIS VIEW"; foreground: root.foreground; fontFamily: root.fontFamily }

            Repeater {
              model: root.viewKeys
              KeyRow { width: parent.width; keys: modelData[0]; action: modelData[1] }
            }
          }
        }
      }
    }
  }

  component KeyRow: Item {
    property string keys: ""
    property string action: ""
    required property var modelData
    implicitHeight: Math.max(keyText.implicitHeight, actionText.implicitHeight)

    Text {
      id: keyText
      textFormat: Text.PlainText
      text: parent.keys
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
      width: Style.space(120)
      renderType: Text.NativeRendering
    }
    Text {
      id: actionText
      textFormat: Text.PlainText
      text: parent.action
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      anchors.left: keyText.right
      anchors.right: parent.right
      elide: Text.ElideRight
      renderType: Text.NativeRendering
    }
  }
}
