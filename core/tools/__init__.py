from __future__ import annotations

from core.tools.browser import (
    ClickTool,
    DownloadFileTool,
    ExtractTextTool,
    GetTabsTool,
    OpenUrlTool,
    PressKeyTool,
    ScreenshotTool,
    SelectOptionTool,
    SnapshotTool,
    TypeTextTool,
    UploadFileTool,
    WaitForTool,
    get_browser_manager,
)
from core.tools.files import ListDirTool, ReadFileTool, SearchFilesTool, WriteFileTool
from core.tools.http import HttpRequestTool
from core.tools.human import AskUserTool, RequestApprovalTool
from core.tools.mail import ReadMailTool, SaveAttachmentTool, SearchMailTool
from core.tools.registry import BaseTool, RiskTier, ToolRegistry, ToolResult, tool_registry


def register_default_tools(registry: ToolRegistry | None = None) -> ToolRegistry:
    """Register all standard CentrAlign tools into the given registry (or global registry)."""
    target = registry or tool_registry

    # Browser tools
    target.register(OpenUrlTool())
    target.register(SnapshotTool())
    target.register(ClickTool())
    target.register(TypeTextTool())
    target.register(SelectOptionTool())
    target.register(PressKeyTool())
    target.register(WaitForTool())
    target.register(ScreenshotTool())
    target.register(ExtractTextTool())
    target.register(DownloadFileTool())
    target.register(UploadFileTool())
    target.register(GetTabsTool())

    # File tools
    target.register(ListDirTool())
    target.register(ReadFileTool())
    target.register(WriteFileTool())
    target.register(SearchFilesTool())

    # HTTP tool
    target.register(HttpRequestTool())

    # Mail tools
    target.register(SearchMailTool())
    target.register(ReadMailTool())
    target.register(SaveAttachmentTool())

    # Human-in-the-loop tools
    target.register(AskUserTool())
    target.register(RequestApprovalTool())

    return target


# Initialize default registry
register_default_tools(tool_registry)

__all__ = [
    "BaseTool",
    "RiskTier",
    "ToolResult",
    "ToolRegistry",
    "tool_registry",
    "register_default_tools",
    "get_browser_manager",
]
