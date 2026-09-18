"""Resolve qwen2.5 / qwen3.5 from the global config.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from tinylocalcoder.model_config import (
    export_env,
    iter_models,
    load_model_choice,
    main,
)

SAMPLE = """\
model = "{key}"

[models."qwen2.5"]
ollama = "qwen2.5:3b"
num_ctx = 2048
url = "https://ollama.com/library/qwen2.5:3b"

[models."qwen3.5"]
ollama = "qwen3.5:4b"
num_ctx = 2048
url = "https://ollama.com/library/qwen3.5:4b"
"""


def _write(tmp_path: Path, key: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE.format(key=key), encoding="utf-8")
    return path


def test_load_qwen25(tmp_path: Path) -> None:
    spec = load_model_choice(_write(tmp_path, "qwen2.5"), required=True)
    assert spec.key == "qwen2.5"
    assert spec.ollama == "qwen2.5:3b"
    assert spec.num_ctx == 2048


def test_load_qwen35(tmp_path: Path) -> None:
    spec = load_model_choice(_write(tmp_path, "qwen3.5"), required=True)
    assert spec.key == "qwen3.5"
    assert spec.ollama == "qwen3.5:4b"
    assert spec.url.endswith("qwen3.5:4b")


def test_unknown_model(tmp_path: Path) -> None:
    path = _write(tmp_path, "nope")
    with pytest.raises(ValueError, match="Unknown model"):
        load_model_choice(path, required=True)


def test_export_env(tmp_path: Path) -> None:
    spec = load_model_choice(_write(tmp_path, "qwen3.5"), required=True)
    env = export_env(spec)
    assert "MODEL_NAME=qwen3.5:4b" in env
    assert "NUM_CTX=2048" in env
    assert "TLC_MODEL_KEY=qwen3.5" in env


def test_iter_models(tmp_path: Path) -> None:
    keys = [m.key for m in iter_models(_write(tmp_path, "qwen2.5"))]
    assert keys == ["qwen2.5", "qwen3.5"]


def test_cli_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, "qwen3.5")
    assert main(["--export", str(path)]) == 0
    out = capsys.readouterr().out
    assert "MODEL_NAME=qwen3.5:4b" in out


def test_repo_config_catalog() -> None:
    repo_config = Path(__file__).resolve().parents[1] / "config.toml"
    keys = [m.key for m in iter_models(repo_config)]
    assert keys == ["qwen2.5", "qwen3.5"]
    spec = load_model_choice(repo_config, required=True)
    assert spec.key in keys
    assert spec.ollama in {"qwen2.5:3b", "qwen3.5:4b"}
    by_key = {m.key: m for m in iter_models(repo_config)}
    assert by_key["qwen3.5"].min_ram_gb >= 8


def test_format_llm_oom_error() -> None:
    from tinylocalcoder.llm import format_llm_error

    msg = format_llm_error(
        RuntimeError(
            "ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate "
            "buffer of size 2631813120"
        ),
        "qwen3.5:4b",
    )
    assert "out of RAM" in msg
    assert "colima-start-stop-on-mac.sh" in msg
    assert "qwen3.5:4b" in msg
