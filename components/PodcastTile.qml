import QtQuick
import qs.Commons
import qs.Ui

// A podcast in the library grid: artwork, title, and how many episodes wait.
CursorSurface {
  id: root

  property var podcast: ({})
  property real artworkSize: Style.space(96)
  property string fontFamily: Style.font.menuFamily

  signal clicked()
  signal hovered()

  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property int unread: podcast && podcast.counts ? Number(podcast.counts.unplayed) || 0 : 0
  readonly property int fresh: podcast && podcast.counts ? Number(podcast.counts.new) || 0 : 0

  implicitWidth: artworkSize + Style.space(16)
  implicitHeight: artworkSize + Style.space(52)

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onEntered: root.hovered()
    onClicked: root.clicked()
  }

  Column {
    anchors.top: parent.top
    anchors.topMargin: Style.space(8)
    anchors.horizontalCenter: parent.horizontalCenter
    width: root.artworkSize
    spacing: Style.space(6)

    Item {
      width: root.artworkSize
      height: root.artworkSize

      Artwork {
        anchors.fill: parent
        size: root.artworkSize
        source: root.podcast ? String(root.podcast.artwork || "") : ""
        foreground: root.foreground
        glyphSize: Math.round(root.artworkSize * 0.4)
      }

      BorderSurface {
        visible: root.fresh > 0
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: -Style.space(4)
        width: Math.max(Style.space(20), pill.implicitWidth + Style.space(10))
        height: Style.space(18)
        radius: Style.cornerRadius > 0 ? height / 2 : 0
        color: Color.accent
        borderSpec: Border.flat(Color.menu.background, Math.max(1, Style.space(2)))

        Text {
          id: pill
          anchors.centerIn: parent
          textFormat: Text.PlainText
          text: root.fresh > 99 ? "99+" : String(root.fresh)
          color: Color.menu.background
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          renderType: Text.NativeRendering
        }
      }
    }

    Text {
      width: parent.width
      textFormat: Text.PlainText
      text: root.podcast ? String(root.podcast.title || "") : ""
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: root.unread > 0
      elide: Text.ElideRight
      maximumLineCount: 2
      wrapMode: Text.WordWrap
      horizontalAlignment: Text.AlignHCenter
      renderType: Text.NativeRendering
    }

    Text {
      width: parent.width
      visible: text !== ""
      textFormat: Text.PlainText
      text: {
        if (!root.podcast) return ""
        if (root.podcast.lastError) return "Feed error"
        if (root.unread > 0) return root.unread + " unplayed"
        return ""
      }
      color: root.podcast && root.podcast.lastError ? Color.urgent : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
      horizontalAlignment: Text.AlignHCenter
      renderType: Text.NativeRendering
    }
  }
}
