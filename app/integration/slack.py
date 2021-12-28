import asyncio
import json
import re
from datetime import datetime

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
from app.db import Session, Channel, ChannelIntegration, SlackInstallation
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
    session = Session()
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
    channel_id = data['channel_id']
    if not channel_id.startswith('C') and not channel_id.startswith('G'):
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
    session = Session()
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

        session = Session()
        integration = session.query(ChannelIntegration).filter(
            ChannelIntegration.type == 'slack',
            ChannelIntegration.target.contains(event['channel']),
            ChannelIntegration.is_authorized == True,
        ).first()
        if not integration:
            return

        team_id = integration.target.split('/')[0]
        installation = session.query(SlackInstallation).filter(
            SlackInstallation.team_id == integration.target.split('/')[0]
        ).first()
        slack = AsyncWebClient(installation.access_token)
        sender = sanitize_nickname((await slack.users_info(user=event['user']))['user']['profile']['display_name'])

        message = event['text']

        print(f'Route message from Slack[{event["channel"]}] to IRC[{integration.channels.name}]: {message}')

        for mention in re.findall(r'<@([UW].+?)>', message):
            user = await slack.users_info(user=mention)
            message = message.replace(f'<@{mention}>', '@' + user['user']['profile']['display_name'])

        for multiline_code in re.findall('(```.+?```)', message, re.DOTALL):
            code = multiline_code.replace('\n', '`\n`')
            message = message.replace(multiline_code, code[2:-2])

        message = message.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')

        if subtype == 'file_share':
            for file in event['files']:
                message += f'\nhttps://{request.headers["Host"]}integration/slack/file/{team_id}/{file["id"]}'

        for line in message.splitlines():
            if not line:
                continue

            await redis.publish('to-ika', json.dumps({
                'event': 'chat_message',
                'sender': f'{sender}+!integration@integrations/{integration.type}/{integration.id}',
                'recipient': integration.channels.name,
                'message': line,
            }))


async def handle_command(command: FormData):
    responder = AsyncWebhookClient(command['response_url'])

    session = Session()
    installation = session.query(SlackInstallation).filter(SlackInstallation.team_id == command['team_id']).first()
    slack = AsyncWebClient(installation.access_token)
    try:
        members = await slack.conversations_members(channel=command['channel_id'])
    except SlackApiError:
        return await responder.send(text="앱을 채널에 먼저 설치해주세요.")
    if installation.bot_user_id not in members['members']:
        return await responder.send(text="앱을 채널에 먼저 설치해주세요.")

    params = command['text'].strip().split(' ')
    subcommand = params[0]
    if subcommand == 'attach':
        if len(params) != 2:
            return await responder.send(text='채널명을 입력해주세요.')

        target = command['text'].split(' ')[1]

        channel = session.query(Channel).filter(Channel.name == target).first()
        if not channel:
            return await responder.send(text=f'오징어 IRC 네트워크에 `{target}` 채널이 등록되어 있지 않습니다.')

        if session.query(ChannelIntegration).filter(ChannelIntegration.type == 'slack',
                                                    ChannelIntegration.channel == channel.id).first():
            return await responder.send(text=f'오징어 IRC 네트워크의 `{target}` 채널에는 이미 연동이 등록되어 있습니다.')

        if session.query(ChannelIntegration).filter(ChannelIntegration.type == 'slack',
                                                    ChannelIntegration.target == f'{command["team_id"]}/{command["channel_id"]}').first():
            return await responder.send(text=f'이 채널에는 이미 연동이 등록되어 있습니다.')

        integration = ChannelIntegration(
            channel=channel.id,
            type='slack',
            target=f'{command["team_id"]}/{command["channel_id"]}',
            is_authorized=False,
            created_at=datetime.now()
        )
        session.add(integration)
        session.commit()

        await redis.publish('to-ika', json.dumps({
            'event': 'add_integration',
            'channel': target,
            'integrationId': integration.id,
        }))

        return await responder.send(
            response_type='in_channel',
            text=f'오징어 IRC 네트워크의 `{target}` 채널에 연동을 요청했습니다.',
        )
    elif subcommand == 'detach':
        target = command['channel_id']

        integration = session.query(ChannelIntegration).filter(ChannelIntegration.target == target).first()
        if not integration:
            return await responder.send(text=f'오징어 IRC 네트워크 채널에 연동되어 있지 않습니다.')

        await redis.publish('to-ika', json.dumps({
            'event': 'remove_integration',
            'channel': integration.channels.name,
            'integrationId': integration.id,
        }))

        session.delete(integration)
        session.commit()
    else:
        return await responder.send(text='사용법: `/ozinger attach #irc_channel`, `/ozinger detach`')


async def redis_listener(event: dict):
    if event['event'] == 'chat_message':
        sender = event['sender'].split('!')[0]

        session = Session()

        channel = session.query(Channel).filter(Channel.name == event['recipient']).first()
        if not channel:
            return

        integration = session.query(ChannelIntegration).filter(
            ChannelIntegration.type == 'slack',
            ChannelIntegration.channel == channel.id,
            ChannelIntegration.is_authorized == True,
        ).first()
        if not integration:
            return

        team, slack_channel = integration.target.split('/')
        installation = session.query(SlackInstallation).filter(SlackInstallation.team_id == team).first()
        slack = AsyncWebClient(installation.access_token)

        message = event['message']

        print(f'Route message from IRC[{channel.name}] to Slack[{slack_channel}]: {message}')

        message = message.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        users = await slack.users_list()
        for member in users['members']:
            if member['profile']['display_name'].strip():
                message = re.sub(re.escape(member['profile']['display_name']) + '[:, ]', f'<@{member["id"]}>', message)

        while True:
            response = await slack.chat_postMessage(
                channel=slack_channel,
                username=sender,
                text=message,
                mrkdwn=False,
            )

            if response['ok']:
                break


redis_listeners.append(redis_listener)
