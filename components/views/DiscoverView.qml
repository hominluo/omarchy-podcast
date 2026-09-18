import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import ".."
import "../../Model.js" as Model

// Browse: the window's landing page. Apple's top charts for the region (per
// genre) fill the grid until the user types; a query searches the catalogue
// as you type, a pasted feed URL previews that feed, and an OPML path imports
// a whole list. Selecting a show opens it in the pane on the right, where it
// can be read and subscribed to.
Item {
  id: root
  required property var browser
  readonly property var service: browser ? browser.service : null

  property string query: ""
  property string genre: ""             // "" = the overall top chart, "trending" = Podcast Index
  property string region: "auto"
  property var genres: []               // [{id, name}] from the daemon
  property var chartResults: []
  property var searchResults: []
  property bool chartsLoading: false
  property bool searching: false
  property bool chartsStale: false
  property string chartsError: ""
  property string chartsStaleReason: ""
  property string searchError: ""
  property string actionError: ""      // the last subscribe/preview failure, cleared by the next action
  property string searchProvider: ""
  property string importResult: ""
  // canonical feed URL -> podcast id. Subscription state lives here rather
  // than in the result arrays: rewriting those would rebuild the grid (and
  // scroll it to the top) for a flag change.
  property var subscribedByUrl: ({})
  property bool restoring: false
  property int restoreCursor: -1
  property string restoreDetail: ""
  property string mode: "search"        // search | opml
  property int cursorIndex: 0
  property bool cursorActive: false
  property int requestSerial: 0
  property int chartSerial: 0

  readonly property bool hasIndex: !!service && !!service.engine && !!service.engine.providers && service.engine.providers.podcastindex === true
  readonly property bool isUrl: /^https?:\/\//i.test(query.trim())
  readonly property bool searchMode: query.trim().length >= 2 && !isUrl
  readonly property var results: searchMode ? searchResults : chartResults
  readonly property int columns: Math.max(1, Math.floor(grid.width / grid.cellWidth))
  readonly property var current: cursorIndex >= 0 && cursorIndex < results.length ? results[cursorIndex] : null

  function subscriptionOf(item) {
    if (!item || !item.feedUrl) return null
    var id = subscribedByUrl[Model.canonicalFeedUrl(item.feedUrl)]
    return id === undefined || id === null ? null : id
  }

  // A result with the live subscription state folded in, for the pane.
  function decorated(item) {
    if (!item) return null
    var copy = {}
    for (var key in item) copy[key] = item[key]
    var id = subscriptionOf(item)
    copy.subscribed = id !== null
    copy.podcastId = id
    return copy
  }
  readonly property var chips: {
    var list = [{ id: "", name: "Top charts" }]
    if (hasIndex) list.push({ id: "trending", name: "Trending" })
    for (var i = 0; i < genres.length; i++) list.push(genres[i])
    return list
  }
  readonly property int chipIndex: {
    for (var i = 0; i < chips.length; i++) if (chips[i].id === genre) return i
    return 0
  }
  readonly property string chipName: chips[chipIndex] ? chips[chipIndex].name : "Top charts"
  readonly property var regions: [
    { value: "auto", label: "Region: system" }, { value: "US", label: "United States" }, { value: "GB", label: "United Kingdom" },
    { value: "CN", label: "China" }, { value: "TW", label: "Taiwan" }, { value: "HK", label: "Hong Kong" }, { value: "JP", label: "Japan" },
    { value: "KR", label: "Korea" }, { value: "DE", label: "Germany" }, { value: "FR", label: "France" }, { value: "ES", label: "Spain" },
    { value: "IT", label: "Italy" }, { value: "BR", label: "Brazil" }, { value: "MX", label: "Mexico" }, { value: "IN", label: "India" },
    { value: "AU", label: "Australia" }, { value: "CA", label: "Canada" }, { value: "NL", label: "Netherlands" }, { value: "SE", label: "Sweden" },
  ]
  readonly property string statusLine: {
    if (searchMode) {
      if (searchError !== "") return searchError
      if (searching) return "Searching…"
      if (searchResults.length === 0) return "No podcasts match “" + query.trim() + "”"
      return searchResults.length + " results from " + (searchProvider === "podcastindex" ? "Podcast Index" : "Apple's catalogue")
    }
    if (isUrl) return "Press Enter to read that feed"
    if (chartsError !== "") return chartsError
    if (chartsLoading && chartResults.length === 0) return "Loading " + chipName.toLowerCase() + "…"
    var where = region === "auto" ? "your region" : region
    var line = genre === "trending" ? "Trending on Podcast Index" : chipName + " in " + where
    if (chartsStale) line += chartsStaleReason === "rate-limited" ? "  ·  cached; Apple is being asked too often, try again shortly" : "  ·  cached; Apple is not answering"
    return line
  }
  readonly property string noticeLine: actionError !== "" ? actionError : statusLine

  // ---- data ----------------------------------------------------------------

  function remember() {
    if (!service || restoring) return
    service.browseState = {
      genre: root.genre, region: root.region, query: root.query,
      cursor: root.cursorActive ? root.cursorIndex : -1,
      detail: browser.detailOpen && detailPane.show ? String(detailPane.show.feedUrl || "") : "",
    }
  }

  // Results just arrived: seed the subscription map from the daemon's
  // canonical match and put the cursor back where a previous visit left it.
  function settleResults(list) {
    var map = {}
    for (var key in root.subscribedByUrl) map[key] = root.subscribedByUrl[key]
    for (var i = 0; i < list.length; i++) {
      if (list[i].subscribed === true && list[i].podcastId) map[Model.canonicalFeedUrl(list[i].feedUrl)] = list[i].podcastId
    }
    root.subscribedByUrl = map
    pointerGate.reset()
    if (root.restoreCursor >= 0 && root.restoreCursor < list.length) {
      root.cursorActive = true
      root.cursorIndex = root.restoreCursor
      grid.positionViewAtIndex(root.cursorIndex, GridView.Contain)
      if (root.restoreDetail !== "" && list[root.cursorIndex].feedUrl === root.restoreDetail) openDetail(list[root.cursorIndex])
    } else {
      root.cursorIndex = 0
      syncDetail()
    }
    root.restoreCursor = -1
    root.restoreDetail = ""
  }

  function loadGenres() {
    if (!service) return
    service.genres(function(ok, result) {
      if (ok && result && Array.isArray(result.genres)) root.genres = result.genres
    })
  }

  function loadCharts(refresh) {
    if (!service) return
    var serial = ++chartSerial
    chartsLoading = true
    chartsError = ""
    var done = function(ok, result, trending) {
      if (serial !== root.chartSerial) return
      root.chartsLoading = false
      if (!ok) {
        root.chartsError = result && result.message ? String(result.message) : "Could not load the chart"
        if (result && result.code === "disconnected") root.chartsError = "The player service is not connected"
        return
      }
      root.chartResults = result && Array.isArray(result.results) ? result.results : []
      root.chartsStale = !trending && result && result.stale === true
      root.chartsStaleReason = root.chartsStale && result.reason ? String(result.reason) : ""
      if (!root.searchMode) root.settleResults(root.chartResults)
    }
    if (genre === "trending") {
      if (!hasIndex) { genre = ""; loadCharts(refresh); return }
      service.trending({}, function(ok, result) { done(ok, result, true) })
      return
    }
    var options = { limit: 50 }
    if (genre !== "") options.genre = genre
    if (region !== "auto") options.country = region
    if (refresh) options.refresh = true
    service.charts(options, function(ok, result) { done(ok, result, false) })
  }

  function runSearch() {
    var text = query.trim()
    if (!service || !searchMode) { searchResults = []; searching = false; return }
    searching = true
    searchError = ""
    var serial = ++requestSerial
    var options = {}
    if (region !== "auto") options.country = region
    service.search(text, "term", options, function(ok, result) {
      if (serial !== root.requestSerial) return
      root.searching = false
      if (!ok) { root.searchError = result && result.message ? String(result.message) : "Search failed"; return }
      root.searchResults = result && result.results ? result.results : []
      root.searchProvider = result && result.provider ? String(result.provider) : ""
      root.settleResults(root.searchResults)
    })
  }

  function setGenre(id) {
    if (id === genre) return
    genre = id
    cursorActive = false
    cursorIndex = 0
    remember()
    loadCharts(false)
    chipStrip.ensureVisible(chipIndex)
  }

  function cycleGenre(direction) {
    var next = (chipIndex + direction + chips.length) % chips.length
    setGenre(chips[next].id)
  }

  function markSubscribed(feedUrl, podcastId) {
    var map = {}
    for (var key in root.subscribedByUrl) map[key] = root.subscribedByUrl[key]
    map[Model.canonicalFeedUrl(feedUrl)] = podcastId
    root.subscribedByUrl = map
    if (detailPane.show && Model.canonicalFeedUrl(detailPane.show.feedUrl) === Model.canonicalFeedUrl(feedUrl)) detailPane.markSubscribed(podcastId)
  }

  function subscribeTo(item) {
    if (!service || !item || !item.feedUrl) return
    var known = subscriptionOf(item)
    if (known !== null) { browser.navigate("podcast", { podcastId: known }, true); return }
    var feed = String(item.feedUrl)
    root.actionError = ""
    service.subscribe(feed, function(ok, result) {
      if (!ok) {
        var message = result && result.message ? String(result.message) : "Could not subscribe"
        root.actionError = "Could not subscribe: " + message
        if (detailPane.show && detailPane.show.feedUrl === feed) detailPane.error = message
        return
      }
      root.markSubscribed(feed, result.podcast.id)
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

  // ---- detail pane ---------------------------------------------------------

  function openDetail(item) {
    if (!item) return
    if (browser.editing) leaveField()
    root.actionError = ""
    browser.detailOpen = true
    detailPane.present(decorated(item))
    detailPane.fetch()
    remember()
  }

  function previewUrl(url) {
    if (!url) return
    cursorActive = false
    pointerGate.reset()
    openDetail({ title: url, feedUrl: url, artwork: "", author: "" })
    browser.pane = "detail"
  }

  // The pane follows the cursor; the feed is read once the cursor rests.
  function syncDetail() {
    if (!browser.detailOpen) return
    if (current) { detailPane.present(decorated(current)); restTimer.restart() }
  }

  // Only genuine pointer movement moves the cursor: delegates sliding under
  // a parked pointer (scroll, model swap) send hover events too.
  function selectFromPointer(index, item, mouse) {
    if (!pointerGate.moved(item, mouse)) return
    cursorActive = true
    cursorIndex = index
    browser.pane = "main"
    syncDetail()
  }

  PointerMoveGate { id: pointerGate; referenceItem: grid }

  Timer {
    id: restTimer
    interval: 450
    onTriggered: if (browser.detailOpen && root.current && detailPane.show && detailPane.show.feedUrl === root.current.feedUrl) detailPane.fetch()
  }

  // ---- input ---------------------------------------------------------------

  function focusField() {
    browser.editing = true
    Qt.callLater(function() {
      var field = root.mode === "opml" ? opmlField : searchField
      field.forceActiveFocus()
      field.selectAll()
    })
  }

  function leaveField() {
    browser.editing = false
    browser.refocus()
  }

  function moveCursor(delta) {
    if (results.length === 0) return
    pointerGate.reset()
    if (!cursorActive) { cursorActive = true; syncDetail(); remember(); return }
    cursorIndex = Math.max(0, Math.min(results.length - 1, cursorIndex + delta))
    grid.positionViewAtIndex(cursorIndex, GridView.Contain)
    syncDetail()
    remember()
  }

  // The window was reopened on this view: the launcher contract is that
  // typing searches straight away.
  function activated() { focusField() }

  function handleEscape() {
    if (actionError !== "") actionError = ""
    if (mode !== "search") { mode = "search"; focusField(); return true }
    if (browser.detailOpen) { browser.detailOpen = false; browser.pane = "main"; remember(); return true }
    if (query !== "") { query = ""; searchField.text = ""; searchResults = []; remember(); return true }
    if (cursorActive) { cursorActive = false; focusField(); return true }
    return false
  }

  function handleKey(event) {
    if (browser.pane === "detail" && browser.detailOpen) {
      if (detailPane.handleKey(event)) return true
      if (event.text === "h" || event.key === Qt.Key_Left) { browser.pane = "main"; return true }
      // The view's own chords still work from the pane.
    }
    if (event.text === "/" || event.text === "u") { mode = "search"; focusField(); return true }
    if (event.text === "o") { mode = "opml"; focusField(); return true }
    if (event.text === "r") { if (searchMode) runSearch(); else loadCharts(true); return true }
    if (event.text === "H") { cycleGenre(-1); return true }
    if (event.text === "L") { cycleGenre(1); return true }
    if (browser.pane === "detail" && browser.detailOpen) return false
    if (results.length > 0) {
      if (event.key === Qt.Key_Down || event.text === "j") { moveCursor(columns); return true }
      if (event.key === Qt.Key_Up || event.text === "k") { if (cursorActive && cursorIndex < columns) { cursorActive = false; focusField(); return true } moveCursor(-columns); return true }
      if (event.key === Qt.Key_Right || event.text === "l") {
        if (cursorActive && browser.detailOpen && (cursorIndex % columns === columns - 1 || cursorIndex === results.length - 1)) { browser.pane = "detail"; return true }
        moveCursor(1); return true
      }
      if (event.key === Qt.Key_Left || event.text === "h") { if (cursorActive && cursorIndex % columns === 0) { browser.pane = "sidebar"; return true } moveCursor(-1); return true }
      if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
        if (!cursorActive) { cursorActive = true; syncDetail(); return true }
        if (browser.detailOpen && detailPane.show && root.current && detailPane.show.feedUrl === root.current.feedUrl) { browser.pane = "detail"; detailPane.fetch(); return true }
        openDetail(current)
        return true
      }
      if (event.text === "s" && cursorActive && current) { subscribeTo(current); return true }
    }
    if (event.text === "h" || event.key === Qt.Key_Left) { browser.pane = "sidebar"; return true }
    return false
  }

  Component.onCompleted: {
    var saved = service ? service.browseState : null
    if (saved) {
      root.restoring = true
      root.genre = saved.genre || ""
      root.region = saved.region || "auto"
      if (saved.query) { root.query = saved.query; searchField.text = saved.query }
      root.restoreCursor = saved.cursor !== undefined ? Number(saved.cursor) : -1
      root.restoreDetail = saved.detail ? String(saved.detail) : ""
      root.restoring = false
    }
    loadGenres()
    loadCharts(false)
    if (searchMode) runSearch()
    if (root.restoreCursor < 0) focusField()
  }

  onRegionChanged: { if (restoring) return; remember(); loadCharts(false); if (searchMode) runSearch() }
  onHasIndexChanged: if (!hasIndex && genre === "trending") setGenre("")

  // The daemon may still be starting when the window first opens: fetch the
  // chart once the service is there and connected.
  onServiceChanged: if (service && service.connected && chartResults.length === 0) { loadGenres(); loadCharts(false) }

  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onConnectedChanged() {
      if (!root.service.connected) return
      if (root.genres.length === 0) root.loadGenres()
      if (root.chartResults.length === 0 || root.chartsError !== "") root.loadCharts(false)
      if (root.searchMode && root.searchResults.length === 0) root.runSearch()
    }
    // Subscriptions made elsewhere (the pane, sync, OPML) show up on the
    // tiles, and unsubscribed ones lose their tick. The daemon stores the
    // post-redirect URL, so a tile's own id counts while it is still there.
    function onLibraryChanged() {
      var lib = root.service.library || []
      var ids = {}
      var map = {}
      for (var i = 0; i < lib.length; i++) {
        ids[lib[i].id] = true
        map[Model.canonicalFeedUrl(lib[i].feedUrl)] = lib[i].id
      }
      for (var key in root.subscribedByUrl) {
        if (ids[root.subscribedByUrl[key]] && map[key] === undefined) map[key] = root.subscribedByUrl[key]
      }
      root.subscribedByUrl = map
    }
  }

  Timer {
    id: debounce
    interval: 400
    onTriggered: root.runSearch()
  }

  Row {
    anchors.fill: parent
    spacing: Style.space(16)

    Item {
      id: mainPane
      width: browser.detailOpen ? parent.width - browser.detailWidth - parent.spacing : parent.width
      height: parent.height

      Column {
        anchors.fill: parent
        spacing: Style.space(12)

        ViewHeader {
          width: parent.width
          title: "Browse"
          subtitle: root.noticeLine
          foreground: browser.foreground
          fontFamily: browser.fontFamily

          Dropdown {
            showLabel: false
            foreground: browser.foreground
            fontFamily: browser.fontFamily
            options: root.regions
            value: root.region
            onChanged: function(value) { root.region = value }
            // Chosen with the mouse, the menu would keep the keyboard.
            onPopupOpenChanged: { browser.editing = popupOpen; if (!popupOpen) browser.refocus() }
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
            placeholderText: "Search podcasts, or paste a feed URL"
            foreground: browser.foreground
            onTextChanged: {
              root.query = text
              root.cursorActive = false
              root.remember()
              if (root.searchMode) debounce.restart()
              else { debounce.stop(); root.searching = false; root.searchError = "" }
            }
            onActiveFocusChanged: browser.editing = activeFocus || opmlField.activeFocus
            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) { root.leaveField(); event.accepted = true }
              else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                if (root.isUrl) { root.previewUrl(root.query.trim()); root.leaveField(); event.accepted = true; return }
                if (root.searchMode && debounce.running) { debounce.stop(); root.runSearch() }
                root.leaveField(); root.cursorActive = root.results.length > 0; root.syncDetail(); event.accepted = true
              }
              else if (event.key === Qt.Key_Down) { root.leaveField(); root.cursorActive = root.results.length > 0; root.syncDetail(); event.accepted = true }
              else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) { root.leaveField(); browser.cyclePane(event.key === Qt.Key_Backtab ? -1 : 1); event.accepted = true }
              // An empty field has no use for these; the window does.
              else if (text === "" && event.key === Qt.Key_Space && service) { service.togglePause(); event.accepted = true }
              else if (text === "" && event.text === "?") { browser.helpOpen = true; root.leaveField(); event.accepted = true }
            }
          }

          TextField {
            id: opmlField
            visible: root.mode === "opml"
            Layout.fillWidth: true
            placeholderText: "~/Downloads/subscriptions.opml"
            foreground: browser.foreground
            onActiveFocusChanged: browser.editing = activeFocus || searchField.activeFocus
            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) { root.leaveField(); root.mode = "search"; event.accepted = true }
              else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { root.importOpml(text.trim()); root.leaveField(); event.accepted = true }
            }
          }

          Button {
            foreground: browser.foreground
            fontFamily: browser.fontFamily
            fontSize: Style.font.bodySmall
            bordered: true
            selected: root.mode === "opml"
            iconText: "󰈠"
            text: root.mode === "opml" ? "Back to search" : "Import OPML"
            onClicked: { root.mode = root.mode === "opml" ? "search" : "opml"; root.focusField() }
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

        // ---------- Category strip ----------
        Flickable {
          id: chipStrip
          visible: !root.searchMode
          width: parent.width
          height: chipRow.implicitHeight
          contentWidth: chipRow.implicitWidth
          contentHeight: height
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          flickableDirection: Flickable.HorizontalFlick

          function ensureVisible(index) {
            var item = chipRepeater.itemAt(index)
            if (!item) return
            if (item.x < contentX) contentX = item.x
            else if (item.x + item.width > contentX + width) contentX = Math.max(0, item.x + item.width - width)
          }

          WheelHandler {
            onWheel: function(event) {
              var delta = event.angleDelta.y !== 0 ? event.angleDelta.y : event.angleDelta.x
              var step = delta > 0 ? -1 : 1
              chipStrip.contentX = Math.max(0, Math.min(Math.max(0, chipStrip.contentWidth - chipStrip.width), chipStrip.contentX + step * Style.space(120)))
            }
          }

          Row {
            id: chipRow
            spacing: Style.space(6)

            Repeater {
              id: chipRepeater
              model: root.chips

              Button {
                required property var modelData
                required property int index
                foreground: browser.foreground
                fontFamily: browser.fontFamily
                fontSize: Style.font.bodySmall
                bordered: true
                selected: root.genre === modelData.id
                iconText: modelData.id === "" ? "󰔵" : (modelData.id === "trending" ? "󰈸" : "")
                text: modelData.name
                onClicked: root.setGenre(modelData.id)
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
          // The cursor is ours; the view must not scroll on its own currentIndex.
          highlightFollowsCurrentItem: false
          keyNavigationEnabled: false
          boundsBehavior: Flickable.StopAtBounds
          cacheBuffer: Style.space(400)
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

          Text {
            anchors.centerIn: parent
            width: parent.width - Style.space(48)
            visible: root.results.length === 0
            textFormat: Text.PlainText
            text: {
              if (root.searchMode) return root.searching ? "Searching…" : (root.searchError !== "" ? root.searchError : "No podcasts match “" + root.query.trim() + "”.")
              if (root.chartsLoading) return "Loading " + root.chipName.toLowerCase() + "…"
              if (root.chartsError !== "") return root.chartsError + "\nPress r to try again, or search by name above."
              return "Nothing here yet."
            }
            color: browser.dim
            font.family: browser.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            renderType: Text.NativeRendering
          }

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
              readonly property var podcastId: root.subscriptionOf(modelData)
              readonly property bool subscribed: podcastId !== null
              hasCursor: browser.pane === "main" && root.cursorActive && root.cursorIndex === index
              current: browser.detailOpen && detailPane.show !== null && detailPane.show.feedUrl === modelData.feedUrl

              MouseArea {
                id: tileArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onEntered: root.selectFromPointer(index, tileArea, { x: mouseX, y: mouseY })
                onPositionChanged: function(mouse) { root.selectFromPointer(index, tileArea, mouse) }
                onClicked: { root.cursorActive = true; root.cursorIndex = index; root.openDetail(modelData) }
                onDoubleClicked: root.subscribeTo(modelData)
              }

              Column {
                anchors.top: parent.top
                anchors.topMargin: Style.space(8)
                anchors.horizontalCenter: parent.horizontalCenter
                width: Style.space(112)
                spacing: Style.space(6)

                Item {
                  width: Style.space(112)
                  height: Style.space(112)

                  RemoteArtwork {
                    anchors.fill: parent
                    size: Style.space(112)
                    service: root.service
                    remoteUrl: String(modelData.artwork || "")
                    foreground: browser.foreground
                    glyphSize: Style.space(40)
                  }

                  // Chart rank, or a tick once the show is in the library.
                  BorderSurface {
                    visible: tile.subscribed || (!root.searchMode && Number(modelData.rank) > 0)
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.margins: -Style.space(4)
                    width: Math.max(Style.space(22), badge.implicitWidth + Style.space(10))
                    height: Style.space(20)
                    radius: Style.cornerRadius > 0 ? height / 2 : 0
                    color: tile.subscribed ? Color.accent : Color.menu.background
                    borderSpec: Border.flat(tile.subscribed ? Color.menu.background : Util.alpha(browser.foreground, 0.35), Math.max(1, Style.space(1)))

                    Text {
                      id: badge
                      anchors.centerIn: parent
                      textFormat: Text.PlainText
                      text: tile.subscribed ? "󰄬" : "#" + modelData.rank
                      color: tile.subscribed ? Color.menu.background : browser.foreground
                      font.family: browser.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: true
                      renderType: Text.NativeRendering
                    }
                  }
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
                  text: tile.subscribed ? "Subscribed" : String(modelData.author || "")
                  color: tile.subscribed ? Color.accent : browser.dim
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

    ShowPreview {
      id: detailPane
      visible: browser.detailOpen
      width: browser.detailWidth
      height: parent.height
      browser: root.browser
      active: browser.pane === "detail"
      onSubscribedShow: function(result) { if (result && result.podcast && detailPane.show) root.markSubscribed(String(detailPane.show.feedUrl), result.podcast.id) }
      onOpenRequested: function(podcastId) { if (podcastId > 0) browser.navigate("podcast", { podcastId: podcastId }, true) }
    }
  }
}
