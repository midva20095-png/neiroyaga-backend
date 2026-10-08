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

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    print(f"Клиент {client_id} успешно подключился!")
    try:
        while True:
            # Получаем текст из виджета на Тильде
            query = await websocket.receive_text()
            print(f"Получен запрос: {query}")
            
            # Отправляем запрос в модель Gemini 2.0 Flash
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=query,
                config={
                    "system_instruction": "Ты — НейроЯга, премиальный искусственный интеллект. Отвечай с легким сказочным вайбом, но четко и по делу."
                }
            )
            
            answer = response.text if response.text else "Туман скрывает ответ, попробуй еще раз."
            
            # Отправляем ответ обратно на сайт в реальном времени
            await websocket.send_text(answer)
            
    except WebSocketDisconnect:
        print(f"Клиент {client_id} отключился.")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)