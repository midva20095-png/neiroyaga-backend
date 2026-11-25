# Сборка запросов в Kie.ai. Сеть здесь не вызывается.

IDEOGRAM_SIZE = {
    "1:1": "square_hd",
    "16:9": "landscape_16_9",
    "9:16": "portrait_16_9",
    "4:3": "landscape_4_3",
    "3:4": "portrait_4_3",
}


def aspect_of(spec, aspect):
    options = spec.get("aspects") or []
    if aspect and aspect in options:
        return aspect
    return options[0] if options else "1:1"


def allowed_size(spec, quality):
    options = spec.get("qualities") or []
    if not options:
        return spec.get("kieSizeValue")
    found = next((item for item in options if item.get("id") == quality), None)
    if not found:
        found = next((item for item in options if item.get("id") == spec.get("quality")), None) or options[0]
    return found.get("id")


def build_image_task(spec, prompt, aspect, quality, image_urls):
    images = [url for url in (image_urls or []) if isinstance(url, str) and url.startswith("https://")][:6]
    if images and not spec.get("kieEdit"):
        raise ValueError("Эта модель рисует только по тексту, без фото.")
    model = spec.get("kieEdit") if images else spec.get("kieModel")
    payload = {"prompt": str(prompt or "").strip()[: spec.get("maxChars") or 4000]}
    ratio = aspect_of(spec, aspect)
    if spec.get("kieAspect") == "image_size":
        payload["image_size"] = IDEOGRAM_SIZE.get(ratio, "square_hd")
        payload["rendering_speed"] = "TURBO"
        payload["style"] = "AUTO"
        payload["expand_prompt"] = True
    elif spec.get("kieAspect"):
        payload[spec["kieAspect"]] = ratio
    size = allowed_size(spec, quality)
    if size and spec.get("kieSize"):
        payload[spec["kieSize"]] = size
    if images and spec.get("kieImage"):
        payload[spec["kieImage"]] = images
    if spec.get("kieExtra"):
        payload.update(spec["kieExtra"])
    return {"model": model, "input": payload}


def build_chat(spec, prompt):
    chat = spec.get("kieChat") or {}
    text = str(prompt or "").strip()[: chat.get("maxChars") or 3500]
    if chat.get("style") == "responses":
        body = {
            "model": chat.get("model"),
            "stream": False,
            "input": text,
            "reasoning": {"effort": "none"},
            "max_output_tokens": chat.get("maxTokens") or 800,
        }
        retry = {"model": chat.get("model"), "stream": False, "input": text, "reasoning": {"effort": "none"}}
        return {"path": chat.get("path"), "body": body, "retry": retry}
    if chat.get("style") == "claude":
        return {
            "path": chat.get("path"),
            "body": {
                "model": chat.get("model"),
                "stream": False,
                "max_tokens": chat.get("maxTokens") or 700,
                "messages": [{"role": "user", "content": text}],
            },
        }
    return {"path": chat.get("path"), "body": {"messages": [{"role": "user", "content": text}]}}


def text_from_response(style, data):
    if not isinstance(data, dict):
        return ""
    if style == "responses":
        parts = []
        for block in data.get("output") or []:
            if not isinstance(block, dict) or block.get("type") == "reasoning":
                continue
            for part in block.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text" and part.get("text"):
                    parts.append(part["text"])
        return "\n".join(parts).strip()
    if style == "claude":
        return "\n".join(
            part.get("text") or ""
            for part in (data.get("content") or [])
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        ).strip()
    message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(str((part or {}).get("text") or (part or {}).get("content") or "") for part in content).strip()
    return ""


def _collect_urls(value, into):
    if not value or len(into) > 4:
        return
    if isinstance(value, str):
        if value.startswith("https://"):
            into.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_urls(item, into)
        return
    if isinstance(value, dict):
        for key in ("resultUrls", "resultUrl", "url", "imageUrl", "image_url", "outputUrl", "downloadUrl"):
            if value.get(key):
                _collect_urls(value[key], into)


def task_view(data):
    row = (data or {}).get("data") if isinstance(data, dict) else {}
    row = row if isinstance(row, dict) else {}
    urls = []
    raw = row.get("resultJson")
    if raw:
        try:
            import json
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            _collect_urls(parsed, urls)
        except Exception:
            pass
    parts = [str(row.get("failCode") or ""), str(row.get("failMsg") or "")]
    if isinstance(data, dict):
        parts.append(str(data.get("msg") or ""))
    fail = " ".join(part.strip() for part in parts if part and part.strip())
    return {
        "state": row.get("state") or "",
        "urls": urls,
        "fail": fail[:300],
    }


def upload_view(data):
    body = data if isinstance(data, dict) else {}
    bag = body.get("data") if isinstance(body.get("data"), dict) else {}
    url = next((item for item in (bag.get("downloadUrl"), bag.get("fileUrl"), bag.get("url"), body.get("downloadUrl"))
                if isinstance(item, str) and item.startswith("https://")), "")
    try:
        code = int(body.get("code"))
    except (TypeError, ValueError):
        code = 0
    if code not in (0, 200):
        url = ""
    return {"url": url, "code": code, "msg": str(body.get("msg") or body.get("message") or "")[:180]}


def error_text(data):
    if not data:
        return ""
    if isinstance(data, str):
        return data[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err = err.get("message") or err.get("error")
        return str(data.get("msg") or data.get("message") or err or "")[:300]
    return ""


def explain_failure(text):
    raw = str(text or "")
    low = raw.lower()
    if any(word in low for word in ("content moderation", "blocked by", "safety", "nsfw")):
        return "Запрос не прошёл проверку. Измените описание или фото и попробуйте ещё раз."
    if "input was rejected" in low or "different input" in low:
        return "Модель не приняла этот запрос. Измените описание и попробуйте ещё раз."
    if any(word in low for word in ("insufficient", "not enough credit", "no credits", "402")):
        return "Эта модель сейчас недоступна. Попробуйте другую или через минуту."
    if any(word in low for word in ("invalid", "parameter", "unsupported", "aspect ratio", "resolution")):
        return "Модель не приняла размер. Выберите другой и попробуйте ещё раз."
    if any(word in low for word in ("internal error", "try again later", "unavailable", "bad gateway", "timeout", "playground task failed")) or raw.strip() in ("500", "501", "505"):
        return "Модель сейчас не отвечает. Попробуйте через минуту."
    return ""


def retryable_failure(text):
    raw = str(text or "").strip()
    low = raw.lower()
    if any(word in low for word in ("content moderation", "blocked by", "safety", "nsfw", "input was rejected", "different input", "insufficient", "not enough credit", "402", "invalid", "parameter", "unsupported", "фото не загрузилось", "не удалось забрать", "пустая картинка", "не подключена", "не запустилась")):
        return False
    if not raw or raw in ("500", "501", "505", "без текста от модели"):
        return True
    if any(word in low for word in ("internal error", "try again later", "unavailable", "bad gateway", "timeout", "playground task failed")):
        return True
    return True


def url_ok(value):
    try:
        from urllib.parse import urlparse
        url = urlparse(value)
    except Exception:
        return False
    host = (url.hostname or "").lower()
    return url.scheme == "https" and host not in ("localhost",) and not host.endswith(".local")


def image_mime(data):
    if not data:
        return ""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def compact_prompt(prompt, history, limit=3500):
    lines = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        who = "Пользователь" if item.get("role") == "user" else "НейроЯга"
        lines.append(f"{who}: {text[:800]}")
    if not lines:
        lines = [str(prompt or "").strip()]
    return "\n".join(lines)[-limit:]
