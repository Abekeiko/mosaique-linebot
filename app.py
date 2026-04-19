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
from datetime import datetime, timezone, timedelta
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY'))

CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID', 'abekeiko0813@gmail.com')

def get_calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        '/etc/secrets/google-credentials.json',
        scopes=['https://www.googleapis.com/auth/calendar']
    )
    return build('calendar', 'v3', credentials=creds)


def delete_calendar_event(title_keyword):
    try:
        service = get_calendar_service()
        jst = timezone(timedelta(hours=9))
        now = datetime.now(jst)
        events = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=now.isoformat(),
            maxResults=20,
            singleEvents=True,
            orderBy='startTime',
            q=title_keyword
        ).execute().get('items', [])
        if not events:
            return False, '該当する予定が見つかりませんでした。'
        event = events[0]
        service.events().delete(calendarId=CALENDAR_ID, eventId=event['id']).execute()
        return True, event.get('summary', '')
    except Exception as e:
        print(f'Calendar delete error: {e}')
        return False, str(e)


def create_calendar_event(summary, start_datetime, end_datetime=None):
    try:
        service = get_calendar_service()
        if end_datetime is None:
            end_datetime = start_datetime.replace(hour=start_datetime.hour + 1)
        event = {
            'summary': summary,
            'start': {'dateTime': start_datetime.isoformat(), 'timeZone': 'Asia/Tokyo'},
            'end': {'dateTime': end_datetime.isoformat(), 'timeZone': 'Asia/Tokyo'},
        }
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        return True
    except Exception as e:
        print(f'Calendar create error: {e}')
        return False


def parse_event_datetime(text):
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    flat = text.replace('\n', ' ')

    def make_dt(base, hour, minute=0):
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    time_m = re.search(r'(\d{1,2})[：:時](\d{2})?', flat)
    hour = int(time_m.group(1)) if time_m else None
    minute = int(time_m.group(2)) if time_m and time_m.group(2) else 0

    if hour is None:
        return None

    if '明日' in flat:
        base = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return make_dt(base, hour, minute)
    elif '今日' in flat:
        return make_dt(now, hour, minute)
    else:
        m = re.search(r'(\d+)月(\d+)日', flat)
        if m:
            return now.replace(month=int(m.group(1)), day=int(m.group(2)), hour=hour, minute=minute, second=0, microsecond=0)
    return None

def get_today_events():
    try:
        service = get_calendar_service()
        jst = timezone(timedelta(hours=9))
        now = datetime.now(jst)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        end = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
        events = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start,
            timeMax=end,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        return events.get('items', [])
    except Exception as e:
        print(f'Calendar error: {e}')
        return []

EMI_CHANNEL_SECRET = os.environ.get('EMI_CHANNEL_SECRET')
EMI_ACCESS_TOKEN = os.environ.get('EMI_CHANNEL_ACCESS_TOKEN')
ANDY_CHANNEL_SECRET = os.environ.get('ANDY_CHANNEL_SECRET')
ANDY_ACCESS_TOKEN = os.environ.get('ANDY_CHANNEL_ACCESS_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')

EMI_PROMPT = """あなたはEmily（Emi）、株式会社mosaiqueのCOOであり、Skylerのスーパー秘書です。

【役割】
- CEO Skylerの参謀として全社を統括する
- 以下すべての領域を管理・サポートする：
  - マーケティング・集客
  - 財務・資金管理
  - 実務・オペレーション
  - スケジュール・タスク管理
  - Skylerのプライベートタスク・個人的な用事
- SkylerとSkylerの妹をサポートする

【性格・コミュニケーションスタイル】
- 本音でズバッと意見を言う。忖度しない
- ブレない。Skylerが迷っていても流されない
- SkylerはADHDのため、論点を絞り、シンプルかつ明確に伝える
- 必要なら反論する。YESマンにならない
- 優先順位を常に意識して動く

【報告スタイル】
- 結論から先に言う
- 箇条書きで簡潔に
- 優先順位を明示する

【タスク・スケジュール管理】
- ユーザーがタスクを伝えたら「登録しました」と確認する
- リマインダーを依頼されたら日時を確認して「登録しました」と伝える
- タスク一覧を聞かれたら登録済みのタスクを優先順位付きで報告する
- プライベートの用事も仕事と同様に管理する"""

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
        # 既存テーブルにtask_type列がなければ追加
        with conn.cursor() as cur:
            cur.execute('''
                ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_type TEXT DEFAULT 'task'
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

            # AIで意図を判定
            jst = timezone(timedelta(hours=9))
            today_str = datetime.now(jst).strftime('%Y-%m-%d')
            intent_response = groq_client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                messages=[
                    {'role': 'system', 'content': f'''今日の日付は{today_str}（JST）です。
ユーザーのメッセージを分析して、以下のJSONのみを返してください。他の文章は不要です。

{{
  "intent": "calendar_create" | "calendar_read" | "task_save" | "shopping_save" | "task_list" | "shopping_list" | "chat",
  "title": "予定タイトル（calendar_createのみ）",
  "datetime": "YYYY-MM-DDTHH:MM:00+09:00（calendar_createのみ）"
}}

- calendar_create: カレンダーに予定を追加したい
- calendar_delete: カレンダーの予定を削除・キャンセルしたい（"keyword"フィールドに検索キーワードを入れる）
- calendar_read: 予定を確認したい
- task_save: タスクを登録したい
- shopping_save: 買い物リストに追加したい
- task_list: タスク一覧を見たい
- shopping_list: 買い物リストを見たい
- chat: それ以外

calendar_deleteの場合は "keyword" フィールドも返すこと:
{{"intent": "calendar_delete", "keyword": "削除する予定のキーワード"}}'''},
                    {'role': 'user', 'content': user_message}
                ],
                temperature=0
            )

            extra_context = ''
            try:
                intent_text = intent_response.choices[0].message.content.strip()
                intent_text = re.search(r'\{.*\}', intent_text, re.DOTALL).group()
                intent_data = json.loads(intent_text)
                intent = intent_data.get('intent', 'chat')
            except Exception as e:
                print(f'Intent parse error: {e}')
                intent = 'chat'

            if intent == 'calendar_create':
                title = intent_data.get('title', '')
                dt_str = intent_data.get('datetime', '')
                try:
                    dt = datetime.fromisoformat(dt_str)
                    success = create_calendar_event(title, dt)
                    extra_context = f'\n\n【カレンダー登録結果】{"成功。「" + title + "」を" + dt.strftime("%m月%d日 %H:%M") + "に登録しました。" if success else "登録失敗。"}'
                except Exception as e:
                    print(f'Calendar create error: {e}')
                    extra_context = '\n\n【カレンダー登録結果】日時の解析に失敗しました。'

            elif intent == 'calendar_delete':
                keyword = intent_data.get('keyword', '')
                success, msg = delete_calendar_event(keyword)
                extra_context = f'\n\n【カレンダー削除結果】{"「" + msg + "」を削除しました。" if success else msg}'

            elif intent == 'calendar_read':
                events = get_today_events()
                if events:
                    event_lines = []
                    for e in events:
                        start = e.get('start', {}).get('dateTime', e.get('start', {}).get('date', ''))
                        if 'T' in start:
                            start = datetime.fromisoformat(start).strftime('%H:%M')
                        event_lines.append(f'・{start} {e.get("summary", "")}')
                    extra_context = '\n\n【今日のGoogleカレンダー】\n' + '\n'.join(event_lines)
                else:
                    extra_context = '\n\n【今日のGoogleカレンダー】今日は予定が登録されていません。架空の予定は絶対に作らないこと。'

            elif intent == 'task_save':
                save_task(user_id, agent_name, user_message, 'task')
                extra_context = '\n\n【タスク登録】完了しました。'

            elif intent == 'shopping_save':
                save_task(user_id, agent_name, user_message, 'shopping')
                extra_context = '\n\n【買い物リスト登録】完了しました。'

            elif intent == 'task_list':
                tasks = get_tasks(user_id, agent_name, 'task')
                items = '\n'.join([f'・{t["content"]}' for t in tasks]) if tasks else 'なし'
                extra_context = f'\n\n【登録済みタスク】\n{items}'

            elif intent == 'shopping_list':
                tasks = get_tasks(user_id, agent_name, 'shopping')
                items = '\n'.join([f'・{t["content"]}' for t in tasks]) if tasks else 'なし'
                extra_context = f'\n\n【買い物リスト】\n{items}'

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


def get_all_user_ids(agent):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT DISTINCT user_id FROM conversations WHERE agent=%s',
                (agent,)
            )
            return [r['user_id'] for r in cur.fetchall()]


@app.route('/remind', methods=['GET'])
def remind():
    tasks = get_pending_reminders()
    for task in tasks:
        agent = task['agent']
        access_token = EMI_ACCESS_TOKEN if agent == 'emi' else ANDY_ACCESS_TOKEN
        push_line(task['user_id'], f'リマインダー: {task["content"]}', access_token)
        mark_task_done(task['id'])
    return f'done: {len(tasks)} reminders sent'


@app.route('/morning-brief', methods=['GET'])
def morning_brief():
    user_ids = get_all_user_ids('emi')
    for user_id in user_ids:
        tasks = get_tasks(user_id, 'emi')
        task_text = '\n'.join([f'・{t["content"]}' for t in tasks]) if tasks else 'なし'

        events = get_today_events()
        if events:
            event_lines = []
            for e in events:
                start = e.get('start', {}).get('dateTime', e.get('start', {}).get('date', ''))
                if 'T' in start:
                    start = datetime.fromisoformat(start).strftime('%H:%M')
                event_lines.append(f'・{start} {e.get("summary", "")}')
            calendar_text = '\n'.join(event_lines)
        else:
            calendar_text = 'なし'

        chat = groq_client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[
                {'role': 'system', 'content': EMI_PROMPT},
                {'role': 'user', 'content': f'おはようございます。今日の朝のブリーフィングをしてください。\n\n【今日のカレンダー】\n{calendar_text}\n\n【未完了タスク】\n{task_text}\n\nSkylerに向けて、今日やるべきことの優先順位と一言アドバイスを送ってください。'}
            ]
        )
        message = chat.choices[0].message.content
        push_line(user_id, message, EMI_ACCESS_TOKEN)

    return f'done: morning brief sent to {len(user_ids)} users'


@app.route('/')
def health():
    return 'mosaique LINE bot is running!'


with app.app_context():
    init_db()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
