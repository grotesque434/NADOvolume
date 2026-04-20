from playwright.sync_api import Page, sync_playwright

from config import ProxyConfig
from sheets_client import PostRow


class ContwsBot:
    def __init__(self, headless: bool = False) -> None:
        self._headless = headless

    def run_profile(
        self,
        profile_id: str,
        user_data_dir: str,
        target_profile_url: str,
        posts: list[PostRow],
        proxy: ProxyConfig | None = None,
    ) -> tuple[str, list[tuple[int, bool]]]:
        with sync_playwright() as p:
            launch_args = {
                "user_data_dir": user_data_dir,
                "headless": self._headless,
            }
            if proxy:
                launch_args["proxy"] = {
                    "server": proxy.server,
                    "username": proxy.username,
                    "password": proxy.password,
                }

            context = p.chromium.launch_persistent_context(**launch_args)
            page = context.pages[0] if context.pages else context.new_page()
            external_ip = self.detect_external_ip(page)
            print(f"[{profile_id}] External IP: {external_ip}")

            print(f"[{profile_id}] Open cont.ws and check auth")
            if not self.check_auth(page):
                context.close()
                raise RuntimeError(
                    f"[{profile_id}] Not authorized on cont.ws. Login manually and retry."
                )

            print(f"[{profile_id}] Open target profile for repost flow")
            self.open_latest_post(page, target_profile_url)
            self.prepare_repost_action(page, profile_id)

            posted_rows: list[tuple[int, bool]] = []
            for post in posts:
                ok = self.prepare_publish_action(page, profile_id, post)
                posted_rows.append((post.source_row, ok))

            context.close()
            return external_ip, posted_rows

    def detect_external_ip(self, page: Page) -> str:
        ip_url = "https://api.ipify.org?format=text"
        page.goto(ip_url, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        ip_text = page.locator("body").inner_text().strip()
        return ip_text or "unknown"

    def check_auth(self, page: Page) -> bool:
        page.goto("https://cont.ws/", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        current_url = page.url.lower()
        has_login_button = page.locator("text=Вход, text=Login").count() > 0
        appears_logged_in = (
            "login" not in current_url and "signin" not in current_url and not has_login_button
        )
        return appears_logged_in

    def open_latest_post(self, page: Page, target_profile_url: str) -> None:
        page.goto(target_profile_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        link = page.locator("a[href*='/@']").first
        if link.count() == 0:
            raise RuntimeError("Could not detect articles on target profile page.")
        link.click()
        page.wait_for_timeout(1500)

    def prepare_repost_action(self, page: Page, profile_id: str) -> None:
        print(f"[{profile_id}] Locate repost button")
        repost_btn = page.locator("button:has-text('Репост'), a:has-text('Репост')").first
        if repost_btn.count() == 0:
            print(f"[{profile_id}] Repost button not found. Skip repost step.")
            return

        print(
            f"[{profile_id}] Repost is prepared. Confirm manually in terminal: y to click, any other key to skip."
        )
        if input("> ").strip().lower() == "y":
            repost_btn.click()
            page.wait_for_timeout(1000)
            print(f"[{profile_id}] Repost button clicked.")
        else:
            print(f"[{profile_id}] Repost skipped by user.")

    def prepare_publish_action(self, page: Page, profile_id: str, post: PostRow) -> bool:
        print(f"[{profile_id}] Open new post form")
        page.goto("https://cont.ws/new", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        title_input = page.locator("input[name='title'], input[placeholder*='Заголовок']").first
        body_input = page.locator("textarea, div[contenteditable='true']").first

        if title_input.count() == 0 or body_input.count() == 0:
            print(f"[{profile_id}] Post form fields not found. Skip row {post.source_row}.")
            return False

        title_input.fill(post.title)
        body_input.fill(post.body)
        print(f"[{profile_id}] Post data filled for row {post.source_row}.")
        print(
            f"[{profile_id}] Confirm manual publish for row {post.source_row}: y to publish click, any other key to skip."
        )
        if input("> ").strip().lower() != "y":
            print(f"[{profile_id}] Publish skipped for row {post.source_row}.")
            return False

        publish_btn = page.locator("button:has-text('Опубликовать'), button:has-text('Publish')").first
        if publish_btn.count() == 0:
            print(f"[{profile_id}] Publish button not found. Skip row {post.source_row}.")
            return False

        publish_btn.click()
        page.wait_for_timeout(1200)
        print(f"[{profile_id}] Publish clicked for row {post.source_row}.")
        return True
