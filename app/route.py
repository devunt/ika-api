import json
from typing import Optional
from fastapi import APIRouter, Header, WebSocket, Path
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from app.db import Application, Session, Snippet
from app.redis import redis, redis_listeners


class Chat(BaseModel):
    token: Optional[str]
    sender: str
    target: str
    message: str


router = APIRouter()
websockets = []


@router.get('/')
async def index():
    return {}


@router.get('/snippets/{id}', response_class=PlainTextResponse)
async def snippet(id: str = Path(...)):
    with Session() as session:
        return session.get(Snippet, id).content


@router.post('/chat')
async def post_chat(chat: Chat, x_oz_token: Optional[str] = Header(None)):
    app = await get_app(x_oz_token or chat.token)
    if not app:
        return {'code': 'invalid_token'}

    return await send_message(app, chat)


@router.websocket('/chat')
async def websocket_chat(ws: WebSocket):
    await ws.accept()
    websockets.append(ws)
    try:
        while True:
            data = await ws.receive_json()
            if data['action'] == 'authenticate':
                ws.state.app = await get_app(data['token'])
                if ws.state.app:
                    ws.state.channels = list(map(lambda x: x.name.lower(), ws.state.app.channels_collection))
                    await ws.send_json({
                        'code': 'authenticated',
                        'name': ws.state.app.name,
                        'slug': ws.state.app.slug,
                        'channels': ws.state.channels,
                    })
                else:
                    await ws.send_json({'code': 'invalid_token'})
            elif data['action'] == 'message':
                if hasattr(ws.state, 'app'):
                    await ws.send_json(await send_message(ws.state.app, Chat(**data)))
                else:
                    await ws.send_json({'code': 'unauthorized'})
    except WebSocketDisconnect:
        pass
    finally:
        websockets.remove(ws)


async def get_app(token: str):
    appid, secret_key = token.split('@')
    return Session().query(Application).filter(Application.id == appid, Application.secret_key == secret_key).first()


async def send_message(app: Application, chat: Chat):
    for channel in app.channels_collection:
        if channel.name.lower() == chat.target.lower():
            break
    else:
        return {'code': 'unauthorized_channel'}

    event = json.dumps({
        'event': 'chat_message',
        'sender': f'{chat.sender}+!app@apps/{app.slug}',
        'recipient': chat.target,
        'message': chat.message,
    })
    await redis.publish('to-ika', event)
    await redis.publish('from-ika', event)
    return {'code': 'sent'}


async def redis_listener(event: dict):
    if event['event'] == 'chat_message':
        if 'app@apps/' in event['sender']:
            sender = event['sender'].split('+')[0]
            origin = event['sender'].split('/')[1]
        else:
            sender = event['sender'].split('!')[0]
            origin = '*'

        for ws in websockets:
            if not hasattr(ws.state, 'app'):
                continue

            if ws.state.app.slug == origin:
                continue

            if event['recipient'].lower() in ws.state.channels:
                await ws.send_json({
                    'code': 'message',
                    'origin': origin,
                    'sender': sender,
                    'target': event['recipient'],
                    'message': event['message'],
                })


redis_listeners.append(redis_listener)
