from pydantic import BaseSettings


class Settings(BaseSettings):
    redis_url: str
    database_url: str

    discord_client_id: str
    discord_client_secret: str
    discord_token: str

    slack_client_id: str
    slack_client_secret: str
    slack_signing_secret: str


settings = Settings()
