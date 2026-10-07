from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite-preview",
)


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise ValueError(f"{path.name}:{number}: ожидается ИМЯ=значение")
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"{path.name}:{number}: незакрытая кавычка")
            value = value[1:-1]
        values[key.strip()] = value
    return values


def split_values(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


@dataclass(frozen=True)
class Config:
    api_keys: tuple[str, ...] = field(default=(), repr=False)
    models: tuple[str, ...] = DEFAULT_MODELS
    materials_dir: Path = ROOT / "MyAssistant"
    paste_shortcut: str = "ctrl+v"
    request_timeout: float = 45.0

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None, env_file: Path | None = None):
        values = read_env(env_file if env_file is not None else ROOT / ".env")
        environment = os.environ if environ is None else environ
        if "GEMINI_API_KEYS" in environment:
            raw_keys = environment["GEMINI_API_KEYS"]
        elif "GEMINI_API_KEY" in environment:
            raw_keys = environment["GEMINI_API_KEY"]
        else:
            raw_keys = values.get("GEMINI_API_KEYS", values.get("GEMINI_API_KEY", ""))
        values.update(environment)
        keys = split_values(raw_keys)
        models = split_values(values.get("GEMINI_MODELS", ",".join(DEFAULT_MODELS)))
        if not models or any(not re.fullmatch(r"[A-Za-z0-9._-]+", model) for model in models):
            raise ValueError("GEMINI_MODELS: укажите имена моделей через запятую")
        shortcut = values.get("CLONEF_PASTE_SHORTCUT", "ctrl+v").lower()
        if shortcut not in ("ctrl+v", "ctrl+shift+v"):
            raise ValueError("CLONEF_PASTE_SHORTCUT: допустимы ctrl+v и ctrl+shift+v")
        raw_dir = values.get("CLONEF_MATERIALS_DIR")
        materials_dir = Path(raw_dir).expanduser() if raw_dir else ROOT / "MyAssistant"
        if not materials_dir.is_absolute():
            materials_dir = ROOT / materials_dir
        return cls(api_keys=keys, models=models, materials_dir=materials_dir, paste_shortcut=shortcut)

    def material_path(self, kind: str) -> Path:
        names = {"DB": "all_materials.txt", "Java": "java_materials.txt"}
        return self.materials_dir / names[kind]
