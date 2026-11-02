"""让 Playwright 使用镜像内系统 Chromium，避免额外下载浏览器。"""

import re
from pathlib import Path

from playwright.sync_api import sync_playwright


def link_browser(path: Path) -> None:
    """为 Playwright 请求的浏览器位置建立系统 Chromium 链接。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to('/usr/bin/chromium')


with sync_playwright() as playwright:
    full = Path(playwright.chromium.executable_path)
    if not full.exists():
        link_browser(full)
    try:
        browser = playwright.chromium.launch()
    except Exception as error:
        missing = re.search(r"Executable doesn't exist at ([^\n]+)", str(error))
        if not missing:
            raise
        link_browser(Path(missing.group(1).strip()))
        browser = playwright.chromium.launch()
    page = browser.new_page()
    page.set_content('<h1>Skill browser ready</h1>')
    assert page.screenshot()
    browser.close()
