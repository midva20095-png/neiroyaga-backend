import os
import json
import base64
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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
            
            if model_key in ['nanobanana', 'nanobanana_pro']:
                loading_payload = json.dumps({
                    "type": "text", 
                    "text": "🎨 НейроЯга ушла в чащу за волшебными красками... Картинка создается, подождите пару секунд!"
                })
                await websocket.send_text(loading_payload)
            
            try:
                # Чистый конфиг без принудительных модальностей
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
                        "text": text_output if text_output else "🎨 Твой сказочный шедевр готов!"
                    }
                else:
                    response_payload = {
                        "type": "text",
                        "text": text_output if text_output else "Туман скрывает ответ..."
                    }
                
                await websocket.send_text(json.dumps(response_payload))

            except Exception as e:
                err_str = str(e)
                print(f"Ошибка при запросе к модели {resolved_model}: {err_str}")
                
                error_payload = json.dumps({"type": "text", "text": f"Ох, туман помешал ответу: {err_str}"})
                await websocket.send_text(error_payload)
            
    except WebSocketDisconnect:
        print(f"Клиент {client_id} отключился.")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)