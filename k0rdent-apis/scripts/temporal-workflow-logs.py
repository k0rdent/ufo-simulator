#!/usr/bin/env python3
"""Dump a Temporal workflow's full event history with payloads decoded.

Fetches history via the lab's temporal-admintools pod and prints every event,
decoding Temporal ``payloads[].data`` (base64) into JSON/text instead of leaving
opaque blobs.

Usage:
  temporal-workflow-logs.py <workflow-id>
  temporal-workflow-logs.py <workflow-id> --run-id <run-id>
  temporal-workflow-logs.py <workflow-id> --namespace default
  temporal-workflow-logs.py <workflow-id> --raw-json   # decoded history as JSON

Requires: kubectl, Python 3.9+
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from typing import Any


DEFAULT_K8S_NS = "k0rdent-apis"
DEFAULT_TEMPORAL_NS = "default"
ADMINTOOLS = "deploy/temporal-admintools"


def die(msg: str, code: int = 1) -> None:
    print(f"temporal-workflow-logs: {msg}", file=sys.stderr)
    raise SystemExit(code)


def b64_decode(value: str) -> bytes | None:
    try:
        return base64.b64decode(value, validate=False)
    except Exception:
        return None


def maybe_json(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def decode_metadata(meta: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in meta.items():
        if isinstance(value, str):
            raw = b64_decode(value)
            # Temporal often stores metadata values themselves as base64
            # (e.g. encoding -> "anNvbi9wbGFpbg==" -> "json/plain").
            if raw is not None and raw and all(32 <= b < 127 for b in raw):
                out[key] = raw.decode("ascii")
            else:
                out[key] = value
        else:
            out[key] = str(value)
    return out


def decode_payload(payload: dict[str, Any]) -> Any:
    meta = decode_metadata(payload.get("metadata") or {})
    data = payload.get("data")
    if not isinstance(data, str) or not data:
        return {"metadata": meta, "data": data}

    raw = b64_decode(data)
    if raw is None:
        return {"metadata": meta, "data": data, "decodeError": "invalid base64"}

    encoding = (meta.get("encoding") or "").lower()
    if encoding in ("json/plain", "json/protobuf", "binary/json"):
        return maybe_json(raw)
    if encoding in ("binary/null",):
        return None
    # Prefer JSON when it parses; otherwise show text.
    decoded = maybe_json(raw)
    return decoded


def decode_payloads_field(value: Any) -> Any:
    """Decode a Temporal Payloads object or a bare list of Payload objects."""
    if isinstance(value, dict) and "payloads" in value:
        items = value.get("payloads") or []
        decoded = [decode_payload(p) if isinstance(p, dict) else p for p in items]
        if len(decoded) == 1:
            return decoded[0]
        return decoded
    if isinstance(value, list) and value and all(
        isinstance(p, dict) and ("data" in p or "metadata" in p) for p in value
    ):
        decoded = [decode_payload(p) for p in value]
        if len(decoded) == 1:
            return decoded[0]
        return decoded
    return None


def decode_tree(node: Any) -> Any:
    """Recursively decode any Payloads-shaped objects in the history tree."""
    if isinstance(node, dict):
        # Prefer decoding a whole payloads wrapper in place.
        as_payloads = decode_payloads_field(node)
        if as_payloads is not None and set(node.keys()) <= {"payloads"}:
            return as_payloads

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("input", "result", "failure", "details", "heartbeatDetails",
                       "lastFailure", "retryState") or key.endswith("Payloads"):
                decoded = decode_payloads_field(value)
                if decoded is not None:
                    out[key] = decode_tree(decoded) if not isinstance(decoded, (str, int, float, bool, type(None))) else decoded
                    # failure objects also carry non-payload fields
                    if isinstance(value, dict) and "payloads" not in value:
                        # already handled below if not payloads-only
                        pass
                    continue
            if key == "payloads" and isinstance(value, list):
                out[key] = [
                    decode_payload(p) if isinstance(p, dict) else decode_tree(p)
                    for p in value
                ]
                continue
            # failure.message is plain text; failure.encodedAttributes may be payloads
            out[key] = decode_tree(value)
        return out
    if isinstance(node, list):
        return [decode_tree(item) for item in node]
    return node


def event_attrs(event: dict[str, Any]) -> dict[str, Any]:
    for key, value in event.items():
        if key.endswith("EventAttributes") and isinstance(value, dict):
            return {key: value}
    return {}


def fetch_history(
    workflow_id: str,
    *,
    run_id: str | None,
    temporal_namespace: str,
    k8s_namespace: str,
) -> dict[str, Any]:
    cmd = [
        "kubectl",
        "-n",
        k8s_namespace,
        "exec",
        ADMINTOOLS,
        "--",
        "temporal",
        "workflow",
        "show",
        "--workflow-id",
        workflow_id,
        "--namespace",
        temporal_namespace,
        "--output",
        "json",
    ]
    if run_id:
        cmd.extend(["--run-id", run_id])

    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        die("kubectl not found on PATH")

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        die(f"temporal workflow show failed (exit {proc.returncode}):\n{err}")

    raw = proc.stdout.strip()
    if not raw:
        die("temporal workflow show returned empty output")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"could not parse temporal JSON: {exc}\n--- stdout ---\n{raw[:2000]}")


def print_human(history: dict[str, Any]) -> None:
    events = history.get("events") or history.get("history", {}).get("events") or []
    if not events:
        # Some CLI versions wrap differently; dump the whole decoded object.
        print(json.dumps(history, indent=2, ensure_ascii=False, default=str))
        return

    print(f"events: {len(events)}")
    print("=" * 72)
    for event in events:
        eid = event.get("eventId")
        etype = event.get("eventType")
        etime = event.get("eventTime")
        print(f"\n[{eid}] {etype}  @ {etime}")
        print("-" * 72)
        attrs = event_attrs(event)
        if not attrs:
            # Fall back to everything except the common envelope fields.
            body = {
                k: v
                for k, v in event.items()
                if k not in ("eventId", "eventType", "eventTime", "taskId", "version")
            }
            print(json.dumps(body, indent=2, ensure_ascii=False, default=str))
        else:
            print(json.dumps(attrs, indent=2, ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show Temporal workflow event history with decoded payloads."
    )
    parser.add_argument("workflow_id", help="Temporal Workflow Id")
    parser.add_argument("--run-id", default=None, help="Specific Run Id (optional)")
    parser.add_argument(
        "--namespace",
        default=DEFAULT_TEMPORAL_NS,
        help=f"Temporal namespace (default: {DEFAULT_TEMPORAL_NS})",
    )
    parser.add_argument(
        "--k8s-namespace",
        default=DEFAULT_K8S_NS,
        help=f"Kubernetes namespace of temporal-admintools (default: {DEFAULT_K8S_NS})",
    )
    parser.add_argument(
        "--raw-json",
        action="store_true",
        help="Print the full decoded history as JSON instead of the per-event view",
    )
    args = parser.parse_args(argv)

    history = fetch_history(
        args.workflow_id,
        run_id=args.run_id,
        temporal_namespace=args.namespace,
        k8s_namespace=args.k8s_namespace,
    )
    decoded = decode_tree(history)

    if args.raw_json:
        print(json.dumps(decoded, indent=2, ensure_ascii=False, default=str))
    else:
        print_human(decoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
