import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if __name__ == "__main__" and sys.platform != "win32":
    from clonef.__main__ import main
    raise SystemExit(main())

import threading
import queue
import time
import base64
import io
import json
import os
import re
import hashlib
import subprocess
import requests
import ctypes
from ctypes import wintypes
from PIL import Image, ImageGrab
import keyboard

# ========== НАСТРОЙКИ ==========
from clonef.config import Config
from clonef.gemini import GeminiClient

_config = Config.load()
_gemini_client = GeminiClient(_config)
GEMINI_API_KEYS = list(_config.api_keys)

MODELS_TO_TRY = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-3-flash-preview",
    "gemini-2.5-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite-preview",
]

UI_COLOR = "#666666"
DURATION_MS = 2200
WINDOW_WIDTH = 400
WINDOW_HEIGHT = 200
MARGIN_LEFT = 15
MARGIN_BOTTOM = 115   # выше на 50 px, чем было (65)
UI_FONT_SIZE = 9

DB_FILE = str(_config.material_path("DB"))
JAVA_FILE = str(_config.material_path("Java"))

# --- КЭШИРОВАНИЕ ---
CACHE_ENABLED = False
CACHE_CREATE_ENABLED = False
CACHE_TTL = 86400
CACHE_MIN_TOKENS = 1024
CACHE_NAME_FILE = os.path.join(os.environ.get("PUBLIC", "C:\\Users\\Public"), "gemini_cache_name.txt")
cache_name = None

current_materials = None
current_type = None
materials_loaded = False
answer_cache = {}
is_processing = False

# --- ПЕРЕМЕННЫЕ MANUAL AUTO-TYPE ---
auto_type_buffer = ""
auto_type_buffer_index = 0
auto_type_active = False
_dynamic_hook_id = None

# Очередь символов на печать. Хук кладёт символ, поток sender_loop печатает.
# Печать идёт из отдельного потока; хук при этом НЕ снимается — keyboard сам
# пропускает юникод-события (vk == VK_PACKET), поэтому реальные клавиши
# остаются заблокированными (нет протечки чужих символов в текст).
# Очередь (а не один слот) нужна, чтобы при быстром наборе символы не терялись.
_type_queue = queue.Queue()

# Сколько наших синтетических Enter/Tab ещё должен пропустить хук. Юникод
# keyboard игнорирует сам, а Enter/Tab шлются реальными VK и возвращаются в хук.
_injected_special = 0
_injected_lock = threading.Lock()

# Сами отслеживаем зажатые модификаторы по событиям хука: keyboard.is_pressed()
# внутри suppress-хука бывает неточным (особенно когда на ту же комбинацию висит
# глобальный хоткей), из-за чего Right Shift+I не определялся как пауза.
_held_mods = set()
_MOD_NAMES = {'right shift', 'left shift', 'shift', 'left ctrl', 'right ctrl',
              'ctrl', 'alt', 'left alt', 'right alt', 'left windows',
              'right windows', 'caps lock', 'num lock'}
_SHIFT_NAMES = {'right shift', 'left shift', 'shift'}
_CTRL_NAMES = {'left ctrl', 'right ctrl', 'ctrl'}

# Печатать подробный лог в консоль (диагностика). Поставь True, если что-то не так.
DEBUG_TYPING = False

# Момент, когда хук сам остановил печать (для защиты от мгновенного повторного
# включения тем же нажатием Right Shift+I через глобальный хоткей).
_last_stop_time = 0.0

PYQT_SITE_PACKAGES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "python", "Lib", "site-packages")
if os.path.isdir(PYQT_SITE_PACKAGES) and PYQT_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, PYQT_SITE_PACKAGES)

from PyQt5.QtWidgets import QApplication, QLabel, QWidget
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QFont

class UISignals(QObject):
    show_signal = pyqtSignal(str)

ui_signals = UISignals()

BASE_INSTRUCTION = """Ты — эксперт по Computer Science и базам данных, а также по Java.

Формат ответа для тестов (СТРОГО):
1. После анализа напиши ===ОТВЕТЫ=== на отдельной строке.
2. После разделителя — каждый ответ с новой строки в формате: НОМЕР_ВОПРОСА:ОТВЕТ
3. Для вопросов с буквами (A,B,C): пиши слитно (ABD)
4. Для True/False: только T или F (НЕ TRUE/FALSE)
5. Для вопросов без вариантов: пронумеруй сверху вниз и напиши номера правильных ответов слитно (24)

Если на изображении задача на программирование:
- игнорируй формат тестовых ответов
- выдай только чистый код
- без комментариев
- без Markdown
- без блоков ```.

Пример:
===ОТВЕТЫ===
14:ABD
15:T
16:24

НЕ ДОБАВЛЯЙ никаких пояснений, текста, рассуждений. Только ответы после разделителя. Ничего до разделителя тоже не пиши, кроме анализа, но после разделителя — только ответы."""

# ---------- ФУНКЦИИ КЭШИРОВАНИЯ ----------
def estimate_token_count(text):
    compact_text = text.strip()
    if not compact_text:
        return 0
    return max(1, len(compact_text) // 4)

def create_context_cache(api_key, content, ttl_seconds=CACHE_TTL):
    if not CACHE_CREATE_ENABLED:
        print("[CACHE] Явное создание кэша отключено, пропускаю.")
        return None

    token_count = estimate_token_count(content)
    print(f"[CACHE] Попытка создать кэш с ключом {api_key[:8]}... (TTL={ttl_seconds} сек, tokens≈{token_count})")
    url = f"https://generativelanguage.googleapis.com/v1beta/cachedContents?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": "models/gemini-2.5-flash",
        "contents": [{"role": "user", "parts": [{"text": content}]}],
        "ttl": f"{ttl_seconds}s"
    }
    start = time.time()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        elapsed = time.time() - start
        print(f"[CACHE] Ответ получен за {elapsed:.2f} сек, статус {resp.status_code}")
        if resp.status_code == 200:
            name = resp.json().get("name")
            print(f"[CACHE] Успех! Имя кэша: {name}")
            return name
        if resp.status_code == 400:
            try:
                error_message = resp.json().get("error", {}).get("message", "Unknown cache error")
            except Exception:
                error_message = resp.text
            print(f"[CACHE] Кэш не создан: {error_message}")
            return None
        print(f"[CACHE] Неожиданный статус {resp.status_code}: {resp.text[:300]}")
        return None
    except Exception as e:
        print(f"[CACHE] Ошибка создания кэша: {e}")
        return None

def load_materials(file_path, material_type, indicator):
    global current_materials, current_type, materials_loaded, cache_name
    print(f"\n[MATERIAL] Загрузка {file_path}...")
    if not os.path.exists(file_path):
        print(f"[MATERIAL] Файл не найден!")
        ui_signals.show_signal.emit("Файл не найден!")
        return
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            current_materials = f.read()
        current_type = material_type
        materials_loaded = True
        print(f"[MATERIAL] Успешно загружено {len(current_materials)} символов (тип: {material_type})")

        if CACHE_ENABLED:
            current_tokens = estimate_token_count(current_materials)
            if current_tokens < CACHE_MIN_TOKENS:
                print(f"[CACHE] Материала слишком мало для явного кэша: ≈{current_tokens} токенов, минимум {CACHE_MIN_TOKENS}. Пропускаю.")
                cache_name = None
            elif os.path.exists(CACHE_NAME_FILE):
                with open(CACHE_NAME_FILE, 'r') as f:
                    cache_name = f.read().strip()
                print(f"[CACHE] Найден сохранённый кэш: {cache_name}")
            elif CACHE_CREATE_ENABLED:
                print("[CACHE] Файл кэша не найден, создаём новый...")
                cache_name = create_context_cache(GEMINI_API_KEYS[0], current_materials, CACHE_TTL)
                if cache_name:
                    with open(CACHE_NAME_FILE, 'w') as f:
                        f.write(cache_name)
                    print("[CACHE] Имя кэша сохранено в файл.")
            else:
                print("[CACHE] Явное создание кэша отключено, пропускаю.")
                cache_name = None
        ui_signals.show_signal.emit(indicator)
    except Exception as e:
        print(f"[MATERIAL] Ошибка загрузки: {e}")
        ui_signals.show_signal.emit("Ошибка загрузки")

def load_db():
    print("\n--- Загрузка лекций БД ---")
    load_materials(DB_FILE, 'DB', '@')

def load_java():
    print("\n--- Загрузка лекций Java ---")
    load_materials(JAVA_FILE, 'Java', '$')

def ask_gemini(image_bytes):
    return _gemini_client.ask(image_bytes, current_materials or "", current_type or "")

def clean_answer(raw):
    print("[CLEAN] Начинаю очистку ответа...")
    if looks_like_code_answer(raw):
        print("[CLEAN] Ответ похож на код, вызываю extract_code_answer.")
        return extract_code_answer(raw)

    text = raw
    if "===ОТВЕТЫ===" in raw:
        text = raw.split("===ОТВЕТЫ===")[-1]
        print("[CLEAN] Разделитель найден, беру часть после ===ОТВЕТЫ===.")

    text = text.strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return "ERR"

    is_test_format = any(re.match(r"^\d+:.*", l) for l in lines)

    if is_test_format:
        filtered = []
        for l in lines:
            match = re.match(r"^(\d+):(.*)", l)
            if match:
                num = match.group(1)
                ans = re.sub(r"[^A-Z0-9]", "", match.group(2).upper())
                if ans:
                    filtered.append(f"{num}:{ans}")
        if filtered:
            result = "\n".join(filtered)
            print(f"[CLEAN] Тестовый формат: {filtered}")
            return result

    print("[CLEAN] Неизвестный формат, возвращаю как текст.")
    return text

def extract_code_answer(raw):
    print("[CODE] Извлекаю чистый код...")
    text = raw.strip()
    if "```" in text:
        blocks = re.findall(r"```(?:[a-zA-Z0-9_+-]+)?\s*([\s\S]*?)```", text)
        if blocks:
            text = "\n".join(block.strip() for block in blocks if block.strip())
        else:
            text = text.replace("```", "").strip()

    if "===ОТВЕТЫ===" in text:
        text = text.split("===ОТВЕТЫ===")[-1].strip()

    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()

def looks_like_code_answer(answer):
    if not answer or answer == "ERR":
        return False
    if "```" in answer:
        return True
    if re.search(r"^\d+:[A-Z0-9]+$", answer, re.MULTILINE):
        return False

    markers = [
        "def ", "class ", "import ", "from ", "return ", "if ",
        "public ", "static ", "void ", "println", "System.out",
        "function ", "console.", "#include", "print(", "let ", "const ",
        "{", "}", ";", "(", ")", "[]",
        "select ", "insert ", "update ", "delete ", "from ", "where ",
        "create table", "join ", "group by", "order by"
    ]
    lowered = answer.lower()
    score = sum(1 for marker in markers if marker in lowered)
    sql_keywords = ["table", "primary key", "foreign key", "references", "values", "into"]
    score += sum(1 for kw in sql_keywords if kw in lowered)
    print(f"[CODE] Кодовый скор: {score}")
    return score >= 2


# ============================================================
# MANUAL ADVANCE TYPING v9 — ЮНИКОДНАЯ ПЕЧАТЬ ИЗ ПОТОКА
# ============================================================
# Как работает:
# 1) keyboard.write() шлёт ВИРТУАЛЬНЫЕ КОДЫ — на русской раскладке каша.
#    Используем SendInput с KEYEVENTF_UNICODE (не зависит от раскладки).
# 2) SendInput из контекста хука ловится самим хуком и блокируется.
#    Поэтому: хук кладёт символ в очередь -> поток-отправитель СНИМАЕТ хук,
#    шлёт символ, ставит хук обратно.
# 3) Выход:
#    * Right Shift + I  — пауза/выключение (вкл — глобальный хоткей toggle_typing,
#                         выкл — сам хук _stop_typing; защита от двойного срабатывания)
#    * Ctrl + Shift + Q — полный аварийный выход
#    * Right Shift + P  — скриншот (символ не печатается)
#    Модификаторы читаются напрямую keyboard.is_pressed(), поэтому
#    снятие/возврат хука между символами НЕ ломает определение выхода.

# ---------- ЮНИКОДНАЯ ОТПРАВКА ЧЕРЕЗ SendInput ----------
user32 = ctypes.WinDLL('user32', use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002

# ВАЖНО: union ДОЛЖЕН содержать MOUSEINPUT, иначе sizeof(INPUT) на x64 = 32
# вместо 40, и SendInput молча отклоняет ввод (cbSize != sizeof(INPUT)).
ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)

class MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR))

class KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR))

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD))

class _INPUT_union(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT),
                ("ki", KEYBDINPUT),
                ("hi", HARDWAREINPUT))

class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD),
                ("u", _INPUT_union))

user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

_INPUT_SIZE = ctypes.sizeof(INPUT)

def _send_inputs(*inputs):
    """Отправляет массив INPUT одним вызовом и проверяет результат."""
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, _INPUT_SIZE)
    if sent != n and DEBUG_TYPING:
        err = ctypes.get_last_error()
        print(f"[SENDINPUT] ОШИБКА: отправлено {sent}/{n}, GetLastError={err}, sizeof(INPUT)={_INPUT_SIZE}")
    return sent

def _key_vk(vk, down):
    flags = 0 if down else KEYEVENTF_KEYUP
    return INPUT(type=INPUT_KEYBOARD,
                 u=_INPUT_union(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags,
                                              time=0, dwExtraInfo=None)))

def _key_unicode(code, down):
    flags = KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP)
    return INPUT(type=INPUT_KEYBOARD,
                 u=_INPUT_union(ki=KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags,
                                              time=0, dwExtraInfo=None)))

def send_unicode_char(ch):
    """Отправляет ОДИН символ через SendInput.
    Обычные символы — KEYEVENTF_UNICODE (не зависит от раскладки),
    перевод строки и таб — реальными VK (Notepad/редакторы ждут именно их)."""
    if ch in ('\n', '\r'):
        _send_inputs(_key_vk(0x0D, True), _key_vk(0x0D, False))  # VK_RETURN
        return
    if ch == '\t':
        _send_inputs(_key_vk(0x09, True), _key_vk(0x09, False))  # VK_TAB
        return
    code = ord(ch)
    _send_inputs(_key_unicode(code, True), _key_unicode(code, False))


# ---------- УПРАВЛЕНИЕ ХУКОМ ----------
def remove_dynamic_hook():
    """Снимает динамический хук если он есть."""
    global _dynamic_hook_id
    if _dynamic_hook_id is not None:
        try:
            keyboard.unhook(_dynamic_hook_id)
        except Exception:
            pass
        _dynamic_hook_id = None


def sender_loop():
    """
    Фоновый поток печати. Берёт символы из очереди и шлёт через SendInput.

    Печать идёт ИЗ ЭТОГО потока (SendInput из контекста хука в окно не доходит),
    но хук НЕ снимается: keyboard игнорирует юникод-события (vk == VK_PACKET),
    поэтому наши символы проходят, а реальные клавиши остаются заблокированными
    хуком — никакой протечки чужих нажатий в текст.

    Enter/Tab шлются реальными VK и возвращаются в хук — перед отправкой
    помечаем их счётчиком _injected_special, чтобы хук их пропустил.
    """
    global _injected_special
    while True:
        ch = _type_queue.get()  # блокируемся, пока хук не положит символ
        if ch is None:
            continue
        if ch in ('\n', '\r', '\t'):
            with _injected_lock:
                _injected_special += 1
        try:
            send_unicode_char(ch)
        except Exception as e:
            print(f"[SENDER] Ошибка отправки {ch!r}: {e}")
        if DEBUG_TYPING:
            print(f"[SENDER] {ch!r} -> {auto_type_buffer_index}/{len(auto_type_buffer)}")
        time.sleep(0.004)  # темп печати; даём системе обработать символ


def _stop_typing(reason):
    """Останавливает печать из контекста хука и запоминает время остановки."""
    global auto_type_active, _last_stop_time, _injected_special
    remove_dynamic_hook()
    auto_type_active = False
    with _injected_lock:
        _injected_special = 0
    _last_stop_time = time.time()
    ui_signals.show_signal.emit(reason)
    print(f"[HOOK] Печать остановлена: {reason}")


def typing_blocker(event):
    """
    Хук во время активной печати. Каждое реальное нажатие -> один символ
    из буфера кладётся в очередь, а поток sender_loop печатает его. Реальная
    клавиша БЛОКИРУЕТСЯ, кроме комбинаций выхода.

    Почему через очередь и отдельный поток, а не SendInput прямо здесь:
      SendInput из контекста хука в окно не доставляется. Поэтому хук только
      кладёт символ в очередь и сразу возвращается, а печатает sender_loop.

    Хук НЕ снимается во время печати: keyboard сам игнорирует юникод-символы
    (vk == VK_PACKET), поэтому реальные клавиши остаются заблокированными
    (нет протечки чужих нажатий). Enter/Tab (реальные VK) пропускаются по
    счётчику _injected_special.

    Выход: Right Shift+I — пауза; Esc — аварийная пауза;
    Ctrl+Shift+Q — полный выход; Right Shift+P — скриншот.

    ВАЖНО про keyboard 0.13.5: blocking-хук должен вернуть
      True  -> ПРОПУСТИТЬ клавишу в программу,
      False -> ЗАБЛОКИРОВАТЬ клавишу.
    Поэтому модификаторы и наши синтетические Enter/Tab возвращают True,
    а реальные клавиши и комбинации выхода — False.

    Выход:
      * Right Shift + I   — пауза/выключение
      * Ctrl + Shift + Q  — полный аварийный выход
      * Right Shift + P   — скриншот (символ не печатается)
    """
    global auto_type_buffer, auto_type_buffer_index, auto_type_active, _injected_special

    name = event.name
    # При зажатом Shift keyboard отдаёт имя буквы в ВЕРХНЕМ регистре ('I', 'Q'),
    # поэтому всё приводим к нижнему регистру.
    name_l = name.lower() if name else name
    down = event.event_type == 'down'

    # Модификаторы: сами ведём учёт зажатых (и down, и up) и всегда пропускаем.
    if name_l in _MOD_NAMES:
        if down:
            _held_mods.add(name_l)
        else:
            _held_mods.discard(name_l)
        return True

    # Отпускания обычных клавиш пропускаем (иначе клавиши "залипнут")
    if not down:
        return True

    # Пропускаем наши собственные синтетические Enter/Tab (их шлёт sender_loop).
    if name_l in ('enter', 'return', 'tab'):
        with _injected_lock:
            if _injected_special > 0:
                _injected_special -= 1
                return True

    # Состояние модификаторов — из нашего набора (надёжнее, чем is_pressed).
    rs_now = 'right shift' in _held_mods
    any_ctrl = bool(_held_mods & _CTRL_NAMES)
    any_shift = bool(_held_mods & _SHIFT_NAMES)

    if DEBUG_TYPING:
        print(f"[HOOK] key={name!r} rs={rs_now} ctrl={any_ctrl} "
              f"idx={auto_type_buffer_index}/{len(auto_type_buffer)}")

    # ===== АВАРИЙНЫЙ ВЫХОД: Ctrl + Shift + Q =====
    if name_l == 'q' and any_ctrl and any_shift:
        auto_type_active = False
        print("[HOOK] Ctrl+Shift+Q — полный выход")
        shutdown()
        return False  # блокируем 'q'

    # ===== ПАУЗА: Right Shift + I  или  Esc (аварийно) =====
    if (name_l == 'i' and rs_now) or name_l == 'esc':
        _stop_typing("ПАУЗА")
        return False

    # ===== Right Shift + P — скриншот, символ не печатаем =====
    if name_l == 'p' and rs_now:
        return False

    # ===== КОНЕЦ БУФЕРА =====
    if auto_type_buffer_index >= len(auto_type_buffer):
        _stop_typing("ЗАВЕРШЕНО")
        return False

    # ===== КЛАДЁМ СЛЕДУЮЩИЙ СИМВОЛ В ОЧЕРЕДЬ, реальную клавишу блокируем =====
    ch = auto_type_buffer[auto_type_buffer_index]
    auto_type_buffer_index += 1
    _type_queue.put(ch)  # печать выполнит sender_loop

    # Последний символ: снимаем хук (sender_loop допечатает остаток очереди).
    if auto_type_buffer_index >= len(auto_type_buffer):
        _stop_typing("ЗАВЕРШЕНО")

    return False  # блокируем реальную клавишу


def toggle_typing():
    """
    Глобальный хоткей Right Shift + I.
    ВКЛЮЧАЕТ печать. Выключение делает сам хук (_stop_typing),
    поэтому здесь только включаем — и игнорируем повторный вызов,
    если печать уже идёт или хук только что её остановил тем же нажатием.
    """
    global auto_type_active, _dynamic_hook_id, _injected_special

    # Уже печатаем — выключение обработает хук, ничего не делаем.
    if auto_type_active:
        return

    # Защита от того же самого нажатия Right Shift+I, которым хук
    # только что поставил паузу (хоткей и хук ловят одно событие).
    if time.time() - _last_stop_time < 0.4:
        return

    if not auto_type_buffer:
        print("[TOGGLE] Буфер пуст, нечего печатать.")
        return

    if auto_type_buffer_index >= len(auto_type_buffer):
        print("[TOGGLE] Буфер закончился, сброс.")
        remove_dynamic_hook()
        ui_signals.show_signal.emit("ЗАВЕРШЕНО")
        return

    auto_type_active = True
    with _injected_lock:
        _injected_special = 0
    _held_mods.clear()  # сбрасываем стейт модификаторов прошлой сессии
    # Чистим очередь от возможных остатков прошлой сессии.
    try:
        while True:
            _type_queue.get_nowait()
    except queue.Empty:
        pass
    remove_dynamic_hook()
    _dynamic_hook_id = keyboard.hook(typing_blocker, suppress=True)
    ui_signals.show_signal.emit("ПЕЧАТЬ...")
    print(f"[TOGGLE] Режим печати ВКЛЮЧЁН. Буфер: {auto_type_buffer_index}/{len(auto_type_buffer)}")


def on_screenshot():
    """Right Shift + P — скриншот"""
    process_test()


def on_exit():
    """Ctrl + Shift + Q — выход"""
    shutdown()


def on_load_db():
    """Ctrl + Shift + D — загрузить лекции БД"""
    load_db()


def on_load_java():
    """Ctrl + Shift + J — загрузить лекции Java"""
    load_java()


def start_auto_typing(text):
    """
    Загружает ответ ИИ в буфер. Показывает "!".
    Режим включается отдельно по Right Shift+I.
    """
    global auto_type_buffer, auto_type_buffer_index, auto_type_active

    if not text or text == "ERR":
        return

    print(f"[MANUAL] Загружаю ответ в буфер ({len(text)} символов)")

    # Если печать была активна — корректно выключаем перед загрузкой нового буфера
    if auto_type_active:
        remove_dynamic_hook()
    auto_type_buffer = text
    auto_type_buffer_index = 0
    auto_type_active = False

    ui_signals.show_signal.emit("!")
    print(f"[MANUAL] Буфер готов! Нажми Right Shift+I для включения.")
    print(f"[MANUAL] Буфер: {auto_type_buffer[:80]}...")


def process_test():
    global is_processing
    if is_processing:
        ui_signals.show_signal.emit("...")  # уже считаем — просто подтверждаем
        return
    is_processing = True
    ui_signals.show_signal.emit("...")  # короткое подтверждение, что скрин снят

    def background_task():
        global is_processing
        try:
            screenshot = ImageGrab.grab()
            img_byte_arr = io.BytesIO()
            screenshot.save(img_byte_arr, format='PNG')
            ans = ask_gemini(img_byte_arr.getvalue())

            if looks_like_code_answer(ans):
                start_auto_typing(ans)
            else:
                ui_signals.show_signal.emit(ans)
        except Exception as e:
            ui_signals.show_signal.emit(f"ERROR: {e}")
        finally:
            is_processing = False

    threading.Thread(target=background_task, daemon=True).start()


def register_hotkeys():
    """Регистрирует все горячие клавиши"""
    keyboard.add_hotkey('ctrl+shift+q', on_exit, suppress=True)
    keyboard.add_hotkey('ctrl+shift+d', on_load_db, suppress=True)
    keyboard.add_hotkey('ctrl+shift+j', on_load_java, suppress=True)
    keyboard.add_hotkey('right shift+p', on_screenshot, suppress=True)
    keyboard.add_hotkey('right shift+i', toggle_typing, suppress=True)
    print("[HOTKEYS] Все горячие клавиши зарегистрированы")


def shutdown():
    remove_dynamic_hook()
    try:
        keyboard.unhook_all()
    except Exception:
        pass
    os._exit(0)


def disable_quick_edit():
    """Отключает QuickEdit/Insert у консоли.

    Без этого ЛЮБОЙ клик по окну консоли (или случайное выделение) переводит
    её в режим выделения и БЛОКИРУЕТ stdout — а вместе с ним и весь процесс,
    включая клавиатурный хук. Снаружи это выглядит как полное "залипание".
    """
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        STD_INPUT_HANDLE = -10
        ENABLE_EXTENDED_FLAGS = 0x0080
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_INSERT_MODE = 0x0020
        h = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if not h or h == ctypes.c_void_p(-1).value:
            return
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            return  # нет консоли (запуск через pythonw) — нечего отключать
        new_mode = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE & ~ENABLE_INSERT_MODE
        kernel32.SetConsoleMode(h, new_mode)
        print("[CONSOLE] QuickEdit отключён (защита от заморозки по клику).")
    except Exception as e:
        print(f"[CONSOLE] Не удалось отключить QuickEdit: {e}")


class TransparentWindow(QWidget):
    """Прозрачный оверлей с серым текстом (как раньше). Удерживается поверх
    всех окон таймером + WinAPI SetWindowPos(TOPMOST)."""
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)   # не воровать фокус
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # клики проходят насквозь
        self.label = QLabel("", self)
        self.label.setStyleSheet(f"color: {UI_COLOR}; background-color: rgba(0, 0, 0, 0); padding: 5px;")
        self.label.setFont(QFont("Consolas", UI_FONT_SIZE))
        self.label.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        self.setGeometry(MARGIN_LEFT,
                         QApplication.desktop().height() - WINDOW_HEIGHT - MARGIN_BOTTOM,
                         WINDOW_WIDTH, WINDOW_HEIGHT)
        ui_signals.show_signal.connect(self.display_text)
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide_text)
        # Держим оверлей поверх всех окон (некоторые программы перехватывают верх).
        self.top_timer = QTimer()
        self.top_timer.timeout.connect(self._keep_on_top)
        self.top_timer.start(700)

    def _keep_on_top(self):
        if not self.isVisible():
            return
        self.raise_()
        try:
            hwnd = int(self.winId())
            HWND_TOPMOST = -1
            SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            pass

    def display_text(self, text):
        self.label.setText(text)
        self.label.adjustSize()
        self.show()
        self.raise_()
        self._keep_on_top()
        self.timer.start(10000 if "КОД ГОТОВ" in text else DURATION_MS)

    def hide_text(self):
        self.label.setText("")


if __name__ == "__main__":
    print("=" * 60)
    print("Запуск ассистента v19 (надёжная пауза Right Shift+I — свой трекинг мод.)...")
    print("=" * 60)
    disable_quick_edit()
    register_hotkeys()

    # Поток печати: символы из очереди -> SendInput (вне контекста хука).
    threading.Thread(target=sender_loop, daemon=True).start()

    app = QApplication(sys.argv)
    window = TransparentWindow()
    ui_signals.show_signal.emit("#")
    sys.exit(app.exec_())
