"""Validate the plan against user constraints and preferences."""
from __future__ import annotations
from typing import Dict, Any, List


def validate_plan(
    food_options: List[Dict],
    activity_options: List[Dict],
    constraints: List[str],
    energy_level: str,
    available_hours: float,
    budget: float,
    total_cost: float,
) -> Dict[str, Any]:
    """Check plan against constraints and return validation results."""
    issues = []
    warnings = []
    suggestions = []
    valid = True

    constraint_lower = [c.lower() for c in constraints]
    is_veg_required = any("veg" in c for c in constraint_lower)
    avoid_crowd = any("crowd" in c or "busy" in c for c in constraint_lower)

    # Check vegetarian constraint
    if is_veg_required:
        non_veg_food = [f for f in food_options if not f.get("is_veg", True)]
        if non_veg_food:
            warnings.append(f"{len(non_veg_food)} restaurant(s) may not be fully vegetarian — verify before visiting.")

    # Check crowd constraint
    if avoid_crowd:
        popular_activities = [a for a in activity_options if a.get("popularity", 0.5) > 0.7]
        if popular_activities:
            warnings.append(
                f"'{popular_activities[0].get('name', '')}' may be crowded on weekends. "
                "Consider visiting early morning (before 9 AM) or opt for lesser-known spots."
            )
        suggestions.append("Visit popular spots early (before 10 AM) or on weekday alternatives.")

    # Check energy level vs activities
    if energy_level == "low":
        high_intensity = [a for a in activity_options if any(
            k in a.get("category", "").lower() for k in ["adventure", "sports", "trekking", "hiking"]
        )]
        if high_intensity:
            warnings.append(
                f"'{high_intensity[0].get('name', '')}' may be tiring given your current energy level. "
                "Consider swapping with a cafe visit or calm park walk."
            )

    # Check time feasibility
    if available_hours < 2:
        warnings.append("With less than 2 hours, you can realistically fit only 1 activity + a quick bite.")
        if len(activity_options) > 1:
            suggestions.append("Focus on one activity + one dining spot to keep it stress-free.")

    # Budget check
    if total_cost > budget:
        valid = False
        issues.append(
            f"Estimated cost (₹{total_cost:.0f}) exceeds budget (₹{budget:.0f}). "
            f"Consider cheaper alternatives or reducing number of activities."
        )

    # Positive validations
    if not issues and not warnings:
        suggestions.append("Your plan looks great — everything fits your preferences!")

    return {
        "valid": valid,
        "issues": issues,
        "warnings": warnings,
        "suggestions": suggestions,
        "energy_appropriate": energy_level != "low" or not any(
            "adventure" in a.get("category", "").lower() for a in activity_options
        ),
        "dietary_compliant": not is_veg_required or all(
            f.get("is_veg", True) for f in food_options
        ),
        "time_feasible": available_hours >= 2,
    }
