"""Parse and analyse user preferences into structured planning data."""
from __future__ import annotations
import re
from typing import Dict, Any


INTEREST_CATEGORIES = {
    "food": ["food", "eating", "dining", "restaurants", "cafe", "coffee"],
    "music": ["music", "concerts", "live music", "bands", "gigs"],
    "walks": ["walks", "walking", "stroll", "parks", "nature", "outdoors"],
    "culture": ["culture", "museums", "art", "history", "heritage"],
    "sports": ["sports", "gym", "fitness", "cricket", "football", "swimming"],
    "movies": ["movies", "cinema", "films", "theatre"],
    "shopping": ["shopping", "mall", "market", "retail"],
    "adventure": ["adventure", "trekking", "hiking", "cycling", "climbing"],
}

FOURSQUARE_CATEGORIES = {
    "food": "13000",
    "music": "10032",
    "walks": "16032",
    "culture": "10027,10028,10029",
    "sports": "18000",
    "movies": "10024",
    "shopping": "17000",
    "adventure": "16000",
}


def parse_preferences(
    city: str,
    budget: float,
    available_time: str,
    mood: str,
    interests: list,
    constraints: list,
) -> Dict[str, Any]:
    """Parse user input into structured planning preferences."""

    # Budget category
    if budget <= 500:
        budget_category = "low"
        price_level_max = 1
    elif budget <= 2500:
        budget_category = "medium"
        price_level_max = 2
    elif budget <= 6000:
        budget_category = "high"
        price_level_max = 3
    else:
        budget_category = "premium"
        price_level_max = 4

    # Parse available time
    hours = _parse_hours(available_time)

    # Dietary restrictions from constraints
    dietary = []
    other_constraints = []
    crowd_sensitive = False
    for c in constraints:
        cl = c.lower()
        if any(k in cl for k in ["vegetarian", "vegan", "jain", "halal", "gluten"]):
            dietary.append(c)
        elif "crowd" in cl or "busy" in cl:
            crowd_sensitive = True
        else:
            other_constraints.append(c)

    # Map interests to Foursquare categories
    fs_cats = []
    normalized_interests = [i.lower() for i in interests]
    for interest in normalized_interests:
        for key, keywords in INTEREST_CATEGORIES.items():
            if interest in keywords or interest == key:
                if key in FOURSQUARE_CATEGORIES:
                    fs_cats.append(FOURSQUARE_CATEGORIES[key])
                break

    # Mood analysis
    mood_lower = mood.lower()
    energy_level = "medium"
    if any(w in mood_lower for w in ["tired", "exhausted", "low energy", "lazy", "relax"]):
        energy_level = "low"
    elif any(w in mood_lower for w in ["energetic", "excited", "pumped", "active", "adventure"]):
        energy_level = "high"

    wants_indoors = any(w in mood_lower for w in ["stay inside", "indoor", "quiet", "cozy", "relax"])
    wants_social = any(w in mood_lower for w in ["social", "friends", "meet", "people"])

    return {
        "city": city,
        "budget": budget,
        "budget_category": budget_category,
        "price_level_max": price_level_max,
        "available_hours": hours,
        "mood_raw": mood,
        "energy_level": energy_level,
        "wants_indoors": wants_indoors,
        "wants_social": wants_social,
        "interests": normalized_interests,
        "foursquare_categories": ",".join(fs_cats) if fs_cats else "13000,16000,10027",
        "dietary_restrictions": dietary,
        "crowd_sensitive": crowd_sensitive,
        "other_constraints": other_constraints,
        "budget_per_activity": round(budget / max(hours, 1) * 2, 0),
    }


def _parse_hours(time_str: str) -> float:
    time_str = time_str.lower()
    hours_match = re.search(r"(\d+(?:\.\d+)?)\s*hour", time_str)
    mins_match = re.search(r"(\d+)\s*min", time_str)
    total = 0.0
    if hours_match:
        total += float(hours_match.group(1))
    if mins_match:
        total += int(mins_match.group(1)) / 60
    if total == 0:
        # Named presets
        if "half" in time_str and "day" in time_str:
            return 8.0
        if "full" in time_str and "day" in time_str:
            return 12.0
        # Try plain numbers
        num = re.search(r"(\d+(?:\.\d+)?)", time_str)
        if num:
            total = float(num.group(1))
    return total if total > 0 else 8.0
