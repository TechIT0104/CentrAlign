from .base import Registry, Tool, ToolContext
from .browser import BROWSER_TOOLS, BrowserManager
from .files import CsvRead, CsvUpsert, EmailSend, FsList, FsRead, FsWrite, KnowledgeSearch
from .http import HttpRequest


def build_registry() -> Registry:
    reg = Registry()
    for cls in (FsRead, FsList, FsWrite, CsvRead, CsvUpsert, EmailSend, KnowledgeSearch, HttpRequest, *BROWSER_TOOLS):
        reg.register(cls())
    return reg


__all__ = ["Registry", "Tool", "ToolContext", "BrowserManager", "build_registry"]
