from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str

    @field_validator("database_url")
    @classmethod
    def async_postgres(cls, value):
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix) :]
        return value


class Config(DatabaseConfig):
    bot_token: SecretStr
    admin_id: int = Field(gt=0)
    admin_ids: str = ""

    @field_validator("admin_ids")
    @classmethod
    def valid_admin_ids(cls, value):
        if value.strip():
            for item in value.split(","):
                if not item.strip().isdigit() or int(item.strip()) <= 0:
                    raise ValueError("ADMIN_IDS must contain positive Telegram IDs separated by commas")
        return value
    encryption_key: SecretStr
    redis_url: str = "redis://localhost:6379/0"
    public_base_url: str = ""
    railway_public_domain: str = ""
    mono_token: SecretStr
    manual_card: str = ""
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_vision_model: str = "deepseek-v4-flash-vision-exp"
    deepseek_timeout: int = Field(default=60, ge=10, le=180)
    google_client_id: str
    google_client_secret: SecretStr
    support_username: str
    page_size: int = Field(default=7, ge=1, le=20)
    code_cooldown: int = Field(default=30, ge=20)
    code_max_age: int = Field(default=180, ge=30, le=300)
    timezone: str = "Europe/Kyiv"

    @field_validator("encryption_key")
    @classmethod
    def valid_key(cls, value):
        Fernet(value.get_secret_value().encode())
        return value

    @model_validator(mode="after")
    def https_url(self):
        value = self.public_base_url.strip()
        if not value and self.railway_public_domain:
            value = "https://" + self.railway_public_domain.strip()
        if not value and self.manual_card.strip():
            # A personal-card bot can work from long polling without a public HTTP URL.
            # Gmail OAuth remains unavailable until an HTTPS URL is configured.
            self.public_base_url = ""
            return self
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Set PUBLIC_BASE_URL to an HTTPS origin or generate a Railway public domain")
        self.public_base_url = value.rstrip("/")
        return self


@lru_cache
def config():
    return Config()
