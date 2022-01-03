import asyncio
import json
import re
from datetime import datetime
from hashlib import md5

import requests
from fastapi import APIRouter, HTTPException, Request, Query, Path
from fastapi.responses import StreamingResponse
from slack_sdk.errors import SlackApiError
from slack_sdk.oauth import AuthorizeUrlGenerator
from fastapi.datastructures import FormData
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.webhook.async_client import AsyncWebhookClient
from slack_sdk.signature import SignatureVerifier
from fastapi.responses import RedirectResponse

from app.conf import settings
from app.db import Session, Channel, ChannelIntegration, SlackInstallation, Snippet
from app.redis import redis, redis_listeners
from app.util import sanitize_nickname

router = APIRouter()
signature_verifier = SignatureVerifier(settings.slack_signing_secret)
authorize_url_generator = AuthorizeUrlGenerator(
    client_id=settings.slack_client_id,
    scopes=[
        'commands',
        'users:read',
        'files:read',
        'channels:read',
        'channels:history',
        'groups:read',
        'groups:history',
        'chat:write',
        'chat:write.customize',
    ]
)


@router.get('/install')
async def install_app():
    return RedirectResponse(authorize_url_generator.generate(''))


@router.get('/oauth')
async def oauth_callback(code: str = Query(...)):
    client = AsyncWebClient()
    oauth_response = await client.oauth_v2_access(
        client_id=settings.slack_client_id,
        client_secret=settings.slack_client_secret,
        code=code,
    )
    with Session() as session:
        session.add(SlackInstallation(
            team_id=oauth_response['team']['id'],
            bot_user_id=oauth_response['bot_user_id'],
            access_token=oauth_response['access_token'],
        ))
        session.commit()
    return {'code': 'success'}


@router.post('/events')
async def receive_events(request: Request):
    if not await verify_request(request):
        raise HTTPException(status_code=401)

    outer_event = await request.json()
    outer_event_type = outer_event['type']
    if outer_event_type == 'url_verification':
        return {'challenge': outer_event['challenge']}
    if outer_event_type == 'event_callback':
        asyncio.create_task(handle_events(request, outer_event['event']))
        return {}


@router.post('/command')
async def receive_command(request: Request):
    if not await verify_request(request):
        raise HTTPException(status_code=401)

    data = await request.form()

    slack_channel_id = data['channel_id']
    if not slack_channel_id.startswith('C') and not slack_channel_id.startswith('G'):
        return {
            'response_type': 'ephemeral',
            'text': '채널에서만 실행할 수 있는 명령입니다.',
        }

    asyncio.create_task(handle_command(data))

    return {
        'response_type': 'in_channel',
    }


@router.get('/file/{team_id}/{file_id}')
async def file_proxy(team_id: str = Path(...), file_id: str = Path(...)):
    with Session() as session:
        installation = session.query(SlackInstallation).filter(SlackInstallation.team_id == team_id).one()
    slack = AsyncWebClient(installation.access_token)

    file = await slack.files_info(file=file_id)

    def iter_file():
        resp = requests.get(
            url=file['file']['url_private'],
            headers={'Authorization': 'Bearer ' + installation.access_token},
            stream=True,
        )
        for chunk in resp.iter_content(chunk_size=1024):
            yield chunk

    return StreamingResponse(iter_file(), media_type=file['file']['mimetype'])


async def verify_request(request: Request):
    return signature_verifier.is_valid(
        body=await request.body(),
        timestamp=request.headers.get("X-Slack-Request-Timestamp"),
        signature=request.headers.get("X-Slack-Signature"),
    )


async def handle_events(request: Request, event: dict):
    event_type = event['type']
    if event_type == 'message':
        subtype = event.get('subtype')
        if subtype is not None and subtype != 'file_share':
            return

        if event['text'].startswith('/'):
            return

        with Session() as session:

            slack_channel_id = event['channel']

            integration = session.query(ChannelIntegration).filter(
                ChannelIntegration.type == 'slack',
                ChannelIntegration.target == slack_channel_id,
                ChannelIntegration.is_authorized == True,
            ).first()
            if not integration:
                return

            slack_team_id = integration.extra

            installation = session.query(SlackInstallation).filter(SlackInstallation.team_id == slack_team_id).first()
            slack = AsyncWebClient(installation.access_token)

            content = event['text']

            print(f'Route message from Slack[{slack_channel_id}] to IRC[{integration.channels.name}]: {content}')

            for mention in re.findall(r'<@([UW].+?)>', content):
                user = await slack.users_info(user=mention)
                content = content.replace(f'<@{mention}>', '@' + user['user']['profile']['display_name'])

            for full, code in re.findall(r'(```(.+?)```)', content, re.DOTALL):
                code = '\n'.join(map(lambda x: f'`{x}`', code.strip().splitlines()))
                content = content.replace(full, code)

            content = re.sub(r'<(.+?)(\|.+)?>', r'\1', content)
            content = content.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')

            if subtype == 'file_share':
                for file in event['files']:
                    content += f'\nhttps://api.ozinger.org/integration/slack/file/{slack_team_id}/{file["id"]}'

            lines = content.splitlines()
            if len(lines) > 5:
                snippet = Snippet(content=content)
                session.add(snippet)
                session.commit()

                lines = [f'https://api.ozinger.org/snippets/{snippet.id}']

            sender = sanitize_nickname((await slack.users_info(user=event['user']))['user']['profile']['display_name'])

            for line in lines:
                if not line:
                    continue

                await redis.publish('to-ika', json.dumps({
                    'event': 'chat_message',
                    'sender': f'{sender}＠s!integration@integrations/{integration.type}/{integration.id}',
                    'recipient': integration.channels.name,
                    'message': line,
                }))


async def handle_command(command: FormData):
    with Session() as session:
        responder = AsyncWebhookClient(command['response_url'])

        installation = session.query(SlackInstallation).filter(SlackInstallation.team_id == command['team_id']).first()
        slack = AsyncWebClient(installation.access_token)

        try:
            members = await slack.conversations_members(channel=command['channel_id'])
        except SlackApiError:
            return await responder.send(text="앱을 채널에 먼저 설치해주세요.")
        else:
            if installation.bot_user_id not in members['members']:
                return await responder.send(text="앱을 채널에 먼저 설치해주세요.")

        params = command['text'].strip().split(' ')
        subcommand = params[0]

        slack_team_id = command['team_id']
        slack_channel_id = command['channel_id']

        if subcommand == 'attach':
            if len(params) != 2:
                return await responder.send(text='채널명을 입력해주세요.')

            irc_channel_name = command['text'].split(' ')[1]

            if session.query(ChannelIntegration).filter(ChannelIntegration.type == 'slack',
                                                        ChannelIntegration.target == slack_channel_id).first():
                return await responder.send(text=f'이 채널에는 이미 연동이 등록되어 있습니다.')

            irc_channel = session.query(Channel).filter(Channel.name == irc_channel_name).first()
            if not irc_channel:
                return await responder.send(text=f'오징어 IRC 네트워크에 `{irc_channel_name}` 채널이 등록되어 있지 않습니다.')

            integration = ChannelIntegration(
                channel=irc_channel.id,
                type='slack',
                target=slack_channel_id,
                extra=slack_team_id,
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

            return await responder.send(
                response_type='in_channel',
                text=f'오징어 IRC 네트워크의 `{irc_channel_name}` 채널에 연동을 요청했습니다.',
            )

        elif subcommand == 'detach':
            integration = session.query(ChannelIntegration).filter(
                ChannelIntegration.type == 'slack',
                ChannelIntegration.target == slack_channel_id,
            ).first()
            if not integration:
                return await responder.send(text='이 채널은 오징어 IRC 네트워크 채널과 연동되어 있지 않습니다.')

            integration_id = integration.id
            irc_channel_name = integration.channels.name

            session.delete(integration)
            session.commit()

            await redis.publish('to-ika', json.dumps({
                'event': 'remove_integration',
                'irc_channel': irc_channel_name,
                'integrationId': integration_id,
            }))

            return await responder.send(text=f'오징어 IRC 네트워크 `{irc_channel_name}` 채널과의 연동이 해제되었습니다.')
        else:
            return await responder.send(text='사용법: `/ozinger attach #irc_channel_name`, `/ozinger detach`')


async def redis_listener(event: dict):
    with Session() as session:
        if event['event'] == 'chat_message':
            sender = event['sender'].split('!')[0]
            sender_parts = sender.split('＠')
            if len(sender_parts) == 2 and sender_parts[1] == 's':
                return

            sender_color = md5(sender.encode()).hexdigest()[:6]
            sender_avatar_url = f'https://ui-avatars.com/api/?name={sender}&background={sender_color}'

            irc_channel = session.query(Channel).filter(Channel.name == event['recipient']).first()
            if not irc_channel:
                return

            integrations = session.query(ChannelIntegration).filter(
                ChannelIntegration.type == 'slack',
                ChannelIntegration.channel == irc_channel.id,
                ChannelIntegration.is_authorized == True,
            ).all()
            if not integrations:
                return

            content = event['message']
            content = content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

            for integration in integrations:
                slack_channel_id = integration.target
                slack_team_id = integration.extra

                installation = session.query(SlackInstallation).filter(SlackInstallation.team_id == slack_team_id).first()

                slack = AsyncWebClient(installation.access_token)

                print(f'Route message from IRC[{irc_channel.name}] to Slack[{slack_channel_id}]: {content}')

                specialized_content = content
                slack_team_members = await slack.users_list()
                for member in slack_team_members['members']:
                    if member['profile']['display_name'].strip():
                        specialized_content = re.sub(
                            r'(^| )' + re.escape(member['profile']['display_name']) + r'[:, ]',
                            f'<@{member["id"]}>',
                            specialized_content,
                        )

                while True:
                    response = await slack.chat_postMessage(
                        channel=slack_channel_id,
                        username=sender,
                        icon_url=sender_avatar_url,
                        text=specialized_content,
                        mrkdwn=False,
                    )

                    if response['ok']:
                        break

        elif event['event'] == 'add_integration':
            integration = session.get(ChannelIntegration, event['integrationId'])
            if integration.type != 'slack':
                return

            slack_channel_id = integration.target
            slack_team_id = integration.extra

            installation = session.query(SlackInstallation).filter(SlackInstallation.team_id == slack_team_id).one()
            slack = AsyncWebClient(installation.access_token)

            await slack.chat_postMessage(
                channel=slack_channel_id,
                text=f'오징어 IRC 네트워크 `{integration.channels.name}` 채널과 연동되었습니다.',
            )

        elif event['event'] == 'remove_integration':
            integration = session.get(ChannelIntegration, event['integrationId'])
            if integration.type != 'slack':
                return

            slack_channel_id = integration.target
            slack_team_id = integration.extra

            installation = session.query(SlackInstallation).filter(SlackInstallation.team_id == slack_team_id).one()
            slack = AsyncWebClient(installation.access_token)

            irc_channel_name = integration.channels.name

            session.delete(integration)
            session.commit()

            await slack.chat_postMessage(
                channel=slack_channel_id,
                text=f'오징어 IRC 네트워크 `{irc_channel_name}` 채널과의 연동이 해제되었습니다.',
            )


redis_listeners.append(redis_listener)
