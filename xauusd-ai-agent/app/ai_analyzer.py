import asyncio
import json
import math
from typing import Annotated, Literal

import httpx
from pydantic import Field, ValidationError

from .config import ROOT, Settings
from .models import Price, Signal, StrictModel
from .structure import confirmation_blockers
from .webhook import reject_constant, unique_keys

PROMPT_VERSION = "signal-filter-v4"


class AnalysisResult(StrictModel):
    decision: Literal["BUY", "SELL", "WAIT"]
    entry: Price
    sl: Price
    tp: Price
    risk_reward: Price
    reasons: Annotated[list[Annotated[str, Field(min_length=1, max_length=500)]], Field(min_length=1, max_length=8)]


class AnalysisError(Exception):
    def __init__(self, code: str, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def check_analysis(result:AnalysisResult,signal:Signal)->AnalysisResult:
    """The same price, direction and confirmation gates for every AI provider."""
    for field in ('entry','sl','tp','risk_reward'):
        if not math.isclose(getattr(result,field),getattr(signal,field),rel_tol=0,abs_tol=1e-8):
            raise AnalysisError('modified_prices_or_rr')
    if result.decision not in (signal.side,'WAIT'):
        raise AnalysisError('reversed_signal')
    if confirmation_blockers(signal) and result.decision!='WAIT':
        raise AnalysisError('unsupported_confirmation')
    return result


class AIAnalyzer:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.prompt = (ROOT / "prompts/trading_analysis.txt").read_text()

    async def analyze(self, signal: Signal) -> AnalysisResult:
        evidence = {"signal": signal.model_dump(), "confirmation_blockers": confirmation_blockers(signal)}
        request = {
            "model": self.settings.lm_studio_model,
            "messages": [{"role": "system", "content": self.prompt},
                         {"role": "user", "content": json.dumps(evidence, allow_nan=False)}],
            "temperature": 0, "max_tokens": 2048, "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "signal_filter", "strict": True, "schema": AnalysisResult.model_json_schema(),
            }},
        }
        if self.settings.ai_reasoning_effort is not None:
            request["reasoning_effort"] = self.settings.ai_reasoning_effort
        try:
            async with asyncio.timeout(self.settings.ai_timeout_seconds):
                async with self.client.stream("POST", self.settings.lm_studio_url.rstrip("/") + "/v1/chat/completions", json=request) as response:
                    if response.status_code != 200:
                        raise AnalysisError(f"http_{response.status_code}", response.status_code in (408, 429) or response.status_code >= 500)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 131072:
                            raise AnalysisError("response_too_large")
                        body.extend(chunk)
        except (TimeoutError, httpx.TimeoutException):
            raise AnalysisError("model_timeout", True) from None
        except httpx.RequestError:
            raise AnalysisError("model_unreachable", True) from None
        try:
            envelope = json.loads(body, parse_constant=reject_constant, object_pairs_hook=unique_keys)
            choice = envelope["choices"][0]
            if choice["finish_reason"] != "stop":
                raise AnalysisError("incomplete_response")
            # Never parse reasoning_content or execute any model-requested tools.
            if choice["message"].get("tool_calls"):
                raise AnalysisError("unexpected_tool_call")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise AnalysisError("invalid_output")
            data = json.loads(content, parse_constant=reject_constant, object_pairs_hook=unique_keys)
            result = AnalysisResult.model_validate(data)
        except (KeyError, IndexError, TypeError, ValueError, RecursionError, ValidationError):
            raise AnalysisError("invalid_output") from None
        return check_analysis(result,signal)
