import QtQuick
import qs.Commons
import qs.Ui

// One entry in the browse window's sidebar: glyph, label, an optional count
// and the digit that jumps to it.
CursorSurface {
  id: root

  property string glyph: ""
  property string label: ""
  property int count: 0
  property string shortcut: ""
  property string fontFamily: Style.font.family

  signal clicked()
  signal hovered()

  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property color faint: Qt.darker(foreground, 1.9)

  implicitHeight: Math.max(Style.spacing.controlHeight, labelText.implicitHeight + Style.spacing.controlPaddingY * 2)

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onEntered: root.hovered()
    onClicked: root.clicked()
  }

  Row {
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    anchors.leftMargin: Style.space(10)
    anchors.rightMargin: Style.space(10)
    spacing: Style.space(10)

    Text {
      textFormat: Text.PlainText
      text: root.glyph
      color: root.current ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.icon
      width: Style.space(18)
      horizontalAlignment: Text.AlignHCenter
      renderType: Text.NativeRendering
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: labelText
      textFormat: Text.PlainText
      text: root.label
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      font.bold: root.current
      renderType: Text.NativeRendering
      anchors.verticalCenter: parent.verticalCenter
      width: parent.width - Style.space(18) - parent.spacing * 2 - trailing.width
      elide: Text.ElideRight
    }

    Text {
      id: trailing
      textFormat: Text.PlainText
      text: root.count > 0 ? String(root.count) : root.shortcut
      color: root.count > 0 ? root.dim : root.faint
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      renderType: Text.NativeRendering
      anchors.verticalCenter: parent.verticalCenter
    }
  }
}
