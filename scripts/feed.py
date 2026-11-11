import argparse
import gzip
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
RSS_URL = "https://hnrss.org/frontpage?count=30"
HN_HOST = "news.ycombinator.com"
FEED_FILE = "data/feed.json"
RUN_FILE = ".cache/run.json"
INPUT_FILE = ".cache/input.jsonl"
TRANSLATIONS_FILE = ".cache/translations.json"
SITE_DIRECTORY = ".cache/site"
MAX_RESPONSE_BYTES = 2_000_000
HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
PUNCTUATION_REPLACEMENTS = str.maketrans({
    "·": ", ",
    "•": ", ",
    "‧": ", ",
    "・": ", ",
    "—": ", ",
    "–": "-",
})


def require(condition, message):
    if not condition:
        raise ValueError(message)


def compact_text(text):
    return " ".join(text.split())


def json_text(value, compact=False):
    if compact:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def json_lines(rows):
    return "".join(json_text(row, compact=True) + "\n" for row in rows)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_json(path, value):
    write_text(path, json_text(value))


def content_hash(content):
    return sha256(content).hexdigest()


def feed_fingerprint():
    return content_hash((ROOT / FEED_FILE).read_bytes())


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def web_url(value):
    parsed = urlparse(value)
    valid = (
        parsed.scheme in {"https", "http"}
        and parsed.hostname
        and not parsed.username
    )

    require(valid, f"Invalid web URL: {value!r}")
    return value


def is_hn_url(url):
    return urlparse(url).hostname == HN_HOST


def read_limited(stream):
    content = stream.read(MAX_RESPONSE_BYTES + 1)
    require(len(content) <= MAX_RESPONSE_BYTES, "Response exceeds size limit")
    return content


def response_body(response):
    content = read_limited(response)
    encoding = response.headers.get("Content-Encoding", "").lower()
    compressed = encoding == "gzip" or content.startswith(b"\x1f\x8b")

    if not compressed:
        return content

    with gzip.GzipFile(fileobj=BytesIO(content)) as stream:
        return read_limited(stream)


def fetch_resource(url, *, accept, timeout, content_types=None):
    request = Request(
        web_url(url),
        headers={
            "User-Agent": "hnfront/1.0 (+https://hnrss.org/)",
            "Accept": accept,
        },
    )

    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()

        if content_types is not None:
            require(
                content_type in content_types,
                f"Unsupported article type: {content_type}",
            )

        body = response_body(response)
        charset = response.headers.get_content_charset() or "utf-8"
        return body, charset, response.url


def fetch_rss():
    body, _, _ = fetch_resource(
        RSS_URL,
        accept="application/rss+xml, application/xml",
        timeout=30,
    )
    return body


def fetch_html(url):
    body, charset, final_url = fetch_resource(
        url,
        accept="text/html",
        timeout=12,
        content_types=HTML_CONTENT_TYPES,
    )
    return body.decode(charset, errors="replace"), final_url


class ArticleParser(HTMLParser):
    SKIPPED_TAGS = {
        "script", "style", "nav", "footer", "header",
        "noscript", "svg", "form", "aside",
    }
    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img",
        "input", "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.article = []
        self.main = []
        self.post = []
        self.post_depth = None
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)

        if tag == "meta":
            key = attributes.get("property") or attributes.get("name") or ""
            self.meta.setdefault(key.lower(), attributes.get("content", ""))

        if tag in self.VOID_TAGS:
            return

        self.stack.append(tag)
        classes = (attributes.get("class") or "").split()

        if "toptext" in classes:
            self.post_depth = len(self.stack)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            closing_index = len(self.stack) - self.stack[::-1].index(tag) - 1
            self.stack = self.stack[:closing_index]

        if self.post_depth is not None and len(self.stack) < self.post_depth:
            self.post_depth = None

    def handle_data(self, data):
        hidden = any(tag in self.SKIPPED_TAGS for tag in self.stack)
        text = compact_text(data)

        if hidden or not text:
            return

        self.text.append(text)

        if self.post_depth is not None:
            self.post.append(text)

        if "article" in self.stack:
            self.article.append(text)

        if "main" in self.stack:
            self.main.append(text)

    def content(self):
        article = " ".join(self.article)
        main = " ".join(self.main)
        candidates = [article, main]

        if len(main) > 2 * len(article):
            candidates.reverse()

        candidates.append(" ".join(self.text))
        return next((text for text in candidates if len(text) >= 200), "")

    def submitted_text(self):
        return " ".join(self.post)

    def description(self):
        return self.meta.get("og:description") or self.meta.get("description")

    def image(self):
        return self.meta.get("og:image") or self.meta.get("twitter:image")


def parse_html(body):
    document = ArticleParser()
    document.feed(body)
    return document


def html_text(body):
    return " ".join(parse_html(body).text)


def hn_item_id(url):
    parsed = urlparse(url)
    item_id = parse_qs(parsed.query).get("id", [""])[0]

    require(
        parsed.hostname == HN_HOST and item_id.isdigit(),
        "RSS item has an invalid HN comments URL",
    )
    return item_id


def extract_count(text, label):
    match = re.search(re.escape(label) + r"\s*(\d+)", text)
    return int(match.group(1)) if match else 0


def rss_post_text(description):
    metadata_start = r"<(?:hr\b|p>\s*(?:Article|Comments) URL:)"
    body = re.split(metadata_start, description, maxsplit=1, flags=re.I)[0]
    return html_text(body)[:16000]


def parse_rss_item(node):
    comments_url = node.findtext("comments") or node.findtext("guid") or ""
    item_id = hn_item_id(comments_url)
    title = (node.findtext("title") or "").strip()
    description = node.findtext("description") or ""
    metadata = html_text(description)
    published_at = parsedate_to_datetime(node.findtext("pubDate"))

    require(title, "RSS item has no title")

    return {
        "id": item_id,
        "title": title,
        "url": web_url(node.findtext("link") or comments_url),
        "comments_url": f"https://{HN_HOST}/item?id={item_id}",
        "points": extract_count(metadata, "Points:"),
        "comments": extract_count(metadata, "# Comments:"),
        "published_at": published_at.astimezone(timezone.utc).isoformat(),
        "rss_text": rss_post_text(description),
    }


def parse_rss(body):
    channel = ET.fromstring(body).find("channel")
    require(channel is not None, "RSS channel is missing")

    articles = {}

    for node in channel.findall("item"):
        article = parse_rss_item(node)
        articles.setdefault(article["id"], article)

    require(articles, "RSS is empty; keeping the previous feed")
    return list(articles.values())


def has_cached_translation(article, cached):
    return (
        cached.get("url") == article["url"]
        and cached.get("title") == article["title"]
        and bool(cached.get("title_ko"))
        and "summary" in cached
    )


def article_metadata(article):
    source_fields = {"rss_text", "source_text"}
    return {key: value for key, value in article.items() if key not in source_fields}


def source_data(status, text="", thumbnail=None):
    return {
        "source_status": status,
        "source_text": text,
        "thumbnail": thumbnail,
    }


def thumbnail_url(document, base_url):
    image = document.image()

    if not image:
        return None

    try:
        return web_url(urljoin(base_url, image))
    except ValueError:
        return None


def fetch_article_source(url, previous_thumbnail):
    body, final_url = fetch_html(url)
    document = parse_html(body)
    thumbnail = thumbnail_url(document, final_url) or previous_thumbnail
    text = document.submitted_text() if is_hn_url(url) else document.content()

    if text:
        return source_data("article", text, thumbnail)

    description = document.description()

    if description and not is_hn_url(url):
        return source_data("description", description[:3000], thumbnail)

    return source_data("unavailable", thumbnail=thumbnail)


def collect_article_source(article, previous_thumbnail):
    rss_text = article["rss_text"]

    if is_hn_url(article["url"]) and rss_text:
        return source_data("rss", rss_text, previous_thumbnail)

    try:
        source = fetch_article_source(article["url"], previous_thumbnail)
    except Exception:
        source = source_data("unavailable", thumbnail=previous_thumbnail)

    if not source["source_text"] and rss_text:
        return source_data("rss", rss_text, source["thumbnail"])

    return source


def enrich(article, cache):
    cached = cache.get(article["id"], {})
    metadata = article_metadata(article)

    if has_cached_translation(article, cached):
        source = source_data(cached["source_status"], thumbnail=cached.get("thumbnail"))
        return {**metadata, **source}

    unchanged_source = (
        cached.get("url") == article["url"]
        and cached.get("title") == article["title"]
    )
    thumbnail = cached.get("thumbnail") if unchanged_source else None
    source = collect_article_source(article, thumbnail)
    return {**metadata, **source}


def collect_articles(rss_file, cache):
    body = Path(rss_file).read_bytes() if rss_file else fetch_rss()
    articles = parse_rss(body)

    with ThreadPoolExecutor(max_workers=5) as pool:
        return list(pool.map(lambda article: enrich(article, cache), articles))


def translation_requests(articles, cache, source_chars):
    return [
        [article["id"], article["title"], article["source_text"][:source_chars]]
        for article in articles
        if not has_cached_translation(article, cache.get(article["id"], {}))
    ]


def normalize_translation(text):
    text = text.translate(PUNCTUATION_REPLACEMENTS)
    return re.sub(r"\s*,\s*", ", ", compact_text(text)).strip()


def validate_text(value, *, label, limit, allow_empty=False, korean=False):
    require(isinstance(value, str), f"{label} must be text")
    require(len(value) <= limit, f"{label} must be at most {limit} characters")

    if not allow_empty:
        require(value.strip(), f"{label} must not be empty")

    if korean and value:
        require(re.search("[가-힣]", value), f"{label} must be Korean")


def validate_translation_style(title, summary):
    require(
        not re.search(r"[∙⋅]|\\cdot\b", title + summary),
        "Write mathematical notation naturally in Korean instead of using dot symbols",
    )
    require(
        not re.search(r"(?:습니다|입니다)[.!?]?(?:\s|$)|소개하는 페이지|이 글은", summary),
        "Summarize facts directly using 한다/이다, without article introductions",
    )


def validate_source_summary(summary, source_status):
    if source_status == "unavailable":
        require(not summary, "No source text; translate the title and leave summary empty")
    elif source_status is not None:
        require(summary.strip(), "Source text is available; summary is required")


def validate_translation(title, summary, source_status):
    validate_text(title, label="Title", limit=120)
    validate_text(summary, label="Summary", limit=400, allow_empty=True, korean=True)

    title = normalize_translation(title)
    summary = normalize_translation(summary)

    validate_text(title, label="Title", limit=120)
    validate_text(summary, label="Summary", limit=400, allow_empty=True, korean=True)
    validate_translation_style(title, summary)
    validate_source_summary(summary, source_status)

    return {"title_ko": title, "summary": summary}


def validate_translation_ids(translations, expected_ids):
    actual = set(translations)
    expected = set(expected_ids)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)

    require(
        not missing and not unexpected,
        f"Translation IDs: missing={missing}, unexpected={unexpected}; previous page preserved",
    )


def validate_summaries(result, run):
    require(
        isinstance(result, dict) and set(result) == {"run", "items"},
        "Expected {run: current_run_id, items: [[id, title_ko, summary], ...]}",
    )
    require(result["run"] == run["id"], "Translation output belongs to a different run")
    require(isinstance(result["items"], list), "Translation items must be an array")

    sources = {article["id"]: article for article in run["items"]}
    translations = {}

    for row in result["items"]:
        require(
            isinstance(row, list) and len(row) == 3,
            "Each translation must be [id, title_ko, summary]",
        )
        item_id, title, summary = row
        require(isinstance(item_id, str), "Translation ID must be a string")
        require(item_id not in translations, f"Duplicate translation ID: {item_id}")

        source_status = sources.get(item_id, {}).get("source_status")

        try:
            translations[item_id] = validate_translation(title, summary, source_status)
        except ValueError as error:
            raise ValueError(f"{item_id}: {error}") from error

    validate_translation_ids(translations, run["pending"])
    return translations


def resumable_run(base):
    run_path = ROOT / RUN_FILE

    if not run_path.exists():
        return None

    run = read_json(run_path)
    can_resume = (
        "published_hash" not in run
        and run["base"] == base
        and (ROOT / INPUT_FILE).exists()
        and (ROOT / TRANSLATIONS_FILE).exists()
    )
    return run if can_resume else None


def create_run(base, articles, requests, input_text):
    items = [article_metadata(article) for article in articles]
    identity = base + json_text(items) + input_text

    return {
        "id": content_hash(identity.encode())[:16],
        "base": base,
        "updated_at": utc_now(),
        "items": items,
        "pending": [row[0] for row in requests],
    }


def save_run(run, input_text):
    write_text(ROOT / INPUT_FILE, input_text)
    write_json(ROOT / TRANSLATIONS_FILE, {"run": run["id"], "items": []})
    write_json(ROOT / RUN_FILE, run)


def apply_translations(previous, run, translations):
    stories = dict(previous["stories"])

    for article in run["items"]:
        translation = translations.get(article["id"]) or stories[article["id"]]
        stories[article["id"]] = {
            **article,
            "title_ko": translation["title_ko"],
            "summary": translation["summary"],
        }

    state = {
        "updated_at": previous["updated_at"],
        "order": [article["id"] for article in run["items"]],
        "stories": stories,
    }

    if state == previous:
        return previous

    state["updated_at"] = run["updated_at"]
    return state


def html_attributes(attributes):
    parts = []

    for name, value in attributes.items():
        if value is None:
            continue

        name = name.rstrip("_").replace("_", "-")
        parts.append(f'{name}="{escape(str(value), quote=True)}"')

    return " " + " ".join(parts) if parts else ""


def html_tag(name, content="", **attributes):
    opening = f"<{name}{html_attributes(attributes)}>"

    if name in ArticleParser.VOID_TAGS:
        return opening

    return opening + content + f"</{name}>"


def external_link(url, content, **attributes):
    return html_tag(
        "a",
        content,
        href=web_url(url),
        target="_blank",
        rel="noopener noreferrer",
        **attributes,
    )


def render_icon(path, filled=False):
    return html_tag(
        "svg",
        html_tag("path", d=path),
        viewBox="0 0 16 16",
        fill="currentColor" if filled else "none",
        stroke=None if filled else "currentColor",
        aria_hidden="true",
    )


def time_html(value):
    instant = datetime.fromisoformat(value).astimezone(timezone.utc)
    return html_tag(
        "time",
        instant.strftime("%Y-%m-%d %H:%M"),
        datetime=instant.isoformat(timespec="seconds"),
    )


def render_title(article):
    title = external_link(article["url"], escape(article["title_ko"]))
    return html_tag("h2", title, id=f"story-{article['id']}")


def render_thumbnail(article):
    if not article.get("thumbnail"):
        return ""

    image = html_tag(
        "img",
        src=web_url(article["thumbnail"]),
        alt="",
        width=1200,
        height=600,
        loading="lazy",
        decoding="async",
    )
    return external_link(
        article["url"],
        image,
        class_="thumb",
        aria_label=f"{article['title_ko']} 원문 읽기",
        tabindex="-1",
    )


def render_summary(article):
    if not article["summary"]:
        return ""

    return html_tag("p", escape(article["summary"]), class_="summary")


def render_points(article):
    count = f"{article['points']:,}"
    icon = render_icon("M8 2 15 14H1Z", filled=True)
    return html_tag("span", icon + count, class_="points", aria_label=f"{count} 포인트")


def render_comments(article):
    count = f"{article['comments']:,}"
    icon = render_icon("M3 2.5h10v8H7l-4 3v-11Z")
    return external_link(
        article["comments_url"],
        icon + count,
        class_="comments",
        aria_label=f"{article['title_ko']} 댓글 {count}개",
    )


def render_metadata(article):
    domain = urlparse(article["url"]).hostname.removeprefix("www.")
    published = time_html(article["published_at"]) if article.get("published_at") else ""
    parts = [
        html_tag("span", escape(domain), class_="source"),
        render_points(article),
        render_comments(article),
        html_tag("span", published, class_="published"),
    ]
    return html_tag("div", "".join(parts), class_="meta")


def render_story(article):
    parts = [
        render_title(article),
        render_thumbnail(article),
        render_summary(article),
        render_metadata(article),
    ]
    body = html_tag(
        "article",
        "\n".join(parts),
        aria_labelledby=f"story-{article['id']}",
    )
    return html_tag("li", body, class_="story")


def render(feed, published_at):
    stories = [render_story(feed["stories"][item_id]) for item_id in feed["order"]]
    template = Template((ROOT / "templates/index.html").read_text(encoding="utf-8"))
    empty = "" if stories else html_tag("p", "게시글이 없습니다.", class_="empty")

    return template.substitute(
        stories="\n".join(stories),
        published=time_html(published_at),
        empty=empty,
    )


def git(*args):
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=ROOT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode:
        message = result.stderr.strip()[:1500] or f"git {args[0]} failed"
        raise ValueError(message)

    return result.stdout.rstrip("\n")


def changed_paths():
    commands = [
        ("diff", "--name-only", "-z"),
        ("diff", "--cached", "--name-only", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ]
    paths = set()

    for command in commands:
        paths.update(path for path in git(*command).split("\0") if path)

    return paths


def push_preflight():
    require(
        not changed_paths() - {FEED_FILE},
        "Unrelated working-tree changes; use a clean clone",
    )
    branch = git("symbolic-ref", "--short", "HEAD")
    git("remote", "get-url", "origin")
    return branch


def distribution_commit_message(published_at):
    instant = datetime.fromisoformat(published_at).astimezone(timezone.utc)
    return f"dist: {instant.isoformat(timespec='seconds')}"


def commit_and_push(branch, published_at):
    git("add", "--", FEED_FILE)

    if git("diff", "--cached", "--name-only"):
        git("commit", "-m", distribution_commit_message(published_at))

    git("push", "origin", f"HEAD:refs/heads/{branch}")


def prepare(rss_file=None, source_chars=2400, refresh=False):
    base = feed_fingerprint()
    existing = None if refresh else resumable_run(base)

    if existing is not None:
        return {"run": existing["id"], "pending": len(existing["pending"]), "resumed": True}

    cache = read_json(ROOT / FEED_FILE)["stories"]
    articles = collect_articles(rss_file, cache)
    requests = translation_requests(articles, cache, source_chars)
    input_text = json_lines(requests)
    run = create_run(base, articles, requests, input_text)

    save_run(run, input_text)

    return {
        "run": run["id"],
        "pending": len(requests),
        "input_chars": len(input_text),
    }


def publish(push=False):
    branch = push_preflight() if push else None
    run = read_json(ROOT / RUN_FILE)
    accepted_hashes = {run["base"], run.get("published_hash")}

    require(
        feed_fingerprint() in accepted_hashes,
        "Feed changed since prepare; run prepare --refresh",
    )

    result = read_json(ROOT / TRANSLATIONS_FILE)
    translations = validate_summaries(result, run)
    previous = read_json(ROOT / FEED_FILE)
    state = apply_translations(previous, run, translations)

    write_json(ROOT / FEED_FILE, state)
    run["published_hash"] = feed_fingerprint()
    write_json(ROOT / RUN_FILE, run)

    if push:
        commit_and_push(branch, state["updated_at"])

    return {
        "articles": len(state["order"]),
        "translated": len(translations),
        "changed": state != previous,
        "pushed": push,
    }


def build_site():
    site = ROOT / SITE_DIRECTORY
    feed = read_json(ROOT / FEED_FILE)
    page = render(feed, utc_now())
    favicon = (ROOT / "favicon.svg").read_text(encoding="utf-8")

    write_text(site / "index.html", page)
    write_text(site / "favicon.svg", favicon)

    return {"site": SITE_DIRECTORY}


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Collect HN articles, publish translation data, or build the static page.",
    )
    parser.add_argument("command", choices=["prepare", "publish", "render"])
    parser.add_argument("--rss-file", help="Collect from a saved RSS response")
    parser.add_argument(
        "--source-chars",
        type=int,
        default=2400,
        help="Maximum source characters per new article",
    )
    parser.add_argument("--refresh", action="store_true", help="Replace an unfinished preparation")
    parser.add_argument("--push", action="store_true", help="Commit and push translation data")
    arguments = parser.parse_args()

    if not 400 <= arguments.source_chars <= 8000:
        parser.error("--source-chars must be between 400 and 8000")

    if arguments.push and arguments.command != "publish":
        parser.error("--push is only valid with publish")

    return arguments


def main():
    arguments = parse_arguments()

    if arguments.command == "prepare":
        result = prepare(
            rss_file=arguments.rss_file,
            source_chars=arguments.source_chars,
            refresh=arguments.refresh,
        )
    elif arguments.command == "publish":
        result = publish(push=arguments.push)
    else:
        result = build_site()

    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Feed update failed: {error}", file=sys.stderr)
        sys.exit(1)
