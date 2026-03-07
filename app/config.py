"""Service configuration via environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """scout-engine configuration. All values can be set via environment variables."""

    # Database
    database_url: str = "postgresql+asyncpg://scout:scout@localhost:5432/scout_engine"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # Botasaurus defaults
    botasaurus_headless: bool = True
    default_timeout_ms: int = 30000
    default_step_delay_ms: int = 500

    # Webhook notifications (optional)
    webhook_url: str = ""

    # API key for remote access (leave empty for local dev)
    api_key: str = ""

    # Directories
    download_dir: str = "./downloads"
    screenshot_dir: str = "./screenshots"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
