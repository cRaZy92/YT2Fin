"""htmx inherits hx-swap from ancestors; a live-updated element must not pick up an ancestor's outerHTML swap."""
from html.parser import HTMLParser

from app.models import VideoStatus

VOID = {"img", "input", "br", "meta", "link", "hr", "source"}


class SwapChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[dict] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        live = "sse-swap" in attrs or "hx-get" in attrs or "hx-post" in attrs
        inherited = next((a["hx-swap"] for a in reversed(self.stack) if "hx-swap" in a), None)
        if live and "hx-swap" not in attrs and inherited not in (None, "innerHTML"):
            self.problems.append(f"<{tag} {attrs}> inherits hx-swap={inherited}")
        if tag not in VOID:
            self.stack.append(attrs)

    def handle_endtag(self, tag):
        if tag not in VOID and self.stack:
            self.stack.pop()


def problems(html: str) -> list[str]:
    checker = SwapChecker()
    checker.feed(html)
    return checker.problems


def test_live_elements_do_not_inherit_outer_swaps(client, make_mapping, add_video):
    mapping = make_mapping(playlist_url="https://www.youtube.com/@Chan")
    add_video(mapping, "a", status=VideoStatus.DOWNLOADING)
    add_video(mapping, "b", status=VideoStatus.FAILED, error="boom")
    add_video(mapping, "c")
    pages = ["/", f"/mappings/{mapping.id}", f"/mappings/{mapping.id}/list", f"/mappings/{mapping.id}/card",
             "/activity", "/settings", "/logs", "/mappings/new"]
    found = {page: problems(client.get(page).text) for page in pages}
    assert {page: p for page, p in found.items() if p} == {}
