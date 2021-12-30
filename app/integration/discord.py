import asyncio
import json
import re
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Query, Path
from fastapi.responses import RedirectResponse
from discord import Bot, Message, Permissions, Interaction, ApplicationContext, Option, ChannelType, MessageType, \
    AllowedMentions, Intents
from discord.utils import oauth_url

from app.conf import settings
from app.db import Session, Channel, ChannelIntegration, Snippet
from app.redis import redis_listeners, redis
from app.util import sanitize_nickname

router = APIRouter()
intents = Intents.default()
intents.members = True
discord = Bot(intents=intents)
commands = discord.create_group(
    name='ozinger',
    description='오징어 IRC 채널 연동 관리',
)


@router.get('/install')
async def install_app():
    url = oauth_url(
        client_id=settings.discord_client_id,
        permissions=Permissions(
            read_messages=True,
            send_messages=True,
            manage_webhooks=True,
        ),
        scopes=[
            'applications.commands',
            'bot',
        ],
    )
    return RedirectResponse(url)


@discord.event
async def on_ready():
    print(f'Connected to discord as {discord.user}')


@discord.event
async def on_message(message: Message):
    if message.author.id == discord.user.id:
        return

    if message.type != MessageType.default:
        return

    with Session() as session:
        integration = session.query(ChannelIntegration).filter(
            ChannelIntegration.type == 'discord',
            ChannelIntegration.target == message.channel.id,
            ChannelIntegration.is_authorized == True,
        ).first()
        if not integration:
            return

        if message.author.id == int(integration.extra):
            return

        content = message.content

        print(f'Route message from Discord[{message.channel.id}] to IRC[{integration.channels.name}]: {content}')

        for mention in message.mentions:
            content = content.replace(f'<@!{mention.id}>', f'@{mention.name}')

        for full, code in re.findall(r'(```(.+?)```)', content, re.DOTALL):
            code = '\n'.join(map(lambda x: f'`{x}`', code.strip().splitlines()))
            content = content.replace(full, code)

        for attachment in message.attachments:
            content += f'\n{attachment.url}'

        lines = content.splitlines()
        if len(lines) > 5:
            snippet = Snippet(content=content)
            session.add(snippet)
            session.commit()

            lines = [f'https://api.ozinger.org/snippets/{snippet.id}']

    sender = sanitize_nickname(message.author.display_name)

    for line in lines:
        if not line:
            continue

        await redis.publish('to-ika', json.dumps({
            'event': 'chat_message',
            'sender': f'{sender}＠d!integration@integrations/{integration.type}/{integration.id}',
            'recipient': integration.channels.name,
            'message': line,
        }))


@commands.command(name="attach", description="오징어 IRC 네트워크의 채널과 연동합니다")
async def add_integration(ctx: ApplicationContext, target: Option(str, '오징어 IRC 네트워크 채널 이름')):
    irc_channel_name = target

    if ctx.channel.type != ChannelType.text:
        return await ctx.respond('채널에서만 실행할 수 있는 명령입니다.')

    with Session() as session:
        if session.query(ChannelIntegration).filter(ChannelIntegration.type == 'discord',
                                                    ChannelIntegration.target == ctx.channel_id).first():
            return await ctx.respond(f'이 채널에는 이미 연동이 등록되어 있습니다.')

        irc_channel = session.query(Channel).filter(Channel.name == irc_channel_name).first()
        if not irc_channel:
            return await ctx.respond(f'오징어 IRC 네트워크에 `{irc_channel_name}` 채널이 등록되어 있지 않습니다.')

        webhook = await ctx.channel.create_webhook(
            name="Ozinger IRC Network Integration",
            reason=f"Ozinger IRC Network Integration with IRC channel {irc_channel_name}"
        )

        integration = ChannelIntegration(
            channel=irc_channel.id,
            type='discord',
            target=ctx.channel_id,
            extra=webhook.id,
            is_authorized=False,
            created_at=datetime.now()
        )
        session.add(integration)
        session.commit()

    await redis.publish('to-ika', json.dumps({
        'event': 'add_integration',
        'channel': irc_channel_name,
        'integrationId': integration.id,
    }))

    await ctx.respond(f'오징어 IRC 네트워크의 `{irc_channel_name}` 채널에 연동을 요청했습니다.')


@commands.command(name="detach", description="오징어 IRC 네트워크의 채널과 연동을 취소합니다.")
async def remove_integration(ctx: ApplicationContext):
    with Session() as session:
        integration = session.query(ChannelIntegration).filter(
            ChannelIntegration.type == 'discord',
            ChannelIntegration.target == ctx.channel_id
        ).first()
        if not integration:
            return await ctx.respond('이 채널은 오징어 IRC 네트워크 채널과 연동되어 있지 않습니다.')

        for webhook in await ctx.channel.webhooks():
            if webhook.id == int(integration.extra):
                await webhook.delete()
                break

        integration_id = integration.id
        irc_channel_name = integration.channels.name

        session.delete(integration)
        session.commit()

    await redis.publish('to-ika', json.dumps({
        'event': 'remove_integration',
        'channel': irc_channel_name,
        'integrationId': integration_id,
    }))

    await ctx.respond(f'오징어 IRC 네트워크 `{irc_channel_name}` 채널과의 연동이 해제되었습니다.')


async def redis_listener(event: dict):
    with Session() as session:
        if event['event'] == 'chat_message':
            sender = event['sender'].split('!')[0]
            sender_parts = sender.split('＠')
            if len(sender_parts) == 2 and sender_parts[1] == 'd':
                return

            irc_channel = session.query(Channel).filter(Channel.name == event['recipient']).first()
            if not irc_channel:
                return

            integrations = session.query(ChannelIntegration).filter(
                ChannelIntegration.type == 'discord',
                ChannelIntegration.channel == irc_channel.id,
                ChannelIntegration.is_authorized == True,
            ).all()
            if not integrations:
                return

            content = event['message']

            for integration in integrations:
                discord_channel_id = int(integration.target)

                print(f'Route message from IRC[{irc_channel.name}] to Discord[{discord_channel_id}]: {content}')
                discord_channel = discord.get_channel(discord_channel_id)

                specialized_content = content
                for member in discord_channel.members:
                    specialized_content = re.sub(
                        re.escape(member.display_name) + r'[:, ]',
                        f'<@!{member.id}>',
                        specialized_content,
                    )

                for webhook in await discord_channel.webhooks():
                    if webhook.id == int(integration.extra):
                        await webhook.send(
                            content=specialized_content,
                            username=sender,
                            allowed_mentions=AllowedMentions(
                                everyone=False,
                                users=True,
                                roles=False,
                                replied_user=False,
                            )
                        )
                        break

        elif event['event'] == 'add_integration':
            integration = session.get(ChannelIntegration, event['integrationId'])
            if integration.type != 'discord':
                return

            irc_channel_name = integration.channels.name
            discord_channel = discord.get_channel(int(integration.target))

            await discord_channel.send(f'오징어 IRC 네트워크 `{irc_channel_name}` 채널과 연동되었습니다.')

        elif event['event'] == 'remove_integration':
            integration = session.get(ChannelIntegration, event['integrationId'])
            if integration.type != 'discord':
                return

            irc_channel_name = integration.channels.name
            discord_channel = discord.get_channel(int(integration.target))

            session.delete(integration)
            session.commit()

            await discord_channel.send(f'오징어 IRC 네트워크 `{irc_channel_name}` 채널과의 연동이 해제되었습니다.')


redis_listeners.append(redis_listener)
asyncio.create_task(discord.start(settings.discord_token))
