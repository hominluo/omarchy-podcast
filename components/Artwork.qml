import QtQuick
import Quickshell.Widgets
import qs.Commons
import qs.Ui

// Square cover art with a glyph standing in until the image is ready (or
// when there is none). The daemon hands us local files it has already
// normalised to JPEG, so `source` is a path; a bare URL also works.
//
// Decoded at display size: a grid of 3000px PNGs decoded at full size is a
// grid that costs hundreds of megabytes to scroll.
Item {
  id: root

  property string source: ""
  property real size: Style.space(64)
  property color foreground: Color.foreground
  property color accent: Color.accent
  property string glyph: "󰦔"
  property real glyphSize: Math.max(Style.font.title, Math.round(size * 0.42))
  property real radius: Style.cornerRadius > 0 ? Math.min(Style.spacing.md, Style.cornerRadius) : 0
  property bool bordered: true

  readonly property string url: {
    var value = String(source || "")
    if (value === "") return ""
    if (value.indexOf("://") !== -1 || value.indexOf("data:") === 0) return value
    return Util.fileUrl(value)
  }
  readonly property bool ready: image.status === Image.Ready && root.url !== ""

  implicitWidth: size
  implicitHeight: size
  width: size
  height: size

  BorderSurface {
    anchors.fill: parent
    radius: root.radius
    color: Style.normalFillFor(root.foreground, root.accent)
    borderSpec: root.bordered ? Border.controlSpec("normal", root.foreground, root.accent) : Border.none()
  }

  ClippingRectangle {
    id: clip
    anchors.fill: parent
    anchors.margins: root.bordered ? Math.max(1, Style.normalBorderWidth) : 0
    radius: Math.max(0, root.radius - anchors.margins)
    color: "transparent"

    Image {
      id: image
      anchors.fill: parent
      source: root.url
      asynchronous: true
      cache: true
      smooth: true
      fillMode: Image.PreserveAspectCrop
      sourceSize.width: Math.round(root.size * 2)
      sourceSize.height: Math.round(root.size * 2)
      visible: root.ready
      opacity: root.ready ? 1 : 0
      Behavior on opacity { NumberAnimation { duration: 160 } }
    }
  }

  Text {
    anchors.centerIn: parent
    visible: !root.ready
    textFormat: Text.PlainText
    text: root.glyph
    color: Qt.darker(root.foreground, 1.4)
    font.family: Style.font.family
    font.pixelSize: root.glyphSize
    renderType: Text.NativeRendering
  }
}
