import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// One episode in a list: small artwork, title, a meta line, and a hairline of
// progress. Follows the CursorSurface contract — colour comes from
// `hasCursor` / `current`, never from the mouse directly; the MouseArea only
// reports hover so the owning panel can move its single cursor here.
CursorSurface {
  id: root

  property var episode: ({})
  property color accentColor: Color.accent
  property string fontFamily: Style.font.family
  property real artworkSize: Style.space(32)
  property bool showPodcast: true
  property bool showArtwork: true
  property bool compact: false
  property string fallbackArtwork: ""
  property string trailing: ""

  signal clicked(var mouse)
  signal hovered()

  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property bool played: !!episode && episode.played === true
  readonly property real progress: episode ? Model.progressFraction(episode.position, episode.duration) : 0
  readonly property bool hasProgress: !played && progress > 0.005
  readonly property string metaText: {
    if (!episode) return ""
    var parts = []
    if (showPodcast && episode.podcastTitle) parts.push(String(episode.podcastTitle))
    var date = Model.formatRelativeDate(episode.pubDate)
    if (date) parts.push(date)
    if (played) parts.push("Played")
    else if (hasProgress) parts.push(Model.remainingText(episode.position, episode.duration))
    else if (episode.duration) parts.push(Model.formatDuration(episode.duration))
    return parts.join("  ·  ")
  }
  readonly property string badges: {
    if (!episode) return ""
    var glyphs = []
    if (episode.download === "done") glyphs.push("󰇚")
    else if (episode.download === "downloading" || episode.download === "queued") glyphs.push("󰇚")
    if (episode.transcript && episode.transcript !== "none") glyphs.push("󰨖")
    return glyphs.join(" ")
  }

  accent: accentColor
  implicitHeight: content.implicitHeight + Style.spacing.rowPaddingX
  opacity: played ? 0.6 : 1

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
    cursorShape: Qt.PointingHandCursor
    onEntered: root.hovered()
    onClicked: function(mouse) { root.clicked(mouse) }
  }

  RowLayout {
    id: content
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    anchors.leftMargin: Style.space(8)
    anchors.rightMargin: Style.space(8)
    spacing: Style.space(10)

    Artwork {
      visible: root.showArtwork
      size: root.artworkSize
      source: root.episode && root.episode.artwork ? String(root.episode.artwork) : root.fallbackArtwork
      foreground: root.foreground
      accent: root.accentColor
      glyphSize: Math.round(root.artworkSize * 0.5)
      Layout.alignment: Qt.AlignVCenter
    }

    Column {
      Layout.fillWidth: true
      spacing: Style.spacing.xxs

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: root.episode ? String(root.episode.title || "Untitled episode") : ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: root.compact ? Style.font.bodySmall : Style.font.body
        font.bold: !root.played && !root.compact
        elide: Text.ElideRight
        maximumLineCount: 1
        renderType: Text.NativeRendering
      }

      Text {
        width: parent.width
        visible: text !== ""
        textFormat: Text.PlainText
        text: root.metaText
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
        maximumLineCount: 1
        renderType: Text.NativeRendering
      }

      Item {
        width: parent.width
        height: Style.spacing.xxs
        visible: root.hasProgress

        Rectangle {
          anchors.fill: parent
          radius: height / 2
          color: Util.alpha(root.foreground, 0.12)
        }
        Rectangle {
          height: parent.height
          width: Math.round(parent.width * root.progress)
          radius: height / 2
          color: Style.selectedStateColor(root.foreground, root.accentColor)
        }
      }
    }

    Text {
      visible: text !== ""
      textFormat: Text.PlainText
      text: root.trailing !== "" ? root.trailing : root.badges
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      renderType: Text.NativeRendering
      Layout.alignment: Qt.AlignVCenter
    }
  }
}
