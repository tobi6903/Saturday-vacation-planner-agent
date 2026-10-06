from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any


class PlannerInput(BaseModel):
    city: str
    budget: float = Field(gt=0, description="Total budget in INR")
    available_time: str
    mood: str
    interests: List[str]
    constraints: List[str]


class TimeSlot(BaseModel):
    time: str
    activity: str
    location: str
    address: Optional[str] = None
    duration: str
    estimated_cost: float
    category: str
    why: str
    tips: Optional[str] = None
    rating: Optional[float] = None
    maps_url: Optional[str] = None


class FinalPlan(BaseModel):
    title: str
    summary: str
    time_slots: List[TimeSlot]
    total_estimated_cost: float
    budget: float
    remaining_budget: float
    trade_offs: Optional[str] = None
    fallback_plan: Optional[str] = None
    agent_notes: Optional[str] = None


class AgentEvent(BaseModel):
    type: str  # "trace" | "tool_call" | "tool_result" | "plan" | "error"
    message: str
    data: Optional[Dict[str, Any]] = None
    step: Optional[int] = None
