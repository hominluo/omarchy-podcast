import QtQuick
import qs.Commons

// Title line at the top of a browse-window view, with room for controls on
// the right (search field, buttons) via the default slot.
Item {
  id: root

  property string title: ""
  property string subtitle: ""
  property color foreground: Color.menu.text
  property string fontFamily: Style.font.menuFamily
  default property alias trailing: trailingRow.children

  readonly property color dim: Qt.darker(foreground, 1.4)

  implicitHeight: Math.max(titleColumn.implicitHeight, trailingRow.implicitHeight, Style.space(34))

  Column {
    id: titleColumn
    anchors.left: parent.left
    anchors.right: trailingRow.left
    anchors.rightMargin: Style.space(12)
    anchors.verticalCenter: parent.verticalCenter
    spacing: Style.spacing.xxs

    Text {
      width: parent.width
      textFormat: Text.PlainText
      text: root.title
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.heading
      font.bold: true
      elide: Text.ElideRight
      renderType: Text.NativeRendering
    }

    Text {
      width: parent.width
      visible: text !== ""
      textFormat: Text.PlainText
      text: root.subtitle
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
      renderType: Text.NativeRendering
    }
  }

  Row {
    id: trailingRow
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    spacing: Style.space(8)
  }
}
