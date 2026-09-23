"""Default LiteLLM model catalog registered after deploy.

All models route through AWS Bedrock:
  - Claude → bedrock/ (runtime + inference profiles)
  - GPT   → bedrock_mantle/ (Mantle OpenAI-compatible / Responses API)
"""

from __future__ import annotations


DEFAULT_BEDROCK_MODELS: list[dict] = [
    {
        "model_name": "claude-sonnet-4-6",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-sonnet-4-6",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Sonnet 4.6 via Bedrock"},
    },
    {
        "model_name": "claude-sonnet-4-5",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Sonnet 4.5 via Bedrock"},
    },
    {
        "model_name": "claude-opus-4-8",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-opus-4-8",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Opus 4.8 via Bedrock"},
    },
    {
        "model_name": "claude-opus-4-7",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-opus-4-7",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Opus 4.7 via Bedrock"},
    },
    {
        "model_name": "claude-opus-4-6",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-opus-4-6-v1",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Opus 4.6 via Bedrock"},
    },
    {
        "model_name": "claude-opus-4-5",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Opus 4.5 via Bedrock"},
    },
    {
        "model_name": "claude-sonnet-5",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-sonnet-5",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Sonnet 5 via Bedrock"},
    },
    {
        "model_name": "claude-opus-5",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-opus-5",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Opus 5.0 via Bedrock"},
    },
    {
        "model_name": "claude-opus-5-5",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-opus-5-5",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Opus 5.5 via Bedrock"},
    },
    {
        "model_name": "claude-fable-5",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-fable-5",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Fable 5 via Bedrock"},
    },
    {
        "model_name": "claude-fable-5-1",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-fable-5-1",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Fable 5.1 via Bedrock"},
    },
    {
        "model_name": "claude-haiku-4-5",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Haiku 4.5 via Bedrock"},
    },
]

# GPT via Bedrock Mantle (SigV4 / ECS task role — no OpenAI API key).
# Mantle GPT (5.4/5.5) is pinned to us-east-1.
# GPT-5.6 + GPT-6 (Astra/Sol/Luna) use Bedrock Converse + US inference profiles (not Mantle).
MANTLE_GPT_REGION = "us-east-1"
MANTLE_GPT_API_BASE = f"https://bedrock-mantle.{MANTLE_GPT_REGION}.api.aws/openai/v1"
CONVERSE_GPT_REGION = "us-west-2"

DEFAULT_MANTLE_GPT_MODELS: list[dict] = [
    {
        "model_name": "gpt-6-astra",
        "litellm_params": {
            "model": "bedrock/converse/us.openai.gpt-6-astra",
            "aws_region_name": CONVERSE_GPT_REGION,
            "drop_params": True,
        },
        # LiteLLM model DB may not know gpt-6-astra yet; without base_model it is
        # classified as plain "bedrock" and drop_params silently strips tools.
        # Inherit Converse+tool support from a known OpenAI-on-Bedrock profile.
        "model_info": {
            "description": "OpenAI GPT-6 Astra via Bedrock Converse (us.openai.gpt-6-astra, us-west-2)",
            "mode": "chat",
            "base_model": "bedrock/converse/us.openai.gpt-5.6-sol",
            "supports_function_calling": True,
            "supports_tool_choice": True,
        },
    },
    {
        "model_name": "gpt-6-sol",
        "litellm_params": {
            "model": "bedrock/converse/us.openai.gpt-6-sol",
            "aws_region_name": CONVERSE_GPT_REGION,
            "drop_params": True,
        },
        "model_info": {
            "description": "OpenAI GPT-6 Sol via Bedrock Converse (us.openai.gpt-6-sol, us-west-2)",
            "mode": "chat",
            "base_model": "bedrock/converse/us.openai.gpt-5.6-sol",
            "supports_function_calling": True,
            "supports_tool_choice": True,
        },
    },
    {
        "model_name": "gpt-6-luna",
        "litellm_params": {
            "model": "bedrock/converse/us.openai.gpt-6-luna",
            "aws_region_name": CONVERSE_GPT_REGION,
            "drop_params": True,
        },
        "model_info": {
            "description": "OpenAI GPT-6 Luna via Bedrock Converse (us.openai.gpt-6-luna, us-west-2)",
            "mode": "chat",
            "base_model": "bedrock/converse/us.openai.gpt-5.6-luna",
            "supports_function_calling": True,
            "supports_tool_choice": True,
        },
    },
    {
        "model_name": "gpt-5.5",
        "litellm_params": {
            "model": "bedrock_mantle/openai.gpt-5.5",
            "aws_region_name": MANTLE_GPT_REGION,
            "api_base": MANTLE_GPT_API_BASE,
        },
        "model_info": {"description": "OpenAI GPT-5.5 via Bedrock Mantle (us-east-1) — default"},
    },
    {
        "model_name": "gpt-5.4",
        "litellm_params": {
            "model": "bedrock_mantle/openai.gpt-5.4",
            "aws_region_name": MANTLE_GPT_REGION,
            "api_base": MANTLE_GPT_API_BASE,
        },
        "model_info": {"description": "OpenAI GPT-5.4 via Bedrock Mantle (us-east-1)"},
    },
    {
        "model_name": "gpt-5.6-sol",
        "litellm_params": {
            "model": "bedrock/converse/us.openai.gpt-5.6-sol",
            "aws_region_name": CONVERSE_GPT_REGION,
            "drop_params": True,
        },
        "model_info": {
            "description": "OpenAI GPT-5.6 Sol via Bedrock Converse (us.openai.gpt-5.6-sol, us-west-2)"
        },
    },
    {
        "model_name": "gpt-5.6-terra",
        "litellm_params": {
            "model": "bedrock/converse/us.openai.gpt-5.6-terra",
            "aws_region_name": CONVERSE_GPT_REGION,
            "drop_params": True,
        },
        "model_info": {
            "description": "OpenAI GPT-5.6 Terra via Bedrock Converse (us.openai.gpt-5.6-terra, us-west-2)"
        },
    },
    {
        "model_name": "gpt-5.6-luna",
        "litellm_params": {
            "model": "bedrock/converse/us.openai.gpt-5.6-luna",
            "aws_region_name": CONVERSE_GPT_REGION,
            "drop_params": True,
        },
        "model_info": {
            "description": "OpenAI GPT-5.6 Luna via Bedrock Converse (us.openai.gpt-5.6-luna, us-west-2)"
        },
    },
]

# Back-compat alias used by older register_models imports
DEFAULT_OPENAI_MODELS = DEFAULT_MANTLE_GPT_MODELS

# Embeddings via Bedrock (OpenAI-compatible /v1/embeddings on the proxy).
# Used by Graphiti and other RAG clients — set model_info.mode = "embedding".
DEFAULT_BEDROCK_EMBEDDING_MODELS: list[dict] = [
    {
        "model_name": "titan-embed-v2",
        "litellm_params": {
            "model": "bedrock/amazon.titan-embed-text-v2:0",
            "aws_region_name": "us-west-2",
        },
        "model_info": {
            "mode": "embedding",
            "description": "Amazon Titan Text Embeddings V2 (dim 1024/512/256) — Graphiti default",
        },
    },
    {
        "model_name": "titan-embed-v1",
        "litellm_params": {
            "model": "bedrock/amazon.titan-embed-text-v1",
            "aws_region_name": "us-west-2",
        },
        "model_info": {
            "mode": "embedding",
            "description": "Amazon Titan Text Embeddings G1 (legacy)",
        },
    },
    {
        "model_name": "cohere-embed-multilingual-v3",
        "litellm_params": {
            "model": "bedrock/cohere.embed-multilingual-v3",
            "aws_region_name": "us-west-2",
        },
        "model_info": {
            "mode": "embedding",
            "description": "Cohere Embed Multilingual v3 via Bedrock (Korean-friendly)",
        },
    },
]
