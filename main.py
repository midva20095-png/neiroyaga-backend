import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from dotenv import load_dotenv

# Загружаем ключ из .env файла
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

# Актуальный список моделей с резервным переключением (на базе актуального конфига)
AVAILABLE_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.1-pro-preview",
    "gemini-2.5-flash"
]

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    print(f"Клиент {client_id} успешно подключился!")
    try:
        while True:
            query = await websocket.receive_text()
            print(f"Получен запрос: {query}")
            
            answer = None
            
            # Последовательно пробуем актуальные модели из списка
            for model_name in AVAILABLE_MODELS:
                try:
                    print(f"Пробуем модель: {model_name}")
                    response = client.models.generate_content(
                        model=model_name,
                        contents=query,
                        config={
                            "system_instruction": "Ты — НейроЯга, премиальный искусственный интеллект. Отвечай с легким сказочным вайбом, но четко и по делу."
                        }
                    )
                    if response.text:
                        answer = response.text
                        print(f"Успешно ответила модель: {model_name}")
                        break
                except Exception as e:
                    print(f"Модель {model_name} недоступна: {e}")
                    continue
            
            # Если все модели временно перегружены
            if not answer:
                answer = "Ох, густой туман застилал все каналы связи... Сервера Google перегружены, попробуй отправить сообщение еще раз!"
            
            # Отправляем ответ на сайт в реальном времени
            await websocket.send_text(answer)
            
    except WebSocketDisconnect:
        print(f"Клиент {client_id} отключился.")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
