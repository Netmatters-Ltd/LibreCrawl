"""JavaScript rendering handler using Playwright"""
import asyncio
import threading
import time
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from urllib.parse import urlparse


class JavaScriptRenderer:
    """Handles JavaScript rendering for dynamic content using Playwright"""

    # Playwright's wording when a page's renderer process or the whole browser
    # has died. A crashed page fails every later navigation, so it is replaced.
    CRASH_MARKERS = (
        'Page crashed',
        'Target crashed',
        'Target closed',
        'has been closed',
        'frame was detached',
    )

    def __init__(self, config):
        self.config = config
        self.playwright = None
        self._relaunching = False
        self.browser = None
        self.page_pool = []
        self.pool_lock = threading.Lock()

    async def initialize(self):
        """Initialize Playwright browser and page pool"""
        try:
            print("Starting Playwright browser...")
            self.playwright = await async_playwright().start()

            await self._launch_browser()

            # Create page pool
            max_pages = self.config.get('js_max_concurrent_pages', 3)
            for i in range(max_pages):
                self.page_pool.append(await self._new_page())

            print(f"JavaScript rendering initialized with {len(self.page_pool)} browser pages")

        except Exception as e:
            print(f"Failed to initialize JavaScript rendering: {e}")
            await self.cleanup()
            raise

    async def _launch_browser(self):
        """Launch the configured browser"""
        browser_type = self.config.get('js_browser', 'chromium').lower()
        headless = self.config.get('js_headless', True)

        if browser_type == 'firefox':
            self.browser = await self.playwright.firefox.launch(headless=headless)
        elif browser_type == 'webkit':
            self.browser = await self.playwright.webkit.launch(headless=headless)
        else:  # Default to chromium
            args = ['--no-sandbox', '--disable-dev-shm-usage'] if headless else []
            self.browser = await self.playwright.chromium.launch(headless=headless, args=args)

    async def _new_page(self):
        """A page in its own context, so one crashed page cannot take others with it"""
        context = await self.browser.new_context(
            user_agent=self.config.get('js_user_agent', 'LibreCrawl/1.0 (Web Crawler with JavaScript)'),
            viewport={
                'width': self.config.get('js_viewport_width', 1920),
                'height': self.config.get('js_viewport_height', 1080)
            }
        )
        page = await context.new_page()
        page.set_default_timeout(self.config.get('js_timeout', 30) * 1000)
        return page

    @classmethod
    def is_crash(cls, error):
        message = str(error)
        return any(marker in message for marker in cls.CRASH_MARKERS)

    async def _replace_page(self, page):
        """Swap a crashed page for a fresh one, relaunching the browser if it died too.

        Without this a single renderer crash fails every URL left in the crawl.
        Returns None when no replacement could be made, which shrinks the pool.
        """
        try:
            await page.context.close()
        except Exception:
            pass

        try:
            while self._relaunching:
                await asyncio.sleep(0.5)

            if not self.browser or not self.browser.is_connected():
                self._relaunching = True
                try:
                    print("JavaScript browser died, relaunching it")
                    try:
                        await self.browser.close()
                    except Exception:
                        pass
                    await self._launch_browser()
                finally:
                    self._relaunching = False

            return await self._new_page()
        except Exception as e:
            print(f"Could not replace a crashed JavaScript page: {e}")
            return None

    async def cleanup(self):
        """Clean up Playwright browser and resources"""
        try:
            if self.page_pool:
                for page in self.page_pool:
                    try:
                        await page.context.close()
                    except:
                        pass
                self.page_pool.clear()

            if self.browser:
                await self.browser.close()
                self.browser = None

            if self.playwright:
                await self.playwright.stop()
                self.playwright = None

            print("JavaScript rendering resources cleaned up")

        except Exception as e:
            print(f"Error during JavaScript cleanup: {e}")

    async def get_page(self):
        """Get an available page from the pool"""
        with self.pool_lock:
            if self.page_pool:
                return self.page_pool.pop()
        return None

    async def return_page(self, page):
        """Return a page to the pool"""
        with self.pool_lock:
            self.page_pool.append(page)

    @staticmethod
    def _response_ms(response, navigation_ms):
        """How long the server actually took, from the browser's own timings.

        Falls back to the measured navigation time when timing data is missing.
        """
        try:
            timing = response.request.timing if response else None
            if timing:
                for key in ('responseEnd', 'responseStart'):
                    value = timing.get(key)
                    if value and value > 0:
                        return float(value)
        except Exception:
            pass
        return navigation_ms

    async def render_page(self, url):
        """
        Render a page with JavaScript and return the HTML content

        Returns:
            tuple: (html_content, status_code, error_message, final_url, timing)
            final_url is where the page ended up after any redirects.
            timing carries response_ms (the server's own response, excluding the
            configured render wait) and render_ms (the whole render). Keeping
            them apart matters because js_wait_time is a deliberate sleep, and
            counting it as response time flags every page as slow.
        """
        page = None
        empty_timing = {'response_ms': 0.0, 'render_ms': 0.0}
        try:
            page = await self.get_page()
            if not page:
                return None, 0, "No JavaScript page available", None, empty_timing

            # Navigate to the page
            try:
                started = time.monotonic()
                response = await page.goto(
                    url,
                    wait_until='domcontentloaded',
                    timeout=self.config.get('js_timeout', 30) * 1000
                )
                navigation_ms = (time.monotonic() - started) * 1000
                response_ms = self._response_ms(response, navigation_ms)

                # Wait for JavaScript to render
                await asyncio.sleep(self.config.get('js_wait_time', 3))

                # Get the rendered HTML content
                html_content = await page.content()
                status_code = response.status if response else 200
                render_ms = (time.monotonic() - started) * 1000

                return html_content, status_code, None, page.url, {
                    'response_ms': response_ms, 'render_ms': render_ms}

            except PlaywrightTimeoutError:
                return None, 0, "JavaScript rendering timeout", None, empty_timing
            except Exception as e:
                if self.is_crash(e):
                    page = await self._replace_page(page)
                return None, 0, f"Navigation error: {str(e)}", None, empty_timing

        except Exception as e:
            return None, 0, f"JavaScript rendering error: {str(e)}", None, empty_timing

        finally:
            if page:
                await self.return_page(page)

    def should_use_javascript(self, url):
        """Determine if a URL should use JavaScript rendering"""
        parsed = urlparse(url)
        path = parsed.path.lower()

        # Skip if it's clearly a non-HTML resource
        if path.endswith(('.pdf', '.jpg', '.jpeg', '.png', '.gif', '.css', '.js', '.xml', '.txt', '.zip')):
            return False

        return True
