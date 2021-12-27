import aioredis
from .conf import settings

redis = aioredis.from_url(settings.redis_url)
