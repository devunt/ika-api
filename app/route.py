import asyncio
import json
from async_timeout import timeout
from typing import Optional
from fastapi import WebSocket
from pydantic import BaseModel
from app.db import Application, Session
from app.redis import redis


class Chat(BaseModel):
    token: Optional[str]
    sender: str
    target: str
    message: str


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


async def post_chat(chat: Chat):
    app = await get_app(chat.token)
    if not app:
        return {'code': 'invalid_token'}

    return await send_message(app, chat)


websockets = []


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
                if ws.state.app:
                    await ws.send_json(await send_message(ws.state.app, Chat(**data)))
                else:
                    await ws.send_json({'code': 'unauthorized'})
    finally:
        websockets.remove(ws)


def websocket_loop(message: str):
    print(message)


async def redis_subscribe():
    pubsub = redis.pubsub()
    await pubsub.subscribe('from-ika')
    while True:
        try:
            async with timeout(1):
                message = await pubsub.get_message(ignore_subscribe_messages=True)
                if message:
                    data = json.loads(message['data'])
                    await redis_process_event(data)
                await asyncio.sleep(0.01)
        except asyncio.TimeoutError:
            pass


async def redis_process_event(event: dict):
    if event['event'] == 'chat_message':
        if 'app@apps/' in event['sender']:
            sender = event['sender'].split('+')[0]
            origin = event['sender'].split('/')[1]
        else:
            sender = event['sender'].split('!')[0]
            origin = '*'

        for ws in websockets:
            if not hasattr(ws.state, 'channels'):
                continue

            if event['recipient'].lower() in ws.state.channels:
                await ws.send_json({
                    'code': 'message',
                    'origin': origin,
                    'sender': sender,
                    'target': event['recipient'],
                    'message': event['message'],
                })

asyncio.create_task(redis_subscribe())
