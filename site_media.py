"""Имена файлов для Братухи и длина записи.

Расширение в публичной ссылке должно совпадать с байтами файла.
"""

import base64
import binascii


def media_file(data: bytes, mime: str, role: str, index: int = 1):
    """Возвращает (имя, content-type) или (None, None), если формат не подходит."""
    if not data:
        return None, None
    if role == "image":
        if data[:3] == b"\xff\xd8\xff":
            return f"photo_{index}.jpg", "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return f"photo_{index}.png", "image/png"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return f"photo_{index}.webp", "image/webp"
        return None, None
    if data[:3] == b"ID3" or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "voice.mp3", "audio/mpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "voice.wav", "audio/wav"
    if data[:4] == b"fLaC":
        return "voice.flac", "audio/flac"
    if data[:4] == b"OggS":
        return "voice.ogg", "audio/ogg"
    if len(data) > 11 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"M4A ", b"mp42", b"isom", b"M4A", b"mp41"):
            return "voice.m4a", "audio/mp4"
        return "voice.m4a", "audio/mp4"
    return None, None


def _wav_seconds(data: bytes):
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    rate = channels = bits = data_len = None
    i = 12
    while i + 8 <= len(data):
        cid = data[i:i + 4]
        size = int.from_bytes(data[i + 4:i + 8], "little")
        body = i + 8
        if cid == b"fmt " and body + 16 <= len(data):
            channels = int.from_bytes(data[body + 2:body + 4], "little") or 1
            rate = int.from_bytes(data[body + 4:body + 8], "little") or None
            bits = int.from_bytes(data[body + 14:body + 16], "little") or 16
        elif cid == b"data":
            data_len = size
            break
        step = 8 + size + (size % 2)
        if step <= 0:
            break
        i += step
    if not rate or not data_len or not channels or not bits:
        return None
    return data_len / (rate * channels * (bits / 8))


SITE_SIZES = ("1:1", "4:5", "3:4", "9:16", "4:3", "16:9")


def site_sizes(raw, is_text=False):
    """Размеры шаблона. Пустое поле — все размеры, как в боте."""
    if is_text:
        return []
    picked = []
    for bit in str(raw or "").replace(",", ";").split(";"):
        bit = bit.strip()
        if bit in SITE_SIZES and bit not in picked:
            picked.append(bit)
    return picked or list(SITE_SIZES)


def cover_bytes(raw: str, limit: int = 2_500_000):
    """Обложка шаблона: ссылка, пусто или байты картинки.

    Возвращает (url, None, None), ('', None, None) или (None, bytes, mime).
    """
    text = str(raw or "").strip()
    if not text:
        return "", None, None
    if text.startswith("https://") or text.startswith("http://"):
        if len(text) > 500:
            raise ValueError("Ссылка на картинку слишком длинная")
        return text, None, None
    payload = text
    if text.startswith("data:"):
        head, _, payload = text.partition(",")
        if not payload or "base64" not in head.lower():
            raise ValueError("Фото не прочиталось")
    try:
        data = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        raise ValueError("Фото не прочиталось")
    if not data or len(data) > limit:
        raise ValueError("Фото больше 2 МБ. Выберите другое")
    _name, mime = media_file(data, "", "image", 1)
    if not mime:
        raise ValueError("Нужна картинка jpg, png или webp")
    return None, data, mime


def audio_seconds(data: bytes):
    """Длительность записи в секундах. Сначала свой разбор WAV, затем mutagen."""
    wav = _wav_seconds(data)
    if wav:
        return wav
    try:
        import io
        import mutagen
        parsed = mutagen.File(io.BytesIO(data))
        if parsed and parsed.info and parsed.info.length:
            return float(parsed.info.length)
    except Exception:
        return None
    return None
