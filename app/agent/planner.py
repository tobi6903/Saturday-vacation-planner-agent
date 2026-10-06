"""Main Saturday Planner agent — orchestrates tools and streams events via SSE."""
from __future__ import annotations
import json
import asyncio
import logging
from typing import AsyncGenerator, Dict, Any

from openai import AsyncOpenAI

from app.config import OPENAI_API_KEY, OPENAI_API_BASE, MODEL, CITY_COORDS
from app.models.schemas import PlannerInput
from app.agent.tools import (
    parse_preferences,
    get_food_options,
    get_activity_options,
    search_events,
    estimate_cost,
    validate_plan,
)

logger = logging.getLogger(__name__)


def _get_openai_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE)


def _get_city_coords(city: str):
    city_lower = city.lower().strip()
    if city_lower in CITY_COORDS:
        return CITY_COORDS[city_lower]
    # Try partial match
    for key, coords in CITY_COORDS.items():
        if key in city_lower or city_lower in key:
            return coords
    return (12.9716, 77.5946)  # Default to Bangalore


def _event(type_: str, message: str, data: Any = None, step: int = None) -> str:
    payload = {"type": type_, "message": message}
    if data is not None:
        payload["data"] = data
    if step is not None:
        payload["step"] = step
    return json.dumps(payload)


SYSTEM_PROMPT = """You are a Saturday planner assistant. Your job is to enrich a pre-computed plan template.
CRITICAL: The plan template below already has the correct names, times, and costs — DO NOT change them.
Your only job is to add: location, address, duration, why, tips, maps_url.
Return ONLY valid JSON — no markdown, no extra text.
"""


async def run_planner(user_input: PlannerInput) -> AsyncGenerator[str, None]:
    """Stream SSE events for the planning process."""
    yield _event("trace", "Starting your Saturday planner...", step=0)
    await asyncio.sleep(0.1)

    # Step 1: Parse preferences
    yield _event("trace", "Analysing your preferences and mood...", step=1)
    prefs = parse_preferences(
        city=user_input.city,
        budget=user_input.budget,
        available_time=user_input.available_time,
        mood=user_input.mood,
        interests=user_input.interests,
        constraints=user_input.constraints,
    )
    yield _event("tool_result", f"Preferences parsed — budget category: {prefs['budget_category']}, energy: {prefs['energy_level']}", data=prefs, step=1)
    await asyncio.sleep(0.1)

    lat, lng = _get_city_coords(user_input.city)

    # Step 2: Get food options
    yield _event("trace", f"Searching for {prefs['budget_category']}-budget restaurants in {user_input.city}...", step=2)
    food_data = await get_food_options(
        city=user_input.city,
        lat=lat,
        lng=lng,
        budget_category=prefs["budget_category"],
        dietary_restrictions=prefs["dietary_restrictions"],
        max_results=8,
    )
    yield _event(
        "tool_result",
        f"Found {food_data['count']} restaurant(s) via {food_data['source']}",
        data={"restaurants": food_data["restaurants"][:3]},
        step=2,
    )
    await asyncio.sleep(0.1)

    # Step 3: Get activities
    yield _event("trace", f"Finding activities matching your interests: {', '.join(user_input.interests)}...", step=3)
    activity_data = await get_activity_options(
        city=user_input.city,
        lat=lat,
        lng=lng,
        interests=user_input.interests,
        budget_category=prefs["budget_category"],
        available_hours=prefs["available_hours"],
        crowd_sensitive=prefs["crowd_sensitive"],
        max_results=6,
    )
    yield _event(
        "tool_result",
        f"Found {activity_data['count']} activit(ies) via {activity_data['source']}",
        data={"activities": activity_data["activities"][:3]},
        step=3,
    )
    await asyncio.sleep(0.1)

    # Step 4: Search for events (only if relevant interests)
    event_data = {"events": [], "summary": ""}
    event_interests = ["music", "concerts", "events", "movies", "festival", "theatre"]
    should_search_events = any(
        i.lower() in event_interests for i in user_input.interests
    ) or any(k in user_input.mood.lower() for k in ["music", "concert", "event"])

    if should_search_events:
        yield _event("trace", f"Searching for live events and happenings in {user_input.city}...", step=4)
        event_data = await search_events(user_input.city, user_input.interests, user_input.mood)
        yield _event(
            "tool_result",
            f"Found {len(event_data.get('events', []))} event(s) via {event_data.get('source', 'search')}",
            data=event_data,
            step=4,
        )
        await asyncio.sleep(0.1)

    # Step 5: Estimate cost
    yield _event("trace", "Estimating total cost and checking budget fit...", step=5)
    cost_data = estimate_cost(
        food_options=food_data["restaurants"],
        activity_options=activity_data["activities"],
        budget=user_input.budget,
        available_hours=prefs["available_hours"],
        budget_category=prefs["budget_category"],
    )
    yield _event(
        "tool_result",
        f"Estimated cost: ₹{cost_data['total_estimated_cost']:.0f} / ₹{user_input.budget} budget ({cost_data['budget_utilization_pct']}% used)",
        data=cost_data,
        step=5,
    )
    await asyncio.sleep(0.1)

    # Step 6: Validate plan
    yield _event("trace", "Validating plan against your constraints...", step=6)
    validation = validate_plan(
        food_options=food_data["restaurants"],
        activity_options=activity_data["activities"],
        constraints=user_input.constraints,
        energy_level=prefs["energy_level"],
        available_hours=prefs["available_hours"],
        budget=user_input.budget,
        total_cost=cost_data["total_estimated_cost"],
    )
    validation_msg = "Plan validated ✓" if validation["valid"] else f"Plan has issues: {'; '.join(validation['issues'])}"
    yield _event("tool_result", validation_msg, data=validation, step=6)
    await asyncio.sleep(0.1)

    # Step 7: Generate final plan using OpenAI
    yield _event("trace", "Crafting your personalised Saturday plan...", step=7)

    context = {
        "user_input": user_input.model_dump(),
        "preferences": prefs,
        "food_options": food_data["restaurants"],
        "activity_options": activity_data["activities"],
        "events": event_data.get("events", []),
        "cost_estimate": cost_data,
        "validation": validation,
        "food_lookup": {r.get("name", ""): r for r in food_data["restaurants"]},
        "activity_lookup": {a.get("name", ""): a for a in activity_data["activities"]},
    }

    final_plan = await _generate_plan_with_llm(context)
    yield _event("plan", "Your perfect Saturday plan is ready!", data=final_plan, step=7)


async def _generate_plan_with_llm(context: Dict[str, Any]) -> Dict[str, Any]:
    """Use OpenAI to enrich the pre-computed slot schedule with descriptions."""
    client = _get_openai_client()
    ui = context["user_input"]
    prefs = context["preferences"]
    cost = context["cost_estimate"]
    budget = ui["budget"]
    slot_schedule = cost.get("slot_schedule", [])

    # Format slot template — LLM copies costs exactly, adds qualitative fields
    template_lines = []
    schedule_total = 0
    food_lookup = context["food_lookup"]
    act_lookup = context["activity_lookup"]

    for i, slot in enumerate(slot_schedule):
        name = slot["name"]
        cat  = slot["category"]
        c    = int(slot["cost"])
        schedule_total += c
        # Provide source data so LLM can write accurate location/why/tips
        if cat == "Food":
            r = food_lookup.get(name, {})
            meta = (f"cuisine={r.get('cuisine','') or 'Restaurant'}, "
                    f"rating={r.get('rating','N/A')}, "
                    f"location={r.get('location', ui['city'])}, "
                    f"address={r.get('address','')}")
        elif cat == "Transport":
            meta = "auto/cab between spots"
        else:
            a = act_lookup.get(name, {})
            meta = (f"location={a.get('location', ui['city'])}, "
                    f"address={a.get('address','')}, "
                    f"rating={a.get('rating','N/A')}")
        template_lines.append(
            f"  Slot {i+1}: time=\"{slot['time']}\", activity=\"{name}\", "
            f"category=\"{cat}\", estimated_cost={c}  [{meta}]"
        )

    template_block = "\n".join(template_lines)
    pct = round(schedule_total / budget * 100, 1)

    event_list = "\n".join(
        f"- {e.get('title','')}: {e.get('description','')[:120]}"
        for e in context["events"][:3]
    ) or "No specific events found."

    prompt = f"""Enrich the plan template below. Return ONLY valid JSON.

City: {ui['city']}  |  Budget: ₹{budget}  |  Mood: {ui['mood']}
Interests: {', '.join(ui['interests'])}  |  Constraints: {', '.join(ui['constraints'])}

=== PRE-COMPUTED SLOT TEMPLATE (COPY time/activity/category/estimated_cost EXACTLY) ===
{template_block}
Template total: ₹{schedule_total} ({pct}% of ₹{budget} budget) ✓

=== EVENTS (mention any relevant ones in summary/why) ===
{event_list}

=== TASK ===
Return a JSON object with EXACTLY {len(slot_schedule)} time_slots — one per template slot, in order.
DO NOT add, remove, or reorder slots. DO NOT change: time, activity, category, estimated_cost.
For each slot add ONLY:
- location (area, city)
- address (street address or landmark)
- duration (e.g. "1.5 hours")
- why (1 sentence — how this fits the mood/interests)
- tips (1 practical tip)
- maps_url (https://maps.google.com/?q=name+city)

total_estimated_cost = {schedule_total} (fixed — do not alter).

=== OUTPUT FORMAT ===
{{
  "title": "catchy title for the day",
  "summary": "2-3 sentence overview",
  "time_slots": [
    {{
      "time": "...", "activity": "...", "location": "...", "address": "...",
      "duration": "...", "estimated_cost": ..., "category": "...",
      "why": "...", "tips": "...", "maps_url": "..."
    }}
  ],
  "total_estimated_cost": {schedule_total},
  "budget": {budget},
  "remaining_budget": {budget - schedule_total},
  "trade_offs": "brief trade-offs",
  "fallback_plan": "alternative if plans change",
  "agent_notes": "data sources: Swiggy, Google Places, Tavily"
}}

Return ONLY the JSON — no markdown, no extra text."""

    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        plan = json.loads(content)
        # Authoritative totals always come from the pre-computed schedule
        plan = _sync_plan_total(plan, budget, cost, context)
        return plan
    except Exception as e:
        logger.error("LLM plan generation failed: %s", e)
        return _fallback_plan(context)


def _sync_plan_total(plan: Dict, budget: float, cost: Dict, context: Dict) -> Dict:
    """
    Rebuild the final plan from the authoritative slot_schedule template.
    The template is ground truth for structure and cost; LLM descriptions
    are grafted in where available, with fallbacks for any that are missing.
    """
    slot_schedule = cost.get("slot_schedule", [])
    if not slot_schedule:
        return plan

    city = context["user_input"].get("city", "Bangalore")
    food_lookup   = context.get("food_lookup", {})
    act_lookup    = context.get("activity_lookup", {})

    # Index LLM-generated slots by activity name for description harvesting
    llm_slots = plan.get("time_slots", [])
    llm_by_name: Dict[str, Dict] = {}
    for s in llm_slots:
        key = s.get("activity", "").lower().strip()
        if key:
            llm_by_name[key] = s

    def _best_llm_slot(tname: str) -> Dict:
        """Find the closest LLM slot by substring match for description fields."""
        tl = tname.lower()
        for k, s in llm_by_name.items():
            if tl in k or k in tl:
                return s
        return {}

    rebuilt = []
    for tslot in slot_schedule:
        tname = tslot["name"]
        cat   = tslot["category"]
        cost_ = int(tslot["cost"])
        time_ = tslot["time"]

        # Try to get rich description from LLM output
        llm = _best_llm_slot(tname)

        # Metadata from pre-fetched data
        r = food_lookup.get(tname, {})
        a = act_lookup.get(tname, {})
        src = r or a

        rebuilt.append({
            "time": time_,
            "activity": tname,
            "location": llm.get("location") or src.get("location") or city,
            "address":  llm.get("address")  or src.get("address")  or "",
            "duration": llm.get("duration") or "1–2 hours",
            "estimated_cost": cost_,
            "category": cat,
            "why":  llm.get("why")  or f"Part of your {cat.lower()} plan for the day.",
            "tips": llm.get("tips") or "Check timings before you go.",
            "maps_url": llm.get("maps_url") or src.get("maps_url")
                        or f"https://maps.google.com/?q={tname.replace(' ','+')},+{city}",
        })

    actual = sum(s["estimated_cost"] for s in rebuilt)
    if len(rebuilt) != len(slot_schedule):
        logger.warning("Rebuilt %d slots but template has %d", len(rebuilt), len(slot_schedule))

    plan["time_slots"]           = rebuilt
    plan["total_estimated_cost"] = round(actual, 0)
    plan["remaining_budget"]     = round(budget - actual, 0)
    plan["budget"]               = budget
    return plan


def _fallback_plan(context: Dict[str, Any]) -> Dict[str, Any]:
    """Static fallback plan when LLM fails."""
    ui = context["user_input"]
    cost = context["cost_estimate"]
    foods = context["food_options"]
    activities = context["activity_options"]

    time_slots = []
    current_hour = 10

    for i, act in enumerate(activities[:2]):
        time_slots.append({
            "time": f"{current_hour}:00 AM",
            "activity": act.get("name", "Activity"),
            "location": act.get("location", ui["city"]),
            "duration": "1.5 hours",
            "estimated_cost": act.get("estimated_cost", 0),
            "category": act.get("category", "Activity"),
            "why": f"Matches your interest in {ui['interests'][0] if ui['interests'] else 'exploring'}.",
            "tips": "Check opening hours before visiting.",
            "maps_url": act.get("maps_url", f"https://maps.google.com/?q={ui['city']}"),
        })
        current_hour += 2

    if foods:
        food = foods[0]
        time_slots.append({
            "time": f"{current_hour}:00 PM" if current_hour >= 12 else f"{current_hour}:00 AM",
            "activity": f"Lunch at {food.get('name', 'Restaurant')}",
            "location": food.get("location", ui["city"]),
            "duration": "1 hour",
            "estimated_cost": food.get("cost_for_two", 400) / 2,
            "category": "Food",
            "why": "Good ratings and fits your budget.",
            "tips": "Arrive slightly before peak hours to avoid wait.",
            "maps_url": food.get("maps_url", f"https://maps.google.com/?q={ui['city']}"),
        })

    return {
        "title": f"Your Saturday in {ui['city']}",
        "summary": f"A curated day based on your mood and interests in {ui['city']}.",
        "time_slots": time_slots,
        "total_estimated_cost": cost["total_estimated_cost"],
        "budget": ui["budget"],
        "remaining_budget": cost["remaining_budget"],
        "trade_offs": "Plan generated with available data.",
        "fallback_plan": f"If plans change, explore {ui['city']}'s local markets or parks.",
        "agent_notes": "This is a fallback plan. Try again for a more detailed plan.",
    }
