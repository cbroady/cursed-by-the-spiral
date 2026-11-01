#!/usr/bin/env python3
"""One-time converter: WordPress.com export -> static archive of "Cursed by the Spiral".

Usage (from the repo root):
    python3 tools/build.py <export.xml> <media-export.tar> [header.jpg]

Writes the site into the repo root (index.html, YYYY/MM/DD/slug/, media/, assets/)
plus redirects.json, and prints a report of anything that needs a human look.
Requires Pillow. Audio re-encoding uses macOS `afconvert`.
"""
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
from datetime import datetime

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_PATH = "/cursed-by-the-spiral/"  # only used in redirects.json (for the landing page repo)
SITE_TITLE = "Cursed by the Spiral"
TAGLINE = "The Life and Times of a Snail Enthusiast"
MAX_DIM = 1600
JPEG_QUALITY = 80
PNG_TO_JPEG_BYTES = 400_000
NS = {
    "wp": "http://wordpress.org/export/1.2/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}
OWN_HOSTS = r"(?:www\.)?(?:snailedit\.xyz|snaileditxyz\.wordpress\.com|snaileditxyz\.files\.wordpress\.com)"
UPLOAD_RE = re.compile(r"https?://" + OWN_HOSTS + r"(?:/wp-content/uploads)?/(\d{4}/\d\d/[^/?#\"'\s<>]+\.[a-zA-Z0-9]{2,4})")
POST_LINK_RE = re.compile(r"https?://" + OWN_HOSTS + r"(/\d{4}/\d\d/\d\d/[^/?#\"]+/?)")
ATTACH_LINK_RE = re.compile(r"https?://" + OWN_HOSTS + r"/([a-z0-9_-]+)/?$")
# Dead hotlinked images that Craig supplied replacements for (in tools/recovered/).
RECOVERED = {
    "https://pbs.twimg.com/media/ES3VTrgUMAAyEFp?format=jpg&name=small": "cattening-the-curve.jpg",
}
OUTPUT_DIRS = ["2019", "2020", "2021", "media", "assets"]

report = []


def note(msg):
    report.append(msg)


# ---------------------------------------------------------------- export parsing

def load_export(path):
    channel = ET.parse(path).getroot().find("channel")
    posts, attachments = [], {}
    for it in channel.findall("item"):
        ptype = it.find("wp:post_type", NS).text
        status = it.find("wp:status", NS).text
        if ptype == "attachment":
            url = it.find("wp:attachment_url", NS).text or ""
            m = UPLOAD_RE.match(url)
            attachments[it.find("wp:post_name", NS).text] = {
                "file": m.group(1) if m else None,
                "parent": it.find("wp:post_parent", NS).text,
            }
        elif ptype == "post" and status == "publish":
            link = it.find("link").text
            path = re.sub(r"^https?://[^/]+", "", link)
            assert re.match(r"^/\d{4}/\d\d/\d\d/[a-z0-9-]+/$", path), path
            posts.append({
                "id": it.find("wp:post_id", NS).text,
                "title": html.unescape(it.find("title").text or "Untitled"),
                "date": datetime.strptime(it.find("wp:post_date", NS).text, "%Y-%m-%d %H:%M:%S"),
                "path": path.strip("/") + "/",
                "body": it.find("content:encoded", NS).text or "",
            })
    posts.sort(key=lambda p: p["date"])
    return posts, attachments


# ---------------------------------------------------------------- media

class Media:
    def __init__(self, tar_path):
        self.tar = tarfile.open(tar_path)
        self.names = set(self.tar.getnames())
        self.used = {}  # source name in tar -> output path under media/

    def resolve(self, rel):
        """Map an uploads path (YYYY/MM/file) to a file that exists in the tar."""
        rel = rel.lower()
        if rel in self.names:
            return rel
        stem, ext = os.path.splitext(rel)
        edited = sorted(n for n in self.names if re.fullmatch(re.escape(stem) + r"-e\d+" + re.escape(ext), n))
        return edited[0] if edited else None

    def local(self, url):
        """Return the media/ path for one of our own upload URLs, or None."""
        m = UPLOAD_RE.match(html.unescape(url))
        if not m:
            return None
        src = self.resolve(m.group(1))
        if not src:
            note(f"MISSING upload not in media export: {url}")
            return None
        if src not in self.used:
            self.used[src] = self.write(src)
        return self.used[src]

    def write(self, src):
        """Process one file from the tar into media/ (skipped if already there)."""
        stem, ext = os.path.splitext(src)
        ext = ext.lower()
        if ext == ".mp3":
            candidates = [stem + ".m4a"]
        elif ext == ".png":
            candidates = [src, stem + ".jpg"]  # big opaque PNGs are photos; stored as JPEG
        else:
            candidates = [src]
        for c in candidates:
            if os.path.exists(os.path.join(ROOT, "media", c)):
                return "media/" + c
        dest = os.path.join(ROOT, "media", candidates[0])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        data = self.tar.extractfile(src).read()
        if ext == ".mp3":
            encode_audio(data, dest)
        elif ext == ".gif":
            with open(dest, "wb") as f:
                f.write(data)
        else:
            img = Image.open(io.BytesIO(data))
            save_web_image(img, dest, ext)
            if ext == ".png" and os.path.getsize(dest) > PNG_TO_JPEG_BYTES and not has_alpha(img):
                os.remove(dest)
                dest = os.path.join(ROOT, "media", candidates[1])
                save_web_image(img, dest, ".jpg")
        return os.path.relpath(dest, ROOT)


def has_alpha(img):
    if img.mode in ("RGBA", "LA") or "transparency" in img.info:
        return img.convert("RGBA").getextrema()[3][0] < 255
    return False


def save_web_image(img, dest, ext):
    """Downscale, apply EXIF rotation, and save without metadata (drops GPS etc.)."""
    try:
        orient = img.getexif().get(274, 1)
    except Exception:
        orient = 1
    img = {3: lambda i: i.rotate(180, expand=True),
           6: lambda i: i.rotate(270, expand=True),
           8: lambda i: i.rotate(90, expand=True)}.get(orient, lambda i: i)(img)
    img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
    if ext in (".jpg", ".jpeg"):
        img.convert("RGB").save(dest, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    else:
        img.save(dest, "PNG", optimize=True)


def encode_audio(data, dest):
    """Re-encode the long spoken-word MP3 as 64 kbps mono AAC (macOS afconvert)."""
    tmp = dest + ".src.mp3"
    with open(tmp, "wb") as f:
        f.write(data)
    subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-c", "1", "-b", "64000", tmp, dest], check=True)
    os.remove(tmp)


def fetch_external(url):
    """Save a hotlinked third-party image under media/external/, or None if it's gone."""
    url = html.unescape(url)
    if url in RECOVERED:
        out = "media/external/" + RECOVERED[url]
        if not os.path.exists(os.path.join(ROOT, out)):
            src = os.path.join(ROOT, "tools", "recovered", RECOVERED[url])
            save_web_image(Image.open(src), os.path.join(ROOT, out), ".jpg")
        return out
    name = re.sub(r"[^a-z0-9]+", "-", url.split("://", 1)[1].lower()).strip("-")[-60:] + ".jpg"
    out = "media/external/" + name
    dest = os.path.join(ROOT, out)
    if os.path.exists(dest):
        return out
    try:
        data = subprocess.run(["curl", "-sfL", "-m", "30", "-A", "Mozilla/5.0", url],
                              check=True, capture_output=True).stdout
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        save_web_image(Image.open(io.BytesIO(data)), dest, ".jpg")
        note(f"SAVED hotlinked image locally: {url}")
        return out
    except Exception as e:
        note(f"DEAD hotlinked image (placeholder shown): {url} ({e})")
        return None


# ---------------------------------------------------------------- content conversion

BLOCK_TAGS = r"(?:p|div|figure|ul|ol|li|h[1-6]|blockquote|table|pre|hr|iframe|audio|video)"


def autop(text):
    """Minimal wpautop for the classic-editor posts: blank lines -> paragraphs."""
    out = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        chunk = chunk.strip()
        if not chunk:
            continue
        if re.match(r"<" + BLOCK_TAGS + r"[\s>]", chunk):
            out.append(chunk)
        else:
            out.append("<p>" + chunk.replace("\n", "<br>\n") + "</p>")
    return "\n\n".join(out)


def convert_captions(body):
    def fig(m):
        attrs, inner = m.group(1), m.group(2)
        im = re.match(r"\s*((?:<a [^>]*>)?\s*<img[^>]*>\s*(?:</a>)?)(.*)$", inner, re.S)
        if not im:
            return inner
        align = re.search(r'align="(\w+)"', attrs)
        cls = f' class="wp-caption {align.group(1)}"' if align else ' class="wp-caption"'
        return f"<figure{cls}>{im.group(1)}<figcaption>{im.group(2).strip()}</figcaption></figure>"
    return re.sub(r"\[caption([^\]]*)\](.*?)\[/caption\]", fig, body, flags=re.S)


def convert_embeds(body):
    def yt(m):
        url = m.group(1)
        vid = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", url)
        if not vid:
            note(f"UNHANDLED embed left as link: {url}")
            return f'<a href="{url}">{url}</a>'
        v = vid.group(1)
        return (f'<div class="video"><iframe src="https://www.youtube-nocookie.com/embed/{v}" '
                f'title="YouTube video" loading="lazy" allowfullscreen></iframe></div>')
    return re.sub(r'<div class="wp-block-embed__wrapper">\s*(\S+)\s*</div>', yt, body)


def rewrite_tag_attrs(body, ctx):
    """Rewrite src/href to local relative paths and strip WordPress-only attributes."""
    def img(m):
        tag = m.group(0)
        src = re.search(r'src="([^"]*)"', tag).group(1)
        alt = re.search(r'alt="([^"]*)"', tag)
        cls = re.search(r'class="([^"]*)"', tag)
        local = ctx["media"].local(src)
        if not local and not UPLOAD_RE.match(src):
            local = fetch_external(src)
            if not local:
                ctx["dead"].append(src)
                return '<span class="missing-image">[Image no longer available]</span>'
        if not local:
            return '<span class="missing-image">[Image missing from export]</span>'
        align = " ".join(c for c in (cls.group(1).split() if cls else []) if c.startswith("align"))
        return (f'<img src="{ctx["root"]}{local}" alt="{alt.group(1) if alt else ""}"'
                + (f' class="{align}"' if align else "") + ' loading="lazy" decoding="async">')

    def href(m):
        url = html.unescape(m.group(1))
        local = ctx["media"].local(url) if UPLOAD_RE.match(url) else None
        if local:
            return f'href="{ctx["root"]}{local}"'
        pm = POST_LINK_RE.match(url)
        if pm:
            path = pm.group(1).strip("/") + "/"
            if path not in ctx["paths"]:
                note(f"LINK to unknown post in {ctx['post']}: {url}")
            return f'href="{ctx["root"]}{path}"'
        am = ATTACH_LINK_RE.match(url)
        if am and am.group(1) in ctx["attachments"]:
            f = ctx["attachments"][am.group(1)]["file"]
            local = ctx["media"].local("https://snailedit.xyz/wp-content/uploads/" + f) if f else None
            if local:
                return f'href="{ctx["root"]}{local}"'
        if re.match(r"https?://" + OWN_HOSTS, url):
            note(f"LINK to old site left unchanged in {ctx['post']}: {url}")
        return m.group(0)

    def audio(m):
        local = ctx["media"].local(m.group(1))
        return f'<audio controls preload="none" src="{ctx["root"]}{local}"></audio>'

    body = re.sub(r"<audio[^>]*src=\"([^\"]+)\"[^>]*></audio>", audio, body)
    body = re.sub(r"<img\b[^>]*>", img, body)
    body = re.sub(r'href="([^"]*)"', href, body)
    body = re.sub(r'\s(?:data-[\w-]+|srcset|sizes)="[^"]*"', "", body)
    return body


def convert_body(post, ctx):
    body = post["body"]
    classic = "<!-- wp:" not in body
    body = convert_captions(body)
    if classic:
        body = autop(body)
    body = re.sub(r"<!-- /?wp:.*?-->\n?", "", body, flags=re.S)
    body = convert_embeds(body)
    body = rewrite_tag_attrs(body, ctx)
    for line in re.findall(r"^\s*(https?://\S+)\s*$", body, re.M):
        note(f"BARE URL line (possible lost embed) in {post['path']}: {line}")
    for sc in set(re.findall(r"\[(gallery|embed|video|audio|playlist|youtube|instagram|tweet)\b", body)):
        note(f"SHORTCODE [{sc}] left in {post['path']}")
    return body.strip()


# ---------------------------------------------------------------- templates

def fmt_date(d):
    return f"{d:%B} {d.day}, {d.year}"


SPIRAL = ('<svg class="spiral" viewBox="0 0 100 100" aria-hidden="true"><path d="M50 50 '
          'm0 0 a3 3 0 0 1 6 0 a6 6 0 0 1 -12 0 a10 10 0 0 1 20 0 a15 15 0 0 1 -30 0 '
          'a21 21 0 0 1 42 0 a28 28 0 0 1 -56 0 a36 36 0 0 1 72 0" fill="none" '
          'stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg>')


def page(title, root, content, description=TAGLINE, body_class=""):
    full_title = f"{html.escape(title)} – {SITE_TITLE}" if title != SITE_TITLE else SITE_TITLE
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{full_title}</title>
<meta name="description" content="{html.escape(description)}">
<link rel="icon" href="{root}assets/snail.png">
<link rel="stylesheet" href="{root}assets/style.css">
</head>
<body class="{body_class}">
<header class="masthead">
  <a class="brand" href="{root}"><img src="{root}assets/snail.png" alt="" width="40" height="40"><span>{SITE_TITLE}</span></a>
  <nav><a href="{root}">All posts</a><a href="{root}../">snailedit.xyz</a></nav>
</header>
{content}
<footer class="site-footer">
  {SPIRAL}
  <p>{SITE_TITLE} · written by Craig Broady, 2019–2021 · <a href="{root}../">snailedit.xyz</a></p>
</footer>
</body>
</html>
"""


def post_page(post, prev, nxt, body):
    root = "../../../../"
    def nav_link(p, label, cls):
        if not p:
            return f'<span class="{cls}"></span>'
        return (f'<a class="{cls}" href="{root}{p["path"]}"><small>{label}</small>'
                f'{html.escape(p["title"])}</a>')
    content = f"""<main class="post">
<article>
  <header class="post-header">
    <p class="post-date"><time datetime="{post['date']:%Y-%m-%d}">{fmt_date(post['date'])}</time></p>
    <h1>{html.escape(post['title'])}</h1>
  </header>
  <div class="entry">
{body}
  </div>
</article>
<nav class="post-nav">
  {nav_link(prev, "← Previous", "prev")}
  {nav_link(nxt, "Next →", "next")}
</nav>
</main>"""
    text = re.sub(r"<[^>]+>", " ", body)
    desc = " ".join(html.unescape(text).split()[:30]) + "…"
    return page(post["title"], root, content, description=desc)


def index_page(posts, coffee):
    welcome = next(p for p in posts if p["path"] == "2019/03/06/welcome/")
    groups = []
    for p in posts:
        key = (p["date"].year, p["date"].strftime("%B"))
        if not groups or groups[-1][0] != key:
            groups.append((key, []))
        groups[-1][1].append(p)
    years = []
    for (year, month), items in groups:
        if not years or years[-1][0] != year:
            years.append((year, []))
        years[-1][1].append((month, items))
    archive = []
    for year, months in years:
        archive.append(f'<section class="year"><h2>{year}</h2>')
        for month, items in months:
            archive.append(f'<h3>{month} <span class="count">{len(items)}</span></h3><ul class="post-list">')
            for p in items:
                archive.append(f'<li><time datetime="{p["date"]:%Y-%m-%d}">{p["date"]:%b} {p["date"].day}</time>'
                               f'<a href="{p["path"]}">{html.escape(p["title"])}</a></li>')
            archive.append("</ul>")
        archive.append("</section>")
    content = f"""<div class="hero">
  <img src="assets/header.jpg" alt="Misty sunrise over a field lined with trees">
  <div class="hero-text"><h1>{SITE_TITLE}</h1><p>{TAGLINE}</p></div>
</div>
<main class="home">
<aside class="about">
  <img src="{coffee}" alt="Craig sipping from a coffee cup" width="120" height="118">
  <div>
    <h2>Hi there!</h2>
    <p>I’m Craig Broady, a Boston-based snail enthusiast, mechanical engineer, and cat parent. Originally this blog was started to document my sabbatical in spring of 2019, when I spent seven weeks traveling the west coast. Now it’s an occasional collection of my thoughts.</p>
    <p>While my posts can generally be read in any order, I recommend starting at the beginning of my travels <a href="{welcome['path']}">here</a>.</p>
    <p class="archive-note">This is a frozen archive of the blog as it stood in 2021. Comments and subscriptions have been retired.</p>
  </div>
</aside>
<div class="archive">
{chr(10).join(archive)}
</div>
</main>"""
    return page(SITE_TITLE, "", content, body_class="is-home")


# ---------------------------------------------------------------- redirects

def redirect_map(posts, attachments):
    to = lambda path: BASE_PATH + path
    posts_by_id = {p["id"]: p for p in posts}
    redirects = {"/" + p["path"]: to(p["path"]) for p in posts}
    misc = ["contact/", "feed/", "comments/feed/", "category/uncategorized/",
            "category/sabbatical-2019/", "author/skittoi/"]
    misc += [f"page/{n}/" for n in range(2, 11)]
    misc += sorted({f"{p['date']:%Y/%m}/" for p in posts} | {f"{p['date']:%Y}/" for p in posts})
    for m in misc:
        redirects["/" + m] = BASE_PATH
    attach = {}
    for slug, a in attachments.items():
        parent = posts_by_id.get(a["parent"])
        attach["/" + slug + "/"] = to(parent["path"]) if parent else BASE_PATH
    return {
        "_about": ("Old WordPress paths on snailedit.xyz -> new archive paths. Paths are root-relative so they "
                   "work on any host. 'redirects' should each get a meta-refresh stub in the landing page repo. "
                   "'attachment_pages' are the old per-image pages (low traffic; optional). 'uploads_prefix' "
                   "can't be done with stubs; a 404.html script could rewrite it. 'post_ids' maps old "
                   "?p=ID shortlinks, which also need a script since GitHub Pages ignores query strings."),
        "base": BASE_PATH,
        "redirects": redirects,
        "attachment_pages": dict(sorted(attach.items())),
        "uploads_prefix": {"from": "/wp-content/uploads/", "to": BASE_PATH + "media/",
                           "note": "Same YYYY/MM/filename; .mp3 became .m4a"},
        "post_ids": {p["id"]: to(p["path"]) for p in posts},
    }


# ---------------------------------------------------------------- main

def main():
    xml_path, tar_path = sys.argv[1], sys.argv[2]
    header_src = sys.argv[3] if len(sys.argv) > 3 else None
    posts, attachments = load_export(xml_path)
    media = Media(tar_path)

    for d in OUTPUT_DIRS:
        if d not in ("media", "assets"):  # media is cached between runs
            shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
    os.makedirs(os.path.join(ROOT, "assets"), exist_ok=True)

    ctx = {"media": media, "attachments": attachments, "root": "../../../../",
           "paths": {p["path"] for p in posts}, "dead": []}
    bodies = []
    for p in posts:
        ctx["post"] = p["path"]
        bodies.append(convert_body(p, ctx))

    for i, (p, body) in enumerate(zip(posts, bodies)):
        prev = posts[i - 1] if i > 0 else None
        nxt = posts[i + 1] if i + 1 < len(posts) else None
        dest = os.path.join(ROOT, p["path"], "index.html")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(post_page(p, prev, nxt, body))

    coffee = media.local("https://snailedit.xyz/wp-content/uploads/2019/03/coffee-e1552015830981.png")
    snail = Image.open(io.BytesIO(media.tar.extractfile("2019/03/snail.png").read()))
    snail.resize((128, 128), Image.NEAREST).save(os.path.join(ROOT, "assets/snail.png"), optimize=True)
    if header_src:
        save_web_image(Image.open(header_src), os.path.join(ROOT, "assets/header.jpg"), ".jpg")

    with open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_page(posts, coffee))
    with open(os.path.join(ROOT, "redirects.json"), "w", encoding="utf-8") as f:
        json.dump(redirect_map(posts, attachments), f, indent=2)
        f.write("\n")
    open(os.path.join(ROOT, ".nojekyll"), "w").close()

    print(f"{len(posts)} posts, {len(media.used)} media files")
    for line in sorted(set(report)):
        print(" -", line)


if __name__ == "__main__":
    main()
