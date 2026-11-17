from app.config import Settings


def test_settings_nested_get():
    settings = Settings({"a": {"b": 3}})
    assert settings.get("a.b") == 3
    assert settings.get("a.c", "fallback") == "fallback"
