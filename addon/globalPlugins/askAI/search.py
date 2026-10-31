# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: searching the Conversation box and filtering saved conversations.
# Pure standard library, no NVDA imports.


def _fold(text):
	return (text or "").casefold()


def findMatches(text, query):
	"""Start offsets of every case-insensitive, non-overlapping occurrence of query in text.
	casefold() can change string lengths (for example "ß" becomes "ss"); in that rare case the
	search falls back to lower(), which keeps offsets aligned with the original text.
	"""
	query = (query or "").strip()
	if not query or not text:
		return []
	foldedText, foldedQuery = _fold(text), _fold(query)
	if len(foldedText) != len(text):
		foldedText, foldedQuery = text.lower(), query.lower()
		if len(foldedText) != len(text):
			foldedText, foldedQuery = text, query
	matches = []
	start = foldedText.find(foldedQuery)
	while start >= 0:
		matches.append(start)
		start = foldedText.find(foldedQuery, start + max(1, len(foldedQuery)))
	return matches


def lineAt(text, index):
	"""The whole line of text that contains index, stripped."""
	start = text.rfind("\n", 0, index) + 1
	end = text.find("\n", index)
	if end < 0:
		end = len(text)
	return text[start:end].strip()


def step(current, count, forward=True):
	"""Next match index from current (-1 for none yet). Returns (index, wrapped)."""
	if count <= 0:
		return -1, False
	if current < 0:
		return (0 if forward else count - 1), False
	if forward:
		if current + 1 >= count:
			return 0, True
		return current + 1, False
	if current - 1 < 0:
		return count - 1, True
	return current - 1, False


def nearestMatch(matches, caret, forward=True):
	"""Index of the first match at or after caret (forward) or before it (backward), wrapping. -1 if none."""
	if not matches:
		return -1
	if forward:
		for i, start in enumerate(matches):
			if start >= caret:
				return i
		return 0
	for i in range(len(matches) - 1, -1, -1):
		if matches[i] < caret:
			return i
	return len(matches) - 1


def controlPosition(value, index, lastPosition):
	"""Map a string index in value to a text control position.
	Windows multi-line edit controls may count each line break as two positions ("\\r\\n"), so the
	extra per line break is measured from the control's own last position.
	"""
	breaks = value.count("\n")
	extra = (lastPosition - len(value)) // breaks if breaks else 0
	extra = 1 if extra >= 1 else 0
	return index + extra * value.count("\n", 0, index)


def conversationMatches(conversation, query):
	"""True if query occurs (case-insensitively) in the conversation's title or any message text."""
	query = _fold((query or "").strip())
	if not query:
		return True
	if query in _fold(conversation.title):
		return True
	return any(query in _fold(message.get("text", "")) for message in conversation.messages)


def filterConversations(conversations, query):
	return [conversation for conversation in conversations if conversationMatches(conversation, query)]
