from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def ensure_dir(path: str | Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_yaml_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def sanitize_for_json(value: Any) -> Any:
    try:
        import numpy as np
        import pandas as pd
    except Exception:  # pragma: no cover - optional import guard
        np = None
        pd = None

    if pd is not None and isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if np is not None and isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_json(v) for v in value]
    return value


def write_json(path: str | Path, data: dict[str, Any]) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(sanitize_for_json(data), handle, indent=2, ensure_ascii=False, sort_keys=True)


def read_json(path: str | Path, default: Any | None = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_dataframe_csv(path: str | Path, frame: Any) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    frame.to_csv(path, index=True, index_label="timestamp")


def read_dataframe_csv(path: str | Path, parse_dates: bool = True):
    import pandas as pd

    kwargs = {"index_col": "timestamp"}
    if parse_dates:
        kwargs["parse_dates"] = ["timestamp"]
    frame = pd.read_csv(path, **kwargs)
    if parse_dates:
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        else:
            frame.index = frame.index.tz_convert("UTC")
    return frame
