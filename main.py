import os
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Инициализация клиента Google Gemini
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Актуальный маппинг моделей
MODEL_MAPPING = {
    'flash': 'gemini-3.8-flash',
    'flash_25': 'gemini-3.8-flash',
    'pro': 'gemini-3.1-pro-preview',
    'nanobanana': 'gemini-3.1-flash-image',
    'nanobanana_pro': 'gemini-3-pro-image'
}

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    print(f"Клиент {client_id} успешно подключился!")
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
            print(f"Запрос: {prompt} | Модель: {resolved_model} ({model_key})")
            
            answer = "Туман скрывает ответ..."
            
            try:
                response = client.models.generate_content(
                    model=resolved_model,
                    contents=prompt,
                    config={
                        "system_instruction": "Ты — НейроЯга, премиальный искусственный интеллект. Отвечай с легким сказочным вайбом, но четко и по делу."
                    }
                )
                
                text_output = ""
                image_data = None
                mime_type = "image/jpeg"
                
                # Безопасно извлекаем текст и картинку из частей ответа без вызова проксирующих текстовых свойств
                candidate = response.candidates[0] if response.candidates else None
                if candidate and candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if getattr(part, 'text', None):
                            text_output += ("\n" if text_output else "") + part.text
                        
                        inline = getattr(part, 'inline_data', None) or getattr(part, 'inlineData', None)
                        if inline and getattr(inline, 'data', None):
                            image_data = inline.data
                            mime_type = getattr(inline, 'mime_type', None) or getattr(inline, 'mimeType', None) or "image/jpeg"
                
                # Если модель вернула изображение
                if image_data:
                    caption = text_output if text_output else "🎨 Твой сказочный котик готов!"
                    answer = f"{caption}<br><img src='data:{mime_type};base64,{image_data}' style='max-width:100%; border-radius:8px; margin-top:8px;'>"
                elif text_output:
                    answer = text_output
                else:
                    answer = "Туман скрывает ответ..."

            except Exception as e:
                err_str = str(e)
                print(f"Ошибка при запросе к модели {resolved_model}: {err_str}")
                
                if "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                    answer = "🎨 Упс! Модели генерации картинок требуют активированного биллинга (плана Pay-as-you-go) в аккаунте Google AI Studio. Используйте текстовые модели Flash или Pro."
                else:
                    # Фолбэк на надежный flash при сбое
                    try:
                        fallback_response = client.models.generate_content(
                            model="gemini-3.8-flash",
                            contents=prompt,
                            config={
                                "system_instruction": "Ты — НейроЯга, премиальный искусственный интеллект. Отвечай с легким сказочным вайбом, но четко и по делу."
                            }
                        )
                        answer = fallback_response.text if fallback_response.text else "Ох, туман помешал ответу..."
                    except Exception as fb_err:
                        answer = f"Ох, густой туман застилал каналы связи... Сервера перегружены. Попробуй еще раз!"
            
            await websocket.send_text(answer)
            
    except WebSocketDisconnect:
        print(f"Клиент {client_id} отключился.")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)