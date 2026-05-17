import asyncio
import json as json_mod
import re
import time
from collections import defaultdict
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

from config import get_settings, PORTAL_CONFIG, KEY_CONCEPTS, ORG_ID
from gitbook_client import GitBookClient, GitBookAPIError, flatten_pages, extract_text_from_document

app = FastAPI(title="impact.com Docs Explorer")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# In-memory page-tree cache: space_id → (timestamp, flat_pages)
# Note: cache is keyed by space_id only — page content is the same regardless
# of which user's token fetched it. Tokens differ in API permissions, not data.
_page_cache: dict[str, tuple[float, list[dict]]] = {}
# In-memory revision cache: space_id → (timestamp, revision_id)
_revision_cache: dict[str, tuple[float, str]] = {}
CACHE_TTL = 300  # 5 minutes


def _get_client(token: str | None = None) -> GitBookClient:
    """Build a GitBookClient. Prefers the user's per-request token, falls
    back to the env token if set, otherwise raises 401."""
    s = get_settings()
    actual_token = (token or "").strip() or s.gitbook_api_token
    if not actual_token:
        raise HTTPException(
            status_code=401,
            detail="GitBook API token required. Set yours in the app's settings.",
        )
    return GitBookClient(token=actual_token, base_url=s.gitbook_base_url)


async def _get_revision(client: GitBookClient, space_id: str) -> str:
    now = time.time()
    if space_id in _revision_cache and now - _revision_cache[space_id][0] < CACHE_TTL:
        return _revision_cache[space_id][1]
    info = await client.get_space_info(space_id)
    revision_id = info.get("revision", "")
    if not revision_id:
        raise HTTPException(status_code=502, detail="Could not determine space revision")
    _revision_cache[space_id] = (now, revision_id)
    return revision_id


async def _get_cached_pages(client: GitBookClient, space_id: str) -> list[dict]:
    now = time.time()
    if space_id in _page_cache and now - _page_cache[space_id][0] < CACHE_TTL:
        return _page_cache[space_id][1]
    revision_id = await _get_revision(client, space_id)
    raw_pages = await client.get_revision_pages(space_id, revision_id)
    flat = flatten_pages(raw_pages)
    _page_cache[space_id] = (now, flat)
    return flat


@app.exception_handler(GitBookAPIError)
async def gitbook_error_handler(request: Request, exc: GitBookAPIError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


# ── Routes ──────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/portal-config")
async def portal_config():
    return {"portals": PORTAL_CONFIG, "concepts": KEY_CONCEPTS}


@app.get("/api/auth/check")
async def auth_check(x_gitbook_token: str | None = Header(default=None)):
    """Validate a GitBook token by making a minimal authenticated call."""
    try:
        client = _get_client(x_gitbook_token)
    except HTTPException as e:
        return {"valid": False, "detail": e.detail}
    # Try the first portal space; if the token works for one, it works for all.
    first_space = next(iter(PORTAL_CONFIG.values()))["sections"]["guides"]["space_id"]
    try:
        await client.get_space_info(first_space)
        return {"valid": True}
    except GitBookAPIError as e:
        return {"valid": False, "detail": e.message, "status": e.status_code}


@app.get("/api/space/{space_id}/pages")
async def get_pages(space_id: str, x_gitbook_token: str | None = Header(default=None)):
    client = _get_client(x_gitbook_token)
    pages = await _get_cached_pages(client, space_id)
    return {"space_id": space_id, "pages": pages}


@app.get("/api/space/{space_id}/page/{page_id}")
async def get_page(space_id: str, page_id: str, x_gitbook_token: str | None = Header(default=None)):
    client = _get_client(x_gitbook_token)
    revision_id = await _get_revision(client, space_id)
    try:
        data = await client.get_page_content(space_id, revision_id, page_id)
    except GitBookAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    document = data.get("document") or data.get("page", {}).get("document", {})
    title = data.get("title") or data.get("page", {}).get("title", "")
    path = data.get("path") or data.get("page", {}).get("path", "")

    if document:
        markdown = extract_text_from_document(document)
    else:
        markdown = f"# {title}\n\n_No content available for this page._"

    return {
        "page_id": page_id,
        "title": title,
        "path": path,
        "markdown": markdown,
    }


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1),
    space_id: str = Query(...),
    x_gitbook_token: str | None = Header(default=None),
):
    if not q.strip():
        return {"items": []}
    client = _get_client(x_gitbook_token)
    try:
        results = await client.search_space(space_id, q.strip())
    except GitBookAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    items = []
    for r in results:
        items.append({
            "id": r.get("id", ""),
            "title": r.get("title", ""),
            "path": r.get("path", ""),
            "excerpt": (r.get("body") or "")[:120],
        })
    return {"items": items}


@app.get("/api/ask")
async def ask_question(
    q: str = Query(..., min_length=1),
    portals: str = Query(default="brand,partner,agency"),
    x_gitbook_token: str | None = Header(default=None),
):
    """AI-powered Q&A over live GitBook content."""
    s = get_settings()
    client = _get_client(x_gitbook_token)
    question = q.strip()

    # Collect guide space IDs for the requested portals
    search_spaces: list[str] = []
    for pid in portals.split(","):
        pid = pid.strip()
        portal = PORTAL_CONFIG.get(pid)
        if portal:
            guides = portal["sections"].get("guides")
            if guides:
                search_spaces.append(guides["space_id"])
    if not search_spaces:
        search_spaces = [PORTAL_CONFIG["brand"]["sections"]["guides"]["space_id"]]

    # Search all requested spaces concurrently
    async def search_one(sid: str) -> tuple[str, list[dict]]:
        try:
            results = await client.search_space(sid, question)
            return sid, results
        except Exception:
            return sid, []

    search_pairs = await asyncio.gather(*[search_one(sid) for sid in search_spaces])

    # Fetch page content for top results (max 2 per space, 4 total)
    async def fetch_content(space_id: str, result: dict) -> dict | None:
        page_id = result.get("id", "")
        if not page_id:
            return None
        try:
            revision_id = await _get_revision(client, space_id)
            data = await client.get_page_content(space_id, revision_id, page_id)
            document = data.get("document") or data.get("page", {}).get("document", {})
            title = data.get("title") or data.get("page", {}).get("title") or result.get("title", "")
            path  = data.get("path")  or data.get("page", {}).get("path")  or result.get("path", "")
            content = extract_text_from_document(document)[:1800] if document else (result.get("body") or "")[:400]
            return {"title": title, "path": path, "content": content, "excerpt": (result.get("body") or "")[:120]}
        except Exception:
            return None

    fetch_tasks = []
    for space_id, results in search_pairs:
        for r in results[:2]:
            fetch_tasks.append(fetch_content(space_id, r))

    fetched = await asyncio.gather(*fetch_tasks)
    pages = [p for p in fetched if p is not None][:5]

    # If no Anthropic key — return rich search results only
    if not s.anthropic_api_key:
        return {
            "question": question,
            "ai_available": False,
            "pages": [{"title": p["title"], "path": p["path"], "excerpt": p["excerpt"]} for p in pages],
        }

    # Build documentation context
    ctx = "\n\n---\n\n".join(
        f"### {p['title']}\n{p['content']}" for p in pages
    ) or "No specific documentation found for this query."

    system = (
        "You are an expert on impact.com's affiliate partnership platform. "
        "Help developers and business users understand the platform's architecture, APIs, and economics. "
        "Be concise, practical, and ground every answer in the provided documentation context."
    )
    user_msg = f"""Question: {question}

Documentation context:
{ctx}

Respond ONLY with a valid JSON object using exactly this structure:
{{
  "summary": "1–2 sentence direct answer",
  "explanation": "deeper explanation in 2–3 short paragraphs (use \\n\\n to separate)",
  "key_points": ["point 1", "point 2", "point 3"],
  "developer_note": "specific API/technical note if relevant, otherwise empty string",
  "related_terms": ["Term1", "Term2", "Term3"]
}}"""

    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": s.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-20240307",
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user_msg}],
            },
        )

    if not resp.is_success:
        return {"question": question, "ai_available": False, "error": "AI service error",
                "pages": [{"title": p["title"], "path": p["path"], "excerpt": p["excerpt"]} for p in pages]}

    raw = resp.json()["content"][0]["text"]
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json_mod.loads(m.group()) if m else json_mod.loads(raw)
    except Exception:
        parsed = {"summary": raw[:300], "explanation": raw, "key_points": [], "developer_note": "", "related_terms": []}

    return {
        "question": question,
        "ai_available": True,
        "summary":        parsed.get("summary", ""),
        "explanation":    parsed.get("explanation", ""),
        "key_points":     parsed.get("key_points", []),
        "developer_note": parsed.get("developer_note", ""),
        "related_terms":  parsed.get("related_terms", []),
        "pages": [{"title": p["title"], "path": p["path"], "excerpt": p["excerpt"]} for p in pages],
    }


@app.get("/api/ask/status")
async def ask_status():
    """Returns whether AI-powered Ask is available."""
    s = get_settings()
    return {"ai_available": bool(s.anthropic_api_key)}


@app.get("/api/stats")
async def live_stats(x_gitbook_token: str | None = Header(default=None)):
    """Return page counts and last-updated dates for all portal spaces."""
    client = _get_client(x_gitbook_token)

    # Collect all space IDs across all portals
    space_ids: dict[str, str] = {}  # space_id → label
    for portal_id, portal in PORTAL_CONFIG.items():
        for section_id, section in portal["sections"].items():
            sid = section["space_id"]
            space_ids[sid] = f"{portal['label']} {section['label']}"

    async def fetch_space_stat(space_id: str) -> dict:
        try:
            info = await client.get_space_info(space_id)
            revision_id = info.get("revision", "")
            updated = info.get("updatedAt", "")
            page_count = 0
            if revision_id:
                pages = await _get_cached_pages(client, space_id)
                page_count = sum(1 for p in pages if p.get("kind") == "sheet")
            return {
                "space_id": space_id,
                "updated_at": updated[:10] if updated else None,
                "page_count": page_count,
            }
        except Exception:
            return {"space_id": space_id, "updated_at": None, "page_count": None}

    results = await asyncio.gather(*[fetch_space_stat(sid) for sid in space_ids])
    return {r["space_id"]: r for r in results}


# ── API SURFACE EXTRACTION ──────────────────────────────────────────────────
# Crawl all three API Reference spaces and derive a unified, comparable view
# of every REST endpoint — grouped by resource, showing portal coverage.

_HTTP_VERBS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
_NATURAL_VERBS = {
    "create": "POST", "add": "POST", "submit": "POST", "new": "POST", "post": "POST",
    "list": "GET", "get": "GET", "retrieve": "GET", "fetch": "GET", "view": "GET",
    "find": "GET", "show": "GET", "read": "GET", "lookup": "GET",
    "update": "PUT", "modify": "PUT", "edit": "PUT", "put": "PUT", "set": "PUT",
    "delete": "DELETE", "remove": "DELETE",
    "patch": "PATCH",
    "reverse": "POST", "cancel": "POST", "duplicate": "POST", "send": "POST",
    "search": "GET", "export": "GET", "download": "GET", "import": "POST", "upload": "POST",
}
_METHOD_ORDER = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}

# Titles that should NEVER be treated as endpoints even if they start with a verb
# (e.g. "Get Started" → not an API endpoint)
_NON_ENDPOINT_SUFFIXES = {
    "started", "going", "help", "feedback", "support", "in touch", "set up",
    "ready", "involved",
}
# Lowercase title contains one of these and we skip outright
_NON_ENDPOINT_KEYWORDS = (
    "overview", "introduction", "getting started", "quickstart", "quick start",
    "authentication", "authorization", "auth method", "credentials",
    "errors", "error codes", "error handling", "rate limit", "rate-limit",
    "changelog", "release notes", "deprecation", "deprecated", "migration",
    "welcome", "about ", "faq", "frequently asked",
    "what is", "what's new", "whats new", "intro to", "introducing",
    "guide ", "guides", "tutorial", "walkthrough", "best practice",
    "schema", "object reference", "data model", "glossary",
)


def _clean_natural_path(rest: str) -> str:
    """Strip leading articles and trailing punctuation from a natural-language path."""
    rest = rest.strip().rstrip(".,:;")
    lower = rest.lower()
    for article in ("a ", "an ", "the "):
        if lower.startswith(article):
            rest = rest[len(article):]
            break
    return rest


def _looks_like_endpoint_title(title: str) -> bool:
    """Heuristic to reject obvious non-endpoint pages before pattern-matching."""
    t = title.strip().lower()
    if len(t) < 3:
        return False
    # Reject if any rejection keyword appears in the title
    if any(kw in t for kw in _NON_ENDPOINT_KEYWORDS):
        return False
    return True


def _parse_endpoint(title: str, slug: str = "", path_url: str = "") -> dict | None:
    """Extract HTTP method + path from a page's title (or slug as fallback).
    Returns None if the page doesn't look like an endpoint."""
    if not title:
        return None
    title = title.strip()
    if not _looks_like_endpoint_title(title):
        return None
    tokens = title.split()
    if not tokens:
        return None
    first_raw = tokens[0].rstrip(":,.")

    # 1) Strict: title begins with EXACT-CASE HTTP verb (e.g. "GET /Conversions")
    if first_raw in _HTTP_VERBS:
        rest = " ".join(tokens[1:]).strip()
        endpoint_path = rest if rest.startswith("/") else ("/" + rest if rest else "/")
        return {"method": first_raw, "path": endpoint_path, "natural": False}

    # 2) Natural-language verb at start: "List Conversions", "Get a Conversion"
    first_lower = first_raw.lower()
    if first_lower in _NATURAL_VERBS:
        # Guard against "Get Started", "Get Help" etc. — common non-endpoint phrases
        rest_lower = " ".join(t.lower() for t in tokens[1:]).strip()
        if any(rest_lower == s or rest_lower.startswith(s + " ") for s in _NON_ENDPOINT_SUFFIXES):
            return None
        # The path text shouldn't read like a sentence
        if len(tokens) > 6:
            return None
        rest = _clean_natural_path(" ".join(tokens[1:]))
        if not rest:
            return None
        return {"method": _NATURAL_VERBS[first_lower], "path": rest, "natural": True}

    # 3) Slug fallback: page slugs like "get-conversions" or "post-action-detail"
    if slug:
        slug_parts = slug.split("-")
        if slug_parts:
            slug_first = slug_parts[0].lower()
            if slug_first in _NATURAL_VERBS and len(slug_parts) > 1 and len(slug_parts) <= 6:
                resource = " ".join(slug_parts[1:]).replace("_", " ")
                return {"method": _NATURAL_VERBS[slug_first], "path": resource, "natural": True}

    return None


def _is_resource_candidate(title: str) -> bool:
    """A page that *looks* like an API resource (not a meta/intro page)."""
    if not title or len(title.strip()) < 3:
        return False
    t = title.strip().lower()
    if any(kw in t for kw in _NON_ENDPOINT_KEYWORDS):
        return False
    # Skip the doc's own title (e.g. "Brand API Reference v13")
    if "reference" in t and ("v1" in t or "v2" in t or "v0" in t):
        return False
    # Skip overly long titles — resources are usually short noun phrases
    if len(title.split()) > 5:
        return False
    return True


@app.get("/api/api-surface")
async def api_surface(x_gitbook_token: str | None = Header(default=None)):
    """Crawl Brand/Partner/Agency API references and return resources with
    portal coverage. Each resource lists its subpages per portal so users can
    drill into the actual GitBook content."""
    client = _get_client(x_gitbook_token)

    portals = {
        "brand":   PORTAL_CONFIG["brand"]["sections"]["reference"]["space_id"],
        "partner": PORTAL_CONFIG["partner"]["sections"]["reference"]["space_id"],
        "agency":  PORTAL_CONFIG["agency"]["sections"]["reference"]["space_id"],
    }

    crawl_errors: list[str] = []
    diagnostics: dict[str, dict] = {}

    async def crawl_portal(portal: str, space_id: str) -> dict[str, dict]:
        """Return a dict of resource_title (lowercase) → {title, page_id, subpages: [...]}."""
        try:
            pages = await _get_cached_pages(client, space_id)
        except GitBookAPIError as e:
            crawl_errors.append(f"{portal}: {e.message}")
            diagnostics[portal] = {"pages": 0, "resources": 0, "error": e.message}
            return {}
        except Exception as e:
            crawl_errors.append(f"{portal}: {e}")
            diagnostics[portal] = {"pages": 0, "resources": 0, "error": str(e)}
            return {}

        resources: dict[str, dict] = {}      # key (lowercase title) → resource record
        # Map page_id → its resource key (so subpages know which resource they belong to)
        page_to_resource: dict[str, str] = {}
        # The "resource depth" is the shallowest depth at which any resource was found.
        # API refs typically have meta pages + a "resources" tree all at depth 0,
        # OR meta at depth 0 + resources at depth 1.

        # First pass: identify resource pages
        for page in pages:
            depth = page.get("depth", 0)
            title = (page.get("title") or "").strip()
            page_id = page.get("id")
            if depth > 1 or not page_id:
                continue
            if not _is_resource_candidate(title):
                continue
            key = title.lower()
            resources[key] = {
                "title": title,
                "page_id": page_id,
                "space_id": space_id,
                "portal": portal,
                "depth": depth,
                "subpages": [],
            }
            page_to_resource[page_id] = key

        # Second pass: collect subpages — every page whose nearest ancestor
        # at a shallower depth is one of our resources.
        parent_id_at_depth: dict[int, str | None] = {}
        for page in pages:
            depth = page.get("depth", 0)
            title = (page.get("title") or "").strip()
            page_id = page.get("id")

            parent_id_at_depth[depth] = page_id
            parent_id_at_depth = {d: pid for d, pid in parent_id_at_depth.items() if d <= depth}

            if not page_id or depth == 0:
                continue
            # Walk up parents to find a resource ancestor
            for d in sorted(parent_id_at_depth.keys(), reverse=True):
                if d >= depth:
                    continue
                parent_id = parent_id_at_depth[d]
                if parent_id and parent_id in page_to_resource:
                    resource_key = page_to_resource[parent_id]
                    if resource_key in resources and page_id != resources[resource_key]["page_id"]:
                        parsed = _parse_endpoint(title, page.get("slug") or "")
                        resources[resource_key]["subpages"].append({
                            "title": title,
                            "page_id": page_id,
                            "depth": depth,
                            "method": parsed["method"] if parsed else None,
                            "path": parsed["path"] if parsed else None,
                        })
                    break

        diagnostics[portal] = {
            "pages": len(pages),
            "resources": len(resources),
        }
        return resources

    portal_resources = await asyncio.gather(*[crawl_portal(p, sid) for p, sid in portals.items()])
    brand_res, partner_res, agency_res = portal_resources

    # If every portal failed → escalate
    if not any([brand_res, partner_res, agency_res]) and crawl_errors and len(crawl_errors) == len(portals):
        raise HTTPException(
            status_code=502,
            detail="Could not crawl API references: " + "; ".join(crawl_errors),
        )

    # Merge resources across portals by lowercase title
    merged: dict[str, dict] = {}
    for portal_name, portal_map in [("brand", brand_res), ("partner", partner_res), ("agency", agency_res)]:
        for key, res in portal_map.items():
            if key not in merged:
                merged[key] = {"name": res["title"], "portals": {"brand": None, "partner": None, "agency": None}}
            else:
                # Prefer the canonical title from the portal with the most subpages
                if len(res["subpages"]) > len(merged[key].get("_best_subpages", [])):
                    merged[key]["name"] = res["title"]
            merged[key]["portals"][portal_name] = res
            merged[key]["_best_subpages"] = max(
                merged[key].get("_best_subpages", []),
                res["subpages"],
                key=len,
            )

    # Sort: resources in more portals first, then by name
    def coverage_count(r):
        return sum(1 for p in r["portals"].values() if p is not None)
    resources_list = sorted(merged.values(), key=lambda r: (-coverage_count(r), r["name"].lower()))
    for r in resources_list:
        r.pop("_best_subpages", None)

    return {
        "total_resources": len(resources_list),
        "portal_totals": {
            p: sum(1 for r in resources_list if r["portals"][p])
            for p in ("brand", "partner", "agency")
        },
        "resources": resources_list,
        "diagnostics": diagnostics,
    }


# ── ENDPOINT EXTRACTION FROM PAGE CONTENT ──────────────────────────────────
# impact.com's API ref docs put the HTTP methods INSIDE each resource page
# (e.g. the "Conversions" page contains "GET /Conversions", "POST /Conversions"
# as section headings). To surface them we crawl the resource page's markdown.

# Patterns that recognise endpoint declarations inside markdown bodies.
_MD_HEADING_METHOD_RE = re.compile(
    r'^#{1,5}\s+(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(/\S+)',
    re.MULTILINE | re.IGNORECASE,
)
_MD_BOLD_METHOD_RE = re.compile(
    r'\*\*\s*(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s*\*\*\s+`?(/\S+?)`?(?=\s|$)',
    re.IGNORECASE,
)
_MD_CODE_METHOD_RE = re.compile(
    r'`\s*(GET|POST|PUT|DELETE|PATCH)\s+(/\S+?)\s*`',
    re.IGNORECASE,
)
_MD_CURL_METHOD_RE = re.compile(
    r'curl\s+(?:--?\w+\s+)*(?:-X\s+)?(GET|POST|PUT|DELETE|PATCH)\s+[\'"]?https?://[^/\s\'"]+(/\S+?)[\'"\s\n]',
    re.IGNORECASE,
)
# Line-start method declaration: "GET /Advertisers/{Id}/Companies"
_MD_LINE_METHOD_RE = re.compile(
    r'^\s*(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9{}/_\-]+)\s*$',
    re.MULTILINE,
)


def _extract_endpoints_from_markdown(markdown: str) -> list[dict]:
    """Pull HTTP endpoints out of a markdown body — heading, bold, inline-code,
    cURL or bare-line patterns."""
    if not markdown:
        return []
    found = []
    seen = set()

    def add(method: str, path: str):
        method = method.upper()
        path = path.rstrip('.,;:)]')
        # Skip obviously invalid paths
        if len(path) < 2 or not path.startswith('/'):
            return
        key = (method, path)
        if key in seen:
            return
        seen.add(key)
        found.append({"method": method, "path": path})

    for m in _MD_HEADING_METHOD_RE.finditer(markdown):
        add(m.group(1), m.group(2))
    for m in _MD_BOLD_METHOD_RE.finditer(markdown):
        add(m.group(1), m.group(2))
    for m in _MD_CODE_METHOD_RE.finditer(markdown):
        add(m.group(1), m.group(2))
    for m in _MD_CURL_METHOD_RE.finditer(markdown):
        add(m.group(1), m.group(2))
    for m in _MD_LINE_METHOD_RE.finditer(markdown):
        add(m.group(1), m.group(2))

    found.sort(key=lambda e: (_METHOD_ORDER.get(e["method"], 9), e["path"]))
    return found


# Cache extracted endpoints per page to avoid re-fetching content
_endpoint_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def _find_subpages(pages: list[dict], page_id: str) -> tuple[dict | None, list[dict]]:
    """Given a flat page list, return (the page with this id, its descendants).
    Descendants are every page that follows it in the list while their depth
    remains > this page's depth."""
    parent = None
    parent_idx = None
    for i, p in enumerate(pages):
        if p.get("id") == page_id:
            parent = p
            parent_idx = i
            break
    if parent is None:
        return None, []
    parent_depth = parent.get("depth", 0)
    children = []
    for p in pages[parent_idx + 1:]:
        d = p.get("depth", 0)
        if d <= parent_depth:
            break
        if p.get("id"):
            children.append(p)
    return parent, children


@app.get("/api/resource-endpoints")
async def resource_endpoints(
    space_id: str = Query(...),
    page_id: str = Query(...),
    x_gitbook_token: str | None = Header(default=None),
):
    """Fetch a resource page AND all its subpages, extract HTTP endpoints
    from each markdown body, and aggregate. impact.com's API ref docs put
    individual endpoint specs on subpages of each resource — so we have to
    crawl those, not just the resource page itself."""
    client = _get_client(x_gitbook_token)

    # Locate the resource + its subpages from the cached page list
    pages = await _get_cached_pages(client, space_id)
    parent, children = _find_subpages(pages, page_id)
    if parent is None:
        return {"endpoints": [], "pages_scanned": 0, "error": "page not found"}

    revision_id = await _get_revision(client, space_id)

    # Diagnostic accumulator — keep one short markdown sample so the frontend
    # can show "here's what the parser saw" if no endpoints were found.
    debug_sample: list[dict] = []

    async def fetch_and_parse(pid: str, title: str) -> list[dict]:
        cache_key = (space_id, pid)
        now = time.time()
        if cache_key in _endpoint_cache and now - _endpoint_cache[cache_key][0] < CACHE_TTL:
            cached = _endpoint_cache[cache_key][1]
            return [{**e, "source_page_id": pid, "source_title": title} for e in cached]
        try:
            data = await client.get_page_content(space_id, revision_id, pid)
        except Exception:
            return []
        document = data.get("document") or data.get("page", {}).get("document", {})
        if not document:
            _endpoint_cache[cache_key] = (now, [])
            return []
        markdown = extract_text_from_document(document)
        endpoints = _extract_endpoints_from_markdown(markdown)
        # Keep the first non-trivial markdown as a diagnostic sample
        if not endpoints and len(debug_sample) < 1 and len(markdown) > 100:
            debug_sample.append({
                "page_title": title,
                "markdown_preview": markdown[:1500],
            })
        _endpoint_cache[cache_key] = (now, endpoints)
        return [{**e, "source_page_id": pid, "source_title": title} for e in endpoints]

    # Fetch resource page + every descendant in parallel
    targets = [(parent["id"], parent.get("title", ""))] + [(c["id"], c.get("title", "")) for c in children]
    results = await asyncio.gather(*[fetch_and_parse(pid, t) for pid, t in targets])

    # Flatten, dedupe by (method, path) preferring the first occurrence (resource page first)
    combined: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for batch in results:
        for ep in batch:
            key = (ep["method"], ep["path"])
            if key in seen:
                continue
            seen.add(key)
            combined.append(ep)

    combined.sort(key=lambda e: (_METHOD_ORDER.get(e["method"], 9), e["path"]))
    return {
        "endpoints": combined,
        "pages_scanned": len(targets),
        "subpages": len(children),
        "debug_sample": debug_sample,
    }


# ── INTEGRATION PATTERN EXTRACTION ──────────────────────────────────────────
# Crawl the Integrations Hub and classify each documented page by its
# architectural pattern — webhook-based, SDK-driven, platform integration, etc.

_PATTERN_RULES = [
    # (pattern_id, label, icon, keywords)
    ("webhook",  "Webhook-based",       "🔗",  ["webhook", "callback", "event"]),
    ("sdk",      "SDK / Language",      "📦",  ["sdk", "python", "node", "javascript",
                                                 "java ", "php", "ruby", "go-sdk", "kotlin",
                                                 "swift", "library"]),
    ("mmp",      "Mobile Measurement",  "📱",  ["mmp", "appsflyer", "adjust", "branch.io",
                                                 "kochava", "singular", "tenjin",
                                                 "mobile measurement"]),
    ("platform", "Platform / eCommerce","🛍️",  ["shopify", "woocommerce", "salesforce",
                                                 "hubspot", "magento", "bigcommerce",
                                                 "wordpress", "wix", "squarespace",
                                                 "klaviyo", "stripe", "paypal"]),
    ("tag",      "Tag-based Tracking",  "🏷️",  ["tag manager", "gtm", "tag-based",
                                                 "tracking tag", " utt", "universal tracking",
                                                 "conversion tag", "pixel"]),
    ("api",      "REST API Integration","🔌",  ["api integration", "rest api", "graphql"]),
    ("recipe",   "Recipe / Tutorial",   "📖",  ["recipe", "tutorial", "how to ", "guide:",
                                                 "walkthrough", "quickstart"]),
]


def _classify_pattern(title: str, group: str = "", path: str = "") -> tuple[str, str, str]:
    """Return (pattern_id, label, icon)."""
    text = f"{title} {group} {path}".lower()
    for pattern_id, label, icon, keywords in _PATTERN_RULES:
        if any(kw in text for kw in keywords):
            return pattern_id, label, icon
    return "other", "Other Integration", "🧩"


@app.get("/api/hub-patterns")
async def hub_patterns(x_gitbook_token: str | None = Header(default=None)):
    """Extract integration patterns from the Integrations Hub space."""
    client = _get_client(x_gitbook_token)
    space_id = PORTAL_CONFIG["hub"]["sections"]["guides"]["space_id"]
    try:
        pages = await _get_cached_pages(client, space_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not crawl Integrations Hub: {e}")

    items: list[dict] = []
    group_by_depth: dict[int, str] = {}
    for page in pages:
        depth = page.get("depth", 0)
        title = (page.get("title") or "").strip()
        kind = page.get("kind") or ""
        if kind == "group" or not page.get("id"):
            group_by_depth[depth] = title
            group_by_depth = {d: g for d, g in group_by_depth.items() if d <= depth}
            continue
        if len(title) < 4:
            continue
        # Find nearest non-empty parent group
        parent_group = ""
        for d in sorted(group_by_depth.keys(), reverse=True):
            if d < depth and group_by_depth[d]:
                parent_group = group_by_depth[d]
                break
        pattern_id, label, icon = _classify_pattern(title, parent_group, page.get("path", ""))
        items.append({
            "title": title,
            "page_id": page.get("id"),
            "space_id": space_id,
            "path": page.get("path", ""),
            "group": parent_group,
            "depth": depth,
            "pattern": pattern_id,
            "pattern_label": label,
            "icon": icon,
        })

    # Group by pattern
    by_pattern: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_pattern[it["pattern"]].append(it)

    pattern_order = [r[0] for r in _PATTERN_RULES] + ["other"]
    groups: list[dict] = []
    for pid in pattern_order:
        if pid not in by_pattern:
            continue
        bucket = by_pattern[pid]
        label = next((r[1] for r in _PATTERN_RULES if r[0] == pid), "Other Integration")
        icon = next((r[2] for r in _PATTERN_RULES if r[0] == pid), "🧩")
        groups.append({
            "pattern": pid,
            "label": label,
            "icon": icon,
            "count": len(bucket),
            "items": bucket,
        })

    return {"total": len(items), "groups": groups}
