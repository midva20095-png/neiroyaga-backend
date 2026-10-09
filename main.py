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
    'nanobanana_pro': 'gemini-3-pro-image',
    'veo': 'veo-3.1-generate-preview'
}

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    print(f"Клиент {client_id} успешно подключился!")
    try:
        while True:
            raw_message = await websocket.receive_text()
            
            # По умолчанию
            prompt = raw_message
            model_key = 'flash'
            
            # Пытаемся распарсить JSON от сайта (если выбрана конкретная модель)
            try:
                data = json.loads(raw_message)
                if isinstance(data, dict):
                    prompt = data.get('prompt', raw_message)
                    model_key = data.get('modelKey', 'flash')
            except json.JSONDecodeError:
                pass # Если пришел обычный текст без JSON
            
            resolved_model = MODEL_MAPPING.get(model_key, 'gemini-3.8-flash')
            print(f"Запрос: {prompt} | Модель: {resolved_model} ({model_key})")
            
            answer = "Туман скрывает ответ..."
            
            try:
                # Особая обработка для генерации видео через Veo
                if model_key == 'veo':
                    operation = client.models.generateVideos(
                        model=resolved_model,
                        prompt=prompt or "Cinematic video generation"
                    )
                    answer = "🎬 Запрос на создание видео отправлен модели Veo! (Генерация видео в облаке требует времени, скоро вернемся с результатом)."
                else:
                    # Стандартная генерация текста или картинок
                    response = client.models.generate_content(
                        model=resolved_model,
                        contents=prompt,
                        config={
                            "system_instruction": "Ты — НейроЯга, премиальный искусственный интеллект. Отвечай с легким сказочным вайбом, но четко и по делу."
                        }
                    )
                    
                    text_output = ""
                    image_data = None
                    
                    if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
                        for part in response.candidates[0].content.parts:
                            if hasattr(part, 'text') and part.text:
                                text_output += ("\n" if text_output else "") + part.text
                            if hasattr(part, 'inline_data') and part.inline_data and part.inline_data.data:
                                image_data = part.inline_data.data
                            elif hasattr(part, 'inlineData') and part.inlineData and part.inlineData.data:
                                image_data = part.inlineData.data
                    
                    if text_output:
                        answer = text_output
                    elif response.text:
                        answer = response.text
                    
                    # Если модель вернула картинку (например, nanobanana)
                    if image_data:
                        answer = f"🎨 Изображение успешно создано!\ndata:image/jpeg;base64,{image_data}"

            except Exception as e:
                print(f"Ошибка при запросе к модели {resolved_model}: {e}")
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
            
            # Отправляем ответ обратно на сайт
            await websocket.send_text(answer)
            
    except WebSocketDisconnect:
        print(f"Клиент {client_id} отключился.")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)