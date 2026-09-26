"""Turn the Markdown files of a project's docs folder into site pages.

No network here: fetch.py passes in the file contents and a function that
copies an image into the build. Jekyll (kramdown, GFM) renders the Markdown;
this module only decides which documents exist, what they are called, where
they live, and rewrites their links so nothing points into the repository.
"""

import html
import posixpath
import re
import unicodedata
import urllib.parse

LANGS = ("en", "ru")
LANG_SUFFIX = re.compile(r"_(en|ru)$", re.IGNORECASE)
FIRST_DOCS = ("readme", "user_guide")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# HTML that may stay HTML inside a document. Any other tag is shown as text:
# the guides write placeholders such as <machine>, and nothing may run script.
ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "dd", "del", "details", "div", "dl",
    "dt", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd",
    "li", "mark", "ol", "p", "pre", "q", "s", "samp", "small", "span", "strike",
    "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "tt", "u", "ul", "var",
}

# What becomes of a link: kept, reduced to its text, dropped (a missing image),
# or, for an image on another site, turned into a link so nothing is hotlinked.
KEEP, PLAIN, REMOVE, LINK_ONLY = "keep", "plain", "remove", "link"

FENCE_OPEN = re.compile(r"^[ \t]*(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.S)
# A tag-like "<name": any letter first, so <path.pow> and <время> count too;
# autolinks (<https://...>, <user@host>) and an already escaped \< stay as they are.
# An underscore right before an escaped "<" is escaped too, or kramdown reads
# it as the start of emphasis (output\<machine>_<time>).
TAG = re.compile(r"(?<!\\)(_?)<(/?)([^\W\d_][^\s/>:@]*)(?=[\s/>])")
LINK = re.compile(
    r"(?P<bang>!?)\[(?P<text>(?:\\.|[^\[\]\\]|\[(?:\\.|[^\[\]\\])*\])*)\]"
    r"\(\s*(?P<url><[^>\n]*>|[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)"
    r"(?P<title>\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?\s*\)"
)
REF_DEF = re.compile(r"^[ ]{0,3}\[(?P<label>[^\]\n]+)\]:[ \t]*(?P<url><[^>\n]*>|\S+)(?P<rest>.*)$", re.M)
HTML_LINK = re.compile(r"<(?P<tag>a|img)\b(?P<attrs>[^>]*)>", re.IGNORECASE)
HTML_ATTR = re.compile(r"\b(?P<name>href|src)\s*=\s*(?P<q>[\"'])(?P<url>.*?)(?P=q)", re.IGNORECASE)
ATX = re.compile(r"^(#{1,6})[ \t]+(.*)$")


def split_fences(text):
    """[(is_code, chunk)]: fenced code blocks are left exactly as written."""
    chunks, buf, fence = [], [], None
    for line in text.splitlines(keepends=True):
        if fence is None:
            match = FENCE_OPEN.match(line)
            if match:
                if buf:
                    chunks.append((False, "".join(buf)))
                buf, fence = [line], match.group(1)
                continue
            buf.append(line)
        else:
            buf.append(line)
            closing = line.strip()
            if len(closing) >= len(fence) and set(closing) == {fence[0]}:
                chunks.append((True, "".join(buf)))
                buf, fence = [], None
    if buf:
        chunks.append((fence is not None, "".join(buf)))
    return chunks


def prose(text, transform):
    """Apply transform to the text outside fenced code and code spans."""
    out = []
    for is_code, chunk in split_fences(text):
        if is_code:
            out.append(chunk)
            continue
        pos = 0
        for span in CODE_SPAN.finditer(chunk):
            out.append(transform(chunk[pos:span.start()]))
            out.append(span.group(0))
            pos = span.end()
        out.append(transform(chunk[pos:]))
    return "".join(out)


def prose_lines(text):
    """Lines outside fenced code, for finding headings."""
    for is_code, chunk in split_fences(text):
        if not is_code:
            yield from chunk.splitlines()


# Heading ids exactly as kramdown's GFM parser makes them (auto_id_stripping on),
# which is also how GitHub makes them: the text, lower-cased, with everything but
# letters, marks, digits, "_", "-" and spaces removed, spaces turned into "-",
# and "-1", "-2" appended to repeats.

def _word(char):
    category = unicodedata.category(char)
    return category[0] in "LM" or category in ("Nd", "Pc") or char in "- \t"


def heading_text(raw):
    """What a heading reads as: link text without URLs, no images, no tags."""
    parts, pos = [], 0
    for span in CODE_SPAN.finditer(raw):
        parts.append(_inline_text(raw[pos:span.start()]))
        parts.append(span.group(2).strip() if span.group(2).strip() else span.group(2))
        pos = span.end()
    parts.append(_inline_text(raw[pos:]))
    return "".join(parts)


def _inline_text(text):
    text = re.sub(r"!\[(?:\\.|[^\]\\])*\]\([^)]*\)", "", text)
    text = re.sub(r"\[((?:\\.|[^\]\\])*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[((?:\\.|[^\]\\])*)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"(?<!\w)(__?)(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", text)
    text = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", text)
    return html.unescape(text)


def heading_id(text, counter):
    base = "".join(c for c in text.lower() if _word(c)).replace(" ", "-").replace("\t", "-")
    counter[base] = counter.get(base, -1) + 1
    return f"{base}-{counter[base]}" if counter[base] else base


def headings(markdown):
    """[(level, text, id)] for the ATX headings kramdown will see."""
    found, counter = [], {}
    for line in prose_lines(markdown):
        match = ATX.match(line)
        if not match:
            continue
        raw = re.sub(r"[\t ]#+$", "", match.group(2).rstrip()).rstrip()
        if not raw:
            continue
        text = heading_text(raw)
        found.append((len(match.group(1)), " ".join(text.split()), heading_id(text, counter)))
    return found


def readable(name):
    """USER_GUIDE -> User Guide; words already in mixed case are kept."""
    words = [w for w in re.split(r"[_\s-]+", name) if w]
    return " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in words)


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "doc"


def language(text):
    """For a file without _EN/_RU: Russian when Cyrillic makes up a good part of the letters."""
    sample = "".join(chunk for is_code, chunk in split_fences(text) if not is_code)
    cyrillic = sum(1 for c in sample if "\u0400" <= c <= "\u04ff")
    latin = sum(1 for c in sample if c.isascii() and c.isalpha())
    return "ru" if cyrillic > 0.3 * (cyrillic + latin) else "en"


class Project:
    """The documents of one project and the pages they become."""

    def __init__(self, owner, repo, files, texts, copy_image):
        """files: {path inside the docs folder: {"path": repo path, "oid": blob id}};
        texts: {path inside the docs folder: Markdown}; copy_image(repo path) -> site URL or None."""
        self.owner, self.repo, self.copy_image = owner, repo, copy_image
        self.base_url = f"/projects/{repo}/"
        self.docs, self.by_path = [], {}
        self._collect(files, texts)

    # Which documents exist ----------------------------------------------

    def _collect(self, files, texts):
        seen_blobs, groups = {}, {}
        texts = {rel: text.lstrip("\ufeff") for rel, text in texts.items()}
        for rel in sorted(texts, key=lambda p: (p.count("/"), p.lower())):
            oid = files[rel]["oid"]
            if oid in seen_blobs:  # the same file copied into another folder
                self.by_path[rel.lower()] = seen_blobs[oid]
                continue
            folder, name = posixpath.split(rel[:-3])
            match = LANG_SUFFIX.search(name)
            base = name[:match.start()] if match else name
            lang = match.group(1).lower() if match else language(texts[rel])
            key = (folder.lower(), base.lower())
            doc = groups.get(key)
            if doc is None:
                doc = groups[key] = {"folder": folder, "base": base, "versions": {}}
                self.docs.append(doc)
            current = doc["versions"].get(lang)
            # NAME_EN.md wins over an unsuffixed NAME.md in the same language.
            if current is None or (match and not current["suffixed"]):
                doc["versions"][lang] = {"path": files[rel]["path"], "text": texts[rel], "suffixed": bool(match)}
            seen_blobs[oid] = (doc, lang)
            self.by_path[rel.lower()] = (doc, lang)

        for doc in self.docs:
            for lang, version in doc["versions"].items():
                title = next((text for level, text, _ in headings(version["text"]) if level == 1), None)
                version["title"] = title or readable(doc["base"])
            doc["langs"] = [lang for lang in LANGS if lang in doc["versions"]]
            doc["title"] = doc["versions"][doc["langs"][0]]["title"]

        def order(doc):
            first = FIRST_DOCS.index(doc["base"].lower()) if doc["base"].lower() in FIRST_DOCS else len(FIRST_DOCS)
            return (doc["folder"] != "", doc["folder"].lower(), first, doc["title"].casefold())

        self.docs.sort(key=order)
        slugs, taken = set(), set()
        for index, doc in enumerate(self.docs):
            parts = [slugify(p) for p in doc["folder"].split("/") if p] + [slugify(doc["base"])]
            slug = "/".join(parts)
            while True:
                home = self.base_url if index == 0 else f"{self.base_url}{slug}/"
                default = doc["langs"][0]
                urls = {lang: home if lang == default else f"{home}{lang}/" for lang in doc["langs"]}
                if slug not in slugs and not taken & set(urls.values()):
                    break
                slug += "-doc"  # two documents would share a page otherwise
            slugs.add(slug)
            taken.update(urls.values())
            doc["slug"], doc["urls"] = slug, urls

    # Links --------------------------------------------------------------

    def page_for(self, rel_path):
        found = self.by_path.get(rel_path.lower())
        if not found:
            return None
        doc, lang = found
        return doc["urls"].get(lang) or doc["urls"][doc["langs"][0]]

    def resolve(self, url, doc_path, image):
        """Where a link or image in the document at doc_path (a repo path) should go."""
        url = url.strip()
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1].strip()
        if not url or url.startswith("#"):
            return KEEP
        parts = urllib.parse.urlsplit(url)
        if parts.scheme or url.startswith("//"):
            repo_path = self._repo_path_of(parts)
            if repo_path is None:
                if parts.scheme not in ("http", "https", "mailto"):
                    return PLAIN
                return LINK_ONLY if image else KEEP
            if repo_path is False:
                return PLAIN
            return self._resolve_repo_path(repo_path, parts.fragment, image)
        path = urllib.parse.unquote(parts.path)
        if path.startswith("/"):
            target = posixpath.normpath(path.lstrip("/"))
        else:
            target = posixpath.normpath(posixpath.join(posixpath.dirname(doc_path), path))
        if target.startswith(".."):
            return PLAIN
        return self._resolve_repo_path(target, parts.fragment, image)

    def _repo_path_of(self, parts):
        """None: not this owner's repository; False: a deep link elsewhere in it;
        otherwise the path of the file inside this project's repository."""
        segments = [s for s in parts.path.split("/") if s]
        host = parts.netloc.lower()
        if host == "raw.githubusercontent.com" and len(segments) >= 4:
            owner, repo, rest = segments[0], segments[1], segments[3:]
        elif host in ("github.com", "www.github.com") and len(segments) >= 3:
            owner, repo = segments[0], segments[1]
            if owner.lower() != self.owner.lower():
                return None
            if segments[2] == "releases":
                return None
            if segments[2] not in ("blob", "tree", "raw") or len(segments) < 5:
                return False
            rest = segments[4:]
        else:
            return None
        if owner.lower() != self.owner.lower():
            return None
        if repo.lower() != self.repo.lower():
            return False
        return urllib.parse.unquote("/".join(rest))

    def _resolve_repo_path(self, target, fragment, image):
        folder, _, rel = target.partition("/")
        page = self.page_for(rel) if folder.lower() == "docs" else None
        if page:
            return page + (f"#{fragment}" if fragment else "")
        if posixpath.splitext(target)[1].lower() in IMAGE_EXTENSIONS:
            copied = self.copy_image(target)
            if copied:
                return copied
            return REMOVE if image else PLAIN
        return PLAIN

    # Rewriting ------------------------------------------------------------

    def rewrite(self, markdown, doc_path):
        markdown = markdown.lstrip("\ufeff")
        plain_refs = set()

        def ref_def(match):
            target = self.resolve(match.group("url"), doc_path, image=False)
            if target == KEEP:
                return match.group(0)
            if target in (PLAIN, REMOVE, LINK_ONLY):
                plain_refs.add(match.group("label").strip().lower())
                return ""
            return f"[{match.group('label')}]: {target}{match.group('rest')}"

        def inline(match):
            image = bool(match.group("bang"))
            target = self.resolve(match.group("url"), doc_path, image)
            if target == KEEP:
                return match.group(0)
            if target == REMOVE:
                return match.group("text") if not image else ""
            if target == PLAIN:
                return match.group("text")
            if target == LINK_ONLY:
                return match.group(0)[1:]
            return f"{match.group('bang')}[{match.group('text')}]({target}{match.group('title') or ''})"

        def html_link(match):
            attrs = match.group("attrs")
            for attr in HTML_ATTR.finditer(attrs):
                target = self.resolve(html.unescape(attr.group("url")), doc_path, match.group("tag").lower() == "img")
                if target == KEEP:
                    continue
                if target in (PLAIN, REMOVE, LINK_ONLY):
                    if match.group("tag").lower() == "img":
                        return ""
                    return "<a>"
                attrs = attrs.replace(attr.group(0), f'{attr.group("name")}="{html.escape(target)}"')
            return f"<{match.group('tag')}{attrs}>"

        def escape_tags(match):
            if match.group(3).lower() in ALLOWED_TAGS:
                return match.group(0)
            return ("\\_" if match.group(1) else "") + "&lt;" + match.group(0)[len(match.group(1)) + 1:]

        def transform(text):
            text = TAG.sub(escape_tags, text)
            text = REF_DEF.sub(ref_def, text)
            text = LINK.sub(inline, text)
            return HTML_LINK.sub(html_link, text)

        markdown = prose(markdown, transform)
        if plain_refs:
            def ref_use(match):
                label = (match.group("ref") or match.group("text")).strip().lower()
                return match.group("text") if label in plain_refs else match.group(0)
            markdown = prose(markdown, lambda t: re.sub(
                r"(?<!\])\[(?P<text>[^\[\]\n]+)\](?:\[(?P<ref>[^\]\n]*)\])?(?![(:])", ref_use, t))
        return markdown

    # Pages ----------------------------------------------------------------

    def pages(self):
        """One entry per document and language, ready to be written as a page."""
        for index, doc in enumerate(self.docs):
            for lang in doc["langs"]:
                version = doc["versions"][lang]
                body = self.rewrite(version["text"], version["path"])
                toc = [{"id": hid, "text": text} for level, text, hid in headings(body) if level == 2]
                yield {
                    "doc": doc["slug"],
                    "lang": lang,
                    "first": index == 0,
                    "title": version["title"],
                    "permalink": doc["urls"][lang],
                    "languages": doc["urls"] if len(doc["langs"]) > 1 else None,
                    "toc": toc,
                    "body": body,
                }

    def sidebar(self):
        """Documents grouped by sub-folder, for the sidebar."""
        groups = []
        for doc in self.docs:
            label = readable(doc["folder"].replace("/", " / ")) if doc["folder"] else ""
            if not groups or groups[-1]["label"] != label:
                groups.append({"label": label, "items": []})
            groups[-1]["items"].append({
                "slug": doc["slug"],
                "titles": {lang: doc["versions"][lang]["title"] for lang in doc["langs"]},
                "title": doc["title"],
                "langs": doc["langs"],
                "urls": doc["urls"],
                "url": doc["urls"][doc["langs"][0]],
            })
        return groups
