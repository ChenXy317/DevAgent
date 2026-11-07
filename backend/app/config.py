from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "coding_agent"
    mysql_password: str = ""
    mysql_database: str = "coding_agent"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    search_api_key: str = ""
    workdir: str = "./workspace"
    projects_root: str = ""

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        extra="ignore",
    )

    def workdir_path(self) -> Path:
        raw = Path(self.workdir)
        if not raw.is_absolute():
            raw = REPO_ROOT / raw
        return raw.resolve()

    def projects_root_path(self) -> Path:
        raw = Path(self.projects_root).expanduser() if self.projects_root.strip() else Path.home() / "Projects"
        if not raw.is_absolute():
            raw = REPO_ROOT / raw
        return raw.resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
