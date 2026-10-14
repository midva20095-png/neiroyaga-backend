import os
import json
import base64
import uuid
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from requests.auth import HTTPBasicAuth
from google import genai
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL")
TEMPLATES_SCRIPT_URL = os.getenv("TEMPLATES_SCRIPT_URL")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

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

def get_user_balance(email: str) -> int:
    if not GOOGLE_SCRIPT_URL:
        return 999
    try:
        response = requests.post(GOOGLE_SCRIPT_URL, json={"action": "balance", "email": email}, timeout=25)
        if response.status_code != 200:
            return 0
        return response.json().get("credits", 0)
    except Exception as e:
        print(f"❌ Ошибка получения баланса: {e}")
        return 0

def check_and_deduct_credits(email: str, cost: int) -> dict:
    if not GOOGLE_SCRIPT_URL:
        return {"success": True, "credits": 999}
    try:
        payload = {"action": "deduct", "email": email, "cost": cost}
        response = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=25)
        if response.status_code != 200:
            return {"success": False, "credits": 0}
        res_data = response.json()
        return {
            "success": res_data.get("success", False),
            "credits": res_data.get("credits", 0)
        }
    except Exception as e:
        print(f"❌ Ошибка списания кредитов: {e}")
        return {"success": False, "credits": 0}

def save_message_to_sheet(email: str, role: str, text: str, image: str = None):
    if not GOOGLE_SCRIPT_URL:
        return
    try:
        requests.post(GOOGLE_SCRIPT_URL, json={
            "action": "save_message",
            "email": email,
            "role": role,
            "text": text,
            "image": image
        }, timeout=15)
    except Exception as e:
        print(f"❌ Ошибка сохранения сообщения: {e}")

def get_chat_history_for_gemini(email: str):
    if not GOOGLE_SCRIPT_URL:
        return []
    try:
        response = requests.post(GOOGLE_SCRIPT_URL, json={"action": "get_history", "email": email}, timeout=25)
        if response.status_code != 200:
            return []
        data = response.json()
        messages = data.get("messages", [])
        
        history = []
        for msg in messages:
            role = "user" if msg.get("role") == "user" else "model"
            text = msg.get("text", "")
            if text:
                history.append({
                    "role": role,
                    "parts": [text]
                })
        return history
    except Exception as e:
        print(f"❌ Ошибка загрузки контекста истории: {e}")
        return []

def get_template_formula(template_id: str) -> str:
    if not TEMPLATES_SCRIPT_URL or not template_id:
        return ""
    try:
        response = requests.post(TEMPLATES_SCRIPT_URL, json={"action": "get_templates"}, timeout=15)
        if response.status_code == 200:
            data = response.json()
            for tpl in data.get("templates", []):
                if tpl.get("id") == template_id:
                    return tpl.get("template", "")
    except Exception as e:
        print(f"❌ Ошибка получения шаблона {template_id}: {e}")
    return ""

@app.get("/api/templates")
async def get_templates():
    if not TEMPLATES_SCRIPT_URL:
        return {"success": True, "templates": []}
    try:
        response = requests.post(TEMPLATES_SCRIPT_URL, json={"action": "get_templates"}, timeout=15)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"❌ Ошибка загрузки шаблонов из таблицы: {e}")
    return {"success": True, "templates": []}

@app.websocket("/ws/{client_email}")
async def websocket_endpoint(websocket: WebSocket, client_email: str):
    await websocket.accept()
    print(f"Клиент {client_email} успешно подключился!")
    
    initial_balance = get_user_balance(client_email)
    
    await websocket.send_text(json.dumps({
        "type": "text",
        "credits": initial_balance,
        "text": "🧚‍♀️ Приветствую в НейроЯге! Выбирай модель или шаблон в меню сверху, задавай вопросы или прикрепляй фото."
    }))

    try:
        while True:
            raw_message = await websocket.receive_text()
            
            prompt = raw_message
            model_key = 'flash'
            template_id = None
            params = {}
            file_obj = None
            
            try:
                data = json.loads(raw_message)
                if isinstance(data, dict):
                    prompt = data.get('prompt', '')
                    model_key = data.get('modelKey', 'flash')
                    template_id = data.get('template_id')
                    params = data.get('params', {})
                    file_obj = data.get('file')
            except json.JSONDecodeError:
                pass 
            
            # Если выбран шаблон, подгружаем формулу и подставляем параметры
            final_prompt = prompt
            if template_id:
                model_key = 'nanobanana' # По умолчанию для шаблонов используется генерация картинок
                template_formula = get_template_formula(template_id)
                if template_formula:
                    final_prompt = template_formula
                    # Заменяем плейсхолдеры вроде {{name}}, {{gender}}, {{zodiac}} на реальные значения
                    for key, val in params.items():
                        final_prompt = final_prompt.replace(f"{{{{{key}}}}}", str(val))
                    if prompt:
                        final_prompt += f", additional notes: {prompt}"

            resolved_model = MODEL_MAPPING.get(model_key, 'gemini-3.8-flash')
            required_credits = CREDIT_COSTS.get(model_key, 1)
            
            credit_result = check_and_deduct_credits(client_email, required_credits)
            can_proceed = credit_result.get("success", False)
            current_balance = credit_result.get("credits", 0)

            if not can_proceed:
                error_payload = json.dumps({
                    "type": "text", 
                    "credits": current_balance,
                    "text": f"💎 Недостаточно кредитов! Требуется: {required_credits} кр., а на балансе: {current_balance} кр. Пополните баланс."
                })
                await websocket.send_text(error_payload)
                continue
            
            if model_key in ['nanobanana', 'nanobanana_pro']:
                loading_payload = json.dumps({
                    "type": "text", 
                    "credits": current_balance,
                    "text": "🎨 НейроЯга колдует над вашим изображением по шаблону, подождите пару секунд..."
                })
                await websocket.send_text(loading_payload)
            
            try:
                save_message_to_sheet(client_email, "user", final_prompt, None)
                previous_history = get_chat_history_for_gemini(client_email)

                config_params = {
                    "system_instruction": "Ты — НейроЯга, премиальный искусственный интеллект. Отвечай с легким сказочным вайбом, но четко и по делу. Помни контекст беседы."
                }

                chat = client.chats.create(
                    model=resolved_model,
                    history=previous_history,
                    config=config_params
                )
                
                message_content = []
                if file_obj and file_obj.get('data'):
                    file_bytes = base64.b64decode(file_obj['data'])
                    message_content.append({
                        "inline_data": {
                            "mime_type": file_obj.get('mimeType', 'image/jpeg'),
                            "data": file_bytes
                        }
                    })
                
                if final_prompt:
                    message_content.append(final_prompt)
                elif not message_content:
                    message_content.append("Сгенерируй изображение по заданным параметрам.")

                response = chat.send_message(message_content)
                
                text_output = ""
                image_data = None
                mime_type = "image/jpeg"
                
                candidate = response.candidates[0] if response.candidates else None
                if candidate and candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if getattr(part, 'text', None):
                            text_output += ("\n" if text_output else "") + part.text
                        
                        inline = getattr(part, 'inline_data', None) or getattr(part, 'inlineData', None)
                        if inline and getattr(inline, 'data', None):
                            raw_bytes = inline.data
                            if isinstance(raw_bytes, bytes):
                                image_data = base64.b64encode(raw_bytes).decode('utf-8')
                            else:
                                image_data = str(raw_bytes)
                                
                            mime_type = getattr(inline, 'mime_type', None) or getattr(inline, 'mimeType', None) or "image/jpeg"
                
                bot_saved_text = text_output if text_output else (f"🎨 Твой шедевр готов! (-{required_credits} кр.)" if image_data else "Туман скрывает ответ...")
                save_message_to_sheet(client_email, "bot", bot_saved_text, image_data)

                if image_data:
                    response_payload = {
                        "type": "image",
                        "data": image_data,
                        "mimeType": mime_type,
                        "credits": current_balance,
                        "text": bot_saved_text
                    }
                else:
                    response_payload = {
                        "type": "text",
                        "credits": current_balance,
                        "text": bot_saved_text
                    }
                
                await websocket.send_text(json.dumps(response_payload))

            except Exception as e:
                err_str = str(e)
                print(f"Ошибка при запросе к модели {resolved_model}: {err_str}")
                error_payload = json.dumps({"type": "text", "credits": current_balance, "text": f"Ох, туман помешал ответу: {err_str}"})
                await websocket.send_text(error_payload)
            
    except WebSocketDisconnect:
        print(f"Клиент {client_email} отключился.")

# ==================== Авторизация и история ====================

@app.post("/api/auth")
async def authenticate_user(request: Request):
    if not GOOGLE_SCRIPT_URL:
        return {"success": True}
    try:
        data = await request.json()
        response = requests.post(GOOGLE_SCRIPT_URL, json={
            "action": "auth",
            "email": data.get("email"),
            "password": data.get("password")
        }, timeout=25)
        if response.status_code != 200:
            return {"success": False, "error": "Ошибка сервера авторизации"}
        return response.json()
    except Exception as e:
        print(f"❌ Ошибка авторизации: {e}")
        return {"success": False, "error": str(e)}

@app.get("/api/history/{email}")
async def get_user_history(email: str):
    if not GOOGLE_SCRIPT_URL:
        return {"success": True, "messages": []}
    try:
        response = requests.post(GOOGLE_SCRIPT_URL, json={"action": "get_history", "email": email}, timeout=25)
        if response.status_code != 200:
            return {"success": True, "messages": []}
        data = response.json()
        return data if isinstance(data, dict) else {"success": True, "messages": []}
    except Exception as e:
        print(f"❌ Ошибка получения истории: {e}")
        return {"success": True, "messages": []}

@app.post("/api/history")
async def save_user_history(request: Request):
    if not GOOGLE_SCRIPT_URL:
        return {"success": True}
    try:
        data = await request.json()
        save_message_to_sheet(data.get("email"), data.get("role"), data.get("text"), data.get("image"))
        return {"success": True}
    except Exception as e:
        print(f"❌ Ошибка сохранения истории: {e}")
        return {"success": False, "error": str(e)}

# ==================== Платежные эндпоинты ЮKassa ====================

@app.post("/api/create-payment")
async def create_payment(request: Request):
    try:
        data = await request.json()
        email = data.get("email")
        amount = data.get("amount")
        credits = data.get("credits")
        
        if not email or not amount or not credits:
            return {"success": False, "error": "Неверные параметры"}
            
        if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
            return {"success": True, "confirmation_url": "https://yookassa.ru"}

        url = "https://api.yookassa.ru/v3/payments"
        idempotence_key = str(uuid.uuid4())
        
        payload = {
            "amount": {
                "value": f"{float(amount):.2f}",
                "currency": "RUB"
            },
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": "https://babayagaland.ru/aiayaga"
            },
            "description": f"Покупка {credits} кредитов НейроЯга для {email}",
            "metadata": {
                "email": email,
                "credits": int(credits)
            }
        }
        
        headers = {
            "Idempotence-Key": idempotence_key,
            "Content-Type": "application/json"
        }
        
        response = requests.post(
            url, 
            json=payload, 
            auth=HTTPBasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            headers=headers,
            timeout=10
        )
        
        res_json = response.json()
        confirmation_url = res_json.get("confirmation", {}).get("confirmation_url")
        
        if confirmation_url:
            return {"success": True, "confirmation_url": confirmation_url}
        else:
            return {"success": False, "error": res_json.get("description", "Ошибка создания платежа")}
            
    except Exception as e:
        print(f"Ошибка создания платежа ЮKassa: {e}")
        return {"success": False, "error": str(e)}

@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    try:
        event_json = await request.json()
        if event_json.get("event") == "payment.succeeded":
            payment_object = event_json.get("object", {})
            metadata = payment_object.get("metadata", {})
            email = metadata.get("email")
            credits_to_add = int(metadata.get("credits", 0))
            
            if email and credits_to_add > 0 and GOOGLE_SCRIPT_URL:
                requests.post(GOOGLE_SCRIPT_URL, json={
                    "action": "add",
                    "email": email,
                    "amount": credits_to_add
                }, timeout=25)
                print(f"✅ Успешно начислено {credits_to_add} кредитов пользователю {email}")
                        
        return {"status": "ok"}
    except Exception as e:
        print(f"❌ Ошибка вебхука ЮKassa: {e}")
        return {"status": "error", "message": str(e)}, 400

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)