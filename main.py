import os
import json
import time
import hmac
import base64
import uuid
import asyncio
import hashlib
import binascii
import re as _re

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from requests.auth import HTTPBasicAuth
from google import genai
from dotenv import load_dotenv

load_dotenv()

# ==================== Настройки ====================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

# Куда ЮKassa вернёт пользователя после оплаты
RETURN_URL = os.getenv("RETURN_URL", "https://babayagaland.ru/aiayaga")

# Пакеты пополнения в формате "сумма:кредиты,сумма:кредиты", например "199:100,499:300,990:700".
# Если заданы, сервер сам решает, сколько кредитов дать за сумму, и подделать это из браузера нельзя.
PAYMENT_PACKAGES_RAW = os.getenv("PAYMENT_PACKAGES", "").strip()

# Секрет для подписи токенов входа (любая длинная случайная строка).
APP_SECRET = os.getenv("APP_SECRET", "").strip()
# "1" = требовать токен на чате и истории. Включать ПОСЛЕ того, как фронтенд начнёт его передавать.
REQUIRE_TOKEN = os.getenv("REQUIRE_TOKEN", "0").strip() == "1"
TOKEN_TTL = int(os.getenv("TOKEN_TTL_DAYS", "30")) * 86400

# Через запятую, например "https://babayagaland.ru". По умолчанию разрешены все.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

MAX_FILE_MB = 15
MAX_HISTORY = 30
SCRIPT_TIMEOUT = 25

MODEL_MAPPING = {
    'flash': 'gemini-3.8-flash',
    'flash_25': 'gemini-3.8-flash',
    'pro': 'gemini-3.1-pro-preview',
    'nanobanana': 'gemini-3.1-flash-image',
    'nanobanana_pro': 'gemini-3-pro-image'
}

# Цены в кредитах (1 кредит = 1 ₽)
CREDIT_COSTS = {
    'flash': 4,
    'flash_25': 4,
    'pro': 15,
    'nanobanana': 20,
    'nanobanana_pro': 50
}

IMAGE_MODELS = {'nanobanana', 'nanobanana_pro'}
BIZ_TOOL_COST = 15

SYSTEM_INSTRUCTION = (
    "Ты — НейроЯга, премиальный искусственный интеллект и бизнес-консультант. "
    "Отвечай структурно, грамотно, с легким сказочным вайбом, но четко и по делу."
)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=GEMINI_API_KEY)


def parse_packages(raw: str) -> dict:
    packages = {}
    for chunk in raw.split(","):
        if ":" not in chunk:
            continue
        a, c = chunk.split(":", 1)
        try:
            packages[round(float(a.strip()), 2)] = int(c.strip())
        except ValueError:
            print(f"⚠️ Не понял пакет оплаты: {chunk!r}")
    return packages


PAYMENT_PACKAGES = parse_packages(PAYMENT_PACKAGES_RAW)
if not PAYMENT_PACKAGES:
    print("⚠️ PAYMENT_PACKAGES не задан: сумму и кредиты присылает браузер, это можно подделать.")
if REQUIRE_TOKEN and not APP_SECRET:
    print("⚠️ REQUIRE_TOKEN=1, но APP_SECRET пуст: проверка токенов отключена.")
    REQUIRE_TOKEN = False


# ==================== Токены входа ====================

def make_token(email: str) -> str:
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{email}|{exp}"
    sig = hmac.new(APP_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def check_token(email: str, token: str) -> bool:
    if not token or not APP_SECRET:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        t_email, exp, sig = raw.rsplit("|", 2)
        expected = hmac.new(APP_SECRET.encode(), f"{t_email}|{exp}".encode(), hashlib.sha256).hexdigest()
        return (hmac.compare_digest(sig, expected) and t_email.strip().lower() == str(email or "").strip().lower()
                and int(exp) > time.time())
    except Exception:
        return False


def is_authorized(email: str, token) -> bool:
    if not REQUIRE_TOKEN:
        return True
    return check_token(email, token or "")


def token_from_request(request: Request, body: dict = None):
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if body and body.get("token"):
        return body.get("token")
    return request.query_params.get("token")


# ==================== База данных PostgreSQL ====================
# Пока пользователи не перенесены из Google-таблицы (флаг migrated), сервер работает по-старому, с таблицей.
# После переноса всё — вход, баланс, списания, оплаты — идёт через базу.
import asyncpg
import secrets as _secrets

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
db_pool = None
DB_MIGRATED = False

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    email TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    credits INTEGER NOT NULL DEFAULT 0 CHECK (credits >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS transactions (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    delta INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    kind TEXT NOT NULL,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS transactions_email_idx ON transactions (email, created_at DESC);
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    amount_rub NUMERIC,
    credits INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def db_ready() -> bool:
    return db_pool is not None and DB_MIGRATED


def hash_password(password: str) -> str:
    salt = _secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${200_000}${salt}${dk}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        test = hashlib.pbkdf2_hmac("sha256", str(password).encode(), salt.encode(), int(it)).hex()
        return hmac.compare_digest(test, dk)
    except Exception:
        return False


def norm_email(email) -> str:
    return str(email or "").strip().lower()


async def _get_setting(con, key):
    return await con.fetchval("SELECT value FROM settings WHERE key=$1", key)


async def _set_setting(con, key, value):
    await con.execute("INSERT INTO settings(key, value) VALUES($1, $2) "
                      "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", key, str(value))


async def _enable_tokens(con):
    """После переноса включаем проверку пропусков. Секрет хранится в базе и не меняется при перезапуске."""
    global APP_SECRET, REQUIRE_TOKEN
    if not APP_SECRET:
        secret = await _get_setting(con, "app_secret")
        if not secret:
            secret = _secrets.token_hex(32)
            await _set_setting(con, "app_secret", secret)
        APP_SECRET = secret
    REQUIRE_TOKEN = True


@app.on_event("startup")
async def db_startup():
    global db_pool, DB_MIGRATED
    if not DATABASE_URL:
        print("ℹ️ DATABASE_URL не задан — пользователи хранятся в Google-таблице")
        return
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=15)
        async with db_pool.acquire() as con:
            await con.execute(SCHEMA_SQL)
            DB_MIGRATED = (await _get_setting(con, "migrated")) == "1"
            if DB_MIGRATED:
                await _enable_tokens(con)
        print("✅ База данных подключена" + (" — пользователи в базе" if DB_MIGRATED else
                                              " — ждёт переноса пользователей из таблицы"))
    except Exception as e:
        db_pool = None
        print(f"❌ Не удалось подключиться к базе: {e}. Работаю с Google-таблицей")


@app.on_event("shutdown")
async def db_shutdown():
    if db_pool:
        await db_pool.close()


async def db_log(con, email, delta, balance, kind, note=""):
    await con.execute("INSERT INTO transactions(email, delta, balance_after, kind, note) VALUES($1,$2,$3,$4,$5)",
                      email, delta, balance, kind, (note or "")[:200])


async def db_auth(email: str, password: str) -> dict:
    email = norm_email(email)
    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT password_hash, credits FROM users WHERE email=$1", email)
        if row is None:
            if len(str(password)) < 4:
                return {"success": False, "error": "Пароль должен быть не короче 4 символов"}
            await con.execute("INSERT INTO users(email, password_hash, last_login) VALUES($1, $2, now())",
                              email, hash_password(password))
            print(f"🆕 Новый пользователь {email}")
            return {"success": True, "credits": 0, "new": True}
        if not check_password(password, row["password_hash"]):
            return {"success": False, "error": "Неверный пароль"}
        await con.execute("UPDATE users SET last_login=now() WHERE email=$1", email)
        return {"success": True, "credits": row["credits"]}


async def db_deduct(email: str, cost: int, note: str = "") -> dict:
    email = norm_email(email)
    async with db_pool.acquire() as con:
        if cost <= 0:
            bal = await con.fetchval("SELECT credits FROM users WHERE email=$1", email)
            return {"success": bal is not None, "credits": bal or 0, "error": False}
        async with con.transaction():
            bal = await con.fetchval("UPDATE users SET credits=credits-$2 WHERE email=$1 AND credits>=$2 "
                                     "RETURNING credits", email, int(cost))
            if bal is None:
                cur = await con.fetchval("SELECT credits FROM users WHERE email=$1", email)
                return {"success": False, "credits": cur or 0, "error": False}
            await db_log(con, email, -int(cost), bal, "spend", note)
            return {"success": True, "credits": bal, "error": False}


async def db_add(email: str, amount: int, kind: str, note: str = ""):
    email = norm_email(email)
    async with db_pool.acquire() as con:
        async with con.transaction():
            bal = await con.fetchval("UPDATE users SET credits=credits+$2 WHERE email=$1 RETURNING credits",
                                     email, int(amount))
            if bal is None:
                return None
            await db_log(con, email, int(amount), bal, kind, note)
            return bal


@app.post("/api/admin/import-users")
async def import_users(request: Request):
    """Разовый перенос пользователей из Google-таблицы. Вызывается скриптом таблицы с секретным ключом."""
    global DB_MIGRATED
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "Неверный запрос"}
    if not TEMPLATES_KEY or data.get("key") != TEMPLATES_KEY:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    if db_pool is None:
        return {"ok": False, "error": "База данных не подключена"}
    added, skipped, bad = 0, 0, []
    async with db_pool.acquire() as con:
        async with con.transaction():
            for u in data.get("users") or []:
                email = norm_email(u.get("email"))
                password = str(u.get("password") or "").strip()
                try:
                    credits = max(0, int(float(u.get("credits") or 0)))
                except (TypeError, ValueError):
                    credits = 0
                if "@" not in email or not password:
                    bad.append(email or "(пусто)")
                    continue
                res = await con.execute(
                    "INSERT INTO users(email, password_hash, credits) VALUES($1,$2,$3) ON CONFLICT (email) DO NOTHING",
                    email, hash_password(password), credits)
                if res.endswith(" 1"):
                    added += 1
                    await db_log(con, email, credits, credits, "migration", "Перенос из Google-таблицы")
                else:
                    skipped += 1
            await _set_setting(con, "migrated", "1")
            await _enable_tokens(con)
    DB_MIGRATED = True
    print(f"📦 Перенос пользователей: добавлено {added}, уже были {skipped}, пропущено {len(bad)}")
    return {"ok": True, "added": added, "skipped": skipped, "bad": bad}


KIND_TITLES = {"payment": "Пополнение", "spend": "Списание", "refund": "Возврат", "migration": "Перенос баланса",
               "admin": "Начисление"}


@app.get("/api/account/{email}")
async def account_info(email: str, request: Request):
    email = norm_email(email)
    if not is_authorized(email, token_from_request(request)):
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    if not db_ready():
        res = await check_and_deduct_credits(email, 0)
        return {"ok": True, "email": email, "credits": res.get("credits", 0), "history": [], "db": False}
    async with db_pool.acquire() as con:
        u = await con.fetchrow("SELECT credits, created_at FROM users WHERE email=$1", email)
        if u is None:
            return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
        rows = await con.fetch("SELECT delta, balance_after, kind, note, created_at FROM transactions "
                               "WHERE email=$1 ORDER BY created_at DESC LIMIT 100", email)
        stats = await con.fetchrow("SELECT COALESCE(SUM(credits),0) AS bought, COUNT(*) AS n FROM payments WHERE email=$1",
                                   email)
        spent = await con.fetchval("SELECT COALESCE(-SUM(delta),0) FROM transactions WHERE email=$1 "
                                   "AND kind IN ('spend','refund')", email)
    return {"ok": True, "db": True, "email": email, "credits": u["credits"],
            "since": u["created_at"].strftime("%d.%m.%Y"),
            "bought": int(stats["bought"]), "payments": int(stats["n"]), "spent": max(0, int(spent or 0)),
            "history": [{"delta": r["delta"], "balance": r["balance_after"], "title": KIND_TITLES.get(r["kind"], r["kind"]),
                         "kind": r["kind"], "note": r["note"] or "", "date": r["created_at"].strftime("%d.%m.%Y %H:%M")}
                        for r in rows]}


@app.post("/api/account/password")
async def change_password(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "Неверный запрос"}
    email = norm_email(data.get("email"))
    if not is_authorized(email, token_from_request(request, data)):
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    if not db_ready():
        return {"ok": False, "error": "Смена пароля появится после переноса базы"}
    old, new = str(data.get("old") or ""), str(data.get("new") or "")
    if len(new) < 6:
        return {"ok": False, "error": "Новый пароль — не короче 6 символов"}
    async with db_pool.acquire() as con:
        h = await con.fetchval("SELECT password_hash FROM users WHERE email=$1", email)
        if not h or not check_password(old, h):
            return {"ok": False, "error": "Текущий пароль неверный"}
        await con.execute("UPDATE users SET password_hash=$2 WHERE email=$1", email, hash_password(new))
    return {"ok": True}


# ==================== Google Apps Script ====================

def _script_call_sync(payload: dict):
    """Возвращает dict-ответ скрипта или None, если скрипт недоступен."""
    try:
        r = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=SCRIPT_TIMEOUT)
        if r.status_code != 200:
            print(f"❌ Apps Script вернул HTTP {r.status_code} на {payload.get('action')}")
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"❌ Ошибка при обращении к Google Apps Script ({payload.get('action')}): {e}")
        return None


async def script_call(payload: dict):
    # requests блокирующий, поэтому уводим его в отдельный поток, чтобы не вешать сервер для всех
    return await asyncio.to_thread(_script_call_sync, payload)


async def check_and_deduct_credits(email: str, cost: int, note: str = "") -> dict:
    if db_ready():
        try:
            return await db_deduct(email, cost, note)
        except Exception as e:
            print(f"❌ База: списание {email}: {e}")
            return {"success": False, "credits": 0, "error": True}
    if not GOOGLE_SCRIPT_URL:
        return {"success": True, "credits": 999, "error": False}
    res = await script_call({"action": "deduct", "email": email, "cost": cost})
    if res is None:
        return {"success": False, "credits": 0, "error": True}
    return {"success": bool(res.get("success", False)), "credits": res.get("credits", 0), "error": False}


async def refund_credits(email: str, amount: int, balance_before: int) -> int:
    """Возвращает кредиты за неудачный запрос. Отдаёт новый баланс для показа."""
    if amount <= 0:
        return balance_before
    if db_ready():
        try:
            bal = await db_add(email, amount, "refund", "Возврат за неудачную генерацию")
            return bal if bal is not None else balance_before
        except Exception as e:
            print(f"❌ База: возврат {amount} кр. {email}: {e} — верните вручную")
            return balance_before
    if not GOOGLE_SCRIPT_URL:
        return balance_before
    res = await script_call({"action": "add", "email": email, "amount": amount, "reason": "refund"})
    if res is None:
        print(f"❌ Не удалось вернуть {amount} кр. пользователю {email} — верните вручную")
        return balance_before
    new_balance = res.get("credits")
    return new_balance if isinstance(new_balance, (int, float)) else balance_before + amount


# ==================== Чат (WebSocket) ====================

async def safe_send(ws: WebSocket, payload: dict) -> bool:
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
        return True
    except Exception:
        return False


def decode_file(file_obj):
    """Возвращает (bytes, mime) или бросает ValueError с понятным текстом."""
    data = file_obj.get('data') or ''
    mime = file_obj.get('mimeType') or 'image/jpeg'
    if not isinstance(data, str):
        raise ValueError("Файл пришёл в непонятном виде")
    if data.startswith('data:') and ',' in data:  # data:image/png;base64,....
        head, data = data.split(',', 1)
        if ';' in head:
            mime = head[5:head.index(';')] or mime
    if len(data) * 3 / 4 > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"Файл больше {MAX_FILE_MB} МБ, сожми его или отправь другой")
    try:
        return base64.b64decode(data, validate=False), mime
    except (binascii.Error, ValueError):
        raise ValueError("Не удалось прочитать файл, попробуй отправить его ещё раз")


def build_history(history_list) -> list:
    contents = []
    if not isinstance(history_list, list):
        return contents
    for item in history_list[-MAX_HISTORY:]:
        if not isinstance(item, dict):
            continue
        text = item.get('text')
        if not isinstance(text, str) or not text.strip():
            continue
        role = 'user' if item.get('role') == 'user' else 'model'
        contents.append({"role": role, "parts": [{"text": text}]})
    return contents


def parse_response(response):
    text_output, image_data, mime_type = "", None, "image/jpeg"
    candidate = response.candidates[0] if getattr(response, 'candidates', None) else None
    if candidate and candidate.content and candidate.content.parts:
        for part in candidate.content.parts:
            if getattr(part, 'text', None):
                text_output += ("\n" if text_output else "") + part.text
            inline = getattr(part, 'inline_data', None) or getattr(part, 'inlineData', None)
            if inline and getattr(inline, 'data', None):
                raw_bytes = inline.data
                image_data = base64.b64encode(raw_bytes).decode('utf-8') if isinstance(raw_bytes, bytes) else str(raw_bytes)
                mime_type = getattr(inline, 'mime_type', None) or getattr(inline, 'mimeType', None) or "image/jpeg"
    return text_output, image_data, mime_type


# ==================== Размер картинки ====================
ASPECTS = {
    "1:1": "квадрат", "4:5": "вертикальный пост", "3:4": "вертикальный", "2:3": "вертикальный",
    "9:16": "вертикальный для сторис", "4:3": "горизонтальный", "3:2": "горизонтальный", "16:9": "широкий горизонтальный",
}


def clean_aspect(value):
    value = str(value or "").strip()
    return value if value in ASPECTS else None


def with_aspect_hint(text: str, aspect) -> str:
    if not aspect:
        return text
    return f"{text}\n\nФормат изображения: соотношение сторон {aspect} ({ASPECTS[aspect]}). Композицию строй под этот формат."


async def generate(model: str, contents, aspect=None, system=None):
    """Генерация с выбранным размером. Если модель не понимает настройку размера,
    повторяем без неё — размер тогда задаёт подсказка в тексте запроса."""
    base = {"system_instruction": system} if system else {}
    if aspect:
        try:
            cfg = dict(base, response_modalities=["TEXT", "IMAGE"], image_config={"aspect_ratio": aspect})
            return await asyncio.to_thread(client.models.generate_content, model=model, contents=contents, config=cfg)
        except Exception as e:
            print(f"⚠️ Размер {aspect} не принят моделью {model}, пробую без него: {e}")
    return await asyncio.to_thread(client.models.generate_content, model=model, contents=contents, config=base or None)


@app.websocket("/ws/{client_email}")
async def websocket_endpoint(websocket: WebSocket, client_email: str):
    await websocket.accept()
    email = client_email.strip()

    if not is_authorized(email, websocket.query_params.get("token")):
        await safe_send(websocket, {"type": "text", "credits": 0, "auth": False,
                                    "text": "🔒 Сессия истекла. Войди в аккаунт заново."})
        await websocket.close(code=4401)
        return

    print(f"Клиент {email} успешно подключился!")
    init_result = await check_and_deduct_credits(email, 0)
    balance = init_result.get("credits", 0)  # последний известный баланс, чтобы фронт не показал пустоту
    await safe_send(websocket, {
        "type": "text",
        "credits": balance,
        "text": "🧚‍♀️ Приветствую в НейроЯге! Выбирай модель в меню сверху, задавай вопросы или прикрепляй фото и файлы."
    })

    try:
        while True:
            raw_message = await websocket.receive_text()

            prompt, model_key, file_obj, history_list, biz_tool_id, aspect = raw_message, 'flash', None, [], None, None
            try:
                data = json.loads(raw_message)
                if isinstance(data, dict):
                    prompt = data.get('prompt', '')
                    model_key = data.get('modelKey', 'flash')
                    file_obj = data.get('file')
                    history_list = data.get('history', [])
                    biz_tool_id = data.get('biz_tool_id')
                    aspect = clean_aspect(data.get('aspect'))
            except json.JSONDecodeError:
                pass

            prompt = prompt.strip() if isinstance(prompt, str) else str(prompt or '').strip()
            if model_key not in MODEL_MAPPING:
                model_key = 'flash'

            # Файл разбираем ДО списания, чтобы не брать кредиты за битый файл
            file_part = None
            if isinstance(file_obj, dict) and file_obj.get('data'):
                try:
                    file_bytes, mime = decode_file(file_obj)
                    file_part = {"inline_data": {"mime_type": mime, "data": file_bytes}}
                except ValueError as e:
                    await safe_send(websocket, {"type": "text", "credits": balance, "text": f"📎 {e}"})
                    continue

            if not prompt and not file_part:
                await safe_send(websocket, {"type": "text", "credits": balance,
                                            "text": "✍️ Напиши вопрос или прикрепи файл."})
                continue

            if biz_tool_id:
                required_credits, model_key = BIZ_TOOL_COST, 'flash'
            else:
                required_credits = CREDIT_COSTS.get(model_key, 1)
            resolved_model = MODEL_MAPPING[model_key]

            credit_result = await check_and_deduct_credits(
                email, required_credits, ("Бизнес-инструмент" if biz_tool_id else "Чат: " + model_key))
            current_balance = credit_result.get("credits", 0)
            if not credit_result.get("error"):
                balance = current_balance

            if not credit_result.get("success"):
                if credit_result.get("error"):
                    text = "⏳ Не удалось связаться с сервером кредитов. Кредиты не списаны, попробуй ещё раз через минуту."
                else:
                    text = (f"💎 Недостаточно кредитов! Требуется: {required_credits} кр., "
                            f"а на балансе: {current_balance} кр. Нажми кнопку «Пополнить» выше.")
                await safe_send(websocket, {"type": "text", "credits": balance, "text": text})
                continue

            if model_key in IMAGE_MODELS:
                await safe_send(websocket, {
                    "type": "text", "credits": current_balance,
                    "text": "🎨 НейроЯга ушла в чащу за волшебными красками... Картинка создается, подождите пару секунд!"
                })

            try:
                contents = build_history(history_list)
                current_parts = []
                if file_part:
                    current_parts.append(file_part)
                is_image = model_key in IMAGE_MODELS
                use_aspect = aspect if is_image else None
                current_parts.append({"text": with_aspect_hint(prompt or "Что изображено на этом файле?", use_aspect)})
                contents.append({"role": "user", "parts": current_parts})

                response = await generate(resolved_model, contents, use_aspect, SYSTEM_INSTRUCTION)
                text_output, image_data, mime_type = parse_response(response)

                if image_data:
                    payload = {
                        "type": "image", "data": image_data, "mimeType": mime_type, "credits": current_balance,
                        "text": text_output or f"🎨 Твой шедевр готов! (-{required_credits} кр.)"
                    }
                elif text_output:
                    payload = {"type": "text", "credits": current_balance, "text": text_output}
                else:
                    # Модель ничего не вернула (например, сработал фильтр) — возвращаем кредиты
                    new_balance = await refund_credits(email, required_credits, current_balance)
                    balance = new_balance
                    payload = {"type": "text", "credits": new_balance,
                               "text": "🌫 Туман скрыл ответ. Кредиты вернула, попробуй переформулировать запрос."}

                await safe_send(websocket, payload)

            except Exception as e:
                print(f"Ошибка при запросе к модели {resolved_model}: {e}")
                new_balance = await refund_credits(email, required_credits, current_balance)
                balance = new_balance
                await safe_send(websocket, {
                    "type": "text", "credits": new_balance,
                    "text": "🌫 Ох, туман помешал ответу. Кредиты вернула, попробуй ещё раз чуть позже."
                })

    except WebSocketDisconnect:
        print(f"Клиент {email} отключился.")
    except Exception as e:
        print(f"❌ Обрыв соединения с {email}: {e}")


# ==================== Авторизация и история ====================

@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/auth")
async def authenticate_user(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"success": False, "error": "Неверный запрос"}
    email = str(data.get("email") or "").strip()
    password = data.get("password")
    if not email or not password:
        return {"success": False, "error": "Введите email и пароль"}

    if db_ready():
        email = norm_email(email)
        try:
            result = await db_auth(email, password)
        except Exception as e:
            print(f"❌ База: вход {email}: {e}")
            return {"success": False, "error": "Ошибка сервера авторизации"}
    elif not GOOGLE_SCRIPT_URL:
        result = {"success": True}
    else:
        result = await script_call({"action": "auth", "email": email, "password": password})
        if result is None:
            return {"success": False, "error": "Ошибка сервера авторизации"}

    if result.get("success") and APP_SECRET:
        result["token"] = make_token(email)
        result["email"] = email
    return result


@app.get("/api/history/{email}")
async def get_user_history(email: str, request: Request):
    email = email.strip()
    if not is_authorized(email, token_from_request(request)):
        return JSONResponse({"success": False, "error": "Нужно войти заново"}, status_code=401)
    if not GOOGLE_SCRIPT_URL:
        return {"success": True, "messages": []}
    data = await script_call({"action": "get_history", "email": email})
    return data if data is not None else {"success": True, "messages": []}


@app.post("/api/history")
async def save_user_history(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"success": False, "error": "Неверный запрос"}
    email = str(data.get("email") or "").strip()
    if not is_authorized(email, token_from_request(request, data)):
        return JSONResponse({"success": False, "error": "Нужно войти заново"}, status_code=401)
    if not GOOGLE_SCRIPT_URL:
        return {"success": True}
    res = await script_call({
        "action": "save_message",
        "email": email,
        "role": data.get("role"),
        "text": data.get("text"),
        "image": data.get("image")
    })
    return {"success": res is not None}


# ==================== Платежи ЮKassa ====================

def _yookassa_request(method: str, url: str, **kwargs):
    return requests.request(method, url, auth=HTTPBasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
                            timeout=15, **kwargs)


@app.post("/api/create-payment")
async def create_payment(request: Request):
    try:
        data = await request.json()
        email = str(data.get("email") or "").strip()
        amount = round(float(data.get("amount") or 0), 2)
        credits = int(data.get("credits") or 0)
    except (ValueError, TypeError):
        return {"success": False, "error": "Неверные параметры"}
    except Exception:
        return {"success": False, "error": "Неверный запрос"}

    if not email or amount <= 0:
        return {"success": False, "error": "Неверные параметры"}

    if PAYMENT_PACKAGES:
        if amount not in PAYMENT_PACKAGES:
            return {"success": False, "error": "Такого пакета нет, обновите страницу"}
        credits = PAYMENT_PACKAGES[amount]  # кредиты определяет сервер, а не браузер
    if credits <= 0:
        return {"success": False, "error": "Неверные параметры"}

    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return {"success": True, "confirmation_url": "https://yookassa.ru"}

    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": RETURN_URL},
        "description": f"Покупка {credits} кредитов НейроЯга для {email}"[:128],
        "metadata": {"email": email, "credits": credits}
    }
    try:
        response = await asyncio.to_thread(
            _yookassa_request, "POST", "https://api.yookassa.ru/v3/payments",
            json=payload, headers={"Idempotence-Key": str(uuid.uuid4()), "Content-Type": "application/json"}
        )
        res_json = response.json()
    except Exception as e:
        print(f"Ошибка создания платежа ЮKassa: {e}")
        return {"success": False, "error": "Платёжный сервис не отвечает, попробуйте позже"}

    confirmation_url = (res_json.get("confirmation") or {}).get("confirmation_url")
    if confirmation_url:
        return {"success": True, "confirmation_url": confirmation_url}
    print(f"ЮKassa отказала: {res_json}")
    return {"success": False, "error": res_json.get("description", "Ошибка создания платежа")}


# id уже зачисленных платежей (защита от повторной доставки вебхука; живёт до перезапуска)
processed_payments = set()
payments_lock = asyncio.Lock()


@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    try:
        event_json = await request.json()
    except Exception:
        return JSONResponse({"status": "bad request"}, status_code=400)

    if event_json.get("event") != "payment.succeeded":
        return {"status": "ok"}

    payment_id = (event_json.get("object") or {}).get("id")
    if not payment_id:
        return JSONResponse({"status": "no payment id"}, status_code=400)

    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        print("⚠️ Вебхук пришёл, но ключи ЮKassa не заданы — платёж не проверить, пропускаю")
        return {"status": "ok"}

    async with payments_lock:
        if payment_id in processed_payments:
            return {"status": "ok"}

        # Не верим телу вебхука: запрашиваем платёж у самой ЮKassa
        try:
            r = await asyncio.to_thread(_yookassa_request, "GET",
                                        f"https://api.yookassa.ru/v3/payments/{payment_id}")
            payment = r.json()
        except Exception as e:
            print(f"❌ Не удалось проверить платёж {payment_id}: {e}")
            return JSONResponse({"status": "retry"}, status_code=500)  # ЮKassa повторит позже

        if payment.get("status") != "succeeded" or not payment.get("paid"):
            print(f"⚠️ Платёж {payment_id} не подтверждён ЮKassa (status={payment.get('status')}), игнорирую")
            return {"status": "ok"}

        metadata = payment.get("metadata") or {}
        email = metadata.get("email")
        try:
            credits_to_add = int(metadata.get("credits", 0))
        except (ValueError, TypeError):
            credits_to_add = 0

        if not email or credits_to_add <= 0:
            return {"status": "ok"}

        if db_ready():
            amount = (payment.get("amount") or {}).get("value")
            try:
                async with db_pool.acquire() as con:
                    async with con.transaction():
                        fresh = await con.fetchval(
                            "INSERT INTO payments(payment_id, email, amount_rub, credits) VALUES($1,$2,$3,$4) "
                            "ON CONFLICT (payment_id) DO NOTHING RETURNING payment_id",
                            payment_id, norm_email(email), amount, credits_to_add)
                        if not fresh:
                            return {"status": "ok"}  # этот платёж уже зачислен
                        bal = await con.fetchval("UPDATE users SET credits=credits+$2 WHERE email=$1 RETURNING credits",
                                                 norm_email(email), credits_to_add)
                        if bal is None:
                            raise RuntimeError("пользователь не найден")
                        await db_log(con, norm_email(email), credits_to_add, bal, "payment", f"Оплата {amount} ₽")
            except Exception as e:
                print(f"❌ База: зачисление платежа {payment_id} для {email}: {e}")
                return JSONResponse({"status": "retry"}, status_code=500)
            processed_payments.add(payment_id)
            print(f"✅ Успешно начислено {credits_to_add} кредитов пользователю {email} (платёж {payment_id})")
            return {"status": "ok"}

        if not GOOGLE_SCRIPT_URL:
            return {"status": "ok"}

        res = await script_call({"action": "add", "email": email, "amount": credits_to_add,
                                 "payment_id": payment_id})
        if res is None:
            return JSONResponse({"status": "retry"}, status_code=500)  # пусть ЮKassa пришлёт снова

        processed_payments.add(payment_id)
        print(f"✅ Успешно начислено {credits_to_add} кредитов пользователю {email} (платёж {payment_id})")
        return {"status": "ok"}



# ==================== Шаблоны из Google-таблицы ====================
# Отдельный Apps Script (apps_script_templates.gs) читает лист с шаблонами.
# Браузер видит только публичные поля, промпт остаётся на сервере.

TEMPLATES_SCRIPT_URL = os.getenv("TEMPLATES_SCRIPT_URL", "").strip()
TEMPLATES_KEY = os.getenv("TEMPLATES_KEY", "").strip()
TEMPLATES_TTL = int(os.getenv("TEMPLATES_CACHE_SEC", "300"))
TEMPLATE_DEFAULT_MODEL = os.getenv("TEMPLATE_MODEL", "nanobanana")
MAX_TEMPLATE_PHOTOS = 6

_tpl_cache = {"at": 0.0, "items": []}
_tpl_lock = asyncio.Lock()
_running = set()  # email-ы, у которых генерация уже идёт (защита от двойного клика)

TRUE_WORDS = {"да", "1", "true", "yes", "+", "✅", "y", "вкл", "on", "x", "х"}
FALSE_WORDS = {"нет", "0", "false", "no", "-", "❌", "выкл", "off"}


def _truthy(v) -> bool:
    return str(v).strip().lower() in TRUE_WORDS


def _falsy(v) -> bool:
    return str(v).strip().lower() in FALSE_WORDS


def _to_int(v, default=0) -> int:
    try:
        return int(float(str(v).replace(",", ".").replace("₽", "").replace("кр", "").strip()))
    except (ValueError, TypeError):
        return default


def parse_fields(raw) -> list:
    """Поля шаблона. Либо JSON-массив (как в ЯгаГенераторе), либо по строке на поле:
       фото | Ваше фото [| до 3] [| необязательно]
       текст | имя | Как вас зовут?
       выбор | пол | Ваш пол | Мужской; Женский
       дата | рождение | Дата рождения
       число | возраст | Сколько лет?"""
    if isinstance(raw, list):
        items = raw
    else:
        raw = str(raw or "").strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                items = []
        else:
            items = []
            photo_n = 0
            for line in raw.splitlines():
                parts = [p.strip() for p in line.split("|")]
                if not parts or not parts[0]:
                    continue
                ftype = parts[0].lower()
                if ftype == "фото":
                    photo_n += 1
                    f = {"type": "фото", "key": f"фото{photo_n}",
                         "label": parts[1] if len(parts) > 1 and parts[1] else "Ваше фото"}
                    for extra in parts[2:]:
                        low = extra.lower()
                        if low.startswith("до"):
                            f["multi"] = max(1, min(MAX_TEMPLATE_PHOTOS, _to_int(low[2:], 1)))
                        elif low.startswith("необяз"):
                            f["optional"] = True
                    items.append(f)
                elif len(parts) >= 3:
                    f = {"type": ftype, "key": parts[1], "label": parts[2]}
                    if ftype == "выбор":
                        opts = parts[3] if len(parts) > 3 else ""
                        f["options"] = [o.strip() for o in opts.replace(",", ";").split(";") if o.strip()]
                    if any(x.lower().startswith("необяз") for x in parts[3:]):
                        f["optional"] = True
                    items.append(f)
    clean = []
    for i, f in enumerate(items):
        if not isinstance(f, dict):
            continue
        ftype = str(f.get("type") or "текст").lower()
        if ftype not in ("фото", "текст", "выбор", "дата", "число"):
            ftype = "текст"
        item = {"type": ftype, "key": str(f.get("key") or f"поле{i + 1}"),
                "label": str(f.get("label") or f.get("key") or "Поле")}
        if ftype == "выбор":
            item["options"] = [str(o) for o in (f.get("options") or [])]
        if ftype == "фото" and f.get("multi"):
            item["multi"] = max(1, min(MAX_TEMPLATE_PHOTOS, _to_int(f.get("multi"), 1)))
        if f.get("optional"):
            item["optional"] = True
        clean.append(item)
    return clean


def normalize_template(row: dict):
    tid = str(row.get("id") or "").strip()
    title = str(row.get("title") or "").strip()
    if not tid or not title or not str(row.get("prompt") or "").strip():
        return None
    if row.get("active") not in (None, "") and _falsy(row.get("active")):
        return None
    if row.get("site") not in (None, "") and _falsy(row.get("site")):
        return None
    ttype = str(row.get("type") or "").lower()
    if "вид" in ttype or "video" in ttype or "chain" in ttype:
        return None  # видео Gemini не делает — на сайте только картинки
    price = _to_int(row.get("credits"), 0) or _to_int(row.get("price"), 0)
    if price <= 0:
        return None
    model = str(row.get("model") or "").strip().lower()
    model_key = "nanobanana_pro" if "pro" in model or "про" in model else (
        "nanobanana" if model else TEMPLATE_DEFAULT_MODEL)
    if model_key not in IMAGE_MODELS:
        model_key = "nanobanana"
    cats = [c.strip() for c in str(row.get("cats") or "").replace(";", ",").split(",") if c.strip()]
    return {
        "id": tid, "title": title, "desc": str(row.get("desc") or "").strip(),
        "image": str(row.get("image") or "").strip(), "price": price,
        "fields": parse_fields(row.get("fields")), "trend": _truthy(row.get("trend")),
        "cats": cats, "runs": _to_int(row.get("runs"), 0),
        "formats": [a for a in (x.strip() for x in str(row.get("formats") or "").replace(",", ";").split(";")) if a in ASPECTS],
        "_prompt": str(row.get("prompt")).strip(), "_model": model_key,
    }


def _count_hit(tid: str):
    try:
        requests.post(TEMPLATES_SCRIPT_URL, json={"action": "hit", "key": TEMPLATES_KEY, "id": tid},
                      timeout=SCRIPT_TIMEOUT)
    except Exception:
        pass


def public_template(t: dict) -> dict:
    return {k: v for k, v in t.items() if not k.startswith("_")}


async def load_templates(force: bool = False) -> list:
    if not TEMPLATES_SCRIPT_URL:
        return []
    async with _tpl_lock:
        if not force and _tpl_cache["items"] and time.time() - _tpl_cache["at"] < TEMPLATES_TTL:
            return _tpl_cache["items"]

        def fetch():
            try:
                r = requests.post(TEMPLATES_SCRIPT_URL, json={"action": "templates", "key": TEMPLATES_KEY},
                                  timeout=SCRIPT_TIMEOUT)
                data = r.json()
                return data if isinstance(data, dict) and data.get("ok") else None
            except Exception as e:
                print(f"❌ Не удалось загрузить шаблоны: {e}")
                return None

        data = await asyncio.to_thread(fetch)
        if data is None:
            return _tpl_cache["items"]  # отдаём старый кэш, если таблица недоступна
        items = [t for t in (normalize_template(r) for r in data.get("templates", []) if isinstance(r, dict)) if t]
        _tpl_cache.update(at=time.time(), items=items)
        return items


ZODIAC = [((1, 20), "Козерог"), ((2, 19), "Водолей"), ((3, 21), "Рыбы"), ((4, 20), "Овен"), ((5, 21), "Телец"),
          ((6, 21), "Близнецы"), ((7, 23), "Рак"), ((8, 23), "Лев"), ((9, 23), "Дева"), ((10, 23), "Весы"),
          ((11, 22), "Скорпион"), ((12, 22), "Стрелец"), ((12, 32), "Козерог")]


def zodiac_sign(date_str: str) -> str:
    """'2000-05-14' или '14.05.2000' → 'Телец'. Пустая строка, если дату не понять."""
    m = _re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})", date_str or "") or _re.match(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})", date_str or "")
    if not m:
        return ""
    if "-" in m.group(0):
        month, day = int(m.group(2)), int(m.group(3))
    else:
        day, month = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    for (mm, dd), name in ZODIAC:
        if (month, day) < (mm, dd):
            return name
    return "Козерог"


def build_template_prompt(template: dict, answers: dict) -> str:
    prompt = template["_prompt"]
    if "{знак}" in prompt:
        date_val = next((answers.get(f["key"], "") for f in template["fields"] if f["type"] == "дата"), "")
        prompt = prompt.replace("{знак}", zodiac_sign(date_val) or "знак зодиака по дате рождения")
    used = set()
    for f in template["fields"]:
        if f["type"] == "фото":
            continue
        val = answers.get(f["key"], "")
        for ph in ("{" + f["key"] + "}", "{" + f["key"].lower() + "}", "{" + f["key"].capitalize() + "}"):
            if ph in prompt:
                prompt = prompt.replace(ph, val)
                used.add(f["key"])
    rest = [f"{f['label']}: {answers[f['key']]}" for f in template["fields"]
            if f["type"] != "фото" and f["key"] not in used and answers.get(f["key"])]
    if rest:
        prompt += "\n\nДанные от пользователя:\n" + "\n".join(rest)
    return prompt


# «Эконом» для шаблонов: Nano Banana Братухи в обычном режиме (2 ₽) × 5
ECO_TEMPLATE_PRICE = 10


async def _eco_template_image(photos, prompt_text: str, aspect):
    """Генерация шаблона через Nano Banana Братухи. Возвращает (base64, mime)."""
    urls = []
    for i, (b, mime) in enumerate(photos[:10]):
        ext = "png" if "png" in str(mime) else "webp" if "webp" in str(mime) else "jpg"
        urls.append(await asyncio.to_thread(_bh_upload, b, mime, f"photo_{i + 1}.{ext}"))
    inp = {"mode": "normal", "prompt": prompt_text[:20000], "image_size": aspect or "auto"}
    if urls:
        inp["image_urls"] = urls
    op_id = await asyncio.to_thread(_bh_post_operation, "nano-banana", inp)
    url = await _bh_wait(op_id, 4 * 60, 3)
    r = await asyncio.to_thread(requests.get, url, timeout=60)
    if r.status_code != 200 or not r.content:
        raise RuntimeError(f"не удалось скачать результат: {r.status_code}")
    return base64.b64encode(r.content).decode(), (r.headers.get("content-type") or "image/jpeg").split(";")[0]


@app.get("/api/templates")
async def get_templates():
    if not TEMPLATES_SCRIPT_URL:
        return {"ok": False, "error": "Шаблоны ещё не подключены", "templates": []}
    items = await load_templates()
    if not items:
        return {"ok": False, "error": "Не удалось загрузить шаблоны, попробуйте позже", "templates": []}
    return {"ok": True, "templates": [public_template(t) for t in items],
            "eco": {"on": bool(BRATUKHA_API_KEY), "price": ECO_TEMPLATE_PRICE, "name": "Nano Banana"}}


@app.get("/api/balance/{email}")
async def get_balance(email: str, request: Request):
    email = email.strip()
    if not is_authorized(email, token_from_request(request)):
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    res = await check_and_deduct_credits(email, 0)
    if res.get("error"):
        return {"ok": False, "error": "Сервер кредитов не отвечает"}
    return {"ok": True, "credits": res.get("credits", 0)}


@app.post("/api/templates/run")
async def run_template(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "Неверный запрос"}
    email = str(data.get("email") or "").strip()
    if not email:
        return JSONResponse({"ok": False, "error": "Войдите в аккаунт"}, status_code=401)
    if not is_authorized(email, token_from_request(request, data)):
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)

    templates = await load_templates()
    template = next((t for t in templates if t["id"] == str(data.get("templateId"))), None)
    if not template:
        return {"ok": False, "error": "Шаблон не найден, обновите страницу"}

    aspect = clean_aspect(data.get("aspect"))
    answers_raw = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    answers = {str(k): str(v).strip()[:600] for k, v in answers_raw.items()}
    photos_raw = data.get("photos") if isinstance(data.get("photos"), list) else []

    # проверяем поля до списания кредитов
    for f in template["fields"]:
        if f["type"] != "фото" and not f.get("optional") and not answers.get(f["key"]):
            return {"ok": False, "error": f"Заполните: {f['label']}"}
    need_photo = any(f["type"] == "фото" and not f.get("optional") for f in template["fields"])
    if need_photo and not photos_raw:
        return {"ok": False, "error": "Добавьте фото"}

    eco = data.get("engine") == "eco" and bool(BRATUKHA_API_KEY)
    photo_parts, photo_bytes = [], []
    for p in photos_raw[:MAX_TEMPLATE_PHOTOS]:
        try:
            b, mime = decode_file({"data": p})
            photo_parts.append({"inline_data": {"mime_type": mime, "data": b}})
            photo_bytes.append((b, mime))
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if email in _running:
        return {"ok": False, "error": "Предыдущая картинка ещё создаётся, подождите"}
    _running.add(email)
    try:
        price = ECO_TEMPLATE_PRICE if eco else template["price"]
        credit = await check_and_deduct_credits(email, price, f"Шаблон «{template.get('title', template['id'])}»"
                                                + (" (Эконом)" if eco else ""))
        balance = credit.get("credits", 0)
        if not credit.get("success"):
            if credit.get("error"):
                return {"ok": False, "error": "Сервер кредитов не отвечает. Кредиты не списаны, попробуйте через минуту"}
            return {"ok": False, "credits": balance, "need": price,
                    "error": f"Недостаточно кредитов: нужно {price}, на балансе {balance}"}

        if template.get("formats") and aspect not in template["formats"]:
            aspect = template["formats"][0]
        try:
            prompt_text = with_aspect_hint(build_template_prompt(template, answers), aspect)
            if eco:
                text_output = ""
                image_data, mime_type = await _eco_template_image(photo_bytes, prompt_text, aspect)
                print(f"💸 Шаблон {template['id']} в «Экономе» для {email}")
            else:
                contents = [{"role": "user", "parts": photo_parts + [{"text": prompt_text}]}]
                response = await generate(MODEL_MAPPING[template["_model"]], contents, aspect)
                text_output, image_data, mime_type = parse_response(response)
        except Exception as e:
            print(f"Ошибка шаблона {template['id']}: {e}")
            image_data, text_output, mime_type = None, "", "image/jpeg"

        if not image_data:
            balance = await refund_credits(email, price, balance)
            return {"ok": False, "credits": balance,
                    "error": "Картинка не получилась. Кредиты вернула, попробуйте другое фото или ещё раз"}

        if TEMPLATES_SCRIPT_URL:  # счётчик популярности, не ждём ответа
            asyncio.create_task(asyncio.to_thread(_count_hit, template["id"]))
        return {"ok": True, "credits": balance, "image": image_data, "mimeType": mime_type, "text": text_output}
    finally:
        _running.discard(email)



# ==================== Несколько чатов (хранятся в таблице «Шаблоны НейроЯги») ====================
import re as _re

CHAT_ID_RE = _re.compile(r"^[A-Za-z0-9_-]{6,40}$")
MAX_CHAT_TEXT = 45000  # в ячейку Google-таблицы помещается до 50 000 символов


def _tscript_sync(payload: dict):
    try:
        body = dict(payload, key=TEMPLATES_KEY)
        r = requests.post(TEMPLATES_SCRIPT_URL, json=body, timeout=SCRIPT_TIMEOUT)
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"❌ Скрипт чатов ({payload.get('action')}): {e}")
        return None


async def tscript(payload: dict):
    if not TEMPLATES_SCRIPT_URL:
        return None
    return await asyncio.to_thread(_tscript_sync, payload)


@app.get("/api/chats/{email}")
async def chats_list(email: str, request: Request):
    email = email.strip()
    if not is_authorized(email, token_from_request(request)):
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    d = await tscript({"action": "chats_list", "email": email})
    if not d or not d.get("ok"):
        return {"ok": False, "error": "Не удалось загрузить чаты", "chats": []}
    return {"ok": True, "chats": d.get("chats", [])}


@app.get("/api/chats/{email}/{chat_id}")
async def chat_get(email: str, chat_id: str, request: Request):
    email = email.strip()
    if not is_authorized(email, token_from_request(request)):
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    if not CHAT_ID_RE.match(chat_id):
        return {"ok": False, "error": "Чат не найден"}
    d = await tscript({"action": "chat_get", "email": email, "chat_id": chat_id})
    if not d or not d.get("ok"):
        return {"ok": False, "error": "Не удалось открыть чат"}
    return {"ok": True, "messages": d.get("messages", [])}


@app.post("/api/chats/message")
async def chat_message(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "Неверный запрос"}
    email = str(data.get("email") or "").strip()
    chat_id = str(data.get("chat_id") or "")
    role = "user" if data.get("role") == "user" else "bot"
    text = str(data.get("text") or "")[:MAX_CHAT_TEXT]
    title = str(data.get("title") or "").strip()[:80]
    if not email or not CHAT_ID_RE.match(chat_id) or not text:
        return {"ok": False, "error": "Неверные параметры"}
    if not is_authorized(email, token_from_request(request, data)):
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    payload = {"action": "chat_add", "email": email, "chat_id": chat_id, "title": title, "role": role, "text": text}
    d = None
    for attempt in range(3):  # таблица могла быть занята другой записью — пробуем ещё раз
        d = await tscript(payload)
        if d and d.get("ok"):
            break
        await asyncio.sleep(1.5 * (attempt + 1))
    return {"ok": bool(d and d.get("ok"))}


@app.post("/api/chats/delete")
async def chat_delete(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "Неверный запрос"}
    email = str(data.get("email") or "").strip()
    chat_id = str(data.get("chat_id") or "")
    if not email or not CHAT_ID_RE.match(chat_id):
        return {"ok": False, "error": "Неверные параметры"}
    if not is_authorized(email, token_from_request(request, data)):
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    d = await tscript({"action": "chat_delete", "email": email, "chat_id": chat_id})
    return {"ok": bool(d and d.get("ok"))}


# ==================== Видео (через Братуху) ====================
# Генерация идёт в фоне: сайт запускает задачу и раз в несколько секунд спрашивает статус.

import math as _math

BRATUKHA_API_URL = os.getenv("BRATUKHA_API_URL", "https://bratuha.ru/api/v1").rstrip("/")
BRATUKHA_API_KEY = (os.getenv("BRATUKHA_API_KEY") or os.getenv("BRATUKHA_TOKEN") or "").strip()

# Цена в кредитах НейроЯги (1 кр. = 1 ₽) = цена Братухи × 2. per_sec — цена за секунду видео.
VIDEO_MODELS = {
    # photo: "required" — только с фото, "forbidden" — только по тексту, None — как угодно
    # Порядок = порядок в меню (от дешёвых к дорогим). 1 кр. = 1 ₽.
    # ---------------- картинки Братухи (цена Братухи × 3) ----------------
    "nano-banana": {"kind": "image", "tool": "nano-banana", "name": "Nano Banana", "credits": 6,
                    "desc": "Быстрые картинки и правка фото", "photo": None,
                    "aspects": ["1:1", "4:5", "3:4", "9:16", "4:3", "16:9"],
                    "qualities": [
                        {"id": "cheap", "name": "Эконом", "desc": "Дешевле, но генерирует медленнее", "credits": 3},
                        {"id": "normal", "name": "Быстро", "desc": "Обычный режим: быстро и стабильно", "credits": 6},
                    ], "quality": "normal"},
    "gpt-image-2": {"kind": "image", "tool": "gpt-image-2", "name": "GPT Image 2", "credits": 30,
                    "desc": "Точный текст на картинке, правка фото по описанию", "resolution": "1k", "cheap_first": True,
                    "photo": None, "aspects": ["1:1", "4:5", "3:4", "9:16", "4:3", "16:9"],
                    "qualities": [
                        {"id": "low", "name": "Быстро", "desc": "Черновик, простые картинки", "credits": 6},
                        {"id": "medium", "name": "Стандарт", "desc": "Хорошее качество и точный текст", "credits": 30},
                        {"id": "high", "name": "Максимум", "desc": "Самая высокая детализация", "credits": 90},
                    ], "quality": "medium"},
    "grok-image":  {"kind": "image", "tool": "grok-image", "name": "Grok Image", "credits": 12,
                    "desc": "Яркие сочные картинки и правка фото", "photo": None, "aspects": ["1:1", "9:16", "16:9"]},
    "seedream-4-5": {"kind": "image", "tool": "seedream-4-5", "name": "Seedream 4.5", "credits": 15,
                     "desc": "Детальные картинки, до 4K для печати", "photo": None, "no_webp": True,
                     "aspects": ["1:1", "3:4", "9:16", "4:3", "16:9"],
                     "qualities": [
                         {"id": "2K", "name": "2K", "desc": "Быстрее, для экрана и соцсетей", "credits": 15},
                         {"id": "4K", "name": "4K", "desc": "Высокая детализация, для печати", "credits": 15},
                     ], "quality": "2K"},
    "wan-2-7-image": {"kind": "image", "tool": "wan-2-7-image", "name": "Wan 2.7 Image", "credits": 15,
                      "desc": "Точно следует описанию, аккуратная правка фото", "photo": None,
                      "aspects": ["1:1", "3:4", "9:16", "4:3", "16:9"],
                      "qualities": [
                          {"id": "standard", "name": "Обычная", "desc": "Хорошее качество", "credits": 15},
                          {"id": "pro", "name": "Pro", "desc": "Больше деталей в портретах и фотореализме", "credits": 30},
                      ], "quality": "standard"},
    # ---------------- музыка (цена Братухи × 3) ----------------
    "suno": {"kind": "audio", "tool": "suno", "name": "Suno", "credits": 30,
             "desc": "Песни с вокалом, музыка и звуки — 2 варианта", "photo": "forbidden", "no_aspect": True,
             "qualities": [
                 {"id": "song", "name": "Песня с вокалом", "desc": "Текст и музыку напишет сама", "credits": 30},
                 {"id": "instrumental", "name": "Инструментал", "desc": "Музыка без слов", "credits": 30},
                 {"id": "sounds", "name": "Звуки и атмосфера", "desc": "Дождь, город, костёр, кафе…", "credits": 30},
             ], "quality": "song"},
    # ---------------- видео (Seedance/Pruna/Sora — цена × 2, Wan — × 3) ----------------
    # ---- Veo 3.1 (ролик всегда 8 сек) ----
    "veo-3-1-lite":    {"tool": "veo-3-1", "vmodel": "veo3.1-lite", "name": "Veo 3.1 Lite", "credits": 30, "photo": "forbidden",
                        "desc": "8 сек по описанию, самая доступная", "aspects": ["16:9", "9:16"]},
    "veo-3-1-fast":    {"tool": "veo-3-1", "vmodel": "veo3.1-fast", "name": "Veo 3.1 Fast", "credits": 60, "photo": None,
                        "desc": "8 сек со звуком, по тексту или из фото", "aspects": ["16:9", "9:16"]},
    "veo-3-1-quality": {"tool": "veo-3-1", "vmodel": "veo3.1-quality", "name": "Veo 3.1 Quality", "credits": 300, "photo": None,
                        "desc": "Максимальное качество Google, 8 сек", "aspects": ["16:9", "9:16"]},
    # ---- Wan 3.0 ----
    "wan-3-0-480":   {"tool": "wan-3-0", "resolution": "480P", "name": "Wan 3.0 480p", "credits": 15, "per_sec": True,
                      "desc": "Видео со звуком по тексту или из фото", "photo": None, "durations": [5, 10, 15], "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4"]},
    "wan-3-0-720":   {"tool": "wan-3-0", "resolution": "720P", "name": "Wan 3.0 720p", "credits": 30, "per_sec": True,
                      "desc": "То же в 720p — чётче", "photo": None, "durations": [5, 10, 15], "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4"]},
    "wan-3-0-prime-480": {"tool": "wan-3-0-video-prime", "resolution": "480P", "name": "Wan 3.0 Prime 480p", "credits": 27,
                          "per_sec": True, "desc": "Флагман Wan: лучше движение и звук", "photo": None,
                          "durations": [5, 10, 15], "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4"]},
    "wan-3-0-prime-720": {"tool": "wan-3-0-video-prime", "resolution": "720P", "name": "Wan 3.0 Prime 720p", "credits": 54,
                          "per_sec": True, "desc": "Флагман Wan в 720p", "photo": None,
                          "durations": [5, 10, 15], "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4"]},
    # ---- Seedance 2.5 ----
    "seedance-2-5-480": {"tool": "seedance-2-5", "resolution": "480p", "name": "Seedance 2.5 480p", "credits": 42, "per_sec": True,
                         "desc": "Новый Seedance со звуком, по тексту или из фото", "photo": None,
                         "durations": [4, 6, 8, 10], "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4"]},
    "seedance-2-5-720": {"tool": "seedance-2-5", "resolution": "720p", "name": "Seedance 2.5 720p", "credits": 84, "per_sec": True,
                         "desc": "Seedance 2.5 в 720p", "photo": None, "durations": [4, 6, 8, 10], "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4"]},
    # ---- Kling 2.1 (цена за ролик 5 или 10 сек) ----
    "kling-2-1-standard": {"tool": "kling-2-1", "kmodel": "kling/v2-1-standard", "name": "Kling 2.1 Standard", "credits": 75,
                           "desc": "Оживляет фото — прикрепите фото 📎", "photo": "required", "durations": [5, 10],
                           "duration_prices": {"5": 75, "10": 150}, "no_aspect": True},
    "kling-2-1-pro":      {"tool": "kling-2-1", "kmodel": "kling/v2-1-pro", "name": "Kling 2.1 Pro", "credits": 120,
                           "desc": "Оживляет фото качественнее — прикрепите фото 📎", "photo": "required", "durations": [5, 10],
                           "duration_prices": {"5": 120, "10": 270}, "no_aspect": True},
    "kling-2-1-master":   {"tool": "kling-2-1", "kmodel": "master", "name": "Kling 2.1 Master", "credits": 420,
                           "desc": "Топ-качество Kling, по тексту или из фото", "photo": None, "durations": [5, 10],
                           "duration_prices": {"5": 420, "10": 840}, "aspects": ["16:9", "9:16", "1:1"]},
    # ---- Gemini Omni (со звуком) ----
    "gemini-omni": {"tool": "gemini-omni", "name": "Gemini Omni", "credits": 180, "photo": None,
                    "desc": "Видео Google со звуком и речью, до 10 сек", "durations": [4, 6, 8, 10],
                    "duration_prices": {"4": 180, "6": 240, "8": 300, "10": 360}, "aspects": ["16:9", "9:16"]},
    # ---- Pika 2.2 (без звука) ----
    "pika-720":  {"tool": "pika", "resolution": "720p", "name": "Pika 2.2 720p", "credits": 105, "photo": None,
                  "desc": "Яркая анимация, без звука", "durations": [5, 10], "duration_prices": {"5": 105, "10": 240},
                  "aspects": ["16:9", "9:16", "1:1", "4:5"]},
    "pika-1080": {"tool": "pika", "resolution": "1080p", "name": "Pika 2.2 1080p", "credits": 210, "photo": None,
                  "desc": "Pika в 1080p, без звука", "durations": [5, 10], "duration_prices": {"5": 210, "10": 480},
                  "aspects": ["16:9", "9:16", "1:1", "4:5"]},
    "grok-video-480":         {"tool": "grok-video", "resolution": "480p", "name": "Grok Video 480p", "credits": 6,
                               "desc": "Ролики 6–30 сек по тексту или из фото", "per_sec": True, "photo": None,
                               "durations": [6, 10, 15, 20, 30], "aspects": ["16:9", "9:16", "1:1"]},
    "grok-video-720":         {"tool": "grok-video", "resolution": "720p", "name": "Grok Video 720p", "credits": 9,
                               "desc": "То же в 720p — чётче", "per_sec": True, "photo": None,
                               "durations": [6, 10, 15, 20, 30], "aspects": ["16:9", "9:16", "1:1"]},
    "seedance-1-0":           {"name": "Seedance 1.0", "desc": "Короткие ролики по описанию", "credits": 60, "photo": None},
    "seedance-2-0-apimart":   {"name": "Seedance 2.0", "desc": "Только по описанию текстом, без фото", "credits": 18,
                               "per_sec": True, "photo": "forbidden"},
    "wan-2-2-avatar":         {"name": "Wan 2.2 Avatar", "desc": "Говорящее фото: фото 📎 + голосовая запись 🎙", "credits": 18,
                               "photo": "required", "audio": True, "no_aspect": True, "max_audio_sec": 60,
                               "qualities": [
                                   {"id": "480p", "name": "480p", "desc": "Быстрее и дешевле", "credits": 18, "per_sec": True},
                                   {"id": "720p", "name": "720p", "desc": "Чётче, но в 2–3 раза дольше", "credits": 30, "per_sec": True},
                               ], "quality": "480p"},
    "sora-2":                 {"name": "Sora 2", "desc": "Видео со звуком по тексту или из фото, до 20 сек", "credits": 30,
                               "per_sec": True, "photo": None, "durations": [4, 8, 12, 16, 20], "aspects": ["9:16", "16:9"]},
    "pruna-ai-p-video-2-pro": {"name": "Pruna P-Video 2 Pro", "desc": "Оживляет фото — прикрепите фото 📎", "credits": 28,
                               "per_sec": True, "photo": "required"},
}
VIDEO_ASPECTS = ("16:9", "9:16", "1:1")
VIDEO_DURATIONS = (5, 10)
VIDEO_TIMEOUT_SEC = 15 * 60

_video_jobs = {}  # job_id -> данные задачи (живут в памяти до перезапуска сервера)


def _bh_headers():
    return {"Authorization": f"Bearer {BRATUKHA_API_KEY}", "Content-Type": "application/json"}


def _bh_upload(data: bytes, mime: str, filename: str = "input_1.jpg") -> str:
    r = requests.post(f"{BRATUKHA_API_URL}/uploads", headers=_bh_headers(), timeout=90, json={
        "filename": filename, "content_type": mime or "image/jpeg", "data": base64.b64encode(data).decode()})
    try:
        url = (r.json() or {}).get("url")
    except Exception:
        url = None
    if not url:
        raise RuntimeError(f"Братуха не приняла файл: {r.status_code} {r.text[:200]}")
    return url


AUDIO_EXT = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/m4a": "m4a",
             "audio/aac": "aac", "audio/x-aac": "aac", "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
             "audio/flac": "flac", "audio/x-flac": "flac", "audio/ogg": "ogg", "audio/opus": "opus"}


def audio_seconds(data: bytes):
    """Длительность записи в секундах (по самому файлу, а не по словам браузера)."""
    try:
        import io
        import mutagen
        f = mutagen.File(io.BytesIO(data))
        return float(f.info.length) if f and f.info and f.info.length else None
    except Exception as e:
        print(f"Не удалось прочитать аудио: {e}")
        return None


VIDEO_ORIENT = {"16:9": "Horizontal 16:9 framing.", "9:16": "Vertical 9:16 framing.", "1:1": "Square 1:1 framing."}


VIDEO_TEXT_WORDS = _re.compile(r"надпис|текст|написа|подпис|титр|слоган|логотип", _re.I)
VIDEO_VOICE_WORDS = _re.compile(r"говор|скаж|произн|озвуч|голос|поёт|поет|песн|диалог|реплик|кричит|шепч", _re.I)
VIDEO_QUOTES = _re.compile(r"[«\"“„']([^«»\"“”„']{1,120})[»\"”']")
VIDEO_ORIENT_RU = {"16:9": "Горизонтальное видео 16:9.", "9:16": "Вертикальное видео 9:16.", "1:1": "Квадратное видео 1:1."}


def _video_text_rule(prompt: str) -> str:
    """Надписи в кадре — только если пользователь сам их попросил."""
    quoted = [q.strip() for q in VIDEO_QUOTES.findall(prompt or "") if q.strip()]
    if quoted and VIDEO_TEXT_WORDS.search(prompt or "") or quoted and not VIDEO_VOICE_WORDS.search(prompt or ""):
        return "На экране крупная чёткая надпись ровно такая: " + "; ".join(f"«{q}»" for q in quoted[:3]) + ". Других надписей нет."
    if VIDEO_TEXT_WORDS.search(prompt or ""):
        return "Надпись на экране сделай так, как описано выше, крупно и чётко. Других надписей и субтитров нет."
    return "Без надписей, букв, субтитров, логотипов и водяных знаков на экране."


def _video_sound_rule(prompt: str) -> str:
    if VIDEO_VOICE_WORDS.search(prompt or ""):
        return "Звук: речь и голоса только те, что описаны выше, плюс естественные звуки сцены."
    return "Звук: только атмосферная музыка и естественные звуки сцены, без речи и без закадрового голоса."


def _video_prompt(prompt: str, has_image: bool, aspect: str) -> str:
    """Естественное описание сцены по-русски: модели со звуком не должны озвучивать служебный текст."""
    p = (prompt or "").strip().rstrip(".")
    if has_image:  # «оживи картинку и …» → «…», чтобы не было «Оживи изображение: оживи картинку»
        p = _re.sub(r"^\s*оживи(те)?\s+(эту\s+|мою\s+)?(картинку|изображение|фото|фотографию|фотку)\s*(,|и\s+)?\s*", "", p, flags=_re.I).strip()
    if has_image:
        parts = [f"Оживи изображение: {p}." if p else "Оживи изображение.",
                 "Сохрани тех же людей, их лица, предметы, композицию, цвета и стиль исходной картинки.",
                 "Плавное естественное движение, кинематографичная камера."]
    else:
        parts = [f"{p}.", "Плавное кинематографичное видео, детальная картинка."]
    parts += [VIDEO_ORIENT_RU.get(aspect, ""), _video_text_rule(p), _video_sound_rule(p)]
    return " ".join(x for x in parts if x)


def _bh_create_image(slug: str, prompt: str, image_url, aspect: str, mode: str, quality: str):
    m = VIDEO_MODELS[slug]
    text = (prompt or "").strip().rstrip(".") + "."
    quoted = [q.strip() for q in VIDEO_QUOTES.findall(prompt or "") if q.strip()]
    if quoted:
        text += " Текст на изображении — дословно, без ошибок: " + "; ".join(f"«{q}»" for q in quoted[:3]) + "."
    elif not VIDEO_TEXT_WORDS.search(prompt or ""):
        text += " Без лишних надписей и водяных знаков."
    if image_url:
        text = "Используй приложенное изображение: " + text + " Сохрани лица и узнаваемые детали с исходного фото."
    tool = m.get("tool", slug)
    if tool == "nano-banana":
        inp = {"mode": quality or "normal", "prompt": text[:20000], "image_size": aspect or "auto"}
    elif tool == "seedream-4-5":
        inp = {"prompt": text[:3000], "size": aspect or "auto", "resolution": quality or "2K", "n": 1}
    elif tool == "wan-2-7-image":
        inp = {"prompt": text[:5000], "tier": quality or "standard", "aspect_ratio": aspect or "auto"}
        if image_url:
            inp["input_urls"] = [image_url]
            image_url = None
    elif tool == "grok-image":
        inp = {"prompt": text[:5000]}
        if not image_url and aspect:
            inp["aspect_ratio"] = aspect
    else:  # gpt-image-2
        inp = {"prompt": text[:20000], "quality": quality or m.get("quality", "medium"), "size": aspect or "auto",
               "resolution": m["resolution"], "mode": mode}
    if image_url:
        inp["image_urls"] = [image_url]
    return _bh_post_operation(tool, inp)


def _bh_create_avatar(image_url: str, audio_url: str, prompt: str, resolution: str):
    inp = {"image": image_url, "audio": audio_url, "resolution": resolution or "480p"}
    if prompt:
        inp["prompt"] = prompt[:2000]
    return _bh_post_operation("wan-2-2-avatar", inp)


def _bh_create_music(prompt: str, kind: str):
    if kind == "sounds":
        inp = {"suno_generation_type": "sounds", "sounds_model": "V6", "sounds_prompt": prompt[:500]}
    else:
        inp = {"suno_generation_type": "create_track", "customMode": "false", "suno_model": "V6",
               "instrumental": "true" if kind == "instrumental" else "false", "prompt_simple": prompt[:3000]}
    return _bh_post_operation("suno", inp)


def _bh_create(slug: str, prompt: str, image_url, aspect: str, duration):
    full = _video_prompt(prompt, bool(image_url), aspect)
    m = VIDEO_MODELS[slug]
    tool = m.get("tool", slug)
    asp = aspect or "16:9"
    if tool in ("wan-3-0", "wan-3-0-video-prime"):
        prime = tool == "wan-3-0-video-prime"
        tkey = "wan_3_0_prime_generation_type" if prime else "wan_3_0_generation_type"
        inp = {"resolution": m["resolution"], "duration": int(duration or 5), "audio": True}
        inp["aspect_ratio" if prime else "size"] = "adaptive" if image_url else asp
        if image_url:
            inp.update({tkey: "first_frame", "frame_prompt": full[:20000], "first_frame_url": image_url})
        else:
            inp.update({tkey: "text", "text_prompt": full[:20000]})
        return _bh_post_operation(tool, inp)
    if tool == "seedance-2-5":
        inp = {"seedance_2_5_generation_type": "image" if image_url else "text", "prompt": full[:20000],
               "size": "adaptive" if image_url else asp, "resolution": m["resolution"], "duration": int(duration or 4),
               "generate_audio": True, "watermark": False, "draft_mode": "off", "output_format": "mp4"}
        if image_url:
            inp["image_urls"] = [image_url]
        return _bh_post_operation(tool, inp)
    if tool == "kling-2-1":
        km = m["kmodel"]
        if km == "master":
            km = "kling/v2-1-master-image-to-video" if image_url else "kling/v2-1-master-text-to-video"
        inp = {"mode": "image" if image_url else "text", "model": km, "prompt": full[:5000],
               "duration": str(duration or 5), "cfg_scale": 0.5, "negative_prompt": "blur, low quality, artifacts, text, watermark"}
        if image_url:
            inp["image_url"] = image_url
        else:
            inp["aspect_ratio"] = asp
        return _bh_post_operation(tool, inp)
    if tool == "veo-3-1":
        inp = {"generation_type": "image" if image_url else "text", "model": m["vmodel"], "prompt": full[:4000],
               "aspect_ratio": asp if asp in ("16:9", "9:16") else "16:9", "resolution": "1080p"}
        if image_url:
            inp["image_urls"] = image_url  # у Veo одна ссылка строкой
        return _bh_post_operation(tool, inp)
    if tool == "gemini-omni":
        inp = {"generation_type": "video", "prompt": full[:4000], "duration": str(duration or 4),
               "aspect_ratio": asp if asp in ("16:9", "9:16") else "16:9", "resolution": "1080p"}
        if image_url:
            inp["image_urls"] = [image_url]
        return _bh_post_operation(tool, inp)
    if tool == "pika":
        inp = {"pika_generation_type": "image_to_video" if image_url else "text_to_video", "prompt": full[:4000],
               "resolution": m["resolution"], "duration_seconds_mode": str(duration or 5)}
        if image_url:
            inp["image_url"] = image_url
        else:
            inp["aspect_ratio"] = asp
        return _bh_post_operation(tool, inp)
    if m.get("tool") == "grok-video":
        inp = {"model": "grok-imagine/image-to-video" if image_url else "grok-imagine/text-to-video",
               "prompt": full[:5000], "duration": int(duration or 6), "resolution": m.get("resolution", "480p")}
        if image_url:
            inp["image_urls"] = [image_url]
        else:
            inp["aspect_ratio"] = aspect or "16:9"
        return _bh_post_operation("grok-video", inp)
    if slug == "sora-2":
        inp = {"sora_ws_generation_type": "image" if image_url else "text", "sora_ws_model_tier": "standard",
               "prompt": full[:5000], "duration": str(duration or 4)}
        if image_url:
            inp["image"] = image_url
        else:
            inp["size_std"] = "720*1280" if aspect == "9:16" else "1280*720"
        return _bh_post_operation(slug, inp)
    inp = {"prompt": full, "text": full}
    if aspect:
        inp.update(aspect_ratio=aspect, ratio=aspect, ar=aspect, size=aspect)
    if duration:
        inp.update(duration=duration, seconds=duration)
    if image_url:
        inp.update(image=image_url, image_url=image_url, images=[image_url], file_url=image_url, files=[image_url])
    return _bh_post_operation(slug, inp)


def _bh_post_operation(slug: str, inp: dict):
    r = requests.post(f"{BRATUKHA_API_URL}/operations", headers=_bh_headers(), timeout=30, json={"tool": slug, "input": inp})
    try:
        d = r.json()
    except Exception:
        d = {}
    if r.status_code >= 400:
        err = d.get("error") if isinstance(d.get("error"), dict) else {}
        raise RuntimeError(err.get("message") or d.get("message") or f"Ошибка API: статус {r.status_code}")
    op_id = d.get("id") or d.get("operation_id")
    if not op_id:
        raise RuntimeError("Братуха не вернула номер операции")
    return op_id


def _bh_status(op_id: str) -> dict:
    r = requests.get(f"{BRATUKHA_API_URL}/operations/{op_id}", headers=_bh_headers(), timeout=20)
    if r.status_code in (429, 503):
        return {"status": "busy"}
    return r.json()


def _bh_video_url(d: dict):
    res = d.get("result") or d
    if isinstance(res, str):
        return res if res.startswith("http") else None
    urls = res.get("urls") or res.get("videos") or res.get("files") or res.get("images") or []
    first = urls[0] if isinstance(urls, list) and urls else None
    if isinstance(first, dict):
        first = first.get("url")
    return first or res.get("video_url") or res.get("url")


def _video_durations(slug: str):
    return VIDEO_MODELS[slug].get("durations") or list(VIDEO_DURATIONS)


def _video_aspects(slug: str):
    return VIDEO_MODELS[slug].get("aspects") or list(VIDEO_ASPECTS)


def _video_quality(slug: str, q):
    qs = VIDEO_MODELS[slug].get("qualities") or []
    return next((x for x in qs if x["id"] == q), None) or next((x for x in qs if x["id"] == VIDEO_MODELS[slug].get("quality")), None)


def _video_cost(slug: str, duration, quality=None) -> int:
    m = VIDEO_MODELS[slug]
    if m.get("duration_prices"):
        d = str(duration or _video_durations(slug)[0])
        return int(m["duration_prices"].get(d) or min(m["duration_prices"].values()))
    if m.get("qualities"):
        q = _video_quality(slug, quality)
        return q["credits"] * int(duration or 1) if q.get("per_sec") else q["credits"]
    return m["credits"] * (duration or _video_durations(slug)[0]) if m.get("per_sec") else m["credits"]


def _bh_all_urls(d: dict) -> list:
    res = d.get("result") or {}
    if isinstance(res, str):
        return [res] if res.startswith("http") else []
    out = []
    for key in ("urls", "videos", "images", "files"):
        for x in res.get(key) or []:
            u = x.get("url") if isinstance(x, dict) else x
            if isinstance(u, str) and u.startswith("http") and u not in out:
                out.append(u)
    for key in ("video_url", "image_url", "audio_url", "url"):
        u = res.get(key)
        if isinstance(u, str) and u.startswith("http") and u not in out:
            out.append(u)
    return out


async def _bh_wait(op_id: str, timeout: int, every: int) -> str:
    return (await _bh_wait_all(op_id, timeout, every))[0]


async def _bh_wait_all(op_id: str, timeout: int, every: int) -> list:
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(every)
        try:
            d = await asyncio.to_thread(_bh_status, op_id)
        except Exception:
            continue
        st = d.get("status")
        if st == "completed":
            urls = _bh_all_urls(d) or ([_bh_video_url(d)] if _bh_video_url(d) else [])
            if not urls:
                raise RuntimeError("результат готов, но ссылки на него нет")
            return urls
        if st == "failed":
            err = d.get("error") if isinstance(d.get("error"), dict) else {}
            raise RuntimeError(d.get("error_message") or err.get("message") or "нейросеть не смогла выполнить задачу")
    raise RuntimeError("слишком долгое ожидание")


async def _video_worker(job_id: str):
    job = _video_jobs[job_id]
    try:
        image_url = None
        if job.get("image"):
            ext = "png" if "png" in str(job.get("mime")) else "webp" if "webp" in str(job.get("mime")) else "jpg"
            image_url = await asyncio.to_thread(_bh_upload, job["image"], job.get("mime"), f"input_1.{ext}")
        m = VIDEO_MODELS[job["model"]]
        if m.get("kind") == "audio":
            op_id = await asyncio.to_thread(_bh_create_music, job["prompt"], job.get("quality") or "song")
            print(f"🎵 suno ({job.get('quality')}) для {job['email']}: операция {op_id}")
            urls = await _bh_wait_all(op_id, 8 * 60, 8)
            job.update(status="done", url=urls[0], urls=urls)
            print(f"✅ suno готово для {job['email']}")
            return
        if m.get("audio"):
            ext = AUDIO_EXT.get(job.get("audio_mime"), "mp3")
            audio_url = await asyncio.to_thread(_bh_upload, job["audio"], job.get("audio_mime"), f"voice.{ext}")
            op_id = await asyncio.to_thread(_bh_create_avatar, image_url, audio_url, job.get("prompt"), job.get("quality"))
            print(f"🎙 {job['model']} ({job.get('quality')}, {job.get('duration')} сек) для {job['email']}: операция {op_id}")
            url = await _bh_wait(op_id, 25 * 60, 10)
            job.update(status="done", url=url)
            print(f"✅ {job['model']} готово для {job['email']}")
            return
        is_image = m.get("kind") == "image"
        cheap = m.get("cheap_first") and job.get("quality") != "low"  # у low цена одинаковая — сразу обычный режим
        modes = (["cheap", "normal"] if cheap else (["normal"] if m.get("cheap_first") else [None])) if is_image else [None]
        last_err = None
        for mode in modes:
            try:
                if is_image:
                    op_id = await asyncio.to_thread(_bh_create_image, job["model"], job["prompt"], image_url, job.get("aspect"), mode,
                                                    job.get("quality"))
                else:
                    op_id = await asyncio.to_thread(_bh_create, job["model"], job["prompt"], image_url, job.get("aspect"), job.get("duration"))
                print(f"🎬 {job['model']}{' (' + mode + ')' if mode else ''} для {job['email']}: операция {op_id}")
                url = await _bh_wait(op_id, 5 * 60 if is_image else VIDEO_TIMEOUT_SEC, 3 if is_image else 5)
                job.update(status="done", url=url)
                print(f"✅ {job['model']} готово для {job['email']}")
                return
            except Exception as e:
                last_err = e
                if mode == "cheap" and "модерац" not in str(e).lower():
                    print(f"⚠️ {job['model']} в дешёвом режиме не вышло ({e}), пробую обычный")
                    continue
                raise
        raise last_err or RuntimeError("не получилось")
    except Exception as e:
        print(f"❌ {job['model']} для {job['email']} не получилось: {e}")
        balance = await refund_credits(job["email"], job["cost"], job.get("balance", 0))
        what = "Картинка" if VIDEO_MODELS[job["model"]].get("kind") == "image" else "Видео"
        job.update(status="error", credits=balance,
                   error=f"{what} не получилось ({str(e)[:160]}). Кредиты вернула, попробуйте изменить описание.")
    finally:
        job.pop("image", None)
        job.pop("audio", None)


def _bh_catalog():
    """Пробуем найти у Братухи список инструментов по распространённым адресам."""
    for path in ("/tools", "/models", "/catalog", "/tools/list"):
        try:
            r = requests.get(f"{BRATUKHA_API_URL}{path}", headers=_bh_headers(), timeout=20)
            if r.status_code == 200:
                d = r.json()
                items = d.get("data") or d.get("tools") or d.get("models") or d.get("items") if isinstance(d, dict) else d
                if isinstance(items, list) and items:
                    out = []
                    for it in items:
                        if isinstance(it, dict):
                            out.append({k: it.get(k) for k in ("slug", "id", "name", "title", "category", "type", "price", "unit") if it.get(k) is not None})
                        else:
                            out.append(it)
                    return {"ok": True, "source": path, "count": len(out), "tools": out}
        except Exception as e:
            print(f"Каталог Братухи {path}: {e}")
    return {"ok": False, "error": "Братуха не отдала список инструментов ни по одному из адресов"}


@app.get("/api/video/catalog")
async def video_catalog(key: str = ""):
    # служебная страница: открыть с ?key=<TEMPLATES_KEY>
    if not TEMPLATES_KEY or key != TEMPLATES_KEY:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    if not BRATUKHA_API_KEY:
        return {"ok": False, "error": "BRATUKHA_API_KEY не задан"}
    data = await asyncio.to_thread(_bh_catalog)
    if data.get("ok"):
        vids = [t for t in data["tools"] if isinstance(t, dict) and "video" in str(t.get("category") or t.get("type") or "").lower()]
        if vids:
            data["video_tools"] = vids
    return data


def _min_price(slug: str) -> int:
    """Минимальная цена одного запуска — для сортировки меню."""
    m = VIDEO_MODELS[slug]
    if m.get("duration_prices"):
        return min(int(v) for v in m["duration_prices"].values())
    if m.get("qualities"):
        q = min(m["qualities"], key=lambda x: x["credits"])
        return q["credits"] * (5 if q.get("per_sec") else 1)
    if m.get("per_sec"):
        return m["credits"] * _video_durations(slug)[0]
    return m["credits"]


@app.get("/api/video/models")
async def video_models():
    return {"ok": True, "enabled": bool(BRATUKHA_API_KEY), "aspects": list(VIDEO_ASPECTS), "durations": list(VIDEO_DURATIONS),
            "models": sorted([dict(id=k, **v, min_price=_min_price(k)) for k, v in VIDEO_MODELS.items()],
                             key=lambda x: x["min_price"]) if BRATUKHA_API_KEY else []}


@app.post("/api/video/start")
async def video_start(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "Неверный запрос"}
    email = str(data.get("email") or "").strip()
    if not email:
        return JSONResponse({"ok": False, "error": "Войдите в аккаунт"}, status_code=401)
    if not is_authorized(email, token_from_request(request, data)):
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    if not BRATUKHA_API_KEY:
        return {"ok": False, "error": "Видео пока не подключено"}

    slug = str(data.get("model") or "")
    if slug not in VIDEO_MODELS:
        return {"ok": False, "error": "Неизвестная видеомодель"}
    prompt = str(data.get("prompt") or "").strip()[:2000]
    aspects = _video_aspects(slug)
    aspect = None if VIDEO_MODELS[slug].get("no_aspect") else (data.get("aspect") if data.get("aspect") in aspects else aspects[0])
    duration = None
    audio, audio_mime = None, None
    if VIDEO_MODELS[slug].get("audio"):
        if not data.get("audio"):
            return {"ok": False, "error": "Прикрепите голосовую запись 🎙 — человек на фото скажет её"}
        try:
            audio, audio_mime = decode_file({"data": data.get("audio")})
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if audio_mime not in AUDIO_EXT:
            return {"ok": False, "error": "Этот формат записи не подходит. Подойдут mp3, m4a, wav, aac, ogg, opus, flac"}
        if len(audio) > 10 * 1024 * 1024:
            return {"ok": False, "error": "Запись больше 10 МБ"}
        secs = audio_seconds(audio)
        if not secs:
            return {"ok": False, "error": "Не получилось прочитать запись. Попробуйте mp3 или m4a"}
        limit = VIDEO_MODELS[slug].get("max_audio_sec", 60)
        if secs > limit:
            return {"ok": False, "error": f"Запись длиннее {limit} секунд. Обрежьте её"}
        duration = max(1, _math.ceil(secs))
    elif VIDEO_MODELS[slug].get("per_sec") or VIDEO_MODELS[slug].get("duration_prices"):
        durs = _video_durations(slug)
        try:
            duration = int(data.get("duration") or durs[0])
        except (TypeError, ValueError):
            duration = durs[0]
        if duration not in durs:
            duration = durs[0]

    image, mime = None, None
    if data.get("image"):
        try:
            image, mime = decode_file({"data": data.get("image")})
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not str(mime).startswith("image/"):
            return {"ok": False, "error": "Для видео можно прикрепить только фото"}
        if len(image) > 10 * 1024 * 1024:
            return {"ok": False, "error": "Фото больше 10 МБ"}
    photo_rule = VIDEO_MODELS[slug].get("photo")
    if photo_rule == "required" and not image:
        return {"ok": False, "error": f"Для {VIDEO_MODELS[slug]['name']} нужно прикрепить фото 📎"}
    if photo_rule == "forbidden" and image:
        return {"ok": False, "error": f"{VIDEO_MODELS[slug]['name']} делает видео только по описанию, без фото"}
    kind = VIDEO_MODELS[slug].get("kind", "video")
    if kind == "audio" and len(prompt) < 3:
        return {"ok": False, "error": "Опишите песню или звук: о чём, жанр, настроение"}
    if VIDEO_MODELS[slug].get("no_webp") and image and "webp" in str(mime):
        return {"ok": False, "error": f"Для {VIDEO_MODELS[slug]['name']} подойдёт фото JPG или PNG"}
    if kind == "image" and len(prompt) < 3:
        return {"ok": False, "error": "Опишите, какую картинку сделать или что изменить на фото"}
    if not prompt and not image:
        return {"ok": False, "error": "Опишите, что должно происходить в видео"}
    if not prompt and not VIDEO_MODELS[slug].get("audio"):
        prompt = "мягкое естественное движение, лёгкое движение камеры"

    now = time.time()
    for jid in [j for j, v in _video_jobs.items() if now - v["created"] > 3 * 3600]:
        _video_jobs.pop(jid, None)
    if any(v["email"] == email and v["status"] == "running" and VIDEO_MODELS[v["model"]].get("kind", "video") == kind
           for v in _video_jobs.values()):
        return {"ok": False, "error": "Предыдущая задача ещё выполняется, дождитесь её"}

    quality = None
    if VIDEO_MODELS[slug].get("qualities"):
        quality = _video_quality(slug, data.get("quality"))["id"]
    cost = _video_cost(slug, duration, quality)
    credit = await check_and_deduct_credits(email, cost, VIDEO_MODELS[slug]["name"] + (f", {duration} сек" if duration else ""))
    if not credit.get("success"):
        if credit.get("error"):
            return {"ok": False, "error": "Сервер кредитов не отвечает. Кредиты не списаны"}
        return {"ok": False, "need": cost, "credits": credit.get("credits", 0),
                "error": f"Недостаточно кредитов: нужно {cost}, на балансе {credit.get('credits', 0)}"}

    job_id = uuid.uuid4().hex
    _video_jobs[job_id] = {"email": email, "model": slug, "prompt": prompt, "aspect": aspect, "duration": duration,
                           "image": image, "mime": mime, "audio": audio, "audio_mime": audio_mime, "cost": cost, "quality": quality, "balance": credit.get("credits", 0),
                           "status": "running", "created": now}
    asyncio.create_task(_video_worker(job_id))
    return {"ok": True, "job_id": job_id, "credits": credit.get("credits", 0), "cost": cost, "seconds": duration}


@app.get("/api/video/status/{job_id}")
async def video_status(job_id: str, request: Request):
    job = _video_jobs.get(job_id)
    email = str(request.query_params.get("email") or "").strip()
    if not job or job["email"] != email:
        return {"ok": False, "status": "lost", "error": "Задача не найдена. Возможно, сервер перезапускался — если кредиты списались, напишите в поддержку"}
    if not is_authorized(email, token_from_request(request)):
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    out = {"ok": True, "status": job["status"]}
    if job["status"] == "done":
        out["url"] = job["url"]
        out["urls"] = job.get("urls") or [job["url"]]
    elif job["status"] == "error":
        out.update(error=job["error"], credits=job.get("credits"))
    return out


# ==================== Скачивание готовых файлов ====================
# Браузер не даёт скачивать файлы с чужого сайта напрямую, поэтому отдаём их через свой сервер
# с заголовком «скачать». Разрешены только хранилища Братухи, чтобы сервер не стал открытым прокси.
from urllib.parse import urlparse as _urlparse
from fastapi.responses import StreamingResponse

FILE_HOSTS = ("bratuha.ru", "selstorage.ru")


@app.get("/api/file")
async def download_file(url: str, name: str = "neyroyaga"):
    u = _urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in FILE_HOSTS):
        return JSONResponse({"ok": False, "error": "Этот файл нельзя скачать"}, status_code=403)
    try:
        r = await asyncio.to_thread(requests.get, url, stream=True, timeout=60)
    except Exception:
        return JSONResponse({"ok": False, "error": "Файл недоступен"}, status_code=502)
    if r.status_code != 200:
        return JSONResponse({"ok": False, "error": "Файл не найден или срок хранения истёк"}, status_code=404)
    safe = _re.sub(r"[^\w.\-]", "_", name)[:80] or "neyroyaga"
    headers = {"Content-Disposition": f'attachment; filename="{safe}"'}
    if r.headers.get("content-length"):
        headers["Content-Length"] = r.headers["content-length"]

    def chunks():
        try:
            for part in r.iter_content(64 * 1024):
                if part:
                    yield part
        finally:
            r.close()

    return StreamingResponse(chunks(), media_type=r.headers.get("content-type", "application/octet-stream"), headers=headers)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)