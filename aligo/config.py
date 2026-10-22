"""Environment-driven settings with no embedded credentials."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, 'true' if default else 'false').lower() in {'true', 'yes', '1'}


def _model_timeout_ceiling() -> float:
    # Preserve historical defaults unless a slow-model deployment explicitly
    # opts into longer runtime deadlines.
    return 3600. if _flag('ALIGO_EXTENDED_MODEL_TIMEOUT') else 300.


@dataclass(frozen=True)
class Settings:
    db_path: Path = (ROOT / os.getenv('ALIGO_DATABASE_PATH', 'data/aligo_v3.sqlite3')).resolve()
    access_token: str = os.getenv('ALIGO_ACCESS_TOKEN', '')
    cors_origins: tuple[str, ...] = tuple(x.strip() for x in os.getenv('ALIGO_CORS_ORIGINS', 'http://127.0.0.1:5173,http://localhost:5173').split(',') if x.strip())
    allow_online: bool = _flag('ALIGO_ALLOW_ONLINE', False)
    request_timeout: float = float(os.getenv('ALIGO_REQUEST_TIMEOUT', '12'))
    max_skill_calls: int = max(1, min(int(os.getenv('ALIGO_MAX_SKILL_CALLS', '8')), 24))
    max_recovery_steps: int = max(0, min(int(os.getenv('ALIGO_MAX_RECOVERY_STEPS', '2')), 8))
    max_parallel_skills: int = max(1, min(int(os.getenv('ALIGO_MAX_PARALLEL_SKILLS', '4')), 12))
    intent_timeout: float = max(1., min(float(os.getenv('ALIGO_INTENT_TIMEOUT', '25')), _model_timeout_ceiling()))
    daily_generation_timeout: float = max(65., min(float(os.getenv('ALIGO_DAILY_GENERATION_TIMEOUT', '180')), _model_timeout_ceiling()))
    execution_timeout: float = max(1., min(float(os.getenv('ALIGO_EXECUTION_TIMEOUT', '90')), _model_timeout_ceiling()))
    synthesis_timeout: float = max(1., min(float(os.getenv('ALIGO_SYNTHESIS_TIMEOUT', '15')), _model_timeout_ceiling()))
    memory_window: int = max(10, min(int(os.getenv('ALIGO_MEMORY_WINDOW', '40')), 120))
    models_file: Path = (ROOT / os.getenv('ALIGO_MODELS_FILE', 'config/models.json')).resolve()
    provider_file: Path = (ROOT / os.getenv('ALIGO_PROVIDERS_FILE', 'config/providers.json')).resolve()
    extensions_dir: Path = (ROOT / os.getenv('ALIGO_SKILLS_DIRECTORY', 'plugins/skills')).resolve()
    embedding_id: str = os.getenv('ALIGO_EMBEDDING_MODEL', 'intfloat/multilingual-e5-large-instruct')
    reranker_id: str = os.getenv('ALIGO_RERANKER_MODEL', 'mixedbread-ai/mxbai-rerank-base-v2')
    retrieval_mode: str = os.getenv('ALIGO_RETRIEVAL_MODE', 'lexical')
    agent_mode: str = os.getenv('ALIGO_AGENT_MODE', 'model-first')
    duffel_token: str = os.getenv('DUFFEL_ACCESS_TOKEN', '')
    duffel_mode: str = os.getenv('DUFFEL_MODE', 'test')
    ors_token: str = os.getenv('ORS_API_KEY', '')

settings = Settings()
