#!/usr/bin/env python3
"""litellm-test.py — LiteLLM Proxy smoke test (health + messages).

Prereqs: LiteLLM deployed via install/installer.py

Usage:
  python3 litellm-test.py
  python3 litellm-test.py --region us-west-2 --stack-name litellm
  python3 litellm-test.py --model claude-haiku-4-5 --register-bedrock
  LITELLM_URL=http://... LITELLM_MASTER_KEY=sk-... python3 litellm-test.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_REGION = "us-west-2"
DEFAULT_STACK = "litellm"
DEFAULT_MODEL = "claude-haiku-4-5"
# Bedrock model id used when --register-bedrock
DEFAULT_BEDROCK_MODEL = "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 60,
) -> tuple[int, Any]:
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype and raw:
                return resp.status, json.loads(raw)
            return resp.status, raw.decode("utf-8", errors="replace") if raw else ""
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", errors="replace")
        return e.code, payload
    except Exception as e:
        return 0, str(e)


def resolve_endpoints(region: str, stack_name: str, state_file: Path | None = None) -> dict[str, str]:
    url = os.environ.get("LITELLM_URL", "").rstrip("/")
    key = os.environ.get("LITELLM_MASTER_KEY", "")

    state_path = state_file or (
        Path(__file__).resolve().parent / "install" / f".state-{stack_name}.json"
    )
    if state_path.is_file() and (not url or not key):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            url = url or (state.get("url") or "").rstrip("/")
            key = key or state.get("master_key") or ""
            region = state.get("region") or region
        except (json.JSONDecodeError, OSError):
            pass

    if not url or not key:
        try:
            import boto3
        except ImportError as e:
            raise SystemExit(
                "boto3 required to look up ALB/master key, or set "
                "LITELLM_URL + LITELLM_MASTER_KEY, or run: "
                "python install/installer.py status"
            ) from e

        session = boto3.Session(region_name=region)
        if not url:
            elbv2 = session.client("elbv2")
            try:
                lb = elbv2.describe_load_balancers(Names=[f"{stack_name}-alb"])[
                    "LoadBalancers"
                ][0]
                url = f"http://{lb['DNSName']}"
            except Exception as e:
                raise SystemExit(
                    f"ALB {stack_name}-alb not found in {region}. "
                    f"Set LITELLM_URL or deploy first. ({e})"
                ) from e

        if not key:
            sm = session.client("secretsmanager")
            try:
                key = sm.get_secret_value(SecretId=f"{stack_name}/master-key")[
                    "SecretString"
                ]
            except Exception as e:
                raise SystemExit(
                    f"Secret {stack_name}/master-key not found. "
                    f"Set LITELLM_MASTER_KEY. ({e})"
                ) from e

    return {"url": url.rstrip("/"), "master_key": key}


def list_models(base: str, key: str) -> list[str]:
    status, body = _http(
        "GET",
        f"{base}/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    if status != 200 or not isinstance(body, dict):
        print(f"  list models FAILED HTTP {status}: {body}")
        return []
    return [m.get("id") for m in (body.get("data") or []) if m.get("id")]


def register_bedrock_model(
    base: str, key: str, model_name: str, bedrock_model: str, region: str
) -> bool:
    status, body = _http(
        "POST",
        f"{base}/model/new",
        headers={"Authorization": f"Bearer {key}"},
        body={
            "model_name": model_name,
            "litellm_params": {
                "model": bedrock_model,
                "aws_region_name": region,
            },
        },
        timeout=30,
    )
    # 200 = created; some versions return existing as success-ish
    ok = status in (200, 201)
    print(f"  register {model_name} → {bedrock_model}: HTTP {status}")
    if not ok:
        print(f"  body: {body}")
    return ok


def ensure_bedrock_task_role(region: str, stack_name: str) -> None:
    """Attach Bedrock invoke policy to ECS task role if missing."""
    try:
        import boto3
    except ImportError:
        return

    iam = boto3.Session(region_name=region).client("iam")
    role = f"{stack_name}-ecs-task-role"
    policy_name = "LiteLLMBedrockAccess"
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockRuntime",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:GetFoundationModel",
                    "bedrock:ListFoundationModels",
                ],
                "Resource": "*",
            },
            {
                "Sid": "BedrockMantle",
                "Effect": "Allow",
                "Action": [
                    "bedrock-mantle:CreateInference",
                    "bedrock-mantle:InvokeModel",
                    "bedrock-mantle:*",
                ],
                "Resource": "*",
            },
        ],
    }
    try:
        iam.put_role_policy(
            RoleName=role,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(doc),
        )
        print(f"  IAM: attached {policy_name} to {role}")
    except Exception as e:
        print(f"  IAM: skip Bedrock policy ({e})")


def main() -> int:
    parser = argparse.ArgumentParser(description="LiteLLM smoke + messages test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", DEFAULT_REGION))
    parser.add_argument("--stack-name", default=DEFAULT_STACK)
    parser.add_argument("--model", default=os.environ.get("LITELLM_TEST_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--register-bedrock",
        action="store_true",
        help="Register Bedrock model and grant task-role Bedrock IAM",
    )
    parser.add_argument(
        "--bedrock-model",
        default=DEFAULT_BEDROCK_MODEL,
        help="litellm_params.model for --register-bedrock",
    )
    parser.add_argument("--skip-messages", action="store_true")
    args = parser.parse_args()

    ep = resolve_endpoints(args.region, args.stack_name)
    base, key = ep["url"], ep["master_key"]
    print("Endpoints")
    print(f"  url:    {base}")
    print(f"  key:    {key[:8]}…{key[-4:]} (len={len(key)})")
    print(f"  model:  {args.model}")
    print(f"  region: {args.region}")

    # 1) Health
    print("\n=== health ===")
    for name, path in (
        ("liveliness", "/health/liveliness"),
        ("readiness", "/health/readiness"),
    ):
        status, body = _http("GET", f"{base}{path}", timeout=15)
        ok = "OK" if status == 200 else "FAIL"
        print(f"  {name}: HTTP {status} {ok}  {str(body)!r}"[:120])
        if status != 200 and name == "liveliness":
            return 1

    auth = {"Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"}

    # 2) Models
    print("\n=== models ===")
    models = list_models(base, key)
    print(f"  registered: {models or '(none)'}")

    if args.register_bedrock:
        print("\n=== register bedrock ===")
        ensure_bedrock_task_role(args.region, args.stack_name)
        if args.model not in models:
            if not register_bedrock_model(
                base, key, args.model, args.bedrock_model, args.region
            ):
                return 1
            time.sleep(2)
            models = list_models(base, key)
            print(f"  registered now: {models}")
        else:
            print(f"  {args.model} already registered")

    if args.skip_messages:
        print("\nPASS — health OK (--skip-messages)")
        return 0

    if args.model not in models and models:
        print(
            f"\nWARN: model {args.model!r} not in list. "
            f"Try --register-bedrock or pick from: {models}"
        )

    # 3) Anthropic Messages API
    print("\n=== POST /v1/messages ===")
    status, msg = _http(
        "POST",
        f"{base}/v1/messages",
        headers=auth,
        body={
            "model": args.model,
            "max_tokens": 64,
            "messages": [
                {"role": "user", "content": "Reply with exactly: LITELLM-OK"}
            ],
        },
        timeout=120,
    )
    print(f"  HTTP {status}")
    if status != 200:
        print(f"  body: {msg}")
        # Fallback: OpenAI chat completions
        print("\n=== POST /v1/chat/completions (fallback) ===")
        status2, msg2 = _http(
            "POST",
            f"{base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            body={
                "model": args.model,
                "max_tokens": 64,
                "messages": [
                    {"role": "user", "content": "Reply with exactly: LITELLM-OK"}
                ],
            },
            timeout=120,
        )
        print(f"  HTTP {status2}")
        if status2 != 200:
            print(f"  body: {msg2}")
            return 1
        if isinstance(msg2, dict):
            choice = (msg2.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content")) or ""
            print(f"  reply: {text!r}")
            print(f"  usage: {msg2.get('usage')}")
        print("\nPASS — health + chat/completions OK")
        return 0

    text = ""
    if isinstance(msg, dict):
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
        print(f"  reply: {text!r}")
        print(f"  usage: {msg.get('usage')}")
        print(f"  model: {msg.get('model')}")
    else:
        print(f"  body: {msg}")

    print("\nPASS — health + /v1/messages OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
