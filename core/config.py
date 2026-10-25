"""Environment-driven settings with no embedded credentials."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from dotenv import load_dotenv

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_CHECKOUT = (_SOURCE_ROOT / 'config' / 'models.json').is_file()
# In a wheel, writable state is separate from installed, read-only assets.
ROOT = _SOURCE_ROOT if _SOURCE_CHECKOUT else (_SOURCE_ROOT / 'poreiago_resources')
if not _SOURCE_CHECKOUT and not (ROOT / 'config' / 'models.json').is_file():
    raise RuntimeError('PoreiaGo installed resources are missing. Reinstall the package.')
load_dotenv((ROOT if _SOURCE_CHECKOUT else Path.cwd()) / '.env')


def _data_root() -> Path:
    if _SOURCE_CHECKOUT:
        return ROOT
    if os.name == 'nt':
        home = Path(os.getenv('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local'))
    else:
        home = Path(os.getenv('XDG_DATA_HOME') or (Path.home() / '.local' / 'share'))
    return home / 'PoreiaGo'


def _database_path() -> Path:
    configured = os.getenv('POREIAGO_DATABASE_PATH')
    if configured:
        path = Path(configured).expanduser()
        # For wheel installs, a relative explicit path belongs to the caller's
        # directory rather than to site-packages.
        return (ROOT / path if _SOURCE_CHECKOUT else Path.cwd() / path).resolve()
    return (_data_root() / ('data/poreiago.sqlite3' if _SOURCE_CHECKOUT else 'poreiago.sqlite3')).resolve()



def _load_legacy_environment() -> None:
    """Accept existing ALIGO_ settings while favoring the POREIAGO_ namespace.

    This compatibility shim can be removed after existing deployments migrate
    their private .env files; it never logs or serializes credentials.
    """
    for name, value in tuple(os.environ.items()):
        if name.startswith('ALIGO_'):
            os.environ.setdefault('POREIAGO_' + name[len('ALIGO_'):], value)


_load_legacy_environment()


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, 'true' if default else 'false').lower() in {'true', 'yes', '1'}


def _model_timeout_ceiling() -> float:
    # Preserve historical defaults unless a slow-model deployment explicitly
    # opts into longer runtime deadlines.
    return 3600. if _flag('POREIAGO_EXTENDED_MODEL_TIMEOUT') else 300.


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=_database_path)
    access_token: str = os.getenv('POREIAGO_ACCESS_TOKEN', '')
    cors_origins: tuple[str, ...] = tuple(x.strip() for x in os.getenv('POREIAGO_CORS_ORIGINS', 'http://127.0.0.1:5173,http://localhost:5173').split(',') if x.strip())
    allow_online: bool = _flag('POREIAGO_ALLOW_ONLINE', False)
    request_timeout: float = float(os.getenv('POREIAGO_REQUEST_TIMEOUT', '12'))
    max_skill_calls: int = max(1, min(int(os.getenv('POREIAGO_MAX_SKILL_CALLS', '8')), 24))
    max_recovery_steps: int = max(0, min(int(os.getenv('POREIAGO_MAX_RECOVERY_STEPS', '2')), 8))
    max_parallel_skills: int = max(1, min(int(os.getenv('POREIAGO_MAX_PARALLEL_SKILLS', '4')), 12))
    intent_timeout: float = max(1., min(float(os.getenv('POREIAGO_INTENT_TIMEOUT', '25')), _model_timeout_ceiling()))
    daily_generation_timeout: float = max(65., min(float(os.getenv('POREIAGO_DAILY_GENERATION_TIMEOUT', '180')), _model_timeout_ceiling()))
    execution_timeout: float = max(1., min(float(os.getenv('POREIAGO_EXECUTION_TIMEOUT', '90')), _model_timeout_ceiling()))
    synthesis_timeout: float = max(1., min(float(os.getenv('POREIAGO_SYNTHESIS_TIMEOUT', '15')), _model_timeout_ceiling()))
    memory_window: int = max(10, min(int(os.getenv('POREIAGO_MEMORY_WINDOW', '40')), 120))
    models_file: Path = (ROOT / os.getenv('POREIAGO_MODELS_FILE', 'config/models.json')).resolve()
    provider_file: Path = (ROOT / os.getenv('POREIAGO_PROVIDERS_FILE', 'config/providers.json')).resolve()
    extensions_dir: Path = (ROOT / os.getenv('POREIAGO_SKILLS_DIRECTORY', 'plugins/skills')).resolve()
    embedding_id: str = os.getenv('POREIAGO_EMBEDDING_MODEL', 'intfloat/multilingual-e5-large-instruct')
    reranker_id: str = os.getenv('POREIAGO_RERANKER_MODEL', 'mixedbread-ai/mxbai-rerank-base-v2')
    retrieval_mode: str = os.getenv('POREIAGO_RETRIEVAL_MODE', 'lexical')
    agent_mode: str = os.getenv('POREIAGO_AGENT_MODE', 'model-first')
    duffel_token: str = os.getenv('DUFFEL_ACCESS_TOKEN', '')
    duffel_mode: str = os.getenv('DUFFEL_MODE', 'test')
    ors_token: str = os.getenv('ORS_API_KEY', '')

settings = Settings()
