#!/usr/bin/env python3
"""Fetch the account from the GitHub API and write what the site is built from.

Writes _data/github.json, one page per project document into the
_project_pages collection, and copies the avatar, icons, screenshots and
document images into media/. All of it is produced inside the build and never
committed. Any failure exits non-zero, so the job stops and the last good site
stays online.

    GITHUB_TOKEN=... python3 scripts/fetch.py
    python3 scripts/fetch.py --save response.json     # also keep the API response
    python3 scripts/fetch.py --input response.json    # rebuild from a kept response
"""

import argparse
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

from PIL import Image

import docs

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "_data" / "github.json"
PAGES = ROOT / "_project_pages"
MEDIA = ROOT / "media"
LOCAL_ICONS = ROOT / "icons"

OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER") or "Tensionix"
GRAPHQL_URL = os.environ.get("GITHUB_GRAPHQL_URL") or "https://api.github.com/graphql"
RAW_URL = "https://raw.githubusercontent.com"

# The program's icon: first the SVG or PNG in the repository's Assets/ (the
# publisher carries it from the release), then the Python projects' own icon.
ICON_FOLDER = "Assets"
ICON_PATHS = ["system_core/icons/app.png"]
# The docs folder is spelled both ways; a file in both comes from the one changed last.
DOC_FOLDERS = ["docs", "Docs"]
DOC_DEPTH = 4
SCREENSHOT = "screenshot.png"

SCREENSHOT_WIDTH = 1600
THUMBNAIL_WIDTH = 800
ICON_SIZE = 256
RECENT_RELEASES = 5
# Repositories per request: with their docs trees a page of 100 runs into
# GitHub's GraphQL time limit (HTTP 502), so the same query is paged.
PAGE_SIZE = 10
LEVELS = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2, "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}


def _tree(depth):
    fields = "name type oid path"
    if depth > 1:
        fields += f" object {{ ... on Tree {{ entries {{ {_tree(depth - 1)} }} }} }}"
    return fields


def _aliases():
    lines = [f'        icon{i}: object(expression: "HEAD:{path}") {{ __typename }}' for i, path in enumerate(ICON_PATHS)]
    lines.append(f'        iconFolder: object(expression: "HEAD:{ICON_FOLDER}") {{ ... on Tree {{ entries {{ name type }} }} }}')
    lines += [
        f'        docs{i}: object(expression: "HEAD:{folder}") {{ ... on Tree {{ entries {{ {_tree(DOC_DEPTH)} }} }} }}'
        for i, folder in enumerate(DOC_FOLDERS)
    ]
    return "\n".join(lines)


def _history():
    return " ".join(
        f'docsCommit{i}: history(first: 1, path: "{folder}") {{ nodes {{ committedDate }} }}'
        for i, folder in enumerate(DOC_FOLDERS)
    )


QUERY = f"""
query($login: String!, $after: String) {{
  user(login: $login) {{
    login
    name
    bio
    url
    websiteUrl
    avatarUrl(size: 460)
    createdAt
    contributionsCollection {{
      contributionCalendar {{
        totalContributions
        weeks {{ contributionDays {{ date weekday contributionCount contributionLevel }} }}
      }}
    }}
    repositories(first: {PAGE_SIZE}, after: $after, ownerAffiliations: [OWNER], privacy: PUBLIC,
                 isFork: false, isArchived: false, orderBy: {{field: NAME, direction: ASC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        name
        description
        homepageUrl
        url
        isPrivate
        isFork
        isArchived
        defaultBranchRef {{ target {{ oid ... on Commit {{ {_history()} }} }} }}
        repositoryTopics(first: 20) {{ nodes {{ topic {{ name }} }} }}
        latestRelease {{
          tagName
          publishedAt
          releaseAssets(first: 100) {{ nodes {{ name size downloadUrl }} }}
        }}
{_aliases()}
      }}
    }}
  }}
}}
"""


class FetchError(Exception):
    pass


def request(url, data=None, headers=None, attempts=4, missing_ok=False):
    """GET (or POST when data is given) with a few retries on transient errors.
    With missing_ok, a 404 returns None instead of failing."""
    headers = {"User-Agent": f"{OWNER}.github.io build", **(headers or {})}
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            transient = error.code >= 500 or error.code == 429
            if not transient or attempt == attempts:
                raise FetchError(f"{url}: HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == attempts:
                raise FetchError(f"{url}: {error}") from error
        time.sleep(2 ** attempt)


def graphql(token, variables):
    body = json.dumps({"query": QUERY, "variables": variables}).encode()
    headers = {"Authorization": f"bearer {token}", "Content-Type": "application/json"}
    result = json.loads(request(GRAPHQL_URL, data=body, headers=headers))
    if result.get("errors"):
        messages = "; ".join(e.get("message", str(e)) for e in result["errors"])
        raise FetchError(f"GraphQL: {messages}")
    return result["data"]


def fetch_account(token):
    """The user with every page of repositories joined into one list."""
    user, repositories, after = None, [], None
    while True:
        page = graphql(token, {"login": OWNER, "after": after})["user"]
        if page is None:
            raise FetchError(f"GraphQL: user {OWNER} not found")
        user = user or page
        connection = page["repositories"]
        repositories.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            break
        after = connection["pageInfo"]["endCursor"]
    user = {key: value for key, value in user.items() if key != "repositories"}
    return {**user, "repositories": repositories}


def raw_file(repo, path, missing_ok=False):
    oid = repo["defaultBranchRef"]["target"]["oid"]
    return request(f"{RAW_URL}/{OWNER}/{repo['name']}/{oid}/{urllib.parse.quote(path)}", missing_ok=missing_ok)


def exists(repo, prefix, index):
    return (repo.get(f"{prefix}{index}") or {}).get("__typename") == "Blob"


def open_image(data, what):
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as error:  # a broken file is skipped, not fatal
        print(f"  warning: {what} is not a readable image ({error}), skipped")
        return None
    # Palette and greyscale images would be resized without filtering.
    return image if image.mode in ("RGB", "RGBA") else image.convert("RGBA")


def scaled(image, width):
    if image.width <= width:
        return image
    height = round(image.height * width / image.width)
    return image.resize((width, height), Image.LANCZOS)


# Icons and screenshots --------------------------------------------------------

def save_icon(name, data, ext, source):
    target = MEDIA / "icons" / f"{name}.{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if ext == "svg":
        if b"<svg" not in data[:4096]:
            print(f"  warning: {source} is not an SVG, skipped")
            return None
        target.write_bytes(data)
    else:
        image = open_image(data, source)
        if image is None:
            return None
        if max(image.size) > ICON_SIZE:
            image.thumbnail((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
            image.save(target, "PNG", optimize=True)
        else:
            target.write_bytes(data)
    return f"/media/icons/{target.name}"


def folder_icon(repo):
    """The icon in Assets/: app.svg or app.png, else its SVG, else its PNG."""
    entries = ((repo.get("iconFolder") or {}).get("entries")) or []
    names = sorted(
        entry["name"] for entry in entries
        if entry.get("type") == "blob" and entry["name"].lower().endswith((".svg", ".png"))
    )
    for preferred in ("app.svg", "app.png"):
        for found in names:
            if found.lower() == preferred:
                return found
    svg = [found for found in names if found.lower().endswith(".svg")]
    return (svg or names or [None])[0]


def find_icon(repo, local_icons):
    """The project's own icon first, so a new icon reaches the site with the next
    release; icons/<repo>.svg|png here only for projects that carry none; else
    none (the glyph)."""
    name = repo["name"]
    candidates = []
    found = folder_icon(repo)
    if found:
        candidates.append(f"{ICON_FOLDER}/{found}")
    candidates += [path for index, path in enumerate(ICON_PATHS) if exists(repo, "icon", index)]
    for path in candidates:
        ext = path.rsplit(".", 1)[1].lower()
        icon = save_icon(name, raw_file(repo, path), ext, f"{name}/{path}")
        if icon:
            return icon, path
    for ext in ("svg", "png"):
        local = local_icons.get(f"{name}.{ext}".lower())
        if local:
            icon = save_icon(name, local.read_bytes(), ext, f"icons/{local.name}")
            if icon:
                return icon, f"icons/{local.name}"
    return None, "glyph"


def save_screenshot(repo, path):
    name = repo["name"]
    image = open_image(raw_file(repo, path), f"{name}/{path}")
    if image is None:
        return None
    folder = MEDIA / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    full = scaled(image, SCREENSHOT_WIDTH)
    full.save(folder / f"{name}.png", "PNG", optimize=True)
    shot = {"src": f"/media/screenshots/{name}.png", "width": full.width, "height": full.height}
    # A smaller copy for the cards; the full one is loaded when enlarged.
    if full.width > THUMBNAIL_WIDTH:
        scaled(full, THUMBNAIL_WIDTH).save(folder / f"{name}.small.png", "PNG", optimize=True)
        shot.update(thumb=f"/media/screenshots/{name}.small.png", thumb_width=THUMBNAIL_WIDTH)
    return shot


# Docs -------------------------------------------------------------------------

def _entries(tree, found, repo_name):
    for entry in tree.get("entries") or []:
        if entry["type"] == "blob":
            found[entry["path"]] = entry["oid"]
        elif entry["type"] == "tree":
            if entry.get("object") is None:
                print(f"  warning: {repo_name}/{entry['path']} is nested too deep, skipped")
            else:
                _entries(entry["object"], found, repo_name)


def doc_files(repo):
    """{path inside the docs folder: {"path", "oid"}} over docs/ and Docs/.
    A file present in both comes from the folder with the more recent commit."""
    target = repo["defaultBranchRef"]["target"]
    folders = []
    for index, folder in enumerate(DOC_FOLDERS):
        tree = repo.get(f"docs{index}")
        if not tree or tree.get("entries") is None:
            continue
        nodes = (target.get(f"docsCommit{index}") or {}).get("nodes") or []
        changed = nodes[0]["committedDate"] if nodes else ""
        found = {}
        _entries(tree, found, repo["name"])
        folders.append((changed, -index, folder, found))
    files = {}
    for changed, _, folder, found in sorted(folders, reverse=True):
        for path, oid in found.items():
            files.setdefault(path.split("/", 1)[1], {"path": path, "oid": oid})
    return files


def doc_image_copier(repo):
    def copy(path):
        target = MEDIA / "docs" / repo["name"] / path
        url = "/" + urllib.parse.quote(f"media/docs/{repo['name']}/{path}")
        if target.exists():
            return url
        data = raw_file(repo, path, missing_ok=True)
        if data is None:
            print(f"  warning: {repo['name']}/{path} is used by the docs but missing")
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        image = None if path.lower().endswith((".svg", ".gif")) else open_image(data, f"{repo['name']}/{path}")
        if image is not None and image.width > SCREENSHOT_WIDTH:
            scaled(image, SCREENSHOT_WIDTH).save(target)
        else:
            target.write_bytes(data)
        return url
    return copy


def write_pages(project, repo_docs):
    """The project page and one page per document and language, for Jekyll to render."""
    folder = PAGES / project["repo"]
    folder.mkdir(parents=True, exist_ok=True)
    pages = list(repo_docs.pages()) if repo_docs else []
    if not pages:
        pages = [{"doc": None, "lang": "en", "first": True, "title": project["name"],
                  "permalink": project["page"], "languages": None, "toc": [], "body": ""}]
    for index, page in enumerate(pages):
        title = project["name"] if page["first"] else f"{page['title']} · {project['name']}"
        front = {
            "layout": "project",
            "repo": project["repo"],
            "doc": page["doc"],
            "lang": page["lang"],
            "home": page["first"],
            "title": title,
            "doc_title": page["title"],
            "languages": page["languages"],
            "toc": page["toc"],
            "permalink": page["permalink"],
            "render_with_liquid": False,
        }
        text = f"---\n{json.dumps(front, ensure_ascii=False)}\n---\n{page['body']}"
        (folder / f"{index:03d}.md").write_text(text, encoding="utf-8")
    return len(pages)


def project_docs(repo):
    files = doc_files(repo)
    markdown = [rel for rel in files if rel.lower().endswith(".md")]
    if not markdown:
        return files, None
    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = dict(zip(markdown, pool.map(lambda rel: raw_file(repo, files[rel]["path"]).decode("utf-8", "replace"), markdown)))
    return files, docs.Project(OWNER, repo["name"], files, texts, doc_image_copier(repo))


# Releases ---------------------------------------------------------------------

FULL = re.compile(r"_full(?=_|$)", re.IGNORECASE)
VERSION = re.compile(r"_v\d[^_]*(?=_|$)", re.IGNORECASE)


def zip_stem(filename):
    return re.sub(r"\.zip$", "", filename, flags=re.IGNORECASE)


def display_name(filename):
    """Audion_Office_OCR_AI_v1.8.2.zip -> Audion Office OCR AI"""
    stem = VERSION.sub("", FULL.sub("", zip_stem(filename)))
    return " ".join(stem.replace("_", " ").split())


def human_size(size):
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.0f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.2f} GB"


def downloads(release):
    zips = [
        asset
        for asset in ((release or {}).get("releaseAssets") or {}).get("nodes", [])
        if asset["name"].lower().endswith(".zip")
    ]
    items = []
    for asset in zips:
        full = len(zips) > 1 and bool(FULL.search(zip_stem(asset["name"])))
        items.append({
            "label": "Download Full" if full else "Download",
            "file": asset["name"],
            "size": asset["size"],
            "size_label": human_size(asset["size"]),
            "url": asset["downloadUrl"],
        })
    items.sort(key=lambda item: item["label"] != "Download")
    return items


def day_label(day):
    return f"{day.day} {day:%b %Y}"


def host(url):
    netloc = urllib.parse.urlsplit(url).netloc
    return netloc[4:] if netloc.startswith("www.") else netloc


def web_url(url):
    url = (url or "").strip()
    if url and "://" not in url:
        url = f"https://{url}"
    return url if url.startswith(("https://", "http://")) else None


# Projects ---------------------------------------------------------------------

def project(repo, local_icons):
    name = repo["name"]
    release = repo.get("latestRelease")
    files = downloads(release)
    plain = [item for item in files if item["label"] == "Download"]
    title = display_name((plain or files)[0]["file"]) if files else ""
    icon, icon_source = find_icon(repo, local_icons)
    doc_tree, repo_docs = project_docs(repo)
    screenshot_path = doc_tree.get(SCREENSHOT, {}).get("path")
    screenshot = save_screenshot(repo, screenshot_path) if screenshot_path else None
    homepage = web_url(repo.get("homepageUrl"))

    version = None
    if release:
        published = release.get("publishedAt")
        released = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
        version = {
            "tag": release["tagName"],
            "published": published or "",
            "date": released.date().isoformat() if released else None,
            "date_label": day_label(released) if released else None,
        }

    item = {
        "repo": name,
        "name": title or name,
        "page": f"/projects/{name}/",
        "description": (repo.get("description") or "").strip(),
        "topics": [node["topic"]["name"] for node in repo["repositoryTopics"]["nodes"]],
        "homepage": homepage,
        "homepage_label": host(homepage) if homepage else None,
        "url": repo["url"],
        "icon": icon,
        "screenshot": screenshot,
        "release": version,
        "downloads": files,
        "docs": repo_docs.sidebar() if repo_docs else [],
        "doc_count": len(repo_docs.docs) if repo_docs else 0,
    }
    count = write_pages(item, repo_docs)
    print(
        f"{name}: {item['name']} | {version['tag'] if version else 'no release'} | {len(files)} zip | "
        f"icon {icon_source} | screenshot {screenshot_path or 'none'} | "
        f"{len(repo_docs.docs) if repo_docs else 0} docs, {count} pages"
    )
    return item


def calendar(collection):
    """The contribution calendar as cells and labels for an inline SVG."""
    source = collection["contributionCalendar"]
    step, cell, left, top = 13, 10, 30, 16
    cells, starts = [], []
    weeks = source["weeks"]
    for column, week in enumerate(weeks):
        days = [date.fromisoformat(day["date"]) for day in week["contributionDays"]]
        for when, day in zip(days, week["contributionDays"]):
            count = day["contributionCount"]
            words = "No contributions" if not count else f"{count} contribution{'s' if count != 1 else ''}"
            cells.append({
                "x": left + column * step,
                "y": top + day["weekday"] * step,
                "level": LEVELS.get(day["contributionLevel"], 0),
                "label": f"{words} on {day_label(when)}",
            })
        first_of_month = next((when for when in days if when.day == 1), None)
        if first_of_month and column < len(weeks) - 2:
            starts.append((column, first_of_month))
    # The month the calendar opens in is named too when its label has room.
    if weeks and (not starts or starts[0][0] >= 3):
        starts.insert(0, (0, date.fromisoformat(weeks[0]["contributionDays"][0]["date"])))
    total = source["totalContributions"]
    return {
        "total": f"{total:,}",
        "caption": f"{total:,} contribution{'s' if total != 1 else ''} in the last year",
        "cells": cells,
        "months": [{"x": left + column * step, "label": f"{when:%b}"} for column, when in starts],
        "days": [{"y": top + weekday * step + cell - 1, "label": label}
                 for weekday, label in ((1, "Mon"), (3, "Wed"), (5, "Fri"))],
        "cell": cell,
        "width": left + len(weeks) * step - (step - cell),
        "height": top + 7 * step - (step - cell),
    }


def save_avatar(url):
    data = request(url)
    try:
        kind = Image.open(io.BytesIO(data)).format
    except Exception as error:
        raise FetchError(f"avatar is not an image ({error})") from error
    ext = {"JPEG": "jpg"}.get(kind, (kind or "png").lower())
    target = MEDIA / f"avatar.{ext}"
    target.write_bytes(data)
    return f"/media/{target.name}"


def build(account):
    excluded = {OWNER.lower(), f"{OWNER.lower()}.github.io"}
    repos = [
        repo
        for repo in account["repositories"]
        if not (repo["isPrivate"] or repo["isFork"] or repo["isArchived"])
        and repo["name"].lower() not in excluded
        and repo.get("defaultBranchRef")
    ]
    if not repos:
        raise FetchError("the API returned no repositories")

    for generated in (MEDIA, PAGES):
        if generated.exists():
            shutil.rmtree(generated)
        generated.mkdir(parents=True)
    local_icons = {path.name.lower(): path for path in LOCAL_ICONS.glob("*") if path.is_file()}

    projects = [project(repo, local_icons) for repo in repos]
    # Latest release first; projects without a release follow, by name.
    projects.sort(key=lambda p: p["name"].lower())
    projects.sort(key=lambda p: (p["release"] or {}).get("published", ""), reverse=True)

    website = web_url(account.get("websiteUrl"))
    contributions = calendar(account["contributionsCollection"])
    profile = {
        "login": account["login"],
        "name": (account.get("name") or "").strip() or account["login"],
        "bio": (account.get("bio") or "").strip(),
        "url": account["url"],
        "url_label": f"{host(account['url'])}/{account['login']}",
        "website": website,
        "website_label": host(website) if website else None,
        "avatar": save_avatar(account["avatarUrl"]),
        "since": account["createdAt"][:4],
    }
    return {
        "profile": profile,
        "contributions": contributions,
        # Each document once, whatever languages it comes in.
        "documents": f"{sum(p['doc_count'] for p in projects):,}",
        "recent": [
            {key: p[key] for key in ("repo", "name", "page", "icon", "release")}
            for p in projects if p["release"]
        ][:RECENT_RELEASES],
        "projects": projects,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, help="read a response kept with --save instead of querying")
    parser.add_argument("--save", type=Path, help="also write the API response to this file")
    args = parser.parse_args()

    try:
        if args.input:
            account = json.loads(args.input.read_text(encoding="utf-8"))
        else:
            token = os.environ.get("GITHUB_TOKEN")
            if not token:
                raise FetchError("GITHUB_TOKEN is not set")
            account = fetch_account(token)
        if args.save:
            args.save.write_text(json.dumps(account, indent=2, ensure_ascii=False), encoding="utf-8")
        site = build(account)
    except FetchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(site, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(site['projects'])} projects written to {DATA_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
