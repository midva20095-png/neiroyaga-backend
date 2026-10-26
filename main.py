import os
import json
import time
import hmac
import base64
import uuid
import asyncio
import hashlib
import binascii

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

CREDIT_COSTS = {
    'flash': 1,
    'flash_25': 1,
    'pro': 3,
    'nanobanana': 5,
    'nanobanana_pro': 10
}

IMAGE_MODELS = {'nanobanana', 'nanobanana_pro'}
BIZ_TOOL_COST = 6

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
        return hmac.compare_digest(sig, expected) and t_email == email and int(exp) > time.time()
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


async def check_and_deduct_credits(email: str, cost: int) -> dict:
    if not GOOGLE_SCRIPT_URL:
        return {"success": True, "credits": 999, "error": False}
    res = await script_call({"action": "deduct", "email": email, "cost": cost})
    if res is None:
        return {"success": False, "credits": 0, "error": True}
    return {"success": bool(res.get("success", False)), "credits": res.get("credits", 0), "error": False}


async def refund_credits(email: str, amount: int, balance_before: int) -> int:
    """Возвращает кредиты за неудачный запрос. Отдаёт новый баланс для показа."""
    if not GOOGLE_SCRIPT_URL or amount <= 0:
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

            credit_result = await check_and_deduct_credits(email, required_credits)
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

    if not GOOGLE_SCRIPT_URL:
        result = {"success": True}
    else:
        result = await script_call({"action": "auth", "email": email, "password": password})
        if result is None:
            return {"success": False, "error": "Ошибка сервера авторизации"}

    if result.get("success") and APP_SECRET:
        result["token"] = make_token(email)
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

        if not email or credits_to_add <= 0 or not GOOGLE_SCRIPT_URL:
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


def build_template_prompt(template: dict, answers: dict) -> str:
    prompt = template["_prompt"]
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


@app.get("/api/templates")
async def get_templates():
    if not TEMPLATES_SCRIPT_URL:
        return {"ok": False, "error": "Шаблоны ещё не подключены", "templates": []}
    items = await load_templates()
    if not items:
        return {"ok": False, "error": "Не удалось загрузить шаблоны, попробуйте позже", "templates": []}
    return {"ok": True, "templates": [public_template(t) for t in items]}


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

    photo_parts = []
    for p in photos_raw[:MAX_TEMPLATE_PHOTOS]:
        try:
            b, mime = decode_file({"data": p})
            photo_parts.append({"inline_data": {"mime_type": mime, "data": b}})
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if email in _running:
        return {"ok": False, "error": "Предыдущая картинка ещё создаётся, подождите"}
    _running.add(email)
    try:
        price = template["price"]
        credit = await check_and_deduct_credits(email, price)
        balance = credit.get("credits", 0)
        if not credit.get("success"):
            if credit.get("error"):
                return {"ok": False, "error": "Сервер кредитов не отвечает. Кредиты не списаны, попробуйте через минуту"}
            return {"ok": False, "credits": balance, "need": price,
                    "error": f"Недостаточно кредитов: нужно {price}, на балансе {balance}"}

        try:
            prompt_text = with_aspect_hint(build_template_prompt(template, answers), aspect)
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
    d = await tscript({"action": "chat_add", "email": email, "chat_id": chat_id, "title": title,
                       "role": role, "text": text})
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

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)