import os
import json
import base64
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
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

def check_and_deduct_credits(email: str, cost: int) -> dict:
    """Запрос в Google Таблицу: проверка баланса и списание. Возвращает dict с успехом и остатком."""
    if not GOOGLE_SCRIPT_URL:
        return {"success": True, "credits": 999}

    try:
        payload = {
            "action": "deduct",
            "email": email,
            "cost": cost
        }
        response = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=8)
        res_data = response.json()
        return {
            "success": res_data.get("success", False),
            "credits": res_data.get("credits", 0)
        }
    except Exception as e:
        print(f"❌ Ошибка при обращении к Google Apps Script: {e}")
        return {"success": False, "credits": 0}

@app.websocket("/ws/{client_email}")
async def websocket_endpoint(websocket: WebSocket, client_email: str):
    await websocket.accept()
    print(f"Клиент {client_email} успешно подключился!")
    
    # Запрос актуального баланса при подключении (стоимость 0)
    init_result = check_and_deduct_credits(client_email, 0)
    initial_balance = init_result.get("credits", 10)
    
    await websocket.send_text(json.dumps({
        "type": "text",
        "credits": initial_balance,
        "text": "🧚‍♀️ Приветствую в НейроЯге! Выбирай модель в меню сверху и задавай вопросы."
    }))

    try:
        while True:
            raw_message = await websocket.receive_text()
            
            prompt = raw_message
            model_key = 'flash'
            
            try:
                data = json.loads(raw_message)
                if isinstance(data, dict):
                    prompt = data.get('prompt', raw_message)
                    model_key = data.get('modelKey', 'flash')
            except json.JSONDecodeError:
                pass 
            
            resolved_model = MODEL_MAPPING.get(model_key, 'gemini-3.8-flash')
            required_credits = CREDIT_COSTS.get(model_key, 1)
            
            print(f"Запрос от {client_email}: {prompt} | Модель: {resolved_model} (Стоимость: {required_credits} кр.)")
            
            # Проверяем и списываем кредиты
            credit_result = check_and_deduct_credits(client_email, required_credits)
            can_proceed = credit_result.get("success", False)
            current_balance = credit_result.get("credits", 0)

            if not can_proceed:
                error_payload = json.dumps({
                    "type": "text", 
                    "credits": current_balance,
                    "text": f"💎 Недостаточно кредитов! Требуется: {required_credits} кр., а на балансе: {current_balance} кр. Пожалуйста, пополни баланс."
                })
                await websocket.send_text(error_payload)
                continue
            
            if model_key in ['nanobanana', 'nanobanana_pro']:
                loading_payload = json.dumps({
                    "type": "text", 
                    "credits": current_balance,
                    "text": "🎨 НейроЯга ушла в чащу за волшебными красками... Картинка создается, подождите пару секунд!"
                })
                await websocket.send_text(loading_payload)
            
            try:
                config_params = {
                    "system_instruction": "Ты — НейроЯга, премиальный искусственный интеллект. Отвечай с легким сказочным вайбом, но четко и по делу."
                }
                
                response = client.models.generate_content(
                    model=resolved_model,
                    contents=prompt,
                    config=config_params
                )
                
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
                
                if image_data:
                    response_payload = {
                        "type": "image",
                        "data": image_data,
                        "mimeType": mime_type,
                        "credits": current_balance,
                        "text": text_output if text_output else f"🎨 Твой шедевр готов! (-{required_credits} кр.)"
                    }
                else:
                    response_payload = {
                        "type": "text",
                        "credits": current_balance,
                        "text": text_output if text_output else "Туман скрывает ответ..."
                    }
                
                await websocket.send_text(json.dumps(response_payload))

            except Exception as e:
                err_str = str(e)
                print(f"Ошибка при запросе к модели {resolved_model}: {err_str}")
                error_payload = json.dumps({"type": "text", "credits": current_balance, "text": f"Ох, туман помешал ответу: {err_str}"})
                await websocket.send_text(error_payload)
            
    except WebSocketDisconnect:
        print(f"Клиент {client_email} отключился.")

@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    try:
        event_json = await request.json()
        if event_json.get("event") == "payment.succeeded":
            payment_object = event_json.get("object", {})
            metadata = payment_object.get("metadata", {})
            email = metadata.get("email") or payment_object.get("receipt", {}).get("email")
            
            amount_value = float(payment_object.get("amount", {}).get("value", 0))
            credits_to_add = int(amount_value / 6)
            
            if email and GOOGLE_SCRIPT_URL:
                requests.post(GOOGLE_SCRIPT_URL, json={
                    "action": "add",
                    "email": email,
                    "amount": credits_to_add
                })
                print(f"Начислено {credits_to_add} кредитов пользователю {email}")
                        
        return {"status": "ok"}
    except Exception as e:
        print(f"Ошибка вебхука ЮKassa: {e}")
        return {"status": "error", "message": str(e)}, 400

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)