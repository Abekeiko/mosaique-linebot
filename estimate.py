"""建築見積システム（一般住宅リフォーム向け）

Web管理画面で単価マスタを管理し、見積書を作成・印刷（PDF保存）する。
/estimate/ 配下にマウントされる Flask Blueprint。
"""
import os
import csv
import io
import hmac
import hashlib
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN, InvalidOperation
from functools import wraps

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import (Blueprint, request, render_template, redirect, url_for,
                   abort, Response, flash)

DATABASE_URL = os.environ.get('DATABASE_URL')
ESTIMATE_USER = os.environ.get('ESTIMATE_USER', 'admin')
ESTIMATE_PASSWORD = os.environ.get('ESTIMATE_PASSWORD', '')

COMPANY = {
    'name': os.environ.get('COMPANY_NAME', '株式会社Kaleido'),
    'postal': os.environ.get('COMPANY_POSTAL', ''),
    'address': os.environ.get('COMPANY_ADDRESS', ''),
    'tel': os.environ.get('COMPANY_TEL', ''),
    'email': os.environ.get('COMPANY_EMAIL', ''),
    'reg_no': os.environ.get('COMPANY_INVOICE_REG_NO', ''),  # 適格請求書発行事業者登録番号
}

STATUSES = ['下書き', '提出済', '受注', '失注']

estimate_bp = Blueprint('estimate', __name__, url_prefix='/estimate')


@estimate_bp.record_once
def set_secret_key(state):
    # flash() 用。未設定なら ESTIMATE_PASSWORD から導出（gunicorn の全ワーカーで同じ値になる）
    if not state.app.secret_key:
        state.app.secret_key = os.environ.get('FLASK_SECRET_KEY') or hashlib.sha256(
            f'mosaique-estimate:{ESTIMATE_PASSWORD}'.encode()).hexdigest()


def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


# サンプル単価（目安）。運用開始時に自社単価へ置き換えること。
SAMPLE_ITEMS = [
    ('仮設・養生', '養生費', '床・壁・共用部', '式', 30000),
    ('仮設・養生', '現場管理費', '', '式', 50000),
    ('解体・撤去', '内装解体', '床・壁・天井', '㎡', 3500),
    ('解体・撤去', '既存設備撤去', 'キッチン・浴室等', '式', 40000),
    ('解体・撤去', '産業廃棄物処分費', '2t車', '台', 80000),
    ('内装', 'クロス張替え', '量産品', '㎡', 1200),
    ('内装', 'クロス張替え', '1000番台', '㎡', 1600),
    ('内装', '天井クロス張替え', '量産品', '㎡', 1400),
    ('内装', 'クッションフロア張替え', '1.8mm', '㎡', 3500),
    ('内装', 'フローリング張替え', '複合フローリング', '㎡', 9500),
    ('内装', 'フローリング重ね張り', '1.5mm', '㎡', 7500),
    ('内装', '畳表替え', '中級', '枚', 8000),
    ('内装', '畳→フローリング変更', '下地調整含む', '帖', 30000),
    ('内装', '襖張替え', '普及品', '枚', 4500),
    ('内装', '障子張替え', '', '枚', 3000),
    ('木工・建具', '石膏ボード張り', 't12.5', '㎡', 3800),
    ('木工・建具', '室内ドア交換', '既製品', 'ヶ所', 85000),
    ('木工・建具', '巾木交換', 'ソフト巾木', 'm', 800),
    ('水回り', 'システムキッチン交換', 'I型2550 普及グレード', '台', 650000),
    ('水回り', 'ユニットバス交換', '1616 普及グレード', '台', 900000),
    ('水回り', '洗面化粧台交換', 'W750', '台', 150000),
    ('水回り', 'トイレ交換', '温水洗浄便座付', '台', 180000),
    ('水回り', '給排水配管工事', '', '式', 80000),
    ('電気', 'コンセント増設', '', 'ヶ所', 15000),
    ('電気', '照明器具交換', 'LEDシーリング', 'ヶ所', 12000),
    ('電気', '換気扇交換', '', 'ヶ所', 35000),
    ('外装', '外壁塗装', 'シリコン', '㎡', 3500),
    ('外装', '屋根塗装', 'シリコン', '㎡', 3800),
    ('外装', '足場', '架払い・メッシュシート', '㎡', 900),
    ('外装', '高圧洗浄', '', '㎡', 200),
    ('外装', 'コーキング打替え', '', 'm', 1000),
]


def init_estimate_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS est_items (
                    id SERIAL PRIMARY KEY,
                    category TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    spec TEXT NOT NULL DEFAULT '',
                    unit TEXT NOT NULL DEFAULT '式',
                    unit_price BIGINT NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS estimates (
                    id SERIAL PRIMARY KEY,
                    estimate_no TEXT,
                    customer_name TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    site_address TEXT NOT NULL DEFAULT '',
                    issue_date DATE NOT NULL DEFAULT CURRENT_DATE,
                    valid_days INTEGER NOT NULL DEFAULT 30,
                    overhead_rate NUMERIC(5,2) NOT NULL DEFAULT 10,
                    discount BIGINT NOT NULL DEFAULT 0,
                    tax_rate NUMERIC(5,2) NOT NULL DEFAULT 10,
                    status TEXT NOT NULL DEFAULT '下書き',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS estimate_lines (
                    id SERIAL PRIMARY KEY,
                    estimate_id INTEGER NOT NULL REFERENCES estimates(id) ON DELETE CASCADE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    category TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    spec TEXT NOT NULL DEFAULT '',
                    quantity NUMERIC(12,2) NOT NULL DEFAULT 0,
                    unit TEXT NOT NULL DEFAULT '',
                    unit_price BIGINT NOT NULL DEFAULT 0
                )
            ''')
            cur.execute('SELECT COUNT(*) AS n FROM est_items')
            if cur.fetchone()['n'] == 0:
                for i, (cat, name, spec, unit, price) in enumerate(SAMPLE_ITEMS):
                    cur.execute(
                        'INSERT INTO est_items (category, name, spec, unit, unit_price, sort_order) '
                        'VALUES (%s, %s, %s, %s, %s, %s)',
                        (cat, name, spec, unit, price, i)
                    )
        conn.commit()


# ---------- 認証・CSRF ----------

def csrf_token():
    return hmac.new(ESTIMATE_PASSWORD.encode(), b'estimate-csrf', hashlib.sha256).hexdigest()


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not ESTIMATE_PASSWORD:
            return Response('ESTIMATE_PASSWORD が設定されていません。環境変数を設定してください。',
                            503, {'Content-Type': 'text/plain; charset=utf-8'})
        auth = request.authorization
        if not (auth and hmac.compare_digest(auth.username or '', ESTIMATE_USER)
                and hmac.compare_digest(auth.password or '', ESTIMATE_PASSWORD)):
            return Response('認証が必要です', 401,
                            {'WWW-Authenticate': 'Basic realm="Kaleido estimate"',
                             'Content-Type': 'text/plain; charset=utf-8'})
        if request.method == 'POST':
            if not hmac.compare_digest(request.form.get('csrf_token', ''), csrf_token()):
                abort(400)
        return f(*args, **kwargs)
    return wrapper


@estimate_bp.app_template_global()
def estimate_csrf_token():
    return csrf_token()


@estimate_bp.app_template_filter('yen')
def yen_filter(value):
    return f'{int(value):,}'


@estimate_bp.app_template_filter('qty')
def qty_filter(value):
    d = Decimal(value)
    return str(int(d)) if d == d.to_integral_value() else f'{d.normalize():f}'


# ---------- 計算 ----------

def to_decimal(value, default='0'):
    try:
        return Decimal(str(value).replace(',', '').strip() or default)
    except InvalidOperation:
        return Decimal(default)


def line_amount(line):
    return int((to_decimal(line['quantity']) * to_decimal(line['unit_price']))
               .quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def calc_totals(estimate, lines):
    subtotal = sum(line_amount(l) for l in lines)
    overhead = int((Decimal(subtotal) * to_decimal(estimate['overhead_rate']) / 100)
                   .quantize(Decimal('1'), rounding=ROUND_DOWN))
    discount = int(estimate['discount'] or 0)
    taxable = subtotal + overhead - discount
    tax = int((Decimal(taxable) * to_decimal(estimate['tax_rate']) / 100)
              .quantize(Decimal('1'), rounding=ROUND_DOWN))
    return {
        'subtotal': subtotal,
        'overhead': overhead,
        'discount': discount,
        'taxable': taxable,
        'tax': tax,
        'total': taxable + tax,
    }


def jsonable(rows):
    """テンプレートの tojson に渡せる形に変換（Decimal・日付を含む行を想定）。"""
    def conv(v):
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, (date,)):
            return v.isoformat()
        return v
    return [{k: conv(v) for k, v in dict(r).items()} for r in rows]


def group_lines(lines):
    """工種（category）ごとにまとめ、小計を付ける。"""
    groups = []
    for l in lines:
        l = dict(l, amount=line_amount(l))
        if groups and groups[-1]['category'] == l['category']:
            groups[-1]['lines'].append(l)
        else:
            groups.append({'category': l['category'], 'lines': [l]})
    for g in groups:
        g['subtotal'] = sum(l['amount'] for l in g['lines'])
    return groups


# ---------- データアクセス ----------

def fetch_items():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM est_items ORDER BY sort_order, category, id')
            return cur.fetchall()


def fetch_estimate(estimate_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM estimates WHERE id=%s', (estimate_id,))
            estimate = cur.fetchone()
            if not estimate:
                abort(404)
            cur.execute('SELECT * FROM estimate_lines WHERE estimate_id=%s ORDER BY sort_order, id',
                        (estimate_id,))
            lines = cur.fetchall()
    return estimate, lines


def parse_estimate_form(form):
    try:
        issue_date = date.fromisoformat(form.get('issue_date', ''))
    except ValueError:
        issue_date = date.today()
    status = form.get('status', '下書き')
    estimate = {
        'customer_name': form.get('customer_name', '').strip(),
        'title': form.get('title', '').strip(),
        'site_address': form.get('site_address', '').strip(),
        'issue_date': issue_date,
        'valid_days': int(to_decimal(form.get('valid_days'), '30')),
        'overhead_rate': to_decimal(form.get('overhead_rate'), '0'),
        'discount': int(to_decimal(form.get('discount'), '0')),
        'tax_rate': to_decimal(form.get('tax_rate'), '10'),
        'status': status if status in STATUSES else '下書き',
        'notes': form.get('notes', '').strip(),
    }
    lines = []
    fields = ['category', 'name', 'spec', 'quantity', 'unit', 'unit_price']
    columns = {f: form.getlist(f'line_{f}') for f in fields}
    for i in range(len(columns['name'])):
        row = {f: (columns[f][i] if i < len(columns[f]) else '').strip() for f in fields}
        if not row['name']:
            continue
        row['quantity'] = to_decimal(row['quantity'])
        row['unit_price'] = int(to_decimal(row['unit_price']))
        row['sort_order'] = len(lines)
        lines.append(row)
    return estimate, lines


def save_lines(cur, estimate_id, lines):
    cur.execute('DELETE FROM estimate_lines WHERE estimate_id=%s', (estimate_id,))
    for l in lines:
        cur.execute(
            'INSERT INTO estimate_lines (estimate_id, sort_order, category, name, spec, quantity, unit, unit_price) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
            (estimate_id, l['sort_order'], l['category'], l['name'], l['spec'],
             l['quantity'], l['unit'], l['unit_price'])
        )


ESTIMATE_COLUMNS = ['customer_name', 'title', 'site_address', 'issue_date', 'valid_days',
                    'overhead_rate', 'discount', 'tax_rate', 'status', 'notes']


def insert_estimate(cur, estimate, lines):
    cols = ', '.join(ESTIMATE_COLUMNS)
    placeholders = ', '.join(['%s'] * len(ESTIMATE_COLUMNS))
    cur.execute(f'INSERT INTO estimates ({cols}) VALUES ({placeholders}) RETURNING id',
                [estimate[c] for c in ESTIMATE_COLUMNS])
    estimate_id = cur.fetchone()['id']
    cur.execute('UPDATE estimates SET estimate_no=%s WHERE id=%s',
                (f"{estimate['issue_date']:%Y%m}-{estimate_id:04d}", estimate_id))
    save_lines(cur, estimate_id, lines)
    return estimate_id


# ---------- 見積一覧・作成・編集 ----------

@estimate_bp.route('/')
@require_auth
def index():
    q = request.args.get('q', '').strip()
    status = request.args.get('status', '')
    sql = '''
        SELECT e.*, COALESCE(SUM(ROUND(l.quantity * l.unit_price)), 0) AS subtotal
        FROM estimates e LEFT JOIN estimate_lines l ON l.estimate_id = e.id
        WHERE 1=1
    '''
    params = []
    if q:
        sql += ' AND (e.customer_name ILIKE %s OR e.title ILIKE %s OR e.estimate_no ILIKE %s)'
        params += [f'%{q}%'] * 3
    if status in STATUSES:
        sql += ' AND e.status = %s'
        params.append(status)
    sql += ' GROUP BY e.id ORDER BY e.issue_date DESC, e.id DESC'
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    estimates = []
    for r in rows:
        # 一覧では明細を読まずに税込合計を出す（明細の小計を1行として計算）
        totals = calc_totals(r, [{'quantity': 1, 'unit_price': r['subtotal']}])
        estimates.append(dict(r, total=totals['total']))
    return render_template('estimate/index.html', estimates=estimates, q=q,
                           status=status, statuses=STATUSES)


def blank_estimate():
    return {
        'id': None, 'estimate_no': '', 'customer_name': '', 'title': '', 'site_address': '',
        'issue_date': date.today(), 'valid_days': 30, 'overhead_rate': Decimal('10'),
        'discount': 0, 'tax_rate': Decimal('10'), 'status': '下書き', 'notes': '',
    }


def render_form(estimate, lines):
    items = fetch_items()
    return render_template('estimate/form.html', estimate=estimate, items=items,
                           items_json=jsonable(items), lines_json=jsonable(lines),
                           statuses=STATUSES)


@estimate_bp.route('/new', methods=['GET', 'POST'])
@require_auth
def new():
    if request.method == 'POST':
        estimate, lines = parse_estimate_form(request.form)
        with get_db() as conn:
            with conn.cursor() as cur:
                estimate_id = insert_estimate(cur, estimate, lines)
            conn.commit()
        flash('見積を作成しました')
        return redirect(url_for('estimate.show', estimate_id=estimate_id))
    return render_form(blank_estimate(), [])


@estimate_bp.route('/<int:estimate_id>/edit', methods=['GET', 'POST'])
@require_auth
def edit(estimate_id):
    if request.method == 'POST':
        fetch_estimate(estimate_id)  # 存在確認
        estimate, lines = parse_estimate_form(request.form)
        sets = ', '.join(f'{c}=%s' for c in ESTIMATE_COLUMNS)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f'UPDATE estimates SET {sets}, updated_at=NOW() WHERE id=%s',
                            [estimate[c] for c in ESTIMATE_COLUMNS] + [estimate_id])
                save_lines(cur, estimate_id, lines)
            conn.commit()
        flash('見積を保存しました')
        return redirect(url_for('estimate.show', estimate_id=estimate_id))
    estimate, lines = fetch_estimate(estimate_id)
    return render_form(estimate, lines)


@estimate_bp.route('/<int:estimate_id>')
@require_auth
def show(estimate_id):
    estimate, lines = fetch_estimate(estimate_id)
    return render_template('estimate/show.html', estimate=estimate,
                           groups=group_lines(lines), totals=calc_totals(estimate, lines),
                           valid_until=estimate['issue_date'] + timedelta(days=estimate['valid_days']),
                           company=COMPANY)


@estimate_bp.route('/<int:estimate_id>/copy', methods=['POST'])
@require_auth
def copy(estimate_id):
    estimate, lines = fetch_estimate(estimate_id)
    estimate = dict(estimate, issue_date=date.today(), status='下書き',
                    title=f"{estimate['title']}（コピー）")
    with get_db() as conn:
        with conn.cursor() as cur:
            new_id = insert_estimate(cur, estimate, lines)
        conn.commit()
    flash('見積を複製しました')
    return redirect(url_for('estimate.edit', estimate_id=new_id))


@estimate_bp.route('/<int:estimate_id>/delete', methods=['POST'])
@require_auth
def delete(estimate_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM estimates WHERE id=%s', (estimate_id,))
        conn.commit()
    flash('見積を削除しました')
    return redirect(url_for('estimate.index'))


# ---------- 単価マスタ ----------

def parse_item_form(form):
    return (form.get('category', '').strip(), form.get('name', '').strip(),
            form.get('spec', '').strip(), form.get('unit', '').strip() or '式',
            int(to_decimal(form.get('unit_price'))), int(to_decimal(form.get('sort_order'))))


@estimate_bp.route('/items')
@require_auth
def items():
    return render_template('estimate/items.html', items=fetch_items())


@estimate_bp.route('/items/add', methods=['POST'])
@require_auth
def item_add():
    values = parse_item_form(request.form)
    if values[1]:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO est_items (category, name, spec, unit, unit_price, sort_order) '
                            'VALUES (%s, %s, %s, %s, %s, %s)', values)
            conn.commit()
        flash('単価を追加しました')
    return redirect(url_for('estimate.items'))


@estimate_bp.route('/items/<int:item_id>/update', methods=['POST'])
@require_auth
def item_update(item_id):
    values = parse_item_form(request.form)
    if values[1]:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE est_items SET category=%s, name=%s, spec=%s, unit=%s, '
                            'unit_price=%s, sort_order=%s WHERE id=%s', values + (item_id,))
            conn.commit()
        flash('単価を更新しました')
    return redirect(url_for('estimate.items'))


@estimate_bp.route('/items/<int:item_id>/delete', methods=['POST'])
@require_auth
def item_delete(item_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM est_items WHERE id=%s', (item_id,))
        conn.commit()
    flash('単価を削除しました')
    return redirect(url_for('estimate.items'))


CSV_HEADER = ['工種', '品名', '仕様', '単位', '単価', '並び順']


@estimate_bp.route('/items/export.csv')
@require_auth
def items_export():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for it in fetch_items():
        writer.writerow([it['category'], it['name'], it['spec'], it['unit'],
                         it['unit_price'], it['sort_order']])
    # Excelで文字化けしないようBOM付きUTF-8
    return Response('﻿' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename=unit_prices.csv'})


@estimate_bp.route('/items/import', methods=['POST'])
@require_auth
def items_import():
    file = request.files.get('file')
    if not file:
        flash('CSVファイルを選択してください')
        return redirect(url_for('estimate.items'))
    raw = file.read()
    for encoding in ('utf-8-sig', 'cp932'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        flash('CSVの文字コードを判別できませんでした（UTF-8 または Shift_JIS で保存してください）')
        return redirect(url_for('estimate.items'))
    rows = list(csv.reader(io.StringIO(text)))
    if rows and rows[0][:2] == CSV_HEADER[:2]:
        rows = rows[1:]
    records = []
    for r in rows:
        r = (r + [''] * 6)[:6]
        if not r[1].strip():
            continue
        records.append((r[0].strip(), r[1].strip(), r[2].strip(), r[3].strip() or '式',
                        int(to_decimal(r[4])), int(to_decimal(r[5], str(len(records))))))
    replace = request.form.get('mode') == 'replace'
    with get_db() as conn:
        with conn.cursor() as cur:
            if replace:
                cur.execute('DELETE FROM est_items')
            for rec in records:
                cur.execute('INSERT INTO est_items (category, name, spec, unit, unit_price, sort_order) '
                            'VALUES (%s, %s, %s, %s, %s, %s)', rec)
        conn.commit()
    flash(f'{len(records)}件の単価を取り込みました' + ('（既存を置き換え）' if replace else ''))
    return redirect(url_for('estimate.items'))
