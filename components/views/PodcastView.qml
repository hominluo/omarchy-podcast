import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import ".."
import "../../Model.js" as Model

// One podcast: its description and settings, then every episode, with a
// detail pane for the one under the cursor.
Item {
  id: root
  required property var browser
  readonly property var service: browser ? browser.service : null

  readonly property int podcastId: browser && browser.viewArgs ? Number(browser.viewArgs.podcastId) || 0 : 0
  readonly property var summary: {
    if (!service) return null
    var list = service.library || []
    for (var i = 0; i < list.length; i++) if (list[i].id === root.podcastId) return list[i]
    return null
  }
  property var detail: null
  property var episodes: []
  property int total: 0
  property bool loading: false
  property string filterName: "all"
  property string query: ""
  property bool searching: false
  property bool descriptionExpanded: false
  property bool confirmOpen: false

  readonly property var visibleEpisodes: Model.filterEpisodes(episodes, query)
  readonly property var podcast: detail && detail.id === podcastId ? detail : summary
  readonly property string artwork: podcast ? String(podcast.artwork || "") : ""
  readonly property var speedOptions: [
    { value: "0", label: "Default speed" }, { value: "1", label: "1×" }, { value: "1.25", label: "1.25×" },
    { value: "1.5", label: "1.5×" }, { value: "1.75", label: "1.75×" }, { value: "2", label: "2×" },
  ]
  readonly property var downloadOptions: [
    { value: "inherit", label: "Auto-download: default" }, { value: "none", label: "Auto-download: off" },
    { value: "latest", label: "Auto-download: latest" }, { value: "all", label: "Auto-download: all" },
  ]
  readonly property var queueOptions: [
    { value: "none", label: "New episodes: inbox" }, { value: "next", label: "New episodes: play next" }, { value: "last", label: "New episodes: add to Up Next" },
  ]

  function reload(reset) {
    if (!service || podcastId <= 0) return
    if (reset) { episodes = []; total = 0 }
    loading = true
    var wanted = podcastId
    service.requestEpisodes(podcastId, reset ? 0 : episodes.length, 100, filterName, "newest", function(ok, result) {
      if (wanted !== root.podcastId) return
      root.loading = false
      if (!ok || !result) return
      root.total = Number(result.total) || 0
      root.episodes = reset ? result.items : root.episodes.concat(result.items)
    })
    service.request("podcast-get", { podcastId: podcastId }, function(ok, result) {
      if (ok && result && result.podcast && wanted === root.podcastId) root.detail = result.podcast
    })
  }

  function loadMore() {
    if (loading || episodes.length >= total) return
    reload(false)
  }

  function openDetail(episode) {
    if (!episode) return
    detailPane.load(episode)
    browser.detailOpen = true
  }

  function startSearch() {
    searching = true
    browser.editing = true
    Qt.callLater(function() { searchField.forceActiveFocus(); searchField.selectAll() })
  }

  function endSearch(clear) {
    if (clear) { list.resetScroll(); query = ""; searchField.text = "" }
    searching = query !== ""
    browser.editing = false
    browser.refocus()
  }

  function setFilter(name) {
    if (name === filterName) return
    filterName = name
    reload(true)
  }

  function handleEscape() {
    if (confirmOpen) { confirmOpen = false; return true }
    if (searching || query !== "") { endSearch(true); return true }
    return false
  }

  function handleKey(event) {
    if (confirmOpen) { confirm.handleKey(event); return true }
    if (browser.pane === "detail" && browser.detailOpen && detailPane.handleKey(event)) return true
    if (event.text === "/") { startSearch(); return true }
    if (event.text === "s") { confirmOpen = true; return true }
    if (event.text === "r") { if (service) service.refresh(podcastId); return true }
    if (event.text === "u") { setFilter(filterName === "unplayed" ? "all" : "unplayed"); return true }
    if (browser.pane === "main") {
      if (list.handleKey(event)) return true
      if (event.text === "l" || event.key === Qt.Key_Right) { if (browser.detailOpen) { browser.pane = "detail"; return true } }
      if (event.text === "h" || event.key === Qt.Key_Left) { browser.pane = "sidebar"; return true }
      // Letter actions apply to the episode under the cursor.
      if (list.current && event.text !== "" && "paAdmet".indexOf(event.text) !== -1) {
        if (!detailPane.episode || detailPane.episode.id !== list.current.id) openDetail(list.current)
        return detailPane.handleKey(event)
      }
    }
    return false
  }

  Component.onCompleted: reload(true)
  onPodcastIdChanged: reload(true)

  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onEvent(name, data) {
      if (name === "episodes-changed" && data && data.podcastId === root.podcastId) root.reload(true)
      if (name === "episode" && data) {
        var next = Model.patchById(root.episodes, data)
        // patchById appends unknown ids; only keep the ones already listed.
        if (next.length === root.episodes.length) root.episodes = next
      }
      if (name === "unsubscribed" && data && data.podcastId === root.podcastId) browser.navigate("library", {}, false)
    }
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

        // ---------- Header ----------
        RowLayout {
          width: parent.width
          spacing: Style.space(14)

          Artwork {
            size: Style.space(browser.detailOpen ? 84 : 112)
            source: root.artwork
            foreground: browser.foreground
            Layout.alignment: Qt.AlignTop
          }

          Column {
            Layout.fillWidth: true
            spacing: Style.space(4)

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: root.podcast ? String(root.podcast.title || "") : "Podcast"
              color: browser.foreground
              font.family: browser.fontFamily
              font.pixelSize: Style.font.heading
              font.bold: true
              elide: Text.ElideRight
              maximumLineCount: 2
              wrapMode: Text.WordWrap
              renderType: Text.NativeRendering
            }

            Text {
              width: parent.width
              visible: text !== ""
              textFormat: Text.PlainText
              text: {
                if (!root.podcast) return ""
                var parts = []
                if (root.podcast.author) parts.push(String(root.podcast.author))
                parts.push(root.total + (root.total === 1 ? " episode" : " episodes"))
                if (root.podcast.counts && root.podcast.counts.unplayed) parts.push(root.podcast.counts.unplayed + " unplayed")
                if (root.podcast.lastError) parts.push("feed error: " + root.podcast.lastError)
                return parts.join("  ·  ")
              }
              color: root.podcast && root.podcast.lastError ? Color.urgent : browser.dim
              font.family: browser.fontFamily
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
              renderType: Text.NativeRendering
            }

            Text {
              width: parent.width
              visible: text !== ""
              textFormat: Text.PlainText
              text: root.podcast ? String(root.podcast.descriptionText || root.podcast.description || "") : ""
              color: browser.dim
              font.family: browser.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
              maximumLineCount: root.descriptionExpanded ? 40 : 2
              elide: Text.ElideRight
              renderType: Text.NativeRendering

              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.descriptionExpanded = !root.descriptionExpanded
              }
            }

            Flow {
              width: parent.width
              spacing: Style.space(6)

              Button {
                foreground: browser.foreground
                fontFamily: browser.fontFamily
                fontSize: Style.font.bodySmall
                bordered: true
                iconText: "󰑐"
                text: "Refresh"
                iconSpinning: service && service.jobs && service.jobs.refreshing === true
                tooltipText: "Fetch this feed now  (r)"
                onClicked: if (service) service.refresh(root.podcastId)
              }

              Dropdown {
                showLabel: false
                foreground: browser.foreground
                fontFamily: browser.fontFamily
                options: root.speedOptions
                value: root.podcast && root.podcast.speed ? String(root.podcast.speed) : "0"
                onChanged: function(value) { if (service) service.updatePodcast(root.podcastId, { speed: Number(value) }) }
                onPopupOpenChanged: browser.editing = popupOpen
              }

              Dropdown {
                showLabel: false
                foreground: browser.foreground
                fontFamily: browser.fontFamily
                options: root.downloadOptions
                value: root.podcast && root.podcast.autoDownload ? String(root.podcast.autoDownload) : "inherit"
                onChanged: function(value) { if (service) service.updatePodcast(root.podcastId, { autoDownload: value }) }
                onPopupOpenChanged: browser.editing = popupOpen
              }

              Dropdown {
                showLabel: false
                foreground: browser.foreground
                fontFamily: browser.fontFamily
                options: root.queueOptions
                value: root.podcast && root.podcast.autoQueue ? String(root.podcast.autoQueue) : "none"
                onChanged: function(value) { if (service) service.updatePodcast(root.podcastId, { autoQueue: value }) }
                onPopupOpenChanged: browser.editing = popupOpen
              }

              Button {
                foreground: browser.foreground
                fontFamily: browser.fontFamily
                fontSize: Style.font.bodySmall
                bordered: true
                iconText: "󰩹"
                text: "Unsubscribe"
                tooltipText: "Remove this podcast  (s)"
                onClicked: root.confirmOpen = true
              }
            }
          }
        }

        // ---------- Filter row ----------
        RowLayout {
          width: parent.width
          spacing: Style.space(8)

          ButtonGroup {
            foreground: browser.foreground
            background: browser.background
            fontFamily: browser.fontFamily
            fontSize: Style.font.bodySmall
            focusable: false
            options: [
              { value: "all", label: "All" }, { value: "unplayed", label: "Unplayed" }, { value: "downloaded", label: "Downloaded" },
            ]
            value: root.filterName
            onChanged: function(value) { root.setFilter(value) }
          }

          Item { Layout.fillWidth: true }

          Text {
            visible: root.loading
            textFormat: Text.PlainText
            text: "Loading…"
            color: browser.dim
            font.family: browser.fontFamily
            font.pixelSize: Style.font.caption
            renderType: Text.NativeRendering
          }

          TextField {
            id: searchField
            visible: root.searching || root.query !== ""
            Layout.preferredWidth: Style.space(220)
            placeholderText: "Search episodes"
            foreground: browser.foreground
            onTextChanged: { list.resetScroll(); root.query = text; list.cursorIndex = 0 }
            onActiveFocusChanged: if (!activeFocus && root.searching) root.endSearch(false)
            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) { root.endSearch(true); event.accepted = true }
              else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Down) { root.endSearch(false); list.cursorActive = true; event.accepted = true }
            }
          }

          Button {
            visible: !searchField.visible
            foreground: browser.foreground
            fontFamily: browser.fontFamily
            iconText: "󰍉"
            tooltipText: "Search episodes  (/)"
            onClicked: root.startSearch()
          }
        }

        EpisodeList {
          id: list
          width: parent.width
          height: parent.height - y
          browser: root.browser
          items: root.visibleEpisodes
          showPodcast: false
          fallbackArtwork: root.artwork
          active: browser.pane === "main"
          emptyText: root.loading ? "Loading episodes…" : (root.query !== "" ? "No episode matches “" + root.query + "”" : "No episodes here.")
          onActivated: function(episode) { root.openDetail(episode); browser.pane = "main" }
          onSecondary: function(episode) { if (service) service.queueAdd(episode.id, "last") }
          onCursorMoved: function(episode) { if (browser.detailOpen) detailPane.load(episode) }
          onReachedEnd: root.loadMore()
        }
      }
    }

    EpisodeDetail {
      id: detailPane
      visible: browser.detailOpen
      width: browser.detailWidth
      height: parent.height
      browser: root.browser
      active: browser.pane === "detail"
    }
  }

  ConfirmDialog {
    id: confirm
    anchors.fill: parent
    z: 10
    opened: root.confirmOpen
    message: root.podcast ? "Unsubscribe from “" + Model.elide(root.podcast.title, 40) + "”?" : "Unsubscribe?"
    confirmText: "Unsubscribe"
    background: browser.background
    foreground: browser.foreground
    scrim: browser.scrim
    selectedBackground: browser.selectedBackground
    selectedText: browser.selectedText
    fontFamily: browser.fontFamily
    cornerRadius: browser.cornerRadius
    onCanceled: { root.confirmOpen = false; browser.refocus() }
    onConfirmed: {
      root.confirmOpen = false
      if (service) service.unsubscribe(root.podcastId, false)
      browser.refocus()
    }
  }
}
