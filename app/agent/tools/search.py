"""Event and contextual search using Tavily."""
from __future__ import annotations
import os
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


async def search_events(city: str, interests: List[str], mood: str) -> Dict[str, Any]:
    """Search for events in the city using Tavily."""
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return {"events": [], "source": "none", "error": "No Tavily API key"}

    try:
        from tavily import AsyncTavilyClient
        client = AsyncTavilyClient(api_key=api_key)

        interest_str = ", ".join(interests[:3]) if interests else "entertainment"
        query = f"things to do this weekend in {city} {interest_str} events 2025"

        result = await client.search(
            query=query,
            search_depth="basic",
            max_results=5,
            include_answer=True,
        )

        events = []
        for r in result.get("results", []):
            events.append({
                "title": r.get("title", ""),
                "description": r.get("content", "")[:300],
                "url": r.get("url", ""),
                "source": r.get("url", "").split("/")[2] if r.get("url") else "",
            })

        answer = result.get("answer", "")
        return {
            "events": events,
            "summary": answer[:500] if answer else "",
            "source": "Tavily",
            "query": query,
        }
    except Exception as e:
        logger.warning("Tavily search failed: %s", e)
        return {
            "events": [],
            "source": "none",
            "error": str(e),
            "fallback_note": f"Search for events in {city} on Insider.in or BookMyShow for local events.",
        }
