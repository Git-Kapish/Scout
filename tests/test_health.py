from fastapi.testclient import TestClient

from scout.api import app


def test_health_endpoint():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("ok", "degraded")
    assert "redis" in data
    assert "providers" in data
    assert data["providers"]["openrouter"]["model"] == "deepseek/deepseek-v4.1-flash-1017"
