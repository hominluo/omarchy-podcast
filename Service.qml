import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// The one object in the shell that talks to the podcast daemon.
//
// Bar widgets exist once per monitor and the browse window is a separate
// component, so none of them may own a socket or a process. They read the
// state slices below (player, queue, library, ...) and call the command
// functions; this service keeps a single connection to `podcastd` and starts
// it when it is not running.
//
// Wire format is newline-delimited JSON (see engine/protocol.py). Every slice
// the daemon broadcasts lands in a property of the same name; requests are
// correlated by id and answered through a callback.
Item {
  id: root

  // Injected by the host.
  property string omarchyPath: ""
  property var shell: null
  property var manifest: null
  property var barWidgetRegistry: null
  property var pluginRegistry: null

  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : "io.github.hominluo.podcast"
  readonly property string pluginVersion: manifest && manifest.version ? String(manifest.version) : ""
  readonly property string runtimeDir: {
    var dir = Quickshell.env("XDG_RUNTIME_DIR")
    return dir && dir !== "" ? dir : "/tmp"
  }
  readonly property string socketPath: runtimeDir + "/omarchy-podcast/daemon.sock"
  readonly property string launcherPath: decodeURIComponent(String(Qt.resolvedUrl("podcastd.py")).replace(/^file:\/\//, ""))

  // ---- state mirrored from the daemon ------------------------------------
  property var player: ({ episodeId: null, pos: 0, duration: 0, paused: true, idle: true, buffering: false,
                          speed: 1.0, baseSpeed: 1.0, volume: 100, mute: false, chapter: -1,
                          skipSilence: false, voiceBoost: false, sleep: { mode: "off", endsAt: 0 } })
  property var nowPlaying: null
  property var queue: []
  property var library: []
  property var inbox: ({ count: 0, items: [] })
  property var downloads: []
  property var jobs: ({ refreshing: false, refreshDone: 0, refreshTotal: 0, transcribing: [], modelDownload: null })
  property var engine: ({ version: "", mpv: "starting", whisper: { available: false, gpu: false, model: "", modelPresent: false },
                          providers: { itunes: true, podcastindex: false } })
  property var config: ({})
  property var sync: ({ provider: "none", lastSyncAt: 0, lastError: "", pending: 0, syncing: false })

  // Connection: "connected" | "connecting" | "starting" (daemon being spawned).
  property string connection: "connecting"
  readonly property bool connected: connection === "connected"
  property string engineVersion: ""

  // The last notice the daemon sent, for footer lines; cleared by consumers.
  property var lastNotice: null
  signal notice(var payload)
  // Fired for events that are not state slices: episode, episodes-changed,
  // download-progress, transcript-progress.
  signal event(string name, var data)

  // Settings as the daemon should see them: this plugin's inline shell.json
  // entry over the manifest defaults. Bar widgets forward their injected
  // `settings` as well, so either path keeps the daemon current.
  property var settings: ({})

  // ---- derived helpers ---------------------------------------------------
  readonly property bool hasEpisode: !!player && player.episodeId !== null && player.episodeId !== undefined
  readonly property bool playing: hasEpisode && !player.paused
  readonly property string title: nowPlaying && nowPlaying.episode ? String(nowPlaying.episode.title || "") : ""
  readonly property string podcastTitle: nowPlaying && nowPlaying.podcast ? String(nowPlaying.podcast.title || "") : ""
  readonly property string artwork: {
    if (!nowPlaying) return ""
    if (nowPlaying.episode && nowPlaying.episode.artwork) return String(nowPlaying.episode.artwork)
    if (nowPlaying.podcast && nowPlaying.podcast.artwork) return String(nowPlaying.podcast.artwork)
    return ""
  }
  readonly property var chapters: nowPlaying && Array.isArray(nowPlaying.chapters) ? nowPlaying.chapters : []

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    if (value === undefined || value === null) {
      var defaults = manifest && manifest.barWidget ? manifest.barWidget.defaults : null
      value = defaults ? defaults[name] : undefined
    }
    return value === undefined || value === null ? fallback : value
  }

  // ---- request plumbing --------------------------------------------------
  property int _nextId: 1
  property var _pending: ({})
  property var _outbox: []
  property bool _flushScheduled: false
  property double _lastSpawn: 0
  property double _lastPong: 0
  property int _retryMs: 250

  function request(cmd, params, callback) {
    var message = {}
    if (params) for (var key in params) message[key] = params[key]
    message.cmd = cmd
    if (callback) {
      message.id = root._nextId++
      var pending = root._pending
      pending[message.id] = callback
      root._pending = pending
    }
    root._send(message)
  }

  // A second Socket.write in the same event-loop turn can be dropped, so
  // requests queue up and go out as one write on the next tick.
  function _send(message) {
    root._outbox.push(JSON.stringify(message))
    if (!root._flushScheduled) {
      root._flushScheduled = true
      Qt.callLater(root._flush)
    }
  }

  function _flush() {
    root._flushScheduled = false
    if (root._outbox.length === 0) return
    if (!socket.connected) return           // kept; sent after the next hello
    var data = root._outbox.join("\n") + "\n"
    root._outbox = []
    socket.write(data)
    socket.flush()
  }

  function _ingest(line) {
    var text = String(line || "").trim()
    if (text === "") return
    var message
    try { message = JSON.parse(text) } catch (e) { console.warn("podcast: bad line from daemon:", text.substring(0, 200)); return }
    if (message.id !== undefined && message.id !== null) {
      var callback = root._pending[message.id]
      if (callback) {
        var pending = root._pending
        delete pending[message.id]
        root._pending = pending
        try { callback(message.ok === true, message.ok === true ? message.result : message.error) }
        catch (e) { console.warn("podcast: callback failed:", e) }
      } else if (message.ok === false && message.error) {
        console.warn("podcast: daemon error:", JSON.stringify(message.error))
      }
      return
    }
    if (message.event) root._applyEvent(String(message.event), message.data)
  }

  function _applyEvent(name, data) {
    switch (name) {
      case "snapshot":
        if (!data) return
        for (var key in data) root._applySlice(key, data[key])
        return
      case "player": case "nowPlaying": case "queue": case "library": case "inbox":
      case "downloads": case "jobs": case "engine": case "config": case "sync":
        root._applySlice(name, data)
        return
      case "notice":
        root.lastNotice = data
        root.notice(data)
        return
      default:
        root.event(name, data)
    }
  }

  function _applySlice(key, value) {
    switch (key) {
      case "player": root.player = value || root.player; break
      case "nowPlaying": root.nowPlaying = value || null; break
      case "queue": root.queue = Array.isArray(value) ? value : []; break
      case "library": root.library = Array.isArray(value) ? value : []; break
      case "inbox": root.inbox = value || { count: 0, items: [] }; break
      case "downloads": root.downloads = Array.isArray(value) ? value : []; break
      case "jobs": root.jobs = value || root.jobs; break
      case "engine": root.engine = value || root.engine; break
      case "config": root.config = value || {}; break
      case "sync": root.sync = value || root.sync; break
    }
  }

  // ---- connection lifecycle ----------------------------------------------
  Socket {
    id: socket
    path: root.socketPath
    connected: false
    parser: SplitParser {
      onRead: function(line) { root._ingest(line) }
    }
    onConnectedChanged: {
      if (socket.connected) {
        root._retryMs = 250
        root.connection = "connecting"
        // Hello goes out alone so the daemon sees it first; queued requests
        // follow on the next tick, after the handshake reply.
        socket.write(JSON.stringify({ id: 0, cmd: "hello", client: "service", protocol: 1, pluginVersion: root.pluginVersion }) + "\n")
        socket.flush()
        var handshake = root._pending
        handshake[0] = function(ok, result) {
          if (!ok) { console.warn("podcast: handshake refused:", JSON.stringify(result)); socket.connected = false; return }
          root.engineVersion = result && result.engineVersion ? String(result.engineVersion) : ""
          root.connection = "connected"
          root._lastPong = Date.now()
          root.pushSettings(true)
          root._flush()
        }
        root._pending = handshake
      } else {
        root._onDisconnected()
      }
    }
    onError: function(error) { root._onDisconnected() }
  }

  function _onDisconnected() {
    if (root.connection !== "starting") root.connection = "connecting"
    root._pending = ({})
    reconnectTimer.interval = root._retryMs
    reconnectTimer.restart()
    root._retryMs = Math.min(4000, root._retryMs * 2)
  }

  function _connectOrSpawn() {
    if (socket.connected) return
    var now = Date.now()
    if (now - root._lastSpawn > 3000) {
      root._lastSpawn = now
      root.connection = "starting"
      Quickshell.execDetached(["python3", root.launcherPath, "serve"])
    }
    socket.connected = true
  }

  Timer {
    id: reconnectTimer
    interval: 250
    repeat: false
    onTriggered: root._connectOrSpawn()
  }

  // Keepalive: a daemon that stops answering gets reconnected rather than
  // leaving every surface frozen on its last state.
  Timer {
    interval: 15000
    repeat: true
    running: root.connected
    onTriggered: {
      if (Date.now() - root._lastPong > 40000) { socket.connected = false; return }
      root.request("ping", {}, function(ok) { if (ok) root._lastPong = Date.now() })
    }
  }

  Component.onCompleted: reconnectTimer.start()

  // ---- settings ----------------------------------------------------------
  function _entryFromBarConfig() {
    var config = root.shell && root.shell.barConfig ? root.shell.barConfig : null
    var layout = config && config.layout ? config.layout : null
    if (!layout) return null
    var sections = ["left", "center", "right"]
    for (var i = 0; i < sections.length; i++) {
      var entries = layout[sections[i]]
      if (!Array.isArray(entries)) continue
      for (var j = 0; j < entries.length; j++) {
        if (entries[j] && String(entries[j].id) === root.pluginId) return entries[j]
      }
    }
    return null
  }

  function _mergedSettings(entry) {
    var merged = {}
    var defaults = root.manifest && root.manifest.barWidget ? root.manifest.barWidget.defaults : null
    if (defaults) for (var key in defaults) merged[key] = defaults[key]
    if (entry) for (var k in entry) if (k !== "id") merged[k] = entry[k]
    return merged
  }

  property string _pushedSettings: ""

  // `force` resends even when nothing changed (after a reconnect).
  function pushSettings(force) {
    var next = root._mergedSettings(root._entryFromBarConfig())
    var serialized = JSON.stringify(next)
    if (serialized !== JSON.stringify(root.settings)) root.settings = next
    if (!root.connected) return
    if (!force && serialized === root._pushedSettings) return
    root._pushedSettings = serialized
    root.request("configure", { settings: next, locale: Quickshell.env("LANG") || "", pluginVersion: root.pluginVersion })
  }

  // Bar widgets call this with their injected `settings`; the entry they get
  // is the same one barConfig holds, so it is only a faster path to the same
  // value when shell.json changes.
  function configure(entrySettings) {
    Qt.callLater(function() { root.pushSettings(false) })
  }

  Connections {
    target: root.shell
    ignoreUnknownSignals: true
    function onBarConfigChanged() { root.pushSettings(false) }
  }

  // Secrets never touch shell.json.
  function setCredentials(provider, values, callback) {
    root.request("set-credentials", { provider: provider, values: values }, callback)
  }

  // ---- playback ----------------------------------------------------------
  function play(episodeId, callback) { root.request("play", { episodeId: episodeId }, callback) }
  function pause() { root.request("pause", {}) }
  function resume() { root.request("resume", {}) }
  function togglePause() { root.request("toggle", {}) }
  function stop() { root.request("stop", {}) }
  function seek(seconds) { root.request("seek", { pos: seconds, mode: "absolute" }) }
  function seekRelative(seconds) { root.request("seek", { pos: seconds, mode: "relative" }) }
  function skipBack() { root.request("skip", { direction: "back" }) }
  function skipForward() { root.request("skip", { direction: "forward" }) }
  function setSpeed(speed, podcastId) {
    var params = { speed: speed }
    if (podcastId !== undefined && podcastId !== null) params.podcastId = podcastId
    root.request("set-speed", params)
  }
  function setVolume(volume) { root.request("set-volume", { volume: volume }) }
  function next() { root.request("next", {}) }
  function previous() { root.request("previous", {}) }
  function setChapter(index) { root.request("set-chapter", { index: index }) }
  function setSleepTimer(mode, minutes) {
    var params = { mode: mode }
    if (minutes !== undefined) params.minutes = minutes
    root.request("sleep-timer", params)
  }
  function toggleSkipSilence() { root.request("set-skip-silence", { enabled: !(root.player && root.player.skipSilence) }) }
  function toggleVoiceBoost() { root.request("set-voice-boost", { enabled: !(root.player && root.player.voiceBoost) }) }
  function markPlayed(episodeIds, played) {
    root.request("mark-played", { episodeIds: Array.isArray(episodeIds) ? episodeIds : [episodeIds], played: played !== false })
  }

  // ---- queue -------------------------------------------------------------
  function queueAdd(episodeIds, where) {
    root.request("queue-add", { episodeIds: Array.isArray(episodeIds) ? episodeIds : [episodeIds], where: where || "last" })
  }
  function queueRemove(episodeIds) {
    root.request("queue-remove", { episodeIds: Array.isArray(episodeIds) ? episodeIds : [episodeIds] })
  }
  function queueMove(episodeId, index) { root.request("queue-move", { episodeId: episodeId, index: index }) }
  function queueClear() { root.request("queue-clear", {}) }

  // ---- library -----------------------------------------------------------
  function subscribe(feedUrl, callback) { root.request("subscribe", { feedUrl: feedUrl }, callback) }
  function unsubscribe(podcastId, deleteDownloads, callback) {
    root.request("unsubscribe", { podcastId: podcastId, deleteDownloads: deleteDownloads === true }, callback)
  }
  function updatePodcast(podcastId, fields, callback) {
    var params = { podcastId: podcastId }
    if (fields) for (var key in fields) params[key] = fields[key]
    root.request("podcast-update", params, callback)
  }
  function requestEpisodes(podcastId, offset, limit, filter, sort, callback) {
    root.request("episodes", { podcastId: podcastId, offset: offset || 0, limit: limit || 100,
                               filter: filter || "all", sort: sort || "newest" }, callback)
  }
  function requestEpisode(episodeId, callback) { root.request("episode-get", { episodeId: episodeId }, callback) }
  function setEpisodeState(episodeIds, state) {
    root.request("episode-set-state", { episodeIds: Array.isArray(episodeIds) ? episodeIds : [episodeIds], state: state })
  }
  function requestInbox(offset, limit, callback) { root.request("inbox", { offset: offset || 0, limit: limit || 100 }, callback) }
  function requestHistory(offset, limit, callback) { root.request("history", { offset: offset || 0, limit: limit || 100 }, callback) }
  function refresh(podcastId) {
    var params = {}
    if (podcastId !== undefined && podcastId !== null) params.podcastId = podcastId
    root.request("refresh", params)
  }
  function previewFeed(feedUrl, callback) { root.request("feed-preview", { feedUrl: feedUrl }, callback) }

  // ---- search ------------------------------------------------------------
  function search(query, kind, options, callback) {
    var params = { query: query, kind: kind || "term" }
    if (options) for (var key in options) params[key] = options[key]
    root.request("search", params, callback)
  }
  function trending(options, callback) { root.request("trending", options || {}, callback) }

  // ---- downloads ---------------------------------------------------------
  function download(episodeIds) {
    root.request("download", { episodeIds: Array.isArray(episodeIds) ? episodeIds : [episodeIds] })
  }
  function cancelDownload(episodeIds) {
    root.request("download-cancel", { episodeIds: Array.isArray(episodeIds) ? episodeIds : [episodeIds] })
  }
  function deleteDownload(episodeIds) {
    root.request("download-delete", { episodeIds: Array.isArray(episodeIds) ? episodeIds : [episodeIds] })
  }

  // ---- transcripts -------------------------------------------------------
  function requestTranscript(episodeId, granularity, callback) {
    root.request("transcript-get", { episodeId: episodeId, granularity: granularity || "paragraphs" }, callback)
  }
  function transcribe(episodeId, callback) { root.request("transcribe", { episodeId: episodeId }, callback) }
  function cancelTranscription(episodeId) { root.request("transcribe-cancel", { episodeId: episodeId }) }

  // ---- sync / opml / misc ------------------------------------------------
  function syncNow(callback) { root.request("sync-now", {}, callback) }
  function importOpml(path, callback) { root.request("opml-import", { path: path }, callback) }
  function exportOpml(path, callback) { root.request("opml-export", path ? { path: path } : {}, callback) }
  function status(callback) { root.request("status", {}, callback) }

  // Open the browse window. The overlay is this plugin's own id, which the
  // host lets a plugin summon for itself.
  function browse(view, extra) {
    if (!root.shell || typeof root.shell.summon !== "function") return false
    var payload = extra ? extra : {}
    if (view) payload.view = view
    return root.shell.summon(root.pluginId, JSON.stringify(payload))
  }

  // ---- IPC: `omarchy-shell io.github.hominluo.podcast <method>` ----------
  IpcHandler {
    target: root.pluginId

    function playPause(): void { root.togglePause() }
    function play(): void { root.resume() }
    function pause(): void { root.pause() }
    function next(): void { root.next() }
    function previous(): void { root.previous() }
    function skipBack(): void { root.skipBack() }
    function skipForward(): void { root.skipForward() }
    function browse(): void { root.browse("") }
    function refresh(): void { root.refresh() }
    function restartDaemon(): void { root.request("restart", {}) }
    function status(): string {
      return JSON.stringify({
        connection: root.connection,
        engineVersion: root.engineVersion,
        playing: root.playing,
        title: root.title,
        podcast: root.podcastTitle,
        position: root.player ? root.player.pos : 0,
        duration: root.player ? root.player.duration : 0,
        queue: root.queue.length,
      })
    }
  }
}
