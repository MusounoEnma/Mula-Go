"""
main.py — MULA GO Entry Point

Launches the PyWebView desktop window and exposes the Python API bridge
to the JavaScript frontend.
"""

import sys
import os
import json
import time
import threading
import webview

# ─── Monkeypatch PathFinder.invalidate_caches to resolve Python 3.14 KeyError bug ───
try:
    import sys
    for finder in sys.meta_path:
        if finder.__name__ == 'PathFinder' if hasattr(finder, '__name__') else False:
            orig_invalidate = finder.invalidate_caches
            def make_safe_invalidate(orig_func):
                def safe_invalidate(*args, **kwargs):
                    try:
                        return orig_func()
                    except TypeError:
                        try:
                            return orig_func(*args, **kwargs)
                        except KeyError:
                            pass
                        except Exception:
                            pass
                    except KeyError:
                        pass
                    except Exception:
                        pass
                return safe_invalidate
            finder.invalidate_caches = classmethod(make_safe_invalidate(orig_invalidate))
except Exception as patch_err:
    print(f"[MulaGo] Failed to patch PathFinder in main: {patch_err}")


from security import get_hardware_id, verify_totp
from license_manager import check_license, save_license

# ─── Resolve paths ────────────────────────────────────────────────────────────
# When frozen as a .exe, sys.executable points to the .exe; use its dir.
_APP_DIR      = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
                else os.path.dirname(os.path.abspath(__file__))
_BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
_UI_DIR       = os.path.join(_BASE_DIR, "ui")
_INDEX_HTML   = os.path.join(_UI_DIR, "index.html")
_SESSIONS_DIR = os.path.join(_BASE_DIR, "data", "sessions")
_MODELS_DIR   = os.path.join(_APP_DIR, "models", "gemma")
_MODEL_REPO   = "abetlen/paligemma-3b-mix-224-gguf"
_QWEN_DIR     = os.path.join(_APP_DIR, "models", "qwen")
_QWEN_REPO     = "bartowski/Qwen2-VL-2B-Instruct-GGUF"


# Supported image extensions for folder scanning
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".avif"}

# Platform login & success URL patterns
_PLATFORM_URLS = {
    "tiktok":    {"login": "https://www.tiktok.com/login",
                  "success": "**/tiktok.com/foryou**"},
    "instagram": {"login": "https://www.instagram.com/accounts/login/",
                  "success": "**/instagram.com/**"},
    "twitter":   {"login": "https://twitter.com/i/flow/login",
                  "success": "**/twitter.com/home**"},
}


# ─── Python ↔ JavaScript API Bridge ──────────────────────────────────────────
class MulaGoApi:
    """
    All public methods on this class are callable from JavaScript via:
        window.pywebview.api.<method_name>(<args>)
    """

    def __init__(self):
        self._hwid: str | None = None
        self._image_queue: list = []
        self._window = None   # injected by main() via set_window() after create_window()

        # ── Model download state (polled by JS during first-boot overlay) ────
        self._dl_state: dict = {
            "running": False,
            "done":    False,
            "percent": 0,
            "status":  "idle",
            "error":   None,
        }

        # ── Pipeline processing state (polled by JS status feed) ─────────────
        self._pipe_state: dict = {
            "running":       False,
            "done":          False,
            "log":           [],    # [{"ts": "MM:SS", "msg": str}]
            "queue_status":  {},    # {image_path: "pending"|"scanning"|"seo"|"posting"|"done"|"failed"}
            "vision_data":   {},    # {image_path: raw_moondream_text}
            "edit_data":     {},    # {image_path: image_edit_prompt}
            "current_index": 0,
            "total":         0,
            "error":         None,
            "results":       [],
        }

    def set_window(self, window) -> None:
        """
        Inject the active PyWebView window object after create_window().
        Must be called before webview.start() so that dialog methods
        can reference self._window instead of webview.windows[0],
        which avoids threading deadlocks on some platforms.
        """
        self._window = window

    # ── Hardware & License ────────────────────────────────────────────────────

    def get_hardware_id(self) -> str:
        """Return the machine's unique Hardware ID (cached after first call)."""
        if self._hwid is None:
            self._hwid = get_hardware_id()
        return self._hwid

    def check_license(self) -> dict:
        """Check if a valid license exists for this machine."""
        return check_license(self.get_hardware_id())

    def verify_code(self, code: str) -> dict:
        """Verify the 6-digit activation code; save license on success."""
        hwid = self.get_hardware_id()
        if verify_totp(hwid, str(code).strip()):
            saved = save_license(hwid)
            if saved:
                return {"success": True, "message": "Activation successful. Welcome to MULA GO."}
            return {"success": False, "message": "Code correct, but failed to save license. Check permissions."}
        return {"success": False, "message": "Invalid or expired code. Please try again."}

    # ── Social Media Connection via Playwright ────────────────────────────────

    def connect_social_account(self, platform: str) -> dict:
        """
        Launch a real Chromium browser for the user to log in to a social
        platform. Once login is detected, extract & save session cookies
        to data/sessions/{platform}_cookies.json.

        Playwright runs in a separate thread to avoid blocking PyWebView's
        event loop. We use a threading.Event to wait for the result.
        """
        platform = str(platform).strip().lower()

        if platform not in _PLATFORM_URLS:
            return {
                "success": False,
                "platform": platform,
                "message": f"Platform '{platform}' is not supported for social login."
            }

        result_box = {"result": None}
        done_event = threading.Event()

        def _playwright_flow():
            try:
                from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
                from executor import _open_session_and_get_cdp, _get_browser_act_exe
                import subprocess
                import time as _time

                urls      = _PLATFORM_URLS[platform]
                login_url = urls["login"]

                with sync_playwright() as p:
                    ws_url = _open_session_and_get_cdp(platform, login_url)
                    if not ws_url:
                        raise RuntimeError("Failed to obtain Chrome CDP WebSocket URL from BrowserAct.")

                    browser = p.chromium.connect_over_cdp(ws_url)
                    context = browser.contexts[0] if browser.contexts else browser.new_context()

                    # ── Find the platform tab (retry up to 12s to allow BrowserAct to open it)
                    platform_keywords = {
                        "tiktok":    ["tiktok.com"],
                        "instagram": ["instagram.com"],
                        "twitter":   ["twitter.com", "x.com"],
                    }
                    keywords = platform_keywords.get(platform, [platform])

                    page = None
                    for _attempt in range(12):  # retry for up to 12 seconds
                        for ctx in browser.contexts:
                            for pg in ctx.pages:
                                pg_url = pg.url or ""
                                if any(kw in pg_url for kw in keywords) or pg_url == login_url:
                                    page = pg
                                    break
                            if page:
                                break
                        if page:
                            break
                        # Also check if BrowserAct opened a new tab we can navigate
                        all_pages = [pg for ctx in browser.contexts for pg in ctx.pages]
                        if all_pages and not page:
                            # Check the most recently created page
                            newest = all_pages[-1]
                            if "about:blank" in (newest.url or "") or newest.url == "":
                                # Navigate it to the login URL
                                try:
                                    newest.goto(login_url, wait_until="domcontentloaded", timeout=15_000)
                                    page = newest
                                    break
                                except Exception:
                                    pass
                        _time.sleep(1)

                    if not page:
                        # Fallback: use first available page and navigate to login URL
                        all_pages = [pg for ctx in browser.contexts for pg in ctx.pages]
                        if all_pages:
                            page = all_pages[-1]
                            try:
                                page.goto(login_url, wait_until="domcontentloaded", timeout=20_000)
                            except Exception:
                                pass
                        else:
                            page = context.new_page()
                            page.goto(login_url, wait_until="domcontentloaded", timeout=20_000)

                    # ── Platform-specific login detection (JS polling, robust) ─
                    # Strategy: "user is on main domain AND past the login page AND page loaded"
                    # Using wait_for_function + polling instead of wait_for_url
                    # because post-login URLs vary by region, device, and A/B test.
                    _JS = {
                        "tiktok": """() => {
                            const u = window.location.href;
                            return u.includes('tiktok.com') &&
                                   !u.includes('/login') &&
                                   !u.includes('/signup') &&
                                   !u.includes('/register') &&
                                   document.readyState === 'complete';
                        }""",
                        "instagram": """() => {
                            const u = window.location.href;
                            const isLoggedInUrl = u.includes('instagram.com') &&
                                   !u.includes('/login') &&
                                   !u.includes('/accounts/login') &&
                                   !u.includes('/challenge');
                            if (!isLoggedInUrl) return false;
                            // Also verify the page has actual content (nav sidebar loaded)
                            const hasNav = !!(
                                document.querySelector('nav') ||
                                document.querySelector('[role="navigation"]') ||
                                document.querySelector('svg[aria-label]') ||
                                document.querySelector('a[href="/"]')
                            );
                            return hasNav && document.readyState === 'complete';
                        }""",
                        "twitter": """() => {
                            const u = window.location.href;
                            const isLoggedInUrl = (u.includes('twitter.com') || u.includes('x.com')) &&
                                   !u.includes('login') &&
                                   !u.includes('/flow') &&
                                   (u.includes('/home') || u.includes('/notifications') ||
                                    u.includes('/messages') || u.includes('/explore') ||
                                     (/(?:twitter|x)\.com\/[A-Za-z0-9_]+$/.test(u)));
                            return isLoggedInUrl && document.readyState === 'complete';
                        }""",
                    }

                    try:
                        page.wait_for_function(
                            _JS[platform],
                            timeout=300_000,   # 5-minute window for manual login
                            polling=1_500,     # re-check every 1.5 seconds
                        )
                    except PWTimeout:
                        try:
                            page.close()
                        except Exception:
                            pass
                        cli = _get_browser_act_exe()
                        try:
                            subprocess.run([cli, "session", "close", platform], capture_output=True, text=True)
                        except Exception:
                            pass
                        result_box["result"] = {
                            "success": False,
                            "platform": platform,
                            "message": "Login timed out (5 minutes). Please try again.",
                        }
                        return

                    # ── Login confirmed — wait briefly for cookies to settle
                    page.wait_for_timeout(1_000)

                    cookies = context.cookies()

                    # Show success overlay on the tab before closing it
                    try:
                        page.evaluate("""
                            () => {
                                document.body.style.margin = '0';
                                document.body.style.overflow = 'hidden';
                                const overlay = document.createElement('div');
                                overlay.style.cssText = `
                                  position:fixed; top:0; left:0;
                                  width:100vw; height:100vh;
                                  display:flex; flex-direction:column;
                                  align-items:center; justify-content:center;
                                  background:linear-gradient(135deg,#f0f9ff,#e0f2fe);
                                  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                                  text-align:center; padding:40px; box-sizing:border-box;
                                  z-index:2147483647;
                                `;
                                overlay.innerHTML = `
                                  <div style="font-size:80px; margin-bottom:16px;">&#x2705;</div>
                                  <h2 style="color:#0369a1; margin:0 0 10px; font-size:28px; font-weight:700;">
                                    Login Successful!
                                  </h2>
                                  <p style="color:#475569; font-size:15px; line-height:1.7; max-width:380px; margin:0;">
                                    Your session cookies have been saved.<br/>
                                    <strong>This tab will close automatically.</strong>
                                  </p>
                                  <div style="margin-top:28px; width:200px; height:4px;
                                    background:#bae6fd; border-radius:99px; overflow:hidden;">
                                    <div style="height:100%; width:0%; background:#0ea5e9;
                                      border-radius:99px; animation:bar 2.2s linear forwards;"></div>
                                  </div>
                                  <style>@keyframes bar { to { width: 100%; } }</style>
                                `;
                                document.body.appendChild(overlay);
                            }
                        """)
                        page.wait_for_timeout(2_400)  # show overlay for ~2.4 seconds
                    except Exception:
                        pass  # page may have been closed manually — that's fine

                    # Close only the tab (NOT the entire Chrome browser)
                    try:
                        page.close()
                    except Exception:
                        pass
                    cli = _get_browser_act_exe()
                    try:
                        subprocess.run([cli, "session", "close", platform], capture_output=True, text=True)
                    except Exception:
                        pass

                    os.makedirs(_SESSIONS_DIR, exist_ok=True)
                    cookie_path = os.path.join(_SESSIONS_DIR, f"{platform}_cookies.json")
                    with open(cookie_path, "w", encoding="utf-8") as f:
                        json.dump(cookies, f, indent=2)

                    result_box["result"] = {
                        "success":      True,
                        "platform":     platform,
                        "status":       "connected",
                        "cookie_count": len(cookies),
                        "message":      (
                            f"Successfully connected to {platform.title()}! "
                            f"{len(cookies)} session cookies saved."
                        ),
                    }

            except ImportError:
                result_box["result"] = {
                    "success": False,
                    "platform": platform,
                    "message": "Playwright is not installed. Run: pip install playwright && playwright install chromium",
                }
            except Exception as exc:
                result_box["result"] = {
                    "success": False,
                    "platform": platform,
                    "message": str(exc),
                }
            finally:
                done_event.set()

        thread = threading.Thread(target=_playwright_flow, daemon=True)
        thread.start()
        done_event.wait()          # Block this API thread until Playwright finishes
        return result_box["result"]

    def save_api_credentials(self, platform: str, credentials: dict) -> dict:
        """Save official API credentials for a platform."""
        platform = str(platform).strip().lower()
        if platform not in _PLATFORM_URLS:
            return {"success": False, "platform": platform, "message": f"Platform '{platform}' is not supported."}
        
        try:
            os.makedirs(_SESSIONS_DIR, exist_ok=True)
            api_path = os.path.join(_SESSIONS_DIR, f"{platform}_api_credentials.json")
            with open(api_path, "w", encoding="utf-8") as f:
                json.dump(credentials, f, indent=2)
            return {
                "success": True,
                "platform": platform,
                "status": "connected",
                "message": f"Successfully saved API credentials for {platform.title()}!"
            }
        except Exception as e:
            return {"success": False, "platform": platform, "message": str(e)}

    def check_connection(self, platform: str) -> dict:
        """Check whether saved session cookies or API credentials exist for a platform."""
        platform = str(platform).strip().lower()
        cookie_path = os.path.join(_SESSIONS_DIR, f"{platform}_cookies.json")
        api_path = os.path.join(_SESSIONS_DIR, f"{platform}_api_credentials.json")
        exists = os.path.isfile(cookie_path) or os.path.isfile(api_path)
        return {"platform": platform, "connected": exists}

    def get_all_connections(self) -> dict:
        """Return connection status for all social platforms."""
        platforms = list(_PLATFORM_URLS.keys())
        return {p: self.check_connection(p)["connected"] for p in platforms}

    def get_connected_platforms(self) -> list:
        """
        Return a list of platforms that have saved session cookies or API credentials.
        """
        connected = []
        for platform in _PLATFORM_URLS:
            cookie_path = os.path.join(_SESSIONS_DIR, f"{platform}_cookies.json")
            api_path = os.path.join(_SESSIONS_DIR, f"{platform}_api_credentials.json")
            if os.path.isfile(cookie_path) or os.path.isfile(api_path):
                connected.append(platform)
        return connected

    def disconnect_account(self, platform: str) -> dict:
        """Delete saved session cookies and API credentials for a platform."""
        platform  = str(platform).strip().lower()
        cookie_path = os.path.join(_SESSIONS_DIR, f"{platform}_cookies.json")
        api_path = os.path.join(_SESSIONS_DIR, f"{platform}_api_credentials.json")
        try:
            if os.path.isfile(cookie_path):
                os.remove(cookie_path)
            if os.path.isfile(api_path):
                os.remove(api_path)
            return {"success": True, "platform": platform, "status": "disconnected"}
        except Exception as e:
            return {"success": False, "platform": platform, "message": str(e)}

    # ── Marketplace connections (placeholder — no cookie login needed) ─────────

    def connect_account(self, platform: str) -> dict:
        """Placeholder for marketplace connections (eBay, Amazon, Alibaba)."""
        platform = str(platform).strip().lower()
        print(f"[MulaGo] Marketplace connect placeholder: {platform}")
        return {
            "success":  True,
            "platform": platform,
            "status":   "connected",
            "message":  f"Connected to {platform.title()} (placeholder — API integration coming)."
        }

    # ── Native Folder Workflow ────────────────────────────────────────────────

    def select_product_folder(self) -> dict:
        """
        Open a native OS folder-picker dialog using a separate process.
        This completely isolates Tkinter from PyWebView's thread pool
        and avoids the deadlocks/silent failures on Windows.

        Returns:
            {"status": "success", "folder": str, "count": int, "images": list}
            {"status": "cancelled"}   -- user closed dialog without picking
            {"status": "error", "message": str}
        """
        try:
            import subprocess
            import sys
            
            # Simple script to run in separate process
            script = "import tkinter as tk; from tkinter import filedialog; root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); print(filedialog.askdirectory(parent=root, title='Select Product Folder', mustexist=True))"
            
            # Use CREATE_NO_WINDOW (0x08000000) on Windows to hide the console
            creationflags = 0x08000000 if sys.platform == "win32" else 0
            
            result = subprocess.run(
                [sys.executable, "-c", script], 
                capture_output=True, 
                text=True,
                creationflags=creationflags
            )
            
            folder = result.stdout.strip()
            
            if not folder:
                return {"status": "cancelled"}
                
            res = self._scan_image_folder(folder)
            
            if res.get("success"):
                return {
                    "status":  "success",
                    "folder":  res["folder"],
                    "count":   res["count"],
                    "images":  res["images"],
                }
            
            return {"status": "error", "message": res.get("message", "Scan failed.")}
            
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # Keep old name as alias so any existing callers don't break
    def open_folder_picker(self) -> dict:
        """Alias for select_product_folder() — retained for compatibility."""
        return self.select_product_folder()

    def process_dropped_folder(self, folder_path: str) -> dict:
        """Legacy drag-and-drop path — now unused; kept for compatibility."""
        return self._scan_image_folder(str(folder_path))

    def _scan_image_folder(self, folder_path: str) -> dict:
        """
        Recursively scan a folder for image files.
        Updates internal _image_queue and returns image metadata list.
        """
        images = []
        try:
            for root, _dirs, files in os.walk(folder_path):
                for fname in sorted(files):
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in _IMAGE_EXTS:
                        full_path = os.path.join(root, fname)
                        rel_folder = os.path.relpath(root, folder_path)
                        images.append({
                            "filename":   fname,
                            "path":       full_path,
                            "ext":        ext.lstrip(".").upper(),
                            "size":       os.path.getsize(full_path),
                            "rel_folder": rel_folder if rel_folder != "." else "",
                        })
        except PermissionError as e:
            return {"success": False, "message": f"Permission denied: {e}"}

        self._image_queue = images

        return {
            "success": True,
            "folder":  folder_path,
            "count":   len(images),
            "images":  images,

            # ── Placeholder: Local AI Vision ──────────────────────────────────
            # FUTURE: After scanning, each image will be passed to the Local AI
            # Vision model (e.g., LLaVA / MiniCPM-V) for object detection and
            # attribute extraction. The AI output will be stored in each image's
            # metadata dict under the key "ai_vision_data".
            # ─────────────────────────────────────────────────────────────────
        }

    def get_image_queue(self) -> list:
        """Return the current in-memory image queue."""
        return self._image_queue

    def clear_image_queue(self) -> dict:
        """Clear the current image queue."""
        self._image_queue = []
        return {"success": True, "cleared": True}

    def get_image_base64(self, image_path: str) -> str:
        """Read a local image and return it as a Base64 data URL."""
        import base64
        import mimetypes
        try:
            if os.path.isfile(image_path):
                mime_type, _ = mimetypes.guess_type(image_path)
                if not mime_type:
                    mime_type = "image/jpeg"
                with open(image_path, "rb") as image_file:
                    encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
                    return f"data:{mime_type};base64,{encoded_string}"
            return ""
        except Exception as e:
            print(f"[MulaGoApi] get_image_base64 error: {e}")
            return ""

    # ── Misc ──────────────────────────────────────────────────────────────────

    def get_app_version(self) -> str:
        return "1.0.0"

    # ── Model Management ─────────────────────────────────────────────────────

    def get_model_status(self) -> dict:
        """
        Check whether the local PaliGemma model is already downloaded.
        JS calls this at boot to decide whether to show the download overlay.
        """
        gemma_exists = False
        p1 = os.path.join(_MODELS_DIR, "paligemma-3b-mix-224-text-model-q4_k_m.gguf")
        p2 = os.path.join(_MODELS_DIR, "paligemma-3b-mix-224-mmproj-f16.gguf")
        if os.path.isfile(p1) and os.path.isfile(p2):
            gemma_exists = True

        return {"exists": gemma_exists, "path": _MODELS_DIR, "gemma_exists": gemma_exists, "qwen_exists": True}

    def start_model_download(self) -> dict:
        """
        Begin downloading required model weights from Hugging Face
        into local directories. Runs in a daemon thread.
        JS should then poll get_download_progress() every 1.5 seconds.
        """
        if self._dl_state["running"]:
            return {"success": False, "message": "Download already in progress."}
        if self.get_model_status()["exists"]:
            return {"success": False, "message": "Models already downloaded."}

        self._dl_state.update({
            "running": True, "done": False,
            "percent": 0, "status": "Starting download...", "error": None,
        })

        def _download_thread():
            monitor_stop   = threading.Event()
            monitor_thread = None
            _dl_start      = time.time()

            try:
                from huggingface_hub import snapshot_download

                status = self.get_model_status()
                gemma_needed = not status.get("gemma_exists", False)
                qwen_needed = False

                gemma_size = 2_500_000_000 if gemma_needed else 0
                qwen_size = 0
                total_size = gemma_size + qwen_size

                # ── Progress monitor — dual track: dir-size + time-elapsed ──────
                def _monitor():
                    while not monitor_stop.is_set():
                        try:
                            elapsed = time.time() - _dl_start

                            # Track actual bytes written to disk
                            dir_bytes = 0
                            if gemma_needed and os.path.isdir(_MODELS_DIR):
                                dir_bytes += sum(
                                    os.path.getsize(os.path.join(r, fn))
                                    for r, _, files in os.walk(_MODELS_DIR)
                                    for fn in files
                                )
                            if qwen_needed and os.path.isdir(_QWEN_DIR):
                                dir_bytes += sum(
                                    os.path.getsize(os.path.join(r, fn))
                                    for r, _, files in os.walk(_QWEN_DIR)
                                    for fn in files
                                )

                            time_pct  = min(int(elapsed / 450 * 95), 95)
                            display_total = max(total_size, dir_bytes + (50 * 1024 * 1024))
                            size_pct  = min(int(dir_bytes * 100 / display_total), 95)
                            pct       = max(size_pct, time_pct)

                            self._dl_state["percent"] = pct

                            mb_done  = dir_bytes  / (1024 * 1024)
                            mb_total = display_total / (1024 * 1024)
                            if dir_bytes > 0:
                                self._dl_state["status"] = (
                                    f"Downloading... {mb_done:.0f} MB / {mb_total:.0f} MB"
                                )
                            else:
                                self._dl_state["status"] = (
                                    "Initializing download of intelligence modules... "
                                    f"({int(elapsed)}s elapsed)"
                                )
                        except Exception:
                            pass
                        time.sleep(1.5)

                monitor_thread = threading.Thread(target=_monitor, daemon=True)
                monitor_thread.start()

                if gemma_needed:
                    self._dl_state["status"] = "Downloading primary intelligence module (Gemma 4)..."
                    os.makedirs(_MODELS_DIR, exist_ok=True)
                    snapshot_download(
                        repo_id=_MODEL_REPO,
                        repo_type="model",
                        local_dir=_MODELS_DIR,
                        allow_patterns=["*text-model-q4_k_m.gguf", "*mmproj-f16.gguf"],
                    )

                # Qwen download is disabled to save space. We only use PaliGemma.
                pass

                monitor_stop.set()
                self._dl_state.update({
                    "running": False, "done": True,
                    "percent": 100, "status": "Download complete. Intelligence modules ready.",
                })

            except Exception as exc:
                monitor_stop.set() if 'monitor_stop' in dir() else None
                self._dl_state.update({
                    "running": False, "done": False,
                    "status": "Download failed.", "error": str(exc),
                })

        threading.Thread(target=_download_thread, daemon=True).start()
        return {"success": True, "message": "Download started."}

    def get_download_progress(self) -> dict:
        """Return the current model download state for JS polling."""
        return dict(self._dl_state)

    # ── Processing Pipeline ────────────────────────────────────────────────────

    def run_pipeline(self, image_paths: list, selected_platforms: list = None, edit_image: bool = False, image_style: str = None, num_edits: int = 2) -> dict:
        """
        Start the full processing pipeline for a list of image paths.
        Runs sequentially in a daemon thread. JS polls get_pipeline_status().

        Args:
            image_paths:        List of absolute image file paths to process.
            selected_platforms: List of platform names the user toggled ON
                                in the Broadcast Destinations UI (e.g.
                                ["instagram", "tiktok"]). Defaults to all
                                connected platforms if not provided.
            edit_image:         Whether to generate image editing prompts.
            image_style:        The creative style filter/vibe selected.
            num_edits:          Number of prompt variations to generate.

        For each image:
          1. Moondream vision scan  (pipeline.py) → vision_cb fires immediately
          2. Gemini SEO generation  (server_bridge.py)
          3. Omni-channel broadcast (executor.py)
        """
        if self._pipe_state["running"]:
            return {"success": False, "message": "Pipeline is already running."}
        if not image_paths:
            return {"success": False, "message": "No images provided."}

        # Fall back to all connected platforms if caller didn't specify any
        if not selected_platforms:
            selected_platforms = self.get_connected_platforms()

        self._pipe_state.update({
            "running":       True,
            "done":          False,
            "log":           [],
            "queue_status":  {p: "pending" for p in image_paths},
            "vision_data":   {},
            "edit_data":     {},
            "current_index": 0,
            "total":         len(image_paths),
            "error":         None,
            "results":       [],
        })

        def _run():
            try:
                from pipeline import process_image_pipeline
                from executor import broadcast_to_connected_socials
            except ImportError as imp_err:
                self._pipe_state.update({
                    "running": False, "done": True,
                    "error": f"Module import failed: {imp_err}",
                })
                return

            _start = time.time()

            def _status_cb(msg: str):
                elapsed = time.time() - _start
                ts = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
                self._pipe_state["log"].append({"ts": ts, "msg": msg})

            # Group image paths by their parent directory
            from collections import defaultdict
            groups = defaultdict(list)
            for path in image_paths:
                parent_dir = os.path.dirname(path)
                groups[parent_dir].append(path)
            
            # Sort the groups to maintain order
            sorted_groups = sorted(groups.items(), key=lambda x: x[0])
            self._pipe_state["total"] = len(sorted_groups)

            for idx, (parent_dir, group_images) in enumerate(sorted_groups):
                _start = time.time()   # reset timer per folder group
                self._pipe_state["current_index"] = idx

                folder_name = os.path.basename(parent_dir) or "Root"
                _status_cb(f"Processing product folder: '{folder_name}' ({len(group_images)} images)...")

                # Set status to scanning for all images in this folder group
                for img in group_images:
                    self._pipe_state["queue_status"][img] = "scanning"

                # Select primary image for AI vision scan
                primary_image = group_images[0]
                for img in group_images:
                    base = os.path.basename(img).lower()
                    if "cover" in base or "main" in base or "primary" in base:
                        primary_image = img
                        break

                def _make_vision_cb(images_to_update):
                    def _vision_cb(raw_text: str):
                        for img in images_to_update:
                            self._pipe_state["vision_data"][img] = raw_text
                    return _vision_cb

                pipe_result = process_image_pipeline(
                    primary_image,
                    _status_cb,
                    vision_cb=_make_vision_cb(group_images),
                    selected_platforms=selected_platforms,
                    edit_image=edit_image,
                    image_style=image_style,
                    num_edits=num_edits,
                )

                if not pipe_result["success"]:
                    for img in group_images:
                        self._pipe_state["queue_status"][img] = "failed"
                    _status_cb(f"Pipeline error for '{folder_name}': {pipe_result.get('error', 'Unknown')}")
                    continue

                if pipe_result.get("image_edit_prompt"):
                    for img in group_images:
                        self._pipe_state["edit_data"][img] = pipe_result["image_edit_prompt"]

                # ── Real Gemini AI-edited images (from pipeline Step 2.5) ─────────
                ai_edited_images = pipe_result.get("ai_edited_images", [])

                # Also apply Pillow style filter for the originals (quick pre-process)
                pillow_edited = []
                if edit_image and image_style:
                    _status_cb(f"Applying '{image_style}' Pillow pre-processing to originals...")
                    from pipeline import apply_visual_style_to_image
                    for img in group_images:
                        try:
                            pillow_path = apply_visual_style_to_image(img, image_style)
                            pillow_edited.append(pillow_path)
                        except Exception as e:
                            _status_cb(f"Warning: Pillow filter failed for {os.path.basename(img)} ({e})")
                            pillow_edited.append(img)
                else:
                    pillow_edited = list(group_images)

                # Build the final broadcast image list:
                # - num_edits=2 → original(s) + real Gemini AI images (carousel)
                # - num_edits=1 → Pillow-edited originals only
                # - edit off    → raw originals
                if edit_image and image_style and num_edits >= 2 and ai_edited_images:
                    # Carousel: originals first, then real Gemini AI-edited images
                    broadcast_images = list(group_images) + ai_edited_images
                    _status_cb(
                        f"Multi-edit carousel: {len(group_images)} original(s) + "
                        f"{len(ai_edited_images)} Gemini AI-generated image(s) ready."
                    )
                elif edit_image and image_style and num_edits >= 2 and not ai_edited_images:
                    # AI editing produced no output — fallback to Pillow edits as carousel
                    broadcast_images = list(group_images) + [
                        ep for ep in pillow_edited if ep not in group_images
                    ]
                    _status_cb(
                        f"AI editing unavailable \u2014 using Pillow-enhanced carousel "
                        f"({len(broadcast_images)} images)."
                    )
                else:
                    broadcast_images = pillow_edited

                for img in group_images:
                    self._pipe_state["queue_status"][img] = "posting"
                _status_cb("Preparing the stage for your global social media drop...")

                metadata = {
                    "title":    pipe_result.get("title", ""),
                    "caption":  next(iter(pipe_result.get("captions", {}).values())) if pipe_result.get("captions") else "",
                    "captions": pipe_result.get("captions", {}),
                    "hashtags": pipe_result.get("hashtags", ""),
                }
                broadcast_result = broadcast_to_connected_socials(
                    image_path=broadcast_images,
                    metadata_json=metadata,
                    status_cb=_status_cb,
                    selected_platforms=selected_platforms,
                )

                for img in group_images:
                    self._pipe_state["queue_status"][img] = "done"

                self._pipe_state["results"].append({
                    "folder":    parent_dir,
                    "pipeline":  pipe_result,
                    "broadcast": broadcast_result,
                })

            self._pipe_state.update({"running": False, "done": True})

        threading.Thread(target=_run, daemon=True).start()

        # Calculate number of groups to return consistent total
        from collections import defaultdict
        groups_count = defaultdict(list)
        for path in image_paths:
            groups_count[os.path.dirname(path)].append(path)
        return {"success": True, "total": len(groups_count)}

    def get_pipeline_status(self) -> dict:
        """Return current pipeline state snapshot for JS polling."""
        s = self._pipe_state
        return {
            "running":       s["running"],
            "done":          s["done"],
            "log":           list(s["log"]),
            "queue_status":  dict(s["queue_status"]),
            "vision_data":   dict(s["vision_data"]),
            "edit_data":     dict(s["edit_data"]),
            "current_index": s["current_index"],
            "total":         s["total"],
            "error":         s["error"],
        }

    def stop_pipeline(self) -> dict:
        """Reset pipeline state and stop llama-server."""
        self._pipe_state.update({"running": False, "done": True})
        try:
            from pipeline import stop_local_llama_server
            stop_local_llama_server()
        except Exception:
            pass
        return {"success": True}


# ─── Window Setup & Launch ────────────────────────────────────────────────────

def main():
    # ─── Clear Hugging Face Cache ─────────────────────────────────────────────
    import shutil
    cache_dir = os.path.expanduser("~/.cache/huggingface/modules/transformers_modules")
    if os.path.exists(cache_dir):
        try:
            shutil.rmtree(cache_dir)
            print("[MulaGo] Cleared Hugging Face modules cache.")
        except Exception as e:
            print(f"[MulaGo] Failed to clear HF cache: {e}")

    api = MulaGoApi()

    # ─── Background Model Warmup ──────────────────────────────────────────────
    def warmup_model():
        try:
            if api.get_model_status()["exists"]:
                print("[MulaGo] Initializing local llama-server in background...")
                from pipeline import start_local_llama_server
                start_local_llama_server()
                print("[MulaGo] Local llama-server is up and ready.")
        except Exception as e:
            print(f"[MulaGo] Background server start failed: {e}")

    threading.Thread(target=warmup_model, daemon=True).start()

    window = webview.create_window(
        title="MULA GO",
        url=_INDEX_HTML,
        js_api=api,
        width=1200,
        height=780,
        min_size=(960, 660),
        resizable=True,
        frameless=False,
        easy_drag=False,
        background_color="#F0F9FF",
        text_select=False,
    )

    try:
        webview.start(debug=True, http_server=True)
    finally:
        print("[MulaGo] Cleaning up local AI server...")
        try:
            from pipeline import stop_local_llama_server
            stop_local_llama_server()
        except Exception:
            pass


if __name__ == "__main__":
    main()

