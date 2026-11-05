"""Provider presets: the dropdown you create providers from.

A preset only pre-fills a new provider row — everything stays editable
afterwards. The DB is the catalog; this is just a convenient starting point.
"""

from __future__ import annotations

PRESETS: dict[str, dict] = {
    "openai": {
        "display_name": "OpenAI",
        "kind": "litellm",
        "litellm_prefix": "openai",
        "env_key": "OPENAI_API_KEY",
        "base_url": None,
        "models": [
            {"model_id": "gpt-5.2", "skill": "high", "cost": "high"},
            {"model_id": "gpt-5.2-mini", "skill": "medium", "cost": "low"},
        ],
    },
    "anthropic": {
        "display_name": "Anthropic",
        "kind": "litellm",
        "litellm_prefix": "anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "base_url": None,
        "models": [
            {"model_id": "claude-opus-4-6", "skill": "high", "cost": "high"},
            {"model_id": "claude-sonnet-4-5", "skill": "medium", "cost": "medium"},
            {"model_id": "claude-haiku-4-5", "skill": "low", "cost": "low"},
        ],
    },
    "zai": {
        "display_name": "Z.ai API (pay-as-you-go)",
        "kind": "openai_compatible",
        "litellm_prefix": None,
        "env_key": "ZAI_API_KEY",
        "base_url": "https://api.z.ai/api/paas/v4",
        "models": [
            {"model_id": "glm-4.7", "skill": "medium", "cost": "medium"},
            {"model_id": "glm-4.7-flash", "skill": "low", "cost": "low"},
        ],
    },
    "zai-plan": {
        "display_name": "Z.ai GLM Coding Plan (subscription)",
        "kind": "litellm",
        "litellm_prefix": "anthropic",
        "env_key": "ZAI_API_KEY",
        "base_url": "https://api.z.ai/api/anthropic",
        "models": [
            {"model_id": "glm-4.7", "skill": "medium", "cost": "low"},
            {"model_id": "glm-4.7-flash", "skill": "low", "cost": "low"},
        ],
    },
    "claude-max": {
        "display_name": "Claude Max (Claude Code OAuth)",
        # runs through the Claude Code CLI: the key is the token from
        # `claude setup-token` (sk-ant-oat01-...), which is not an API key
        "kind": "claude_code",
        "litellm_prefix": None,
        "env_key": "CLAUDE_CODE_OAUTH_TOKEN",
        "base_url": None,
        "models": [
            {"model_id": "claude-opus-5-5", "skill": "high", "cost": "high"},
            {"model_id": "claude-sonnet-5-5", "skill": "medium", "cost": "medium"},
            {"model_id": "claude-haiku-5-5", "skill": "low", "cost": "low"},
        ],
    },
    "openrouter": {
        "display_name": "OpenRouter",
        "kind": "litellm",
        "litellm_prefix": "openrouter",
        "env_key": "OPENROUTER_API_KEY",
        "base_url": None,
        "models": [
            {"model_id": "anthropic/claude-sonnet-4.5", "skill": "high", "cost": "high"},
            {"model_id": "openai/gpt-5.2", "skill": "high", "cost": "high"},
            {"model_id": "z-ai/glm-4.7", "skill": "medium", "cost": "medium"},
            {"model_id": "deepseek/deepseek-v4", "skill": "medium", "cost": "low"},
        ],
    },
    "gemini": {
        "display_name": "Google Gemini",
        "kind": "litellm",
        "litellm_prefix": "gemini",
        "env_key": "GEMINI_API_KEY",
        "base_url": None,
        "models": [
            {"model_id": "gemini-3-pro", "skill": "high", "cost": "medium"},
            {"model_id": "gemini-3-flash", "skill": "low", "cost": "low"},
        ],
    },
    "ollama": {
        "display_name": "Ollama (local)",
        "kind": "openai_compatible",
        "litellm_prefix": None,
        "env_key": None,
        "base_url": "http://localhost:11434/v1",
        "models": [
            {"model_id": "llama3.3", "skill": "low", "cost": "low"},
            {"model_id": "qwen2.5-coder", "skill": "medium", "cost": "low"},
        ],
    },
    "custom": {
        "display_name": "Custom (OpenAI-compatible)",
        "kind": "openai_compatible",
        "litellm_prefix": None,
        "env_key": None,
        "base_url": "",
        "models": [],
    },
}

# The built-in test provider, seeded as a protected row.
ECHO_PROVIDER = {
    "id": "echo",
    "display_name": "Echo (test)",
    "kind": "echo",
    "litellm_prefix": None,
    "env_key": None,
    "base_url": None,
    "protected": True,
    "models": [{"model_id": "dev", "skill": "low", "cost": "low"}],
}
