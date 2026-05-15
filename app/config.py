from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env.

    Every field has a permissive default so the app can boot during
    incremental development. Bricks that need a specific secret should
    raise a clear error at the point of use when their setting is empty.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_webhook_secret: SecretStr = SecretStr("")

    # Public-facing base URL for the backend (Telegram webhook target +
    # Splitwise OAuth redirect)
    public_base_url: str = ""

    # Splitwise OAuth 2.0
    splitwise_client_id: SecretStr = SecretStr("")
    splitwise_client_secret: SecretStr = SecretStr("")

    # AI providers
    groq_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: SecretStr = SecretStr("")

    # Fernet key for encrypting Splitwise tokens at rest
    token_encryption_key: SecretStr = SecretStr("")

    # Logging
    log_level: str = "INFO"


settings = Settings()
