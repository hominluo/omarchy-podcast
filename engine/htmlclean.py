"""Show-notes sanitizer.

Feeds ship whatever HTML the host's editor produced: tracking pixels, inline
styles, scripts, iframes, the lot. The shell renders notes with Qt's rich
text, which understands a small subset of HTML and would happily fetch every
<img>. So notes are reduced to that subset — paragraphs, emphasis, lists,
headings, block quotes and links to http(s)/mailto — with everything else
either unwrapped or dropped with its contents.

`clean(html)` returns (html, text): the safe rich text and a plain-text
rendering for search and one-line previews.
"""

import html
import re
from html.parser import HTMLParser

ALLOWED = {"p", "br", "b", "i", "em", "strong", "u", "a", "ul", "ol", "li", "h3", "h4", "blockquote", "pre", "code"}
# Headings above h3 are shouty inside a panel; keep the structure, lower the size.
RENAME = {"h1": "h3", "h2": "h3", "h5": "h4", "h6": "h4", "strike": "i", "s": "i", "del": "i", "ins": "u"}
# Block-level tags whose contents are worth keeping as their own paragraph.
BLOCK_TO_P = {"div", "section", "article", "header", "footer", "main", "aside", "table", "tr", "td", "th",
              "tbody", "thead", "figure", "figcaption", "dl", "dt", "dd", "center", "summary", "details"}
# Tags whose content is discarded entirely.
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "svg", "noscript", "head", "title",
                     "video", "audio", "canvas", "template", "form", "input", "button", "select", "textarea"}
VOID = {"br", "img", "hr", "input", "meta", "link", "source", "track", "wbr"}
SAFE_SCHEMES = ("http://", "https://", "mailto:")

MAX_OUTPUT = 200 * 1024


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.text = []
        self.stack = []
        self.drop_depth = 0
        self.size = 0
        self.truncated = False

    # ---- helpers -----------------------------------------------------------

    def _emit(self, chunk):
        if self.truncated:
            return
        self.size += len(chunk)
        if self.size > MAX_OUTPUT:
            self.truncated = True
            return
        self.out.append(chunk)

    def _open(self, tag, attrs=None):
        attr_text = ""
        if attrs:
            attr_text = "".join(' %s="%s"' % (key, html.escape(value, quote=True)) for key, value in attrs)
        self._emit("<%s%s>" % (tag, attr_text))
        self.stack.append(tag)

    def _close(self, tag):
        if tag in self.stack:
            while self.stack:
                top = self.stack.pop()
                self._emit("</%s>" % top)
                if top == tag:
                    break

    # ---- parser callbacks --------------------------------------------------

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.drop_depth:
            if tag in DROP_WITH_CONTENT and tag not in VOID:
                self.drop_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            if tag not in VOID:
                self.drop_depth = 1
            return
        if tag == "img" or tag == "hr":
            # Images never render (they would fetch); a rule becomes a break.
            if tag == "hr":
                self._emit("<br>")
            return
        tag = RENAME.get(tag, tag)
        if tag == "br":
            self._emit("<br>")
            self.text.append("\n")
            return
        if tag in BLOCK_TO_P or tag == "p":
            # Nested blocks (a table cell inside a row inside a div) collapse
            # into the paragraph already open; rich text has no use for depth.
            if "p" in self.stack:
                self.stack.append("_skip")
            else:
                self._open("p")
            self.text.append("\n")
            return
        if tag not in ALLOWED:
            return  # unknown inline tag: unwrap
        if tag == "a":
            href = ""
            for key, value in attrs:
                if key.lower() == "href" and value:
                    candidate = value.strip()
                    if candidate.lower().startswith(SAFE_SCHEMES):
                        href = candidate
                    break
            if href:
                self._open("a", [("href", href)])
            else:
                self.stack.append("_a")   # unwrapped link: remember to skip the end tag
            return
        if tag in ("ul", "ol", "li", "h3", "h4", "blockquote", "pre"):
            self.text.append("\n")
        self._open(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.drop_depth:
            if tag in DROP_WITH_CONTENT and tag not in VOID:
                self.drop_depth -= 1
            return
        if tag in BLOCK_TO_P or tag == "p":
            if self.stack and self.stack[-1] == "_skip":
                self.stack.pop()
            else:
                self._close("p")
                self.text.append("\n")
            return
        tag = RENAME.get(tag, tag)
        if tag == "a":
            if self.stack and self.stack[-1] == "_a":
                self.stack.pop()
                return
            self._close("a")
            return
        if tag in ALLOWED and tag not in VOID:
            self._close(tag)
            if tag in ("li", "p", "h3", "h4"):
                self.text.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self.drop_depth:
            return
        if not data:
            return
        self._emit(html.escape(data, quote=False))
        self.text.append(data)

    def finish(self):
        while self.stack:
            top = self.stack.pop()
            if top not in ("_a", "_skip"):
                self._emit("</%s>" % top)
        return "".join(self.out), _collapse("".join(self.text))


def clean(raw):
    """Sanitize show notes. Plain text (no tags at all) is paragraphized so it
    still reads well in rich text."""
    source = str(raw or "").strip()
    if source == "":
        return "", ""
    if "<" not in source:
        text = _collapse(html.unescape(source))
        paragraphs = [html.escape(part.strip()) for part in re.split(r"\n\s*\n", html.unescape(source)) if part.strip()]
        rich = "".join("<p>%s</p>" % part.replace("\n", "<br>") for part in paragraphs)
        return rich, text
    cleaner = _Cleaner()
    try:
        cleaner.feed(source)
        cleaner.close()
    except Exception:  # noqa: BLE001 - html.parser is forgiving, but the fallback must never raise
        return html.escape(_strip_tags(source)), _collapse(_strip_tags(source))
    rich, text = cleaner.finish()
    rich = re.sub(r"(<br>\s*){3,}", "<br><br>", rich)
    rich = re.sub(r"<p>\s*</p>", "", rich)
    return rich.strip(), text


def _strip_tags(source):
    return html.unescape(re.sub(r"<[^>]+>", " ", source))


def _collapse(text):
    text = text.replace("\r", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def summary(text, limit=200):
    """One-line preview of plain notes."""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(flat) <= limit:
        return flat
    return flat[:limit - 1].rstrip() + "…"
