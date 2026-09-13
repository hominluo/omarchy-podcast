import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import ".."
import "../../Model.js" as Model

// Preferences and accounts.
//
// Ordinary settings are the plugin's inline entry in shell.json: this view
// writes them through the shell facade and the bar widget hands the new
// values to the daemon, so there is one source of truth. Secrets (API keys,
// the sync password) never enter shell.json; they go straight to the daemon,
// which keeps them in a 0600 file of its own.
Item {
  id: root
  required property var browser
  readonly property var service: browser ? browser.service : null
  readonly property var settings: service ? service.settings : ({})
  readonly property var engine: service ? service.engine : null
  readonly property var sync: service ? service.sync : null
  property string notice: ""

  function value(key, fallback) {
    var v = settings ? settings[key] : undefined
    return v === undefined || v === null ? fallback : v
  }

  function persist(values) {
    if (!browser || !browser.shell || typeof browser.shell.updateEntryInline !== "function") { root.notice = "Settings cannot be saved from here."; return }
    var entry = { id: browser.pluginId }
    var current = root.settings || {}
    for (var key in current) if (key !== "id") entry[key] = current[key]
    for (var k in values) entry[k] = values[k]
    var changed = browser.shell.updateEntryInline(browser.pluginId, entry)
    if (changed) root.notice = "Saved."
    else if (JSON.stringify(entry) === JSON.stringify(Object.assign({ id: browser.pluginId }, current))) root.notice = "No changes."
    else root.notice = "Could not save: the Podcast widget is not on the bar."
    noticeTimer.restart()
  }

  function saveCredentials(provider, values, label) {
    if (!service) return
    service.setCredentials(provider, values, function(ok, result) {
      root.notice = ok ? label + " saved." : (result && result.message ? String(result.message) : "Could not save")
      noticeTimer.restart()
    })
  }

  function handleKey(event) {
    var maxY = Math.max(0, flick.contentHeight - flick.height)
    if (event.key === Qt.Key_Down || event.text === "j") { flick.contentY = Math.min(maxY, flick.contentY + Style.space(80)); return true }
    if (event.key === Qt.Key_Up || event.text === "k") { flick.contentY = Math.max(0, flick.contentY - Style.space(80)); return true }
    if (event.key === Qt.Key_PageDown) { flick.contentY = Math.min(maxY, flick.contentY + flick.height * 0.9); return true }
    if (event.key === Qt.Key_PageUp) { flick.contentY = Math.max(0, flick.contentY - flick.height * 0.9); return true }
    return false
  }
  function handleEscape() { return false }

  Timer { id: noticeTimer; interval: 3000; onTriggered: root.notice = "" }

  Column {
    anchors.fill: parent
    spacing: Style.space(12)

    ViewHeader {
      width: parent.width
      title: "Settings"
      subtitle: root.notice !== "" ? root.notice : "Changes save as you make them. The same options are in the bar's widget settings."
      foreground: browser.foreground
      fontFamily: browser.fontFamily

      Button {
        foreground: browser.foreground
        fontFamily: browser.fontFamily
        iconText: "󰈠"
        text: "Export OPML"
        tooltipText: "Write your subscriptions to " + String(root.value("downloadDir", "~/Music/Podcasts")) + "/subscriptions.opml"
        onClicked: if (service) service.exportOpml("", function(ok, result) {
          root.notice = ok ? "Wrote " + result.count + " subscriptions to " + result.path : (result && result.message ? String(result.message) : "Export failed")
          noticeTimer.restart()
        })
      }
    }

    Flickable {
      id: flick
      width: parent.width
      height: parent.height - y
      contentWidth: width
      contentHeight: form.implicitHeight + Style.space(20)
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

      Column {
        id: form
        width: Math.min(flick.width - Style.space(20), Style.space(720))
        spacing: Style.space(22)

        // ---------- Playback ----------
        Section {
          title: "PLAYBACK"

          Flow {
            width: parent.width
            spacing: Style.space(12)

            NumberField {
              label: "Skip back (s)"
              from: 5; to: 120; stepSize: 5
              value: Number(root.value("skipBack", 15))
              foreground: browser.foreground; fontFamily: browser.fontFamily
              onModified: function(v) { root.persist({ skipBack: v }) }
              field.onActiveFocusChanged: browser.editing = field.activeFocus
            }
            NumberField {
              label: "Skip forward (s)"
              from: 5; to: 120; stepSize: 5
              value: Number(root.value("skipForward", 30))
              foreground: browser.foreground; fontFamily: browser.fontFamily
              onModified: function(v) { root.persist({ skipForward: v }) }
              field.onActiveFocusChanged: browser.editing = field.activeFocus
            }
            NumberField {
              label: "Sleep timer default (min)"
              from: 5; to: 120; stepSize: 5
              value: Number(root.value("sleepTimerDefault", 30))
              foreground: browser.foreground; fontFamily: browser.fontFamily
              onModified: function(v) { root.persist({ sleepTimerDefault: v }) }
              field.onActiveFocusChanged: browser.editing = field.activeFocus
            }
            Dropdown {
              label: "Default speed"
              foreground: browser.foreground; fontFamily: browser.fontFamily
              options: [{ value: "1", label: "1×" }, { value: "1.25", label: "1.25×" }, { value: "1.5", label: "1.5×" }, { value: "1.75", label: "1.75×" }, { value: "2", label: "2×" }]
              value: String(Number(root.value("defaultSpeed", 1)))
              onChanged: function(v) { root.persist({ defaultSpeed: Number(v) }); if (service) service.setSpeed(Number(v)) }
              onPopupOpenChanged: browser.editing = popupOpen
            }
          }

          Toggle {
            width: parent.width
            label: "Continuous playback"
            description: "When an episode ends, play the next one in Up Next."
            checked: root.value("continuousPlayback", true) === true
            foreground: browser.foreground; fontFamily: browser.fontFamily
            onClicked: root.persist({ continuousPlayback: !checked })
          }
        }

        // ---------- Library ----------
        Section {
          title: "LIBRARY"

          Flow {
            width: parent.width
            spacing: Style.space(12)

            NumberField {
              label: "Refresh feeds every (min)"
              from: 5; to: 1440; stepSize: 5
              value: Number(root.value("refreshIntervalMin", 30))
              foreground: browser.foreground; fontFamily: browser.fontFamily
              onModified: function(v) { root.persist({ refreshIntervalMin: v }) }
              field.onActiveFocusChanged: browser.editing = field.activeFocus
            }
            NumberField {
              label: "Inbox items on subscribe"
              from: 0; to: 20; stepSize: 1
              value: Number(root.value("initialInboxCount", 1))
              foreground: browser.foreground; fontFamily: browser.fontFamily
              onModified: function(v) { root.persist({ initialInboxCount: v }) }
              field.onActiveFocusChanged: browser.editing = field.activeFocus
            }
          }

          Toggle {
            width: parent.width
            label: "Notify about new episodes"
            description: "A quiet desktop notification when a refresh finds something new."
            checked: root.value("notifyNewEpisodes", false) === true
            foreground: browser.foreground; fontFamily: browser.fontFamily
            onClicked: root.persist({ notifyNewEpisodes: !checked })
          }
        }

        // ---------- Downloads ----------
        Section {
          title: "DOWNLOADS"

          PathField {
            width: parent.width
            label: "Download folder"
            text: String(root.value("downloadDir", "~/Music/Podcasts"))
            onCommitted: function(v) { root.persist({ downloadDir: v }) }
          }

          Flow {
            width: parent.width
            spacing: Style.space(12)

            Dropdown {
              label: "Auto-download new episodes"
              foreground: browser.foreground; fontFamily: browser.fontFamily
              options: [{ value: "none", label: "Off" }, { value: "latest", label: "Newest per podcast" }, { value: "all", label: "Everything new" }]
              value: String(root.value("autoDownload", "latest"))
              onChanged: function(v) { root.persist({ autoDownload: v }) }
              onPopupOpenChanged: browser.editing = popupOpen
            }
            NumberField {
              label: "Keep per podcast"
              from: 1; to: 50; stepSize: 1
              value: Number(root.value("keepDownloads", 5))
              foreground: browser.foreground; fontFamily: browser.fontFamily
              onModified: function(v) { root.persist({ keepDownloads: v }) }
              field.onActiveFocusChanged: browser.editing = field.activeFocus
            }
            NumberField {
              label: "Delete played after (days, 0 = never)"
              from: 0; to: 365; stepSize: 1
              value: Number(root.value("deletePlayedAfterDays", 7))
              foreground: browser.foreground; fontFamily: browser.fontFamily
              onModified: function(v) { root.persist({ deletePlayedAfterDays: v }) }
              field.onActiveFocusChanged: browser.editing = field.activeFocus
            }
          }
        }

        // ---------- Transcription ----------
        Section {
          title: "TRANSCRIPTION"

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: {
              var w = root.engine ? root.engine.whisper : null
              if (!w) return ""
              if (!w.available) return "whisper.cpp is not installed. Run:  omarchy pkg add whisper-cpp ggml-vulkan"
              return "whisper.cpp ready  ·  " + (w.gpu ? "GPU (Vulkan)" : "CPU") + "  ·  model " + w.model + (w.modelPresent ? "" : " (downloads on first use)")
            }
            color: root.engine && root.engine.whisper && root.engine.whisper.available ? browser.dim : Color.urgent
            font.family: browser.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
            renderType: Text.NativeRendering
          }

          Flow {
            width: parent.width
            spacing: Style.space(12)

            Dropdown {
              label: "Transcribe locally"
              foreground: browser.foreground; fontFamily: browser.fontFamily
              options: [{ value: "off", label: "Off" }, { value: "on-demand", label: "When I ask" }, { value: "downloaded", label: "Every download" }, { value: "queued", label: "Everything in Up Next" }]
              value: String(root.value("transcriptionPolicy", "on-demand"))
              onChanged: function(v) { root.persist({ transcriptionPolicy: v }) }
              onPopupOpenChanged: browser.editing = popupOpen
            }
            Dropdown {
              label: "Device"
              foreground: browser.foreground; fontFamily: browser.fontFamily
              options: [{ value: "auto", label: "Auto" }, { value: "gpu", label: "GPU (Vulkan)" }, { value: "cpu", label: "CPU only" }]
              value: String(root.value("whisperDevice", "auto"))
              onChanged: function(v) { root.persist({ whisperDevice: v }) }
              onPopupOpenChanged: browser.editing = popupOpen
            }
            Dropdown {
              label: "Model"
              foreground: browser.foreground; fontFamily: browser.fontFamily
              options: [{ value: "auto", label: "Auto" }, { value: "large-v3-turbo", label: "large-v3-turbo (574 MB, best)" }, { value: "small", label: "small (190 MB)" }, { value: "base", label: "base (60 MB, fastest)" }]
              value: String(root.value("whisperModel", "auto"))
              onChanged: function(v) { root.persist({ whisperModel: v }) }
              onPopupOpenChanged: browser.editing = popupOpen
            }
          }
        }

        // ---------- Discovery ----------
        Section {
          title: "DISCOVERY"

          Flow {
            width: parent.width
            spacing: Style.space(12)

            Dropdown {
              label: "Search provider"
              foreground: browser.foreground; fontFamily: browser.fontFamily
              options: [{ value: "itunes", label: "Apple's catalogue (no key)" }, { value: "podcastindex", label: "Podcast Index (needs a key)" }]
              value: String(root.value("searchProvider", "itunes"))
              onChanged: function(v) { root.persist({ searchProvider: v }) }
              onPopupOpenChanged: browser.editing = popupOpen
            }
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: "Podcast Index keys are free at api.podcastindex.org. " + (root.engine && root.engine.providers && root.engine.providers.podcastindex ? "A key is on file." : "No key on file.")
            color: browser.dim
            font.family: browser.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
            renderType: Text.NativeRendering
          }

          RowLayout {
            width: parent.width
            spacing: Style.space(8)

            TextField {
              id: piKey
              Layout.fillWidth: true
              placeholderText: "Podcast Index API key"
              foreground: browser.foreground
              onActiveFocusChanged: browser.editing = activeFocus || piSecret.activeFocus
            }
            TextField {
              id: piSecret
              Layout.fillWidth: true
              placeholderText: "API secret"
              password: true
              foreground: browser.foreground
              onActiveFocusChanged: browser.editing = activeFocus || piKey.activeFocus
            }
            Button {
              foreground: browser.foreground; fontFamily: browser.fontFamily
              bordered: true
              text: "Save key"
              onClicked: { root.saveCredentials("podcastindex", { key: piKey.text.trim(), secret: piSecret.text.trim() }, "Podcast Index key"); piKey.text = ""; piSecret.text = "" }
            }
          }
        }

        // ---------- Sync ----------
        Section {
          title: "SYNC"

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: {
              if (!root.sync || root.sync.provider === "none") return "Keep subscriptions and positions in step with other apps through gpodder.net or the Nextcloud GPodder Sync app."
              var parts = []
              parts.push(root.sync.configured ? "Configured" : "Enter a username and password to finish setup")
              if (root.sync.syncing) parts.push("syncing…")
              else if (root.sync.lastSyncAt) parts.push("last sync " + Model.formatRelativeDate(root.sync.lastSyncAt).toLowerCase())
              if (root.sync.pending) parts.push(root.sync.pending + " pending")
              if (root.sync.lastError) parts.push("error: " + root.sync.lastError)
              return parts.join("  ·  ")
            }
            color: root.sync && root.sync.lastError ? Color.urgent : browser.dim
            font.family: browser.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
            renderType: Text.NativeRendering
          }

          Flow {
            width: parent.width
            spacing: Style.space(12)

            Dropdown {
              label: "Service"
              foreground: browser.foreground; fontFamily: browser.fontFamily
              options: [{ value: "none", label: "Off" }, { value: "gpodder", label: "gpodder.net" }, { value: "nextcloud", label: "Nextcloud GPodder Sync" }]
              value: String(root.value("syncProvider", "none"))
              onChanged: function(v) {
                var changes = { syncProvider: v }
                if (v === "gpodder" && String(root.value("syncServer", "")) === "") changes.syncServer = "https://gpodder.net"
                root.persist(changes)
              }
              onPopupOpenChanged: browser.editing = popupOpen
            }
            NumberField {
              label: "Sync every (min)"
              from: 1; to: 1440; stepSize: 1
              value: Number(root.value("syncIntervalMin", 10))
              foreground: browser.foreground; fontFamily: browser.fontFamily
              onModified: function(v) { root.persist({ syncIntervalMin: v }) }
              field.onActiveFocusChanged: browser.editing = field.activeFocus
            }
          }

          PathField {
            width: parent.width
            label: "Server"
            text: String(root.value("syncServer", "https://gpodder.net"))
            placeholder: "https://gpodder.net or https://cloud.example.com"
            onCommitted: function(v) { root.persist({ syncServer: v }) }
          }

          RowLayout {
            width: parent.width
            spacing: Style.space(8)

            TextField {
              id: syncUser
              Layout.fillWidth: true
              placeholderText: "Username"
              text: String(root.value("syncUsername", ""))
              foreground: browser.foreground
              onActiveFocusChanged: browser.editing = activeFocus || syncPass.activeFocus || syncDevice.activeFocus
              onEditingFinished: if (text.trim() !== String(root.value("syncUsername", ""))) root.persist({ syncUsername: text.trim() })
            }
            TextField {
              id: syncPass
              Layout.fillWidth: true
              placeholderText: "Password or app password"
              password: true
              foreground: browser.foreground
              onActiveFocusChanged: browser.editing = activeFocus || syncUser.activeFocus || syncDevice.activeFocus
            }
            TextField {
              id: syncDevice
              Layout.preferredWidth: Style.space(160)
              placeholderText: "Device id (optional)"
              text: String(root.value("syncDeviceId", ""))
              foreground: browser.foreground
              onActiveFocusChanged: browser.editing = activeFocus || syncUser.activeFocus || syncPass.activeFocus
              onEditingFinished: if (text.trim() !== String(root.value("syncDeviceId", ""))) root.persist({ syncDeviceId: text.trim() })
            }
            Button {
              foreground: browser.foreground; fontFamily: browser.fontFamily
              bordered: true
              text: "Save password"
              onClicked: { root.saveCredentials("sync", { password: syncPass.text }, "Sync password"); syncPass.text = "" }
            }
            Button {
              foreground: browser.foreground; fontFamily: browser.fontFamily
              bordered: true
              iconText: "󰓦"
              text: "Sync now"
              enabled: root.sync && root.sync.configured === true
              onClicked: if (service) service.syncNow()
            }
          }
        }

        // ---------- Bar ----------
        Section {
          title: "BAR"

          Toggle {
            width: parent.width
            label: "Show the episode title in the bar"
            description: "Scrolls beside the icon while something plays."
            checked: root.value("showTitle", true) === true
            foreground: browser.foreground; fontFamily: browser.fontFamily
            onClicked: root.persist({ showTitle: !checked })
          }
          Toggle {
            width: parent.width
            label: "Progress line under the bar icon"
            description: "A two-pixel line that grows with the episode."
            checked: root.value("barProgress", false) === true
            foreground: browser.foreground; fontFamily: browser.fontFamily
            onClicked: root.persist({ barProgress: !checked })
          }
          Flow {
            width: parent.width
            spacing: Style.space(12)
            NumberField {
              label: "Title width"
              from: 0; to: 400; stepSize: 10
              value: Number(root.value("maxLabelWidth", 160))
              foreground: browser.foreground; fontFamily: browser.fontFamily
              onModified: function(v) { root.persist({ maxLabelWidth: v }) }
              field.onActiveFocusChanged: browser.editing = field.activeFocus
            }
            Dropdown {
              label: "Scroll wheel on the icon"
              foreground: browser.foreground; fontFamily: browser.fontFamily
              options: [{ value: "seek", label: "Seeks" }, { value: "none", label: "Does nothing" }]
              value: String(root.value("wheelAction", "seek"))
              onChanged: function(v) { root.persist({ wheelAction: v }) }
              onPopupOpenChanged: browser.editing = popupOpen
            }
          }
        }
      }
    }
  }

  component Section: Column {
    property string title: ""
    default property alias content: body.children
    width: parent ? parent.width : implicitWidth
    spacing: Style.space(10)

    PanelSectionHeader { text: parent.title; foreground: browser.foreground; fontFamily: browser.fontFamily }
    Column {
      id: body
      width: parent.width
      spacing: Style.space(10)
    }
    PanelSeparator { foreground: browser.foreground }
  }

  component PathField: Column {
    property string label: ""
    property alias text: field.text
    property alias placeholder: field.placeholderText
    signal committed(string value)
    spacing: Style.spacing.labelGap

    Text {
      textFormat: Text.PlainText
      text: parent.label
      color: browser.dim
      font.family: browser.fontFamily
      font.pixelSize: Style.font.caption
      renderType: Text.NativeRendering
    }
    TextField {
      id: field
      width: parent.width
      foreground: browser.foreground
      onActiveFocusChanged: browser.editing = activeFocus
      onEditingFinished: parent.committed(text.trim())
      Keys.onPressed: function(event) { if (event.key === Qt.Key_Escape) { browser.editing = false; browser.refocus(); event.accepted = true } }
    }
  }
}
