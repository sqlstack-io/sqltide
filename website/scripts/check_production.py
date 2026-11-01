"""Check production indexing and ensure unselected hero routes cannot be published."""

from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1] / "dist"
ORIGIN = "https://sqltide.io"


class HeadParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonical = None
        self.noindex = False
        self.redirect = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href")
        if tag == "meta" and attrs.get("name") == "robots":
            self.noindex |= "noindex" in attrs.get("content", "")
        if tag == "meta" and attrs.get("http-equiv") == "refresh":
            self.redirect = True


pages = [ROOT / "index.html", *(ROOT / "docs").rglob("index.html")]
validated = 0
for page in pages:
    route = "/" + page.relative_to(ROOT).as_posix().removesuffix("index.html")
    head = HeadParser()
    head.feed(page.read_text())
    if head.redirect:
        continue
    assert head.canonical == ORIGIN + route, (page, head.canonical)
    assert not head.noindex, page
    validated += 1
for concept in ["virtual-lifecycle", "sql-plan", "ingestion-replay"]:
    assert not (ROOT / "concepts" / concept).exists(), "Unselected concept reached production"
locations = {
    element.text
    for sitemap in ROOT.glob("sitemap-*.xml")
    for element in ElementTree.parse(sitemap).iter(
        "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
    )
}
assert {ORIGIN + "/", ORIGIN + "/docs/", ORIGIN + "/docs/quickstart/"} <= locations
assert all(
    url.startswith(ORIGIN + "/") and not url.startswith(ORIGIN + "/concepts/") for url in locations
)
assert "Disallow: /\n" not in (ROOT / "robots.txt").read_text()
home = (ROOT / "index.html").read_text()
assert 'data-showcase="virtual"' in home, "Selected virtual hero missing from production"
assert 'class="deployment-status status-b">Receiving live data' in home, (
    "Static explanation missing"
)
assert home.count("Promotion switches one view at a time") == 1
print(
    f"Production: {validated} indexable canonical pages; "
    "selected virtual hero; sitemap includes homepage/docs; no review concepts"
)
