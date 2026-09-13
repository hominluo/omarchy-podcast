import QtQuick
import qs.Commons
import qs.Ui
import ".."

// Preferences and accounts. (Filled in with the settings milestone.)
Item {
  id: root
  required property var browser
  readonly property var service: browser ? browser.service : null

  function handleKey(event) { return false }
  function handleEscape() { return false }

  ViewHeader {
    id: header
    width: parent.width
    title: "Settings"
    subtitle: "Most options live in the bar widget's settings for now (right-click the bar › Podcast)."
    foreground: browser.foreground
    fontFamily: browser.fontFamily
  }
}
