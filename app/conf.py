from pydantic import BaseSettings


class Settings(BaseSettings):
    redis_url: str
    database_url: str


settings = Settings()
