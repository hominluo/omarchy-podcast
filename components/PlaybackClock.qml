import QtQuick

// A smooth position between the daemon's updates.
//
// The daemon reports `pos` a few times a second; a seek bar or a transcript
// highlight driven straight from that steps visibly. This keeps the last
// reported position and the moment it arrived, and advances it at playback
// speed on a short timer — only while `running`, so a closed panel costs
// nothing.
Item {
  id: root

  property var service: null
  property bool running: false
  property int interval: 200

  readonly property var player: service ? service.player : null
  readonly property bool playing: !!player && player.episodeId !== null && player.episodeId !== undefined && !player.paused && !player.buffering
  readonly property real speed: player && player.speed > 0 ? player.speed : 1
  readonly property real duration: player ? Number(player.duration) || 0 : 0

  property real position: 0
  property real _base: 0
  property real _baseAt: 0

  function _resync() {
    root._base = root.player ? Number(root.player.pos) || 0 : 0
    root._baseAt = Date.now()
    root.position = root._base
  }

  onPlayerChanged: _resync()
  onRunningChanged: if (running) _resync()
  Component.onCompleted: _resync()

  Timer {
    interval: root.interval
    repeat: true
    running: root.running && root.playing
    onTriggered: {
      var next = root._base + (Date.now() - root._baseAt) / 1000 * root.speed
      if (root.duration > 0) next = Math.min(next, root.duration)
      root.position = next
    }
  }
}
