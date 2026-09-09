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
    what: str = "resource",
    timeout: float = 900,
    interval: float = 5,
    fail_states: tuple[str, ...] = ("failed",),
    steps: "Steps | None" = None,
    log_every: int = 1,
) -> dict:
    """Poll get_fn until obj['state'] == target.

    ``what`` names the resource in progress logs (e.g. ``instance group foo``,
    ``vpc vpc-nico``) so consecutive waits are distinguishable.
    """
    deadline = time.monotonic() + timeout
    last: dict | None = None
    attempt = 0
    if steps:
        steps.info(f"waiting for {what} state={target!r} (timeout={timeout:.0f}s)")
    while time.monotonic() < deadline:
        obj = get_fn()
        last = obj
        state = obj.get("state")
        if state in fail_states:
            raise AssertionError(f"{what} entered {state!r}: {obj}")
        if state == target:
            if steps:
                steps.ok(f"{what} state={target}")
            return obj
        attempt += 1
        if steps and attempt % max(log_every, 1) == 0:
            steps.progress(f"… {what} state={state!r}, want {target!r}")
        time.sleep(interval)
    if steps:
        steps.info(f"{what} state={target!r} TIMED OUT after {timeout:.0f}s")
    raise AssertionError(
        f"{what} state={target} not met within {timeout}s; last={last!r}"
    )

def await_api_states(
    getters: dict[str, Callable[[], dict]],
    target: str,
    *,
    timeout: float = 1800,
    interval: float = 15,
    fail_states: tuple[str, ...] = ("failed",),
    steps: "Steps | None" = None,
    log_every: int = 1,
) -> dict[str, dict]:
    """Poll several resources until ALL reach obj['state'] == target.

    For resources created back-to-back and then awaited together, so they
    provision concurrently and the wall clock is max() rather than sum().
    Creates are asynchronous — POST returns before anything is provisioned —
    so the parallelism comes from deferring the waits, not from threads.

    Polling them together (rather than calling await_api_state once per
    resource) matters for failures: a resource that enters a fail state is
    reported on the next tick instead of after whichever wait happens to be
    running first has finished or timed out.

    ``getters`` maps a display name to a zero-arg getter; the return maps the
    same names to the final objects.
    """
    deadline = time.monotonic() + timeout
    settled: dict[str, dict] = {}
    last: dict[str, Any] = {}
    attempt = 0
    if steps:
        names = ", ".join(getters)
        steps.info(
            f"waiting for {names} state={target!r} (timeout={timeout:.0f}s)"
        )
    while time.monotonic() < deadline:
        for name, get_fn in getters.items():
            if name in settled:
                continue
            obj = get_fn()
            last[name] = obj
            state = obj.get("state")
            if state in fail_states:
                raise AssertionError(f"{name} entered {state!r}: {obj}")
            if state == target:
                settled[name] = obj
                if steps:
                    steps.ok(f"{name} state={target}")
        if len(settled) == len(getters):
            return settled
        attempt += 1
        if steps and attempt % max(log_every, 1) == 0:
            pending = ", ".join(
                f"{name}={last.get(name, {}).get('state')!r}"
                for name in getters
                if name not in settled
            )
            steps.progress(f"… waiting for {pending} (want {target!r})")
        time.sleep(interval)
    pending = [name for name in getters if name not in settled]
    if steps:
        steps.info(f"{', '.join(pending)} state={target!r} TIMED OUT after {timeout:.0f}s")
    raise AssertionError(
        f"{', '.join(pending)} state={target} not met within {timeout}s; last={last!r}"
    )


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
