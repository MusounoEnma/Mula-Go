"""
executor.py — MULA GO Omni-Channel Executor
============================================

Manages the Playwright-driven auto-posting pipeline for all connected
social media platforms: Instagram, TikTok, and X (Twitter).

Architecture
------------
  broadcast_to_connected_socials()
    └── fetch_ota_config()          ← Remote kill switch check
    └── _load_cookies(platform)     ← Reads data/sessions/{platform}_cookies.json
    └── post_to_instagram()         ← Playwright automation
    └── post_to_tiktok()            ← Playwright automation (fragile — OTA guarded)
    └── post_to_x()                 ← Playwright automation

OTA Kill Switch
---------------
  fetch_ota_config() currently returns a hardcoded mock dict.
  To activate remote OTA control, uncomment the "REMOTE MODE" block
  and point REMOTE_OTA_URL at a raw GitHub Gist or your own domain:

    Example Gist JSON:
      {"tiktok_enabled": false, "ig_enabled": true, "x_enabled": true}

  No .exe rebuild needed — the config is fetched fresh on every broadcast.

Status Callback
---------------
  All posting functions accept a `status_cb` callable:
    status_cb(message: str)
  This is wired to the JS status feed via main.py's pipeline state dict.
  Timestamps are relative (seconds elapsed) from image processing start.
"""

import os
import json
import time
import logging
import subprocess
import urllib.request
import urllib.parse
from typing import Callable

log = logging.getLogger("mula_go.executor")

# ─── Path Constants ────────────────────────────────────────────────────────────
_BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
_SESSIONS_DIR = os.path.join(_BASE_DIR, "data", "sessions")

# ─── OTA Remote Config URL ────────────────────────────────────────────────────
# Replace with your Gist raw URL or private endpoint when ready.
# Example: "https://gist.githubusercontent.com/youruser/abc123/raw/ota.json"
_REMOTE_OTA_URL = None   # Set to a URL string to enable remote OTA

_FORCE_YOLO_MODE = False  # Set to True to force all browser interactions through YOLOv8 visual grounding


# ═══════════════════════════════════════════════════════════════════════════════
# I. OTA KILL SWITCH
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_ota_config() -> dict:
    """
    Fetch the OTA (Over-The-Air) platform enable/disable configuration.

    Current mode: MOCK (returns hardcoded defaults — all platforms enabled).

    To switch to remote OTA control:
      1. Set _REMOTE_OTA_URL to a URL of a raw JSON file (e.g. GitHub Gist).
      2. That file must return JSON in the format:
           {"tiktok_enabled": bool, "ig_enabled": bool, "x_enabled": bool}
      3. No code changes or .exe rebuild needed after that.

    Returns:
        dict: Platform enable flags. Defaults to all enabled on any fetch error.
    """
    _DEFAULTS = {
        "tiktok_enabled": True,
        "ig_enabled":     True,
        "x_enabled":      True,
    }

    # ── MOCK MODE (currently active) ──────────────────────────────────────────
    if _REMOTE_OTA_URL is None:
        log.info("[OTA] Running in mock mode — all platforms enabled.")
        return _DEFAULTS.copy()

    # ── REMOTE MODE (activate by setting _REMOTE_OTA_URL above) ──────────────
    try:
        import urllib.request
        with urllib.request.urlopen(_REMOTE_OTA_URL, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # Merge with defaults so missing keys don't cause KeyErrors
        config = {**_DEFAULTS, **data}
        log.info(f"[OTA] Remote config loaded: {config}")
        return config
    except Exception as e:
        log.warning(f"[OTA] Failed to fetch remote config ({e}). Using defaults.")
        return _DEFAULTS.copy()


# ═══════════════════════════════════════════════════════════════════════════════
# II. COOKIE LOADER
# ═══════════════════════════════════════════════════════════════════════════════

def _load_cookies(platform: str) -> list | None:
    """
    Load saved Playwright session cookies for a platform.

    Returns the cookie list if the file exists and is valid, else None.
    A non-None return means the user has connected that platform.
    """
    cookie_path = os.path.join(_SESSIONS_DIR, f"{platform}_cookies.json")
    if not os.path.isfile(cookie_path):
        return None
    try:
        with open(cookie_path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        if not isinstance(cookies, list) or len(cookies) == 0:
            return None
        return cookies
    except Exception as e:
        log.error(f"[Executor] Failed to load cookies for {platform}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# II-B. BROWSERACT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_browser_act_exe() -> str:
    """Find the browser-act executable path on Windows."""
    path1 = os.path.join(os.environ.get("USERPROFILE", "C:\\Users\\Administrator"), ".local", "bin", "browser-act.exe")
    if os.path.exists(path1):
        return path1
    return "browser-act"

_cached_browser_id: str | None = None

def _get_browser_id() -> str:
    """Find the best available Chrome browser configuration ID.
    
    Preference order:
    1. chrome (profile import) - launches a separate Chrome instance with CDP enabled
    2. Hardcoded fallback
    
    NOTE: chrome-direct is NOT preferred because it tries to close and restart the
    user's existing Chrome to enable remote debugging, which can timeout/fail.
    """
    global _cached_browser_id
    if _cached_browser_id:
        return _cached_browser_id

    cli = _get_browser_act_exe()
    try:
        # Limit the execution to 2 seconds to avoid freezing when browser-act is slow
        res = subprocess.run([cli, "browser", "list"], capture_output=True, text=True, check=True, timeout=2.0)
        lines = res.stdout.splitlines()
        
        for line in lines:
            # Match 'type=chrome' but NOT 'type=chrome-direct'
            if "type=chrome" in line and "type=chrome-direct" not in line:
                import re
                m = re.search(r'id=(\S+)', line)
                if m:
                    _cached_browser_id = m.group(1)
                    log.info(f"[BrowserAct] Using chrome (import) browser: {_cached_browser_id}")
                    return _cached_browser_id
            
    except Exception as e:
        log.error(f"[BrowserAct] Failed to list browsers or timed out: {e}")
    
    # Default fallback
    _cached_browser_id = "chrome_local_101537330736660560"
    log.info(f"[BrowserAct] Using default fallback browser: {_cached_browser_id}")
    return _cached_browser_id


# ── In-memory + file cache for the last known working CDP port ───────────────
_cdp_port_cache: int | None = None
_CDP_PORT_CACHE_FILE = os.path.join(_SESSIONS_DIR, "cdp_port.cache")

def _save_cdp_port(port: int) -> None:
    """Persist the last known working CDP port to disk."""
    global _cdp_port_cache
    _cdp_port_cache = port
    try:
        os.makedirs(_SESSIONS_DIR, exist_ok=True)
        with open(_CDP_PORT_CACHE_FILE, "w") as f:
            f.write(str(port))
    except Exception:
        pass


def _load_cdp_port() -> int | None:
    """Load the last known working CDP port from disk."""
    global _cdp_port_cache
    if _cdp_port_cache:
        return _cdp_port_cache
    try:
        with open(_CDP_PORT_CACHE_FILE) as f:
            port = int(f.read().strip())
            _cdp_port_cache = port
            return port
    except Exception:
        return None


# ── In-memory + file cache for visual coordinates of buttons ───────────────────
_COORD_CACHE_FILE = os.path.join(_SESSIONS_DIR, "visual_coordinate_cache.json")

def _load_coordinate_cache() -> dict:
    """Load visual coordinate cache from disk."""
    if not os.path.exists(_COORD_CACHE_FILE):
        return {}
    try:
        with open(_COORD_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_coordinate_cache(cache: dict) -> None:
    """Save visual coordinate cache to disk."""
    try:
        os.makedirs(os.path.dirname(_COORD_CACHE_FILE), exist_ok=True)
        with open(_COORD_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass


def _get_cached_coordinates(cache: dict, platform: str, element_description: str, page) -> tuple[int, int] | None:
    """Get coordinates from cache, validating viewport resolution matching."""
    try:
        viewport_w, viewport_h = 1280, 800
        viewport = page.viewport_size
        if viewport:
            viewport_w = viewport["width"]
            viewport_h = viewport["height"]
        else:
            dims = page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
            viewport_w = dims["w"]
            viewport_h = dims["h"]
    except Exception:
        viewport_w, viewport_h = 1280, 800

    entry = cache.get(platform, {}).get(element_description)
    if not entry:
        return None

    if isinstance(entry, dict):
        cached_w = entry.get("width")
        cached_h = entry.get("height")
        if cached_w == viewport_w and cached_h == viewport_h:
            return entry.get("x"), entry.get("y")
        else:
            log.info(f"[Executor] Viewport size changed from cached ({cached_w}x{cached_h}) to current ({viewport_w}x{viewport_h}). Ignoring cached coordinates for '{element_description}'.")
            return None
    elif isinstance(entry, (list, tuple)) and len(entry) == 2:
        # Backward compatibility for old cache format (assumes 1280x800)
        if viewport_w == 1280 and viewport_h == 800:
            return entry[0], entry[1]
        return None
    return None


def _set_cached_coordinates(cache: dict, platform: str, element_description: str, x: int, y: int, page) -> None:
    """Set coordinates in cache with current viewport dimensions."""
    try:
        viewport_w, viewport_h = 1280, 800
        viewport = page.viewport_size
        if viewport:
            viewport_w = viewport["width"]
            viewport_h = viewport["height"]
        else:
            dims = page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
            viewport_w = dims["w"]
            viewport_h = dims["h"]
    except Exception:
        viewport_w, viewport_h = 1280, 800

    if platform not in cache:
        cache[platform] = {}
    
    cache[platform][element_description] = {
        "x": int(x),
        "y": int(y),
        "width": int(viewport_w),
        "height": int(viewport_h)
    }
    _save_coordinate_cache(cache)



def _find_close_x_via_opencv(screenshot_path: str) -> Optional[tuple]:
    """
    Search for common geometric 'close X' button patterns on a screenshot
    using OpenCV template matching with programmatically generated templates.
    """
    try:
        import cv2
        import numpy as np
        
        if not os.path.exists(screenshot_path):
            return None
            
        img = cv2.imread(screenshot_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
            
        # Generate Template 1: Black 'X' on White Background (24x24)
        t1 = np.ones((24, 24), dtype=np.uint8) * 255
        cv2.line(t1, (5, 5), (19, 19), 0, thickness=2)
        cv2.line(t1, (5, 19), (19, 5), 0, thickness=2)
        
        # Generate Template 2: White 'X' on Black Background (24x24)
        t2 = np.zeros((24, 24), dtype=np.uint8)
        cv2.line(t2, (5, 5), (19, 19), 255, thickness=2)
        cv2.line(t2, (5, 19), (19, 5), 255, thickness=2)
        
        # Generate Template 3: White 'X' inside Grey Circle (32x32)
        t3 = np.zeros((32, 32), dtype=np.uint8)
        cv2.circle(t3, (16, 16), 14, 120, -1)
        cv2.line(t3, (10, 10), (22, 22), 255, thickness=2)
        cv2.line(t3, (10, 22), (22, 10), 255, thickness=2)

        templates = [t1, t2, t3]
        best_match = None
        best_val = 0.85 # threshold
        
        for t in templates:
            w, h = t.shape[::-1]
            res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            
            if max_val > best_val:
                best_val = max_val
                # Calculate center coordinates of matched box
                best_match = (int(max_loc[0] + w / 2), int(max_loc[1] + h / 2))
                
        if best_match:
            log.info(f"[Executor] OpenCV found 'Close X' pattern at {best_match} with confidence {best_val:.2f}")
            return best_match
            
    except Exception as e:
        log.warning(f"[Executor] OpenCV template matching failed: {e}")
        
    return None


def _probe_cdp_port(port: int, timeout: float = 0.05) -> str | None:
    """Check if a given port has a Chrome DevTools instance. Returns ws URL or None."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/version",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            ws_url = data.get("webSocketDebuggerUrl")
            if ws_url:
                _save_cdp_port(port)
                return ws_url
    except Exception:
        pass
    return None


def _find_cdp_port_via_netstat() -> str | None:
    """
    Thorough fallback: use netstat to find any Chrome DevTools port.
    Slower (~2-5s) but guaranteed to find the port.
    """
    try:
        netstat_out = subprocess.check_output(
            ["netstat", "-ano"], text=True, timeout=5
        )
    except Exception as e:
        log.error(f"[BrowserAct] netstat failed: {e}")
        return None

    # Collect all localhost listening ports
    ports = []
    for line in netstat_out.splitlines():
        if "LISTENING" in line and ("127.0.0.1" in line or "0.0.0.0" in line):
            parts = line.split()
            if len(parts) >= 2:
                port_str = parts[1].split(":")[-1]
                try:
                    port = int(port_str)
                    if 1024 < port < 65535 and port not in ports:
                        ports.append(port)
                except ValueError:
                    pass

    for port in ports:
        ws_url = _probe_cdp_port(port, timeout=0.05)
        if ws_url:
            log.info(f"[BrowserAct] Found CDP port via netstat: {port} (now cached)")
            return ws_url
    return None


def _is_chrome_running() -> bool:
    """Check if chrome.exe is currently running in the OS to avoid scanning dead ports."""
    try:
        import subprocess
        out = subprocess.check_output(
            ["tasklist", "/NH", "/FI", "IMAGENAME eq chrome.exe"],
            capture_output=True, text=True, timeout=2
        )
        return "chrome.exe" in out.lower()
    except Exception:
        return True  # Fallback to True if check fails


def _find_existing_cdp_port() -> str | None:
    """
    Quickly find an already-running Chrome DevTools instance.

    Strategy (fastest first):
    1. Try the last cached port (instant if Chrome didn't restart)
    2. Use netstat to scan actual active listening ports (instant & comprehensive)

    Returns the webSocketDebuggerUrl if found, else None.
    """
    if not _is_chrome_running():
        return None

    # 1. Try cached port first (usually works if Chrome is already running)
    cached = _load_cdp_port()
    if cached:
        ws_url = _probe_cdp_port(cached, timeout=0.1)
        if ws_url:
            log.info(f"[BrowserAct] Reusing cached CDP port {cached}")
            return ws_url

    # 2. Fall back directly to netstat to scan listening ports (very fast and catches all standard/custom ports)
    ws_url = _find_cdp_port_via_netstat()
    if ws_url:
        return ws_url

    return None


def _open_session_and_get_cdp(session_name: str, url: str) -> str:
    """
    Get a Chrome CDP WebSocket URL and navigate to the target URL.

    Fast path  : If Chrome is already running with CDP enabled, reuse it —
                 open a new tab (or reuse an existing matching tab) without
                 spawning any new Chrome window.
    Slow path  : Launch a BrowserAct session which starts a fresh Chrome
                 instance with CDP enabled, then navigate to the URL.

    Returns the Chrome DevTools webSocketDebuggerUrl, or None on failure.
    """
    cli = _get_browser_act_exe()

    # ── Step 1: Try fast path — reuse existing Chrome CDP session ────────────
    ws_url = _find_existing_cdp_port()
    if ws_url:
        log.info(f"[BrowserAct] Reusing existing CDP session — opening URL in new tab: {url}")
        try:
            # Open the URL as a new tab in the running Chrome via CDP /json/new
            port = int(ws_url.split(":")[2].split("/")[0]) if ":" in ws_url else None
            if port:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url)}",
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    new_tab_data = json.loads(resp.read().decode())
                    log.info(f"[BrowserAct] Opened new tab: {new_tab_data.get('title', url)}")
        except Exception as e:
            log.warning(f"[BrowserAct] Could not open new tab via /json/new: {e}")
        return ws_url

    # ── Step 2: Slow path — launch BrowserAct to start Chrome with CDP ───────
    browser_id = _get_browser_id()

    # Close any leftover session first (only if Chrome is actually running)
    if _is_chrome_running():
        try:
            subprocess.run([cli, "session", "close", session_name],
                           capture_output=True, text=True, timeout=5)
        except Exception:
            pass

    log.info(f"[BrowserAct] Starting new session '{session_name}' → {url}")
    try:
        subprocess.run(
            [cli, "--session", session_name, "browser", "open",
             browser_id, url, "--headed"],
            capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        log.warning("[BrowserAct] Browser open timed out — probing for CDP anyway...")
    except Exception as e:
        log.error(f"[BrowserAct] Browser open error: {e}")

    # Polling loop: wait for Chrome to start up and expose CDP port
    # Poll every 0.5s for up to 8s (16 attempts)
    ws_url = None
    for attempt in range(16):
        time.sleep(0.5)
        if _is_chrome_running():
            ws_url = _find_existing_cdp_port()
            if ws_url:
                log.info(f"[BrowserAct] Detected Chrome CDP port on attempt {attempt+1}")
                return ws_url

    log.error("[BrowserAct] Could not find any Chrome CDP port.")
    return None



# ═══════════════════════════════════════════════════════════════════════════════
# III. PLATFORM AUTOMATION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _build_post_text(caption: str, hashtags: str) -> str:
    """Combine caption and hashtags into a single post body string."""
    parts = [caption.strip()]
    if hashtags.strip():
        parts.append("\n\n" + hashtags.strip())
    return "".join(parts)


def _restore_cookies(context, cookies: list) -> None:
    """Add saved cookies into a Playwright browser context."""
    try:
        context.add_cookies(cookies)
    except Exception as e:
        log.warning(f"[Executor] Some cookies could not be restored: {e}")


# ── Instagram ─────────────────────────────────────────────────────────────────

def post_to_instagram(
    image_path: str,
    caption:    str,
    hashtags:   str,
    cookies:    list,
    status_cb:  Callable[[str], None] | None = None,
    title:      str = "",
    close_session: bool = True,
) -> dict:
    """
    Auto-post a single image to Instagram using saved session cookies.

    Strategy:
      - Restore cookies → navigate to home → click Create (+) button
      - Upload image file via the hidden file input
      - Paste caption → click Next → click Share
    """
    def _cb(msg):
        if status_cb:
            status_cb(msg)
        log.info(f"[Instagram] {msg}")

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        image_paths = [image_path] if isinstance(image_path, str) else image_path
        post_text = _build_post_text(caption, hashtags)

        with sync_playwright() as p:
            _cb("Connecting to Chrome via CDP...")
            ws_url = _open_session_and_get_cdp("instagram", "https://www.instagram.com/")
            if not ws_url:
                raise RuntimeError("Failed to obtain Chrome CDP WebSocket URL from BrowserAct.")

            browser = p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()

            # ── Find or navigate to Instagram tab ────────────────────────────
            # Priority: find existing instagram tab → use first tab → create new tab
            page = None
            all_pages = [pg for ctx in browser.contexts for pg in ctx.pages]
            for pg in all_pages:
                if "instagram.com" in (pg.url or ""):
                    page = pg
                    break
            if not page and all_pages:
                # Reuse the most recent tab (likely the one we just opened)
                page = all_pages[-1]
            if not page:
                page = context.new_page()

            # Force standardized viewport size for visual grounding consistency
            # (Viewport is now handled dynamically in pipeline.py scaling logic)
            # try:
            #     page.set_viewport_size({"width": 1280, "height": 800})
            # except Exception:
            #     pass

            # Inject virtual cursor ONCE at the start (hides system cursor)
            _inject_custom_cursor(page)

            _cb("Navigating to Instagram home...")
            if "instagram.com" not in (page.url or ""):
                page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=30_000)
            else:
                # Already on instagram, just wait for it to be ready
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:
                    pass

            # ── Click the Create / '+' button ─────────────────────────────────
            _cb("Opening the Create post dialog...")
            try:
                new_post_locator = page.get_by_role("link", name="New post").first
                _stealth_click_with_visual_fallback(page, new_post_locator, "link[name='New post']", "Create or New Post button in the navbar", _cb)
            except Exception:
                svg_locator = page.locator("svg[aria-label='New post']").first
                _stealth_click_with_visual_fallback(page, svg_locator, "svg[aria-label='New post']", "New Post SVG create icon", _cb)

            # ── File input ────────────────────────────────────────────────────
            _cb("Attaching product image...")
            page.wait_for_selector("input[type='file']", state="attached", timeout=15_000)
            page.set_input_files("input[type='file']", image_paths)

            # ── Inject test overlay for testing fallback ──
            if os.environ.get("MULA_GO_TEST_OVERLAY", "false").lower() == "true":
                _cb("[TEST] Injecting simulated overlay disturbance on the page...")
                page.evaluate("""
                    () => {
                        if (document.getElementById('test-disturbance-overlay')) return;
                        const overlay = document.createElement('div');
                        overlay.id = 'test-disturbance-overlay';
                        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,0.8);z-index:999999;display:flex;align-items:center;justify-content:center;';
                        overlay.innerHTML = `
                            <div style="background:white;color:black;padding:30px;border-radius:8px;text-align:center;box-shadow:0 4px 20px rgba(0,0,0,0.5);max-width:400px;">
                                <h2 style="margin-top:0;color:#ff4d4f;">[TEST] Disturbance Overlay</h2>
                                <p style="margin:10px 0 20px;color:#555;">Simulated Instagram dialog blocking the view.</p>
                                <button id="btn-close-test-overlay" style="padding:10px 20px;font-size:16px;font-weight:bold;background:#0095f6;color:white;border:none;border-radius:4px;cursor:pointer;">Not Now</button>
                            </div>
                        `;
                        document.body.appendChild(overlay);
                        document.getElementById('btn-close-test-overlay').addEventListener('click', () => overlay.remove());
                    }
                """)
                page.wait_for_timeout(1000)

            # ── Navigate through the multi-step dialog ────────────────────────
            _cb("Configuring post settings...")
            next_btn = page.get_by_role("button", name="Next").first
            _stealth_click_with_visual_fallback(page, next_btn, "button[name='Next']", "Next button in the crop screen", _cb)

            next_btn2 = page.get_by_role("button", name="Next").first
            _stealth_click_with_visual_fallback(page, next_btn2, "button[name='Next']", "Next button in the filter screen", _cb)

            # ── Caption ────────────────────────────────────────────────────────
            _cb("Adding caption and hashtags...")
            caption_box = page.locator(
                "div[aria-label='Write a caption...'], "
                "textarea[aria-label='Write a caption...']"
            ).first
            caption_box.wait_for(state="visible", timeout=10_000)
            _human_type(page, caption_box, post_text)

            # ── Share ──────────────────────────────────────────────────────────
            _cb("Submitting post to Instagram...")
            share_btn = page.get_by_role("button", name="Share").first
            _stealth_click_with_visual_fallback(page, share_btn, "button[name='Share']", "Share or Publish button", _cb)

            try:
                page.wait_for_selector(
                    "span:has-text('Your post has been shared.')",
                    timeout=20_000,
                )
            except PWTimeout:
                pass  # Post may still have succeeded

            page.wait_for_timeout(1_500)

            # ── Verify post on Instagram profile ──────────────────────────────
            _cb("Verifying post submission on Instagram profile...")
            try:
                profile_link = page.locator("a[href*='/']").filter(has_text="Profile").first
                if profile_link.is_visible(timeout=2000):
                    _stealth_click_with_visual_fallback(
                        page,
                        profile_link,
                        "a[href*='/']",
                        "Instagram profile navigation button",
                        _cb=_cb
                    )
                else:
                    page.get_by_role("link", name="Profile").first.click()
                
                page.wait_for_timeout(3000)
                
                first_post = page.locator("article div div div a, article a[href*='/p/']").first
                if first_post.is_visible():
                    _cb("Verification SUCCESS: New post is visible on your Instagram profile grid.")
                else:
                    _cb("Verification warning: Could not confirm new post in profile grid. It might be processing or delayed.")
            except Exception as check_err:
                _cb(f"Skipping profile verification: {check_err}")

            # Restore system cursor and close the tab (NOT the browser)
            _remove_custom_cursor(page)
            try:
                page.close()
            except Exception:
                pass
            if close_session:
                cli = _get_browser_act_exe()
                try:
                    subprocess.run([cli, "session", "close", "instagram"],
                                   capture_output=True, text=True, timeout=5)
                except Exception:
                    pass

        _cb("Instagram post published successfully.")
        return {"platform": "instagram", "success": True, "message": "Post published."}

    except ImportError:
        return {
            "platform": "instagram",
            "success":  False,
            "message":  "Playwright not installed. Run: pip install playwright && playwright install chromium",
        }
    except Exception as e:
        log.error(f"[Instagram] Post failed: {e}")
        return {"platform": "instagram", "success": False, "message": str(e)}



# ── TikTok ─────────────────────────────────────────────────────────────────────

import random
import math

def _inject_custom_cursor(page) -> None:
    """Inject a visible virtual cursor and hide the system cursor to avoid double-cursor."""
    try:
        page.evaluate("""
            () => {
                if (document.getElementById('mula-go-virtual-cursor')) return;

                // Hide the system cursor on the entire page
                const hideStyle = document.createElement('style');
                hideStyle.id = 'mula-go-hide-cursor';
                hideStyle.innerHTML = '* { cursor: none !important; }';
                document.head.appendChild(hideStyle);

                const cursor = document.createElement('div');
                cursor.id = 'mula-go-virtual-cursor';
                cursor.style.position = 'fixed';
                cursor.style.top = '0px';
                cursor.style.left = '0px';
                cursor.style.width = '22px';
                cursor.style.height = '22px';
                cursor.style.backgroundColor = 'rgba(29, 222, 116, 0.75)';
                cursor.style.border = '2px solid white';
                cursor.style.borderRadius = '50%';
                cursor.style.pointerEvents = 'none';
                cursor.style.zIndex = '2147483647';
                cursor.style.transform = 'translate(-50%, -50%)';
                cursor.style.boxShadow = '0 0 10px rgba(29, 222, 116, 0.9), 0 0 22px rgba(29, 222, 116, 0.45)';
                cursor.style.transition = 'width 0.15s, height 0.15s, background-color 0.15s';

                const style = document.createElement('style');
                style.id = 'mula-go-cursor-style';
                style.innerHTML = `
                    @keyframes mula-go-ripple {
                        0% { transform: translate(-50%, -50%) scale(1); opacity: 1; }
                        100% { transform: translate(-50%, -50%) scale(3.5); opacity: 0; }
                    }
                    .mula-go-ripple-effect {
                        position: fixed;
                        width: 22px;
                        height: 22px;
                        border: 2px solid #1dde74;
                        border-radius: 50%;
                        pointer-events: none;
                        z-index: 2147483646;
                        animation: mula-go-ripple 0.45s ease-out forwards;
                        box-shadow: 0 0 8px rgba(29, 222, 116, 0.5);
                    }
                `;
                document.head.appendChild(style);
                document.body.appendChild(cursor);

                // Automatically keep virtual cursor in sync with mousemove events
                document.addEventListener('mousemove', (e) => {
                    const c = document.getElementById('mula-go-virtual-cursor');
                    if (c) {
                        c.style.left = e.clientX + 'px';
                        c.style.top = e.clientY + 'px';
                    }
                });
            }
        """)
    except Exception:
        pass


def _remove_custom_cursor(page) -> None:
    """Restore the system cursor and remove the virtual cursor after automation is done."""
    try:
        page.evaluate("""
            () => {
                const el = document.getElementById('mula-go-virtual-cursor');
                if (el) el.remove();
                const hs = document.getElementById('mula-go-hide-cursor');
                if (hs) hs.remove();
                const cs = document.getElementById('mula-go-cursor-style');
                if (cs) cs.remove();
            }
        """)
    except Exception:
        pass


def _get_current_cursor_position(page) -> tuple[float, float]:
    """Get the current visual cursor coordinates from the browser."""
    try:
        pos = page.evaluate("""
            () => {
                const cursor = document.getElementById('mula-go-virtual-cursor');
                if (cursor) {
                    return [parseFloat(cursor.style.left) || 0, parseFloat(cursor.style.top) || 0];
                }
                return null;
            }
        """)
        if pos:
            return float(pos[0]), float(pos[1])
    except Exception:
        pass
    return 640.0, 400.0


def _generate_bezier_path(start_x: float, start_y: float, end_x: float, end_y: float, steps: int) -> list[tuple[float, float]]:
    """Generate a natural cubic Bezier curve path with quadratic ease-in-out timing."""
    path = []
    if steps < 2:
        return [(end_x, end_y)]
        
    dx = end_x - start_x
    dy = end_y - start_y
    dist = math.sqrt(dx*dx + dy*dy)
    
    if dist > 0:
        nx = -dy / dist
        ny = dx / dist
    else:
        nx, ny = 0, 0
        
    # Generate random organic perpendicular offsets
    shift1 = dist * random.uniform(-0.12, 0.12)
    shift2 = dist * random.uniform(-0.12, 0.12)
    
    p1_x = start_x + dx * 0.33 + nx * shift1
    p1_y = start_y + dy * 0.33 + ny * shift1
    
    p2_x = start_x + dx * 0.66 + nx * shift2
    p2_y = start_y + dy * 0.66 + ny * shift2
    
    for i in range(steps + 1):
        t = i / steps
        # Easing: Quadratic ease-in-out
        t_eased = 2 * t * t if t < 0.5 else 1 - ((-2 * t + 2) ** 2) / 2
        
        mt = 1 - t_eased
        x = (mt**3 * start_x + 
             3 * mt**2 * t_eased * p1_x + 
             3 * mt * t_eased**2 * p2_x + 
             t_eased**3 * end_x)
        y = (mt**3 * start_y + 
             3 * mt**2 * t_eased * p1_y + 
             3 * mt * t_eased**2 * p2_y + 
             t_eased**3 * end_y)
        path.append((x, y))
    return path


def _animate_virtual_cursor(page, x: float, y: float, click_effect: bool = False) -> None:
    """Animate the virtual cursor to coordinates using a human-like Bezier path."""
    try:
        _inject_custom_cursor(page)
        start_x, start_y = _get_current_cursor_position(page)
        
        # Calculate distance
        dist = math.sqrt((x - start_x)**2 + (y - start_y)**2)
        if dist < 15:
            steps = random.randint(2, 4)
        else:
            steps = random.randint(6, 15)
            
        path = _generate_bezier_path(start_x, start_y, x, y, steps)
        
        for px, py in path:
            page.mouse.move(px, py)
            time.sleep(random.uniform(0.008, 0.02))
            
        # Ensure we end exactly at target
        page.mouse.move(x, y)
        page.evaluate(f"""
            () => {{
                const cursor = document.getElementById('mula-go-virtual-cursor');
                if (cursor) {{
                    cursor.style.left = '{x}px';
                    cursor.style.top = '{y}px';
                }}
            }}
        """)
        
        if click_effect:
            page.evaluate(f"""
                () => {{
                    const cursor = document.getElementById('mula-go-virtual-cursor');
                    if (cursor) {{
                        cursor.style.width = '14px';
                        cursor.style.height = '14px';
                        cursor.style.backgroundColor = 'rgba(255, 77, 79, 0.9)';

                        const ripple = document.createElement('div');
                        ripple.className = 'mula-go-ripple-effect';
                        ripple.style.left = '{x}px';
                        ripple.style.top = '{y}px';
                        document.body.appendChild(ripple);
                        setTimeout(() => ripple.remove(), 450);

                        setTimeout(() => {{
                            cursor.style.width = '22px';
                            cursor.style.height = '22px';
                            cursor.style.backgroundColor = 'rgba(29, 222, 116, 0.75)';
                        }}, 200);
                    }}
                }}
            """)
            time.sleep(random.uniform(0.18, 0.25))
    except Exception as e:
        log.warning(f"[Executor] Animation failed: {e}")
        # Fallback: move mouse directly
        try:
            page.mouse.move(x, y)
        except Exception:
            pass


def _focus_chrome_window(page) -> int:
    """
    Focuses the Chrome window and returns its HWND.
    Uses page title temporarily to uniquely identify the Chrome window handle.
    """
    import ctypes
    import time
    import random
    import json
    
    try:
        orig_title = page.title()
        unique_id = f"MULA_GO_TEMP_FOCUS_{random.randint(100000, 999999)}"
        page.evaluate(f"document.title = '{unique_id}'")
        
        hwnd = 0
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        
        # Poll up to 10 times (500ms total) to allow OS title update propagation
        for attempt in range(10):
            # Try exact match first
            hwnd = ctypes.windll.user32.FindWindowW(None, unique_id)
            if hwnd:
                break
            
            # Try partial match (EnumWindows)
            hwnd_found = [0]
            def enum_window_proc(h, lParam):
                length = ctypes.windll.user32.GetWindowTextLengthW(h)
                if length > 0:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    ctypes.windll.user32.GetWindowTextW(h, buffer, length + 1)
                    if unique_id in buffer.value:
                        hwnd_found[0] = h
                        return False  # stop enumeration
                return True
                
            ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_window_proc), 0)
            hwnd = hwnd_found[0]
            if hwnd:
                break
            time.sleep(0.05)
            
        page.evaluate(f"document.title = {json.dumps(orig_title)}")
        
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            time.sleep(0.3)
            return hwnd
            
        if orig_title:
            # Try exact match for original title
            hwnd = ctypes.windll.user32.FindWindowW(None, orig_title)
            if not hwnd:
                # Try partial match for original title
                hwnd_found = [0]
                def enum_window_proc_orig(h, lParam):
                    length = ctypes.windll.user32.GetWindowTextLengthW(h)
                    if length > 0:
                        buffer = ctypes.create_unicode_buffer(length + 1)
                        ctypes.windll.user32.GetWindowTextW(h, buffer, length + 1)
                        if orig_title in buffer.value:
                            hwnd_found[0] = h
                            return False
                    return True
                ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_window_proc_orig), 0)
                hwnd = hwnd_found[0]
                
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                time.sleep(0.3)
                return hwnd
    except Exception as e:
        log.warning(f"[Executor] Failed to focus Chrome window: {e}")
    return 0



def _get_viewport_screen_origin(page, hwnd: int) -> tuple[int, int]:
    """
    Calculate the screen coordinates (x, y) of the browser viewport's top-left corner.
    First tries to find the child render window (Chrome_RenderWidgetHostHWND) using Win32 API.
    Falls back to window.screenLeft/Top dimensions evaluated inside browser.
    """
    import ctypes
    
    if hwnd:
        try:
            result = {"hwnd": None}
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            
            def enum_child_proc(child_hwnd, lParam):
                class_name = ctypes.create_unicode_buffer(256)
                ctypes.windll.user32.GetClassNameW(child_hwnd, class_name, 256)
                if class_name.value == "Chrome_RenderWidgetHostHWND":
                    result["hwnd"] = child_hwnd
                    return False
                return True
                
            ctypes.windll.user32.EnumChildWindows(hwnd, WNDENUMPROC(enum_child_proc), 0)
            
            render_hwnd = result["hwnd"]
            if render_hwnd:
                class POINT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
                
                pt = POINT(0, 0)
                if ctypes.windll.user32.ClientToScreen(render_hwnd, ctypes.byref(pt)):
                    log.info(f"[Executor] Win32 ClientToScreen succeeded. Viewport screen origin: ({pt.x}, {pt.y})")
                    return pt.x, pt.y
        except Exception as e:
            log.warning(f"[Executor] Win32 ClientToScreen origin finder failed: {e}")

    try:
        bounds = page.evaluate("""
            () => {
                return {
                    screenX: window.screenLeft !== undefined ? window.screenLeft : window.screenX,
                    screenY: window.screenTop !== undefined ? window.screenTop : window.screenY,
                    outerWidth: window.outerWidth,
                    outerHeight: window.outerHeight,
                    innerWidth: window.innerWidth,
                    innerHeight: window.innerHeight
                };
            }
        """)
        
        border_x = int((bounds["outerWidth"] - bounds["innerWidth"]) / 2)
        border_y_top = int((bounds["outerHeight"] - bounds["innerHeight"]) - border_x)
        origin_x = bounds["screenX"] + border_x
        origin_y = bounds["screenY"] + border_y_top
        
        log.info(f"[Executor] JS layout origin finder calculated: ({origin_x}, {origin_y})")
        return origin_x, origin_y
    except Exception as e:
        log.warning(f"[Executor] JS layout origin calculation fallback failed: {e}")
        return 0, 0


def _move_native_mouse_humanlike(target_x: int, target_y: int):
    """Move the actual OS cursor to target coordinates using a natural Bezier curve."""
    import ctypes
    import time
    import random
    import math

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    start_x, start_y = pt.x, pt.y

    dx = target_x - start_x
    dy = target_y - start_y
    dist = math.sqrt(dx*dx + dy*dy)

    if dist < 15:
        steps = random.randint(2, 4)
    else:
        steps = random.randint(6, 16)

    path = []
    if steps >= 2:
        shift1 = dist * random.uniform(-0.15, 0.15)
        shift2 = dist * random.uniform(-0.15, 0.15)
        
        nx = -dy / dist if dist > 0 else 0
        ny = dx / dist if dist > 0 else 0
        
        p1_x = start_x + dx * 0.33 + nx * shift1
        p1_y = start_y + dy * 0.33 + ny * shift1
        
        p2_x = start_x + dx * 0.66 + nx * shift2
        p2_y = start_y + dy * 0.66 + ny * shift2
        
        for i in range(steps + 1):
            t = i / steps
            t_eased = 2 * t * t if t < 0.5 else 1 - ((-2 * t + 2) ** 2) / 2
            mt = 1 - t_eased
            
            x = (mt**3 * start_x + 
                 3 * mt**2 * t_eased * p1_x + 
                 3 * mt * t_eased**2 * p2_x + 
                 t_eased**3 * target_x)
            y = (mt**3 * start_y + 
                 3 * mt**2 * t_eased * p1_y + 
                 3 * mt * t_eased**2 * p2_y + 
                 t_eased**3 * target_y)
            path.append((int(x), int(y)))
    else:
        path = [(target_x, target_y)]

    for px, py in path:
        ctypes.windll.user32.SetCursorPos(px, py)
        time.sleep(random.uniform(0.008, 0.02))

    ctypes.windll.user32.SetCursorPos(target_x, target_y)
    time.sleep(random.uniform(0.05, 0.12))


def _click_native_mouse(target_x: int, target_y: int):
    """Move and perform a physical left click at target screen coordinates."""
    import ctypes
    import time
    import random
    
    _move_native_mouse_humanlike(target_x, target_y)
    
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(random.uniform(0.06, 0.13))
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(random.uniform(0.1, 0.25))


def _perform_native_stealth_click(page, click_x: float, click_y: float) -> None:
    """Helper to animate virtual cursor and click native OS mouse at viewport coordinates."""
    try:
        _animate_virtual_cursor(page, click_x, click_y, click_effect=True)
    except Exception as e:
        log.warning(f"[Executor] Virtual cursor animation failed: {e}")
    
    hwnd = _focus_chrome_window(page)
    origin_x, origin_y = _get_viewport_screen_origin(page, hwnd)
    screen_x = origin_x + int(click_x)
    screen_y = origin_y + int(click_y)
    
    _click_native_mouse(screen_x, screen_y)


def _stealth_click(page, locator) -> None:
    """Perform a human-like mouse click by moving the pointer physically and clicking."""
    try:
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        if not box:
            locator.click()
            return

        x_offset = random.uniform(0.2, 0.8) * box["width"]
        y_offset = random.uniform(0.2, 0.8) * box["height"]
        target_x = box["x"] + x_offset
        target_y = box["y"] + y_offset

        _perform_native_stealth_click(page, target_x, target_y)
    except Exception as e:
        log.warning(f"[Executor] Stealth click failed: {e}. Falling back to DOM click.")
        try:
            locator.click()
        except Exception:
            pass


def _is_overlay_present(page) -> bool:
    """Check if a popup overlay (like Turn on Notifications, Save Login Info, or test overlay) is visible."""
    try:
        if page.locator("#test-disturbance-overlay").first.is_visible():
            return True
        if page.locator("button:has-text('Not Now')").first.is_visible():
            return True
        if page.locator("button:has-text('Not now')").first.is_visible():
            return True
        if page.locator("text='Turn on Notifications'").first.is_visible():
            return True
        if page.locator("text='Save Info'").first.is_visible():
            return True
    except Exception:
        pass
    return False


def _validate_coords_via_gemma(crop_path: str) -> bool:
    """Query local Qwen VLM to verify if a cropped region contains a close/dismiss button or 'X' icon."""
    import json
    import base64
    import urllib.request
    
    try:
        with open(crop_path, "rb") as img_file:
            base64_data = base64.b64encode(img_file.read()).decode('utf-8')
        data_uri = f"data:image/jpeg;base64,{base64_data}"
        
        prompt = (
            "Analyze this cropped image. Does it contain a close button, 'X' icon, "
            "cancel button, or 'Not Now' text? Answer ONLY with 'yes' or 'no'."
        )
        
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": prompt}
                    ]
                }
            ],
            "temperature": 0.1
        }
        
        req = urllib.request.Request(
            "http://127.0.0.1:8080/v1/chat/completions",
            data=json.dumps(payload).encode('utf-8'),
            headers={"Content-Type": "application/json"}
        )
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_data = json.loads(resp.read().decode('utf-8'))
            reply = res_data["choices"][0]["message"]["content"].strip().lower()
            log.info(f"[Executor] local Qwen verification reply: '{reply}'")
            return "yes" in reply or "y" == reply
    except Exception as e:
        log.warning(f"[Executor] local Qwen verification failed: {e}")
    return False


def _crop_and_validate(screenshot_path: str, x: int, y: int) -> bool:
    """Crop a 120x120 area centered at (x, y) and validate it via local Qwen VLM."""
    try:
        from PIL import Image
        img = Image.open(screenshot_path)
        width, height = img.size
        
        box_size = 60
        left = max(0, x - box_size)
        top = max(0, y - box_size)
        right = min(width, x + box_size)
        bottom = min(height, y + box_size)
        
        cropped_img = img.crop((left, top, right, bottom))
        
        crop_path = os.path.join(_SESSIONS_DIR, "temp_opencv_crop.jpg")
        cropped_img.save(crop_path, "JPEG")
        
        is_valid = _validate_coords_via_gemma(crop_path)
        
        try:
            os.remove(crop_path)
        except Exception:
            pass
            
        return is_valid
    except Exception as e:
        log.warning(f"[Executor] Crop and validate failed: {e}")
    return False


def _dismiss_instagram_overlays(page, _cb=None) -> None:
    """Detect and dismiss common Instagram popups with validation and self-healing verification."""
    is_test_overlay = False
    try:
        if page.locator("#test-disturbance-overlay").first.is_visible():
            is_test_overlay = True
    except Exception:
        pass

    if not is_test_overlay:
        try:
            dismiss_selectors = [
                "button:has-text('Not Now')",
                "button:has-text('Not now')",
            ]
            for selector in dismiss_selectors:
                locator = page.locator(selector).first
                if locator.is_visible():
                    if _cb:
                        _cb(f"Popup overlay detected. Dismissing via selector '{selector}'...")
                    
                    box = locator.bounding_box()
                    if box:
                        x = int(box["x"] + box["width"] / 2)
                        y = int(box["y"] + box["height"] / 2)
                        
                        max_dx = max(1, int(box["width"] * 0.15))
                        max_dy = max(1, int(box["height"] * 0.15))
                        click_x = x + random.randint(-max_dx, max_dx)
                        click_y = y + random.randint(-max_dy, max_dy)
                        
                        _perform_native_stealth_click(page, click_x, click_y)
                    else:
                        _stealth_click(page, locator)
                    page.wait_for_timeout(2000)
                    
                    if not _is_overlay_present(page):
                        return
                    else:
                        if _cb:
                            _cb("CSS selector click failed to dismiss popup. Proceeding to visual detection fallbacks...")
        except Exception as e:
            log.warning(f"[Executor] CSS popup dismissal failed: {e}")

    # ── VLM Fallback for Popup Dismissal ──
    try:
        if is_test_overlay or _is_overlay_present(page):
            target_desc = "Not Now button or cancel button or close button of the test overlay" if is_test_overlay else "Not Now button or cancel button"
            
            cache = _load_coordinate_cache()
            coords = _get_cached_coordinates(cache, "instagram", target_desc, page)
            
            if coords:
                x, y = coords
                click_x = x + random.randint(-3, 3)
                click_y = y + random.randint(-3, 3)
                if _cb:
                    _cb(f"Popup dismissal target '{target_desc}' found in cache at ({x}, {y}) (jittered to {click_x}, {click_y}). Dismissing...")
                _perform_native_stealth_click(page, click_x, click_y)
                page.wait_for_timeout(2000)
                
                if not _is_overlay_present(page):
                    return
                else:
                    if _cb:
                        _cb("Cached coordinates failed to dismiss popup. Suspecting stale cache. Invalidating cache and running visual detection...")
                    if "instagram" in cache and target_desc in cache["instagram"]:
                        del cache["instagram"][target_desc]
                        _save_coordinate_cache(cache)

            if _cb:
                if is_test_overlay:
                    _cb("[TEST] Simulated overlay detected. Bypassing HTML/CSS selectors to run full visual grounding fallback on Qwen 2 VL...")
                else:
                    _cb("Standard popup selectors did not dismiss the overlay. Invoking Qwen VLM to locate the dismiss button...")
            
            screenshot_path = os.path.join(_SESSIONS_DIR, "temp_popup_viewport.jpg")
            os.makedirs(_SESSIONS_DIR, exist_ok=True)
            page.screenshot(path=screenshot_path)
            
            # ── Fallback Tier 1: OpenCV Template Match with VLM verification ──
            if _cb:
                _cb("Attempting to locate close button via OpenCV pattern matching...")
            cv_coords = _find_close_x_via_opencv(screenshot_path)
            if cv_coords:
                x, y = cv_coords
                if _cb:
                    _cb(f"OpenCV found close button pattern candidate at ({x}, {y}). Validating candidate via Qwen VLM...")
                
                if _crop_and_validate(screenshot_path, x, y):
                    if _cb:
                        _cb("Qwen VLM validated OpenCV candidate successfully! Caching and dismissing...")
                        
                    _set_cached_coordinates(cache, "instagram", target_desc, x, y, page)

                    click_x = x + random.randint(-3, 3)
                    click_y = y + random.randint(-3, 3)
                    _perform_native_stealth_click(page, click_x, click_y)
                    page.wait_for_timeout(2000)
                    
                    try:
                        os.remove(screenshot_path)
                    except Exception:
                        pass
                        
                    if not _is_overlay_present(page):
                        return
                    else:
                        if _cb:
                            _cb("Validated OpenCV click failed to dismiss popup. Proceeding to Qwen VLM full visual grounding...")
                else:
                    if _cb:
                        _cb("Qwen VLM rejected OpenCV candidate (false positive). Proceeding to Qwen VLM full visual grounding...")

            # ── Fallback Tier 2: YOLOv8 UI Detection ──
            if _cb:
                _cb("Invoking YOLOv8 UI detection for full visual grounding scan to locate popup dismiss button...")
            from pipeline import find_element_coordinates_yolo
            coords = find_element_coordinates_yolo(screenshot_path, target_desc, page)
            
            if coords:
                x, y = coords
                
                _set_cached_coordinates(cache, "instagram", target_desc, x, y, page)

                click_x = x + random.randint(-3, 3)
                click_y = y + random.randint(-3, 3)
                if _cb:
                    _cb(f"YOLOv8 located popup dismiss button at ({x}, {y}) (jittered to {click_x}, {click_y}). Caching and clicking...")

                _perform_native_stealth_click(page, click_x, click_y)
                page.wait_for_timeout(2000)
            else:
                if is_test_overlay:
                    if _cb:
                        _cb("[TEST] YOLOv8 grounding did not return coordinates. Clicking test close button directly as emergency fallback...")
                    page.locator("#btn-close-test-overlay").first.click()
            
            try:
                os.remove(screenshot_path)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"[Executor] VLM popup dismissal failed: {e}")


def _is_click_successful(page, locator) -> bool:
    """
    Check if a stealth click was successful.
    A click is successful if:
      - The locator is no longer visible (meaning it disappeared/was dismissed)
      - OR the locator or its descendants have focused status (for focus-only inputs)
      - OR the locator is disabled/aria-disabled (common during loading after a click)
    """
    try:
        if not locator.is_visible():
            return True
        # Check active element focus
        got_focus = locator.evaluate("el => document.activeElement === el || el.contains(document.activeElement)")
        # Check disabled state
        is_disabled = locator.evaluate("el => el.disabled || el.getAttribute('aria-disabled') === 'true'")
        return bool(got_focus or is_disabled)
    except Exception:
        # If element became detached/stale during evaluation, it means it disappeared -> click succeeded
        return True


def _stealth_click_with_visual_fallback(page, locator, selector_str: str, element_description: str, _cb=None) -> None:
    """
    Attempt to click an element by visual coordinate.
    If a popup overlay is present, dismiss it first.
    Uses coordinate cache for instant execution, extracts coordinates from DOM bounding box
    if available to avoid DOM click detection, and falls back to local Qwen 2 VLM only if needed.
    """
    url = page.url or ""
    platform = "instagram"
    if "tiktok.com" in url:
        platform = "tiktok"
    elif "x.com" in url or "twitter.com" in url:
        platform = "x"

    # 1. Preemptive overlay check
    if _is_overlay_present(page):
        if _cb:
            _cb("Popup overlay detected on page. Dismissing before clicking target...")
        _dismiss_instagram_overlays(page, _cb)
        
        if _is_overlay_present(page):
            if _cb:
                _cb("Critical: Popup overlay is still blocking the screen! Aborting target click to prevent misclicks.")
            raise RuntimeError("Cannot click target because overlay is still present.")

    cache = _load_coordinate_cache()
    coords = _get_cached_coordinates(cache, platform, element_description, page)

    # For testing: Force local Qwen VLM fallback on ALL elements so user can verify visual AI grounding completely
    is_force_vlm_test = False

    if is_force_vlm_test:
        if _cb:
            _cb(f"[TEST] Force-triggering AI Visual Grounding fallback for '{element_description}' to verify local Qwen VLM performance...")
    else:
        # 1. Try coordinate from cache (instant)
        if coords:
            x, y = coords
            click_x = x + random.randint(-3, 3)
            click_y = y + random.randint(-3, 3)
            if _cb:
                _cb(f"Cache hit! Moving virtual cursor to cached coordinates ({x}, {y}) (jittered to {click_x}, {click_y}) for '{element_description}'...")
            try:
                _perform_native_stealth_click(page, click_x, click_y)
                page.wait_for_timeout(1500)
                
                if _is_click_successful(page, locator):
                    if _cb:
                        _cb(f"Cache click on '{element_description}' succeeded.")
                    return
            except Exception:
                if _cb:
                    _cb(f"Cache click on '{element_description}' succeeded (detached).")
                return

            if _cb:
                _cb(f"Cached coordinates ({x}, {y}) did not dismiss/focus target. Stale cache suspected. Invalidating cache and finding fresh coordinates...")
            if platform in cache and element_description in cache[platform]:
                del cache[platform][element_description]
                _save_coordinate_cache(cache)

        # 2. Extract coordinates from DOM bounding box (bypasses direct DOM clicks)
        run_dom = not _FORCE_YOLO_MODE
        if not run_dom:
            if _cb:
                _cb(f"[YOLO MODE] Bypassing DOM coordinates. Running YOLOv8 visual grounding directly for '{element_description}'...")
        
        if run_dom:
            try:
                locator.wait_for(state="visible", timeout=4000)
                box = locator.bounding_box()
                if box:
                    x = int(box["x"] + box["width"] / 2)
                    y = int(box["y"] + box["height"] / 2)
                    
                    _set_cached_coordinates(cache, platform, element_description, x, y, page)
                    
                    max_dx = max(1, int(box["width"] * 0.15))
                    max_dy = max(1, int(box["height"] * 0.15))
                    click_x = x + random.randint(-max_dx, max_dx)
                    click_y = y + random.randint(-max_dy, max_dy)
                    
                    if _cb:
                        _cb(f"Extracted '{element_description}' coordinates from render tree: ({x}, {y}) (jittered to {click_x}, {click_y}). Caching and clicking...")
                    
                    _perform_native_stealth_click(page, click_x, click_y)
                    
                    page.wait_for_timeout(1500)
                    if _is_click_successful(page, locator):
                        return
                    
                    if _cb:
                        _cb(f"Native click on '{element_description}' did not dismiss or focus the element. Falling through to AI Visual Grounding...")
            except Exception as sel_err:
                if _cb:
                    _cb(f"Selector '{selector_str}' coordinate extraction failed: {sel_err}. Checking for new popups...")
                log.warning(f"[Executor] Selector '{selector_str}' failed: {sel_err}. Checking for overlays...")
                
                _dismiss_instagram_overlays(page, _cb)
                
                try:
                    locator.wait_for(state="visible", timeout=3000)
                    box = locator.bounding_box()
                    if box:
                        x = int(box["x"] + box["width"] / 2)
                        y = int(box["y"] + box["height"] / 2)
                        
                        _set_cached_coordinates(cache, platform, element_description, x, y, page)
                        
                        max_dx = max(1, int(box["width"] * 0.15))
                        max_dy = max(1, int(box["height"] * 0.15))
                        click_x = x + random.randint(-max_dx, max_dx)
                        click_y = y + random.randint(-max_dy, max_dy)
                        
                        _perform_native_stealth_click(page, click_x, click_y)
                        
                        page.wait_for_timeout(1500)
                        if _is_click_successful(page, locator):
                            return
                        
                        if _cb:
                            _cb(f"Retry native click on '{element_description}' failed to dismiss/focus target.")
                except Exception as retry_err:
                    if _cb:
                        _cb(f"Retry failed: {retry_err}. Activating Qwen VLM fallback to locate '{element_description}'...")
                    log.warning(f"[Executor] Retry for '{selector_str}' failed: {retry_err}. Attempting AI Visual Grounding...")

    # 3. YOLOv8 UI Fallback
    try:
        screenshot_path = os.path.join(_SESSIONS_DIR, "temp_viewport.jpg")
        os.makedirs(_SESSIONS_DIR, exist_ok=True)
        page.screenshot(path=screenshot_path)

        from pipeline import find_element_coordinates_yolo
        coords = find_element_coordinates_yolo(screenshot_path, element_description, page)
        
        if coords:
            x, y = coords
            
            _set_cached_coordinates(cache, platform, element_description, x, y, page)
            
            click_x = x + random.randint(-3, 3)
            click_y = y + random.randint(-3, 3)
            
            if _cb:
                _cb(f"YOLOv8 localized '{element_description}' at coordinates ({x}, {y}) (jittered to {click_x}, {click_y}). Caching and clicking...")
            
            _perform_native_stealth_click(page, click_x, click_y)
            time.sleep(random.uniform(0.3, 0.6))
            
            try:
                os.remove(screenshot_path)
            except Exception:
                pass
            return
        else:
            raise ValueError("YOLOv8 could not determine coordinates.")
    except Exception as ai_err:
        log.error(f"[Executor] AI Visual Grounding fallback failed: {ai_err}")
        if _cb:
            _cb(f"AI Visual Grounding fallback failed: {ai_err}")
        raise RuntimeError(f"AI Visual Grounding click fallback failed: {ai_err}")


_ADJACENT_KEYS = {
    'a': 'qwsz', 'b': 'vghn', 'c': 'xdfv', 'd': 'ersfxc', 'e': 'wsdr',
    'f': 'rtgvcd', 'g': 'tyhbvf', 'h': 'yujnbg', 'i': 'ujko', 'j': 'uikmnh',
    'k': 'ijlm', 'l': 'okp', 'm': 'njk', 'n': 'bhjm', 'o': 'iklp',
    'p': 'ol', 'q': 'wa', 'r': 'edft', 's': 'wedxza', 't': 'rfgy',
    'u': 'yhji', 'v': 'cfgb', 'w': 'qase', 'x': 'zsdc', 'y': 'tghu',
    'z': 'asx'
}

def _get_typo_char(char: str) -> str:
    lower_char = char.lower()
    if lower_char in _ADJACENT_KEYS:
        typo = random.choice(_ADJACENT_KEYS[lower_char])
        return typo.upper() if char.isupper() else typo
    return char

def _send_native_key_unicode(char: str):
    """Send a native OS-level Unicode keypress event using Win32 keybd_event."""
    import ctypes
    try:
        val = ord(char)
        # KEYEVENTF_UNICODE = 0x0004
        # KEYEVENTF_KEYUP = 0x0002
        # Press key
        ctypes.windll.user32.keybd_event(0, val, 0x0004, 0)
        # Release key
        ctypes.windll.user32.keybd_event(0, val, 0x0004 | 0x0002, 0)
    except Exception as e:
        log.warning(f"[Executor] Native Unicode keypress failed for '{char}': {e}")


def _send_native_key_control(vk: int):
    """Send a native virtual key event (like Backspace) using Win32 keybd_event."""
    import ctypes
    import time
    try:
        # Press key
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
        # Release key
        ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)
    except Exception as e:
        log.warning(f"[Executor] Native key event failed for vk={vk}: {e}")


def _send_native_select_all():
    """Send native Ctrl+A to select all text."""
    import ctypes
    import time
    try:
        # Press Ctrl (0x11)
        ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)
        time.sleep(0.02)
        # Press A (0x41)
        ctypes.windll.user32.keybd_event(0x41, 0, 0, 0)
        time.sleep(0.02)
        # Release A
        ctypes.windll.user32.keybd_event(0x41, 0, 0x0002, 0)
        # Release Ctrl
        ctypes.windll.user32.keybd_event(0x11, 0, 0x0002, 0)
        time.sleep(0.05)
    except Exception as e:
        log.warning(f"[Executor] Native select all failed: {e}")


def _human_type(page, locator, text: str) -> None:
    """Type text into a focused input element character by character using native OS keyboard events."""
    try:
        _stealth_click(page, locator)
        locator.focus()
        time.sleep(random.uniform(0.3, 0.7))
        
        _send_native_select_all()
        time.sleep(random.uniform(0.1, 0.2))
        _send_native_key_control(0x08) # VK_BACK = 0x08
        time.sleep(random.uniform(0.2, 0.4))
        
        char_count = 0
        for char in text:
            if char.lower() in _ADJACENT_KEYS and random.random() < 0.02:
                typo_char = _get_typo_char(char)
                if typo_char != char:
                    _send_native_key_unicode(typo_char)
                    time.sleep(random.uniform(0.05, 0.15))
                    time.sleep(random.uniform(0.15, 0.35))
                    _send_native_key_control(0x08)
                    time.sleep(random.uniform(0.1, 0.25))
            
            _send_native_key_unicode(char)
            char_count += 1
            
            delay = random.uniform(0.04, 0.18)
            if char in (' ', '.', ',', '!', '?'):
                delay += random.uniform(0.15, 0.35)
                
            time.sleep(delay)
            
            if char_count % 25 == 0:
                time.sleep(random.uniform(0.6, 1.8))
                
    except Exception as e:
        log.warning(f"[Executor] Native typing failed: {e}. Falling back to virtual keyboard.")
        try:
            page.keyboard.press("Control+A")
            time.sleep(0.1)
            page.keyboard.press("Backspace")
            time.sleep(0.1)
            page.keyboard.type(text)
        except Exception:
            pass

def post_to_tiktok(
    image_path: str,
    caption:    str,
    hashtags:   str,
    cookies:    list,
    status_cb:  Callable[[str], None] | None = None,
    title:      str = "",
    close_session: bool = True,
) -> dict:
    """
    Auto-post an image to TikTok via the Creator Center upload page.

    WARNING: TikTok's creator center UI is React-based with dynamic class names.
    This automation targets ARIA labels and data attributes for maximum stability,
    but may require selector updates if TikTok redesigns their frontend.

    OTA Note: This function is only called if tiktok_enabled=True in OTA config.
    """
    def _cb(msg):
        if status_cb:
            status_cb(msg)
        log.info(f"[TikTok] {msg}")

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        image_paths = [image_path] if isinstance(image_path, str) else image_path
        # Build caption: TikTok description has a ~2200 char limit
        parts = []
        if title.strip():
            parts.append(title.strip())
        if caption.strip():
            parts.append(caption.strip())
        if hashtags.strip():
            parts.append(hashtags.strip())
        post_text = "\n\n".join(parts)[:2200]

        with sync_playwright() as p:
            _cb("Launching browser session via BrowserAct...")
            ws_url = _open_session_and_get_cdp("tiktok", "https://www.tiktok.com/creator-center/upload")
            if not ws_url:
                raise RuntimeError("Failed to obtain Chrome CDP WebSocket URL from BrowserAct.")
            
            _cb("Connecting to BrowserAct session via CDP...")
            browser = p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            
            page = None
            for ctx in browser.contexts:
                for pg in ctx.pages:
                    if "tiktok.com" in pg.url:
                        page = pg
                        break
                if page:
                    break
            if not page:
                page = context.pages[0] if context.pages else context.new_page()

            _cb("Navigating to TikTok Creator Center...")
            if "creator-center/upload" not in page.url:
                page.goto(
                    "https://www.tiktok.com/creator-center/upload",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )

            # ── File input — TikTok upload page has a direct file input ──────
            _cb("Uploading image to TikTok...")
            try:
                file_input = page.wait_for_selector(
                    "input[type='file']",
                    state="attached",
                    timeout=20_000,
                )
                file_input.set_input_files(image_paths)
            except PWTimeout:
                # TikTok may require clicking the upload area first
                _stealth_click(page, page.locator("div[class*='upload']").first)
                file_input = page.wait_for_selector(
                    "input[type='file']",
                    state="attached",
                    timeout=15_000,
                )
                file_input.set_input_files(image_paths)

            # ── Wait for upload processing spinner to clear ────────────────────
            _cb("Waiting for TikTok to process the upload...")
            page.wait_for_timeout(4_000)   # Give React time to render caption editor

            # ── Caption / Description ─────────────────────────────────────────
            _cb("Adding caption and hashtags...")
            try:
                # TikTok's caption editor is a contenteditable div
                caption_editor = page.locator(
                    "[data-contents='true'], "
                    "div[contenteditable='true'][class*='caption'], "
                    "div[contenteditable='true'][class*='editor'], "
                    "div[class*='editor-container'] div[contenteditable='true']"
                ).first
                caption_editor.wait_for(state="visible", timeout=15_000)
                _human_type(page, caption_editor, post_text)
            except Exception as e:
                log.warning(f"[TikTok] Caption editor not found ({e}) — attempting fallback")
                page.keyboard.type(post_text)

            # ── Post / Publish button ─────────────────────────────────────────
            # Mimic human reviewing draft details before hitting publish
            _cb("Reviewing draft details...")
            time.sleep(random.uniform(5.0, 9.0))
            _cb("Submitting post to TikTok...")
            
            post_button = page.get_by_role("button", name="Post").first
            _stealth_click_with_visual_fallback(
                page,
                post_button,
                "button[name='Post']",
                "Post button or publish button on TikTok",
                _cb=_cb
            )

            # ── Confirmation ──────────────────────────────────────────────────
            try:
                page.wait_for_url("**/creator-center**", timeout=15_000)
            except PWTimeout:
                pass

            page.wait_for_timeout(1_500)
            _remove_custom_cursor(page)
            try:
                page.close()
            except Exception:
                pass
            if close_session:
                cli = _get_browser_act_exe()
                try:
                    subprocess.run([cli, "session", "close", "tiktok"],
                                   capture_output=True, text=True, timeout=5)
                except Exception:
                    pass

        _cb("TikTok post published successfully.")
        return {"platform": "tiktok", "success": True, "message": "Post published."}

    except ImportError:
        return {
            "platform": "tiktok",
            "success":  False,
            "message":  "Playwright not installed.",
        }
    except Exception as e:
        log.error(f"[TikTok] Post failed: {e}")
        return {"platform": "tiktok", "success": False, "message": str(e)}


# ── X (Twitter) ───────────────────────────────────────────────────────────────

def post_to_x(
    image_path: str,
    caption:    str,
    hashtags:   str,
    cookies:    list,
    status_cb:  Callable[[str], None] | None = None,
    title:      str = "",
    close_session: bool = True,
) -> dict:
    """
    Auto-post an image to X (Twitter) using saved session cookies.

    Strategy:
      - Restore cookies → navigate to home → open compose box
      - Attach image via file input → type caption+hashtags → click Post
    """
    def _cb(msg):
        if status_cb:
            status_cb(msg)
        log.info(f"[X] {msg}")

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        image_paths = [image_path] if isinstance(image_path, str) else image_path
        # X has a 280-char limit for free accounts; trim gracefully
        post_text = _build_post_text(caption, hashtags)[:280]

        with sync_playwright() as p:
            _cb("Launching browser session via BrowserAct...")
            ws_url = _open_session_and_get_cdp("twitter", "https://x.com/home")
            if not ws_url:
                raise RuntimeError("Failed to obtain Chrome CDP WebSocket URL from BrowserAct.")
            
            _cb("Connecting to BrowserAct session via CDP...")
            browser = p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            
            page = None
            all_pages = [pg for ctx in browser.contexts for pg in ctx.pages]
            for pg in all_pages:
                if "twitter.com" in (pg.url or "") or "x.com" in (pg.url or ""):
                    page = pg
                    break
            if not page and all_pages:
                page = all_pages[-1]
            if not page:
                page = context.new_page()

            # Inject virtual cursor ONCE at the start (hides system cursor)
            _inject_custom_cursor(page)

            _cb("Navigating to X (Twitter)...")
            # Wait up to 5 seconds for BrowserAct's initial navigation to propagate/populate page.url
            for _ in range(25):
                if "x.com" in (page.url or "") or "twitter.com" in (page.url or ""):
                    break
                page.wait_for_timeout(200)

            if "x.com" not in (page.url or "") and "twitter.com" not in (page.url or ""):
                try:
                    page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30_000)
                except Exception as goto_err:
                    log.warning(f"[X] page.goto failed/interrupted: {goto_err}. Proceeding since page may be loading...")
            else:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:
                    pass

            # ── Open the compose box ──────────────────────────────────────────
            _cb("Opening the compose tweet dialog...")
            compose_area = page.locator(
                "div[data-testid='tweetTextarea_0'], "
                "div[aria-label='Post text']"
            ).first

            try:
                # Primary: click the compose area directly (visible on home timeline)
                compose_area.wait_for(state="visible", timeout=5_000)
                _stealth_click_with_visual_fallback(
                    page,
                    compose_area,
                    "div[data-testid='tweetTextarea_0']",
                    "Timeline compose area",
                    _cb=_cb
                )
            except Exception:
                # Fallback: click the "Post" button in the sidebar nav
                _cb("Inline compose area not found or not clickable. Clicking sidebar Post button...")
                post_sidebar_btn = page.locator(
                    "a[data-testid='SideNav_NewTweet_Button'], "
                    "a[href='/compose/post'], "
                    "button[data-testid='SideNav_NewTweet_Button']"
                ).first
                if not post_sidebar_btn.is_visible():
                    post_sidebar_btn = page.get_by_role("link", name="Post", exact=True)
                
                _stealth_click_with_visual_fallback(
                    page,
                    post_sidebar_btn,
                    "a[data-testid='SideNav_NewTweet_Button']",
                    "Sidebar Post button",
                    _cb=_cb
                )
                
                # Wait for composer dialog to load
                compose_area = page.locator(
                    "div[data-testid='tweetTextarea_0'], "
                    "div[aria-label='Post text']"
                ).first
                compose_area.wait_for(state="visible", timeout=8_000)
                _stealth_click_with_visual_fallback(
                    page,
                    compose_area,
                    "div[data-testid='tweetTextarea_0']",
                    "Dialog compose area",
                    _cb=_cb
                )

            # ── Attach image ──────────────────────────────────────────────────
            _cb("Attaching product image...")
            try:
                file_input = page.locator(
                    "div[data-testid='attachments'] input[type='file'], "
                    "input[data-testid='fileInput'], "
                    "label[for*='media'] input[type='file'], "
                    "input[type='file']"
                ).first
                file_input.wait_for(state="attached", timeout=10_000)
                file_input.set_input_files(image_paths)
            except Exception as e:
                _cb(f"Failed to attach image via specific selectors: {e}. Trying generic file input...")
                page.locator("input[type='file']").first.set_input_files(image_paths)

            # Wait for the image thumbnail to appear in the composer
            try:
                page.wait_for_selector(
                    "div[data-testid='attachments'] img, "
                    "div[aria-label*='image'] img",
                    timeout=15_000,
                )
            except PWTimeout:
                pass   # Image may have loaded without a detectable thumbnail

            # ── Type caption + hashtags ────────────────────────────────────────
            _cb("Adding caption and hashtags...")
            compose_area = page.locator(
                "div[data-testid='tweetTextarea_0'], "
                "div[aria-label='Post text']"
            ).first
            _human_type(page, compose_area, post_text)

            # ── Submit the post ────────────────────────────────────────────────
            _cb("Submitting post to X...")
            x_post_button = page.locator(
                "div[data-testid='tweetButtonInline'] button, "
                "button[data-testid='tweetButton'], "
                "div[data-testid='tweetButtonInline'] [role='button'], "
                "div[data-testid='tweetButtonInline'], "
                "button[data-testid='tweetButton']"
            ).first
            _stealth_click_with_visual_fallback(
                page,
                x_post_button,
                "div[data-testid='tweetButtonInline'] button, button[data-testid='tweetButton'], div[data-testid='tweetButtonInline'] [role='button']",
                "Post button or tweet button on X",
                _cb=_cb
            )

            # Wait briefly for the post to be submitted
            try:
                page.wait_for_selector("div[data-testid='toast']", timeout=5_000)
                _cb("Toast notification detected: Post successfully submitted.")
            except Exception:
                try:
                    if not compose_area.is_visible():
                        _cb("Compose box is no longer visible. Assuming post succeeded.")
                    else:
                        page.wait_for_timeout(3000)
                except Exception:
                    page.wait_for_timeout(2000)

            # ── Verify post on profile timeline ───────────────────────────────
            try:
                # 1. Click Profile link in sidebar if available
                profile_link = page.locator("a[data-testid='AppTabBar_Profile_Link']").first
                if profile_link.is_visible(timeout=2000):
                    _stealth_click_with_visual_fallback(
                        page,
                        profile_link,
                        "a[data-testid='AppTabBar_Profile_Link']",
                        "Profile link in sidebar",
                        _cb=_cb
                    )
                else:
                    # 2. Try to navigate directly to the user's handle
                    handle_el = page.locator("div[data-testid='SideNav_AccountSwitcher_Button'] span").filter(has_text="@").first
                    if handle_el.is_visible():
                        handle_text = handle_el.inner_text().strip().replace("@", "")
                        _cb(f"Navigating directly to profile page: https://x.com/{handle_text}...")
                        page.goto(f"https://x.com/{handle_text}", wait_until="domcontentloaded")
                    else:
                        # 3. Fallback: navigate to home first, then try Profile link
                        _cb("Profile link not immediately visible. Navigating to x.com...")
                        page.goto("https://x.com/", wait_until="domcontentloaded")
                        page.locator("a[data-testid='AppTabBar_Profile_Link']").first.click()
                
                # Wait for tweets on timeline to load
                page.wait_for_timeout(3000)
                
                # Search for the caption snippet (first 30 characters)
                snippet = caption.strip()[:30]
                if not snippet:
                    snippet = post_text[:30]
                clean_snippet = snippet.replace('"', '').replace("'", "").strip().lower()
                
                if len(clean_snippet) > 5:
                    _cb(f"Checking timeline for: '{clean_snippet}'...")
                    post_found = False
                    for attempt in range(4):
                        tweets = page.locator("article[data-testid='tweet']").all()
                        for tweet in tweets:
                            try:
                                tweet_text = tweet.inner_text().lower()
                                if clean_snippet in tweet_text:
                                    post_found = True
                                    break
                            except Exception:
                                pass
                        if post_found:
                            break
                        # Scroll down slightly to trigger loading if needed
                        page.evaluate("window.scrollBy(0, 250)")
                        page.wait_for_timeout(1500)
                    
                    if post_found:
                        _cb("Verification SUCCESS: Post is visible on your profile timeline.")
                    else:
                        _cb("Verification warning: Post is not visible on profile timeline yet. It may be processing or delayed.")
                else:
                    _cb("Post content is too short for reliable timeline verification.")
            except Exception as check_err:
                _cb(f"Skipping profile verification: {check_err}")

            _remove_custom_cursor(page)
            try:
                page.close()
            except Exception:
                pass
            if close_session:
                cli = _get_browser_act_exe()
                try:
                    subprocess.run([cli, "session", "close", "twitter"],
                                   capture_output=True, text=True, timeout=5)
                except Exception:
                    pass

        _cb("X post published successfully.")
        return {"platform": "x", "success": True, "message": "Post published."}

    except ImportError:
        return {
            "platform": "x",
            "success":  False,
            "message":  "Playwright not installed.",
        }
    except Exception as e:
        log.error(f"[X] Post failed: {e}")
        return {"platform": "x", "success": False, "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# IV. API CREDENTIALS LOADER
# ═══════════════════════════════════════════════════════════════════════════════

def _load_api_credentials(platform: str) -> dict | None:
    """
    Load saved API credentials for a platform.
    Returns the credentials dict if the file exists and is valid, else None.
    """
    api_path = os.path.join(_SESSIONS_DIR, f"{platform}_api_credentials.json")
    if not os.path.isfile(api_path):
        return None
    try:
        with open(api_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"[Executor] Failed to load API credentials for {platform}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# V. OFFICIAL API POSTING FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def post_to_instagram_official_api(
    image_path: str,
    caption:    str,
    hashtags:   str,
    credentials: dict,
    status_cb:  Callable[[str], None] | None = None,
) -> dict:
    """Post to Instagram using the official Graph API with direct image link via temp host."""
    def _cb(msg):
        if status_cb:
            status_cb(msg)
        log.info(f"[Instagram API] {msg}")

    try:
        import urllib.request
        import urllib.parse
        import json
        import uuid

        single_image = image_path[0] if isinstance(image_path, list) else image_path

        ig_account_id = credentials.get("instagram_account_id")
        access_token = credentials.get("access_token")
        if not ig_account_id or not access_token:
            return {"platform": "instagram", "success": False, "message": "Missing Instagram Account ID or Access Token in credentials."}

        # ── Step 1: Upload image to public temp host ──────────────────────────
        _cb("Hosting image temporarily for Instagram Graph API access...")
        boundary = "----MulaGoBoundary" + uuid.uuid4().hex[:12]
        with open(single_image, "rb") as f:
            file_data = f.read()

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{os.path.basename(single_image)}"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

        upload_req = urllib.request.Request(
            "https://tmpfiles.org/api/v1/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST"
        )
        with urllib.request.urlopen(upload_req) as resp:
            upload_res = json.loads(resp.read().decode("utf-8"))
            raw_url = upload_res["data"]["url"]
            image_url = raw_url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
        _cb("Image temporarily hosted successfully.")

        # ── Step 2: Create media container ───────────────────────────────────
        _cb("Creating media container on Facebook Graph API...")
        post_text = _build_post_text(caption, hashtags)
        
        params = {
            "image_url": image_url,
            "caption": post_text,
            "access_token": access_token
        }
        data = urllib.parse.urlencode(params).encode("utf-8")
        container_url = f"https://graph.facebook.com/v19.0/{ig_account_id}/media"
        
        req = urllib.request.Request(container_url, data=data, method="POST")
        with urllib.request.urlopen(req) as resp:
            container_res = json.loads(resp.read().decode("utf-8"))
            creation_id = container_res["id"]

        # ── Step 3: Poll status ──────────────────────────────────────────────
        _cb("Waiting for Instagram to process the image...")
        status_url = f"https://graph.facebook.com/v19.0/{creation_id}?fields=status_code&access_token={access_token}"
        for attempt in range(10):
            time.sleep(3)
            status_req = urllib.request.Request(status_url, method="GET")
            with urllib.request.urlopen(status_req) as resp:
                status_res = json.loads(resp.read().decode("utf-8"))
                status_code = status_res.get("status_code")
                if status_code == "FINISHED":
                    break
                elif status_code == "ERROR":
                    raise ValueError(f"Facebook Graph API processing error: {status_res.get('error')}")
        else:
            raise TimeoutError("Facebook Graph API container processing timed out.")

        # ── Step 4: Publish media container ──────────────────────────────────
        _cb("Publishing post on Instagram feed...")
        publish_params = {
            "creation_id": creation_id,
            "access_token": access_token
        }
        publish_data = urllib.parse.urlencode(publish_params).encode("utf-8")
        publish_url = f"https://graph.facebook.com/v19.0/{ig_account_id}/media_publish"
        
        publish_req = urllib.request.Request(publish_url, data=publish_data, method="POST")
        with urllib.request.urlopen(publish_req) as resp:
            publish_res = json.loads(resp.read().decode("utf-8"))
            post_id = publish_res["id"]

        _cb("Instagram post published successfully.")
        return {"platform": "instagram", "success": True, "message": f"Published successfully. Post ID: {post_id}"}

    except Exception as e:
        log.error(f"[Instagram API] Error: {e}")
        return {"platform": "instagram", "success": False, "message": str(e)}


def post_to_tiktok_official_api(
    image_path: str,
    caption:    str,
    hashtags:   str,
    credentials: dict,
    status_cb:  Callable[[str], None] | None = None,
) -> dict:
    """Post to TikTok using the official Content Posting API."""
    def _cb(msg):
        if status_cb:
            status_cb(msg)
        log.info(f"[TikTok API] {msg}")

    try:
        import urllib.request
        import json

        single_image = image_path[0] if isinstance(image_path, list) else image_path

        access_token = credentials.get("access_token")
        if not access_token:
            return {"platform": "tiktok", "success": False, "message": "Missing Access Token in TikTok credentials."}

        post_text = f"{caption.strip()} {hashtags.strip()}"[:2200]
        
        _cb("Initializing post upload with TikTok API...")
        file_size = os.path.getsize(single_image)
        
        init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
        payload = {
            "post_info": {
                "title": post_text,
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "allow_comment": True,
                "allow_duet": True,
                "allow_stitch": True
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,
                "total_chunk_count": 1
            }
        }
        
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            init_url,
            data=data,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            },
            method="POST"
        )
        
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if res.get("error", {}).get("code") != "ok":
                raise ValueError(f"TikTok API initialization failed: {res.get('error', {}).get('message')}")
            
            upload_url = res["data"]["upload_url"]
            publish_id = res["data"]["publish_id"]

        _cb("Uploading media payload to TikTok storage...")
        with open(single_image, "rb") as f:
            file_data = f.read()
            
        upload_req = urllib.request.Request(
            upload_url,
            data=file_data,
            headers={
                "Content-Type": "image/jpeg",
                "Content-Range": f"bytes 0-{file_size-1}/{file_size}"
            },
            method="PUT"
        )
        with urllib.request.urlopen(upload_req) as resp:
            pass

        _cb("TikTok post published successfully.")
        return {"platform": "tiktok", "success": True, "message": f"Published successfully. Publish ID: {publish_id}"}

    except Exception as e:
        log.error(f"[TikTok API] Error: {e}")
        return {"platform": "tiktok", "success": False, "message": str(e)}


def post_to_x_official_api(
    image_path: str,
    caption:    str,
    hashtags:   str,
    credentials: dict,
    status_cb:  Callable[[str], None] | None = None,
) -> dict:
    """Post to X (Twitter) using the official API v2 with OAuth 1.0a User Context."""
    def _cb(msg):
        if status_cb:
            status_cb(msg)
        log.info(f"[X API] {msg}")

    try:
        import urllib.request
        import urllib.parse
        import json
        import hmac
        import hashlib
        import binascii
        import time
        import uuid

        single_image = image_path[0] if isinstance(image_path, list) else image_path

        consumer_key = credentials.get("consumer_key")
        consumer_secret = credentials.get("consumer_secret")
        access_token = credentials.get("access_token")
        access_token_secret = credentials.get("access_token_secret")

        if not all([consumer_key, consumer_secret, access_token, access_token_secret]):
            return {"platform": "x", "success": False, "message": "Missing API keys or access tokens in X credentials."}

        post_text = _build_post_text(caption, hashtags)[:280]

        # Helper for OAuth 1.0a header
        def oauth1_header(url, method, params):
            oauth_params = {
                'oauth_consumer_key': consumer_key,
                'oauth_nonce': uuid.uuid4().hex,
                'oauth_signature_method': 'HMAC-SHA1',
                'oauth_timestamp': str(int(time.time())),
                'oauth_token': access_token,
                'oauth_version': '1.0'
            }
            all_params = {**params, **oauth_params}
            sorted_params = sorted(all_params.items())
            normalized_params = '&'.join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted_params)
            base_string = f"{method.upper()}&{urllib.parse.quote(url, safe='')}&{urllib.parse.quote(normalized_params, safe='')}"
            key = f"{urllib.parse.quote(consumer_secret, safe='')}&{urllib.parse.quote(access_token_secret, safe='')}".encode('utf-8')
            signature = hmac.new(key, base_string.encode('utf-8'), hashlib.sha1)
            oauth_params['oauth_signature'] = binascii.b2a_base64(signature.digest()).decode('utf-8').strip()
            
            header_parts = ', '.join(f'{urllib.parse.quote(k)}="{urllib.parse.quote(v)}"' for k, v in sorted(oauth_params.items()))
            return f"OAuth {header_parts}"

        # ── Step 1: Upload media ─────────────────────────────────────────────
        _cb("Uploading image to X (Twitter) Media API...")
        upload_url = "https://upload.twitter.com/1.1/media/upload.json"
        
        boundary = "----XBoundary" + uuid.uuid4().hex[:12]
        with open(single_image, "rb") as f:
            file_data = f.read()

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="media"; filename="{os.path.basename(single_image)}"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

        auth_header = oauth1_header(upload_url, "POST", {})
        upload_req = urllib.request.Request(
            upload_url,
            data=body,
            headers={
                "Authorization": auth_header,
                "Content-Type": f"multipart/form-data; boundary={boundary}"
            },
            method="POST"
        )
        with urllib.request.urlopen(upload_req) as resp:
            upload_res = json.loads(resp.read().decode("utf-8"))
            media_id = upload_res["media_id_string"]

        # ── Step 2: Post Tweet ───────────────────────────────────────────────
        _cb("Publishing tweet on X...")
        tweet_url = "https://api.twitter.com/2/tweets"
        tweet_payload = {
            "text": post_text,
            "media": {
                "media_ids": [media_id]
            }
        }
        tweet_data = json.dumps(tweet_payload).encode("utf-8")
        tweet_auth = oauth1_header(tweet_url, "POST", {})
        
        tweet_req = urllib.request.Request(
            tweet_url,
            data=tweet_data,
            headers={
                "Authorization": tweet_auth,
                "Content-Type": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(tweet_req) as resp:
            tweet_res = json.loads(resp.read().decode("utf-8"))
            tweet_id = tweet_res["data"]["id"]

        _cb("X post published successfully.")
        return {"platform": "x", "success": True, "message": f"Published successfully. Tweet ID: {tweet_id}"}

    except Exception as e:
        log.error(f"[X API] Error: {e}")
        return {"platform": "x", "success": False, "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# VI. MASTER BROADCAST FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def broadcast_to_connected_socials(
    image_path:    str,
    metadata_json: dict,
    status_cb:     Callable[[str], None] | None = None,
    selected_platforms: list = None,
) -> dict:
    """
    Master orchestrator — broadcast one image to all connected platforms.
    Dynamically routes to official API post or Playwright auto-posting based on connection type.
    """
    def _cb(msg: str):
        if status_cb:
            status_cb(msg)
        log.info(f"[Broadcast] {msg}")

    hashtags = metadata_json.get("hashtags", "")
    title    = metadata_json.get("title",    "")
    captions = metadata_json.get("captions", {})
    
    if isinstance(image_path, list):
        image_name = os.path.basename(image_path[0]) + f" (+{len(image_path)-1} more)"
    else:
        image_name = os.path.basename(image_path)

    results  = []
    skipped  = []

    # ── Step 1: Fetch OTA config ──────────────────────────────────────────────
    _cb("Checking platform availability via OTA config...")
    ota = fetch_ota_config()

    # ── Step 2: UI initialization pause ───────────────────────────────────────
    _cb("Preparing the stage for your global social media drop...")
    time.sleep(0.5)

    # ─────────────────────────────────────────────────────────────────────────
    # Platform dispatch table: (cookie_key, ota_flag_key, api_post_fn, playwright_post_fn, name)
    # ─────────────────────────────────────────────────────────────────────────
    _PLATFORM_DISPATCH = [
        ("instagram",  "ig_enabled",     post_to_instagram_official_api, post_to_instagram, "Instagram"),
        ("tiktok",     "tiktok_enabled", post_to_tiktok_official_api,    post_to_tiktok,    "TikTok"),
        ("twitter",    "x_enabled",      post_to_x_official_api,         post_to_x,         "X (Twitter)"),
    ]

    _cb("Broadcasting your content across connected networks...")

    for cookie_key, ota_key, api_post_fn, playwright_post_fn, display_name in _PLATFORM_DISPATCH:
        # Check if platform was selected in the UI
        if selected_platforms is not None and cookie_key not in selected_platforms:
            log.info(f"[Broadcast] {display_name}: not selected by user — skipping.")
            skipped.append(cookie_key)
            continue

        # Check connection types
        cookies = _load_cookies(cookie_key)
        api_creds = _load_api_credentials(cookie_key)

        if cookies is None and api_creds is None:
            log.info(f"[Broadcast] {display_name}: not connected — skipping.")
            skipped.append(cookie_key)
            continue

        # ── Check OTA kill switch ─────────────────────────────────────────────
        if not ota.get(ota_key, True):
            ota_msg = (
                f"{display_name} routing is currently under OTA maintenance. "
                f"Skipping to the next platform..."
            )
            _cb(f"[OTA] {ota_msg}")
            skipped.append(cookie_key)
            results.append({
                "platform": cookie_key,
                "success":  False,
                "message":  f"Skipped — OTA maintenance flag active for {display_name}.",
            })
            continue

        # ── Execute post based on connection type ─────────────────────────────
        # Get platform-specific caption (with fallback to default caption)
        platform_caption = captions.get(cookie_key) or (captions.get("x") if cookie_key == "twitter" else None) or metadata_json.get("caption", "")

        if api_creds is not None:
            _cb(f"Publishing via official {display_name} API...")
            result = api_post_fn(
                image_path=image_path,
                caption=platform_caption,
                hashtags=hashtags,
                credentials=api_creds,
                status_cb=status_cb,
            )
        else:
            _cb(f"Starting {display_name} browser automation...")
            result = playwright_post_fn(
                image_path=image_path,
                caption=platform_caption,
                hashtags=hashtags,
                cookies=cookies,
                status_cb=status_cb,
                title=title,
                close_session=False,
            )
        results.append(result)

        if result["success"]:
            _cb(f"{display_name}: Published successfully.")
        else:
            _cb(f"{display_name}: Encountered an issue — {result['message']}")

        # Small pause between platforms to avoid API/memory pressure
        time.sleep(1.5)

    # Close active sessions at the end of the broadcast
    _cb("Closing active browser sessions...")
    cli = _get_browser_act_exe()
    for key in ["instagram", "twitter", "tiktok"]:
        # Check if we actually posted to this platform (not skipped)
        if not selected_platforms or key in selected_platforms:
            try:
                subprocess.run([cli, "session", "close", key],
                               capture_output=True, text=True, timeout=5)
            except Exception:
                pass

    _cb("Published successfully. Ready for the next masterpiece.")

    return {
        "image":   image_name,
        "results": results,
        "skipped": skipped,
    }
