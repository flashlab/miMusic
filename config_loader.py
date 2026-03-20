from __future__ import annotations

import json
import os
from typing import Any


def _resolve_path(base_dir: str, path_value: str) -> str:
    if not path_value:
        return path_value
    if os.path.isabs(path_value):
        return path_value
    return os.path.abspath(os.path.join(base_dir, path_value))


def load_config(config_path: str) -> dict[str, Any]:
    if not str(config_path).strip():
        raise ValueError("Config path is required.")

    resolved_config_path = os.path.abspath(config_path)
    if not os.path.isfile(resolved_config_path):
        raise FileNotFoundError(f"Config file does not exist: {resolved_config_path}")

    with open(resolved_config_path, "r", encoding="utf-8") as file:
        loaded = json.load(file)

    if not isinstance(loaded, dict):
        raise ValueError(f"Config file must contain a JSON object: {resolved_config_path}")

    config = loaded
    config_dir = os.path.dirname(resolved_config_path)

    config["music_dirs"] = [
        _resolve_path(config_dir, str(path).strip())
        for path in (config.get("music_dirs") or [])
        if str(path).strip()
    ]

    search_config = config.get("search") or {}
    index_file = str(search_config.get("index_file") or "").strip()
    if index_file:
        search_config["index_file"] = _resolve_path(config_dir, index_file)
    config["search"] = search_config

    return config
