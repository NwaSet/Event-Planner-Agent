import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import openai
from dotenv import load_dotenv
from rich.markup import escape
from rich.table import Table

from model import trace

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

class llm :

    def __init__() :
        pass

    def init_env() :
        load_dotenv(_ENV_PATH, override=False, interpolate=False)
        self.api_key = os.getenv("LLM_API_KEY", "").strip()
        self.base_url = os.getenv("LLM_BASE_URL", "").strip()
        self.model = os.getenv("LLM_MODEL", "").strip()