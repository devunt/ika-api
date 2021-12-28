from pydantic import BaseSettings


class Settings(BaseSettings):
    redis_url: str
    database_url: str

    slack_client_id: str
    slack_client_secret: str
    slack_signing_secret: bytes


settings = Settings()
