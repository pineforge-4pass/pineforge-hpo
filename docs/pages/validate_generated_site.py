#!/usr/bin/env python3
"""Sanitize build paths and validate local links in generated API documentation."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


TEXT_SUFFIXES = {".css", ".html", ".js", ".svg", ".xml"}


class _LinkCollector(HTMLParser):
    def __init__(self, source: Path) -> None:
        super().__init__()
        self.source = source
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        for name, value in attrs:
            if name in {"href", "src"} and value:
                self.links.append(value)


def _sanitize(site: Path, source_root: Path) -> None:
    source_prefix = source_root.resolve().as_posix().rstrip("/") + "/"
    for path in site.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        content = path.read_text(encoding="utf-8", errors="strict")
        sanitized = content.replace(source_prefix, "")
        if sanitized != content:
            path.write_text(sanitized, encoding="utf-8")


def _missing_local_links(site: Path) -> list[tuple[str, str]]:
    missing: set[tuple[str, str]] = set()
    for page in site.rglob("*.html"):
        collector = _LinkCollector(page)
        collector.feed(page.read_text(encoding="utf-8"))
        for value in collector.links:
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc or value.startswith("#"):
                continue
            relative = unquote(parsed.path)
            if not relative:
                continue
            if relative.startswith("/"):
                missing.add((page.relative_to(site).as_posix(), value))
                continue
            target = (page.parent / relative).resolve()
            if relative.endswith("/"):
                target /= "index.html"
            if not target.exists():
                missing.add((page.relative_to(site).as_posix(), value))
    return sorted(missing)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sanitize a Doxygen site and reject broken local links."
    )
    parser.add_argument("site", type=Path, help="generated HTML directory")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="source root whose absolute path must not be published",
    )
    args = parser.parse_args()

    site = args.site.resolve()
    if not (site / "index.html").is_file():
        parser.error(f"site does not contain index.html: {site}")

    _sanitize(site, args.source_root)

    source_prefix = args.source_root.resolve().as_posix().rstrip("/") + "/"
    leaked = [
        path.relative_to(site).as_posix()
        for path in site.rglob("*")
        if path.is_file()
        and path.suffix.lower() in TEXT_SUFFIXES
        and source_prefix in path.read_text(encoding="utf-8", errors="strict")
    ]
    if leaked:
        print("absolute source path remains in generated files:")
        for path in leaked:
            print(f"  {path}")
        return 1

    missing = _missing_local_links(site)
    if missing:
        print("broken local links in generated documentation:")
        for source, link in missing:
            print(f"  {source}: {link}")
        return 1

    html_count = sum(1 for _ in site.rglob("*.html"))
    print(f"validated {html_count} HTML files; local links and source paths are clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
