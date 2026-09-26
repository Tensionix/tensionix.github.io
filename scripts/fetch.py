#!/usr/bin/env python3
"""Fetch the account from the GitHub API and write the data the site is built from.

Writes _data/github.json and copies the avatar, icons and screenshots into
media/. Both are produced inside the build and never committed. Any failure
exits non-zero, so the job stops and the last good site stays online.

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
from datetime import datetime
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "_data" / "github.json"
MEDIA = ROOT / "media"
LOCAL_ICONS = ROOT / "icons"

OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER") or "Tensionix"
GRAPHQL_URL = os.environ.get("GITHUB_GRAPHQL_URL") or "https://api.github.com/graphql"
RAW_URL = "https://raw.githubusercontent.com"

# Looked up in each project repository, first found wins.
ICON_PATHS = ["Assets/app.svg", "system_core/icons/app.png"]
SCREENSHOT_PATHS = ["docs/screenshot.png", "Docs/screenshot.png"]

SCREENSHOT_WIDTH = 1600
THUMBNAIL_WIDTH = 800
ICON_SIZE = 256


def _objects(prefix, paths):
    return "\n".join(
        f'        {prefix}{i}: object(expression: "HEAD:{path}") {{ __typename }}'
        for i, path in enumerate(paths)
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
    repositories(first: 100, after: $after, ownerAffiliations: [OWNER], privacy: PUBLIC,
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
        defaultBranchRef {{ target {{ oid }} }}
        repositoryTopics(first: 20) {{ nodes {{ topic {{ name }} }} }}
        latestRelease {{
          tagName
          publishedAt
          releaseAssets(first: 100) {{ nodes {{ name size downloadUrl }} }}
        }}
{_objects("icon", ICON_PATHS)}
{_objects("screenshot", SCREENSHOT_PATHS)}
      }}
    }}
  }}
}}
"""


class FetchError(Exception):
    pass


def request(url, data=None, headers=None, attempts=4):
    """GET (or POST when data is given) with a few retries on transient errors."""
    headers = {"User-Agent": f"{OWNER}.github.io build", **(headers or {})}
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
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


def raw_file(repo, path):
    oid = repo["defaultBranchRef"]["target"]["oid"]
    return request(f"{RAW_URL}/{OWNER}/{repo['name']}/{oid}/{urllib.parse.quote(path)}")


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


def find_icon(repo, local_icons):
    """icons/<repo>.svg|png here, then the project's own files, else none (the glyph)."""
    name = repo["name"]
    for ext in ("svg", "png"):
        local = local_icons.get(f"{name}.{ext}".lower())
        if local:
            icon = save_icon(name, local.read_bytes(), ext, f"icons/{local.name}")
            if icon:
                return icon, f"icons/{local.name}"
    for index, path in enumerate(ICON_PATHS):
        if exists(repo, "icon", index):
            ext = path.rsplit(".", 1)[1].lower()
            icon = save_icon(name, raw_file(repo, path), ext, f"{name}/{path}")
            if icon:
                return icon, path
    return None, "glyph"


def scaled(image, width):
    if image.width <= width:
        return image
    height = round(image.height * width / image.width)
    return image.resize((width, height), Image.LANCZOS)


def find_screenshot(repo):
    name = repo["name"]
    for index, path in enumerate(SCREENSHOT_PATHS):
        if not exists(repo, "screenshot", index):
            continue
        image = open_image(raw_file(repo, path), f"{name}/{path}")
        if image is None:
            continue
        folder = MEDIA / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        full = scaled(image, SCREENSHOT_WIDTH)
        full.save(folder / f"{name}.png", "PNG", optimize=True)
        shot = {"src": f"/media/screenshots/{name}.png", "width": full.width, "height": full.height}
        # A smaller copy for the card; the full one is loaded when enlarged.
        if full.width > THUMBNAIL_WIDTH:
            scaled(full, THUMBNAIL_WIDTH).save(folder / f"{name}.small.png", "PNG", optimize=True)
            shot.update(thumb=f"/media/screenshots/{name}.small.png", thumb_width=THUMBNAIL_WIDTH)
        return shot, path
    return None, None


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


def host(url):
    netloc = urllib.parse.urlsplit(url).netloc
    return netloc[4:] if netloc.startswith("www.") else netloc


def web_url(url):
    url = (url or "").strip()
    if url and "://" not in url:
        url = f"https://{url}"
    return url if url.startswith(("https://", "http://")) else None


def project(repo, local_icons):
    name = repo["name"]
    release = repo.get("latestRelease")
    files = downloads(release)
    plain = [item for item in files if item["label"] == "Download"]
    title = display_name((plain or files)[0]["file"]) if files else ""
    icon, icon_source = find_icon(repo, local_icons)
    screenshot, screenshot_source = find_screenshot(repo)
    homepage = web_url(repo.get("homepageUrl"))

    version = None
    if release:
        published = release.get("publishedAt")
        date = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
        version = {
            "tag": release["tagName"],
            "published": published or "",
            "date": date.date().isoformat() if date else None,
            "date_label": f"{date.day} {date:%b %Y}" if date else None,
        }

    print(
        f"{name}: {title or '(repository name)'} | "
        f"{version['tag'] if version else 'no release'} | {len(files)} zip | "
        f"icon {icon_source} | screenshot {screenshot_source or 'none'}"
    )
    return {
        "repo": name,
        "name": title or name,
        "description": (repo.get("description") or "").strip(),
        "topics": [node["topic"]["name"] for node in repo["repositoryTopics"]["nodes"]],
        "homepage": homepage,
        "homepage_label": host(homepage) if homepage else None,
        "url": repo["url"],
        "icon": icon,
        "screenshot": screenshot,
        "release": version,
        "downloads": files,
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

    if MEDIA.exists():
        shutil.rmtree(MEDIA)
    MEDIA.mkdir(parents=True)
    local_icons = {path.name.lower(): path for path in LOCAL_ICONS.glob("*") if path.is_file()}

    projects = [project(repo, local_icons) for repo in repos]
    # Latest release first; projects without a release follow, by name.
    projects.sort(key=lambda p: p["name"].lower())
    projects.sort(key=lambda p: (p["release"] or {}).get("published", ""), reverse=True)

    website = web_url(account.get("websiteUrl"))
    profile = {
        "login": account["login"],
        "name": (account.get("name") or "").strip() or account["login"],
        "bio": (account.get("bio") or "").strip(),
        "url": account["url"],
        "url_label": f"{host(account['url'])}/{account['login']}",
        "website": website,
        "website_label": host(website) if website else None,
        "avatar": save_avatar(account["avatarUrl"]),
    }
    return {"profile": profile, "projects": projects}


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
