from fastapi import FastAPI
from app.route import router as default_router
from app.integration.slack import router as slack_router

app = FastAPI()
app.include_router(default_router)
app.include_router(slack_router, prefix="/integration/slack")
