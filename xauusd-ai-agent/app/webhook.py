import asyncio
import hmac
import json
import time
from collections import deque
from threading import Lock

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .database import SignalConflict
from .models import Signal

router = APIRouter()


def authenticate(provided: str, expected: str):
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(401, "Unauthorized")


def bearer(request: Request):
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token if scheme.lower() == "bearer" else ""


class RateLimiter:
    """One bounded, process-local budget for authenticated webhook traffic."""

    def __init__(self, maximum: int):
        self.maximum = maximum
        self.events = deque()
        self.lock = Lock()

    def check(self):
        now = time.monotonic()
        with self.lock:
            while self.events and self.events[0] <= now - 60:
                self.events.popleft()
            if len(self.events) >= self.maximum:
                raise HTTPException(429, "Webhook rate limit exceeded", headers={"Retry-After": "60"})
            self.events.append(now)


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


async def read_json(request: Request, max_body_bytes: int):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Content-Type must be application/json")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HTTPException(415, "Compressed request bodies are not supported")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            parsed_length = int(length)
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length") from None
        if parsed_length < 0:
            raise HTTPException(400, "Invalid Content-Length")
        if parsed_length > max_body_bytes:
            raise HTTPException(413, "Request body is too large")
    body = bytearray()
    try:
        async with asyncio.timeout(1.0):
            async for chunk in request.stream():
                if len(body) + len(chunk) > max_body_bytes:
                    raise HTTPException(413, "Request body is too large")
                body.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, "Request body timeout") from None
    except ClientDisconnect:
        raise HTTPException(400, "Incomplete request") from None
    try:
        payload = json.loads(body.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        raise HTTPException(400, "Invalid JSON") from None
    return payload


async def receive(request: Request):
    settings = request.app.state.settings
    request.app.state.limiter.check()
    payload = await read_json(request, settings.max_body_bytes)
    try:
        signal = Signal.model_validate(payload)
    except ValidationError as exc:
        # Do not echo submitted payloads or unknown fields back into logs/clients.
        errors = [{"field": ".".join(map(str, e["loc"])), "type": e["type"]} for e in exc.errors()]
        raise HTTPException(422, {"message": "Invalid signal", "errors": errors}) from None
    if signal.symbol not in settings.allowed_symbols:
        raise HTTPException(422, "Symbol is not allowed")
    age = time.time() - signal.bar_close_ms / 1000
    if age > settings.max_signal_age_seconds or age < -settings.max_future_seconds:
        raise HTTPException(422, "Signal timestamp is stale or in the future")
    try:
        record_id, created = await run_in_threadpool(request.app.state.db.insert, signal)
    except SignalConflict:
        raise HTTPException(409, "signal_id already exists with different data") from None
    return JSONResponse(status_code=201 if created else 200, content={
        "id": record_id, "signal_id": signal.signal_id,
        "status": "received", "duplicate": not created,
    })


@router.post("/webhook")
async def webhook(request: Request):
    authenticate(bearer(request), request.app.state.settings.webhook_token)
    return await receive(request)


@router.post("/webhook/tradingview/{token}", include_in_schema=False)
async def tradingview_webhook(token: str, request: Request):
    # Capability URL for TradingView's URL + JSON alert form. Never log this URL.
    authenticate(token, request.app.state.settings.webhook_token)
    return await receive(request)
