import httpx

from src.api import create_app
from src.config import Settings


async def test_swagger_ui_is_available_without_an_api_key(
    settings: Settings,
) -> None:
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/docs")
    await app.state.database.close()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "SwaggerUIBundle" in response.text
    assert "/openapi.json" in response.text


async def test_openapi_schema_describes_api_key_authentication(
    settings: Settings,
) -> None:
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/openapi.json")
    await app.state.database.close()

    assert response.status_code == 200
    schema = response.json()
    assert schema["components"]["securitySchemes"]["APIKeyHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }
    for path, method in (
        ("/api/v1/payments", "post"),
        ("/api/v1/payments/{payment_id}", "get"),
    ):
        assert schema["paths"][path][method]["security"] == [
            {"APIKeyHeader": []}
        ]
