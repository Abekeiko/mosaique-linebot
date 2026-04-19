import os
import json
import hashlib
import hmac
import base64
from flask import Flask, request, abort
from groq import Groq
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import re

app = Flask(__name__)

groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY'))

EMI_CHANNEL_SECRET = os.environ.get('EMI_CHANNEL_SECRET')
EMI_ACCESS_TOKEN = os.environ.get('EMI_CHANNEL_ACCESS_TOKEN')
ANDY_CHANNEL_SECRET = os.environ.get('ANDY_CHANNEL_SECRET')
ANDY_ACCESS_TOKEN = os.environ.get('ANDY_CHANNEL_ACCESS_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')

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
- 優先順位を明示する

【タスク・スケジュール管理】
- ユーザーがタスクを伝えたら「タスク登録しました」と確認する
- リマインダーを依頼されたら日時を確認して「登録しました」と伝える
- タスク一覧を聞かれたら登録済みのタスクを報告する"""

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
- 細かいことも気にかけて報告する

【タスク・スケジュール管理】
- ユーザーがタスクを伝えたら「タスク登録しました」と確認する
- リマインダーを依頼されたら日時を確認して「登録しました」と伝える
- タスク一覧を聞かれたら登録済みのタスクを報告する"""


def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    agent TEXT,
                    role TEXT,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    agent TEXT,
                    content TEXT,
                    task_type TEXT DEFAULT 'task',
                    remind_at TIMESTAMP,
                    done BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
        conn.commit()


def get_history(user_id, agent, limit=10):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT role, content FROM conversations WHERE user_id=%s AND agent=%s ORDER BY created_at DESC LIMIT %s',
                (user_id, agent, limit)
            )
            rows = cur.fetchall()
    return list(reversed(rows))


def save_message(user_id, agent, role, content):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO conversations (user_id, agent, role, content) VALUES (%s, %s, %s, %s)',
                (user_id, agent, role, content)
            )
        conn.commit()


def save_task(user_id, agent, content, task_type='task', remind_at=None):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO tasks (user_id, agent, content, task_type, remind_at) VALUES (%s, %s, %s, %s, %s)',
                (user_id, agent, content, task_type, remind_at)
            )
        conn.commit()


def get_tasks(user_id, agent, task_type=None):
    with get_db() as conn:
        with conn.cursor() as cur:
            if task_type:
                cur.execute(
                    'SELECT content, created_at FROM tasks WHERE user_id=%s AND agent=%s AND task_type=%s AND done=FALSE ORDER BY created_at',
                    (user_id, agent, task_type)
                )
            else:
                cur.execute(
                    'SELECT content, task_type, created_at FROM tasks WHERE user_id=%s AND agent=%s AND done=FALSE ORDER BY created_at',
                    (user_id, agent)
                )
            return cur.fetchall()


def get_pending_reminders():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tasks WHERE done=FALSE AND remind_at IS NOT NULL AND remind_at <= NOW()",
            )
            rows = cur.fetchall()
    return rows


def mark_task_done(task_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE tasks SET done=TRUE WHERE id=%s', (task_id,))
        conn.commit()


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
    print(f'LINE reply status: {r.status_code} {r.text}')


def push_line(user_id, message, access_token):
    requests.post(
        'https://api.line.me/v2/bot/message/push',
        headers={
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        },
        json={
            'to': user_id,
            'messages': [{'type': 'text', 'text': message}]
        }
    )


def parse_remind_at(text):
    now = datetime.now()
    patterns = [
        (r'(\d+)時間後', lambda m: now.replace(hour=now.hour + int(m.group(1)), minute=0, second=0)),
        (r'明日(\d+)時', lambda m: now.replace(day=now.day + 1, hour=int(m.group(1)), minute=0, second=0)),
        (r'今日(\d+)時', lambda m: now.replace(hour=int(m.group(1)), minute=0, second=0)),
    ]
    for pattern, handler in patterns:
        m = re.search(pattern, text)
        if m:
            try:
                return handler(m)
            except Exception:
                pass
    return None


def handle_webhook(body_bytes, signature, channel_secret, access_token, system_prompt, agent_name):
    if not verify_signature(body_bytes, signature, channel_secret):
        abort(400)

    body = json.loads(body_bytes.decode('utf-8'))
    print(f'Received body: {body}')

    for event in body.get('events', []):
        if event['type'] == 'message' and event['message']['type'] == 'text':
            user_id = event['source']['userId']
            user_message = event['message']['text']

            # タスク・買い物リスト保存
            if any(kw in user_message for kw in ['買い物', '買う', '購入']):
                remind_at = parse_remind_at(user_message)
                save_task(user_id, agent_name, user_message, 'shopping', remind_at)
            elif any(kw in user_message for kw in ['タスク', 'やること', 'TODO', 'todo']):
                remind_at = parse_remind_at(user_message)
                save_task(user_id, agent_name, user_message, 'task', remind_at)

            # リスト照会
            extra_context = ''
            if any(kw in user_message for kw in ['やることリスト', 'タスクリスト', 'タスク一覧']):
                tasks = get_tasks(user_id, agent_name, 'task')
                if tasks:
                    items = '\n'.join([f'・{t["content"]}' for t in tasks])
                    extra_context = f'\n\n【登録済みタスク】\n{items}'
                else:
                    extra_context = '\n\n【登録済みタスク】なし'
            elif any(kw in user_message for kw in ['買い物リスト', '買うもの一覧']):
                tasks = get_tasks(user_id, agent_name, 'shopping')
                if tasks:
                    items = '\n'.join([f'・{t["content"]}' for t in tasks])
                    extra_context = f'\n\n【買い物リスト】\n{items}'
                else:
                    extra_context = '\n\n【買い物リスト】なし'

            # 会話履歴取得
            history = get_history(user_id, agent_name)
            messages = [{'role': 'system', 'content': system_prompt + extra_context}]
            for h in history:
                messages.append({'role': h['role'], 'content': h['content']})
            messages.append({'role': 'user', 'content': user_message})

            # AI返答
            chat = groq_client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                messages=messages
            )
            reply = chat.choices[0].message.content

            # 履歴保存
            save_message(user_id, agent_name, 'user', user_message)
            save_message(user_id, agent_name, 'assistant', reply)

            reply_line(event['replyToken'], reply, access_token)

    return 'OK'


@app.route('/emi/callback', methods=['POST'])
def emi_callback():
    return handle_webhook(
        request.get_data(),
        request.headers.get('X-Line-Signature', ''),
        EMI_CHANNEL_SECRET,
        EMI_ACCESS_TOKEN,
        EMI_PROMPT,
        'emi'
    )


@app.route('/andy/callback', methods=['POST'])
def andy_callback():
    return handle_webhook(
        request.get_data(),
        request.headers.get('X-Line-Signature', ''),
        ANDY_CHANNEL_SECRET,
        ANDY_ACCESS_TOKEN,
        ANDY_PROMPT,
        'andy'
    )


@app.route('/remind', methods=['GET'])
def remind():
    tasks = get_pending_reminders()
    for task in tasks:
        agent = task['agent']
        access_token = EMI_ACCESS_TOKEN if agent == 'emi' else ANDY_ACCESS_TOKEN
        push_line(task['user_id'], f'リマインダー: {task["content"]}', access_token)
        mark_task_done(task['id'])
    return f'done: {len(tasks)} reminders sent'


@app.route('/')
def health():
    return 'mosaique LINE bot is running!'


with app.app_context():
    init_db()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
