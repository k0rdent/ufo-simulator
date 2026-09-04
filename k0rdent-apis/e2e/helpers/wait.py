"""Polling wait helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from helpers.steps import Steps


def await_predicate(
    pred: Callable[[], Any],
    *,
    timeout: float = 900,
    interval: float = 5,
    desc: str = "condition",
    steps: "Steps | None" = None,
    log_every: int = 1,
) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    attempt = 0
    if steps:
        steps.info(f"waiting for {desc} (timeout={timeout:.0f}s)")
    while time.monotonic() < deadline:
        last = pred()
        if last:
            if steps:
                steps.ok(desc)
            return last
        attempt += 1
        if steps and attempt % max(log_every, 1) == 0:
            steps.progress(f"… still waiting for {desc}")
        time.sleep(interval)
    if steps:
        steps.info(f"{desc} TIMED OUT after {timeout:.0f}s")
    raise AssertionError(f"{desc} not met within {timeout}s; last={last!r}")


def await_api_state(
    get_fn: Callable[[], dict],
    target: str,
    *,
    timeout: float = 900,
    interval: float = 5,
    fail_states: tuple[str, ...] = ("failed",),
    steps: "Steps | None" = None,
    log_every: int = 1,
) -> dict:
    deadline = time.monotonic() + timeout
    last: dict | None = None
    attempt = 0
    if steps:
        steps.info(f"waiting for state={target!r} (timeout={timeout:.0f}s)")
    while time.monotonic() < deadline:
        obj = get_fn()
        last = obj
        state = obj.get("state")
        if state in fail_states:
            raise AssertionError(f"resource entered {state!r}: {obj}")
        if state == target:
            if steps:
                steps.ok(f"state={target}")
            return obj
        attempt += 1
        if steps and attempt % max(log_every, 1) == 0:
            steps.progress(f"… state={state!r}, want {target!r}")
        time.sleep(interval)
    if steps:
        steps.info(f"state={target!r} TIMED OUT after {timeout:.0f}s")
    raise AssertionError(f"state={target} not met within {timeout}s; last={last!r}")


def await_api_absent(
    get_fn: Callable[[], Any],
    *,
    timeout: float = 900,
    interval: float = 5,
    desc: str = "resource absent",
    steps: "Steps | None" = None,
    log_every: int = 1,
) -> None:
    """Wait until get_fn returns None / falsy (e.g. HTTP 404 mapped to None)."""

    def _gone():
        return True if get_fn() is None else None

    await_predicate(
        _gone,
        timeout=timeout,
        interval=interval,
        desc=desc,
        steps=steps,
        log_every=log_every,
    )
