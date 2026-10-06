"""Food options: Swiggy (primary) → Google Places New API (fallback)."""
from __future__ import annotations
import httpx
import os
import re
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

SWIGGY_URL = "https://www.swiggy.com/dapi/restaurants/list/v5"
GOOGLE_PLACES_NEW_URL = "https://places.googleapis.com/v1/places:searchText"

SWIGGY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.swiggy.com/",
    "Origin": "https://www.swiggy.com",
}

GOOGLE_PRICE_LEVEL = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}


async def get_food_options(
    city: str,
    lat: float,
    lng: float,
    budget_category: str,
    dietary_restrictions: List[str],
    max_results: int = 5,
) -> Dict[str, Any]:
    """Fetch restaurant options — Swiggy first, Google Places fallback."""
    is_veg = any("veg" in d.lower() for d in dietary_restrictions)

    # Try Swiggy first
    swiggy_results = await _fetch_swiggy(lat, lng, budget_category, is_veg)

    # Always blend with curated budget-appropriate options so LLM has range to hit budget
    curated = _curated_restaurants(city, budget_category, is_veg)

    if budget_category in ("high", "premium"):
        # High/premium budgets: Google Places for real restaurants, curated for pricey anchors.
        # Swiggy delivery apps don't list fine dining so it's not useful here.
        google_results = await _fetch_google_places_food(city, budget_category, is_veg)
        all_names: set = {r["name"].lower() for r in google_results}

        merged = list(google_results)
        merged_names = set(all_names)

        # Always add 3 curated premium options — they act as budget anchors when
        # Google Places only returns MODERATE-priced restaurants.
        curated_top = curated[:4]
        curated_added = []
        for c in curated_top:
            if c["name"].lower() not in merged_names:
                merged.append(c)
                merged_names.add(c["name"].lower())
                curated_added.append(c)

        # Supplement with Swiggy (pricier ones if any)
        swiggy_supplement = [
            r for r in swiggy_results
            if r.get("name", "").lower() not in merged_names
            and (r.get("cost_for_two", 0) or 0) >= 800
        ]
        merged += swiggy_supplement

        blended = merged
        source_parts = ["Google Places"]
        if curated_added:
            source_parts.append("Curated")
        if swiggy_supplement:
            source_parts.append("Swiggy")
        source = " + ".join(source_parts)
    elif len(swiggy_results) >= 5:
        # Medium/low with enough Swiggy data — use Swiggy directly
        blended = swiggy_results
        source = "Swiggy"
    elif swiggy_results:
        swiggy_names = {r["name"].lower() for r in swiggy_results}
        extra_curated = [c for c in curated if c["name"].lower() not in swiggy_names]
        blended = swiggy_results + extra_curated
        source = "Swiggy + Curated"
    else:
        google_results = await _fetch_google_places_food(city, budget_category, is_veg)
        if google_results:
            google_names = {r["name"].lower() for r in google_results}
            extra_curated = [c for c in curated if c["name"].lower() not in google_names]
            blended = google_results + extra_curated
            source = "Google Places + Curated"
        else:
            blended = curated
            source = "Curated"

    # Sort blended list: for medium/high budgets prefer pricier options first so cost estimator picks them
    if budget_category in ("medium", "high", "premium"):
        blended.sort(key=lambda r: r.get("cost_for_two", 0) or 0, reverse=True)
    else:
        blended.sort(key=lambda r: r.get("cost_for_two", 9999) or 9999)

    return {
        "restaurants": blended,  # pass ALL to LLM and cost estimator
        "source": source,
        "count": len(blended),
        "budget_category": budget_category,
        "dietary_filters": dietary_restrictions,
    }


# ─── Swiggy ──────────────────────────────────────────────────────────────────

async def _fetch_swiggy(lat: float, lng: float, budget_category: str, is_veg: bool) -> List[Dict]:
    try:
        params = {
            "lat": lat,
            "lng": lng,
            "is-seo-homepage-enabled": "true",
            "page_type": "DESKTOP_WEB_LISTING",
        }
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(SWIGGY_URL, params=params, headers=SWIGGY_HEADERS)
            if resp.status_code != 200:
                logger.warning("Swiggy HTTP %s", resp.status_code)
                return []
            data = resp.json()

        restaurants = _parse_swiggy(data)
        logger.info("Swiggy parsed %d restaurants before filter", len(restaurants))
        filtered = _filter_swiggy(restaurants, budget_category, is_veg)
        logger.info("Swiggy filtered to %d restaurants", len(filtered))
        return filtered
    except Exception as e:
        logger.warning("Swiggy fetch failed: %s", e)
        return []


def _parse_swiggy(data: dict) -> List[Dict]:
    """Parse Swiggy GridWidget response structure."""
    results = []
    try:
        cards = data.get("data", {}).get("cards", [])
        for card in cards:
            inner = card.get("card", {}).get("card", {})
            # Restaurants live inside gridElements.infoWithStyle.restaurants
            grid = inner.get("gridElements", {})
            info_with_style = grid.get("infoWithStyle", {})
            restaurants = info_with_style.get("restaurants", [])
            for r in restaurants:
                info = r.get("info", {})
                name = info.get("name", "")
                if not name:
                    continue
                cost_str = info.get("costForTwo", "") or ""
                cost = _parse_cost(cost_str)
                cuisines = info.get("cuisines", [])
                area = info.get("areaName", "") or info.get("locality", "")
                veg = bool(info.get("veg", False))
                rating = info.get("avgRating", "")
                results.append({
                    "name": name,
                    "cuisine": ", ".join(cuisines) if cuisines else "",
                    "location": f"{name}, {area}" if area else name,
                    "cost_for_two_text": cost_str,
                    "cost_for_two": cost,
                    "rating": rating,
                    "is_veg": veg,
                    "source": "Swiggy",
                    "maps_url": f"https://maps.google.com/?q={name.replace(' ', '+')}+{area}",
                })
    except Exception as e:
        logger.warning("Error parsing Swiggy response: %s", e)
    return results


def _parse_cost(cost_str: str) -> float:
    if not cost_str:
        return 0
    nums = re.findall(r"\d+", cost_str.replace(",", ""))
    return float(nums[0]) if nums else 0


def _filter_swiggy(restaurants: List[Dict], budget_category: str, is_veg: bool) -> List[Dict]:
    filtered = []
    # Wide ranges — let the cost estimator handle selection; don't drop real restaurants
    budget_ranges = {
        "low":    (0, 600),
        "medium": (0, 2000),
        "high":   (300, 6000),
        "premium":(500, 99999),
    }
    lo, hi = budget_ranges.get(budget_category, (0, 99999))

    for r in restaurants:
        if is_veg and not r.get("is_veg", False):
            continue
        cost = r.get("cost_for_two", 0)
        if cost == 0 or lo <= cost <= hi:
            filtered.append(r)

    # Sort: for high/premium budgets, prefer expensive (higher cost) items first
    # For low budgets, prefer cheapest. Medium: prefer by rating.
    if budget_category in ("high", "premium"):
        filtered.sort(key=lambda x: (float(x.get("cost_for_two", 0) or 0), float(x.get("rating", 0) or 0)), reverse=True)
    elif budget_category == "low":
        filtered.sort(key=lambda x: float(x.get("cost_for_two", 9999) or 9999))
    else:
        filtered.sort(key=lambda x: float(x.get("rating", 0) or 0), reverse=True)
    return filtered


# ─── Google Places New API ────────────────────────────────────────────────────

async def _fetch_google_places_food(city: str, budget_category: str, is_veg: bool) -> List[Dict]:
    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        return []

    veg_prefix = "vegetarian pure veg" if is_veg else ""
    budget_suffix = {
        "low": "cheap budget street food",
        "medium": "",
        "high": "fine dining upscale",
        "premium": "luxury fine dining Michelin",
    }.get(budget_category, "")

    query = " ".join(filter(None, [veg_prefix, "restaurants", budget_suffix, "in", city]))

    body = {
        "textQuery": query,
        "maxResultCount": 10,
        "languageCode": "en",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.rating,places.priceLevel,places.googleMapsUri",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(GOOGLE_PLACES_NEW_URL, json=body, headers=headers)
            if resp.status_code != 200:
                logger.warning("Google Places New API: %s %s", resp.status_code, resp.text[:200])
                return []
            data = resp.json()

        results = []
        for place in data.get("places", []):
            name = place.get("displayName", {}).get("text", "")
            if not name:
                continue
            # Use priceLevel to estimate cost, but do NOT filter out restaurants
            # that lack it — many real restaurants on Google don't have it set
            price_str = place.get("priceLevel", "")
            price_level = GOOGLE_PRICE_LEVEL.get(price_str, -1)
            if price_level == -1:
                # Unknown price level — estimate from budget category
                cost_for_two = _budget_category_to_inr(budget_category)
            else:
                cost_for_two = _price_level_to_inr(price_level)
            results.append({
                "name": name,
                "cuisine": "",
                "location": place.get("formattedAddress", city),
                "cost_for_two": cost_for_two,
                "cost_for_two_text": f"₹{cost_for_two} for two",
                "rating": place.get("rating", "N/A"),
                "is_veg": is_veg,
                "maps_url": place.get("googleMapsUri", f"https://maps.google.com/?q={name.replace(' ', '+')}"),
                "source": "Google Places",
            })

        logger.info("Google Places food: %d results for '%s'", len(results), query)
        return results
    except Exception as e:
        logger.warning("Google Places New API food failed: %s", e)
        return []


def _price_level_to_inr(price_level: int) -> float:
    return {0: 150, 1: 400, 2: 800, 3: 1800, 4: 3500}.get(price_level, 800)


def _budget_category_to_inr(budget_category: str) -> float:
    """Fallback cost estimate when Google Places doesn't return a priceLevel."""
    return {"low": 300, "medium": 800, "high": 2000, "premium": 4000}.get(budget_category, 800)


# ─── Curated fallback ─────────────────────────────────────────────────────────

def _curated_restaurants(city: str, budget_category: str, is_veg: bool) -> List[Dict]:
    city_lower = city.lower()
    is_blr = "bangalore" in city_lower or "bengaluru" in city_lower
    is_hyd = "hyderabad" in city_lower or "secunderabad" in city_lower
    is_mum = "mumbai" in city_lower or "bombay" in city_lower
    is_del = "delhi" in city_lower or "new delhi" in city_lower

    blr = {
        "low": [
            {"name": "Vidyarthi Bhavan", "cuisine": "South Indian", "location": "Gandhi Bazaar, Bangalore", "cost_for_two": 200, "rating": 4.5, "is_veg": True},
            {"name": "MTR (Mavalli Tiffin Room)", "cuisine": "South Indian", "location": "Lalbagh Road, Bangalore", "cost_for_two": 300, "rating": 4.6, "is_veg": True},
            {"name": "Brahmin's Coffee Bar", "cuisine": "South Indian", "location": "Shankarapuram, Bangalore", "cost_for_two": 150, "rating": 4.4, "is_veg": True},
            {"name": "VV Puram Food Street", "cuisine": "Street Food", "location": "VV Puram, Bangalore", "cost_for_two": 200, "rating": 4.3, "is_veg": True},
            {"name": "Shri Sagar (CTR)", "cuisine": "South Indian", "location": "Malleshwaram, Bangalore", "cost_for_two": 200, "rating": 4.6, "is_veg": True},
            {"name": "Darshini Breakfast Spot", "cuisine": "South Indian", "location": "Basavanagudi, Bangalore", "cost_for_two": 180, "rating": 4.2, "is_veg": True},
            {"name": "Veena Stores", "cuisine": "South Indian", "location": "Malleshwaram, Bangalore", "cost_for_two": 160, "rating": 4.3, "is_veg": True},
        ],
        "medium": [
            {"name": "The Fatty Bao", "cuisine": "Asian", "location": "Indiranagar, Bangalore", "cost_for_two": 900, "rating": 4.3, "is_veg": False},
            {"name": "Toit Brewpub", "cuisine": "Continental, Craft Beer", "location": "Indiranagar, Bangalore", "cost_for_two": 1200, "rating": 4.4, "is_veg": False},
            {"name": "Anand Bhavan", "cuisine": "North Indian Veg", "location": "Rajajinagar, Bangalore", "cost_for_two": 500, "rating": 4.1, "is_veg": True},
            {"name": "Meghana Foods", "cuisine": "Biryani & South Indian", "location": "Residency Road, Bangalore", "cost_for_two": 800, "rating": 4.5, "is_veg": True},
            {"name": "Truffles", "cuisine": "Continental Veg-Friendly", "location": "Koramangala, Bangalore", "cost_for_two": 900, "rating": 4.3, "is_veg": True},
            {"name": "Saravana Bhavan", "cuisine": "South Indian Veg", "location": "Jayanagar, Bangalore", "cost_for_two": 600, "rating": 4.3, "is_veg": True},
            {"name": "Udupi Palace", "cuisine": "Udupi Veg", "location": "Koramangala, Bangalore", "cost_for_two": 500, "rating": 4.2, "is_veg": True},
            {"name": "Chutney Chang", "cuisine": "South Indian Veg", "location": "Indiranagar, Bangalore", "cost_for_two": 700, "rating": 4.2, "is_veg": True},
            {"name": "Koshy's", "cuisine": "Continental Veg", "location": "St Mark's Road, Bangalore", "cost_for_two": 800, "rating": 4.1, "is_veg": True},
        ],
        "high": [
            {"name": "Karavalli", "cuisine": "Coastal Karnataka", "location": "Gateway Hotel, Bangalore", "cost_for_two": 2500, "rating": 4.7, "is_veg": False},
            {"name": "The Permit Room", "cuisine": "Modern South Indian", "location": "Indiranagar, Bangalore", "cost_for_two": 1800, "rating": 4.4, "is_veg": False},
            {"name": "Truffles", "cuisine": "Continental Veg-Friendly", "location": "Koramangala, Bangalore", "cost_for_two": 1200, "rating": 4.3, "is_veg": True},
            {"name": "Nagarjuna", "cuisine": "Andhra", "location": "Residency Road, Bangalore", "cost_for_two": 700, "rating": 4.4, "is_veg": True},
            {"name": "Meghana Foods", "cuisine": "Biryani & South Indian", "location": "Residency Road, Bangalore", "cost_for_two": 800, "rating": 4.5, "is_veg": True},
        ],
        "premium": [
            {"name": "Karavalli", "cuisine": "Coastal Karnataka", "location": "Gateway Hotel, Bangalore", "cost_for_two": 2500, "rating": 4.7, "is_veg": False},
            {"name": "Caperberry", "cuisine": "European", "location": "Lavelle Road, Bangalore", "cost_for_two": 2000, "rating": 4.5, "is_veg": False},
            {"name": "The Reservoire", "cuisine": "Fine Dining", "location": "UB City, Bangalore", "cost_for_two": 3000, "rating": 4.6, "is_veg": False},
            {"name": "Toscano", "cuisine": "Italian", "location": "UB City, Bangalore", "cost_for_two": 2200, "rating": 4.4, "is_veg": False},
            {"name": "Olive Beach", "cuisine": "Mediterranean", "location": "HAL, Bangalore", "cost_for_two": 2400, "rating": 4.3, "is_veg": False},
        ],
    }

    hyd = {
        "low": [
            {"name": "Shah Ghouse Cafe", "cuisine": "Hyderabadi", "location": "Saidabad, Hyderabad", "cost_for_two": 300, "rating": 4.4, "is_veg": False},
            {"name": "Hotel Shadab", "cuisine": "Biryani & Mughlai", "location": "High Court Road, Hyderabad", "cost_for_two": 400, "rating": 4.3, "is_veg": False},
            {"name": "Chutneys", "cuisine": "South Indian Veg", "location": "Banjara Hills, Hyderabad", "cost_for_two": 350, "rating": 4.4, "is_veg": True},
            {"name": "Minerva Coffee Shop", "cuisine": "South Indian", "location": "Abids, Hyderabad", "cost_for_two": 200, "rating": 4.2, "is_veg": True},
            {"name": "Kamat Restaurant", "cuisine": "South Indian Veg", "location": "Secunderabad, Hyderabad", "cost_for_two": 250, "rating": 4.1, "is_veg": True},
        ],
        "medium": [
            {"name": "Paradise Restaurant", "cuisine": "Hyderabadi Biryani", "location": "Secunderabad, Hyderabad", "cost_for_two": 900, "rating": 4.4, "is_veg": False},
            {"name": "Chutneys", "cuisine": "South Indian Veg", "location": "Banjara Hills, Hyderabad", "cost_for_two": 700, "rating": 4.4, "is_veg": True},
            {"name": "Eat Street", "cuisine": "Street Food", "location": "Necklace Road, Hyderabad", "cost_for_two": 600, "rating": 4.2, "is_veg": True},
            {"name": "Bawarchi Restaurant", "cuisine": "Biryani & Mughlai", "location": "RTC Cross Roads, Hyderabad", "cost_for_two": 700, "rating": 4.3, "is_veg": False},
            {"name": "Amrutha Castle", "cuisine": "North Indian Veg", "location": "Somajiguda, Hyderabad", "cost_for_two": 800, "rating": 4.2, "is_veg": True},
            {"name": "Pista House", "cuisine": "Hyderabadi Haleem", "location": "Shah Ali Banda, Hyderabad", "cost_for_two": 500, "rating": 4.3, "is_veg": False},
        ],
        "high": [
            {"name": "Flechazo", "cuisine": "Continental", "location": "Banjara Hills, Hyderabad", "cost_for_two": 2000, "rating": 4.5, "is_veg": False},
            {"name": "Zega", "cuisine": "Asian Fusion", "location": "Banjara Hills, Hyderabad", "cost_for_two": 1800, "rating": 4.4, "is_veg": False},
            {"name": "Jewel of Nizam", "cuisine": "Hyderabadi", "location": "Taj Krishna, Hyderabad", "cost_for_two": 2500, "rating": 4.6, "is_veg": False},
            {"name": "Southern Spice", "cuisine": "South Indian", "location": "Taj Banjara, Hyderabad", "cost_for_two": 2000, "rating": 4.5, "is_veg": True},
            {"name": "Ohri's Jiva Imperia", "cuisine": "Multi-cuisine", "location": "Banjara Hills, Hyderabad", "cost_for_two": 1500, "rating": 4.3, "is_veg": False},
        ],
        "premium": [
            {"name": "Jewel of Nizam", "cuisine": "Hyderabadi Fine Dining", "location": "Taj Krishna, Hyderabad", "cost_for_two": 5000, "rating": 4.7, "is_veg": False},
            {"name": "Flechazo", "cuisine": "Continental Fine Dining", "location": "Banjara Hills, Hyderabad", "cost_for_two": 4000, "rating": 4.5, "is_veg": False},
            {"name": "Zega", "cuisine": "Asian Fusion", "location": "Banjara Hills, Hyderabad", "cost_for_two": 3500, "rating": 4.4, "is_veg": False},
            {"name": "Southern Spice", "cuisine": "South Indian Fine Dining", "location": "Taj Banjara, Hyderabad", "cost_for_two": 4000, "rating": 4.5, "is_veg": True},
            {"name": "Feast at Sheraton", "cuisine": "International Buffet", "location": "Sheraton Hyderabad, Gachibowli", "cost_for_two": 3500, "rating": 4.4, "is_veg": True},
            {"name": "Ohri's Tansen", "cuisine": "North Indian Fine Dining", "location": "Banjara Hills, Hyderabad", "cost_for_two": 3000, "rating": 4.3, "is_veg": False},
        ],
    }

    mum = {
        "low": [
            {"name": "Sardar Refreshments", "cuisine": "Maharashtrian", "location": "Tardeo, Mumbai", "cost_for_two": 200, "rating": 4.3, "is_veg": True},
            {"name": "Cafe Madras", "cuisine": "South Indian", "location": "Matunga, Mumbai", "cost_for_two": 300, "rating": 4.4, "is_veg": True},
            {"name": "Kyani & Co.", "cuisine": "Irani Cafe", "location": "Marine Lines, Mumbai", "cost_for_two": 250, "rating": 4.2, "is_veg": False},
        ],
        "medium": [
            {"name": "The Table", "cuisine": "Contemporary", "location": "Colaba, Mumbai", "cost_for_two": 1200, "rating": 4.5, "is_veg": False},
            {"name": "Bastian", "cuisine": "Seafood", "location": "Bandra, Mumbai", "cost_for_two": 1500, "rating": 4.4, "is_veg": False},
            {"name": "Swati Snacks", "cuisine": "Gujarati Veg", "location": "Tardeo, Mumbai", "cost_for_two": 600, "rating": 4.5, "is_veg": True},
            {"name": "Bade Miyan", "cuisine": "Mughlai", "location": "Colaba, Mumbai", "cost_for_two": 500, "rating": 4.3, "is_veg": False},
        ],
        "high": [
            {"name": "Wasabi by Morimoto", "cuisine": "Japanese", "location": "Taj Mahal Palace, Mumbai", "cost_for_two": 4000, "rating": 4.7, "is_veg": False},
            {"name": "Trishna", "cuisine": "Coastal Seafood", "location": "Fort, Mumbai", "cost_for_two": 2000, "rating": 4.6, "is_veg": False},
            {"name": "Peshwa Pavilion", "cuisine": "Maharashtrian Fine Dining", "location": "ITC Maratha, Mumbai", "cost_for_two": 3000, "rating": 4.5, "is_veg": True},
        ],
        "premium": [
            {"name": "Wasabi by Morimoto", "cuisine": "Japanese Fine Dining", "location": "Taj Mahal Palace, Mumbai", "cost_for_two": 8000, "rating": 4.7, "is_veg": False},
            {"name": "Trishna", "cuisine": "Coastal Seafood", "location": "Fort, Mumbai", "cost_for_two": 4000, "rating": 4.6, "is_veg": False},
            {"name": "La Folie", "cuisine": "French Patisserie & Dining", "location": "Lower Parel, Mumbai", "cost_for_two": 3000, "rating": 4.4, "is_veg": True},
            {"name": "Masala Library", "cuisine": "Modern Indian", "location": "BKC, Mumbai", "cost_for_two": 5000, "rating": 4.6, "is_veg": False},
        ],
    }

    del_ = {
        "low": [
            {"name": "Paranthe Wali Gali", "cuisine": "North Indian Street Food", "location": "Chandni Chowk, Delhi", "cost_for_two": 200, "rating": 4.3, "is_veg": True},
            {"name": "Karim's", "cuisine": "Mughlai", "location": "Jama Masjid, Delhi", "cost_for_two": 400, "rating": 4.4, "is_veg": False},
            {"name": "Saravana Bhavan", "cuisine": "South Indian Veg", "location": "Connaught Place, Delhi", "cost_for_two": 350, "rating": 4.2, "is_veg": True},
        ],
        "medium": [
            {"name": "Bukhara", "cuisine": "North Indian", "location": "ITC Maurya, Delhi", "cost_for_two": 2500, "rating": 4.7, "is_veg": False},
            {"name": "Moti Mahal", "cuisine": "Mughlai", "location": "Daryaganj, Delhi", "cost_for_two": 1000, "rating": 4.3, "is_veg": False},
            {"name": "Haldiram's", "cuisine": "Indian Sweets & Snacks", "location": "Chandni Chowk, Delhi", "cost_for_two": 500, "rating": 4.2, "is_veg": True},
            {"name": "Lodi - The Garden Restaurant", "cuisine": "European", "location": "Lodhi Colony, Delhi", "cost_for_two": 1200, "rating": 4.4, "is_veg": False},
        ],
        "high": [
            {"name": "Bukhara", "cuisine": "North Indian", "location": "ITC Maurya, Delhi", "cost_for_two": 5000, "rating": 4.7, "is_veg": False},
            {"name": "Indian Accent", "cuisine": "Modern Indian", "location": "The Manor, Delhi", "cost_for_two": 4000, "rating": 4.8, "is_veg": False},
            {"name": "Dum Pukht", "cuisine": "Awadhi", "location": "ITC Maurya, Delhi", "cost_for_two": 4500, "rating": 4.6, "is_veg": False},
        ],
        "premium": [
            {"name": "Indian Accent", "cuisine": "Modern Indian Fine Dining", "location": "The Manor, Delhi", "cost_for_two": 8000, "rating": 4.8, "is_veg": False},
            {"name": "Bukhara", "cuisine": "North Indian Fine Dining", "location": "ITC Maurya, Delhi", "cost_for_two": 6000, "rating": 4.7, "is_veg": False},
            {"name": "Dum Pukht", "cuisine": "Awadhi Fine Dining", "location": "ITC Maurya, Delhi", "cost_for_two": 5500, "rating": 4.6, "is_veg": False},
            {"name": "Varq", "cuisine": "Indian Contemporary", "location": "Taj Mahal Hotel, Delhi", "cost_for_two": 6000, "rating": 4.5, "is_veg": False},
        ],
    }

    # Generic pool for all other cities — 5+ options per tier
    generic = {
        "low": [
            {"name": f"City Tiffin House", "cuisine": "South Indian", "location": city, "cost_for_two": 200, "rating": 4.0, "is_veg": True},
            {"name": f"Street Food Corner", "cuisine": "Street Food", "location": city, "cost_for_two": 200, "rating": 4.0, "is_veg": True},
            {"name": f"Local Chai & Snacks", "cuisine": "Snacks & Tea", "location": city, "cost_for_two": 150, "rating": 4.0, "is_veg": True},
            {"name": f"Budget Biryani House", "cuisine": "Biryani", "location": city, "cost_for_two": 300, "rating": 4.1, "is_veg": False},
            {"name": f"Vegetarian Thali Center", "cuisine": "Thali", "location": city, "cost_for_two": 250, "rating": 4.0, "is_veg": True},
        ],
        "medium": [
            {"name": f"Barbeque Nation", "cuisine": "BBQ & Grill", "location": city, "cost_for_two": 1200, "rating": 4.2, "is_veg": True},
            {"name": f"The Spice Route", "cuisine": "Pan Indian", "location": city, "cost_for_two": 800, "rating": 4.1, "is_veg": False},
            {"name": f"Cafe Zoe", "cuisine": "Continental", "location": city, "cost_for_two": 900, "rating": 4.2, "is_veg": True},
            {"name": f"Mainland China", "cuisine": "Chinese", "location": city, "cost_for_two": 1000, "rating": 4.2, "is_veg": False},
            {"name": f"Punjab Grill", "cuisine": "North Indian", "location": city, "cost_for_two": 1100, "rating": 4.3, "is_veg": False},
            {"name": f"Saffron Garden", "cuisine": "Mughlai Veg", "location": city, "cost_for_two": 700, "rating": 4.0, "is_veg": True},
            {"name": f"Cafe Coffee Day Premium", "cuisine": "Cafe", "location": city, "cost_for_two": 500, "rating": 3.9, "is_veg": True},
        ],
        "high": [
            {"name": f"ITC Hotel Restaurant", "cuisine": "Multi-cuisine", "location": city, "cost_for_two": 3000, "rating": 4.6, "is_veg": False},
            {"name": f"Taj Hotel Fine Dining", "cuisine": "Continental", "location": city, "cost_for_two": 4000, "rating": 4.7, "is_veg": False},
            {"name": f"Kebab Factory", "cuisine": "North Indian", "location": city, "cost_for_two": 1500, "rating": 4.3, "is_veg": False},
            {"name": f"The Grand Pavilion", "cuisine": "International", "location": city, "cost_for_two": 2500, "rating": 4.5, "is_veg": True},
            {"name": f"Spice Market Restaurant", "cuisine": "Indian Contemporary", "location": city, "cost_for_two": 2000, "rating": 4.4, "is_veg": False},
        ],
        "premium": [
            {"name": f"Taj Hotels Signature Restaurant", "cuisine": "Fine Dining", "location": city, "cost_for_two": 6000, "rating": 4.8, "is_veg": False},
            {"name": f"ITC Royal Bengal", "cuisine": "Luxury Indian", "location": city, "cost_for_two": 5000, "rating": 4.7, "is_veg": False},
            {"name": f"Marriott Rooftop Restaurant", "cuisine": "International Cuisine", "location": city, "cost_for_two": 5500, "rating": 4.6, "is_veg": False},
            {"name": f"Hyatt Regency Fine Dining", "cuisine": "Modern Fusion", "location": city, "cost_for_two": 4500, "rating": 4.6, "is_veg": True},
            {"name": f"Oberoi Luxury Dining", "cuisine": "Contemporary Indian", "location": city, "cost_for_two": 7000, "rating": 4.8, "is_veg": False},
        ],
    }

    if is_blr:
        pool = blr
    elif is_hyd:
        pool = hyd
    elif is_mum:
        pool = mum
    elif is_del:
        pool = del_
    else:
        pool = generic

    options = pool.get(budget_category, pool.get("medium", []))

    if is_veg:
        veg_options = [o for o in options if o.get("is_veg", False)]
        if not veg_options:
            # Pull veg options from across all tiers of the same city pool
            veg_options = [o for tier in pool.values() for o in tier if o.get("is_veg", False)]
        if not veg_options:
            veg_options = [{"name": "Pure Veg Restaurant", "cuisine": "Multi-cuisine Veg", "location": city, "cost_for_two": 600, "rating": 4.0, "is_veg": True}]
        options = veg_options

    for o in options:
        o["source"] = o.get("source", "Curated")
        o.setdefault("cost_for_two_text", f"₹{o['cost_for_two']} for two")
        o.setdefault("maps_url", f"https://maps.google.com/?q={o['name'].replace(' ', '+')}+{city}")

    return options
