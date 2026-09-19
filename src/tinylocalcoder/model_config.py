"""Resolve the active Ollama model from the repo's global config.toml."""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_KEY = "qwen2.5"
DEFAULT_OLLAMA = "qwen2.5:3b"
DEFAULT_NUM_CTX = 2048
DEFAULT_URL = "https://ollama.com/library/qwen2.5:3b"


@dataclass(frozen=True)
class ModelSpec:
    key: str
    ollama: str
    num_ctx: int
    url: str
    min_ram_gb: int = 0


def default_spec() -> ModelSpec:
    return ModelSpec(
        key=DEFAULT_KEY,
        ollama=DEFAULT_OLLAMA,
        num_ctx=DEFAULT_NUM_CTX,
        url=DEFAULT_URL,
    )


def find_config_path(explicit: str | Path | None = None) -> Path | None:
    if explicit is not None:
        path = Path(explicit)
        return path if path.is_file() else None
    env = os.environ.get("TLC_CONFIG")
    if env:
        path = Path(env)
        return path if path.is_file() else None
    here = Path(__file__).resolve()
    candidates = [Path("/app/config.toml"), Path.cwd() / "config.toml"]
    try:
        candidates.insert(1, here.parents[2] / "config.toml")
    except IndexError:
        pass
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _parse_spec(key: str, raw: object) -> ModelSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"models.{key} must be a table")
    ollama = str(raw.get("ollama") or "").strip()
    if not ollama:
        raise ValueError(f"models.{key}.ollama is required")
    num_ctx = int(raw.get("num_ctx") or DEFAULT_NUM_CTX)
    url = str(raw.get("url") or f"https://ollama.com/library/{ollama}").strip()
    min_ram_gb = int(raw.get("min_ram_gb") or 0)
    return ModelSpec(
        key=key, ollama=ollama, num_ctx=num_ctx, url=url, min_ram_gb=min_ram_gb
    )


def load_catalog(path: Path) -> tuple[str, dict[str, ModelSpec]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_models = data.get("models")
    if not isinstance(raw_models, dict) or not raw_models:
        raise ValueError(f"{path} has no [models.*] entries")
    catalog = {key: _parse_spec(key, spec) for key, spec in raw_models.items()}
    key = str(data.get("model") or DEFAULT_KEY).strip()
    return key, catalog


def load_model_choice(
    path: str | Path | None = None,
    *,
    required: bool = False,
) -> ModelSpec:
    config_path = find_config_path(path)
    if config_path is None:
        if required:
            hint = Path(path) if path is not None else "config.toml"
            raise FileNotFoundError(f"{hint} not found")
        return default_spec()
    key, catalog = load_catalog(config_path)
    if key not in catalog:
        options = ", ".join(sorted(catalog))
        raise ValueError(
            f"Unknown model {key!r} in {config_path}. Choose one of: {options}"
        )
    return catalog[key]


def iter_models(path: str | Path | None = None) -> list[ModelSpec]:
    config_path = find_config_path(path)
    if config_path is None:
        return [default_spec()]
    _, catalog = load_catalog(config_path)
    return [catalog[key] for key in catalog]


def export_env(spec: ModelSpec) -> str:
    return (
        f"MODEL_NAME={spec.ollama}\n"
        f"NUM_CTX={spec.num_ctx}\n"
        f"TLC_MODEL_KEY={spec.key}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config",
        nargs="?",
        help="Path to config.toml (default: search TLC_CONFIG / repo root)",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Print MODEL_NAME / NUM_CTX / TLC_MODEL_KEY for eval",
    )
    args = parser.parse_args(argv)
    try:
        spec = load_model_choice(args.config, required=bool(args.config))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.export:
        sys.stdout.write(export_env(spec))
        return 0
    print(f"{spec.key} → {spec.ollama}  (num_ctx={spec.num_ctx})")
    print(spec.url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
