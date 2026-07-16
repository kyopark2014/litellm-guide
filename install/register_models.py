#!/usr/bin/env python3
"""Register default models on a deployed LiteLLM Proxy.

Claude → Bedrock runtime profiles
GPT   → Bedrock Mantle (openai.gpt-* ; SigV4 / task role — no OpenAI API key)

Usage:
  python install/register_models.py
  python install/register_models.py --region us-west-2 --stack-name litellm
  python install/register_models.py --force   # re-register even if name exists
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

INSTALL_DIR = Path(__file__).resolve().parent
if str(INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(INSTALL_DIR))

from models import DEFAULT_BEDROCK_MODELS, DEFAULT_MANTLE_GPT_MODELS  # noqa: E402


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


def resolve_proxy(region: str, stack_name: str) -> tuple[str, str]:
    url = os.environ.get("LITELLM_URL", "").rstrip("/")
    key = os.environ.get("LITELLM_MASTER_KEY", "")
    state = INSTALL_DIR / f".state-{stack_name}.json"
    if state.is_file() and (not url or not key):
        data = json.loads(state.read_text(encoding="utf-8"))
        url = url or (data.get("url") or "").rstrip("/")
        key = key or data.get("master_key") or ""
        region = data.get("region") or region

    if not url or not key:
        import boto3

        session = boto3.Session(region_name=region)
        if not url:
            lb = session.client("elbv2").describe_load_balancers(
                Names=[f"{stack_name}-alb"]
            )["LoadBalancers"][0]
            url = f"http://{lb['DNSName']}"
        if not key:
            key = session.client("secretsmanager").get_secret_value(
                SecretId=f"{stack_name}/master-key"
            )["SecretString"]
    return url.rstrip("/"), key


def list_models(base: str, key: str) -> set[str]:
    status, body = _http(
        "GET", f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30
    )
    if status != 200 or not isinstance(body, dict):
        return set()
    return {m.get("id") for m in (body.get("data") or []) if m.get("id")}


def model_info_map(base: str, key: str) -> dict[str, str]:
    """model_name → model id from /model/info (for delete)."""
    status, body = _http(
        "GET", f"{base}/model/info", headers={"Authorization": f"Bearer {key}"}, timeout=30
    )
    out: dict[str, str] = {}
    if status != 200 or not isinstance(body, dict):
        return out
    for row in body.get("data") or []:
        name = row.get("model_name")
        mid = (row.get("model_info") or {}).get("id")
        if name and mid:
            out[name] = mid
    return out


def delete_model(base: str, key: str, model_id: str) -> bool:
    status, body = _http(
        "POST",
        f"{base}/model/delete",
        headers={"Authorization": f"Bearer {key}"},
        body={"id": model_id},
        timeout=30,
    )
    return status in (200, 201)


def ensure_bedrock_iam(region: str, stack_name: str) -> None:
    import boto3

    iam = boto3.Session(region_name=region).client("iam")
    role = f"{stack_name}-ecs-task-role"
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
            PolicyName="LiteLLMBedrockAccess",
            PolicyDocument=json.dumps(doc),
        )
        print(f"  IAM: Bedrock + Mantle on {role}")
    except Exception as e:
        print(f"  IAM: skip ({e})")


def _with_region(spec: dict, region: str) -> dict:
    params = dict(spec["litellm_params"])
    params["aws_region_name"] = region
    # Keep api_base aligned with region for Mantle GPT
    model = params.get("model") or ""
    if model.startswith("bedrock_mantle/"):
        params["api_base"] = f"https://bedrock-mantle.{region}.api.aws/openai/v1"
    return {**spec, "litellm_params": params}


def register_one(
    base: str, key: str, spec: dict, existing: set[str], *, force: bool, info_ids: dict[str, str]
) -> str:
    name = spec["model_name"]
    if name in existing and force:
        mid = info_ids.get(name)
        if mid and delete_model(base, key, mid):
            print(f"  del  {name} ({mid[:12]}…)")
            existing.discard(name)
        else:
            print(f"  warn could not delete {name} for --force; trying new anyway")
    elif name in existing:
        print(f"  skip {name} (already registered)")
        return "skip"

    status, body = _http(
        "POST",
        f"{base}/model/new",
        headers={"Authorization": f"Bearer {key}"},
        body=spec,
        timeout=30,
    )
    if status in (200, 201):
        print(f"  OK   {name} → {spec['litellm_params'].get('model')}")
        existing.add(name)
        return "ok"
    msg = str(body).lower()
    if status in (400, 409) and ("already" in msg or "exist" in msg or "duplicate" in msg):
        print(f"  skip {name} (exists)")
        existing.add(name)
        return "skip"
    print(f"  FAIL {name} HTTP {status}: {body}")
    return "fail"


def register_default_models(
    *,
    region: str = "us-west-2",
    stack_name: str = "litellm",
    include_gpt: bool = True,
    force: bool = False,
) -> dict[str, int]:
    """Register Claude (Bedrock) + GPT (Bedrock Mantle). Returns counts."""
    base, master = resolve_proxy(region, stack_name)
    print(f"Proxy: {base}")
    ensure_bedrock_iam(region, stack_name)

    existing = list_models(base, master)
    info_ids = model_info_map(base, master) if force else {}
    print(f"Existing models: {sorted(existing) or '(none)'}")

    counts = {"ok": 0, "skip": 0, "fail": 0}

    print("\n=== Bedrock (Claude) ===")
    for spec in DEFAULT_BEDROCK_MODELS:
        payload = _with_region(spec, region)
        # Claude uses inference profile api via bedrock/ — drop mantle api_base if any
        payload["litellm_params"].pop("api_base", None)
        counts[register_one(base, master, payload, existing, force=force, info_ids=info_ids)] += 1

    if include_gpt:
        print("\n=== Bedrock Mantle (GPT) ===")
        for spec in DEFAULT_MANTLE_GPT_MODELS:
            payload = _with_region(spec, region)
            counts[
                register_one(base, master, payload, existing, force=force, info_ids=info_ids)
            ] += 1

    print(f"\nDone: ok={counts['ok']} skip={counts['skip']} fail={counts['fail']}")
    print(f"Models now: {sorted(list_models(base, master))}")
    return counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Register default LiteLLM models (Bedrock + Mantle)")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    p.add_argument("--stack-name", default="litellm")
    p.add_argument("--no-gpt", action="store_true", help="Skip GPT Mantle models")
    p.add_argument(
        "--force",
        action="store_true",
        help="Delete+recreate models that already exist (e.g. switch GPT to Mantle)",
    )
    args = p.parse_args(argv)

    counts = register_default_models(
        region=args.region,
        stack_name=args.stack_name,
        include_gpt=not args.no_gpt,
        force=args.force,
    )
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
