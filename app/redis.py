import asyncio
import json

import aioredis
from async_timeout import timeout

from .conf import settings

redis = aioredis.from_url(settings.redis_url)

redis_listeners = []


async def redis_subscribe():
    pubsub = redis.pubsub()
    await pubsub.subscribe('from-ika')
    while True:
        try:
            async with timeout(1):
                message = await pubsub.get_message(ignore_subscribe_messages=True)
                if message:
                    data = json.loads(message['data'])
                    for listener in redis_listeners:
                        try:
                            asyncio.create_task(listener(data))
                        except:
                            pass
                await asyncio.sleep(0.01)
        except asyncio.TimeoutError:
            pass


asyncio.create_task(redis_subscribe())
