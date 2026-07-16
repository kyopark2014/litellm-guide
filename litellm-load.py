#!/usr/bin/env python3
"""Send N chat completions through LiteLLM Proxy (default: Haiku × 20).

Prereqs: LiteLLM deployed; model registered (see litellm-test.py --register-bedrock)

  python3 litellm-load.py
  python3 litellm-load.py --count 50 --concurrency 5
  python3 litellm-load.py --model claude-haiku-4-5 --region us-west-2
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Lock
from typing import Any

DEFAULT_REGION = "us-west-2"
DEFAULT_STACK = "litellm"
DEFAULT_MODEL = "claude-haiku-4-5"

_print_lock = Lock()


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 120,
) -> tuple[int, Any, float]:
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            elapsed = time.perf_counter() - t0
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype and raw:
                return resp.status, json.loads(raw), elapsed
            return resp.status, raw.decode("utf-8", errors="replace") if raw else "", elapsed
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - t0
        raw = e.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", errors="replace")
        return e.code, payload, elapsed
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return 0, str(e), elapsed


def resolve_endpoints(region: str, stack_name: str) -> dict[str, str]:
    url = os.environ.get("LITELLM_URL", "").rstrip("/")
    key = os.environ.get("LITELLM_MASTER_KEY", "")

    state_path = Path(__file__).resolve().parent / "install" / f".state-{stack_name}.json"
    if state_path.is_file() and (not url or not key):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            url = url or (state.get("url") or "").rstrip("/")
            key = key or state.get("master_key") or ""
            region = state.get("region") or region
        except (json.JSONDecodeError, OSError):
            pass

    if not url or not key:
        import boto3

        session = boto3.Session(region_name=region)
        if not url:
            elbv2 = session.client("elbv2")
            lb = elbv2.describe_load_balancers(Names=[f"{stack_name}-alb"])[
                "LoadBalancers"
            ][0]
            url = f"http://{lb['DNSName']}"
        if not key:
            sm = session.client("secretsmanager")
            key = sm.get_secret_value(SecretId=f"{stack_name}/master-key")["SecretString"]

    return {"url": url.rstrip("/"), "master_key": key}


def one_call(
    i: int,
    total: int,
    base: str,
    key: str,
    model: str,
    max_tokens: int,
    use_openai: bool,
) -> dict[str, Any]:
    if use_openai:
        status, body, elapsed = _http(
            "POST",
            f"{base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            body={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "user", "content": f"Reply with exactly: LOAD-{i:03d}-OK"}
                ],
            },
            timeout=180,
        )
        in_tok = out_tok = 0
        text = ""
        err = ""
        if status == 200 and isinstance(body, dict):
            usage = body.get("usage") or {}
            in_tok = int(usage.get("prompt_tokens") or 0)
            out_tok = int(usage.get("completion_tokens") or 0)
            choice = (body.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content")) or ""
        else:
            err = str(body)[:200]
    else:
        status, body, elapsed = _http(
            "POST",
            f"{base}/v1/messages",
            headers={
                "Authorization": f"Bearer {key}",
                "anthropic-version": "2023-06-01",
            },
            body={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "user", "content": f"Reply with exactly: LOAD-{i:03d}-OK"}
                ],
            },
            timeout=180,
        )
        in_tok = out_tok = 0
        text = ""
        err = ""
        if status == 200 and isinstance(body, dict):
            usage = body.get("usage") or {}
            in_tok = int(usage.get("input_tokens") or 0)
            out_tok = int(usage.get("output_tokens") or 0)
            for block in body.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += block.get("text", "")
        else:
            err = str(body)[:200]

    with _print_lock:
        mark = "OK" if status == 200 else "FAIL"
        print(
            f"[{i:03d}/{total}] {mark} HTTP {status}  "
            f"{elapsed:.2f}s  in={in_tok} out={out_tok}  {text[:40]!r}"
            + (f"  err={err}" if err else "")
        )
    return {
        "i": i,
        "status": status,
        "elapsed": elapsed,
        "in": in_tok,
        "out": out_tok,
        "ok": status == 200,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="LiteLLM load traffic")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", DEFAULT_REGION))
    p.add_argument("--stack-name", default=DEFAULT_STACK)
    p.add_argument("--model", default=os.environ.get("LITELLM_LOAD_MODEL", DEFAULT_MODEL))
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--max-tokens", type=int, default=32)
    p.add_argument(
        "--openai",
        action="store_true",
        help="Use /v1/chat/completions instead of /v1/messages",
    )
    args = p.parse_args()

    ep = resolve_endpoints(args.region, args.stack_name)
    base, key = ep["url"], ep["master_key"]
    print(f"url={base}")
    print(f"model={args.model}  count={args.count}  concurrency={args.concurrency}")
    print(f"api={'openai' if args.openai else 'anthropic'}\n")

    # probe one request to pick API if needed
    use_openai = args.openai
    if not use_openai:
        st, _, _ = _http(
            "POST",
            f"{base}/v1/messages",
            headers={
                "Authorization": f"Bearer {key}",
                "anthropic-version": "2023-06-01",
            },
            body={
                "model": args.model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=60,
        )
        if st != 200:
            print(f"probe /v1/messages HTTP {st} — falling back to chat/completions")
            use_openai = True

    t0 = time.perf_counter()
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [
            ex.submit(
                one_call,
                i,
                args.count,
                base,
                key,
                args.model,
                args.max_tokens,
                use_openai,
            )
            for i in range(1, args.count + 1)
        ]
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())

    wall = time.perf_counter() - t0
    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    total_in = sum(r["in"] for r in results)
    total_out = sum(r["out"] for r in results)
    latencies = sorted(r["elapsed"] for r in results if r["ok"])

    print("\n=== summary ===")
    print(f"  ok/fail:     {ok}/{fail}")
    if wall > 0:
        print(f"  wall time:   {wall:.1f}s  ({ok / wall:.2f} rps ok)")
    print(f"  tokens:      in={total_in} out={total_out} total={total_in + total_out}")
    if latencies:
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        print(f"  latency:     p50={p50:.2f}s  p95={p95:.2f}s  max={latencies[-1]:.2f}s")

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
