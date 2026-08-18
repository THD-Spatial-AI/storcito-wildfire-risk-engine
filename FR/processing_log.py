"""Compact, consistent processing diagnostics for long wildfire jobs."""
from __future__ import annotations

import math
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from functools import wraps
from typing import Callable, Iterator, TypeVar


_T = TypeVar("_T")
_LOG_CONTEXT: ContextVar[dict[str, object]] = ContextVar(
    "ffrm_log_context", default={}
)


def bind_log_context(**fields: object) -> Token:
    """Attach request fields to subsequent logs in the current async context."""
    merged = {**_LOG_CONTEXT.get(), **fields}
    return _LOG_CONTEXT.set(merged)


def reset_log_context(token: Token) -> None:
    _LOG_CONTEXT.reset(token)


def _format_value(value: object) -> str:
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return f"{value:.4f}"
    text = str(value).replace("\n", " ").strip()
    return repr(text) if any(char.isspace() for char in text) else text


def log_event(component: str, event: str, **fields: object) -> None:
    """Emit one grep-friendly lifecycle line to stdout/engine.log."""
    job_id = os.environ.get("STORCITO_JOB_ID", "").strip()
    payload: dict[str, object] = dict(_LOG_CONTEXT.get())
    if job_id:
        payload["job"] = job_id
    payload.update(fields)
    details = " ".join(
        f"{key}={_format_value(value)}"
        for key, value in payload.items()
        if value is not None and value != ""
    )
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    suffix = f" {details}" if details else ""
    print(f"[FFRM][{timestamp}][{component}][{event}]{suffix}", flush=True)


@contextmanager
def processing_step(component: str, step: str, **fields: object) -> Iterator[None]:
    """Log START/DONE/FAILED around a processing operation."""
    started = perf_counter()
    log_event(component, "START", step=step, **fields)
    try:
        yield
    except BaseException as exc:
        log_event(
            component,
            "FAILED",
            step=step,
            elapsed_s=perf_counter() - started,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
        raise
    else:
        log_event(
            component,
            "DONE",
            step=step,
            elapsed_s=perf_counter() - started,
        )


def logged_step(component: str, step: str):
    """Decorate a function with the same lifecycle diagnostics."""

    def decorator(function: Callable[..., _T]) -> Callable[..., _T]:
        @wraps(function)
        def wrapped(*args, **kwargs):
            with processing_step(component, step):
                return function(*args, **kwargs)

        return wrapped

    return decorator


def log_array_stats(
    component: str,
    name: str,
    values,
    *,
    nodata: float | int | None = None,
    max_sample_pixels: int = 1_000_000,
) -> None:
    """Log bounded-cost raster statistics from a deterministic sample."""
    import numpy as np

    array = np.asanyarray(values)
    flat = array.reshape(-1)
    stride = max(1, math.ceil(flat.size / max_sample_pixels))
    sample = flat[::stride]
    valid = np.isfinite(sample)
    if nodata is not None:
        valid &= sample != nodata
    valid_values = sample[valid]
    fields: dict[str, object] = {
        "name": name,
        "shape": "x".join(str(value) for value in array.shape),
        "dtype": array.dtype,
        "sample_stride": stride,
        "sample_pixels": sample.size,
        "valid_sample_pixels": valid_values.size,
        "valid_sample_pct": (
            100.0 * valid_values.size / sample.size if sample.size else 0.0
        ),
    }
    if valid_values.size:
        fields.update(
            min=float(valid_values.min()),
            max=float(valid_values.max()),
            mean=float(valid_values.mean()),
        )
        integer_like = np.all(valid_values == np.floor(valid_values))
        if integer_like:
            unique = np.unique(valid_values)
            if unique.size <= 16:
                fields["classes"] = ",".join(
                    f"{int(value)}:{int(np.count_nonzero(valid_values == value))}"
                    for value in unique
                )
    log_event(component, "STATS", **fields)
