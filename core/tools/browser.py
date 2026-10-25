from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from pydantic import BaseModel, Field

from core.tools.registry import BaseTool, RiskTier, ToolResult

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = os.getenv("ARTIFACTS_DIR", os.path.abspath("./artifacts"))
SCREENSHOTS_DIR = os.path.join(ARTIFACTS_DIR, "screenshots")
DOWNLOADS_DIR = os.path.join(ARTIFACTS_DIR, "downloads")


def ensure_browser_artifact_dirs() -> None:
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)


class ElementRef(BaseModel):
    ref: str  # e.g. "e1"
    tag: str  # e.g. "button", "input", "a"
    role: str  # e.g. "button", "textbox", "link"
    name: str | None = None
    id: str | None = None
    label: str | None = None
    text: str | None = None
    value: str | None = None
    type: str | None = None
    selector: str


class BrowserManager:
    """
    Manages Playwright browser instance, contexts, pages, settling, and semantic element snapshots.
    """

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._playwright: Any = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.ref_map: dict[str, ElementRef] = {}

    async def get_page(self) -> Page:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if self._browser is None:
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        if self._context is None:
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                accept_downloads=True,
            )
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    async def settle(self, timeout_ms: int = 1500) -> None:
        """Wait for page load and DOM mutations to settle."""
        if not self._page or self._page.is_closed():
            return
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            try:
                await self._page.wait_for_load_state("networkidle", timeout=800)
            except Exception:
                pass
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.debug(f"Settle warning: {e}")

    async def close(self) -> None:
        if self._page and not self._page.is_closed():
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self.ref_map.clear()

    async def build_snapshot(self) -> str:
        """
        Distills page DOM into stable element references (e1, e2...) with semantic attributes.
        Annotates DOM elements with `data-centralign-ref` attribute for 100% deterministic targeting.
        """
        page = await self.get_page()
        await self.settle()

        js_distiller = """
        () => {
            const elements = [];
            let counter = 1;

            document.querySelectorAll('[data-centralign-ref]').forEach(el => el.removeAttribute('data-centralign-ref'));

            const query = 'h1, h2, h3, [role="alert"], .alert, a[href], button, input, select, textarea, table';
            const nodes = document.querySelectorAll(query);

            for (const node of nodes) {
                const style = window.getComputedStyle(node);
                if (node.type !== 'file' && (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0')) {
                    continue;
                }

                const refId = 'e' + counter++;
                node.setAttribute('data-centralign-ref', refId);

                let label = '';
                if (node.labels && node.labels.length > 0) {
                    label = node.labels[0].innerText.trim();
                } else if (node.getAttribute('aria-label')) {
                    label = node.getAttribute('aria-label').trim();
                } else if (node.placeholder) {
                    label = node.placeholder.trim();
                }

                let text = '';
                if (['button', 'a', 'h1', 'h2', 'h3'].includes(node.tagName.toLowerCase()) || node.getAttribute('role') === 'alert' || node.classList.contains('alert')) {
                    text = node.innerText ? node.innerText.trim() : '';
                }

                let value = null;
                if ('value' in node && node.type !== 'password') {
                    value = node.value;
                }

                elements.push({
                    ref: refId,
                    tag: node.tagName.toLowerCase(),
                    role: node.getAttribute('role') || node.tagName.toLowerCase(),
                    id: node.id || null,
                    name: node.getAttribute('name') || null,
                    type: node.getAttribute('type') || null,
                    label: label || null,
                    text: text || null,
                    value: value || null,
                    href: node.getAttribute('href') || null
                });
            }

            return {
                title: document.title,
                url: window.location.href,
                elements: elements
            };
        }
        """

        raw = await page.evaluate(js_distiller)
        self.ref_map.clear()

        lines: list[str] = [f"Page Title: {raw['title']}", f"URL: {raw['url']}", "", "Interactive & Semantic Elements:"]

        for item in raw["elements"]:
            ref = item["ref"]
            tag = item["tag"]
            self.ref_map[ref] = ElementRef(
                ref=ref,
                tag=tag,
                role=item["role"],
                name=item["name"],
                id=item["id"],
                label=item["label"],
                text=item["text"],
                value=item["value"],
                selector=f'[data-centralign-ref="{ref}"]',
            )

            desc_parts = [f"[{ref}]", tag]
            if item.get("type"):
                desc_parts[1] += f"[{item['type']}]"
            if item.get("label"):
                desc_parts.append(f'label="{item["label"]}"')
            if item.get("text"):
                desc_parts.append(f'text="{item["text"]}"')
            if item.get("id"):
                desc_parts.append(f'id="{item["id"]}"')
            if item.get("name"):
                desc_parts.append(f'name="{item["name"]}"')
            if item.get("value"):
                desc_parts.append(f'value="{item["value"]}"')
            if item.get("href"):
                desc_parts.append(f'href="{item["href"]}"')

            lines.append(" ".join(desc_parts))

        return "\n".join(lines)

    async def resolve_locator(self, target: str):
        page = await self.get_page()
        clean_target = target.strip().strip("[]").lower()

        # 1. Check ref_map
        if clean_target in self.ref_map:
            ref_info = self.ref_map[clean_target]
            return page.locator(ref_info.selector)

        # 2. Check live DOM ref attribute
        ref_loc = page.locator(f'[data-centralign-ref="{clean_target}"]')
        if await ref_loc.count() > 0:
            return ref_loc

        # 3. Try matching visible text / label in ref_map
        for ref_id, info in self.ref_map.items():
            if (info.text and target.lower() in info.text.lower()) or (
                info.label and target.lower() in info.label.lower()
            ):
                return page.locator(info.selector)

        # 4. Try standard CSS selector
        try:
            loc = page.locator(target)
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

        # 5. Try role-based text match
        try:
            button_loc = page.get_by_role("button", name=target)
            if await button_loc.count() > 0:
                return button_loc.first
        except Exception:
            pass

        try:
            link_loc = page.get_by_role("link", name=target)
            if await link_loc.count() > 0:
                return link_loc.first
        except Exception:
            pass

        available_buttons = [
            f"{info.text or info.label or info.name} (ref={ref})"
            for ref, info in self.ref_map.items()
            if info.tag in ("button", "a", "input")
        ]
        sample_choices = available_buttons[:8]
        raise ValueError(
            f"Element '{target}' not found on page. Available interactive elements: {sample_choices}. "
            "Tip: Call browser_snapshot to view the latest element reference IDs."
        )


_global_browser_manager: BrowserManager | None = None


def get_browser_manager() -> BrowserManager:
    global _global_browser_manager
    if _global_browser_manager is None:
        headless = os.getenv("HEADLESS_BROWSER", "true").lower() in ("true", "1", "yes")
        _global_browser_manager = BrowserManager(headless=headless)
    return _global_browser_manager


# ---------------------------------------------------------------------------
# Browser Tools
# ---------------------------------------------------------------------------


class OpenUrlArgs(BaseModel):
    url: str = Field(description="URL to navigate to (e.g. http://localhost:8001/erp/login)")


class OpenUrlTool(BaseTool):
    name = "browser_open_url"
    description = "Navigate browser to a URL and return the initial page snapshot."
    args_schema = OpenUrlArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        url: str = kwargs["url"]
        mgr = get_browser_manager()
        page = await mgr.get_page()
        res = await page.goto(url)
        status = res.status if res else 200
        snapshot = await mgr.build_snapshot()
        return ToolResult.success(
            data={"status": status, "url": page.url, "snapshot": snapshot},
            evidence_refs=[page.url],
        )


class SnapshotArgs(BaseModel):
    pass


class SnapshotTool(BaseTool):
    name = "browser_snapshot"
    description = (
        "Capture a distilled accessibility/DOM snapshot of the current page with numbered element references (e1, e2...)."
    )
    args_schema = SnapshotArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        mgr = get_browser_manager()
        snapshot = await mgr.build_snapshot()
        page = await mgr.get_page()
        return ToolResult.success(data={"url": page.url, "snapshot": snapshot})


class ClickArgs(BaseModel):
    target: str = Field(
        description="Target reference ID (e.g. 'e5'), visible text (e.g. 'Process & Post Bill'), or CSS selector"
    )


class ClickTool(BaseTool):
    name = "browser_click"
    description = "Click an element on the page by reference ID (e.g. 'e1'), text label, or selector."
    args_schema = ClickArgs
    risk_tier = RiskTier.REVERSIBLE_WRITE

    async def _run(self, **kwargs: Any) -> ToolResult:
        target: str = kwargs["target"]
        mgr = get_browser_manager()
        locator = await mgr.resolve_locator(target)
        await locator.click()
        await mgr.settle()
        snapshot = await mgr.build_snapshot()
        page = await mgr.get_page()
        return ToolResult.success(
            data={"action": "click", "target": target, "url": page.url, "snapshot": snapshot}
        )


class TypeTextArgs(BaseModel):
    target: str = Field(description="Target input reference ID (e.g. 'e2') or field name")
    text: str = Field(description="Text to type into input field")
    clear: bool = Field(default=True, description="Whether to clear existing text before typing")


class TypeTextTool(BaseTool):
    name = "browser_type_text"
    description = "Type text into an input field or textarea."
    args_schema = TypeTextArgs
    risk_tier = RiskTier.REVERSIBLE_WRITE

    async def _run(self, **kwargs: Any) -> ToolResult:
        target: str = kwargs["target"]
        text: str = kwargs["text"]
        clear: bool = kwargs.get("clear", True)
        mgr = get_browser_manager()
        locator = await mgr.resolve_locator(target)
        if clear:
            await locator.fill("")
        await locator.fill(text)
        return ToolResult.success(data={"action": "type_text", "target": target, "value": text})


class SelectOptionArgs(BaseModel):
    target: str = Field(description="Target select element reference ID (e.g. 'e4')")
    value: str = Field(description="Option value or text to select")


class SelectOptionTool(BaseTool):
    name = "browser_select_option"
    description = "Select an option from a dropdown (<select>) element."
    args_schema = SelectOptionArgs
    risk_tier = RiskTier.REVERSIBLE_WRITE

    async def _run(self, **kwargs: Any) -> ToolResult:
        target: str = kwargs["target"]
        value: str = kwargs["value"]
        mgr = get_browser_manager()
        locator = await mgr.resolve_locator(target)
        await locator.select_option(value=value)
        return ToolResult.success(data={"action": "select_option", "target": target, "selected": value})


class PressKeyArgs(BaseModel):
    key: str = Field(description="Key to press (e.g. 'Enter', 'Tab', 'Escape')")


class PressKeyTool(BaseTool):
    name = "browser_press_key"
    description = "Press a keyboard key on the active page."
    args_schema = PressKeyArgs
    risk_tier = RiskTier.REVERSIBLE_WRITE

    async def _run(self, **kwargs: Any) -> ToolResult:
        key: str = kwargs["key"]
        mgr = get_browser_manager()
        page = await mgr.get_page()
        await page.keyboard.press(key)
        await mgr.settle()
        return ToolResult.success(data={"action": "press_key", "key": key})


class WaitForArgs(BaseModel):
    target: str = Field(description="Element reference, text, or selector to wait for")
    timeout_ms: int = Field(default=5000, description="Max wait time in milliseconds")


class WaitForTool(BaseTool):
    name = "browser_wait_for"
    description = "Wait for an element to appear on the page."
    args_schema = WaitForArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        target: str = kwargs["target"]
        timeout_ms: int = kwargs.get("timeout_ms", 5000)
        mgr = get_browser_manager()
        page = await mgr.get_page()
        try:
            if target.strip("[]") in mgr.ref_map:
                selector = mgr.ref_map[target.strip("[]")].selector
                await page.wait_for_selector(selector, timeout=timeout_ms)
            else:
                await page.wait_for_selector(target, timeout=timeout_ms)
            return ToolResult.success(data={"waited_for": target, "status": "visible"})
        except Exception as e:
            return ToolResult.failure(f"Timeout waiting for element '{target}' after {timeout_ms}ms: {e}")


class ScreenshotArgs(BaseModel):
    name: str | None = Field(default=None, description="Optional label for the screenshot file")


class ScreenshotTool(BaseTool):
    name = "browser_screenshot"
    description = "Capture a visual PNG screenshot of the current browser page for evidence."
    args_schema = ScreenshotArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        name: str | None = kwargs.get("name")
        ensure_browser_artifact_dirs()
        mgr = get_browser_manager()
        page = await mgr.get_page()

        timestamp = int(time.time() * 1000)
        label = name.replace(" ", "_") if name else "screen"
        filename = f"{timestamp}_{label}.png"
        filepath = os.path.join(SCREENSHOTS_DIR, filename)

        await page.screenshot(path=filepath, full_page=True)
        return ToolResult.success(
            data={"screenshot_path": filepath, "url": page.url},
            evidence_refs=[filepath],
        )


class ExtractTextArgs(BaseModel):
    target: str | None = Field(default=None, description="Element reference/selector, or None for entire page body")


class ExtractTextTool(BaseTool):
    name = "browser_extract_text"
    description = "Extract inner text content from an element or the entire page."
    args_schema = ExtractTextArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        target: str | None = kwargs.get("target")
        mgr = get_browser_manager()
        page = await mgr.get_page()
        if target:
            locator = await mgr.resolve_locator(target)
            text = await locator.inner_text()
        else:
            text = await page.inner_text("body")
        return ToolResult.success(data={"text": text.strip()})


class DownloadFileArgs(BaseModel):
    target: str = Field(description="Download button or link reference (e.g. 'e8') to trigger download")
    save_dir: str | None = Field(default=None, description="Directory to save file (defaults to artifacts/downloads)")


class DownloadFileTool(BaseTool):
    name = "browser_download_file"
    description = "Click a download link or button, intercept the download, and save file to disk."
    args_schema = DownloadFileArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        target: str = kwargs["target"]
        save_dir: str | None = kwargs.get("save_dir")
        ensure_browser_artifact_dirs()
        dest_dir = save_dir or DOWNLOADS_DIR
        os.makedirs(dest_dir, exist_ok=True)

        mgr = get_browser_manager()
        page = await mgr.get_page()
        locator = await mgr.resolve_locator(target)

        async with page.expect_download() as download_info:
            await locator.click()

        download = await download_info.value
        filename = download.suggested_filename
        dest_path = os.path.join(dest_dir, filename)
        await download.save_as(dest_path)

        return ToolResult.success(
            data={"downloaded_file": dest_path, "filename": filename, "size_bytes": os.path.getsize(dest_path)},
            evidence_refs=[dest_path],
        )


class UploadFileArgs(BaseModel):
    target: str = Field(description="File input reference (e.g. 'e7') or selector")
    file_path: str = Field(description="Absolute or relative path to file on disk to upload")


class UploadFileTool(BaseTool):
    name = "browser_upload_file"
    description = "Upload a file from disk into a file input field."
    args_schema = UploadFileArgs
    risk_tier = RiskTier.REVERSIBLE_WRITE

    async def _run(self, **kwargs: Any) -> ToolResult:
        target: str = kwargs["target"]
        file_path: str = kwargs["file_path"]
        if not os.path.exists(file_path):
            return ToolResult.failure(f"File to upload does not exist at: {file_path}")

        mgr = get_browser_manager()
        locator = await mgr.resolve_locator(target)
        await locator.set_input_files(os.path.abspath(file_path))
        return ToolResult.success(
            data={"action": "upload_file", "target": target, "uploaded_file": file_path},
            evidence_refs=[file_path],
        )


class GetTabsArgs(BaseModel):
    pass


class GetTabsTool(BaseTool):
    name = "browser_get_tabs"
    description = "List all open browser pages and tabs."
    args_schema = GetTabsArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        mgr = get_browser_manager()
        if not mgr._context:
            return ToolResult.success(data={"tabs": []})
        tabs = [{"index": i, "url": p.url, "title": await p.title()} for i, p in enumerate(mgr._context.pages)]
        return ToolResult.success(data={"tabs": tabs})
