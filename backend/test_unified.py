"""Exercise merged HTTP routes without calling Gemini or starting a second fleet."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from backend.live import app
from backend.main import app as main_app


async def request(path, method="GET", payload=None, headers=()):
    messages = []
    body = json.dumps(payload).encode() if payload is not None else b""

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
               "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
               "query_string": b"", "root_path": "", "server": ("localhost", 8000),
               "client": ("127.0.0.1", 1234),
               "headers": [(b"content-type", b"application/json"), *headers]}, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    try:
        result = json.loads(data) if data else None
    except json.JSONDecodeError:
        result = data.decode()
    return status, result


class UnifiedTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_entrypoints_share_health_state_and_investigator(self):
        self.assertIs(app, main_app)
        status, data = await request("/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, state = await request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["type"], "state")
        _, schema = await request("/openapi.json")
        self.assertTrue({"/health", "/investigate", "/api/state"} <= set(schema["paths"]))
        self.assertTrue(any(getattr(route, "path", None) == "/ws" for route in app.routes))

    async def test_investigator_receives_original_question_and_packet(self):
        packet = {"id": "F1", "events": []}
        expected = {"diagnosis": {"summary": "Test diagnosis"}, "error": None}
        with patch("backend.investigator_api.investigate", new_callable=AsyncMock, return_value=expected) as mock:
            status, result = await request("/investigate", "POST", {"question": "What happened?", "packet": packet})
            self.assertEqual(status, 200)
            self.assertEqual(result, expected)
            mock.assert_awaited_once_with("What happened?", packet)

    async def test_invalid_packet_is_rejected_without_calling_gemini(self):
        with patch("backend.investigator_api.investigate", new_callable=AsyncMock) as mock:
            status, _ = await request("/investigate", "POST", {"question": "Why?", "packet": "invalid"})
            self.assertEqual(status, 422)
            mock.assert_not_awaited()

    async def test_vite_origin_can_call_investigator_directly(self):
        status, _ = await request("/investigate", "OPTIONS", headers=[
            (b"origin", b"http://localhost:5173"), (b"access-control-request-method", b"POST")])
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
