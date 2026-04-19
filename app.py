import os
import json
import hashlib
import hmac
import base64
from flask import Flask, request, abort
from groq import Groq
import requests

app = Flask(__name__)

groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY'))

EMI_CHANNEL_SECRET = os.environ.get('EMI_CHANNEL_SECRET')
EMI_ACCESS_TOKEN = os.environ.get('EMI_CHANNEL_ACCESS_TOKEN')
ANDY_CHANNEL_SECRET = os.environ.get('ANDY_CHANNEL_SECRET')
ANDY_ACCESS_TOKEN = os.environ.get('ANDY_CHANNEL_ACCESS_TOKEN')

EMI_PROMPT = """あなたはEmily（Emi）、株式会社mosaiqueのCOOです。

【役割】
- CEO Skylerの参謀として全社オペレーションを統括する
- SkylerとSkylerの妹をサポートする

【性格・コミュニケーションスタイル】
- 本音でズバッと意見を言う。忖度しない
- ブレない。Skylerが迷っていても流されない
- SkylerはADHDのため、論点を絞り、シンプルかつ明確に伝える
- 必要なら反論する。YESマンにならない

【報告スタイル】
- 結論から先に言う
- 箇条書きで簡潔に
- 優先順位を明示する"""

ANDY_PROMPT = """あなたはAndy、株式会社mosaiqueの宿泊業・民泊業ヘッドです。

【役割】
- 民泊・旅館事業の専任責任者
- SkylerとSkylerの妹をサポートする
- 現在1軒目の旅館申請をフォロー中

【性格・コミュニケーションスタイル】
- かわいくてキュートなトーン
- おもてなし精神旺盛
- 気がきいて、やさしい
- 頑張り屋さんで前向き

【報告スタイル】
- 温かみのある言葉で報告
- 細かいことも気にかけて報告する"""


def verify_signature(body_bytes, signature, channel_secret):
    hash_val = hmac.new(channel_secret.encode('utf-8'), body_bytes, hashlib.sha256).digest()
    return base64.b64encode(hash_val).decode('utf-8') == signature


def reply_line(reply_token, message, access_token):
    r = requests.post(
        'https://api.line.me/v2/bot/message/reply',
        headers={
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        },
        json={
            'replyToken': reply_token,
            'messages': [{'type': 'text', 'text': message}]
        }
    )
    app.logger.info(f'LINE reply status: {r.status_code} {r.text}')


def handle_webhook(body_bytes, signature, channel_secret, access_token, system_prompt):
    if not verify_signature(body_bytes, signature, channel_secret):
        abort(400)

    body = json.loads(body_bytes.decode('utf-8'))
    for event in body.get('events', []):
        if event['type'] == 'message' and event['message']['type'] == 'text':
            user_message = event['message']['text']
            chat = groq_client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_message}
                ]
            )
            reply_line(event['replyToken'], chat.choices[0].message.content, access_token)

    return 'OK'


@app.route('/emi/callback', methods=['POST'])
def emi_callback():
    return handle_webhook(
        request.get_data(),
        request.headers.get('X-Line-Signature', ''),
        EMI_CHANNEL_SECRET,
        EMI_ACCESS_TOKEN,
        EMI_PROMPT
    )


@app.route('/andy/callback', methods=['POST'])
def andy_callback():
    return handle_webhook(
        request.get_data(),
        request.headers.get('X-Line-Signature', ''),
        ANDY_CHANNEL_SECRET,
        ANDY_ACCESS_TOKEN,
        ANDY_PROMPT
    )


@app.route('/')
def health():
    return 'mosaique LINE bot is running!'


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
