import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import ".."
import "../../Model.js" as Model

// Find podcasts: search the catalogue as you type, paste a feed URL, import
// an OPML file, or browse what is trending (with a Podcast Index key).
Item {
  id: root
  required property var browser
  readonly property var service: browser ? browser.service : null

  property string query: ""
  property var results: []
  property string provider: ""
  property bool searching: false
  property string error: ""
  property int cursorIndex: 0
  property bool cursorActive: false
  property string mode: "search"        // search | url | opml
  property var preview: null
  property bool previewLoading: false
  property string importResult: ""
  property int requestSerial: 0
  property string region: "auto"

  readonly property bool hasIndex: !!service && !!service.engine && !!service.engine.providers && service.engine.providers.podcastindex === true
  readonly property int columns: Math.max(1, Math.floor(grid.width / grid.cellWidth))
  readonly property var current: cursorIndex >= 0 && cursorIndex < results.length ? results[cursorIndex] : null
  readonly property var regions: [
    { value: "auto", label: "Region: system" }, { value: "US", label: "United States" }, { value: "GB", label: "United Kingdom" },
    { value: "CN", label: "China" }, { value: "TW", label: "Taiwan" }, { value: "HK", label: "Hong Kong" }, { value: "JP", label: "Japan" },
    { value: "KR", label: "Korea" }, { value: "DE", label: "Germany" }, { value: "FR", label: "France" }, { value: "ES", label: "Spain" },
    { value: "IT", label: "Italy" }, { value: "BR", label: "Brazil" }, { value: "MX", label: "Mexico" }, { value: "IN", label: "India" },
    { value: "AU", label: "Australia" }, { value: "CA", label: "Canada" }, { value: "NL", label: "Netherlands" }, { value: "SE", label: "Sweden" },
  ]

  function focusField() {
    browser.editing = true
    Qt.callLater(function() {
      var field = root.mode === "url" ? urlField : (root.mode === "opml" ? opmlField : searchField)
      field.forceActiveFocus()
      field.selectAll()
    })
  }

  function leaveField() {
    browser.editing = false
    browser.refocus()
  }

  function runSearch() {
    var text = query.trim()
    if (!service || text.length < 2) { results = []; searching = false; return }
    searching = true
    error = ""
    var serial = ++requestSerial
    var options = {}
    if (region !== "auto") options.country = region
    service.search(text, "term", options, function(ok, result) {
      if (serial !== root.requestSerial) return
      root.searching = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Search failed"; return }
      root.results = result && result.results ? result.results : []
      root.provider = result && result.provider ? String(result.provider) : ""
      root.cursorIndex = 0
    })
  }

  function loadTrending() {
    if (!service || !hasIndex) return
    searching = true
    var serial = ++requestSerial
    service.trending({}, function(ok, result) {
      if (serial !== root.requestSerial) return
      root.searching = false
      if (ok && result) { root.results = result.results || []; root.provider = "trending"; root.cursorIndex = 0 }
    })
  }

  function subscribeTo(item) {
    if (!service || !item) return
    if (item.subscribed && item.podcastId) { browser.navigate("podcast", { podcastId: item.podcastId }, true); return }
    var feed = item.feedUrl
    service.subscribe(feed, function(ok, result) {
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not subscribe"; return }
      var next = []
      for (var i = 0; i < root.results.length; i++) {
        var r = root.results[i]
        if (r.feedUrl === feed) { var copy = {}; for (var k in r) copy[k] = r[k]; copy.subscribed = true; copy.podcastId = result.podcast.id; next.push(copy) }
        else next.push(r)
      }
      root.results = next
      if (root.preview && root.preview.podcast && root.preview.podcast.feed_url === feed) root.preview = null
    })
  }

  function previewUrl(url) {
    if (!service || !url) return
    previewLoading = true
    error = ""
    service.previewFeed(url, function(ok, result) {
      root.previewLoading = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not read that feed"; root.preview = null; return }
      root.preview = result
    })
  }

  function importOpml(path) {
    if (!service || !path) return
    importResult = "Importing…"
    service.importOpml(path, function(ok, result) {
      if (!ok) { root.importResult = result && result.message ? String(result.message) : "Import failed"; return }
      root.importResult = "Added " + result.added.length + ", already had " + result.skipped.length + (result.failed.length ? ", failed " + result.failed.length : "")
    })
  }

  function moveCursor(delta) {
    if (results.length === 0) return
    if (!cursorActive) { cursorActive = true; return }
    cursorIndex = Math.max(0, Math.min(results.length - 1, cursorIndex + delta))
    grid.positionViewAtIndex(cursorIndex, GridView.Contain)
  }

  function handleEscape() {
    if (preview) { preview = null; return true }
    if (mode !== "search") { mode = "search"; return true }
    if (query !== "") { query = ""; searchField.text = ""; results = []; if (hasIndex) loadTrending(); return true }
    return false
  }

  function handleKey(event) {
    if (event.text === "/") { mode = "search"; focusField(); return true }
    if (event.text === "u") { mode = "url"; focusField(); return true }
    if (event.text === "o") { mode = "opml"; focusField(); return true }
    if (results.length > 0) {
      if (event.key === Qt.Key_Down || event.text === "j") { moveCursor(columns); return true }
      if (event.key === Qt.Key_Up || event.text === "k") { if (cursorActive && cursorIndex < columns) { cursorActive = false; focusField(); return true } moveCursor(-columns); return true }
      if (event.key === Qt.Key_Right || event.text === "l") { moveCursor(1); return true }
      if (event.key === Qt.Key_Left || event.text === "h") { if (cursorActive && cursorIndex % columns === 0) { browser.pane = "sidebar"; return true } moveCursor(-1); return true }
      if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { if (!cursorActive) { cursorActive = true; return true } subscribeTo(current); return true }
      if (event.text === "s" && current) { subscribeTo(current); return true }
    }
    if (event.text === "h" || event.key === Qt.Key_Left) { browser.pane = "sidebar"; return true }
    return false
  }

  Component.onCompleted: {
    if (hasIndex) loadTrending()
    focusField()
  }

  Timer {
    id: debounce
    interval: 400
    onTriggered: root.runSearch()
  }

  Column {
    anchors.fill: parent
    spacing: Style.space(12)

    ViewHeader {
      width: parent.width
      title: "Discover"
      subtitle: {
        if (root.error !== "") return root.error
        if (root.searching) return "Searching…"
        if (root.query.trim().length >= 2) return root.results.length + " results" + (root.provider === "podcastindex" ? " from Podcast Index" : " from Apple's catalogue")
        if (root.provider === "trending" && root.results.length > 0) return "Trending on Podcast Index"
        return "Type a name, paste a feed URL (u), or import an OPML file (o)."
      }
      foreground: browser.foreground
      fontFamily: browser.fontFamily

      Dropdown {
        showLabel: false
        foreground: browser.foreground
        fontFamily: browser.fontFamily
        options: root.regions
        value: root.region
        onChanged: function(value) { root.region = value; if (root.query.trim().length >= 2) root.runSearch() }
        onPopupOpenChanged: browser.editing = popupOpen
      }
    }

    // ---------- Input row ----------
    RowLayout {
      width: parent.width
      spacing: Style.space(8)

      TextField {
        id: searchField
        visible: root.mode === "search"
        Layout.fillWidth: true
        placeholderText: "Search podcasts by name or author"
        foreground: browser.foreground
        onTextChanged: { root.query = text; root.cursorActive = false; debounce.restart() }
        onActiveFocusChanged: browser.editing = activeFocus || urlField.activeFocus || opmlField.activeFocus
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) { root.leaveField(); event.accepted = true }
          else if (event.key === Qt.Key_Down || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            if (root.query.trim().length >= 2 && debounce.running) { debounce.stop(); root.runSearch() }
            root.leaveField(); root.cursorActive = root.results.length > 0; event.accepted = true
          }
        }
      }

      TextField {
        id: urlField
        visible: root.mode === "url"
        Layout.fillWidth: true
        placeholderText: "https://example.com/feed.xml"
        foreground: browser.foreground
        onActiveFocusChanged: browser.editing = activeFocus || searchField.activeFocus || opmlField.activeFocus
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) { root.leaveField(); root.mode = "search"; event.accepted = true }
          else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { root.previewUrl(text.trim()); root.leaveField(); event.accepted = true }
        }
      }

      TextField {
        id: opmlField
        visible: root.mode === "opml"
        Layout.fillWidth: true
        placeholderText: "~/Downloads/subscriptions.opml"
        foreground: browser.foreground
        onActiveFocusChanged: browser.editing = activeFocus || searchField.activeFocus || urlField.activeFocus
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) { root.leaveField(); root.mode = "search"; event.accepted = true }
          else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { root.importOpml(text.trim()); root.leaveField(); event.accepted = true }
        }
      }

      ButtonGroup {
        foreground: browser.foreground
        background: browser.background
        fontFamily: browser.fontFamily
        fontSize: Style.font.bodySmall
        focusable: false
        options: [{ value: "search", label: "Search", icon: "󰍉" }, { value: "url", label: "Feed URL", icon: "󰑫" }, { value: "opml", label: "OPML", icon: "󰈠" }]
        value: root.mode
        onChanged: function(value) { root.mode = value; root.focusField() }
      }
    }

    Text {
      visible: root.mode === "opml" && root.importResult !== ""
      width: parent.width
      textFormat: Text.PlainText
      text: root.importResult
      color: browser.dim
      font.family: browser.fontFamily
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
      renderType: Text.NativeRendering
    }

    // ---------- Feed preview ----------
    BorderSurface {
      visible: root.mode === "url" && (root.preview !== null || root.previewLoading)
      width: parent.width
      implicitHeight: previewRow.implicitHeight + Style.space(24)
      radius: Style.cornerRadius
      color: Style.normalFillFor(browser.foreground, Color.accent)
      borderSpec: Border.controlSpec("normal", browser.foreground, Color.accent)
      padding: Style.space(12)

      RowLayout {
        id: previewRow
        anchors.fill: parent
        anchors.margins: Style.space(12)
        spacing: Style.space(12)

        Artwork {
          size: Style.space(72)
          source: root.preview && root.preview.podcast ? String(root.preview.podcast.image_url || "") : ""
          foreground: browser.foreground
        }

        Column {
          Layout.fillWidth: true
          spacing: Style.spacing.xxs

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.previewLoading ? "Reading feed…" : (root.preview && root.preview.podcast ? String(root.preview.podcast.title || "") : "")
            color: browser.foreground
            font.family: browser.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
            elide: Text.ElideRight
            renderType: Text.NativeRendering
          }
          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.preview && root.preview.podcast ? [String(root.preview.podcast.author || ""), root.preview.podcast.episodeCount + " episodes"].filter(function(s) { return s !== "" }).join("  ·  ") : ""
            color: browser.dim
            font.family: browser.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
            renderType: Text.NativeRendering
          }
          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.preview && root.preview.podcast ? Model.elide(String(root.preview.podcast.description_text || ""), 240) : ""
            color: browser.dim
            font.family: browser.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
            maximumLineCount: 3
            elide: Text.ElideRight
            renderType: Text.NativeRendering
          }
        }

        Button {
          foreground: browser.foreground
          fontFamily: browser.fontFamily
          bordered: true
          enabled: !!root.preview
          iconText: root.preview && root.preview.podcast && root.preview.podcast.subscribed ? "󰗠" : "󰐕"
          text: root.preview && root.preview.podcast && root.preview.podcast.subscribed ? "Subscribed" : "Subscribe"
          onClicked: {
            if (!root.preview) return
            if (root.preview.podcast.subscribed) browser.navigate("podcast", { podcastId: root.preview.podcast.podcastId }, true)
            else root.subscribeTo({ feedUrl: root.preview.podcast.feed_url })
          }
        }
      }
    }

    // ---------- Results ----------
    GridView {
      id: grid
      width: parent.width
      height: parent.height - y
      clip: true
      visible: root.mode === "search"
      model: root.results
      cellWidth: Style.space(148)
      cellHeight: Style.space(196)
      boundsBehavior: Flickable.StopAtBounds
      cacheBuffer: Style.space(400)
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

      delegate: Item {
        required property var modelData
        required property int index
        width: grid.cellWidth
        height: grid.cellHeight

        CursorSurface {
          id: tile
          anchors.centerIn: parent
          width: grid.cellWidth - Style.space(8)
          height: grid.cellHeight - Style.space(8)
          foreground: browser.foreground
          hasCursor: browser.pane === "main" && root.cursorActive && root.cursorIndex === index
          current: modelData.subscribed === true

          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onEntered: { root.cursorActive = true; root.cursorIndex = index; browser.pane = "main" }
            onClicked: root.subscribeTo(modelData)
          }

          Column {
            anchors.top: parent.top
            anchors.topMargin: Style.space(8)
            anchors.horizontalCenter: parent.horizontalCenter
            width: Style.space(112)
            spacing: Style.space(6)

            Artwork {
              size: Style.space(112)
              source: String(modelData.artwork || "")
              foreground: browser.foreground
              glyphSize: Style.space(40)
            }

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: String(modelData.title || "")
              color: browser.foreground
              font.family: browser.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
              wrapMode: Text.WordWrap
              maximumLineCount: 2
              elide: Text.ElideRight
              horizontalAlignment: Text.AlignHCenter
              renderType: Text.NativeRendering
            }

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: modelData.subscribed ? "Subscribed" : [String(modelData.author || ""), modelData.episodeCount ? modelData.episodeCount + " ep" : ""].filter(function(s) { return s !== "" }).join(" · ")
              color: modelData.subscribed ? Color.accent : browser.dim
              font.family: browser.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
              horizontalAlignment: Text.AlignHCenter
              renderType: Text.NativeRendering
            }
          }
        }
      }
    }
  }
}
