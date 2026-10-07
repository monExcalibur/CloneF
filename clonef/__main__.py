from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from .config import Config


def doctor(config: Config) -> int:
    dependencies = {name: importlib.util.find_spec(name) is not None for name in ("requests", "PIL", "PyQt5")}
    session = os.environ.get("XDG_SESSION_TYPE") or ("wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11" if os.environ.get("DISPLAY") else "unknown")
    materials = {kind: config.material_path(kind).is_file() for kind in ("DB", "Java")}
    result = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "session": session,
        "api_keys_configured": len(config.api_keys),
        "models": config.models,
        "dependencies": dependencies,
        "materials": materials,
    }
    if sys.platform == "linux":
        result["evdev_installed"] = importlib.util.find_spec("evdev") is not None
        result["uinput_writable"] = os.access("/dev/uinput", os.W_OK)
        result["input_events_readable"] = sum(os.access(path, os.R_OK) for path in Path("/dev/input").glob("event*"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(dependencies.values()) and all(materials.values()) and config.api_keys else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CloneF: изображение и лекции → ответ Gemini")
    parser.add_argument("--doctor", action="store_true", help="Проверить зависимости и настройки без захвата экрана/клавиатуры")
    parser.add_argument("--image", type=Path, help="Обработать файл изображения без запуска GUI")
    parser.add_argument("--materials", choices=("DB", "Java"), help="Лекции для --image или начальная загрузка в GUI")
    args = parser.parse_args(argv)
    try:
        config = Config.load()
        if args.doctor:
            return doctor(config)
        if args.image:
            from .gemini import GeminiClient
            materials = config.material_path(args.materials).read_text(encoding="utf-8") if args.materials else ""
            print(GeminiClient(config).ask(args.image.read_bytes(), materials, args.materials or ""))
            return 0
        if sys.platform == "linux" and (os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("WAYLAND_DISPLAY")):
            os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
        from PyQt5.QtWidgets import QApplication
        from .desktop import AssistantWindow
        app = QApplication([sys.argv[0]])
        window = AssistantWindow(config)
        if args.materials:
            window.load_materials(args.materials)
        window.show()
        return app.exec_()
    except (OSError, ValueError, ImportError, RuntimeError) as error:
        print(f"CloneF: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
