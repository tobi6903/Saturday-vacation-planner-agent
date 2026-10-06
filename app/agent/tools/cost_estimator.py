"""
Estimate cost and build a complete pre-planned slot schedule.
"""
from __future__ import annotations
from typing import Dict, Any, List

# Budget splits by tier — high/premium lean on food because premium restaurants
# are genuinely expensive; low/medium are balanced.
FOOD_SHARE = {"low": 0.55, "medium": 0.40, "high": 0.50, "premium": 0.55}
ACT_SHARE  = {"low": 0.30, "medium": 0.50, "high": 0.42, "premium": 0.38}

# Meals per budget tier
MEALS_COUNT = {"low": 3, "medium": 2, "high": 3, "premium": 3}


def estimate_cost(
    food_options: List[Dict],
    activity_options: List[Dict],
    budget: float,
    available_hours: float,
    budget_category: str,
) -> Dict[str, Any]:
    transport = _transport_cost(budget_category)
    available  = budget - transport

    num_activities = max(1, min(int(available_hours // 1.5), 5))
    num_meals      = MEALS_COUNT.get(budget_category, 2)

    food_target     = round(available * FOOD_SHARE.get(budget_category, 0.40), 0)
    activity_target = round(available * ACT_SHARE.get(budget_category, 0.50), 0)

    # --- Food selection ---
    food_items = _select_food(food_options, food_target, num_meals)
    food_cost  = sum(f["cost"] for f in food_items)
    food_names = [f["name"] for f in food_items]

    # Overflow unused food budget into activities
    activity_budget = activity_target + max(0, food_target - food_cost)
    act_items   = _select_activities(activity_options, activity_budget, num_activities)
    activity_cost = sum(a["cost"] for a in act_items)
    activity_names = [a["name"] for a in act_items]

    # Build the slot schedule — use its actual total as the authoritative figure
    slot_schedule   = _build_schedule(food_items, act_items, transport, available_hours)
    scheduled_total = sum(s["cost"] for s in slot_schedule)

    utilization = round(scheduled_total / budget * 100, 1) if budget else 0
    gap         = max(0.0, budget * 0.8 - scheduled_total)

    return {
        "food_cost": round(food_cost, 0),
        "activity_cost": round(activity_cost, 0),
        "transport_cost": transport,
        "total_estimated_cost": round(scheduled_total, 0),
        "budget": budget,
        "remaining_budget": round(budget - scheduled_total, 0),
        "fits_budget": scheduled_total <= budget,
        "budget_utilization_pct": utilization,
        "budget_gap_to_80pct": round(gap, 0),
        "recommendations": _recommendations(scheduled_total, budget, utilization, gap),
        "selected_food_names": food_names,
        "selected_activity_names": activity_names,
        "num_meals": len(food_items),
        "num_activities": len(act_items),
        "food_budget_target": food_target,
        "activity_budget_target": activity_target,
        # Pre-computed schedule passed to LLM as a cost template
        "slot_schedule": slot_schedule,
    }


# ─── Food selection ──────────────────────────────────────────────────────────

def _select_food(food_options: List[Dict], food_target: float, num_meals: int) -> List[Dict]:
    """Greedily pick the most expensive meals that fit within food_target."""
    if not food_options:
        return []

    by_cost = sorted(food_options, key=lambda r: r.get("cost_for_two", 0) or 0, reverse=True)
    selected, remaining, used = [], food_target, set()

    while len(selected) < num_meals and remaining > 0:
        added = False
        for r in by_cost:
            name = r.get("name", "")
            if name in used:
                continue
            pp = (r.get("cost_for_two", 0) or 0) / 2
            if pp > 0 and pp <= remaining:
                selected.append({"name": name, "cost": pp, "raw": r})
                used.add(name)
                remaining -= pp
                added = True
                break
        if not added:
            break  # nothing fits remaining budget

    return selected


# ─── Activity selection ───────────────────────────────────────────────────────

def _select_activities(activity_options: List[Dict], budget: float, max_n: int) -> List[Dict]:
    """
    Select activities with diversity-first: one per category before greedy fill.
    This ensures all user interests appear in the plan, not just the priciest category.
    """
    if not activity_options:
        return []

    selected: List[Dict] = []
    remaining = budget
    used: set = set()

    # Group by category — cheapest first within each group so we don't blow budget on one category
    by_cat: Dict[str, List[Dict]] = {}
    for act in activity_options:
        cat = act.get("category", "Activity")
        by_cat.setdefault(cat, []).append(act)
    for cat in by_cat:
        by_cat[cat].sort(key=lambda a: a.get("estimated_cost", 0) or 0)

    # Pass 1: one item per category (cheapest that fits)
    for cat, acts in by_cat.items():
        if len(selected) >= max_n:
            break
        for act in acts:
            name = act.get("name", "")
            ec   = act.get("estimated_cost", 0) or 0
            if name in used:
                continue
            if ec <= remaining:
                selected.append({"name": name, "cost": ec, "raw": act})
                used.add(name)
                remaining -= ec
                break

    # Pass 2: fill remaining slots greedily (most expensive first)
    by_cost = sorted(activity_options, key=lambda a: a.get("estimated_cost", 0) or 0, reverse=True)
    for act in by_cost:
        if len(selected) >= max_n:
            break
        name = act.get("name", "")
        ec   = act.get("estimated_cost", 0) or 0
        if name in used:
            continue
        if ec <= remaining:
            selected.append({"name": name, "cost": ec, "raw": act})
            used.add(name)
            remaining -= ec

    # Fallback: take the cheapest paid activity if nothing fits
    if not selected:
        paid = sorted(activity_options, key=lambda a: a.get("estimated_cost", 0) or 0)
        for a in paid:
            if (a.get("estimated_cost", 0) or 0) > 0:
                selected = [{"name": a.get("name", ""), "cost": a.get("estimated_cost", 0) or 0, "raw": a}]
                break

    return selected


# ─── Slot schedule builder ───────────────────────────────────────────────────

def _build_schedule(food_items, act_items, transport, available_hours) -> List[Dict]:
    """
    Build a time-ordered schedule from pre-selected food and activity items.
    Uses fixed candidate slots so there's no infinite-loop risk.
    """
    end_hour = min(10 + int(available_hours), 22)
    MAX_EVENTS = 7  # hard cap on food+activity events (transport added separately)

    # Candidate times for food and activities — meals anchor the day.
    # Act slots: 11, 14, 15, 16, 17 (five fit in a half_day ending at 18).
    food_candidates = [10, 13, 17, 19, 20, 21]
    act_candidates  = [11, 14, 15, 16, 17, 20, 21]

    events: list = []  # list of (hour, name, category, cost)

    food_q = list(food_items)
    act_q  = list(act_items)

    # Interleave: fill candidate slots in time order
    fi, ai = 0, 0
    while (food_q or act_q) and len(events) < MAX_EVENTS:
        fc = food_candidates[fi] if fi < len(food_candidates) else 99
        ac = act_candidates[ai]  if ai < len(act_candidates)  else 99

        if fc > end_hour and ac > end_hour:
            break  # both exhausted valid times

        if fc <= ac and food_q and fc <= end_hour:
            f = food_q.pop(0)
            events.append((fc, f["name"], "Food", f["cost"]))
            fi += 1
        elif act_q and ac <= end_hour:
            a = act_q.pop(0)
            events.append((ac, a["name"], a["raw"].get("category", "Activity"), a["cost"]))
            ai += 1
        else:
            # Advance whichever pointer was chosen but had nothing
            if fc <= ac:
                fi += 1
            else:
                ai += 1

    # Sort by time and build slot dicts
    events.sort(key=lambda e: e[0])
    slots = [
        {
            "time": f"{h:02d}:00 {'AM' if h < 12 else 'PM'}",
            "name": name,
            "category": cat,
            "cost": round(cost, 0),
        }
        for h, name, cat, cost in events
    ]

    # Transport at the end (clamped to end_hour)
    if transport > 0:
        last_h = events[-1][0] + 1 if events else 14
        transport_h = min(last_h, end_hour)
        slots.append({
            "time": f"{transport_h:02d}:00 {'AM' if transport_h < 12 else 'PM'}",
            "name": "Transport (auto/cab between spots)",
            "category": "Transport",
            "cost": round(transport, 0),
        })

    return slots


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _transport_cost(budget_category: str) -> float:
    return {"low": 50, "medium": 150, "high": 300, "premium": 500}.get(budget_category, 150)


def _recommendations(total, budget, utilization, gap) -> List[str]:
    if total > budget:
        return [f"Plan is ₹{total - budget:.0f} over budget."]
    if gap > 0:
        return [f"₹{gap:.0f} short of 80% target — upgrade to a pricier restaurant to close it."]
    return [f"Budget well used — {utilization}% allocated."]
