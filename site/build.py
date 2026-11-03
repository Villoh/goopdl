#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["markdown-it-py>=4,<5"]
# ///
"""Build the static site into _site/: English at /, Spanish at /es/, docs prerendered from docs/*.md.

    uv run site/build.py            # build, then check every internal link and anchor
    uv run site/build.py --serve    # same, then serve _site on http://127.0.0.1:8765/

Sources: layout.html (shell), home.html and doc.html (page bodies), i18n.json (Spanish, plus
strings the build generates), the static files in STATIC, and the repo's docs/ Markdown.
"""
from __future__ import annotations

import functools
import html
import http.server
import json
import posixpath
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
OUT = REPO / "_site"
SITE = "https://villoh.github.io/goopdl/"
BLOB = "https://github.com/Villoh/goopdl/blob/main/"
PAGES = ["installation", "commands", "authentication", "how-it-works"]
LANGS = {
    "en": {"prefix": "", "docs": REPO / "docs", "locale": "en_US"},
    "es": {"prefix": "es/", "docs": REPO / "docs" / "es", "locale": "es_ES"},
}
STATIC = ["assets", "style.css", "site.js", "main.js", "docs.js", "llms.txt"]

def source(name: str) -> str:
    try:
        return (SRC / name).read_text(encoding="utf-8")
    except OSError as e:
        sys.exit(f"build: cannot read site/{name}: {e}")


try:
    STRINGS = json.loads(source("i18n.json"))
except json.JSONDecodeError as e:
    sys.exit(f"build: site/i18n.json is not valid JSON: {e}")
LAYOUT, HOME, DOC = source("layout.html"), source("home.html"), source("doc.html")
used_keys: set[str] = set()
esc = functools.partial(html.escape, quote=True)


def t(lang: str, key: str) -> str:
    used_keys.add(key)
    return STRINGS[lang][key]


def rel(cur: str, target: str) -> str:
    """Relative link from page directory `cur` to directory `target` (both site-relative, "" = root)."""
    r = posixpath.relpath(target or ".", cur or ".")
    return "./" if r == "." else f"{r}/"


def url(path: str) -> str:
    return SITE + (f"{path}/" if path else "")


def fill(template: str, values: dict[str, str]) -> str:
    """Replace every {{name}}; a missing value is a KeyError, so typos fail the build."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m[1]], template)


# English is the text written in the templates; other languages swap each [data-i18n] element's inner HTML.
I18N = re.compile(r'<(\w+)([^>]*?)\sdata-i18n="([^"]+)"([^>]*)>(.*?)</\1>', re.S)


def translate(page: str, lang: str) -> str:
    if lang != "en":
        def swap(m: re.Match) -> str:
            tag, before, key, after, inner = m.groups()
            assert f"<{tag}" not in inner, f'data-i18n="{key}": nested <{tag}> is not supported'
            return f"<{tag}{before}{after}>{t(lang, key)}</{tag}>"

        page = I18N.sub(swap, page)
        page = re.sub(r'aria-label="[^"]*"\s+data-i18n-aria="([^"]+)"', lambda m: f'aria-label="{esc(t(lang, m[1]))}"', page)
    return re.sub(r'\sdata-i18n(?:-aria)?="[^"]*"', "", page)


# ── markdown ────────────────────────────────────────────────────────────────
md = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
SHELL_LANGS = {"bash", "sh", "shell", "console", "dockerfile"}


def highlight_shell(code: str) -> str:
    """Dim `# comments` and mark the program name; everything else is escaped text."""
    lines = []
    for line in code.split("\n"):
        m = re.search(r"(^|\s)(#.*)$", line)
        body, comment = (line[: m.start(2)], m[2]) if m else (line, "")
        cmd = re.match(r"(\s*)(goopdl|uvx?|pipx|pip3?|nix|git|adb|cd|RUN)\b", body)
        out = f'{esc(cmd[1])}<span class="p">{cmd[2]}</span>{esc(body[cmd.end():])}' if cmd else esc(body)
        lines.append(out + (f'<span class="c">{esc(comment)}</span>' if comment else ""))
    return "\n".join(lines)


def render_fence(self, tokens, idx, options, env) -> str:
    tok = tokens[idx]
    lang = tok.info.strip().split(" ")[0]
    code = tok.content.rstrip("\n")
    body = highlight_shell(code) if lang in SHELL_LANGS else esc(code)
    cls = f' class="language-{esc(lang)}"' if lang else ""
    return f'<pre><code{cls}>{body}</code><button type="button" class="copy-code">{env["copy"]}</button></pre>\n'


md.add_render_rule("fence", render_fence)
md.add_render_rule("table_open", lambda self, tokens, idx, options, env: '<div class="table-wrap"><table>\n')
md.add_render_rule("table_close", lambda self, tokens, idx, options, env: "</table></div>\n")


def plain(inline: Token) -> str:
    parts = []
    for c in inline.children or []:
        if c.type in ("text", "code_inline"):
            parts.append(c.content)
        elif c.type in ("softbreak", "hardbreak"):
            parts.append(" ")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def github_slug(text: str, seen: dict[str, int]) -> str:
    """GitHub's heading anchors, so links like authentication.md#google-account keep working."""
    base = re.sub(r"[^\w\s-]", "", text.lower()).replace(" ", "-")
    n = seen[base] = seen.get(base, -1) + 1
    return f"{base}-{n}" if n else base


def html_token(content: str) -> Token:
    tok = Token("html_inline", "", 0)
    tok.content = content
    return tok


def rewrite_link(href: str, here: str, lang: str, cur: str) -> str:
    """Links to other docs stay on the site (same language); any other relative link goes to GitHub."""
    if href.startswith("#"):
        return href
    target = urljoin(here, href)
    parts = urlsplit(target)
    m = re.fullmatch(r"/Villoh/goopdl/blob/main/docs/(?:es/)?([\w-]+)\.md", parts.path)
    if parts.netloc == "github.com" and m and m[1] in PAGES:
        return rel(cur, f"{LANGS[lang]['prefix']}docs/{m[1]}") + (f"#{parts.fragment}" if parts.fragment else "")
    return target


def mark_alert(tokens: list[Token], i: int, lang: str) -> None:
    """GitHub alerts: `> [!WARNING]` becomes a styled callout with a translated title."""
    if tokens[i + 1].type != "paragraph_open":
        return
    children = tokens[i + 2].children
    if not children or children[0].type != "text":
        return
    first = children[0]
    m = re.match(r"\[!(\w+)\]\s*", first.content)
    if not m:
        return
    kind = m[1].lower()
    first.content = first.content[m.end():]
    if not first.content and len(children) > 1 and children[1].type == "softbreak":
        del children[1]
    tokens[i].attrJoin("class", f"alert alert-{kind}")
    children.insert(0, html_token(f'<span class="alert-title">{esc(t(lang, f"alert.{kind}"))}</span>'))


def render_doc(lang: str, name: str) -> dict:
    cur = f"{LANGS[lang]['prefix']}docs/{name}"
    source = (LANGS[lang]["docs"] / f"{name}.md").read_text(encoding="utf-8")
    source = re.sub(r"^(\*\*English\*\*|\[English\]).*\n+", "", source, count=1, flags=re.M)  # language switch line
    tokens = md.parse(source)
    here = f"{BLOB}docs/{'es/' if lang == 'es' else ''}{name}.md"
    seen: dict[str, int] = {}
    title, toc, paragraphs = None, [], []
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open":
            inline = tokens[i + 1]
            text = plain(inline)
            slug = github_slug(text, seen)
            tok.attrSet("id", slug)
            if tok.tag == "h1":
                title = title or text
            else:
                anchor = html_token(f'<a class="anchor" href="#{slug}" aria-hidden="true" tabindex="-1">#</a>')
                inline.children = [anchor, *(inline.children or [])]
            if tok.tag in ("h2", "h3"):
                toc.append((tok.tag, slug, text))
        elif tok.type == "inline":
            for child in tok.children or []:
                href = child.attrGet("href") if child.type == "link_open" else None
                if isinstance(href, str):
                    child.attrSet("href", rewrite_link(href, here, lang, cur))
            if tokens[i - 1].type == "paragraph_open" and not tokens[i - 1].hidden:
                paragraphs.append(plain(tok))
        elif tok.type == "blockquote_open":
            mark_alert(tokens, i, lang)
    assert title, f"{lang}/{name}.md has no # title"
    summary = next((p for p in paragraphs if len(p) >= 60), paragraphs[0] if paragraphs else title)
    return {
        "cur": cur,
        "title": title,
        "description": summary if len(summary) <= 160 else summary[:157].rstrip() + "…",
        "html": md.renderer.render(tokens, md.options, {"copy": t(lang, "copy")}),
        "toc": toc,
    }


# ── pages ───────────────────────────────────────────────────────────────────
def write_page(lang: str, paths: dict[str, str], main: str, *, title: str, description: str,
               og_type: str, jsonld: str, script: str, docs_current: bool) -> None:
    cur = paths[lang]
    root, home = rel(cur, ""), rel(cur, LANGS[lang]["prefix"].rstrip("/"))
    other = "es" if lang == "en" else "en"
    alternates = "\n".join(
        f'  <link rel="alternate" hreflang="{hl}" href="{url(paths[code])}">'
        for hl, code in (("en", "en"), ("es", "es"), ("x-default", "en"))
    )
    page = fill(LAYOUT, {
        "lang": lang, "title": esc(title), "description": esc(description), "url": url(cur),
        "alternates": alternates, "og_type": og_type, "og_locale": LANGS[lang]["locale"],
        "og_locale_alt": LANGS[other]["locale"], "jsonld": jsonld, "root": root, "home": home,
        "scripts": f'  <script src="{root}{script}" defer></script>', "main": main,
        "docs_current": "page" if docs_current else "false",
        "href_en": rel(cur, paths["en"]), "href_es": rel(cur, paths["es"]),
        "en_current": str(lang == "en").lower(), "es_current": str(lang == "es").lower(),
    })
    target = OUT / cur / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(translate(page, lang), encoding="utf-8")


def ld_script(data: dict) -> str:
    text = json.dumps({"@context": "https://schema.org", **data}, ensure_ascii=False, indent=2).replace("</", "<\\/")
    return f'  <script type="application/ld+json">\n{text}\n  </script>'


def strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def build_home(lang: str) -> None:
    paths = {code: LANGS[code]["prefix"].rstrip("/") for code in LANGS}
    main = translate(fill(HOME, {"root": rel(paths[lang], "")}), lang)
    # FAQPage is generated from the rendered FAQ, so the structured data always matches the page.
    faq = re.findall(r'<summary>(.*?)</summary><div class="faq-a">(.*?)</div>', main, re.S)
    assert faq, "FAQ items not found in home.html"
    graph = [
        {
            "@type": "SoftwareApplication", "name": "goopdl", "url": url(""), "inLanguage": lang,
            "description": t(lang, "ld.description"), "image": f"{SITE}assets/poster.jpg",
            "applicationCategory": "DeveloperApplication", "operatingSystem": "Linux, macOS, Windows",
            "softwareRequirements": "Python 3.10+", "license": "https://opensource.org/license/mit",
            "isAccessibleForFree": True, "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
            "downloadUrl": "https://pypi.org/project/goopdl/", "installUrl": "https://pypi.org/project/goopdl/",
            "sameAs": ["https://github.com/Villoh/goopdl", "https://pypi.org/project/goopdl/"],
            "author": {"@type": "Person", "name": "Mikel Villota", "url": "https://github.com/Villoh"},
        },
        {
            "@type": "FAQPage", "inLanguage": lang,
            "mainEntity": [
                {"@type": "Question", "name": strip_tags(q), "acceptedAnswer": {"@type": "Answer", "text": strip_tags(a)}}
                for q, a in faq
            ],
        },
    ]
    write_page(lang, paths, main, title=t(lang, "home.title"), description=t(lang, "home.description"),
               og_type="website", jsonld=ld_script({"@graph": graph}), script="main.js", docs_current=False)


def build_docs(lang: str) -> None:
    docs = {name: render_doc(lang, name) for name in PAGES}
    for i, name in enumerate(PAGES):
        d = docs[name]
        cur = d["cur"]
        sidebar = "\n".join(
            f'        <a href="{rel(cur, docs[p]["cur"])}"{" aria-current=\"page\"" if p == name else ""}>{esc(docs[p]["title"])}</a>'
            for p in PAGES
        )
        toc = "\n".join(f'        <a href="#{slug}" class="{tag}">{esc(text)}</a>' for tag, slug, text in d["toc"])
        pager = "".join(
            f'<a class="pager pager-{kind}" href="{rel(cur, docs[p]["cur"])}"><small>{label}</small><b>{esc(docs[p]["title"])}</b></a>'
            for kind, p, label in (
                ("prev", PAGES[i - 1] if i else None, f'← {t(lang, "docs.prev")}'),
                ("next", PAGES[i + 1] if i + 1 < len(PAGES) else None, f'{t(lang, "docs.next")} →'),
            )
            if p
        )
        main = fill(DOC, {
            "nav_label": esc(t(lang, "docs.nav_label")), "sidebar": sidebar, "content": d["html"],
            "pager": pager, "edit_url": f"{BLOB}docs/{'es/' if lang == 'es' else ''}{name}.md",
            "edit_label": t(lang, "docs.edit"), "toc_label": t(lang, "docs.toc"), "toc": toc,
        })
        paths = {code: f"{LANGS[code]['prefix']}docs/{name}" for code in LANGS}
        write_page(lang, paths, main, title=f'{d["title"]} · {t(lang, "docs.title_suffix")}',
                   description=d["description"], og_type="article", jsonld="", script="docs.js", docs_current=True)
    # /docs/ itself points at the first guide.
    first = f"{SITE}{LANGS[lang]['prefix']}docs/{PAGES[0]}/"
    (OUT / LANGS[lang]["prefix"] / "docs" / "index.html").write_text(
        f'<!doctype html>\n<meta charset="utf-8">\n<title>goopdl docs</title>\n<meta name="robots" content="noindex">\n'
        f'<link rel="canonical" href="{first}">\n<meta http-equiv="refresh" content="0; url={PAGES[0]}/">\n'
        f'<a href="{PAGES[0]}/">{PAGES[0]}</a>\n', encoding="utf-8")


def build_sitemap() -> None:
    pairs = [{code: LANGS[code]["prefix"].rstrip("/") for code in LANGS}]
    pairs += [{code: f"{LANGS[code]['prefix']}docs/{name}" for code in LANGS} for name in PAGES]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">']
    for paths in pairs:
        for code in LANGS:
            lines.append(f"  <url>\n    <loc>{url(paths[code])}</loc>")
            for hl, alt in (("en", "en"), ("es", "es"), ("x-default", "en")):
                lines.append(f'    <xhtml:link rel="alternate" hreflang="{hl}" href="{url(paths[alt])}"/>')
            lines.append("  </url>")
    lines.append("</urlset>\n")
    (OUT / "sitemap.xml").write_text("\n".join(lines), encoding="utf-8")


def build_llms_full() -> None:
    """Every English guide in one Markdown file, for LLM tools that read a single URL."""
    parts = ["# goopdl documentation\n\n> Full text of the goopdl docs. Index: " + SITE + "llms.txt\n"]
    for name in PAGES:
        text = (REPO / "docs" / f"{name}.md").read_text(encoding="utf-8")
        text = re.sub(r"^\*\*English\*\*.*\n+", "", text, count=1, flags=re.M)
        parts.append(f"\n---\n\nSource: {SITE}docs/{name}/\n\n{text.strip()}\n")
    (OUT / "llms-full.txt").write_text("".join(parts), encoding="utf-8")


# ── check ───────────────────────────────────────────────────────────────────
def check_links() -> list[str]:
    """Every relative href/src in the output must exist, and every #fragment must be an id there."""
    errors, ids = [], {}
    for page in sorted(OUT.rglob("*.html")):
        for ref in re.findall(r'\s(?:href|src|poster)="([^"]+)"', page.read_text(encoding="utf-8")):
            ref = html.unescape(ref)
            if re.match(r"[a-z][a-z0-9+.-]*:", ref, re.I):
                continue  # https:, mailto:, ...
            path, _, fragment = ref.partition("#")
            target = (page.parent / path.split("?")[0]).resolve() if path else page
            if target.is_dir():
                target /= "index.html"
            where = f"{page.relative_to(OUT)}: {ref}"
            if not target.exists():
                errors.append(f"{where} (missing file)")
            elif fragment and target.suffix == ".html":
                if target not in ids:
                    ids[target] = set(re.findall(r'\sid="([^"]+)"', target.read_text(encoding="utf-8")))
                if unquote(fragment) not in ids[target]:
                    errors.append(f"{where} (missing #{fragment})")
    return errors


def main() -> int:
    if OUT.exists():
        assert OUT.name == "_site"
        try:
            shutil.rmtree(OUT)
        except OSError as e:
            sys.exit(f"cannot clean {OUT}: {e}")
    OUT.mkdir()
    for name in STATIC:
        src = SRC / name
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, OUT / name)
    shutil.copytree(REPO / "docs", OUT / "docs")  # raw Markdown too: docs/*.md, docs/es/*.md
    for lang in LANGS:
        build_home(lang)
        build_docs(lang)
    build_sitemap()
    build_llms_full()

    unused = {k for k in set(STRINGS["es"]) - used_keys if not k.startswith("alert.")}  # all 5 GitHub alert kinds are kept
    if unused:
        print(f"warning: unused Spanish strings: {', '.join(sorted(unused))}")
    errors = check_links()
    for e in errors:
        print(f"broken link: {e}")
    pages = len(list(OUT.rglob("*.html")))
    print(f"built {pages} pages into {OUT.relative_to(REPO)}/" + (f", {len(errors)} broken links" if errors else ", links ok"))
    if errors:
        return 1
    if "--serve" in sys.argv:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=OUT)
        print("serving http://127.0.0.1:8765/ (Ctrl+C to stop)")
        http.server.ThreadingHTTPServer(("127.0.0.1", 8765), handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
