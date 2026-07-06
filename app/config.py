import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "False")  # Gemini API key only

@dataclass
class AgentConfig:
    # Reads model from environment GEMINI_MODEL. Default gemini-2.5-flash (the 1.5 family is retired and returns 404). Use gemini-2.5-flash-lite for tighter free-tier quota.
    model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    mcp_server_port: int = 8090
    max_iterations: int = 3
    pii_redaction_enabled: bool = True
    injection_detection_enabled: bool = True

    # Quota / Exhaustion Status Tracked Today (2026-07-06):
    # - 'gemini-2.5-pro': 0 limit (ClientError, 429 RESOURCE_EXHAUSTED)
    # - 'gemini-2.0-flash': 0 limit (ClientError, 429 RESOURCE_EXHAUSTED)
    # - 'gemini-2.5-flash': 20 requests/day (Exhausted, 429 RESOURCE_EXHAUSTED)
    # - 'gemini-2.5-flash-lite': 20 requests/day (Exhausted, 429 RESOURCE_EXHAUSTED)
    confirmed_exhausted_models: dict[str, str] = None

    def __post_init__(self):
        self.confirmed_exhausted_models = {
            "gemini-2.5-pro": "0 limit - Blocked (429 Resource Exhausted)",
            "gemini-2.0-flash": "0 limit - Blocked (429 Resource Exhausted)",
            "gemini-2.5-flash": "20/day limit - Exhausted (429 Resource Exhausted)",
            "gemini-2.5-flash-lite": "20/day limit - Exhausted (429 Resource Exhausted)"
        }

config = AgentConfig()
