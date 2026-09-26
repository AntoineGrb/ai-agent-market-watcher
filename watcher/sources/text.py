"""Extraction de texte brut depuis les formats des sources (HTML, PDF)."""

from __future__ import annotations

import io
import logging
import re

from bs4 import BeautifulSoup, Comment, Declaration, Doctype

log = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")
_BLOCK_TAGS = ["p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "section",
               "article", "header", "blockquote", "pre", "dd", "dt", "hr", "type", "sequence", "filename",
               "description"]   # les 4 derniers : en-tête SGML des documents EDGAR
_CELL_TAGS = ["td", "th"]
_BLANKS = re.compile(r"[ \t ]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_NON_CONTENT_TAGS = ("script", "style", "noscript", "head", "nav", "footer", "form", "svg")

# pypdf signale en WARNING des défauts de structure bénins (« Ignoring wrong pointing object ») : bruit dans les logs.
logging.getLogger("pypdf").setLevel(logging.ERROR)


def normalize(text: str) -> str:
    lines = (_BLANKS.sub(" ", line).strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(html: str | bytes) -> str:
    """Texte visible d'une page HTML, une ligne par bloc. L'encodage des octets est détecté par BeautifulSoup.

    Comme un navigateur : les blancs d'un nœud texte valent un espace, seuls les éléments de bloc (paragraphe,
    ligne de tableau...) coupent la ligne. Une balise en ligne (`<b>`, `<span>`) ne hache donc pas une phrase.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_NON_CONTENT_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, (Comment, Declaration, Doctype))):
        comment.extract()
    for node in soup.find_all(string=True):
        node.replace_with(_WHITESPACE.sub(" ", str(node)))
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for tag in soup.find_all(_CELL_TAGS):
        tag.insert_after(" ")
    return normalize(soup.get_text())


def article_text(html: str | bytes) -> str:
    """Corps d'un article de presse : paragraphes `<p>` seulement (menus, bandeaux et pieds de page exclus)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_NON_CONTENT_TAGS):
        tag.decompose()
    root = soup.find("article") or soup.body or soup
    paragraphs = (p.get_text(" ", strip=True) for p in root.find_all("p"))
    return normalize("\n\n".join(p for p in paragraphs if len(p) > 40))


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader   # import local : pypdf n'est utile qu'aux documents retenus au tri

    reader = PdfReader(io.BytesIO(data))
    return normalize("\n".join(page.extract_text() or "" for page in reader.pages))


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
