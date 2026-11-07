from .anthropic import AnthropicProvider
from .aws import AWSProvider
from .base import BreakNotSupported, Provider, ProviderError, Snapshot
from .cloudflare import CloudflareProvider
from .openai import OpenAIProvider
from .vercel import VercelProvider

REGISTRY = {p.name: p for p in (OpenAIProvider, AnthropicProvider, CloudflareProvider, VercelProvider, AWSProvider)}
