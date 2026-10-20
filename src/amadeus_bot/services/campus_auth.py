from __future__ import annotations

import os
import re
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PORTAL_LIST_URL = "http://my.bupt.edu.cn/list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1154"
JWGL_LOGIN_URL = "https://jwgl.bupt.edu.cn/jsxsd/"
JWGL_HOME_URL = "https://jwgl.bupt.edu.cn/jsxsd/framework/xsMain_bjyddx.jsp"


class CampusSessionExpired(RuntimeError):
    pass


class CampusAuthenticator:
    def __init__(self) -> None:
        self.password_file = _resolve_project_path(os.getenv("AMADEUS_PASSWORD_FILE", "../password.txt"))

    @property
    def available(self) -> bool:
        return not os.getenv("AMADEUS_SIMULATOR_ROOT") and self.password_file.is_file()

    async def login_portal(self) -> None:
        account, password = self._load_credentials()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self._portal_headless(),
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(PORTAL_LIST_URL, wait_until="domcontentloaded", timeout=30_000)
                if "auth.bupt.edu.cn/authserver/login" not in page.url:
                    await self._save_cookie_header(context, PORTAL_LIST_URL, "portal")
                    return
                await self._submit_portal_login(page, account, password)
                if not re.match(r"^https?://my\.bupt\.edu\.cn/", page.url):
                    raise RuntimeError(f"信息门户自动登录未成功；当前页面 {page.url}")
                await page.goto(PORTAL_LIST_URL, wait_until="domcontentloaded", timeout=30_000)
                if "auth.bupt.edu.cn/authserver/login" in page.url:
                    raise RuntimeError("信息门户自动登录后仍返回统一认证页")
                await self._save_cookie_header(context, PORTAL_LIST_URL, "portal")
            finally:
                await browser.close()

    @staticmethod
    async def _submit_portal_login(page: Page, account: str, password: str) -> None:
        """Submit the current BUPT CAS login UI.

        The public page keeps an old hidden form in its top document, while the
        actual login controls live in ``#loginIframe``.  Prefer the real UI and
        retain the hidden form only as a compatibility fallback.
        """
        if await page.locator("#loginIframe").count():
            frame = page.frame_locator("#loginIframe")
            # The iframe response is occasionally decoded with the wrong
            # charset in headless Chromium, so avoid Chinese text selectors.
            login_tabs = frame.locator('a[href="javascript:;"]')
            await login_tabs.nth(1).wait_for(state="visible", timeout=15_000)
            await login_tabs.nth(1).click()

            username = frame.locator("#username")
            password_box = frame.locator("#password")
            await username.wait_for(state="visible", timeout=15_000)
            await username.fill(account)
            await password_box.fill(password)
            captcha = frame.locator("#cptValue")
            if await captcha.is_visible():
                raise RuntimeError("信息门户本次登录要求图形验证码，无法自动续登")
            await frame.locator("input.submit-btn:visible").first.click()
        else:
            username = page.locator('input[name="username"]')
            password_box = page.locator('input[name="password"]')
            if not await username.is_visible():
                await page.locator("#default").evaluate("element => { element.style.display = 'block'; }")
            await username.wait_for(state="visible", timeout=15_000)
            await username.fill(account)
            await password_box.fill(password)
            await page.locator('input[type="submit"]').click()

        try:
            await page.wait_for_url(
                re.compile(r"^https?://my\.bupt\.edu\.cn/"),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("信息门户登录表单提交后没有进入信息门户") from exc

    async def login_jwgl(self) -> None:
        account, password = self._load_credentials()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self._headless())
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(JWGL_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                await page.locator('input[name="userAccount"]').fill(account)
                await page.locator('input[name="userPassword"]').fill(password)
                await page.get_by_role("button", name="登 录").click()
                await page.wait_for_url(re.compile(r"/jsxsd/framework/"), timeout=30_000)
                await page.goto(JWGL_HOME_URL, wait_until="domcontentloaded", timeout=30_000)
                if await _looks_like_jwgl_login(page):
                    raise RuntimeError("教务系统自动登录后仍返回登录页")
                await self._save_cookie_header(context, JWGL_HOME_URL, "jwgl")
            finally:
                await browser.close()

    def _load_credentials(self) -> tuple[str, str]:
        if os.getenv("AMADEUS_SIMULATOR_ROOT"):
            raise RuntimeError("模拟器不执行校园自动续登")
        if not self.password_file.is_file():
            raise RuntimeError(f"自动登录凭据文件不存在：{self.password_file}")
        lines = self.password_file.read_text(encoding="utf-8-sig").splitlines()
        if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
            raise RuntimeError("password.txt 必须第一行为账号、第二行为密码")
        return lines[0].strip(), lines[1].strip()

    @staticmethod
    def _headless() -> bool:
        return os.getenv("AMADEUS_CAMPUS_BROWSER_HEADLESS", "true").strip().lower() not in {
            "0",
            "false",
            "no",
        }

    @staticmethod
    def _portal_headless() -> bool:
        return os.getenv("AMADEUS_PORTAL_BROWSER_HEADLESS", "true").strip().lower() not in {
            "0",
            "false",
            "no",
        }

    async def _save_cookie_header(
        self,
        context: BrowserContext,
        target_url: str,
        source: str,
    ) -> None:
        cookies = await context.cookies([target_url])
        if not cookies:
            raise RuntimeError(f"{source} 自动登录成功但没有获得会话 Cookie")
        # RFC 6265 requires longer (more specific) paths first when names repeat.
        cookies.sort(key=lambda item: -len(str(item.get("path") or "/")))
        header = "; ".join(f"{item['name']}={item['value']}" for item in cookies)
        variable = "AMADEUS_PORTAL_COOKIE_FILE" if source == "portal" else "AMADEUS_JWGL_COOKIE_FILE"
        default = "secrets/portal-cookie.txt" if source == "portal" else "secrets/jwgl-cookie.txt"
        path = _resolve_project_path(os.getenv(variable, default))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(header, encoding="utf-8")
        temporary.replace(path)


def _resolve_project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


async def _looks_like_jwgl_login(page: Page) -> bool:
    return "/login" in page.url.lower() or await page.locator('input[name="userPassword"]').count() > 0
