"""Activity options: Foursquare → Google Places → Tavily search → curated fallback."""
from __future__ import annotations
import httpx
import os
import re
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

FOURSQUARE_URL = "https://api.foursquare.com/v3/places/search"
GOOGLE_PLACES_NEW_URL = "https://places.googleapis.com/v1/places:searchText"

FS_CATEGORY_MAP = {
    "food": "13000", "music": "10032", "concert": "10032", "live music": "10032",
    "walks": "16032", "walking": "16032", "parks": "16032", "nature": "16000",
    "outdoors": "16000", "culture": "10027", "museum": "10028", "art": "10056",
    "history": "10027", "heritage": "10027", "sports": "18000", "fitness": "18000",
    "movies": "10024", "cinema": "10024", "shopping": "17000", "mall": "17069",
    "market": "17000", "adventure": "16000", "trekking": "16032", "hiking": "16032",
    "cafe": "13035", "coffee": "13035",
}

INTEREST_TO_QUERY = {
    "food": "popular food markets street food in {city}",
    "music": "live music venue concert hall in {city}",
    "walks": "parks gardens walking trails in {city}",
    "parks": "parks gardens in {city}",
    "culture": "museums art galleries cultural centres in {city}",
    "museum": "museums in {city}",
    "art": "art galleries in {city}",
    "sports": "sports complex stadium in {city}",
    "movies": "cinema multiplex in {city}",
    "shopping": "shopping mall market in {city}",
    "adventure": "adventure park outdoor activities in {city}",
    "nature": "nature park botanical garden in {city}",
    "coffee": "specialty coffee cafe in {city}",
    "hiking": "hiking trekking trails near {city}",
}

ACTIVITY_COST_ESTIMATE = {
    "park": 0, "garden": 30, "museum": 150, "art": 100, "gallery": 100,
    "cinema": 300, "mall": 0, "market": 100, "adventure": 800, "music": 500,
    "concert": 600, "cafe": 250, "coffee": 200, "sports": 300, "stadium": 200,
    "walk": 0, "trail": 0, "default": 150,
}

# Minimum spend per activity by budget tier — ensures high/premium budgets
# have realistic activity costs rather than free-museum defaults
BUDGET_ACTIVITY_FLOOR = {
    "low": 0, "medium": 0, "high": 400, "premium": 800,
}

GOOGLE_PRICE_LEVEL = {
    "PRICE_LEVEL_FREE": 0, "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2, "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}


async def get_activity_options(
    city: str,
    lat: float,
    lng: float,
    interests: List[str],
    budget_category: str,
    available_hours: float,
    crowd_sensitive: bool = False,
    max_results: int = 6,
) -> Dict[str, Any]:
    """
    Fetch activity options.
    Priority: Foursquare → Google Places → Tavily (web search) → curated.
    Sources are blended so the cost estimator always has 6+ real options.
    """
    cats = {FS_CATEGORY_MAP[i.lower()] for i in interests if i.lower() in FS_CATEGORY_MAP}
    if not cats:
        cats = {"16032", "10027", "13035"}

    sources_used = []
    activities: List[Dict] = []
    seen_names: set = set()

    def _add(items: List[Dict], source_label: str):
        added = 0
        for item in items:
            n = item.get("name", "").lower()
            if n and n not in seen_names:
                seen_names.add(n)
                activities.append(item)
                added += 1
        if added:
            sources_used.append(source_label)

    # 1. Foursquare
    fs_results = await _fetch_foursquare(lat, lng, ",".join(cats), budget_category, max_results)
    _add(fs_results, "Foursquare")

    # 2. Google Places
    gp_results = await _fetch_google_places_activities(city, interests, budget_category, max_results)
    _add(gp_results, "Google Places")

    # 3. Tavily web search — real venue names from the web, works for any city
    if len(activities) < max_results:
        tv_results = await _fetch_tavily_activities(city, interests, budget_category, max_results)
        _add(tv_results, "Tavily")

    # 4. Curated fallback — always backfill any interest that has 0 representation
    activity_interests_nonfood = [i for i in interests if i.lower() != "food"]
    represented = {a.get("category", "").lower() for a in activities}
    missing_interests = [i for i in activity_interests_nonfood if i.lower() not in represented]
    if missing_interests or len(activities) < 3:
        backfill_for = missing_interests if missing_interests else activity_interests_nonfood
        curated = _curated_activities(city, backfill_for, budget_category)
        _add(curated, "Curated")

    if crowd_sensitive:
        activities = sorted(activities, key=lambda a: a.get("popularity", 0.5))

    source_label = " + ".join(sources_used) if sources_used else "Curated"
    return {
        "activities": activities[:max_results],
        "source": source_label,
        "count": len(activities[:max_results]),
        "interests_matched": interests,
    }


# ─── Foursquare ───────────────────────────────────────────────────────────────

async def _fetch_foursquare(lat, lng, categories, budget_category, limit) -> List[Dict]:
    api_key = os.getenv("FOURSQUARE_API_KEY", "")
    if not api_key:
        return []
    try:
        params = {"ll": f"{lat},{lng}", "categories": categories, "limit": limit, "sort": "POPULARITY", "radius": 10000}
        headers = {"Authorization": api_key, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(FOURSQUARE_URL, params=params, headers=headers)
            if resp.status_code == 401:
                logger.warning("Foursquare 401 — API key invalid or expired")
                return []
            if resp.status_code != 200:
                logger.warning("Foursquare %s", resp.status_code)
                return []
            data = resp.json()
        results = []
        for place in data.get("results", []):
            name = place.get("name", "")
            if not name:
                continue
            cats = [c.get("name", "") for c in place.get("categories", [])]
            category = cats[0] if cats else "Activity"
            loc = place.get("location", {})
            address = loc.get("formatted_address", "") or loc.get("address", "")
            locality = loc.get("locality", "") or loc.get("neighborhood", "")
            display_loc = f"{name}, {locality}" if locality else name
            est_cost = _estimate_cost(category, budget_category)
            results.append({
                "name": name,
                "category": category,
                "location": display_loc,
                "address": address,
                "estimated_cost": est_cost,
                "popularity": place.get("popularity", 0.5),
                "maps_url": f"https://maps.google.com/?q={name.replace(' ', '+')}+{locality.replace(' ', '+')}",
                "source": "Foursquare",
            })
        logger.info("Foursquare: %d activities", len(results))
        return results
    except Exception as e:
        logger.warning("Foursquare failed: %s", e)
        return []


# ─── Google Places New API ────────────────────────────────────────────────────

async def _fetch_google_places_activities(city: str, interests: List[str], budget_category: str, limit: int) -> List[Dict]:
    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        return []

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.rating,places.priceLevel,places.googleMapsUri,places.types",
    }

    results = []
    seen = set()
    # "food" interest is fully handled by get_food_options — skip it here
    activity_interests = [i for i in interests if i.lower() != "food"]
    # Spread the result cap evenly across interests so no single interest dominates
    per_interest_cap = max(2, limit // max(len(activity_interests), 1))
    for interest in activity_interests[:4]:
        query_tpl = INTEREST_TO_QUERY.get(interest.lower(), f"{interest} activities in {{city}}")
        query = query_tpl.format(city=city)
        body = {"textQuery": query, "maxResultCount": 5, "languageCode": "en"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(GOOGLE_PLACES_NEW_URL, json=body, headers=headers)
                if resp.status_code != 200:
                    logger.warning("Google Places activities: %s %s", resp.status_code, resp.text[:100])
                    continue
                data = resp.json()
            added_for_interest = 0
            places_found = data.get("places", [])
            logger.info("Google Places '%s' in %s: %d raw results", interest, city, len(places_found))
            for place in places_found:
                if added_for_interest >= per_interest_cap:
                    break
                name = place.get("displayName", {}).get("text", "")
                if not name or name in seen:
                    continue
                seen.add(name)
                category = interest.title()
                est_cost = _estimate_cost(category, budget_category)
                results.append({
                    "name": name,
                    "category": category,
                    "location": place.get("formattedAddress", city),
                    "address": place.get("formattedAddress", ""),
                    "estimated_cost": est_cost,
                    "rating": place.get("rating", "N/A"),
                    "popularity": 0.5,
                    "maps_url": place.get("googleMapsUri", f"https://maps.google.com/?q={name.replace(' ', '+')}+{city}"),
                    "source": "Google Places",
                })
                added_for_interest += 1
        except Exception as e:
            logger.warning("Google Places activity fetch for '%s' failed: %s", interest, e)

    logger.info("Google Places activities: %d results", len(results))
    return results[:limit]


# ─── Tavily web search ────────────────────────────────────────────────────────

TAVILY_QUERIES = {
    "food":     "best food markets street food places to eat in {city}",
    "music":    "best live music venues concerts bars in {city}",
    "walks":    "best parks gardens walking trails in {city}",
    "parks":    "best parks gardens in {city}",
    "culture":  "best museums heritage monuments historical sites in {city}",
    "museum":   "top museums to visit in {city}",
    "art":      "best art galleries exhibitions in {city}",
    "history":  "best historical places monuments in {city}",
    "sports":   "best sports stadiums arenas in {city}",
    "movies":   "best cinema multiplexes IMAX in {city}",
    "shopping": "best shopping malls markets bazaars in {city}",
    "adventure":"best adventure parks outdoor activities in {city}",
    "nature":   "best nature parks wildlife sanctuaries in {city}",
    "coffee":   "best specialty coffee cafes roasteries in {city}",
    "hiking":   "best hiking trekking spots near {city}",
}


async def _fetch_tavily_activities(city: str, interests: List[str], budget_category: str, limit: int) -> List[Dict]:
    """
    Search Tavily for real venue names in any city.
    Uses include_answer=True to get a named list from the LLM summary,
    and also scans individual result pages that look like venue pages.
    """
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return []

    try:
        from tavily import AsyncTavilyClient
        client = AsyncTavilyClient(api_key=api_key)
    except ImportError:
        logger.warning("Tavily client not installed")
        return []

    # Collect results per interest, then interleave so no single interest dominates
    per_interest: Dict[str, List[Dict]] = {}
    seen: set = set()

    activity_interests_tv = [i for i in interests if i.lower() != "food"]
    per_interest_cap = max(2, limit // max(len(activity_interests_tv), 1))

    import asyncio
    for interest in activity_interests_tv[:3]:
        query_tpl = TAVILY_QUERIES.get(interest.lower(), f"best {interest} places in {{city}}")
        query = query_tpl.format(city=city)
        interest_results: List[Dict] = []
        try:
            response = await asyncio.wait_for(
                client.search(
                    query=query,
                    search_depth="basic",
                    max_results=6,
                    include_answer=True,
                ),
                timeout=12,
            )

            est_cost = _estimate_cost(interest, budget_category)

            # 1. Parse venue names from the AI-generated answer (most reliable)
            answer = response.get("answer", "") or ""
            answer_venues = _extract_venues_from_text(answer, city)
            for name in answer_venues:
                if len(interest_results) >= per_interest_cap:
                    break
                if name.lower() not in seen:
                    seen.add(name.lower())
                    interest_results.append(_make_activity(name, interest, city, est_cost))

            # 2. Check each search result page
            for r in response.get("results", []):
                if len(interest_results) >= per_interest_cap:
                    break
                title = r.get("title", "")
                content = r.get("content", "")

                if _is_venue_page(title, city):
                    name = _clean_venue_title(title, city)
                    if name and name.lower() not in seen and len(name) >= 4:
                        seen.add(name.lower())
                        interest_results.append(_make_activity(name, interest, city, est_cost))
                else:
                    for name in _extract_venues_from_text(content, city)[:2]:
                        if len(interest_results) >= per_interest_cap:
                            break
                        if name.lower() not in seen:
                            seen.add(name.lower())
                            interest_results.append(_make_activity(name, interest, city, est_cost))

            per_interest[interest] = interest_results
            logger.info("Tavily '%s' %s: %d venues", interest, city, len(interest_results))
        except Exception as e:
            logger.warning("Tavily activity search failed for '%s': %s", interest, e)

    # Interleave results across interests so all get representation
    results: List[Dict] = []
    queues = [q for q in per_interest.values() if q]
    while queues and len(results) < limit:
        for q in list(queues):
            if len(results) >= limit:
                break
            if q:
                results.append(q.pop(0))
            if not q:
                queues.remove(q)

    return results


def _make_activity(name: str, interest: str, city: str, est_cost: float) -> Dict:
    return {
        "name": name,
        "category": interest.title(),
        "location": city,
        "address": city,
        "estimated_cost": est_cost,
        "popularity": 0.65,
        "rating": "",
        "maps_url": f"https://maps.google.com/?q={name.replace(' ', '+')}+{city}",
        "source": "Tavily",
    }


def _is_venue_page(title: str, city: str) -> bool:
    """Return True if the page title looks like a specific venue page, not a list article."""
    list_patterns = [r"^\d+\s", r"\b(best|top|amazing|famous|must.visit|guide|list|places|things to do)\b"]
    for p in list_patterns:
        if re.search(p, title, re.IGNORECASE):
            return False
    return True


def _clean_venue_title(title: str, city: str) -> str:
    """Strip review-site noise from a venue page title."""
    noise = ["TripAdvisor", "Google", "Wikipedia", "Yelp", "Zomato", "MakeMyTrip",
             "Booking.com", "Justdial", "Sulekha", "Reviews", "Official Site"]
    for n in noise:
        title = re.sub(rf"\s*[|\-–]\s*{re.escape(n)}.*", "", title, flags=re.IGNORECASE)
    title = re.sub(rf"\s*[|\-–]\s*{re.escape(city)}.*", "", title, flags=re.IGNORECASE)
    title = re.split(r"\s*[-|–]\s*", title)[0].strip().rstrip(".,;:")
    return title[:60]


def _extract_venues_from_text(text: str, city: str) -> List[str]:
    """
    Extract individual venue names from an answer snippet or content paragraph.
    Looks for numbered list items, bullet points, and 'include X, Y, Z' patterns.
    """
    if not text:
        return []
    venues = []

    # Pattern 1: Numbered list  "1. Venue Name" or "1) Venue Name"
    for m in re.finditer(r"\d+[.)]\s+([A-Z][^.\n,;!?]{3,50}?)(?:\s*[-–,]|\s*\n|$)", text):
        venues.append(m.group(1).strip())

    # Pattern 2: Bullet / dash list  "- Venue Name" or "• Venue Name"
    for m in re.finditer(r"[•\-\*]\s+([A-Z][^.\n,;!?]{3,50}?)(?:\s*[-–,]|\s*\n|$)", text):
        venues.append(m.group(1).strip())

    # Pattern 3: "include/includes/such as X, Y and Z"
    inc = re.search(r"(?:include|includes|such as|like|namely)\s+(.+?)(?:\.|$)", text, re.IGNORECASE)
    if inc:
        chunk = inc.group(1)
        for part in re.split(r",\s*| and | & ", chunk):
            part = part.strip().rstrip(".,;:")
            if 4 <= len(part) <= 60 and part[0].isupper():
                venues.append(part)

    seen_local: set = set()
    clean = []
    for v in venues:
        v = v.strip().rstrip(".,;:")
        if _is_valid_venue_name(v) and v.lower() not in seen_local:
            seen_local.add(v.lower())
            clean.append(v)

    return clean[:5]


def _is_valid_venue_name(name: str) -> bool:
    """Return True if the string looks like a real venue name, not web noise."""
    if not name or len(name) < 5 or len(name) > 60:
        return False
    # Must start with a capital letter
    if not name[0].isupper():
        return False
    # Must have at least 2 characters that are letters
    if sum(1 for c in name if c.isalpha()) < 4:
        return False
    # Reject strings with bullet chars or pipe chars
    if any(c in name for c in ("•", "|", "–", "\n", "\t")):
        return False
    # Reject common web artifacts and generic category labels
    # Indian city names — reject if the entire name is just a city
    _CITIES = {"bangalore", "bengaluru", "hyderabad", "mumbai", "delhi", "chennai",
               "pune", "kolkata", "goa", "jaipur", "ahmedabad", "surat", "secunderabad"}
    noise = ["mentioned on", "visit ", "see all", "read more", "click here",
             "top rated", "best of", "things to do", "places to", "check out",
             "hotels", "hotel ", "hostel", "buildings", "landmarks in ", "sites in ",
             "worst", "terrible", "avoid", "not recommended", "block ", " block",
             "venues in ", "parks in ", "cafes in ", "bars in ", "places in ",
             "activities in ", "things in "]
    name_lower = name.lower()
    if name_lower.strip() in _CITIES:
        return False
    if any(n in name_lower for n in noise):
        return False
    # Reject neighbourhood/area names (end with Block, Nagar, Road, Layout, Colony etc.)
    area_suffixes = [" block", " nagar", " road", " layout", " colony", " area", " district", " zone"]
    if any(name_lower.endswith(s) for s in area_suffixes):
        return False
    # Reject if it ends with a preposition (truncated phrase like "Live Music Venues in")
    if name_lower.rstrip().endswith((" in", " at", " of", " for", " to", " the", " and")):
        return False
    # Reject if it looks like a sentence (has a verb at the start)
    verb_starts = ["is ", "are ", "was ", "has ", "have ", "get ", "find ", "go ", "make "]
    if any(name_lower.startswith(v) for v in verb_starts):
        return False
    # Must contain at least one word that's not a common stopword
    skip_solo = {"the", "a", "an", "in", "at", "on", "of", "and", "or"}
    words = [w.lower() for w in name.split() if w.lower() not in skip_solo]
    if not words:
        return False
    return True


_FLOOR_EXEMPT = {"cinema", "mall", "market", "park", "garden", "walk", "trail", "movies", "shopping"}

def _estimate_cost(category: str, budget_category: str) -> float:
    cat_lower = category.lower()
    base = ACTIVITY_COST_ESTIMATE["default"]
    for key, cost in ACTIVITY_COST_ESTIMATE.items():
        if key in cat_lower:
            base = cost
            break
    multiplier = {"low": 0.5, "medium": 1.0, "high": 1.5, "premium": 2.5}.get(budget_category, 1.0)
    estimated = round(base * multiplier, 0)
    # Budget floor lifts free-park defaults for premium — but not categories with natural price caps
    exempt = any(k in cat_lower for k in _FLOOR_EXEMPT)
    if not exempt:
        floor = BUDGET_ACTIVITY_FLOOR.get(budget_category, 0)
        estimated = max(estimated, floor)
    return estimated


# ─── Curated fallback ─────────────────────────────────────────────────────────

def _curated_activities(city: str, interests: List[str], budget_category: str) -> List[Dict]:
    city_lower = city.lower()
    is_blr = "bangalore" in city_lower or "bengaluru" in city_lower
    is_hyd = "hyderabad" in city_lower or "secunderabad" in city_lower
    is_mum = "mumbai" in city_lower or "bombay" in city_lower
    is_del = "delhi" in city_lower or "new delhi" in city_lower

    blr_pool = {
        "walks": [
            {"name": "Cubbon Park", "category": "Park", "location": "Cubbon Park, Central Bangalore", "estimated_cost": 0},
            {"name": "Lalbagh Botanical Garden", "category": "Garden", "location": "Lalbagh, Bangalore", "estimated_cost": 30},
            {"name": "Ulsoor Lake", "category": "Nature Walk", "location": "Ulsoor, Bangalore", "estimated_cost": 0},
        ],
        "culture": [
            {"name": "Bangalore Palace", "category": "Heritage", "location": "Vasanth Nagar, Bangalore", "estimated_cost": 230},
            {"name": "Visvesvaraya Museum", "category": "Museum", "location": "Kasturba Road, Bangalore", "estimated_cost": 60},
            {"name": "National Gallery of Modern Art", "category": "Art Gallery", "location": "Palace Road, Bangalore", "estimated_cost": 20},
        ],
        "music": [
            {"name": "Hard Rock Cafe", "category": "Live Music", "location": "MG Road, Bangalore", "estimated_cost": 800},
            {"name": "Windmills Craftworks", "category": "Music Venue", "location": "Whitefield, Bangalore", "estimated_cost": 600},
            {"name": "Vapour Pub & Brewery", "category": "Music Bar", "location": "Indiranagar, Bangalore", "estimated_cost": 700},
        ],
        "shopping": [
            {"name": "Commercial Street", "category": "Market", "location": "Commercial Street, Bangalore", "estimated_cost": 0},
            {"name": "Chickpet Market", "category": "Market", "location": "Chickpet, Bangalore", "estimated_cost": 0},
            {"name": "Phoenix Marketcity", "category": "Mall", "location": "Whitefield, Bangalore", "estimated_cost": 0},
        ],
        "movies": [
            {"name": "PVR IMAX Forum Mall", "category": "Cinema", "location": "Koramangala, Bangalore", "estimated_cost": 350},
            {"name": "INOX Garuda Mall", "category": "Cinema", "location": "MG Road, Bangalore", "estimated_cost": 280},
        ],
        "food": [
            {"name": "VV Puram Food Street", "category": "Food Street", "location": "VV Puram, Bangalore", "estimated_cost": 200},
            {"name": "Church Street Social", "category": "Cafe", "location": "Church Street, Bangalore", "estimated_cost": 600},
        ],
        "art": [
            {"name": "Rangoli Metro Art Center", "category": "Art Gallery", "location": "MG Road, Bangalore", "estimated_cost": 0},
            {"name": "Chitrakala Parishath", "category": "Art Gallery", "location": "Kumaraswamy Layout, Bangalore", "estimated_cost": 50},
        ],
        "nature": [
            {"name": "Bannerghatta National Park", "category": "Nature", "location": "Bannerghatta, Bangalore", "estimated_cost": 250},
            {"name": "Lalbagh Botanical Garden", "category": "Garden", "location": "Lalbagh, Bangalore", "estimated_cost": 30},
        ],
        "coffee": [
            {"name": "Blue Tokai Coffee", "category": "Specialty Cafe", "location": "Indiranagar, Bangalore", "estimated_cost": 300},
            {"name": "Matteo Coffea", "category": "Specialty Cafe", "location": "Church Street, Bangalore", "estimated_cost": 350},
            {"name": "Third Wave Coffee", "category": "Specialty Cafe", "location": "Koramangala, Bangalore", "estimated_cost": 300},
        ],
    }

    hyd_pool = {
        "walks": [
            {"name": "KBR National Park", "category": "Park", "location": "Jubilee Hills, Hyderabad", "estimated_cost": 25},
            {"name": "Hussain Sagar Lake Walk", "category": "Nature Walk", "location": "Hussain Sagar, Hyderabad", "estimated_cost": 0},
            {"name": "Sanjeevaiah Park", "category": "Park", "location": "Necklace Road, Hyderabad", "estimated_cost": 30},
        ],
        "culture": [
            {"name": "Charminar", "category": "Heritage", "location": "Old City, Hyderabad", "estimated_cost": 25},
            {"name": "Salar Jung Museum", "category": "Museum", "location": "Salar Jung Road, Hyderabad", "estimated_cost": 50},
            {"name": "Golconda Fort", "category": "Heritage", "location": "Ibrahim Bagh, Hyderabad", "estimated_cost": 100},
            {"name": "Qutb Shahi Tombs", "category": "Heritage", "location": "Ibrahim Bagh, Hyderabad", "estimated_cost": 25},
        ],
        "music": [
            {"name": "Hard Rock Cafe Hyderabad", "category": "Live Music", "location": "Banjara Hills, Hyderabad", "estimated_cost": 800},
            {"name": "10 Downing Street", "category": "Music Bar", "location": "Begumpet, Hyderabad", "estimated_cost": 600},
        ],
        "shopping": [
            {"name": "Laad Bazaar", "category": "Market", "location": "Charminar, Hyderabad", "estimated_cost": 0},
            {"name": "Shilparamam Crafts Village", "category": "Market", "location": "Hi-Tech City, Hyderabad", "estimated_cost": 40},
            {"name": "GVK One Mall", "category": "Mall", "location": "Banjara Hills, Hyderabad", "estimated_cost": 0},
        ],
        "movies": [
            {"name": "PVR IMAX Inorbit Mall", "category": "Cinema", "location": "Hitech City, Hyderabad", "estimated_cost": 350},
            {"name": "Cinepolis Forum Sujana", "category": "Cinema", "location": "Kukatpally, Hyderabad", "estimated_cost": 280},
        ],
        "food": [
            {"name": "Eat Street Necklace Road", "category": "Food Street", "location": "Necklace Road, Hyderabad", "estimated_cost": 300},
            {"name": "Mehdipatnam Food Street", "category": "Food Street", "location": "Mehdipatnam, Hyderabad", "estimated_cost": 200},
        ],
        "nature": [
            {"name": "KBR National Park", "category": "Nature Park", "location": "Jubilee Hills, Hyderabad", "estimated_cost": 25},
            {"name": "Nehru Zoological Park", "category": "Zoo", "location": "Bahadurpura, Hyderabad", "estimated_cost": 50},
            {"name": "Botanical Garden HDMC", "category": "Garden", "location": "Kothapet, Hyderabad", "estimated_cost": 20},
        ],
        "adventure": [
            {"name": "Wonderla Amusement Park", "category": "Adventure", "location": "Ranga Reddy, Hyderabad", "estimated_cost": 900},
            {"name": "Ramoji Film City", "category": "Adventure Park", "location": "Hayathnagar, Hyderabad", "estimated_cost": 1200},
            {"name": "Snow World Hyderabad", "category": "Adventure", "location": "Lower Tank Bund, Hyderabad", "estimated_cost": 700},
        ],
        "coffee": [
            {"name": "Third Wave Coffee Hyderabad", "category": "Specialty Cafe", "location": "Banjara Hills, Hyderabad", "estimated_cost": 350},
            {"name": "Roastery Coffee House", "category": "Specialty Cafe", "location": "Jubilee Hills, Hyderabad", "estimated_cost": 400},
            {"name": "The Black Baza Coffee", "category": "Specialty Cafe", "location": "Banjara Hills, Hyderabad", "estimated_cost": 350},
        ],
        "art": [
            {"name": "Salar Jung Museum", "category": "Museum & Art", "location": "Salar Jung Road, Hyderabad", "estimated_cost": 50},
            {"name": "Shilparamam Art & Crafts", "category": "Art", "location": "Hi-Tech City, Hyderabad", "estimated_cost": 40},
        ],
        "parks": [
            {"name": "KBR National Park", "category": "Park", "location": "Jubilee Hills, Hyderabad", "estimated_cost": 25},
            {"name": "Sanjeevaiah Park", "category": "Park", "location": "Necklace Road, Hyderabad", "estimated_cost": 30},
        ],
    }

    mum_pool = {
        "walks": [
            {"name": "Marine Drive Promenade", "category": "Nature Walk", "location": "Marine Drive, Mumbai", "estimated_cost": 0},
            {"name": "Bandra Bandstand", "category": "Nature Walk", "location": "Bandra, Mumbai", "estimated_cost": 0},
        ],
        "culture": [
            {"name": "Chhatrapati Shivaji Maharaj Vastu Sangrahalaya", "category": "Museum", "location": "Fort, Mumbai", "estimated_cost": 85},
            {"name": "Elephanta Caves", "category": "Heritage", "location": "Elephanta Island, Mumbai", "estimated_cost": 40},
            {"name": "Gateway of India", "category": "Heritage", "location": "Colaba, Mumbai", "estimated_cost": 0},
        ],
        "nature": [
            {"name": "Sanjay Gandhi National Park", "category": "Nature Park", "location": "Borivali, Mumbai", "estimated_cost": 53},
            {"name": "Powai Lake", "category": "Nature Walk", "location": "Powai, Mumbai", "estimated_cost": 0},
        ],
        "adventure": [
            {"name": "Essel World", "category": "Adventure Park", "location": "Gorai, Mumbai", "estimated_cost": 800},
            {"name": "Aquamagica Water Park", "category": "Adventure", "location": "Gorai, Mumbai", "estimated_cost": 700},
        ],
        "coffee": [
            {"name": "Starbucks Reserve Bandra", "category": "Specialty Cafe", "location": "Bandra, Mumbai", "estimated_cost": 500},
            {"name": "Azalea by Palladium", "category": "Specialty Cafe", "location": "Lower Parel, Mumbai", "estimated_cost": 600},
        ],
        "shopping": [
            {"name": "Colaba Causeway", "category": "Market", "location": "Colaba, Mumbai", "estimated_cost": 0},
            {"name": "Linking Road", "category": "Market", "location": "Bandra, Mumbai", "estimated_cost": 0},
        ],
        "music": [
            {"name": "Blue Frog Mumbai", "category": "Live Music", "location": "Lower Parel, Mumbai", "estimated_cost": 800},
        ],
        "movies": [
            {"name": "PVR Icon Mumbai", "category": "Cinema", "location": "Andheri, Mumbai", "estimated_cost": 400},
        ],
        "art": [
            {"name": "Jehangir Art Gallery", "category": "Art Gallery", "location": "Fort, Mumbai", "estimated_cost": 0},
        ],
        "food": [
            {"name": "Mohammed Ali Road Food Street", "category": "Food Street", "location": "Mohammed Ali Road, Mumbai", "estimated_cost": 300},
        ],
    }

    del_pool = {
        "walks": [
            {"name": "Lodhi Garden", "category": "Garden", "location": "Lodhi Colony, Delhi", "estimated_cost": 0},
            {"name": "India Gate Lawns", "category": "Park", "location": "Rajpath, Delhi", "estimated_cost": 0},
        ],
        "culture": [
            {"name": "Red Fort", "category": "Heritage", "location": "Chandni Chowk, Delhi", "estimated_cost": 35},
            {"name": "National Museum", "category": "Museum", "location": "Janpath, Delhi", "estimated_cost": 20},
            {"name": "Qutub Minar", "category": "Heritage", "location": "Mehrauli, Delhi", "estimated_cost": 30},
            {"name": "Humayun's Tomb", "category": "Heritage", "location": "Nizamuddin East, Delhi", "estimated_cost": 35},
        ],
        "nature": [
            {"name": "Lodhi Garden", "category": "Garden", "location": "Lodhi Colony, Delhi", "estimated_cost": 0},
            {"name": "Deer Park Hauz Khas", "category": "Nature Park", "location": "Hauz Khas, Delhi", "estimated_cost": 0},
        ],
        "shopping": [
            {"name": "Dilli Haat", "category": "Market", "location": "INA, Delhi", "estimated_cost": 30},
            {"name": "Sarojini Nagar Market", "category": "Market", "location": "Sarojini Nagar, Delhi", "estimated_cost": 0},
        ],
        "adventure": [
            {"name": "Worlds of Wonder", "category": "Adventure Park", "location": "Noida, Delhi NCR", "estimated_cost": 700},
        ],
        "coffee": [
            {"name": "Blue Tokai Coffee Roasters", "category": "Specialty Cafe", "location": "Vasant Vihar, Delhi", "estimated_cost": 400},
            {"name": "Perch Wine & Coffee Bar", "category": "Specialty Cafe", "location": "Khan Market, Delhi", "estimated_cost": 500},
        ],
        "music": [
            {"name": "Hard Rock Cafe Delhi", "category": "Live Music", "location": "Connaught Place, Delhi", "estimated_cost": 800},
        ],
        "movies": [
            {"name": "PVR Cinemas Select City Walk", "category": "Cinema", "location": "Saket, Delhi", "estimated_cost": 350},
        ],
        "food": [
            {"name": "Chandni Chowk Food Street", "category": "Food Street", "location": "Chandni Chowk, Delhi", "estimated_cost": 200},
        ],
        "art": [
            {"name": "National Gallery of Modern Art Delhi", "category": "Art Gallery", "location": "Jaipur House, Delhi", "estimated_cost": 20},
        ],
    }

    generic_pool = {
        "walks": [
            {"name": f"City Central Park", "category": "Park", "location": city, "estimated_cost": 0},
            {"name": f"Lakeside Promenade", "category": "Nature Walk", "location": city, "estimated_cost": 0},
        ],
        "culture": [
            {"name": f"City Museum", "category": "Museum", "location": city, "estimated_cost": 100},
            {"name": f"Heritage Monument", "category": "Heritage", "location": city, "estimated_cost": 50},
        ],
        "music": [
            {"name": f"Live Music Bar", "category": "Music", "location": city, "estimated_cost": 600},
            {"name": f"Concert Hall", "category": "Music", "location": city, "estimated_cost": 500},
        ],
        "shopping": [
            {"name": f"City Market", "category": "Market", "location": city, "estimated_cost": 0},
            {"name": f"Shopping Mall", "category": "Mall", "location": city, "estimated_cost": 0},
        ],
        "movies": [
            {"name": f"Multiplex Cinema", "category": "Cinema", "location": city, "estimated_cost": 300},
        ],
        "food": [
            {"name": f"City Food Street", "category": "Food Street", "location": city, "estimated_cost": 250},
        ],
        "nature": [
            {"name": f"Botanical Garden", "category": "Garden", "location": city, "estimated_cost": 30},
            {"name": f"City Nature Park", "category": "Nature Park", "location": city, "estimated_cost": 50},
        ],
        "adventure": [
            {"name": f"City Adventure Park", "category": "Adventure", "location": city, "estimated_cost": 800},
            {"name": f"Water Sports Zone", "category": "Adventure", "location": city, "estimated_cost": 600},
        ],
        "coffee": [
            {"name": f"Specialty Coffee Roastery", "category": "Specialty Cafe", "location": city, "estimated_cost": 350},
            {"name": f"Artisan Cafe", "category": "Cafe", "location": city, "estimated_cost": 300},
        ],
        "art": [
            {"name": f"City Art Gallery", "category": "Art Gallery", "location": city, "estimated_cost": 50},
        ],
        "parks": [
            {"name": f"City Park", "category": "Park", "location": city, "estimated_cost": 0},
        ],
    }

    if is_blr:
        pool = blr_pool
    elif is_hyd:
        pool = hyd_pool
    elif is_mum:
        pool = mum_pool
    elif is_del:
        pool = del_pool
    else:
        pool = generic_pool
    results = []
    seen = set()
    for interest in interests:
        if interest.lower() == "food":
            continue  # food venues are handled by get_food_options
        for item in pool.get(interest.lower(), []):
            if item["name"] not in seen:
                seen.add(item["name"])
                item = dict(item)
                item["source"] = "Curated"
                # Override category to match interest so diversity-pass groups by interest, not sub-type
                item["category"] = interest.title()
                item.setdefault("maps_url", f"https://maps.google.com/?q={item['name'].replace(' ', '+')}+{city}")
                item.setdefault("popularity", 0.5)
                item.setdefault("rating", "")
                results.append(item)

    if not results:
        results = [{"name": f"Explore {city}", "category": "Exploration", "location": city,
                    "estimated_cost": 0, "source": "Curated", "popularity": 0.3,
                    "maps_url": f"https://maps.google.com/?q={city}"}]
    return results
