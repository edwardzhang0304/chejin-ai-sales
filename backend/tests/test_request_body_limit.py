import asyncio
import json

from app.main import C2IngestBodyLimitMiddleware


def test_ingest_stream_is_rejected_without_content_length_before_full_body_is_read():
    inner_called = False
    received_messages = 0

    async def inner_app(scope, receive, send):
        nonlocal inner_called, received_messages
        inner_called = True
        while True:
            message = await receive()
            received_messages += 1
            if not message.get("more_body"):
                break

    chunks = [
        {"type": "http.request", "body": b"123456", "more_body": True},
        {"type": "http.request", "body": b"789012", "more_body": True},
        {"type": "http.request", "body": b"must-not-be-read", "more_body": False},
    ]
    sent: list[dict] = []

    async def receive():
        return chunks.pop(0)

    async def send(message):
        sent.append(message)

    middleware = C2IngestBodyLimitMiddleware(inner_app, max_bytes=10)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/workers/worker-1/wechat/messages/ingest",
        "headers": [],
    }

    asyncio.run(middleware(scope, receive, send))

    assert inner_called is True
    assert received_messages == 1
    assert len(chunks) == 1
    start = next(item for item in sent if item["type"] == "http.response.start")
    body = next(item for item in sent if item["type"] == "http.response.body")
    assert start["status"] == 413
    payload = json.loads(body["body"])
    assert payload["code"] == "MESSAGE_INGEST_REQUEST_TOO_LARGE"
    assert payload["data"]["retryable"] is False
