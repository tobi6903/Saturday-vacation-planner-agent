import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://openai.vocareum.com/v1")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
FOURSQUARE_API_KEY = os.getenv("FOURSQUARE_API_KEY", "")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")

MODEL = "gpt-4o-mini"

CITY_COORDS = {
    "bangalore": (12.9716, 77.5946),
    "bengaluru": (12.9716, 77.5946),
    "mumbai": (19.0760, 72.8777),
    "delhi": (28.6139, 77.2090),
    "new delhi": (28.6139, 77.2090),
    "hyderabad": (17.3850, 78.4867),
    "chennai": (13.0827, 80.2707),
    "kolkata": (22.5726, 88.3639),
    "pune": (18.5204, 73.8567),
    "ahmedabad": (23.0225, 72.5714),
    "jaipur": (26.9124, 75.7873),
    "surat": (21.1702, 72.8311),
    "lucknow": (26.8467, 80.9462),
    "goa": (15.2993, 74.1240),
    "chandigarh": (30.7333, 76.7794),
    "kochi": (9.9312, 76.2673),
    "coimbatore": (11.0168, 76.9558),
}
