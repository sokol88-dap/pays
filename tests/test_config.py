from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import Settings, get_settings


def test_settings_loads_api_key_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "environment-key")
    get_settings.cache_clear()
    try:
        assert get_settings().api_key.get_secret_value() == "environment-key"
    finally:
        get_settings.cache_clear()


def test_settings_loads_api_key_from_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("API_KEY", raising=False)
    (tmp_path / ".env").write_text("API_KEY=dotenv-key\n")
    get_settings.cache_clear()
    try:
        assert get_settings().api_key.get_secret_value() == "dotenv-key"
    finally:
        get_settings.cache_clear()


def test_settings_still_rejects_missing_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings.model_validate({})
