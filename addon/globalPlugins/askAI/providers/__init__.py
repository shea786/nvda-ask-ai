# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: the AI services the add-on can use, each through its official command line program.
# Pure standard library, no NVDA imports.

from .base import (  # noqa: F401
	KIND_CANCELLED,
	KIND_CONFIG,
	KIND_NETWORK,
	KIND_NOT_INSTALLED,
	KIND_NOT_LOGGED_IN,
	KIND_OTHER,
	KIND_TIMEOUT,
	KIND_USAGE_LIMIT,
	REASON_BUSY,
	REASON_CUSTOM_PATH,
	REASON_EMPTY_ANSWER,
	REASON_INCOMPLETE_INSTALL,
	REASON_NODE_MISSING,
	REASON_OUTDATED,
	REASON_PROJECT_MISSING,
	STATE_API_KEY,
	STATE_SIGNED_OUT,
	STATE_SUBSCRIPTION,
	ProviderError,
)
from .claude import ClaudeProvider
from .codex import CodexProvider

DEFAULT_PROVIDER = CodexProvider.providerId
# In the order they are offered.
PROVIDER_CLASSES = (CodexProvider, ClaudeProvider)
_BY_ID = {cls.providerId: cls for cls in PROVIDER_CLASSES}


def ids():
	return [cls.providerId for cls in PROVIDER_CLASSES]


def isKnown(providerId):
	return providerId in _BY_ID


def get(providerId):
	"""The provider class for an id; unknown ids (for example from a newer version) fall back to the default."""
	return _BY_ID.get(providerId) or _BY_ID[DEFAULT_PROVIDER]


def create(providerId, **kwargs):
	"""A provider instance (it runs the service's program)."""
	return get(providerId)(**kwargs)


def displayName(providerId):
	return get(providerId).displayName


def shortName(providerId):
	return get(providerId).shortName
