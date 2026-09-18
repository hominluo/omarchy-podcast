"""OPML import and export: the interchange format every podcast app speaks."""

import os
import stat
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

from . import feeds, fsio, log, protocol

LOG = log.get("opml")
A = protocol.Arg


def parse(data):
    """Feed URLs (with titles) from an OPML document, in document order."""
    feeds.reject_doctype(data)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise protocol.ProtocolError(protocol.BAD_REQUEST, "not an OPML file: %s" % error)
    found = []
    seen = set()
    for outline in root.iter():
        if outline.tag.split("}")[-1].lower() != "outline":
            continue
        url = ""
        for key, value in outline.attrib.items():
            if key.lower() in ("xmlurl", "url") and value and value.strip().lower().startswith(("http://", "https://")):
                url = value.strip()
                break
        if not url or url in seen:
            continue
        seen.add(url)
        title = outline.attrib.get("text") or outline.attrib.get("title") or ""
        found.append({"url": url, "title": title.strip()})
    return found


def render(podcasts):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        "    <title>Omarchy-Podcast subscriptions</title>",
        "    <dateCreated>%s</dateCreated>" % time.strftime("%a, %d %b %Y %H:%M:%S %z"),
        "  </head>",
        "  <body>",
    ]
    for podcast in podcasts:
        lines.append("    <outline type=\"rss\" text=%s title=%s xmlUrl=%s%s/>" % (
            quoteattr(podcast.get("title") or podcast.get("feedUrl", "")),
            quoteattr(podcast.get("title") or podcast.get("feedUrl", "")),
            quoteattr(podcast.get("feedUrl", "")),
            (" htmlUrl=%s" % quoteattr(podcast["link"])) if podcast.get("link") else ""))
    lines.extend(["  </body>", "</opml>", ""])
    return "\n".join(lines)


MAX_IMPORT_BYTES = 20 * 1024 * 1024


async def import_file(engine, path):
    path = os.path.expanduser(str(path or "").strip())
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOCTTY)
    except OSError as error:
        raise protocol.ProtocolError(protocol.NOT_FOUND, "cannot read %s: %s" % (path, error.strerror))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise protocol.ProtocolError(protocol.BAD_REQUEST, "%s is not a regular file" % path)
        data = handle.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise protocol.ProtocolError(protocol.BAD_REQUEST, "%s is larger than %d MB" % (path, MAX_IMPORT_BYTES // (1024 * 1024)))
    entries = parse(data)
    added, skipped, failed = [], [], []
    for entry in entries:
        if engine.store.one("SELECT id FROM podcasts WHERE feed_url = ?", (entry["url"],)) is not None:
            skipped.append(entry["url"])
            continue
        try:
            podcast = await engine.library.subscribe(entry["url"])
            added.append(podcast["title"])
        except protocol.ProtocolError as error:
            failed.append({"url": entry["url"], "error": error.message})
    LOG.info("opml import: %d added, %d skipped, %d failed", len(added), len(skipped), len(failed))
    return {"added": added, "skipped": skipped, "failed": failed, "total": len(entries)}


def export_file(engine, path=None):
    """Write the subscriptions somewhere under the user's home. The path is
    the client's choice, so it is confined: any process running as this
    user can talk to the daemon, and an export must not become a way to
    overwrite an arbitrary file or to write through a planted link."""
    if not path:
        path = os.path.join(engine.settings.download_dir, "subscriptions.opml")
    path = os.path.expanduser(str(path))
    if os.path.islink(path):
        raise protocol.ProtocolError(protocol.BAD_REQUEST, "refusing to write through a symlink")
    home = os.path.realpath(os.path.expanduser("~"))
    folder = os.path.realpath(os.path.dirname(path) or ".")
    if folder != home and not folder.startswith(home + os.sep):
        raise protocol.ProtocolError(protocol.BAD_REQUEST, "the export path must be inside your home directory (%s)" % home)
    target = os.path.join(folder, os.path.basename(path))
    podcasts = engine.library.library_list()
    # 0600: subscription lists carry private-feed tokens.
    fsio.atomic_write(target, render(podcasts), mode=0o600)
    return {"path": target, "count": len(podcasts)}


@protocol.command("opml-import", "Subscribe to every feed in an OPML file", path=A(str))
async def cmd_opml_import(engine, client, path):
    return await import_file(engine, path)


@protocol.command("opml-export", "Write subscriptions to an OPML file", path=A(str, required=False))
def cmd_opml_export(engine, client, path):
    return export_file(engine, path)
