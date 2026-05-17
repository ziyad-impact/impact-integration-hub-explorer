import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PAGE_FETCH_SEMAPHORE = asyncio.Semaphore(10)


class GitBookAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"GitBook API error {status_code}: {message}")


class GitBookClient:
    def __init__(self, token: str, base_url: str):
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict | None = None) -> Any:
        try:
            resp = await client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise GitBookAPIError(503, "Request to GitBook API timed out") from exc

        if resp.status_code == 401:
            raise GitBookAPIError(401, "Invalid or expired API token")
        if resp.status_code == 403:
            raise GitBookAPIError(403, "Insufficient permissions")
        if resp.status_code == 404:
            raise GitBookAPIError(404, f"Resource not found: {path}")
        if not resp.is_success:
            body = resp.text[:200]
            raise GitBookAPIError(resp.status_code, f"Unexpected error: {body}")

        return resp.json()

    async def get_space_info(self, space_id: str) -> dict:
        async with self._make_client() as client:
            return await self._get(client, f"/spaces/{space_id}")

    async def get_revision_pages(self, space_id: str, revision_id: str) -> list[dict]:
        async with self._make_client() as client:
            data = await self._get(client, f"/spaces/{space_id}/revisions/{revision_id}")
        return data.get("pages", [])

    async def get_page_content(self, space_id: str, revision_id: str, page_id: str) -> dict:
        async with _PAGE_FETCH_SEMAPHORE:
            async with self._make_client() as client:
                return await self._get(
                    client,
                    f"/spaces/{space_id}/revisions/{revision_id}/page/{page_id}",
                )

    async def search_space(self, space_id: str, query: str) -> list[dict]:
        async with self._make_client() as client:
            data = await self._get(client, f"/spaces/{space_id}/search",
                                   {"query": query, "limit": 15})
        return data.get("items", [])


def flatten_pages(pages: list[dict], result: list[dict] | None = None, depth: int = 0) -> list[dict]:
    if result is None:
        result = []
    for page in pages:
        result.append({
            "id": page.get("id", ""),
            "title": page.get("title", "Untitled"),
            "path": page.get("path", ""),
            "slug": page.get("slug", ""),
            "kind": page.get("kind", ""),
            "depth": depth,
        })
        flatten_pages(page.get("pages", []), result, depth + 1)
    return result


def _get_block_text(node: dict) -> str:
    if node.get("object") == "text":
        return "".join(leaf.get("text", "") for leaf in node.get("leaves", []))
    return "".join(_get_block_text(child) for child in node.get("nodes", []))


_HEADING_MAP = {
    "heading-1": 1, "heading-2": 2, "heading-3": 3,
    "heading-4": 4, "heading-5": 5, "heading-6": 6,
    "heading-one": 1, "heading-two": 2, "heading-three": 3,
    "heading-four": 4, "heading-five": 5, "heading-six": 6,
}


def extract_text_from_document(document: dict) -> str:
    lines: list[str] = []

    def walk(node: dict | list, depth: int = 0) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, depth)
            return
        if not isinstance(node, dict):
            return

        obj = node.get("object", "")
        ntype = node.get("type", "")
        children = node.get("nodes", [])

        if obj in ("document",):
            walk(children, depth)
            return

        if ntype in _HEADING_MAP:
            text = _get_block_text(node).strip()
            if text:
                lines.append("#" * _HEADING_MAP[ntype] + " " + text)
                lines.append("")
            return

        if ntype == "paragraph":
            text = _get_block_text(node).strip()
            if text:
                lines.append(text)
                lines.append("")
            return

        if ntype in ("unordered-list",):
            for child in children:
                item_text = _get_block_text(child).strip()
                if item_text:
                    lines.append("- " + item_text)
            lines.append("")
            return

        if ntype in ("ordered-list",):
            for i, child in enumerate(children, 1):
                item_text = _get_block_text(child).strip()
                if item_text:
                    lines.append(f"{i}. " + item_text)
            lines.append("")
            return

        if ntype == "list-item":
            text = _get_block_text(node).strip()
            if text:
                lines.append("- " + text)
            return

        if ntype in ("code", "code-block"):
            text = _get_block_text(node).strip()
            if text:
                lang = node.get("data", {}).get("syntax", "")
                lines.append(f"```{lang}")
                lines.append(text)
                lines.append("```")
                lines.append("")
            return

        if ntype in ("blockquote", "hint", "quote"):
            text = _get_block_text(node).strip()
            if text:
                for line in text.splitlines():
                    lines.append("> " + line)
                lines.append("")
            return

        if ntype in ("divider", "horizontal-rule"):
            lines.append("---")
            lines.append("")
            return

        if ntype == "table":
            _walk_table(node, lines)
            return

        walk(children, depth + 1)

    def _walk_table(node: dict, lines: list[str]) -> None:
        rows = [c for c in node.get("nodes", []) if c.get("type") in ("table-row", "tr")]
        for i, row in enumerate(rows):
            cells = [c for c in row.get("nodes", []) if c.get("type") in ("table-cell", "table-header", "td", "th")]
            cell_texts = [_get_block_text(c).strip().replace("|", "\\|") for c in cells]
            lines.append("| " + " | ".join(cell_texts) + " |")
            if i == 0:
                lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
        lines.append("")

    root = document.get("document", document)
    walk(root)
    result, prev_blank = [], False
    for line in lines:
        is_blank = line.strip() == ""
        if is_blank and prev_blank:
            continue
        result.append(line)
        prev_blank = is_blank
    return "\n".join(result).strip()


def get_gitbook_client() -> GitBookClient:
    from config import get_settings
    s = get_settings()
    return GitBookClient(token=s.gitbook_api_token, base_url=s.gitbook_base_url)
