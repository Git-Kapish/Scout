from scout.config import get_settings


def test_settings_load():
    settings = get_settings()
    assert settings.version == "1.0"
    assert "planner" in settings.roles
    assert settings.get_role_primary("planner").model == "nvidia/nemotron-3.5-super-vl:free"
    assert settings.budgets["default"].total_llm_calls == 40
    assert settings.minimum_evidence.min_verified_findings == 4


def test_no_hardcoded_model():
    settings = get_settings()
    for role, entry in settings.roles.items():
        assert entry.primary.model != "", f"Role {role} missing model config"
