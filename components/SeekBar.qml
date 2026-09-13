import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// Seek slider with chapter notches and the elapsed / remaining readout.
//
// `PanelSlider` commits on release and keeps a live value while dragging, so
// the labels follow the knob without seeking on every pixel. Chapter starts
// are cut into the track the way PanelSlider draws its own ticks, at their
// real time rather than evenly spaced; hovering one names the chapter and
// clicking it jumps there.
Item {
  id: root

  property QtObject bar: null
  property real position: 0
  property real duration: 0
  property var chapters: []
  property int step: 15
  property bool enabled: duration > 0
  property color foreground: bar ? bar.foreground : Color.foreground
  property color background: bar ? bar.background : Color.popups.background
  property string fontFamily: bar ? bar.fontFamily : Style.font.family

  signal seekRequested(real seconds)

  readonly property real shownPosition: slider.dragging ? slider.liveValue : position
  readonly property int chapterIndex: Model.chapterIndexAt(chapters, shownPosition)
  readonly property color dim: Qt.darker(foreground, 1.4)

  implicitHeight: slider.implicitHeight + labels.implicitHeight + Style.spacing.xxs
  height: implicitHeight

  PanelSlider {
    id: slider
    bar: root.bar
    width: parent.width
    minimum: 0
    maximum: Math.max(1, root.duration)
    step: root.step
    value: root.position
    enabled: root.enabled
    opacity: root.enabled ? 1 : 0.45
    fillColor: root.foreground
    knobColor: root.foreground
    trackColor: Style.selectedFillFor(root.foreground, Color.accent)
    tickColor: root.background
    onReleased: function(value) { if (root.enabled) root.seekRequested(value) }
  }

  Repeater {
    model: root.duration > 0 && root.chapters ? root.chapters : []

    Item {
      id: notch
      required property var modelData
      required property int index
      readonly property real fraction: Math.max(0, Math.min(1, (Number(modelData.startTime) || 0) / root.duration))
      visible: index > 0 && fraction > 0.004 && fraction < 0.996
      width: Math.max(1, Style.space(2))
      height: slider.trackHeight + Style.space(4)
      anchors.verticalCenter: slider.verticalCenter
      x: Math.round(slider.width * fraction - width / 2)

      Rectangle {
        anchors.fill: parent
        radius: 1
        color: root.background
      }

      MouseArea {
        id: notchMouse
        anchors.centerIn: parent
        width: parent.width + Style.space(8)
        height: parent.height + Style.space(8)
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.seekRequested(Number(notch.modelData.startTime) || 0)
      }

      PanelToolTip {
        visible: notchMouse.containsMouse && String(notch.modelData.title || "") !== ""
        text: String(notch.modelData.title || "")
        fontFamily: root.fontFamily
      }
    }
  }

  Item {
    id: labels
    anchors.top: slider.bottom
    anchors.topMargin: Style.spacing.xxs
    width: parent.width
    implicitHeight: elapsed.implicitHeight

    Text {
      id: elapsed
      anchors.left: parent.left
      textFormat: Text.PlainText
      text: Model.formatTime(root.shownPosition)
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      renderType: Text.NativeRendering
    }

    Text {
      anchors.horizontalCenter: parent.horizontalCenter
      textFormat: Text.PlainText
      visible: root.chapterIndex >= 0 && root.chapters[root.chapterIndex] && String(root.chapters[root.chapterIndex].title || "") !== ""
      text: visible ? Model.elide(String(root.chapters[root.chapterIndex].title), 34) : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      renderType: Text.NativeRendering
      width: Math.min(implicitWidth, parent.width - elapsed.implicitWidth - remaining.implicitWidth - Style.space(24))
      elide: Text.ElideRight
      horizontalAlignment: Text.AlignHCenter
    }

    Text {
      id: remaining
      anchors.right: parent.right
      textFormat: Text.PlainText
      text: root.duration > 0 ? "-" + Model.formatTime(Math.max(0, root.duration - root.shownPosition)) : "--:--"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      renderType: Text.NativeRendering
    }
  }
}
