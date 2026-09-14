"""Omarchy-Podcast engine: the daemon behind the shell plugin.

`VERSION` is compared against the plugin manifest's version on every client
handshake; a mismatch makes the running daemon re-exec into the new code, so
updating the plugin folder always ends with the matching engine in charge.
`PROTOCOL` guards the wire format between Service.qml and this package.
"""

VERSION = "1.0.1"
PROTOCOL = 1
PLUGIN_ID = "io.github.hominluo.podcast"
