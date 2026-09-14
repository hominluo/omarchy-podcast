import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// The Browse view's detail pane: one show from a chart, a search or a pasted
// feed URL. The card data is shown at once; the feed itself is read when the
// cursor rests on the show, adding the description and the latest episodes.
Item {
  id: root

  property var browser: null
  property var show: null             // chart/search result (title, author, feedUrl, artwork, …)
  property var preview: null          // feed-preview answer: { podcast, episodes }
  property bool loading: false
  property string error: ""
  property bool active: false          // the pane has keyboard focus
  property int actionIndex: 0
  property int fetchSerial: 0

  signal subscribedShow(var result)   // subscribe answered; result.podcast is the library row
  signal openRequested(int podcastId)

  readonly property var service: browser ? browser.service : null
  readonly property color foreground: browser ? browser.foreground : Color.menu.text
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property string fontFamily: browser ? browser.fontFamily : Style.font.menuFamily
  readonly property var podcast: preview && preview.podcast ? preview.podcast : null
  readonly property var episodes: preview && preview.episodes ? preview.episodes : []
  readonly property bool subscribed: podcast ? podcast.subscribed === true : (show ? show.subscribed === true : false)
  readonly property int podcastId: podcast && podcast.podcastId ? Number(podcast.podcastId) : (show && show.podcastId ? Number(show.podcastId) : 0)
  readonly property string title: podcast && podcast.title ? String(podcast.title) : (show ? String(show.title || show.feedUrl || "") : "")
  readonly property string author: podcast && podcast.author ? String(podcast.author) : (show ? String(show.author || "") : "")
  readonly property string artwork: podcast && podcast.image_url ? String(podcast.image_url) : (show ? String(show.artwork || "") : "")
  readonly property string description: {
    if (podcast && podcast.description_text) return String(podcast.description_text)
    return show ? String(show.description || "") : ""
  }
  readonly property string metaLine: {
    var parts = []
    if (show && show.rank) parts.push("#" + show.rank)
    var count = podcast ? Number(podcast.episodeCount) : (show ? Number(show.episodeCount) : 0)
    if (count > 0) parts.push(count + " episode" + (count === 1 ? "" : "s"))
    var cats = show && Array.isArray(show.categories) ? show.categories.slice(0, 2) : []
    for (var i = 0; i < cats.length; i++) parts.push(String(cats[i]))
    if (podcast && podcast.language) parts.push(String(podcast.language).toUpperCase())
    return parts.join("  ·  ")
  }
  readonly property var actions: {
    if (!show) return []
    var list = []
    if (subscribed) list.push({ key: "open", glyph: "󰌱", label: "Open in library", hint: "Enter" })
    else list.push({ key: "subscribe", glyph: "󰐕", label: "Subscribe", hint: "s" })
    if (!subscribed && podcast === null && !loading) list.push({ key: "reload", glyph: "󰑐", label: "Read feed", hint: "r" })
    return list
  }

  // The action list shrinks once the feed is read; the cursor must not
  // point past its end.
  onActionsChanged: actionIndex = Math.max(0, Math.min(actionIndex, actions.length - 1))

  // Show the card straight away; the feed follows when `fetch()` is called.
  function present(item) {
    if (root.show && item && root.show.feedUrl === item.feedUrl) { root.show = item; return }
    root.show = item || null
    root.preview = null
    root.error = ""
    root.loading = false
    root.actionIndex = 0
    root.fetchSerial++
    if (body) body.contentY = 0
  }

  function fetch() {
    if (!service || !show || !show.feedUrl) return
    if (preview && podcast && podcast.feed_url === show.feedUrl) return
    var serial = ++root.fetchSerial
    var wanted = String(show.feedUrl)
    root.loading = true
    root.error = ""
    service.previewFeed(wanted, function(ok, result) {
      if (serial !== root.fetchSerial) return
      root.loading = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not read that feed"; return }
      root.preview = result
    })
  }

  function subscribe() {
    if (!service || !show || !show.feedUrl) return
    if (subscribed) { root.openRequested(root.podcastId); return }
    root.loading = true
    root.error = ""
    var wanted = String(show.feedUrl)
    service.subscribe(wanted, function(ok, result) {
      if (!root.show || root.show.feedUrl !== wanted) return
      root.loading = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not subscribe"; return }
      root.markSubscribed(result.podcast.id)
      root.subscribedShow(result)
    })
  }

  function markSubscribed(podcastId) {
    if (root.show) {
      var copy = {}
      for (var key in root.show) copy[key] = root.show[key]
      copy.subscribed = true
      copy.podcastId = podcastId
      root.show = copy
    }
    if (root.preview && root.preview.podcast) {
      var next = JSON.parse(JSON.stringify(root.preview))
      next.podcast.subscribed = true
      next.podcast.podcastId = podcastId
      root.preview = next
    }
    root.actionIndex = 0
  }

  function run(key) {
    switch (key) {
      case "subscribe": subscribe(); break
      case "open": root.openRequested(root.podcastId); break
      case "reload": fetch(); break
    }
  }

  function handleKey(event) {
    if (!show) return false
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      var action = root.actions[root.actionIndex]
      if (action) run(action.key)
      return true
    }
    if (event.text === "s") { subscribe(); return true }
    if (event.text === "r") { root.preview = null; fetch(); return true }
    if (event.text === "j" || event.key === Qt.Key_Down) { body.contentY = Math.min(Math.max(0, body.contentHeight - body.height), body.contentY + Style.space(48)); return true }
    if (event.text === "k" || event.key === Qt.Key_Up) { body.contentY = Math.max(0, body.contentY - Style.space(48)); return true }
    if (event.text === "l" || event.key === Qt.Key_Right) { root.actionIndex = Math.min(root.actions.length - 1, root.actionIndex + 1); return true }
    if (event.text === "h" || event.key === Qt.Key_Left) {
      if (root.actionIndex > 0) { root.actionIndex--; return true }
      return false   // the view moves the focus back to the grid
    }
    return false
  }

  Column {
    anchors.fill: parent
    spacing: Style.space(12)
    visible: !!root.show

    RowLayout {
      width: parent.width
      spacing: Style.space(12)

      Artwork {
        size: Style.space(84)
        source: root.artwork
        foreground: root.foreground
        Layout.alignment: Qt.AlignTop
      }

      Column {
        Layout.fillWidth: true
        spacing: Style.spacing.xxs

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: root.title
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.title
          font.bold: true
          wrapMode: Text.WordWrap
          maximumLineCount: 3
          elide: Text.ElideRight
          renderType: Text.NativeRendering
        }

        Text {
          width: parent.width
          visible: text !== ""
          textFormat: Text.PlainText
          text: root.author
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          elide: Text.ElideRight
          renderType: Text.NativeRendering
        }

        Text {
          width: parent.width
          visible: text !== ""
          textFormat: Text.PlainText
          text: root.metaLine
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          renderType: Text.NativeRendering
        }
      }
    }

    Flow {
      width: parent.width
      spacing: Style.space(6)

      Repeater {
        model: root.actions

        Button {
          required property var modelData
          required property int index
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          bordered: true
          hasCursor: root.active && root.actionIndex === index
          selected: modelData.key === "open"
          iconText: modelData.glyph
          text: modelData.label
          tooltipText: modelData.label + "  (" + modelData.hint + ")"
          onHovered: function(isHovered) { if (isHovered) { root.actionIndex = index; if (root.browser) root.browser.pane = "detail" } }
          onClicked: root.run(modelData.key)
        }
      }
    }

    Text {
      width: parent.width
      visible: text !== ""
      textFormat: Text.PlainText
      text: root.error !== "" ? root.error : (root.loading ? (root.podcast ? "Subscribing…" : "Reading the feed…") : "")
      color: root.error !== "" ? (root.browser ? root.browser.urgent : Color.urgent) : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
      renderType: Text.NativeRendering
    }

    PanelSeparator { foreground: root.foreground }

    Flickable {
      id: body
      width: parent.width
      height: parent.height - y
      contentHeight: bodyColumn.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds

      Column {
        id: bodyColumn
        width: body.width
        spacing: Style.space(12)

        Text {
          width: parent.width
          visible: text !== ""
          textFormat: Text.PlainText
          text: root.description
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
          renderType: Text.NativeRendering
        }

        Column {
          width: parent.width
          visible: root.episodes.length > 0
          spacing: Style.space(4)

          PanelSectionHeader { text: "LATEST EPISODES"; foreground: root.foreground; fontFamily: root.fontFamily }

          Repeater {
            model: root.episodes.slice(0, 8)

            Column {
              required property var modelData
              width: parent.width
              spacing: Style.spacing.xxs
              topPadding: Style.space(4)
              bottomPadding: Style.space(4)

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: String(modelData.title || "")
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
                renderType: Text.NativeRendering
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: {
                  var parts = []
                  var date = Model.formatRelativeDate(modelData.pubDate)
                  if (date) parts.push(date)
                  if (modelData.duration) parts.push(Model.formatDuration(modelData.duration))
                  return parts.join("  ·  ")
                }
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
                renderType: Text.NativeRendering
              }
            }
          }
        }

        Text {
          width: parent.width
          visible: !root.loading && root.podcast === null && root.error === "" && root.description === ""
          textFormat: Text.PlainText
          text: "Press r to read the feed for its description and latest episodes."
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
          renderType: Text.NativeRendering
        }
      }
    }
  }
}
