from __future__ import annotations

import hashlib
import io
import base64
import threading
import time

import requests
from PIL import Image

from .answers import BASE_INSTRUCTION, clean_answer
from .config import Config


class GeminiError(RuntimeError):
    pass


class GeminiClient:
    def __init__(self, config: Config, session=None, sleep=time.sleep):
        self.config = config
        self.session = session if session is not None else requests.Session()
        self.sleep = sleep
        self._cache = {}
        self._lock = threading.Lock()

    def _redact(self, text: str) -> str:
        for key in self.config.api_keys:
            text = text.replace(key, "<API key>")
        return text[:300]

    @staticmethod
    def _image_part(image_bytes: bytes) -> str:
        with Image.open(io.BytesIO(image_bytes)) as original:
            image = original.convert("RGB")
            image.thumbnail((2400, 2400))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90)
        return base64.b64encode(output.getvalue()).decode("ascii")

    def ask(self, image_bytes: bytes, materials: str = "", material_type: str = "") -> str:
        if not self.config.api_keys:
            raise GeminiError("Не задан GEMINI_API_KEY. Заполните .env или переменную окружения.")
        cache_key = hashlib.sha256(image_bytes + b"\0" + materials.encode("utf-8") + b"\0" + material_type.encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        prompt = BASE_INSTRUCTION
        if materials:
            prompt = f"Лекции по {material_type}:\n{materials}\n\n{prompt}"
        payload = {"contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": self._image_part(image_bytes)}},
        ]}]}
        failures = []
        last_error = None
        for model in self.config.models:
            unavailable_model = False
            for key in self.config.api_keys:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                for attempt in range(2):
                    try:
                        response = self.session.post(url, headers={"x-goog-api-key": key}, json=payload, timeout=(10, self.config.request_timeout))
                    except requests.RequestException as error:
                        last_error = error
                        if attempt == 0:
                            self.sleep(1)
                            continue
                        failures.append(f"{model}: ошибка соединения ({type(error).__name__})")
                        break
                    if response.status_code in (408, 429, 500, 502, 503, 504) and attempt == 0:
                        self.sleep(1)
                        continue
                    if response.status_code != 200:
                        unavailable_model = response.status_code == 404
                        detail = ""
                        try:
                            data = response.json()
                            detail = data.get("error", {}).get("message", "")
                        except (ValueError, AttributeError, TypeError):
                            pass
                        failures.append(f"{model}: HTTP {response.status_code} {self._redact(str(detail))}".rstrip())
                        break
                    try:
                        data = response.json()
                        candidates = data.get("candidates") or []
                        candidate = candidates[0] if candidates else {}
                        parts = candidate.get("content", {}).get("parts", [])
                        text = "".join(part.get("text", "") for part in parts if not part.get("thought", False))
                        finish = candidate.get("finishReason")
                        if finish == "MAX_TOKENS":
                            raise GeminiError(f"{model}: ответ обрезан по лимиту токенов")
                        if finish in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"):
                            raise GeminiError(f"{model}: генерация остановлена ({finish})")
                        if not text.strip():
                            reason = data.get("promptFeedback", {}).get("blockReason") or finish or "нет текста"
                            raise GeminiError(f"{model}: пустой ответ ({reason})")
                        answer = clean_answer(text)
                        if not answer or answer == "ERR":
                            raise GeminiError(f"{model}: ответ не удалось разобрать")
                    except (ValueError, AttributeError, TypeError, KeyError, GeminiError) as error:
                        last_error = error
                        failures.append(self._redact(str(error)))
                        break
                    with self._lock:
                        if len(self._cache) >= 64:
                            self._cache.pop(next(iter(self._cache)))
                        self._cache[cache_key] = answer
                    return answer
                if unavailable_model:
                    break
        raise GeminiError("Gemini не вернул ответ: " + "; ".join(failures[-6:])) from last_error
