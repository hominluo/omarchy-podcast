import QtQuick
import QtQuick.Controls
import qs.Commons

// Show notes as rich text. The daemon already reduced the HTML to a safe
// subset; Model.sanitizeShowNotes runs once more here because this is the
// one place untrusted markup meets Text.RichText. Links open in the browser
// only when clicked, never on their own.
Flickable {
  id: root

  property string html: ""
  property color foreground: Color.menu.text
  property string fontFamily: Style.font.menuFamily
  property real fontSize: Style.font.body
  property int maxChars: 40000

  readonly property string safeHtml: {
    var text = String(html || "")
    if (text.length > maxChars) text = text.substring(0, maxChars) + "…"
    return _sanitize(text)
  }

  function _sanitize(text) {
    text = text.replace(/<\s*(script|style|iframe|object|embed|svg|noscript|head|title)[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "")
    text = text.replace(/<\s*img[^>]*>/gi, "")
    text = text.replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "")
    text = text.replace(/href\s*=\s*("|')\s*javascript:[^"']*\1/gi, 'href="#"')
    // Qt's rich text paints links with its own palette blue regardless of
    // linkColor; the theme's accent goes on the anchor itself.
    text = text.replace(/<a\s+href=/gi, '<a style="color:' + String(Color.accent) + '" href=')
    return text
  }

  function openLink(link) {
    var url = String(link || "")
    if (!/^https?:\/\//i.test(url)) return
    Util.execArgv(["omarchy-launch-browser", url])
  }

  function scrollBy(delta) {
    var maxY = Math.max(0, contentHeight - height)
    contentY = Math.max(0, Math.min(maxY, contentY + delta))
  }

  contentWidth: width
  contentHeight: body.implicitHeight
  clip: true
  boundsBehavior: Flickable.StopAtBounds
  flickableDirection: Flickable.VerticalFlick
  interactive: contentHeight > height
  ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

  Text {
    id: body
    width: root.width - Style.space(12)
    textFormat: Text.RichText
    text: root.safeHtml !== "" ? root.safeHtml : "<p>No show notes.</p>"
    color: root.foreground
    linkColor: Color.accent
    font.family: root.fontFamily
    font.pixelSize: root.fontSize
    wrapMode: Text.WordWrap
    lineHeight: 1.25
    onLinkActivated: function(link) { root.openLink(link) }

    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.NoButton
      cursorShape: parent.hoveredLink ? Qt.PointingHandCursor : Qt.ArrowCursor
    }
  }
}
