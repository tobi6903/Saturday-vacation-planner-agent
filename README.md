# Perfect Saturday Planner ✨

An AI-powered agent that crafts a personalised Saturday plan based on your city, budget, mood, interests, and constraints.

## Features

- **6 real tools** orchestrating the plan: preferences parser, food finder, activity finder, event searcher, cost estimator, plan validator
- **Real data sources**: Swiggy (food), Foursquare (activities), Google Places (fallback), Tavily (events)
- **Budget-aware**: recommends cheap places for low budgets, premium for high budgets
- **Live agent trace** streaming via Server-Sent Events (SSE)
- **Graceful fallbacks** at every API layer
- **Constraint handling**: vegetarian, avoid crowds, energy level, etc.

## Tech Stack

- **Backend**: FastAPI (Python)
- **AI**: OpenAI GPT-4o-mini via Vocareum proxy
- **Food API**: Swiggy (unofficial) → Google Places fallback
- **Activities API**: Foursquare Places v3 → Google Places fallback
- **Events**: Tavily search
- **Frontend**: Vanilla HTML/JS + Tailwind CSS

## Local Setup

```bash
# 1. Clone / extract the project
cd sat_planner

# 2. Create a virtual environment
python -m venv venv
venv\Scripts\activate   # Windows
# source venv/bin/activate  # Mac/Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env with your API keys

# 5. Run the server
uvicorn app.main:app --reload --port 8000

# 6. Open http://localhost:8000 in your browser
```

## Environment Variables

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | OpenAI / Vocareum API key |
| `OPENAI_API_BASE` | API base URL (default: Vocareum proxy) |
| `TAVILY_API_KEY` | Tavily search API key |
| `FOURSQUARE_API_KEY` | Foursquare Places API key |
| `GOOGLE_PLACES_API_KEY` | Google Places API key |

## Deploy to Railway

1. Push to GitHub
2. Create a new project on [Railway](https://railway.app)
3. Connect the GitHub repo
4. Add environment variables in Railway dashboard
5. Deploy — Railway auto-detects `railway.toml`

## How AI Tools Were Used

- **Claude Code** was used to architect the full project structure, help in backend tools, help the agent orchestration, and build the streaming frontend in a single session.
- **OpenAI GPT-4o-mini** (via Vocareum proxy) is used at runtime to generate the final personalised plan from tool results.
- The agent pattern (parse → gather → estimate → validate → generate) was designed to match the assignment's requirement of 3+ distinct tool functions.

## Agent Flow

```
User Input
    ↓
1. parse_preferences()    — budget category, energy, dietary filters
    ↓
2. get_food_options()     — Swiggy → Google Places → curated fallback
3. get_activity_options() — Foursquare → Google Places → curated fallback
4. search_events()        — Tavily (if music/events interest detected)
    ↓
5. estimate_cost()        — budget breakdown, utilisation %
6. validate_plan()        — constraint checks, warnings
    ↓
7. LLM (GPT-4o-mini)      — generates final structured plan with explanations
    ↓
Streamed to Frontend via SSE
```
