import QtQuick
import qs.Commons

// Artwork for an image that only exists as a remote URL (search results,
// feed previews): the daemon fetches, sniffs and re-encodes it into a
// bounded thumbnail cache and hands back a local path, which is the only
// thing the Image element ever loads. Until then, the glyph shows.
Artwork {
  id: root

  property var service: null
  property string remoteUrl: ""
  property string resolved: ""

  source: resolved

  function resolve() {
    var wanted = String(root.remoteUrl || "")
    root.resolved = ""
    if (wanted === "" || !root.service || typeof root.service.thumbnail !== "function") return
    root.service.thumbnail(wanted, function(path) {
      // Delegates are recycled: only accept the answer for the URL still shown.
      if (String(root.remoteUrl || "") === wanted) root.resolved = path ? String(path) : ""
    })
  }

  onRemoteUrlChanged: resolve()
  onServiceChanged: resolve()
  Component.onCompleted: resolve()
}
