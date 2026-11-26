"""Career Memory local FastAPI foundation scaffold."""

from career_memory_api.app import create_app
from career_memory_api.config import Settings

__all__ = ["Settings", "create_app"]
