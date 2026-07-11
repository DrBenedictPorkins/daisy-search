"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings

from daisy.constants import (
    DEFAULT_EXTRACTION_MODEL,
    DEFAULT_NEO4J_AUTH_PASS,
    DEFAULT_NEO4J_AUTH_USER,
    DEFAULT_NEO4J_URL,
    DEFAULT_OLLAMA_EXTRACTION_MODEL,
    DEFAULT_RESOLUTION_MODEL,
    DEFAULT_TRIAGE_MODEL,
)


class Settings(BaseSettings):
    """Application settings populated from environment variables or .env file."""

    # Database
    database_url: str = (
        "postgresql+asyncpg://daisy:daisy_dev@localhost:5432/daisy"
    )
    database_url_sync: str = (
        "postgresql+psycopg2://daisy:daisy_dev@localhost:5432/daisy"
    )

    # Redis (still used for optional batch mode)
    redis_url: str = "redis://localhost:6379/0"

    # Logging
    log_level: str = "INFO"

    # LLM models
    extraction_model: str = DEFAULT_EXTRACTION_MODEL
    resolution_model: str = DEFAULT_RESOLUTION_MODEL
    triage_model: str = DEFAULT_TRIAGE_MODEL

    # Ollama (priority 1 for extraction)
    ollama_url: str | None = None
    ollama_extraction_model: str = DEFAULT_OLLAMA_EXTRACTION_MODEL

    # Neo4j
    neo4j_url: str = DEFAULT_NEO4J_URL
    neo4j_user: str = DEFAULT_NEO4J_AUTH_USER
    neo4j_password: str = DEFAULT_NEO4J_AUTH_PASS

    # API keys
    anthropic_api_key: str | None = None

    # Search backends (set one or both)
    brave_search_api_key: str | None = None
    searxng_url: str | None = None

    # Crawl4AI (optional — enables daisy_fetch_clean and /extract endpoint)
    crawl4ai_url: str | None = None
    crawl4ai_api_token: str | None = None

    # Project workspace (root dir for project folders)
    projects_dir: str = "projects"

    # Web dashboard
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
