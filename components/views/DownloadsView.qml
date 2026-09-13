import QtQuick
import qs.Commons
import qs.Ui
import ".."
import "../../Model.js" as Model

// Episodes on disk, and the ones on their way there.
Item {
  id: root
  required property var browser
  readonly property var service: browser ? browser.service : null
  readonly property var items: service ? (service.downloads || []) : []

  function handleKey(event) { return body.handleKey(event) }
  function handleEscape() { return false }

  ListWithDetail {
    id: body
    anchors.fill: parent
    browser: root.browser
    title: "Downloads"
    subtitle: root.items.length === 0 ? "Downloaded episodes play offline and can be transcribed locally."
             : root.items.length + (root.items.length === 1 ? " episode" : " episodes")
    items: root.items
    emptyText: "No downloads yet. Press d on an episode to keep it on disk."
    extraKeys: function(event) {
      if (event.text === "x" && body.current && service) {
        if (body.current.download === "done") service.deleteDownload(body.current.id)
        else service.cancelDownload(body.current.id)
        return true
      }
      return false
    }
    onActivated: function(episode) { body.openDetail(episode) }
  }
}
