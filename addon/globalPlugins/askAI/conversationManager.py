# -*- coding: UTF-8 -*-
# Ask AI NVDA add-on: the conversation manager (continue, rename, archive, delete, export, import).

import os

import wx

import addonHandler
import ui
from gui import guiHelper
from gui.dpiScalingHelper import DpiScalingHelperMixinWithoutInit
from logHandler import log

from . import conversations, dialogs, exporters, providers, search
from .textUtils import stripMarkdown

addonHandler.initTranslation()

ANNOUNCE_DELAY_MS = 300
# Filter the list this long after the last key press in the Search field.
SEARCH_DEBOUNCE_MS = 300


def conversationBlocks(conversation, raw=False):
	"""Export blocks for a whole conversation: a heading per speaker, then the text."""
	blocks = []
	for message in conversation.messages:
		role, text = message.get("role"), message.get("text", "")
		if role == conversations.ROLE_USER:
			# Translators: Heading for the user's question in an exported conversation.
			blocks.append(("heading", _("You")))
			blocks.append(("text", text))
		elif role == conversations.ROLE_ASSISTANT:
			# The service that wrote the answer ("ChatGPT", "Claude"): a product name, not translated.
			blocks.append(("heading", providers.shortName(message.get("provider") or conversation.provider)))
			blocks.append(("text", text if raw else (stripMarkdown(text) or text)))
		elif role == conversations.ROLE_ERROR:
			# Translators: Heading for an error in an exported conversation.
			blocks.append(("heading", _("Error")))
			blocks.append(("text", text))
		else:
			blocks.append(("text", text))
	return blocks


class ConversationManagerDialog(DpiScalingHelperMixinWithoutInit, wx.Dialog):
	"""Lists saved conversations. Mnemonics: S V H C R A D X I."""

	def __init__(self, parent):
		# Translators: Title of the conversation manager.
		super().__init__(parent, title=_("Ask AI conversations"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self.chosen = None
		self._items = []
		self._searchTimer = None
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		sHelper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)

		self.searchCtrl = sHelper.addLabeledControl(
			# Translators: Label of the field that filters the conversations by title and message text.
			_("&Search:"),
			wx.TextCtrl,
			style=wx.TE_PROCESS_ENTER,
			size=(self.scaleSize(300), -1),
		)
		self.searchCtrl.Bind(wx.EVT_TEXT, self.onSearchText)
		# Enter filters at once and continues the first match.
		self.searchCtrl.Bind(wx.EVT_TEXT_ENTER, self.onSearchEnter)

		self.listCtrl = sHelper.addLabeledControl(
			# Translators: Label of the list of saved conversations.
			_("Con&versations:"),
			wx.ListCtrl,
			style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
			size=self.scaleSize((710, 260)),
		)
		for column, (label, width) in enumerate(
			(
				# Translators: Column in the conversation list.
				(_("Title"), 240),
				# Translators: Column in the conversation list: when it was last used.
				(_("Updated"), 120),
				# Translators: Column in the conversation list: number of questions and answers.
				(_("Messages"), 70),
				# Translators: Column in the conversation list: the project folder (full path).
				(_("Project"), 200),
				# Translators: Column in the conversation list: the AI service, e.g. "ChatGPT" or "Claude".
				(_("Service"), 80),
			),
		):
			self.listCtrl.InsertColumn(column, label, width=self.scaleSize(width))
		self.listCtrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda evt: self.onContinue(None))
		self.listCtrl.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda evt: self._updateButtons())
		self.listCtrl.Bind(wx.EVT_LIST_ITEM_DESELECTED, lambda evt: self._updateButtons())
		self.listCtrl.Bind(wx.EVT_KEY_DOWN, self.onListKey)

		# Translators: Shown instead of the list when there are no saved conversations.
		self.emptyText = sHelper.addItem(wx.StaticText(self, label=_("No saved conversations yet.")))

		self.archivedCheck = sHelper.addItem(
			# Translators: Checkbox that also lists archived conversations.
			wx.CheckBox(self, label=_("Show arc&hived")),
		)
		self.archivedCheck.Bind(wx.EVT_CHECKBOX, lambda evt: self.populate(self._selectedId()))

		actionButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that continues the selected conversation in the Ask AI dialog.
		self.continueButton = actionButtons.addButton(self, label=_("&Continue"))
		self.continueButton.SetDefault()
		self.continueButton.Bind(wx.EVT_BUTTON, self.onContinue)
		# Translators: Button that renames the selected conversation.
		self.renameButton = actionButtons.addButton(self, label=_("&Rename..."))
		self.renameButton.Bind(wx.EVT_BUTTON, self.onRename)
		# Translators: Button that archives the selected conversation (hides it from the list).
		self.archiveButton = actionButtons.addButton(self, label=_("&Archive"))
		self.archiveButton.Bind(wx.EVT_BUTTON, self.onArchive)
		# Translators: Button that permanently deletes the selected conversation.
		self.deleteButton = actionButtons.addButton(self, label=_("&Delete..."))
		self.deleteButton.Bind(wx.EVT_BUTTON, self.onDelete)
		sHelper.addItem(actionButtons)

		fileButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: Button that saves the selected conversation as a document.
		self.exportButton = fileButtons.addButton(self, label=_("E&xport..."))
		self.exportButton.Bind(wx.EVT_BUTTON, self.onExport)
		# Translators: Button that adds a conversation from a .json file.
		importButton = fileButtons.addButton(self, label=_("&Import..."))
		importButton.Bind(wx.EVT_BUTTON, self.onImport)
		# Translators: Button that closes the conversation manager.
		closeButton = fileButtons.addButton(self, id=wx.ID_CLOSE, label=_("Close"))
		closeButton.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CANCEL))
		sHelper.addDialogDismissButtons(fileButtons, separated=True)

		mainSizer.Add(sHelper.sizer, proportion=1, border=guiHelper.BORDER_FOR_DIALOGS, flag=wx.ALL | wx.EXPAND)
		self.SetSizer(mainSizer)
		mainSizer.Fit(self)
		self.CentreOnScreen()
		self.SetEscapeId(wx.ID_CLOSE)
		self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

		current = dialogs.state.current
		if current is not None:
			dialogs.state.save(current)
		self.populate(current.id if current is not None else None)
		self._focusList()
		count = len(self._items)
		if count:
			# Translators: Announced when the conversation manager opens. {count} is a number.
			message = ngettext("{count} conversation", "{count} conversations", count).format(count=count)
		else:
			message = self.emptyText.GetLabel()
		wx.CallLater(ANNOUNCE_DELAY_MS, ui.message, message)

	# List

	@property
	def store(self):
		return dialogs.state.store

	def _resolve(self, conversation):
		"""Use the live current conversation object when it is the same conversation."""
		current = dialogs.state.current
		if current is not None and current.id == conversation.id:
			return current
		return conversation

	@property
	def query(self):
		return self.searchCtrl.GetValue().strip()

	def onSearchText(self, evt):
		# Debounced: filter about 300 ms after the last key press.
		if self._searchTimer is not None:
			self._searchTimer.Stop()
		self._searchTimer = wx.CallLater(SEARCH_DEBOUNCE_MS, self._applySearch)

	def _applySearch(self, announce=True):
		self._searchTimer = None
		if not self:
			return
		self.populate(self._selectedId())
		if announce:
			ui.message(searchSummary(self.query, self._items))

	def onSearchEnter(self, evt):
		if self._searchTimer is not None:
			self._searchTimer.Stop()
			self._searchTimer = None
		self._applySearch(announce=False)
		if not self._items:
			ui.message(searchSummary(self.query, self._items))
			return
		self.listCtrl.Select(0)
		self.onContinue(None)

	def populate(self, selectId=None, selectIndex=None):
		includeArchived = self.archivedCheck.GetValue()
		listed = self.store.list(includeArchived=includeArchived)
		# Title and message text, across all (and with Show archived, archived) conversations.
		self._items = [self._resolve(c) for c in search.filterConversations(listed, self.query)]
		self.listCtrl.DeleteAllItems()
		# Translators: Title shown for a conversation that has no title yet.
		untitled = _("Untitled conversation")
		# Translators: Added to the title of an archived conversation in the list.
		archivedMark = _("{title} (archived)")
		for index, conversation in enumerate(self._items):
			title = conversation.title or untitled
			if conversation.archived:
				title = archivedMark.format(title=title)
			self.listCtrl.InsertItem(index, title)
			self.listCtrl.SetItem(
				index,
				1,
				conversations.friendlyTime(
					conversation.updated,
					# Translators: Prefix for times today in the conversation list, e.g. "Today 14:05".
					todayLabel=_("Today"),
					# Translators: Prefix for times yesterday in the conversation list.
					yesterdayLabel=_("Yesterday"),
				),
			)
			self.listCtrl.SetItem(index, 2, str(conversation.turnCount))
			# The full path: a basename would be empty for a drive root and hide where the folder is.
			self.listCtrl.SetItem(index, 3, conversation.projectFolder)
			self.listCtrl.SetItem(index, 4, providers.shortName(conversation.provider))
		if self.query:
			# Translators: Shown instead of the list when no conversation matches the search.
			self.emptyText.SetLabel(_("No conversations match."))
		else:
			self.emptyText.SetLabel(_("No saved conversations yet."))
		self.emptyText.Show(not self._items)
		self.Layout()
		if self._items:
			index = 0
			if selectId is not None:
				index = next((i for i, c in enumerate(self._items) if c.id == selectId), 0)
			elif selectIndex is not None:
				index = max(0, min(selectIndex, len(self._items) - 1))
			self.listCtrl.Select(index)
			self.listCtrl.Focus(index)
			self.listCtrl.EnsureVisible(index)
		self._updateButtons()

	def _focusList(self):
		"""Focus the list, or the Search field when the (filtered) list is empty."""
		if self._items:
			self.listCtrl.SetFocus()
		else:
			self.searchCtrl.SetFocus()

	def _selectedIndex(self):
		return self.listCtrl.GetFirstSelected()

	def _selected(self):
		index = self._selectedIndex()
		return self._items[index] if 0 <= index < len(self._items) else None

	def _selectedId(self):
		conversation = self._selected()
		return conversation.id if conversation is not None else None

	def _updateButtons(self):
		conversation = self._selected()
		for button in (self.continueButton, self.renameButton, self.archiveButton, self.deleteButton, self.exportButton):
			button.Enable(conversation is not None)
		if conversation is not None and conversation.archived:
			# Translators: Button that brings an archived conversation back to the list.
			self.archiveButton.SetLabel(_("Un&archive"))
		else:
			self.archiveButton.SetLabel(_("&Archive"))

	def _refreshAskDialog(self):
		dialog = dialogs.AskAIDialog.getInstance()
		if dialog is not None:
			dialog.loadConversation()

	# Keyboard

	def onCharHook(self, evt):
		if evt.GetKeyCode() == wx.WXK_ESCAPE:
			if self._searchTimer is not None:
				self._searchTimer.Stop()
				self._searchTimer = None
			self.EndModal(wx.ID_CANCEL)
			return
		evt.Skip()

	def onListKey(self, evt):
		if evt.GetKeyCode() == wx.WXK_DELETE:
			self.onDelete(None)
			return
		evt.Skip()

	# Actions

	def onContinue(self, evt):
		conversation = self._selected()
		if conversation is None:
			return
		if conversation.archived:
			conversation.archived = False
			dialogs.state.save(conversation)
		self.chosen = conversation
		self.EndModal(wx.ID_OK)

	def onRename(self, evt):
		conversation = self._selected()
		if conversation is None:
			return
		with wx.TextEntryDialog(
			self,
			# Translators: Prompt for a conversation's new title. Leaving it empty restores the automatic title.
			_("New title (leave empty for the automatic title):"),
			# Translators: Title of the rename dialog.
			_("Rename conversation"),
			conversation.title,
		) as renameDialog:
			if renameDialog.ShowModal() != wx.ID_OK:
				return
			conversation.rename(renameDialog.GetValue())
		dialogs.state.save(conversation)
		self.populate(conversation.id)
		self._refreshAskDialog()
		self._focusList()

	def onArchive(self, evt):
		conversation = self._selected()
		if conversation is None:
			return
		index = self._selectedIndex()
		conversation.archived = not conversation.archived
		dialogs.state.save(conversation)
		if conversation.archived:
			# Translators: Announced after archiving a conversation.
			message = _("Archived")
		else:
			# Translators: Announced after unarchiving a conversation.
			message = _("Unarchived")
		if self.archivedCheck.GetValue():
			self.populate(conversation.id)
		else:
			self.populate(selectIndex=index)
		self._focusList()
		wx.CallLater(ANNOUNCE_DELAY_MS, ui.message, message)

	def onDelete(self, evt):
		conversation = self._selected()
		if conversation is None:
			return
		index = self._selectedIndex()
		title = conversation.title or _("Untitled conversation")
		# Translators: Confirmation before permanently deleting a conversation. {title} is its title.
		if not dialogs.askYesNo(_("Permanently delete the conversation {title}?").format(title=title), _("Ask AI"), self):
			return
		state = dialogs.state
		if state.pending is not None and state.pending.conversationId == conversation.id:
			dialogs.cancelRequest()
		try:
			self.store.delete(conversation.id)
		except OSError as e:
			log.error("Ask AI: could not delete a conversation", exc_info=True)
			# Translators: Error when a conversation file could not be deleted. {error} is the reason.
			dialogs.showError(_("The conversation could not be deleted: {error}").format(error=e), self)
			return
		if state.current is not None and state.current.id == conversation.id:
			state.current = state.newConversation()
			self._refreshAskDialog()
		self.populate(selectIndex=index)
		self._focusList()
		# Translators: Announced after deleting a conversation.
		wx.CallLater(ANNOUNCE_DELAY_MS, ui.message, _("Deleted"))

	def onExport(self, evt):
		conversation = self._selected()
		if conversation is None:
			return
		title = conversation.title or _("Untitled conversation")
		# Translators: Title of the dialogs for exporting a conversation.
		chosen = dialogs.chooseFormatAndName(self, exporters.sanitizeFileName(title), _("Export conversation"))
		if chosen is None:
			return
		formatKey, baseName = chosen
		folder = dialogs.existingProjectFolder(conversation) or dialogs.documentsFolder()
		dialogs.saveDocument(
			self,
			folder,
			formatKey,
			baseName,
			title,
			conversationBlocks(conversation),
			markdownBlocks=conversationBlocks(conversation, raw=True),
		)

	def onImport(self, evt):
		with wx.FileDialog(
			self,
			# Translators: Title of the file chooser for importing a conversation.
			_("Import a conversation"),
			# Translators: File type filter for conversation files.
			wildcard=_("Ask AI conversations (*.json)|*.json|All files (*.*)|*.*"),
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
		) as fileDialog:
			if fileDialog.ShowModal() != wx.ID_OK:
				return
			path = fileDialog.GetPath()
		try:
			conversation = self.store.importFile(path)
		except (OSError, ValueError) as e:
			log.debugWarning("Ask AI: import failed: %s" % e)
			# Translators: Error when a file is not an Ask AI conversation. {error} is the reason.
			dialogs.showError(_("This file is not an Ask AI conversation: {error}").format(error=e), self)
			return
		self.archivedCheck.SetValue(False)
		self.populate(conversation.id)
		self._focusList()
		# Translators: Announced after importing. {title} is the conversation title.
		wx.CallLater(ANNOUNCE_DELAY_MS, ui.message, _("Imported {title}").format(title=conversation.title or _("Untitled conversation")))


def searchSummary(query, items):
	"""After filtering: "3 match; first: Trip to Paris", "No conversations match", or the count."""
	count = len(items)
	if not query:
		# Translators: Announced when the search field is emptied. {count} is a number.
		return ngettext("{count} conversation", "{count} conversations", count).format(count=count)
	if not count:
		# Translators: Announced when no conversation matches the search.
		return _("No conversations match")
	# Translators: Announced after filtering, e.g. "3 match; first: Trip to Paris".
	return ngettext("{count} match; first: {title}", "{count} match; first: {title}", count).format(
		count=count,
		title=items[0].title or _("Untitled conversation"),
	)


def continuingText(title, template="Continuing {title}."):
	""""Continuing Trip to Paris." without a doubled full stop after a title ending in ".", "?" or "!"."""
	title = title.rstrip(".")
	text = template.format(title=title)
	if title.endswith(("?", "!")):
		text = text.replace(title + ".", title, 1)
	return text


def showConversationManager(parent):
	"""Show the manager modally; continuing a conversation makes it current and shows the Ask dialog."""
	dialog = ConversationManagerDialog(parent)
	try:
		result = dialog.ShowModal()
		chosen = dialog.chosen
	finally:
		dialog.Destroy()
	if result != wx.ID_OK or chosen is None:
		return
	restored = dialogs.switchConversation(chosen)
	askDialog = dialogs.AskAIDialog.getInstance()
	if askDialog is None:
		dialogs.openAskDialog()
		askDialog = dialogs.AskAIDialog.getInstance()
		restored = bool(askDialog is not None and chosen.draft and askDialog.questionCtrl.GetValue() == chosen.draft)
	else:
		askDialog.Raise()
	# Translators: Announced after continuing a conversation. {title} is its title.
	message = continuingText(chosen.title or _("Untitled conversation"), _("Continuing {title}."))
	if restored:
		# Translators: Added after "Continuing <title>." when the conversation's unsent question came back.
		message += " " + _("Draft restored.")
	if askDialog is not None:
		askDialog.questionCtrl.SetFocus()
		dialogs.announceWhenFocused(askDialog.questionCtrl, message)
	else:
		ui.message(message)
