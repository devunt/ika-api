from fastapi import FastAPI
from app.route import post_chat, websocket_chat

app = FastAPI()
app.add_route('/chat', post_chat)
app.add_websocket_route('/chat', websocket_chat)
