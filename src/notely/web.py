"""Web page fetching via Firecrawl."""

from __future__ import annotations

import os


def fetch_page(url: str) -> tuple[str, dict]:
    """Fetch URL via Firecrawl, return (markdown_content, metadata).

    metadata includes: title, description, sourceURL, etc.
    Raises ImportError if firecrawl-py not installed.
    Raises ValueError if FIRECRAWL_API_KEY not set.
    """
    try:
        from firecrawl import FirecrawlApp
    except ImportError:
        raise ImportError(
            "firecrawl-py not installed. Install: pip install firecrawl-py"
        )

    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        raise ValueError(
            "FIRECRAWL_API_KEY not set. Add it to .env or export it. "
            "Get a free key at https://firecrawl.dev"
        )

    client = FirecrawlApp(api_key=api_key)
    result = client.scrape_url(url, params={"formats": ["markdown"]})
    markdown = result.get("markdown", "")
    metadata = result.get("metadata", {})
    return markdown, metadata
