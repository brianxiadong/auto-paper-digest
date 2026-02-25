"""
NotebookLM browser automation module.

Uses Playwright with persistent context to automate:
- Google login (first run, headful)
- Notebook creation
- PDF upload and ingestion
- Video Overview generation
- Video download
"""

import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from .config import (
    DEFAULT_PROFILE,
    NOTEBOOKLM_URL,
    PLAYWRIGHT_NAVIGATION_TIMEOUT,
    PLAYWRIGHT_TIMEOUT,
    PLAYWRIGHT_VIDEO_TIMEOUT,
    PROFILE_DIR,
    SLIDES_DIR,
    Status,
    VIDEO_DIR,
)
from .db import get_paper, update_status, upsert_paper
from .utils import ensure_dir, get_logger, get_period_subdir, sanitize_filename

logger = get_logger()


class NotebookLMBot:
    """
    Browser automation for NotebookLM.
    
    Uses Playwright persistent context to maintain Google login session
    across runs. First run must be headful for manual login/2FA.
    """
    
    def __init__(
        self,
        headless: bool = True,
        profile_name: str = DEFAULT_PROFILE,
        slow_mo: int = 0
    ):
        """
        Initialize the NotebookLM bot.
        
        Args:
            headless: Run browser in headless mode (False for first-time login)
            profile_name: Name of the browser profile directory
            slow_mo: Slow down operations by this many ms (for debugging)
        """
        self.headless = headless
        self.profile_path = ensure_dir(PROFILE_DIR / profile_name)
        self.slow_mo = slow_mo
        
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
    
    def __enter__(self) -> "NotebookLMBot":
        """Start the browser context."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Clean up browser resources."""
        self.stop()
    
    def start(self) -> None:
        """Start the Playwright browser with persistent context."""
        logger.info(f"Starting browser (headless={self.headless})")
        
        self._playwright = sync_playwright().start()
        
        # Use persistent context for login persistence
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_path),
            headless=self.headless,
            slow_mo=self.slow_mo,
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        
        # Set default timeouts
        self._context.set_default_timeout(PLAYWRIGHT_TIMEOUT)
        self._context.set_default_navigation_timeout(PLAYWRIGHT_NAVIGATION_TIMEOUT)
        
        # Get or create main page
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = self._context.new_page()
        
        logger.debug("Browser started successfully")
    
    def stop(self) -> None:
        """Stop the browser and clean up."""
        if self._context:
            self._context.close()
            self._context = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        self._page = None
        logger.debug("Browser stopped")
    
    @property
    def page(self) -> Page:
        """Get the current page, raising if not started."""
        if not self._page:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page
    
    def take_screenshot(self, name: str) -> Path:
        """
        Take a screenshot for debugging.
        
        Args:
            name: Screenshot filename (without extension)
            
        Returns:
            Path to the saved screenshot
        """
        screenshot_dir = ensure_dir(PROFILE_DIR / "screenshots")
        path = screenshot_dir / f"{name}_{int(time.time())}.png"
        self.page.screenshot(path=str(path))
        logger.debug(f"Screenshot saved: {path}")
        return path
    
    def save_trace(self, name: str) -> None:
        """Save a trace for debugging (if tracing is enabled)."""
        try:
            trace_dir = ensure_dir(PROFILE_DIR / "traces")
            path = trace_dir / f"{name}_{int(time.time())}.zip"
            if self._context:
                self._context.tracing.stop(path=str(path))
                logger.debug(f"Trace saved: {path}")
        except Exception as e:
            logger.warning(f"Failed to save trace: {e}")
    
    def navigate_to_notebooklm(self) -> bool:
        """
        Navigate to NotebookLM homepage.
        
        Returns:
            True if successfully reached NotebookLM, False if login needed
            
        Raises:
            RuntimeError: If login is required but running in headless mode
        """
        logger.info("Navigating to NotebookLM...")
        self.page.goto(NOTEBOOKLM_URL, timeout=30000)
        
        # Wait a moment for page to load
        time.sleep(2)
        
        # Check if we landed on the app or need to login
        current_url = self.page.url
        
        if "accounts.google.com" in current_url:
            if self.headless:
                # In headless mode, we cannot complete login - fail fast with clear message
                error_msg = (
                    "Google login required but running in headless mode.\n"
                    "Please run 'apd login' first to authenticate, or use '--headful' flag."
                )
                logger.error(error_msg)
                raise RuntimeError(error_msg)
            else:
                logger.warning("Google login required. Please login manually in the browser window.")
                return False
        
        # Check for logged-in state by looking for UI elements
        try:
            # Look for "我的笔记本" or "新建" button (Chinese UI)
            my_notebooks = self.page.get_by_text("我的笔记本", exact=True)
            new_btn = self.page.get_by_text("新建", exact=False)
            
            if (my_notebooks.count() > 0 and my_notebooks.first.is_visible()) or \
               (new_btn.count() > 0 and new_btn.first.is_visible()):
                logger.info("Successfully loaded NotebookLM")
                return True
            
            # Try English UI
            my_notebooks_en = self.page.get_by_text("My notebooks", exact=True)
            if my_notebooks_en.count() > 0 and my_notebooks_en.first.is_visible():
                logger.info("Successfully loaded NotebookLM")
                return True
            
            # Fallback: Just check if we're on notebooklm.google.com
            if "notebooklm.google.com" in current_url:
                logger.info("Successfully loaded NotebookLM (URL check)")
                return True
                
            logger.warning("NotebookLM UI not detected - may need login")
            return False
            
        except Exception as e:
            logger.debug(f"Error checking login state: {e}")
            # If we're on notebooklm.google.com, assume success
            if "notebooklm.google.com" in self.page.url:
                logger.info("Successfully loaded NotebookLM (fallback)")
                return True
            return False
    
    def wait_for_login(self, timeout: int = 300) -> bool:
        """
        Wait for user to complete manual login.
        
        Args:
            timeout: Maximum seconds to wait
            
        Returns:
            True if login successful
        """
        logger.info(f"Waiting for manual login (timeout: {timeout}s)...")
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            current_url = self.page.url
            
            # Check if we're now on NotebookLM (not on login page)
            if "notebooklm.google" in current_url and "accounts.google" not in current_url:
                logger.info("Login detected, waiting for UI...")
                try:
                    # Wait for page to fully load
                    self.page.wait_for_load_state("networkidle", timeout=10000)
                    
                    # Use multiple detection strategies for robustness
                    ui_loaded = False
                    
                    # Strategy 1: Look for notebook list/create button text in any language
                    for text_pattern in ["我的笔记本", "My notebooks", "新建笔记本", "新建", 
                                        "Create", "New notebook", "精选笔记本"]:
                        try:
                            elem = self.page.get_by_text(text_pattern, exact=False).first
                            if elem.is_visible(timeout=2000):
                                ui_loaded = True
                                logger.debug(f"Found UI element with text: {text_pattern}")
                                break
                        except Exception:
                            continue
                    
                    # Strategy 2: Check for notebook cards (mat-card elements)
                    if not ui_loaded:
                        try:
                            cards = self.page.locator('mat-card').count()
                            if cards > 0:
                                ui_loaded = True
                                logger.debug(f"Found {cards} notebook card(s)")
                        except Exception:
                            pass
                    
                    # Strategy 3: Check for any notebook-related buttons
                    if not ui_loaded:
                        try:
                            buttons = self.page.locator('button').count()
                            if buttons > 5:  # Page has multiple interactive elements
                                ui_loaded = True
                                logger.debug(f"Found {buttons} buttons")
                        except Exception:
                            pass
                    
                    # Strategy 4: Check for "全部" (All) tab which is on the home page
                    if not ui_loaded:
                        try:
                            tab = self.page.get_by_text("全部", exact=True)
                            if tab.count() > 0 and tab.first.is_visible():
                                ui_loaded = True
                                logger.debug("Found '全部' tab")
                        except Exception:
                            pass
                    
                    if ui_loaded:
                        logger.info("Login successful!")
                        return True
                        
                except PlaywrightTimeout:
                    logger.debug("Timeout waiting for UI elements, continuing to poll...")
                except Exception as e:
                    logger.debug(f"Error checking login status: {e}")
            
            time.sleep(2)
        
        logger.error("Login timeout - please try again")
        return False
    
    def create_notebook(self, name: str) -> bool:
        """
        Create a new notebook.
        
        Args:
            name: Name for the notebook
            
        Returns:
            True if notebook created successfully
        """
        logger.info(f"Creating notebook: {name}")
        
        try:
            # Click "New notebook" or "+" button (English and Chinese)
            new_btn = self.page.locator(
                'button:has-text("New notebook"), '
                'button:has-text("Create"), '
                'button:has-text("新建笔记本"), '
                'button:has-text("新建"), '
                '[aria-label*="new notebook" i], '
                '[aria-label*="create" i], '
                '[aria-label*="新建" i]'
            ).first
            new_btn.click()
            
            # Wait for notebook creation dialog or new notebook to open
            time.sleep(2)
            
            logger.info("Notebook creation initiated")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create notebook: {e}")
            self.take_screenshot("create_notebook_failed")
            return False
    
    def upload_pdf(self, pdf_path: Path) -> bool:
        """
        Upload a PDF to the current notebook.
        
        Args:
            pdf_path: Path to the PDF file
            
        Returns:
            True if upload successful
        """
        logger.info(f"Uploading PDF: {pdf_path}")
        
        if not pdf_path.exists():
            logger.error(f"PDF file not found: {pdf_path}")
            return False
        
        try:
            # Wait a moment for the page to stabilize
            time.sleep(1)
            
            # Strategy 1: Direct file input (may be hidden but still works)
            # This is the most reliable method - set files on hidden input
            file_inputs = self.page.locator('input[type="file"]')
            if file_inputs.count() > 0:
                logger.debug(f"Found {file_inputs.count()} file input(s)")
                # Set files on the first file input (even if hidden)
                file_inputs.first.set_input_files(str(pdf_path))
                logger.info("PDF uploaded via file input")
            else:
                # Strategy 2: Need to trigger the file dialog first
                # Look for "选择文件" (Select file) link or upload button
                select_file_selectors = [
                    'text=选择文件',  # Chinese: Select file (link in upload dialog)
                    'a:text("选择文件")',  # Link format
                    'text=Select file',
                    'text=Choose file', 
                    'a:has-text("选择")',
                    'button:has-text("选择")',
                    '[class*="upload"] a',
                    '[class*="upload"] button',
                    'text=Upload',
                    'text=上传',
                    # New patterns for upload dialog
                    'text=上传来源',  # Upload source
                    '[class*="dropzone"] a',
                    '[role="dialog"] a:text("选择")',
                    '[role="dialog"] text=选择文件',
                ]
                
                clicked = False
                for selector in select_file_selectors:
                    try:
                        elem = self.page.locator(selector).first
                        if elem.count() > 0 and elem.is_visible():
                            # Use file chooser handler
                            with self.page.expect_file_chooser() as fc_info:
                                elem.click()
                            file_chooser = fc_info.value
                            file_chooser.set_files(str(pdf_path))
                            clicked = True
                            logger.info(f"PDF uploaded via file chooser (selector: {selector})")
                            break
                    except Exception as e:
                        logger.debug(f"Selector {selector} failed: {e}")
                        continue
                
                if not clicked:
                    # Strategy 3: Click on the upload zone area
                    upload_zone_selectors = [
                        '[class*="dropzone"]',
                        '[class*="drop-zone"]', 
                        '[class*="upload-area"]',
                        'text=拖放或',  # Chinese: Drag and drop or
                        'text=Drag',
                        # New patterns for dialog upload zone
                        'text=上传来源',  # Upload source icon/text
                        '[role="dialog"] [class*="upload"]',
                        # The upload icon area
                        '[class*="source"] [class*="upload"]',
                    ]
                    
                    for selector in upload_zone_selectors:
                        try:
                            elem = self.page.locator(selector).first
                            if elem.count() > 0 and elem.is_visible():
                                with self.page.expect_file_chooser() as fc_info:
                                    elem.click()
                                file_chooser = fc_info.value
                                file_chooser.set_files(str(pdf_path))
                                clicked = True
                                logger.info(f"PDF uploaded via drop zone click")
                                break
                        except Exception:
                            continue
                
                if not clicked:
                    logger.error("Could not find a way to upload the PDF")
                    self.take_screenshot("upload_pdf_no_input")
                    return False
            
            # Wait for upload and ingestion to complete
            logger.info("Waiting for PDF ingestion...")
            self._wait_for_ingestion()
            
            logger.info("PDF uploaded and ingested successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to upload PDF: {e}")
            self.take_screenshot("upload_pdf_failed")
            return False
    
    def _wait_for_ingestion(self, timeout: int = 120) -> bool:
        """
        Wait for PDF ingestion to complete.
        
        NotebookLM needs time to process the PDF and extract content.
        We wait for the main content area to show paper content.
        
        Args:
            timeout: Maximum seconds to wait
            
        Returns:
            True if ingestion completed
        """
        logger.debug("Starting ingestion wait...")
        start_time = time.time()
        
        # Wait for initial upload to start
        time.sleep(5)
        
        while time.time() - start_time < timeout:
            try:
                # Check for content loaded in main area
                # When PDF is processed, the main area shows paper content with questions
                
                # Method 1: Look for "对话" header with actual content below it
                # The paper summary or questions appear after processing
                try:
                    # Check for sample questions that appear after ingestion
                    # These are like "Avatar Forcing 如何通过扩散..." in Chinese
                    questions = self.page.locator('text=/如何|什么|为什么|哪些/')
                    if questions.count() >= 2:  # At least 2 questions visible
                        logger.info("PDF ingestion complete (questions visible)")
                        return True
                except Exception:
                    pass
                
                # Method 2: Check for paper title or summary in main content
                # After processing, the paper title appears in the main area
                try:
                    # Look for content with substantial text (paper summary)
                    main_content = self.page.locator('[class*="chat"], [class*="content"]')
                    if main_content.count() > 0:
                        text_content = main_content.first.text_content()
                        if text_content and len(text_content) > 200:  # Has substantial content
                            logger.info("PDF ingestion complete (content loaded)")
                            return True
                except Exception:
                    pass
                
                # Method 3: Check for the "开始输入" (Start typing) text at bottom
                # This only appears when content is ready
                try:
                    start_input = self.page.get_by_text("开始输入", exact=False)
                    if start_input.count() > 0 and start_input.first.is_visible():
                        # Also verify source count shows in main area (not just sidebar)
                        source_in_main = self.page.locator('text=/\\d+ 个来源/')
                        if source_in_main.count() > 0:
                            logger.info("PDF ingestion complete (ready for input)")
                            return True
                except Exception:
                    pass
                
            except Exception as e:
                logger.debug(f"Error during ingestion check: {e}")
            
            elapsed = int(time.time() - start_time)
            if elapsed > 0 and elapsed % 15 == 0:
                logger.debug(f"Still waiting for PDF processing... ({elapsed}s)")
            
            time.sleep(3)
        
        logger.warning("Ingestion wait timeout")
        self.take_screenshot("ingestion_timeout")
        return False
    
    def extract_summary(self) -> Optional[str]:
        """
        Extract the auto-generated summary from the chat/dialogue area.
        
        NotebookLM automatically generates a summary after PDF is uploaded.
        This summary appears as the first message in the dialogue area.
        
        Returns:
            The summary text, or None if extraction failed
        """
        logger.info("Extracting summary from NotebookLM...")
        
        try:
            # Wait a moment for content to be fully rendered
            time.sleep(2)
            
            # The summary is the first response in the dialogue area
            # It's usually in a highlighted/colored block
            
            # Method 1: Look for the summary block with yellow/highlighted background
            try:
                # The summary appears in a div with specific styling
                summary_blocks = self.page.locator('[class*="response"], [class*="message"], [class*="chat"] p')
                if summary_blocks.count() > 0:
                    # Get all text from the first substantial block
                    for i in range(min(summary_blocks.count(), 5)):
                        block = summary_blocks.nth(i)
                        text = block.text_content()
                        if text and len(text) > 100:  # Substantial content
                            summary = text.strip()
                            logger.info(f"Extracted summary ({len(summary)} chars)")
                            return summary
            except Exception as e:
                logger.debug(f"Method 1 failed: {e}")
            
            # Method 2: Look for main content area
            try:
                main_area = self.page.locator('[class*="main"] [class*="content"]')
                if main_area.count() > 0:
                    text = main_area.first.text_content()
                    if text and len(text) > 100:
                        # Clean up the text - remove suggested questions etc.
                        summary = text.strip()
                        # Limit to reasonable length
                        if len(summary) > 1000:
                            summary = summary[:1000] + "..."
                        logger.info(f"Extracted summary from main area ({len(summary)} chars)")
                        return summary
            except Exception as e:
                logger.debug(f"Method 2 failed: {e}")
            
            # Method 3: Try to get any substantial text in the dialogue section
            try:
                dialogue = self.page.locator('text=/本文|这篇|该论文|这项研究/')
                if dialogue.count() > 0:
                    parent = dialogue.first.locator('xpath=ancestor::div[contains(@class, "content") or contains(@class, "message")]')
                    if parent.count() > 0:
                        summary = parent.first.text_content()
                        if summary and len(summary) > 50:
                            logger.info(f"Extracted summary via text pattern ({len(summary)} chars)")
                            return summary.strip()
            except Exception as e:
                logger.debug(f"Method 3 failed: {e}")
            
            logger.warning("Could not extract summary")
            return None
            
        except Exception as e:
            logger.error(f"Failed to extract summary: {e}")
            self.take_screenshot("summary_extraction_failed")
            return None
    
    def navigate_to_studio(self) -> bool:
        """
        Verify Studio panel is accessible.
        
        In the new NotebookLM UI, the Studio panel is always visible on the right side.
        
        Returns:
            True if Studio panel is accessible
        """
        logger.info("Checking Studio panel availability...")
        
        try:
            # Give page a moment to render
            time.sleep(2)
            
            # Check for Studio panel elements
            # Method 1: Look for "Studio" text
            try:
                studio = self.page.get_by_text("Studio", exact=True)
                if studio.count() > 0 and studio.first.is_visible():
                    logger.info("Studio panel found")
                    return True
            except Exception:
                pass
            
            # Method 2: Look for video/audio overview cards
            try:
                video_card = self.page.get_by_text("视频概览", exact=True)
                if video_card.count() > 0 and video_card.first.is_visible():
                    logger.info("Studio panel found (视频概览 visible)")
                    return True
            except Exception:
                pass
            
            # If we got here, Studio panel was not found but we'll try to continue anyway
            logger.warning("Studio panel not explicitly found, will try to proceed")
            return True  # Return True to continue the flow
            
        except Exception as e:
            logger.error(f"Failed to check Studio panel: {e}")
            return True  # Return True to continue even on error
    
    def generate_video_overview(self, steering_prompt: Optional[str] = None) -> bool:
        """
        Generate a Video Overview.
        
        In the new NotebookLM UI, clicking the "视频概览" (Video Overview) card
        directly triggers generation.
        
        Args:
            steering_prompt: Optional prompt to guide the overview content
            
        Returns:
            True if generation started successfully
        """
        logger.info("Starting Video Overview generation...")
        
        try:
            # Wait for page to stabilize
            time.sleep(2)
            
            # Method 1: Click on the "视频概览" (Video Overview) card
            # This directly triggers generation in the new UI
            try:
                video_card = self.page.get_by_text("视频概览", exact=True)
                if video_card.count() > 0 and video_card.first.is_visible():
                    video_card.first.click()
                    time.sleep(1)
                    logger.info("Clicked on 视频概览 card")
                    
                    # Check if a dialog appeared asking for customization
                    # If so, click the generate button
                    try:
                        generate_btn = self.page.get_by_role("button", name="生成")
                        if generate_btn.count() > 0 and generate_btn.first.is_visible():
                            generate_btn.first.click()
                            logger.info("Clicked 生成 button in dialog")
                    except Exception:
                        pass  # No dialog, generation started directly
                    
                    logger.info("Video Overview generation started")
                    return True
            except Exception as e:
                logger.debug(f"Failed to click 视频概览: {e}")
            
            # Method 2: Try English text
            try:
                video_card_en = self.page.get_by_text("Video Overview", exact=True)
                if video_card_en.count() > 0 and video_card_en.first.is_visible():
                    video_card_en.first.click()
                    time.sleep(1)
                    logger.info("Video Overview generation started (English)")
                    return True
            except Exception:
                pass
            
            # Method 3: Try by aria-label
            try:
                video_elem = self.page.locator('[aria-label*="视频概览"], [aria-label*="Video Overview"]').first
                if video_elem.count() > 0 and video_elem.is_visible():
                    video_elem.click()
                    time.sleep(1)
                    logger.info("Video Overview generation started (aria-label)")
                    return True
            except Exception:
                pass
            
            logger.error("Could not find Video Overview card to click")
            self.take_screenshot("video_card_not_found")
            return False
            
        except Exception as e:
            logger.error(f"Failed to start video generation: {e}")
            self.take_screenshot("generate_failed")
            return False
    
    def generate_slides(self) -> bool:
        """
        Generate a PDF presentation (slides).
        
        In the new NotebookLM UI, clicking the "演示文稿" (Slides/Presentation) card
        directly triggers generation.
        
        Returns:
            True if generation started successfully
        """
        logger.info("Starting Slides/Presentation generation...")
        
        try:
            # Wait for page to stabilize
            time.sleep(2)
            
            # Method 1: Click on the "演示文稿" (Slides) card
            try:
                slides_card = self.page.get_by_text("演示文稿", exact=True)
                if slides_card.count() > 0 and slides_card.first.is_visible():
                    slides_card.first.click()
                    time.sleep(1)
                    logger.info("Clicked on 演示文稿 card")
                    
                    # Check if a dialog appeared asking for customization
                    try:
                        generate_btn = self.page.get_by_role("button", name="生成")
                        if generate_btn.count() > 0 and generate_btn.first.is_visible():
                            generate_btn.first.click()
                            logger.info("Clicked 生成 button in dialog")
                    except Exception:
                        pass  # No dialog, generation started directly
                    
                    logger.info("Slides generation started")
                    return True
            except Exception as e:
                logger.debug(f"Failed to click 演示文稿: {e}")
            
            # Method 2: Try English text
            try:
                slides_card_en = self.page.get_by_text("Slides", exact=True)
                if slides_card_en.count() > 0 and slides_card_en.first.is_visible():
                    slides_card_en.first.click()
                    time.sleep(1)
                    logger.info("Slides generation started (English)")
                    return True
            except Exception:
                pass
            
            # Method 3: Try "Presentation"
            try:
                pres_card = self.page.get_by_text("Presentation", exact=True)
                if pres_card.count() > 0 and pres_card.first.is_visible():
                    pres_card.first.click()
                    time.sleep(1)
                    logger.info("Presentation generation started")
                    return True
            except Exception:
                pass
            
            logger.error("Could not find Slides/演示文稿 card to click")
            self.take_screenshot("slides_card_not_found")
            return False
            
        except Exception as e:
            logger.error(f"Failed to start slides generation: {e}")
            self.take_screenshot("slides_generate_failed")
            return False
    
    def rename_notebook(self, new_name: str) -> bool:
        """
        Rename the current notebook.
        
        The notebook title is at the top-left in an input field.
        NotebookLM auto-populates it with the PDF title.
        
        Args:
            new_name: New name for the notebook
            
        Returns:
            True if renamed successfully
        """
        logger.info(f"Renaming notebook to: {new_name}")
        
        try:
            time.sleep(1)
            
            # Find the title input - it's the first/main input at the top of the page
            # The title input contains the notebook name (auto-populated from PDF)
            title_input = self.page.locator('input').first
            
            if title_input.count() > 0 and title_input.is_visible():
                # Click to focus
                title_input.click()
                time.sleep(0.3)
                
                # Select all (Cmd+A on Mac)
                self.page.keyboard.press("Meta+a")
                time.sleep(0.2)
                
                # Type new name (this replaces selected text)
                self.page.keyboard.type(new_name)
                time.sleep(0.3)
                
                # Press Enter to confirm
                self.page.keyboard.press("Enter")
                time.sleep(1)
                
                logger.info(f"Notebook renamed to: {new_name}")
                return True
            else:
                logger.warning("Title input not found")
                return False
            
        except Exception as e:
            logger.error(f"Failed to rename notebook: {e}")
            return False
    
    def wait_for_video_ready(self, timeout: int = 600) -> bool:
        """
        Wait for video generation to complete.
        
        Args:
            timeout: Maximum seconds to wait (default 10 minutes)
            
        Returns:
            True if video is ready
        """
        logger.info(f"Waiting for video generation (timeout: {timeout}s)...")
        start_time = time.time()
        
        # First, wait a bit for generation to actually start
        time.sleep(5)
        
        # Track if we've seen the "generating" state
        seen_generating = False
        
        while time.time() - start_time < timeout:
            try:
                # Check if generation is in progress
                try:
                    generating_text = self.page.get_by_text("正在生成", exact=False)
                    if generating_text.count() > 0 and generating_text.first.is_visible():
                        seen_generating = True
                        elapsed = int(time.time() - start_time)
                        if elapsed % 30 == 0 and elapsed > 0:
                            logger.debug(f"Still generating video... ({elapsed}s elapsed)")
                        time.sleep(5)
                        continue
                except Exception:
                    pass
                
                # If we've seen generating and now it's gone, generation is complete
                if seen_generating:
                    # Wait a moment for UI to update
                    time.sleep(2)
                    logger.info("Video generation complete! (generating text disappeared)")
                    return True
                
                # Alternative: Check for generated video title in the Studio output area
                # Generated videos appear in the list below the cards
                try:
                    # Look for video items that have a timestamp (indicates generated content)
                    video_items = self.page.locator('text=/\\d+ 分钟|刚刚/')
                    if video_items.count() > 0:
                        logger.info("Video generation complete! (found generated item with timestamp)")
                        return True
                except Exception:
                    pass
                
                # Check for error state
                try:
                    error_text = self.page.get_by_text("失败", exact=False)
                    if error_text.count() > 0 and error_text.first.is_visible():
                        logger.error("Video generation failed")
                        self.take_screenshot("video_generation_error")
                        return False
                except Exception:
                    pass
                    
            except Exception as e:
                logger.debug(f"Error checking video status: {e}")
            
            time.sleep(5)
        
        logger.error("Video generation timeout")
        self.take_screenshot("video_timeout")
        return False
    
    def _get_artifact_title(self) -> Optional[str]:
        """
        Get the title of the generated video/audio artifact from the Studio panel.
        
        Returns:
            The artifact title, or None if not found
        """
        try:
            # Look for artifact button content which contains the title
            artifact = self.page.locator('.artifact-button-content')
            if artifact.count() > 0:
                # The title is in a span inside the button
                title_elem = artifact.first.locator('span').first
                if title_elem.count() > 0:
                    title = title_elem.text_content()
                    if title:
                        # Clean up the title for use as filename
                        return title.strip()
            
            # Fallback: look for generated item with timestamp
            items = self.page.locator('[class*="artifact"]')
            for i in range(min(3, items.count())):
                text = items.nth(i).text_content()
                if text and ("分钟" in text or "小时" in text or "刚刚" in text):
                    # Extract title (first part before the timestamp info)
                    lines = text.strip().split('\n')
                    if lines:
                        return lines[0].strip()
        except Exception as e:
            logger.debug(f"Failed to get artifact title: {e}")
        
        return None
    
    def download_video(self, paper_id: str, video_dir: Path) -> Optional[Path]:
        """
        Download the generated video.
        
        In the new NotebookLM UI, download is accessed via the three-dot menu
        on the generated item, then clicking "下载" (Download).
        
        The video is saved with filename format: {paper_id}_{video_title}.mp4
        
        Args:
            paper_id: The paper ID (used as filename prefix)
            video_dir: Directory to save the video file
            
        Returns:
            Path to downloaded video, or None on failure
        """
        try:
            # Ensure directory exists
            ensure_dir(video_dir)
            
            # Get the artifact title for the filename
            artifact_title = self._get_artifact_title()
            if artifact_title:
                # Sanitize the title for use as filename
                safe_title = sanitize_filename(artifact_title)
                save_path = video_dir / f"{paper_id}_{safe_title}.mp4"
            else:
                # Fallback to paper_id only
                save_path = video_dir / f"{paper_id}.mp4"
            
            logger.info(f"Downloading video to: {save_path}")
            
            # First, scroll the Studio panel to reveal generated items
            try:
                studio_panel = self.page.locator('[class*="studio"], [class*="right-panel"]').first
                if studio_panel.count() > 0:
                    studio_panel.evaluate("el => el.scrollTop = el.scrollHeight")
                    time.sleep(1)
            except Exception:
                pass
            
            # Find the generated video item and click its "更多" (More) button
            # The artifact items in Studio panel have a more options button (three dots)
            # The button has class "artifact-more-button" and aria-label="更多"
            more_clicked = False
            
            # Method 1: Use the specific artifact-more-button class (most reliable)
            try:
                more_btn = self.page.locator('button.artifact-more-button[aria-label="更多"]')
                if more_btn.count() > 0 and more_btn.first.is_visible():
                    more_btn.first.click()
                    time.sleep(1)
                    more_clicked = True
                    logger.debug("Clicked artifact-more-button")
            except Exception as e:
                logger.debug(f"Method 1 (artifact-more-button) failed: {e}")
            
            # Method 2: Find all more_vert buttons and click the one in Studio panel
            if not more_clicked:
                try:
                    # Look for buttons containing the more_vert icon
                    more_buttons = self.page.locator('mat-icon:text("more_vert")')
                    if more_buttons.count() > 0:
                        # Click the last one (usually the one in Studio panel for generated items)
                        # or iterate through them
                        for i in range(more_buttons.count()):
                            btn = more_buttons.nth(i)
                            if btn.is_visible():
                                # Click the parent button element
                                parent_btn = btn.locator('xpath=ancestor::button')
                                if parent_btn.count() > 0:
                                    parent_btn.first.click()
                                else:
                                    btn.click()
                                time.sleep(1)
                                # Check if menu appeared
                                menu = self.page.locator('[role="menu"]')
                                if menu.count() > 0 and menu.first.is_visible():
                                    more_clicked = True
                                    logger.debug(f"Clicked more_vert button #{i}")
                                    break
                except Exception as e:
                    logger.debug(f"Method 2 failed: {e}")
            
            # Method 3: Direct aria-label search
            if not more_clicked:
                try:
                    more_btn = self.page.locator('[aria-label="更多"], [aria-label="More options"]').last
                    if more_btn.count() > 0 and more_btn.is_visible():
                        more_btn.click()
                        time.sleep(1)
                        more_clicked = True
                        logger.debug("Clicked more button by aria-label")
                except Exception as e:
                    logger.debug(f"Method 3 failed: {e}")
            
            if not more_clicked:
                logger.error("Could not find or click 更多 button")
                self.take_screenshot("more_button_not_found")
                return None
            
            # Click the "下载" (Download) menu item
            with self.page.expect_download(timeout=120000) as download_info:
                try:
                    time.sleep(0.5)  # Wait for menu to fully appear
                    
                    # Method 1: Look for Download menu item by role
                    download_item = self.page.get_by_role("menuitem", name="下载")
                    if download_item.count() > 0 and download_item.first.is_visible():
                        download_item.first.click()
                        logger.debug("Clicked 下载 menu item by role")
                    else:
                        # Method 2: Find by CSS class (mat-mdc-menu-item) and text
                        download_item = self.page.locator('.mat-mdc-menu-item:has-text("下载")')
                        if download_item.count() > 0 and download_item.first.is_visible():
                            download_item.first.click()
                            logger.debug("Clicked 下载 menu item by CSS class")
                        else:
                            # Method 3: Find button with role=menuitem containing span with text
                            download_item = self.page.locator('button[role="menuitem"] span:text("下载")')
                            if download_item.count() > 0:
                                # Click the parent button
                                download_item.first.locator('xpath=ancestor::button').click()
                                logger.debug("Clicked 下载 menu item via span text")
                            else:
                                # Method 4: Try English
                                download_item_en = self.page.get_by_role("menuitem", name="Download")
                                if download_item_en.count() > 0:
                                    download_item_en.first.click()
                                    logger.debug("Clicked Download menu item (English)")
                                else:
                                    # Method 5: Find by text in menu
                                    menu = self.page.locator('[role="menu"]')
                                    if menu.count() > 0:
                                        download_in_menu = menu.locator('button:has-text("下载")')
                                        if download_in_menu.count() > 0:
                                            download_in_menu.first.click()
                                            logger.debug("Clicked 下载 in menu via text search")
                                        else:
                                            logger.error("Could not find download menu item")
                                            self.take_screenshot("download_menu_not_found")
                                            return None
                                    else:
                                        logger.error("Menu not visible after clicking more button")
                                        self.take_screenshot("menu_not_visible")
                                        return None
                except Exception as e:
                    logger.error(f"Failed to click download: {e}")
                    self.take_screenshot("download_click_failed")
                    return None
            
            download = download_info.value
            download.save_as(str(save_path))
            
            logger.info(f"Video downloaded: {save_path}")
            return save_path
            
        except Exception as e:
            logger.error(f"Failed to download video: {e}")
            self.take_screenshot("download_failed")
            return None
    
    def download_slides(self, paper_id: str, slides_dir: Path) -> Optional[Path]:
        """
        Download the generated PDF slides/presentation.
        
        Similar to download_video but for PDF artifacts.
        The slides are saved with filename format: {paper_id}_slides.pdf
        
        Args:
            paper_id: The paper ID (used as filename prefix)
            slides_dir: Directory to save the PDF file
            
        Returns:
            Path to downloaded PDF, or None on failure
        """
        try:
            # Ensure directory exists
            ensure_dir(slides_dir)
            
            save_path = slides_dir / f"{paper_id}_slides.pdf"
            logger.info(f"Downloading slides to: {save_path}")
            
            # First, scroll the Studio panel to reveal generated items
            try:
                studio_panel = self.page.locator('[class*="studio"], [class*="right-panel"]').first
                if studio_panel.count() > 0:
                    studio_panel.evaluate("el => el.scrollTop = el.scrollHeight")
                    time.sleep(1)
            except Exception:
                pass
            
            # Find the slides artifact item - it should have "演示文稿" or slides icon
            # Look for artifacts that are PDF type (not video)
            more_clicked = False
            
            # We need to find the slides artifact, not the video artifact
            # Try to locate by looking for PDF-related elements
            try:
                # Look for slides/presentation artifacts by icon or text
                slides_artifacts = self.page.locator('[class*="artifact"]:has-text("演示文稿"), [class*="artifact"]:has-text("Slides")')
                if slides_artifacts.count() > 0:
                    # Find the more button within this artifact
                    artifact = slides_artifacts.first
                    more_btn = artifact.locator('button[aria-label="更多"], button.artifact-more-button')
                    if more_btn.count() > 0 and more_btn.first.is_visible():
                        more_btn.first.click()
                        time.sleep(1)
                        more_clicked = True
                        logger.debug("Clicked more button on slides artifact")
            except Exception as e:
                logger.debug(f"Could not find slides artifact directly: {e}")
            
            # If we couldn't find slides artifact specifically, try all artifacts
            # and look for PDF download option
            if not more_clicked:
                try:
                    more_buttons = self.page.locator('button.artifact-more-button[aria-label="更多"]')
                    # Try clicking each one and check for PDF download option
                    for i in range(min(more_buttons.count(), 3)):  # Check up to 3 artifacts
                        btn = more_buttons.nth(i)
                        if btn.is_visible():
                            btn.click()
                            time.sleep(0.5)
                            # Check if this menu has PDF or slides related text
                            menu = self.page.locator('[role="menu"]')
                            if menu.count() > 0:
                                menu_text = menu.first.text_content() or ""
                                # If this is not the video menu (no .mp4), could be slides
                                if "下载" in menu_text and i > 0:  # Second or later artifact
                                    more_clicked = True
                                    logger.debug(f"Found potential slides artifact at index {i}")
                                    break
                                else:
                                    # Close this menu and try next
                                    self.page.keyboard.press("Escape")
                                    time.sleep(0.3)
                except Exception as e:
                    logger.debug(f"Failed to find slides artifact: {e}")
            
            if not more_clicked:
                logger.warning("Could not find slides artifact to download")
                return None
            
            # Click the "下载" (Download) menu item
            with self.page.expect_download(timeout=120000) as download_info:
                try:
                    time.sleep(0.5)
                    download_item = self.page.get_by_role("menuitem", name="下载")
                    if download_item.count() > 0 and download_item.first.is_visible():
                        download_item.first.click()
                        logger.debug("Clicked 下载 menu item for slides")
                    else:
                        download_item = self.page.locator('.mat-mdc-menu-item:has-text("下载")')
                        if download_item.count() > 0:
                            download_item.first.click()
                        else:
                            logger.error("Could not find download menu item for slides")
                            return None
                except Exception as e:
                    logger.error(f"Failed to click download for slides: {e}")
                    return None
            
            download = download_info.value
            download.save_as(str(save_path))
            
            logger.info(f"Slides downloaded: {save_path}")
            return save_path
            
        except Exception as e:
            logger.error(f"Failed to download slides: {e}")
            self.take_screenshot("slides_download_failed")
            return None
    
    def process_paper(
        self,
        paper_id: str,
        pdf_path: Path,
        week_id: str,
        steering_prompt: Optional[str] = None,
        force: bool = False
    ) -> bool:
        """
        Full pipeline: create notebook, upload PDF, generate video, download.
        
        Args:
            paper_id: The paper ID
            pdf_path: Path to the PDF file
            week_id: Week identifier
            steering_prompt: Optional steering prompt for video
            force: Force reprocessing even if already done
            
        Returns:
            True if successful
        """
        logger.info(f"Processing paper: {paper_id}")
        
        # Check current status
        paper = get_paper(paper_id)
        if not paper:
            logger.error(f"Paper not found in database: {paper_id}")
            return False
        
        if paper.status == Status.VIDEO_OK and not force:
            logger.info(f"Paper {paper_id} already has VIDEO_OK status, skipping")
            return True
        
        try:
            # Navigate to NotebookLM
            if not self.navigate_to_notebooklm():
                if not self.wait_for_login():
                    update_status(paper_id, Status.ERROR, "Login failed")
                    return False
            
            # Create notebook
            notebook_name = sanitize_filename(f"APD_{paper_id}")
            if not self.create_notebook(notebook_name):
                update_status(paper_id, Status.ERROR, "Failed to create notebook")
                return False
            
            # Upload PDF
            if not self.upload_pdf(pdf_path):
                update_status(paper_id, Status.ERROR, "Failed to upload PDF")
                return False
            
            # Update status to NBLM_OK
            upsert_paper(
                paper_id=paper_id,
                week_id=week_id,
                notebooklm_note_name=notebook_name,
                status=Status.NBLM_OK
            )
            
            # Navigate to Studio
            if not self.navigate_to_studio():
                update_status(paper_id, Status.ERROR, "Failed to navigate to Studio")
                return False
            
            # Generate video
            if not self.generate_video_overview(steering_prompt):
                update_status(paper_id, Status.ERROR, "Failed to start video generation")
                return False
            
            # Wait for video
            if not self.wait_for_video_ready():
                update_status(paper_id, Status.ERROR, "Video generation timeout")
                return False
            
            # Download video
            video_dir = ensure_dir(VIDEO_DIR / get_period_subdir(week_id))
            
            result = self.download_video(paper_id, video_dir)
            if not result:
                update_status(paper_id, Status.ERROR, "Failed to download video")
                return False
            
            # Update status to VIDEO_OK
            upsert_paper(
                paper_id=paper_id,
                week_id=week_id,
                video_path=str(result),
                status=Status.VIDEO_OK
            )
            
            logger.info(f"Successfully processed paper: {paper_id}")
            return True
            
        except Exception as e:
            error_msg = f"Error processing paper {paper_id}: {e}"
            logger.error(error_msg)
            self.take_screenshot(f"error_{paper_id}")
            update_status(paper_id, Status.ERROR, error=error_msg, increment_retry=True)
            return False

    def delete_all_notebooks(self) -> int:
        """
        Delete all notebooks from NotebookLM.
        
        Navigates to the homepage and repeatedly deletes the first notebook
        until none remain. Each deletion follows the flow:
        1. Click three-dot menu (⋮) on the first notebook card/row
        2. Click "删除" (Delete) in the dropdown menu
        3. Click "删除" (Delete) in the confirmation dialog
        
        Works in both Grid View and List View.
        
        Returns:
            Number of notebooks deleted
        """
        logger.info("Starting deletion of all notebooks...")
        
        # Navigate to NotebookLM homepage
        if not self.navigate_to_notebooklm():
            if not self.wait_for_login():
                logger.error("Login failed, cannot delete notebooks")
                return 0
        
        time.sleep(3)  # Wait for page to load
        
        # Make sure we're on the "我的笔记本" tab
        try:
            my_notebooks_tab = self.page.get_by_text("我的笔记本", exact=True)
            if my_notebooks_tab.count() > 0 and my_notebooks_tab.first.is_visible():
                my_notebooks_tab.first.click()
                time.sleep(2)
                logger.debug("Clicked '我的笔记本' tab")
        except Exception as e:
            logger.debug(f"Could not click '我的笔记本' tab: {e}")
        
        # Take a screenshot to debug initial state
        self.take_screenshot("delete_initial_state")
        
        deleted_count = 0
        max_attempts = 200  # Safety limit to prevent infinite loop
        
        for attempt in range(max_attempts):
            # Detect notebooks using the three-dot menu button which exists in both views
            # This is the most reliable selector across Grid and List views
            more_buttons = self.page.locator('button.project-button-more, button[aria-label="项目操作菜单"]')
            button_count = more_buttons.count()
            
            if button_count == 0:
                # Also check for mat-card as fallback
                cards = self.page.locator('mat-card')
                rows = self.page.locator('tr.mat-mdc-row')
                if cards.count() == 0 and rows.count() == 0:
                    logger.info(f"No more notebooks found. Total deleted: {deleted_count}")
                    break
                else:
                    # Cards/rows exist but no more buttons visible, might need to scroll
                    logger.warning("Found cards/rows but no menu buttons, taking screenshot")
                    self.take_screenshot(f"delete_no_buttons_{attempt}")
                    break
            
            logger.info(f"Found {button_count} notebooks remaining. Deleting notebook #{deleted_count + 1}...")
            
            try:
                # Step 1: Click the three-dot menu button on the first notebook
                menu_clicked = False
                
                try:
                    first_btn = more_buttons.first
                    if first_btn.is_visible():
                        first_btn.click()
                        time.sleep(1)
                        menu_clicked = True
                        logger.debug("Clicked project-button-more")
                except Exception as e:
                    logger.debug(f"Direct button click failed: {e}")
                
                if not menu_clicked:
                    # Fallback: try clicking any more_vert icon
                    try:
                        more_icons = self.page.locator('mat-icon:text("more_vert")')
                        if more_icons.count() > 0 and more_icons.first.is_visible():
                            more_icons.first.click()
                            time.sleep(1)
                            menu_clicked = True
                            logger.debug("Clicked more_vert icon directly")
                    except Exception as e:
                        logger.debug(f"more_vert icon click failed: {e}")
                
                if not menu_clicked:
                    logger.error("Could not find or click the three-dot menu button")
                    self.take_screenshot(f"delete_menu_not_found_{attempt}")
                    break
                
                # Step 2: Click "删除" (Delete) in the dropdown menu
                delete_clicked = False
                try:
                    # The delete button in the menu has class "delete-button"
                    delete_btn = self.page.locator('button.delete-button')
                    if delete_btn.count() > 0 and delete_btn.first.is_visible():
                        delete_btn.first.click()
                        time.sleep(1)
                        delete_clicked = True
                        logger.debug("Clicked delete-button in menu")
                except Exception as e:
                    logger.debug(f"delete-button click failed: {e}")
                
                if not delete_clicked:
                    try:
                        # Fallback: find by text in the menu
                        menu = self.page.locator('[role="menu"]')
                        if menu.count() > 0:
                            del_item = menu.locator('button:has-text("删除")')
                            if del_item.count() > 0 and del_item.first.is_visible():
                                del_item.first.click()
                                time.sleep(1)
                                delete_clicked = True
                                logger.debug("Clicked delete by text in menu")
                    except Exception as e:
                        logger.debug(f"Menu text search failed: {e}")
                
                if not delete_clicked:
                    logger.error("Could not find or click delete option in menu")
                    self.take_screenshot(f"delete_option_not_found_{attempt}")
                    self.page.keyboard.press("Escape")
                    time.sleep(0.5)
                    break
                
                # Step 3: Click "删除" (Delete) in the confirmation dialog
                confirm_clicked = False
                try:
                    # The confirm button has aria-label="确认删除" or class "submit"
                    confirm_btn = self.page.locator(
                        'button[aria-label="确认删除"], '
                        'button.submit:has-text("删除"), '
                        'mat-dialog-actions button:has-text("删除")'
                    ).first
                    if confirm_btn.is_visible():
                        confirm_btn.click()
                        time.sleep(2)  # Wait for deletion to complete
                        confirm_clicked = True
                        logger.debug("Clicked confirm delete button")
                except Exception as e:
                    logger.debug(f"Confirm button click failed: {e}")
                
                if not confirm_clicked:
                    try:
                        # Fallback: find any visible "删除" button (dialog button)
                        delete_buttons = self.page.locator('button:has-text("删除"):visible')
                        if delete_buttons.count() > 0:
                            delete_buttons.last.click()
                            time.sleep(2)
                            confirm_clicked = True
                    except Exception as e:
                        logger.debug(f"Fallback confirm click failed: {e}")
                
                if not confirm_clicked:
                    logger.error("Could not confirm deletion")
                    self.take_screenshot(f"delete_confirm_failed_{attempt}")
                    self.page.keyboard.press("Escape")
                    time.sleep(0.5)
                    break
                
                deleted_count += 1
                logger.info(f"✅ Deleted notebook #{deleted_count}")
                
                # Wait for the page to update after deletion
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"Error during deletion attempt {attempt + 1}: {e}")
                self.take_screenshot(f"delete_error_{attempt}")
                break
        
        logger.info(f"Deletion complete. Total notebooks deleted: {deleted_count}")
        return deleted_count


def delete_all_notebooks_workflow(headless: bool = False) -> int:
    """
    Delete all notebooks from NotebookLM.
    
    Convenience function for CLI usage. Opens NotebookLM and deletes
    all notebooks one by one.
    
    Args:
        headless: Run browser in headless mode (default False for visibility)
        
    Returns:
        Number of notebooks deleted
    """
    with NotebookLMBot(headless=headless) as bot:
        return bot.delete_all_notebooks()


def process_papers_for_week(
    week_id: str,
    headless: bool = True,
    max_papers: Optional[int] = None,
    force: bool = False,
    steering_prompt: Optional[str] = None
) -> tuple[int, int]:
    """
    Process all papers for a week through NotebookLM.
    
    Args:
        week_id: Week identifier
        headless: Run browser in headless mode
        max_papers: Maximum papers to process
        force: Force reprocessing
        steering_prompt: Optional steering prompt for videos
        
    Returns:
        Tuple of (success_count, failure_count)
    """
    from .db import list_papers
    
    papers = list_papers(week_id=week_id, status=Status.PDF_OK)
    
    if max_papers:
        papers = papers[:max_papers]
    
    if not papers:
        logger.info(f"No papers ready for NotebookLM processing in week {week_id}")
        return 0, 0
    
    success = 0
    failure = 0
    
    with NotebookLMBot(headless=headless) as bot:
        for paper in papers:
            if not paper.pdf_path:
                logger.warning(f"Paper {paper.paper_id} has no PDF path")
                failure += 1
                continue
            
            pdf_path = Path(paper.pdf_path)
            if not pdf_path.exists():
                logger.warning(f"PDF not found for {paper.paper_id}: {pdf_path}")
                failure += 1
                continue
            
            result = bot.process_paper(
                paper_id=paper.paper_id,
                pdf_path=pdf_path,
                week_id=week_id,
                steering_prompt=steering_prompt,
                force=force
            )
            
            if result:
                success += 1
            else:
                failure += 1
    
    logger.info(f"NotebookLM processing complete for week {week_id}: {success} success, {failure} failed")
    return success, failure


def upload_papers_for_week(
    week_id: str,
    headless: bool = True,
    max_papers: Optional[int] = None,
    force: bool = False,
) -> tuple[int, int]:
    """
    Upload all PDFs for a week to NotebookLM and trigger video generation.
    
    This is Phase 1 of the two-phase workflow:
    1. Create notebook with name format: {week_id}_{paper_id}
    2. Upload PDF and wait for ingestion
    3. Trigger video generation (don't wait for completion)
    4. Move to next paper
    
    Args:
        week_id: Week identifier (e.g., "2026-02")
        headless: Run browser in headless mode
        max_papers: Maximum papers to process
        force: Force re-upload even if already done (process PDF_OK, NBLM_OK, VIDEO_OK)
        
    Returns:
        Tuple of (success_count, failure_count)
    """
    from .db import list_papers
    
    if force:
        # When force is True, get all papers regardless of status
        papers_pdf = list_papers(week_id=week_id, status=Status.PDF_OK)
        papers_nblm = list_papers(week_id=week_id, status=Status.NBLM_OK)
        papers_video = list_papers(week_id=week_id, status=Status.VIDEO_OK)
        papers = papers_pdf + papers_nblm + papers_video
    else:
        # Only process papers with PDF_OK status
        papers = list_papers(week_id=week_id, status=Status.PDF_OK)
    
    if max_papers:
        papers = papers[:max_papers]
    
    if not papers:
        logger.info(f"No papers ready for upload in week {week_id}")
        return 0, 0
    
    success = 0
    failure = 0
    
    with NotebookLMBot(headless=headless) as bot:
        for paper in papers:
            if not paper.pdf_path:
                logger.warning(f"Paper {paper.paper_id} has no PDF path")
                failure += 1
                continue
            
            pdf_path = Path(paper.pdf_path)
            if not pdf_path.exists():
                logger.warning(f"PDF not found for {paper.paper_id}: {pdf_path}")
                failure += 1
                continue
            
            try:
                logger.info(f"Processing paper: {paper.paper_id}")
                
                # Navigate to NotebookLM
                if not bot.navigate_to_notebooklm():
                    if not bot.wait_for_login():
                        update_status(paper.paper_id, Status.ERROR, "Login failed")
                        failure += 1
                        continue
                
                # Create notebook with week prefix
                notebook_name = f"{week_id}_{paper.paper_id}"
                if not bot.create_notebook(notebook_name):
                    update_status(paper.paper_id, Status.ERROR, "Failed to create notebook")
                    failure += 1
                    continue
                
                # Upload PDF
                if not bot.upload_pdf(pdf_path):
                    update_status(paper.paper_id, Status.ERROR, "Failed to upload PDF")
                    failure += 1
                    continue
                
                # Rename notebook (in case it was created with default name)
                bot.rename_notebook(notebook_name)
                
                # Extract summary from the auto-generated dialogue
                summary = bot.extract_summary()
                if summary:
                    logger.info(f"Extracted summary for {paper.paper_id}: {summary[:100]}...")
                
                # Navigate to Studio and trigger video generation
                if not bot.navigate_to_studio():
                    logger.warning(f"Could not navigate to Studio for {paper.paper_id}")
                
                if not bot.generate_video_overview():
                    logger.warning(f"Could not trigger video generation for {paper.paper_id}")
                
                # Also trigger slides/presentation generation
                if not bot.generate_slides():
                    logger.warning(f"Could not trigger slides generation for {paper.paper_id}")
                
                # Update status to UPLOADED (video and slides are generating)
                upsert_paper(
                    paper_id=paper.paper_id,
                    week_id=week_id,
                    notebooklm_note_name=notebook_name,
                    summary=summary,  # Save extracted summary
                    status=Status.NBLM_OK  # Use NBLM_OK to indicate uploaded
                )
                
                logger.info(f"Successfully uploaded and triggered video+slides for: {paper.paper_id}")
                success += 1
                
            except Exception as e:
                error_msg = f"Error uploading paper {paper.paper_id}: {e}"
                logger.error(error_msg)
                bot.take_screenshot(f"upload_error_{paper.paper_id}")
                update_status(paper.paper_id, Status.ERROR, error=error_msg, increment_retry=True)
                failure += 1
    
    logger.info(f"Upload complete for week {week_id}: {success} success, {failure} failed")
    return success, failure


def download_videos_for_week(
    week_id: str,
    headless: bool = True,
    max_papers: Optional[int] = None,
    force: bool = False,
) -> tuple[int, int, int]:
    """
    Download generated videos for a week from NotebookLM.
    
    This is Phase 2 of the two-phase workflow:
    1. Go to NotebookLM home page
    2. Find all notebooks with prefix {week_id}_
    3. For each notebook, check if video is ready
    4. If ready, download the video
    
    Implements caching: if video file already exists, skip download unless force=True.
    
    Args:
        week_id: Week identifier (e.g., "2026-02")
        headless: Run browser in headless mode
        max_papers: Maximum papers to process
        force: Force re-download even if video exists
        
    Returns:
        Tuple of (success_count, failure_count, skipped_count)
    """
    from .db import list_papers
    
    # Get papers that have been uploaded (NBLM_OK) or already have video (VIDEO_OK if force)
    papers_nblm = list_papers(week_id=week_id, status=Status.NBLM_OK)
    papers_video = list_papers(week_id=week_id, status=Status.VIDEO_OK) if force else []
    
    papers = papers_nblm + papers_video
    
    if max_papers:
        papers = papers[:max_papers]
    
    if not papers:
        logger.info(f"No papers awaiting video download in week {week_id}")
        return 0, 0, 0
    
    success = 0
    failure = 0
    skipped = 0
    
    with NotebookLMBot(headless=headless) as bot:
        for paper in papers:
            notebook_name = paper.notebooklm_note_name or f"{week_id}_{paper.paper_id}"
            
            # Check if video already exists (caching) using prefix matching
            video_dir = ensure_dir(VIDEO_DIR / get_period_subdir(week_id))
            
            # Find any existing video file with this paper_id as prefix
            existing_videos = list(video_dir.glob(f"{paper.paper_id}_*.mp4"))
            # Also check for legacy format without title
            legacy_video = video_dir / f"{paper.paper_id}.mp4"
            if legacy_video.exists():
                existing_videos.append(legacy_video)
            
            if existing_videos and not force:
                # Use the first (or only) existing video
                existing_video = existing_videos[0]
                file_size = existing_video.stat().st_size
                if file_size > 1024:  # More than 1KB (valid video)
                    logger.info(f"Video already exists for {paper.paper_id} ({existing_video.name}, {file_size / 1024 / 1024:.1f} MB), skipping")
                    skipped += 1
                    continue
                else:
                    logger.warning(f"Video file for {paper.paper_id} is too small ({file_size} bytes), will re-download")
            
            try:
                logger.info(f"Downloading video for: {paper.paper_id}")
                
                # Navigate to NotebookLM home
                if not bot.navigate_to_notebooklm():
                    if not bot.wait_for_login():
                        update_status(paper.paper_id, Status.ERROR, "Login failed")
                        failure += 1
                        continue
                
                # Find and click on the notebook by name
                # NotebookLM uses mat-card elements with a button.primary-action-button inside
                time.sleep(3)  # Wait for notebooks to load
                
                notebook_found = False
                
                try:
                    # Method 1: Find mat-card containing the notebook name and click its action button
                    card = bot.page.locator(f'mat-card:has-text("{notebook_name}")')
                    if card.count() > 0:
                        # Click the primary action button inside the card
                        action_btn = card.first.locator('button.primary-action-button')
                        if action_btn.count() > 0:
                            action_btn.first.click()
                            time.sleep(3)
                            if "notebook" in bot.page.url:
                                logger.info(f"Opened notebook: {notebook_name}")
                                notebook_found = True
                except Exception as e:
                    logger.debug(f"mat-card click failed: {e}")
                
                if not notebook_found:
                    try:
                        # Method 2: Try clicking anywhere on the mat-card
                        card = bot.page.locator(f'mat-card:has-text("{notebook_name}")')
                        if card.count() > 0:
                            card.first.click()
                            time.sleep(3)
                            if "notebook" in bot.page.url:
                                logger.info(f"Opened notebook via card click: {notebook_name}")
                                notebook_found = True
                    except Exception as e:
                        logger.debug(f"Card direct click failed: {e}")
                
                if not notebook_found:
                    try:
                        # Method 3: Fallback - find by text and force click
                        text_elem = bot.page.get_by_text(notebook_name, exact=True)
                        if text_elem.count() > 0:
                            text_elem.first.click(force=True)
                            time.sleep(3)
                            if "notebook" in bot.page.url:
                                logger.info(f"Opened notebook via text click: {notebook_name}")
                                notebook_found = True
                    except Exception as e:
                        logger.debug(f"Text click failed: {e}")
                
                if not notebook_found:
                    logger.warning(f"Notebook not found or could not click: {notebook_name}")
                    bot.take_screenshot(f"notebook_not_found_{paper.paper_id}")
                    failure += 1
                    continue
                
                # Check if video is ready
                # Videos appear in Studio panel, below the creation buttons
                # May need to scroll down the Studio panel to see them
                time.sleep(2)  # Wait for page to load
                
                video_ready = False
                
                # Try scrolling the Studio panel to reveal generated items
                try:
                    studio_panel = bot.page.locator('[class*="studio"], [class*="right-panel"]').first
                    if studio_panel.count() > 0:
                        # Scroll down to see generated items
                        studio_panel.evaluate("el => el.scrollTop = el.scrollHeight")
                        time.sleep(1)
                except Exception:
                    pass
                
                # Method 1: Look for artifact buttons (generated items have this class)
                try:
                    artifacts = bot.page.locator('.artifact-button-content, button[class*="artifact"]')
                    if artifacts.count() > 0:
                        video_ready = True
                        logger.info("Video is ready for download (artifact found)")
                except Exception:
                    pass
                
                # Method 2: Look for timestamps (分钟, 小时, 刚刚)
                if not video_ready:
                    try:
                        video_items = bot.page.locator('text=/\\d+ 分钟|\\d+ 小时|刚刚/')
                        if video_items.count() > 0:
                            video_ready = True
                            logger.info("Video is ready for download (timestamp found)")
                    except Exception:
                        pass
                
                # Method 3: Look for play button in Studio area
                if not video_ready:
                    try:
                        play_btn = bot.page.locator('[aria-label="播放"]')
                        if play_btn.count() > 0 and play_btn.first.is_visible():
                            video_ready = True
                            logger.info("Video is ready for download (play button found)")
                    except Exception:
                        pass
                
                if not video_ready:
                    # Check if still generating
                    try:
                        generating = bot.page.get_by_text("正在生成", exact=False)
                        if generating.count() > 0 and generating.first.is_visible():
                            logger.info(f"Video still generating for {paper.paper_id}, will retry later")
                            failure += 1
                            continue
                    except Exception:
                        pass
                    
                    logger.warning(f"Video not found for {paper.paper_id}")
                    bot.take_screenshot(f"video_not_found_{paper.paper_id}")
                    failure += 1
                    continue
                
                # Download video
                video_dir = ensure_dir(VIDEO_DIR / get_period_subdir(week_id))
                
                result = bot.download_video(paper.paper_id, video_dir)
                if not result:
                    logger.error(f"Failed to download video for {paper.paper_id}")
                    failure += 1
                    continue
                
                # Also try to download slides if available
                slides_dir = ensure_dir(SLIDES_DIR / get_period_subdir(week_id))
                slides_result = bot.download_slides(paper.paper_id, slides_dir)
                if slides_result:
                    logger.info(f"Successfully downloaded slides for: {paper.paper_id}")
                else:
                    logger.debug(f"No slides found for {paper.paper_id} (this is optional)")
                
                # Update status to VIDEO_OK with actual downloaded paths
                upsert_paper(
                    paper_id=paper.paper_id,
                    week_id=week_id,
                    video_path=str(result),
                    slides_path=str(slides_result) if slides_result else None,
                    status=Status.VIDEO_OK
                )
                
                logger.info(f"Successfully downloaded video for: {paper.paper_id}")
                success += 1
                
            except Exception as e:
                error_msg = f"Error downloading video for {paper.paper_id}: {e}"
                logger.error(error_msg)
                bot.take_screenshot(f"download_error_{paper.paper_id}")
                failure += 1
    
    logger.info(f"Download complete for week {week_id}: {success} success, {failure} failed, {skipped} skipped")
    return success, failure, skipped
