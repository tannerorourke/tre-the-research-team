"""Literature search across arXiv, Semantic Scholar, and Hugging Face papers.

Plain synchronous functions (workflow nodes wrap them in threads; the MCP
server wraps them for agent tool calls). No API keys required. Every searcher
is failure-tolerant: it returns an empty list plus a warning rather than
sinking a run because one index was down.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Callable, Optional

import httpx
from pydantic import BaseModel, Field

UA = {"User-Agent": "tre/0.1 (research assistant; contact: local user)"}
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class Paper(BaseModel):
    key: str
    source: str
    ext_id: str = ""
    title: str
    authors: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    abstract: str = ""
    url: str = ""
    citations: Optional[int] = None

    def brief(self, abstract_chars: int = 320) -> str:
        cite = f", cited {self.citations}" if self.citations is not None else ""
        ab = re.sub(r"\s+", " ", self.abstract)[:abstract_chars]
        return f"[{self.key}] {self.title} ({self.year or '?'}{cite}) — {ab}"


def dedupe_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())[:72]


# -- searchers ---------------------------------------------------------------

def arxiv_search(query: str, max_results: int = 12) -> list[Paper]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    with httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=True) as client:
        resp = client.get(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": f"all:{query}",
                "max_results": max_results,
                "sortBy": "relevance",
            },
        )
        resp.raise_for_status()
    root = ET.fromstring(resp.text)
    out: list[Paper] = []
    for entry in root.findall("a:entry", ns):
        title = re.sub(r"\s+", " ", (entry.findtext("a:title", "", ns) or "")).strip()
        if not title:
            continue
        raw_id = entry.findtext("a:id", "", ns) or ""
        arxiv_id = raw_id.rsplit("/abs/", 1)[-1]
        published = entry.findtext("a:published", "", ns) or ""
        out.append(Paper(
            key=dedupe_key(title),
            source="arxiv",
            ext_id=arxiv_id,
            title=title,
            authors=[a.findtext("a:name", "", ns) or "" for a in entry.findall("a:author", ns)][:8],
            year=int(published[:4]) if published[:4].isdigit() else None,
            abstract=re.sub(r"\s+", " ", (entry.findtext("a:summary", "", ns) or "")).strip(),
            url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else raw_id,
        ))
    return out


def semantic_scholar_search(query: str, max_results: int = 12) -> list[Paper]:
    with httpx.Client(timeout=TIMEOUT, headers=UA) as client:
        resp = client.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={
                "query": query,
                "limit": max_results,
                "fields": "title,abstract,year,authors,url,citationCount,externalIds",
            },
        )
        resp.raise_for_status()
    out: list[Paper] = []
    for item in resp.json().get("data", []) or []:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        ext = item.get("externalIds") or {}
        arxiv_id = ext.get("ArXiv") or ""
        out.append(Paper(
            key=dedupe_key(title),
            source="semantic_scholar",
            ext_id=arxiv_id or (item.get("paperId") or ""),
            title=title,
            authors=[a.get("name", "") for a in (item.get("authors") or [])][:8],
            year=item.get("year"),
            abstract=(item.get("abstract") or "").strip(),
            url=(f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else (item.get("url") or "")),
            citations=item.get("citationCount"),
        ))
    return out


def hf_papers_search(query: str, max_results: int = 12) -> list[Paper]:
    with httpx.Client(timeout=TIMEOUT, headers=UA) as client:
        resp = client.get("https://huggingface.co/api/papers/search", params={"q": query})
        resp.raise_for_status()
    out: list[Paper] = []
    for item in (resp.json() or [])[:max_results]:
        paper = item.get("paper", item) or {}
        title = (paper.get("title") or "").strip()
        if not title:
            continue
        pid = paper.get("id") or ""
        published = paper.get("publishedAt") or ""
        out.append(Paper(
            key=dedupe_key(title),
            source="hf_papers",
            ext_id=pid,
            title=title,
            authors=[a.get("name", "") for a in (paper.get("authors") or []) if isinstance(a, dict)][:8],
            year=int(published[:4]) if published[:4].isdigit() else None,
            abstract=(paper.get("summary") or "").strip(),
            url=f"https://huggingface.co/papers/{pid}" if pid else "",
        ))
    return out


SEARCHERS: dict[str, Callable[[str, int], list[Paper]]] = {
    "arxiv": arxiv_search,
    "semantic_scholar": semantic_scholar_search,
    "hf_papers": hf_papers_search,
}


def merge_dedupe(batches: list[list[Paper]]) -> list[Paper]:
    """Merge result batches, preferring the entry with more metadata."""
    best: dict[str, Paper] = {}
    for batch in batches:
        for paper in batch:
            cur = best.get(paper.key)
            if cur is None:
                best[paper.key] = paper
                continue
            score_new = (paper.citations or 0, len(paper.abstract), bool(paper.url))
            score_cur = (cur.citations or 0, len(cur.abstract), bool(cur.url))
            if score_new > score_cur:
                merged = paper.model_copy()
                merged.citations = paper.citations if paper.citations is not None else cur.citations
                if not merged.abstract:
                    merged.abstract = cur.abstract
                best[paper.key] = merged
    return sorted(best.values(), key=lambda p: (-(p.citations or 0), -(p.year or 0)))


# -- full text ---------------------------------------------------------------

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_ALL_TAGS = re.compile(r"<[^>]+>")


def fetch_paper_text(arxiv_id: str, max_chars: int = 30_000) -> str:
    """Best-effort full text via ar5iv HTML; falls back to the abstract page."""
    arxiv_id = arxiv_id.strip().split("v")[0] if re.match(r"^\d{4}\.\d{4,5}v\d+$", arxiv_id.strip()) else arxiv_id.strip()
    urls = [
        f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}",
        f"https://arxiv.org/abs/{arxiv_id}",
    ]
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), headers=UA, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = client.get(url)
                if resp.status_code != 200 or len(resp.text) < 500:
                    continue
                text = _TAG_RE.sub(" ", resp.text)
                text = _ALL_TAGS.sub(" ", text)
                text = html.unescape(text)
                text = re.sub(r"[ \t]+", " ", text)
                text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
                text = text.strip()
                if len(text) > 800:
                    marker = f"[source: {url}]\n"
                    return marker + text[: max_chars - len(marker)]
            except httpx.HTTPError:
                continue
    return ""
