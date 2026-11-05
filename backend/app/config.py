"""Application settings, loaded from environment (.env supported)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FUBALA_",
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_path: Path = BACKEND_ROOT / "data" / "fubala.db"
    # Bind host for uvicorn (FUBALA_HOST). A self-update restart reuses this
    # so the server always comes back on the interface it was started with.
    host: str = "127.0.0.1"
    # Bind port (FUBALA_PORT). One setting decides the bind for both the
    # `python -m app` start command and every self-update restart.
    port: int = 8000
    # Overrides the registry's default_model when set (FUBALA_DEFAULT_MODEL).
    default_model: str | None = None
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Explicit output budget per model call (FUBALA_MAX_OUTPUT_TOKENS). Without
    # it litellm's anthropic adapter falls back to 4096 for unknown models, and a
    # thinking model (glm-5.3 via z.ai's anthropic endpoint) spends all of it on
    # thinking: no text, no tool calls. None = let the provider decide.
    max_output_tokens: int | None = 16384
    # Idle bound per streamed chunk, in seconds (FUBALA_STREAM_IDLE_TIMEOUT_SECONDS).
    # A model stream that sends nothing for this long fails that one round,
    # which the agent repeats (see agent.MAX_STREAM_STALL_RETRIES). 0 or less
    # disables the bound. A model with a long silent "thinking" phase before
    # its first chunk can trip the default: raise it for such models.
    stream_idle_timeout_seconds: float = 180.0


def get_settings() -> Settings:
    return Settings()
