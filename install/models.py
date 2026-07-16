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
        "model_name": "claude-opus-4-8",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-opus-4-8",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Opus 4.8 via Bedrock"},
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
        "model_name": "claude-fable-5",
        "litellm_params": {
            "model": "bedrock/us.anthropic.claude-fable-5",
            "aws_region_name": "us-west-2",
        },
        "model_info": {"description": "Claude Fable 5 via Bedrock"},
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

# GPT via Bedrock Mantle (no OpenAI API key — uses ECS task role / SigV4)
DEFAULT_MANTLE_GPT_MODELS: list[dict] = [
    {
        "model_name": "gpt-5.4",
        "litellm_params": {
            "model": "bedrock_mantle/openai.gpt-5.4",
            "aws_region_name": "us-west-2",
            "api_base": "https://bedrock-mantle.us-west-2.api.aws/openai/v1",
        },
        "model_info": {"description": "OpenAI GPT-5.4 via Bedrock Mantle"},
    },
    {
        "model_name": "gpt-5.5",
        "litellm_params": {
            "model": "bedrock_mantle/openai.gpt-5.5",
            "aws_region_name": "us-west-2",
            "api_base": "https://bedrock-mantle.us-west-2.api.aws/openai/v1",
        },
        "model_info": {"description": "OpenAI GPT-5.5 via Bedrock Mantle"},
    },
    {
        "model_name": "gpt-5.6-sol",
        "litellm_params": {
            "model": "bedrock_mantle/openai.gpt-5.6-sol",
            "aws_region_name": "us-west-2",
            "api_base": "https://bedrock-mantle.us-west-2.api.aws/openai/v1",
        },
        "model_info": {"description": "OpenAI GPT-5.6 Sol via Bedrock Mantle"},
    },
    {
        "model_name": "gpt-5.6-terra",
        "litellm_params": {
            "model": "bedrock_mantle/openai.gpt-5.6-terra",
            "aws_region_name": "us-west-2",
            "api_base": "https://bedrock-mantle.us-west-2.api.aws/openai/v1",
        },
        "model_info": {"description": "OpenAI GPT-5.6 Terra via Bedrock Mantle"},
    },
    {
        "model_name": "gpt-5.6-luna",
        "litellm_params": {
            "model": "bedrock_mantle/openai.gpt-5.6-luna",
            "aws_region_name": "us-west-2",
            "api_base": "https://bedrock-mantle.us-west-2.api.aws/openai/v1",
        },
        "model_info": {"description": "OpenAI GPT-5.6 Luna via Bedrock Mantle"},
    },
]

# Back-compat alias used by older register_models imports
DEFAULT_OPENAI_MODELS = DEFAULT_MANTLE_GPT_MODELS
