"""Calls to Claude: one request per extraction job, sent directly or through the Batch API.

Large runs (at least `llm.batch_min_requests` requests) go through the Message Batches
API, which costs half as much and usually finishes within an hour. Small runs are sent
directly, a few at a time, so a run with a handful of new messages finishes in seconds.

Also here: the price table, the characters ÷ 4 token estimate, and a running usage and
cost total for the run summary.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from kb.errors import is_billing_error

Log = Callable[[str], None]

# USD per million tokens: (input, output). Batch API requests cost half. Cache reads cost
# 0.1× input and cache writes 1.25× input. Update when prices change; an unknown model
# just shows no cost.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
BATCH_DISCOUNT = 0.5

DEFAULT_MAX_TOKENS = 64_000  # room for thinking plus a long list of records
DIRECT_CONCURRENCY = 4
POLL_SECONDS = 30


def estimate_tokens(text: str) -> int:
    """Characters ÷ 4: close enough for size limits and cost estimates."""
    return math.ceil(len(text) / 4)


def cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    batch: bool = False,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float | None:
    """Cost in USD, or None for a model not in the price table."""
    price = PRICES.get(model)
    if price is None:
        return None
    p_in, p_out = price
    usd = (
        input_tokens * p_in
        + cache_read * p_in * 0.1
        + cache_write * p_in * 1.25
        + output_tokens * p_out
    ) / 1_000_000
    return usd * (BATCH_DISCOUNT if batch else 1.0)


def format_cost(usd: float | None) -> str:
    if usd is None:
        return "unknown (model not in the price table)"
    return f"${usd:,.2f}"


@dataclass
class Usage:
    """Tokens and cost added up over a run."""

    model: str = ""
    requests: int = 0
    batch_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float | None = 0.0

    def add(self, usage: dict[str, int], batch: bool) -> None:
        self.requests += 1
        self.batch_requests += int(batch)
        i = int(usage.get("input_tokens") or 0)
        o = int(usage.get("output_tokens") or 0)
        cr = int(usage.get("cache_read_input_tokens") or 0)
        cw = int(usage.get("cache_creation_input_tokens") or 0)
        self.input_tokens += i
        self.output_tokens += o
        self.cache_read_input_tokens += cr
        self.cache_creation_input_tokens += cw
        c = cost(self.model, i, o, batch=batch, cache_read=cr, cache_write=cw)
        self.cost_usd = None if c is None or self.cost_usd is None else self.cost_usd + c

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["cost_usd"] is not None:
            d["cost_usd"] = round(d["cost_usd"], 4)
        return d

    def line(self) -> str:
        if not self.requests:
            return "no LLM calls"
        how = f", {self.batch_requests} through the Batch API" if self.batch_requests else ""
        total_in = (
            self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens
        )
        return (
            f"{self.requests} LLM call{'s' if self.requests != 1 else ''}{how}: "
            f"{total_in:,} input tokens, {self.output_tokens:,} output tokens, "
            f"cost {format_cost(self.cost_usd)} ({self.model})"
        )


@dataclass
class Request:
    custom_id: str  # [a-zA-Z0-9_-]{1,64}, as the Batch API requires; see request_id()
    system: str
    user: str
    schema: dict[str, Any]


def request_id(model: str, effort: str | None, system: str, user: str, schema: dict) -> str:
    """A content hash, so the same request always has the same ID (used for caching)."""
    payload = json.dumps([model, effort, system, user, schema], sort_keys=True)
    return "r" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


@dataclass
class Response:
    custom_id: str
    text: str | None = None  # the JSON output, when the call succeeded
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    batch: bool = False


class LLM(Protocol):
    model: str

    def run(self, requests: list[Request], log: Log) -> dict[str, Response]:
        """Answer every request; failures come back as a Response with `error` set.

        Errors that would hit every request (bad API key, unknown model) are raised.
        """


class AnthropicLLM:
    """The real client. Transient errors (429, 5xx, timeouts) are retried by the SDK."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        effort: str | None = None,
        batch_min_requests: int = 20,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        pending_batch_path: Path | None = None,
    ) -> None:
        import anthropic

        self.model = model
        self.pending_batch_path = pending_batch_path
        self.effort = effort
        self.batch_min_requests = batch_min_requests
        self.max_tokens = max_tokens
        self.client = client or anthropic.Anthropic(api_key=api_key or None, max_retries=4)
        self._sleep = sleep

    # -- request shape -------------------------------------------------------

    def params(self, r: Request) -> dict[str, Any]:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": r.schema}}
        if self.effort:
            output_config["effort"] = self.effort
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": r.system,
            "messages": [{"role": "user", "content": r.user}],
            "output_config": output_config,
        }

    @staticmethod
    def _usage(message: Any) -> dict[str, int]:
        u = getattr(message, "usage", None)
        if u is None:
            return {}
        keys = (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        return {k: int(getattr(u, k, 0) or 0) for k in keys}

    def _from_message(self, custom_id: str, message: Any, batch: bool) -> Response:
        usage = self._usage(message)
        stop = getattr(message, "stop_reason", None)
        if stop == "refusal":
            details = getattr(message, "stop_details", None)
            why = getattr(details, "category", None) if details else None
            return Response(
                custom_id, error=f"model declined (refusal: {why})", usage=usage, batch=batch
            )
        if stop == "max_tokens":
            return Response(custom_id, error="output hit max_tokens", usage=usage, batch=batch)
        text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
        return Response(custom_id, text=text, usage=usage, batch=batch)

    # -- direct --------------------------------------------------------------

    def _direct(self, r: Request) -> Response:
        import anthropic

        try:
            with self.client.messages.stream(**self.params(r)) as stream:
                message = stream.get_final_message()
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
        ):
            raise  # every request would fail the same way
        except anthropic.APIStatusError as e:
            if isinstance(e, anthropic.BadRequestError) and is_billing_error(e):
                raise  # out of credit: every request would fail the same way
            return Response(r.custom_id, error=f"API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError as e:
            return Response(r.custom_id, error=f"connection error: {e}")
        return self._from_message(r.custom_id, message, batch=False)

    def _run_direct(self, requests: list[Request], log: Log) -> dict[str, Response]:
        out: dict[str, Response] = {}
        pool = ThreadPoolExecutor(max_workers=DIRECT_CONCURRENCY)
        try:
            for n, resp in enumerate(pool.map(self._direct, requests), 1):
                out[resp.custom_id] = resp
                if n % 10 == 0 and n < len(requests):
                    log(f"{n}/{len(requests)} requests answered")
        finally:
            # On Ctrl-C (or an error), don't send the requests still queued: only the
            # ones already running are waited for, so the run can stop and release its lock.
            pool.shutdown(wait=True, cancel_futures=True)
        return out

    # -- batch ---------------------------------------------------------------

    def _pending_batch(self, requests: list[Request]) -> str | None:
        """A batch from an interrupted run that holds every one of these requests."""
        path = self.pending_batch_path
        if path is None or not path.is_file():
            return None
        try:
            pending = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if {r.custom_id for r in requests} <= set(pending.get("custom_ids", [])):
            return pending.get("batch_id")
        return None

    def _run_batch(self, requests: list[Request], log: Log) -> dict[str, Response]:
        batch_id = self._pending_batch(requests)
        if batch_id:
            batch = self.client.messages.batches.retrieve(batch_id)
            log(f"picking up batch {batch.id} from an interrupted run")
        else:
            batch = self.client.messages.batches.create(
                requests=[{"custom_id": r.custom_id, "params": self.params(r)} for r in requests]
            )
            if self.pending_batch_path is not None:
                self.pending_batch_path.parent.mkdir(parents=True, exist_ok=True)
                self.pending_batch_path.write_text(
                    json.dumps(
                        {"batch_id": batch.id, "custom_ids": [r.custom_id for r in requests]}
                    ),
                    encoding="utf-8",
                )
            log(
                f"sent {len(requests)} requests as batch {batch.id}; batches usually finish "
                "within an hour. If this run is interrupted, the next run picks the batch up "
                "again instead of sending a new one"
            )
        last = None
        while batch.processing_status != "ended":
            self._sleep(POLL_SECONDS)
            batch = self.client.messages.batches.retrieve(batch.id)
            c = batch.request_counts
            now = (c.processing, c.succeeded, c.errored)
            if now != last:
                log(
                    f"batch {batch.id}: {c.processing} processing, {c.succeeded} done, "
                    f"{c.errored} errored"
                )
                last = now

        out: dict[str, Response] = {}
        for result in self.client.messages.batches.results(batch.id):
            cid = result.custom_id
            kind = result.result.type
            if kind == "succeeded":
                out[cid] = self._from_message(cid, result.result.message, batch=True)
            elif kind == "errored":
                err = result.result.error
                detail = getattr(getattr(err, "error", None), "message", None) or str(err)
                out[cid] = Response(cid, error=f"batch request errored: {detail}", batch=True)
            else:
                out[cid] = Response(cid, error=f"batch request {kind}", batch=True)
        wanted = {r.custom_id for r in requests}
        out = {cid: resp for cid, resp in out.items() if cid in wanted}
        for r in requests:  # shouldn't happen, but never lose a request silently
            out.setdefault(r.custom_id, Response(r.custom_id, error="missing from batch results"))
        if self.pending_batch_path is not None:
            self.pending_batch_path.unlink(missing_ok=True)
        return out

    def run(self, requests: list[Request], log: Log) -> dict[str, Response]:
        if not requests:
            return {}
        if len(requests) >= self.batch_min_requests:
            return self._run_batch(requests, log)
        return self._run_direct(requests, log)


class CachedLLM:
    """Keeps every successful answer in a local folder, keyed by request ID.

    If a run fails after extraction (or is interrupted), the next run reuses the answers
    instead of paying for them again. The cache is local and can be deleted any time.
    """

    def __init__(self, inner: LLM, cache_dir: Path) -> None:
        self.inner = inner
        self.model = inner.model
        self.dir = cache_dir

    def _path(self, custom_id: str) -> Path:
        return self.dir / f"{custom_id}.json"

    def forget(self, custom_id: str) -> None:
        """Drop a cached answer that turned out to be unusable, so a retry asks again."""
        self._path(custom_id).unlink(missing_ok=True)

    def run(self, requests: list[Request], log: Log) -> dict[str, Response]:
        out: dict[str, Response] = {}
        todo: list[Request] = []
        for r in requests:
            p = self._path(r.custom_id)
            if p.is_file():
                try:
                    out[r.custom_id] = Response(r.custom_id, text=json.loads(p.read_text())["text"])
                    continue
                except (json.JSONDecodeError, KeyError, OSError):
                    pass
            todo.append(r)
        if out:
            log(f"{len(out)} answers reused from the local cache ({self.dir})")
        fresh = self.inner.run(todo, log)
        self.dir.mkdir(parents=True, exist_ok=True)
        for cid, resp in fresh.items():
            if resp.text is not None and resp.error is None:
                self._path(cid).write_text(json.dumps({"text": resp.text}), encoding="utf-8")
        out.update(fresh)
        return out
