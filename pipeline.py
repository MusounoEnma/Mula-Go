"""
pipeline.py — MULA GO Processing Pipeline
==========================================

Orchestrates the two-step content processing loop for a single image:
  Step 1 — Local Vision  : Moondream2 analyses the product image and
                            extracts materials, colors, and style as raw text.
                            vision_cb() is fired immediately so the UI can
                            display the raw output before Gemini finishes.
  Step 2 — SEO Refiner   : server_bridge.generate_seo() sends that raw text
                            to Gemini 1.5 Flash and returns structured JSON.
  Cooldown               : time.sleep(5) after the Gemini call to respect
                            the free-tier rate limit.

The caller (main.py) runs process_image_pipeline() in a daemon thread and
passes a status_cb callable to stream live messages to the JS status feed,
and a vision_cb callable that receives the raw Moondream text early.

Model path: <app_dir>/models/moondream2/
The moondream package is used for inference (pip install moondream).
"""

import os
import sys

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
    print(f"[MulaGo] Failed to patch PathFinder in pipeline: {patch_err}")

import time
import logging
from typing import Callable, List, Optional

import urllib.request
import json
import re

from server_bridge import generate_seo

log = logging.getLogger("mula_go.pipeline")

# ─── Resolve app directory (works both as .py and PyInstaller .exe) ───────────
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    log_file = os.path.join(_APP_DIR, "debug_log.txt")
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger("mula_go").addHandler(file_handler)
    logging.getLogger("mula_go").setLevel(logging.INFO)
except Exception as le:
    print(f"Failed to setup file logging: {le}")

_MODELS_DIR  = os.path.join(_APP_DIR, "models", "gemma")

# ─── Portable llama-server Subprocess Management ──────────────────────────────
_llama_server_proc = None
_current_model_type = None

def get_total_ram_gb() -> float:
    try:
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys / (1024 ** 3)
    except Exception:
        pass
    try:
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        return 8.0


def start_local_llama_server(model_type: str = "gemma"):
    """
    Start the local llama-server.exe with the specified model type (gemma or qwen).
    If a different model type is already running, stops it first.
    """
    global _llama_server_proc, _current_model_type
    
    # 1. If server is already running, check if it has the correct model
    if _llama_server_proc is not None:
        if _current_model_type == model_type:
            try:
                req = urllib.request.Request("http://127.0.0.1:8080/health")
                with urllib.request.urlopen(req, timeout=1) as resp:
                    if resp.status == 200:
                        log.info(f"[Pipeline] Local llama-server is already running with {model_type} model.")
                        return
            except Exception:
                pass
        
        # Shut down current model server before running the requested one
        log.info(f"[Pipeline] Stopping current model server ({_current_model_type}) to load requested model ({model_type})...")
        stop_local_llama_server()

    # 2. Resolve paths for the chosen model
    server_exe = os.path.join(_APP_DIR, "models", "llama_server", "llama-server.exe")
    
    if model_type == "qwen":
        model_path = os.path.join(_APP_DIR, "models", "qwen", "Qwen2-VL-2B-Instruct-Q4_K_M.gguf")
        projector_path = os.path.join(_APP_DIR, "models", "qwen", "mmproj-Qwen2-VL-2B-Instruct-f16.gguf")
        model_desc = "Qwen 2 VL"
    else:
        model_path = os.path.join(_APP_DIR, "models", "gemma", "paligemma-3b-mix-224-text-model-q4_k_m.gguf")
        projector_path = os.path.join(_APP_DIR, "models", "gemma", "paligemma-3b-mix-224-mmproj-f16.gguf")
        model_desc = "PaliGemma 3B"

    if not os.path.exists(server_exe):
        raise FileNotFoundError(f"llama-server.exe not found at: {server_exe}")
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"{model_desc} model not found at: {model_path}")
    if not os.path.exists(projector_path):
        raise FileNotFoundError(f"{model_desc} projector not found at: {projector_path}")

    # 3. Start subprocess
    log.info(f"[Pipeline] Starting local llama-server for {model_desc} in the background...")
    cmd = [
        server_exe,
        "-m", model_path,
        "--mmproj", projector_path,
        "--port", "8080",
        "--host", "127.0.0.1",
        "-c", "3072",
        "-np", "1",           # Limit to 1 slot to reduce RAM consumption and avoid swap thrashing
        "-t", "4",            # Use all 4 cores on low-end AMD CPU
        "--embedding",
        "-fit", "off",         # Disable memory-fitting abort check to allow paging/swap memory usage
        "--no-warmup",         # Skip slow empty run warmup at startup to speed up launch
        "--reasoning", "off"   # Disable slow thinking CoT to optimize coordinate detection speed
    ]

    stdout_log = os.path.join(_APP_DIR, "llama_server_stdout.log")
    stderr_log = os.path.join(_APP_DIR, "llama_server_stderr.log")

    # Clean existing logs
    for path in (stdout_log, stderr_log):
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    f_out = open(stdout_log, "w", encoding="utf-8")
    f_err = open(stderr_log, "w", encoding="utf-8")

    import subprocess
    # Run server process (hide console window on Windows if frozen)
    creationflags = 0x08000000 if getattr(sys, "frozen", False) else 0
    _llama_server_proc = subprocess.Popen(
        cmd, 
        stdout=f_out, 
        stderr=f_err, 
        text=True, 
        creationflags=creationflags
    )

    # Wait for server to bind and respond
    server_ready = False
    for i in range(180):  # 180 seconds timeout
        if _llama_server_proc.poll() is not None:
            break
        try:
            req = urllib.request.Request("http://127.0.0.1:8080/health")
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    log.info(f"[Pipeline] Local llama-server started and ready with {model_desc} on http://127.0.0.1:8080")
                    server_ready = True
                    break
        except Exception as check_err:
            log.warning(f"[Pipeline] Server check failed: {check_err}")
        time.sleep(1)

    f_out.close()
    f_err.close()

    if not server_ready:
        stop_local_llama_server()
        raise RuntimeError(f"Failed to start local llama-server for {model_desc} in 180 seconds.")
    
    _current_model_type = model_type


def stop_local_llama_server():
    """Terminate the local llama-server process if active."""
    global _llama_server_proc, _current_model_type
    if _llama_server_proc is not None:
        log.info("[Pipeline] Terminating local llama-server...")
        try:
            _llama_server_proc.terminate()
            _llama_server_proc.wait(timeout=5)
        except Exception:
            try:
                _llama_server_proc.kill()
            except Exception:
                pass
        _llama_server_proc = None
        _current_model_type = None
        log.info("[Pipeline] Local llama-server terminated.")



def analyze_image_vision(image_path: str) -> str:
    """
    Pass a product image to the local Gemma/VLM GGUF model via local llama-server API.
    Falls back to Gemini API Vision Scan on any failure.

    Args:
        image_path: Absolute path to the image file.

    Returns:
        Raw text description extracted from the image.
    """
    if not os.path.exists(image_path):
        log.warning(f"[Pipeline] Image not found: {image_path}")
        return "Product image could not be read."

    # ── Attempt Local Gemma/VLM GGUF Scan via HTTP ─────────────────────────
    try:
        start_local_llama_server("gemma")
        
        from PIL import Image
        import io
        import base64

        with Image.open(image_path) as img:
            # Resize product image to a max of 384x384 to avoid huge visual token counts and memory overhead on slow CPU
            img.thumbnail((384, 384), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            img.convert("RGB").save(buffer, format="JPEG", quality=85)
            base64_data = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
        data_uri = f"data:image/jpeg;base64,{base64_data}"

        prompt = (
            "Describe this product in detail. Cover its materials, primary and accent colors, "
            "style (e.g. minimalist, luxury, streetwear), target audience, and any standout "
            "visual features that would appeal to online buyers."
        )

        payload = {
            "model": "gemma",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}}
                    ]
                }
            ],
            "temperature": 0.2
        }

        log.info("[Pipeline] Sending product description request to local llama-server...")
        req = urllib.request.Request(
            "http://127.0.0.1:8080/v1/chat/completions",
            data=json.dumps(payload).encode('utf-8'),
            headers={"Content-Type": "application/json"}
        )
        
        # 300 seconds timeout for processing
        with urllib.request.urlopen(req, timeout=300) as resp:
            res_data = json.loads(resp.read().decode('utf-8'))
            result = res_data["choices"][0]["message"]["content"].strip()
            log.info("[Pipeline] Local llama-server product description successful.")
            return str(result).strip()

    except Exception as e:
        log.warning(f"[Pipeline] Local llama-server vision scan failed: {e}. Generating rich fallback description from filename and parent folder to protect image privacy...")

    # ── Safe Local Fallback: Extract keywords from folder and filename (No image upload to Gemini) ──
    filename = os.path.basename(image_path)
    parent_dir = os.path.dirname(image_path)
    category = os.path.basename(parent_dir).strip().lower() if parent_dir else ""
    
    name_without_ext = os.path.splitext(filename)[0]
    clean_name = re.sub(r'[-_]+', ' ', name_without_ext).strip()

    # Pre-defined descriptive templates for common e-commerce categories in English (highly detailed)
    category_templates = {
        "sepatu": "premium quality footwear with a modern, trendsetting athletic design. Features a highly cushioned, shock-absorbing inner sole and a durable, flexible rubber outer sole with a specialized anti-slip tread pattern for maximum grip. Constructed using premium breathable mesh fabric and lightweight synthetic overlays with reinforced stitching. Engineered for ultimate comfort, ventilation, and foot support, making it perfect for athletic activities, casual daily wear, and long walks.",
        "shoes": "premium quality footwear with a modern, trendsetting athletic design. Features a highly cushioned, shock-absorbing inner sole and a durable, flexible rubber outer sole with a specialized anti-slip tread pattern for maximum grip. Constructed using premium breathable mesh fabric and lightweight synthetic overlays with reinforced stitching. Engineered for ultimate comfort, ventilation, and foot support, making it perfect for athletic activities, casual daily wear, and long walks.",
        "sneakers": "stylish and versatile sneakers featuring a low-top silhouette with a cushioned collar and tongue for enhanced ankle support. Built with a responsive foam midsole and a high-abrasion rubber outsole that delivers superior grip on urban surfaces. The upper is made of a breathable mesh and premium suede overlay combination, accented by clean lines and modern design details. Ideal for streetwear, active lifestyles, and daily comfort.",
        "baju": "fashion-forward apparel item crafted from premium 100% organic cotton/linen blend. Extremely soft to the touch, lightweight, and highly breathable with excellent sweat-absorbing properties to ensure all-day comfort. Features a meticulously tailored fit (regular/slim) with clean, reinforced stitching. Ideal for a wide range of settings from casual streetwear and daily lounging to semi-formal social gatherings.",
        "pakaian": "fashion-forward apparel item crafted from premium 100% organic cotton/linen blend. Extremely soft to the touch, lightweight, and highly breathable with excellent sweat-absorbing properties to ensure all-day comfort. Features a meticulously tailored fit (regular/slim) with clean, reinforced stitching. Ideal for a wide range of settings from casual streetwear and daily lounging to semi-formal social gatherings.",
        "kaos": "casual everyday essential t-shirt crafted from premium 100% combed ringspun cotton for an ultra-soft feel and durability. Features a classic crew neck design, short sleeves, and shoulder-to-shoulder taping for structural integrity. The fabric is highly breathable, sweat-wicking, and pre-shrunk to retain its shape after washing. A versatile wardrobe staple perfect for layered styling or casual solo wear.",
        "tshirt": "casual everyday essential t-shirt crafted from premium 100% combed ringspun cotton for an ultra-soft feel and durability. Features a classic crew neck design, short sleeves, and shoulder-to-shoulder taping for structural integrity. The fabric is highly breathable, sweat-wicking, and pre-shrunk to retain its shape after washing. A versatile wardrobe staple perfect for layered styling or casual solo wear.",
        "kemeja": "elegant and crisp button-down shirt constructed from a premium long-staple cotton and polyester blend that offers a smart, wrinkle-resistant finish. Features a neat, structured collar, buttoned cuffs, and a tailored fit that provides a smart, professional silhouette. Perfect for formal business environments, academic presentations, or upscale social occasions.",
        "shirt": "elegant and crisp button-down shirt constructed from a premium long-staple cotton and polyester blend that offers a smart, wrinkle-resistant finish. Features a neat, structured collar, buttoned cuffs, and a tailored fit that provides a smart, professional silhouette. Perfect for formal business environments, academic presentations, or upscale social occasions.",
        "tas": "a highly versatile and stylish bag designed for modern daily utility. Constructed from premium heavy-duty, water-resistant canvas/nylon with durable reinforced zippers and metallic buckles. Features a spacious main compartment with a padded laptop sleeve, multiple exterior quick-access pockets, and ergonomic, padded shoulder straps designed to reduce fatigue. Perfect for students, professionals, travelers, and daily commuters.",
        "bag": "a highly versatile and stylish bag designed for modern daily utility. Constructed from premium heavy-duty, water-resistant canvas/nylon with durable reinforced zippers and metallic buckles. Features a spacious main compartment with a padded laptop sleeve, multiple exterior quick-access pockets, and ergonomic, padded shoulder straps designed to reduce fatigue. Perfect for students, professionals, travelers, and daily commuters.",
        "backpack": "ergonomic high-capacity backpack built with water-repellent ballistic nylon fabric. Includes a dedicated padded compartment that secures up to a 15.6-inch laptop, a spacious main storage area for books or travel gear, and multiple zippered utility pockets. Designed with breathable mesh back panels and adjustable, contoured shoulder straps for maximum comfort during long commutes or outdoor travels.",
        "jam": "exquisite luxury timepiece showcasing a minimalist yet sophisticated analog dial design. Driven by a high-precision Japanese quartz movement housed in a premium rust-resistant stainless steel casing. Complemented by a durable, comfortable genuine leather or stainless steel link strap with a secure folding clasp. Splash-resistant and perfect for elevating casual, business, or formal outfits.",
        "watch": "exquisite luxury timepiece showcasing a minimalist yet sophisticated analog dial design. Driven by a high-precision Japanese quartz movement housed in a premium rust-resistant stainless steel casing. Complemented by a durable, comfortable genuine leather or stainless steel link strap with a secure folding clasp. Splash-resistant and perfect for elevating casual, business, or formal outfits.",
        "hijab": "graceful modest wear item designed for elegant draping and maximum comfort. Made from high-quality, lightweight, and breathable material (such as premium chiffon or silk voile) that drapes beautifully and resists wrinkling. Gentle on the skin and non-sheer, providing complete coverage and a neat look for everyday wear, religious events, or formal occasions.",
        "jilbab": "graceful modest wear item designed for elegant draping and maximum comfort. Made from high-quality, lightweight, and breathable material (such as premium chiffon or silk voile) that drapes beautifully and resists wrinkling. Gentle on the skin and non-sheer, providing complete coverage and a neat look for everyday wear, religious events, or formal occasions.",
        "kerudung": "graceful modest wear item designed for elegant draping and maximum comfort. Made from high-quality, lightweight, and breathable material (such as premium chiffon or silk voile) that drapes beautifully and resists wrinkling. Gentle on the skin and non-sheer, providing complete coverage and a neat look for everyday wear, religious events, or formal occasions.",
        "kosmetik": "high-performance beauty and skincare product formulated with gentle, skin-nourishing ingredients. Dermatologically tested and safe for all skin types, including sensitive skin. Delivers a flawless, natural-looking finish with a long-lasting, smudge-proof formulation that keeps the skin hydrated, glowing, and protected throughout the day.",
        "makeup": "high-performance beauty and skincare product formulated with gentle, skin-nourishing ingredients. Dermatologically tested and safe for all skin types, including sensitive skin. Delivers a flawless, natural-looking finish with a long-lasting, smudge-proof formulation that keeps the skin hydrated, glowing, and protected throughout the day.",
        "skincare": "high-performance beauty and skincare product formulated with gentle, skin-nourishing ingredients. Dermatologically tested and safe for all skin types, including sensitive skin. Delivers a flawless, natural-looking finish with a long-lasting, smudge-proof formulation that keeps the skin hydrated, glowing, and protected throughout the day.",
        "lipstik": "long-wearing matte lip cream offering rich, high-intensity pigmentation in a single swipe. Formulated with hydrating natural oils to prevent dryness and lip peeling, providing a lightweight, comfortable feel all day. Features a transfer-proof, smudge-resistant finish that keeps lips looking bold and beautiful.",
        "aksesoris": "fashionable and elegant accessory designed with fine craftsmanship. Made from high-quality, non-tarnish materials that retain their luster and resist rusting. Features intricate detailing and a modern aesthetic, serving as the perfect statement piece to elevate any outfit and showcase your unique sense of style.",
        "accessory": "fashionable and elegant accessory designed with fine craftsmanship. Made from high-quality, non-tarnish materials that retain their luster and resist rusting. Features intricate detailing and a modern aesthetic, serving as the perfect statement piece to elevate any outfit and showcase your unique sense of style.",
        "kuliner": "delectable gourmet culinary product prepared under strict hygiene standards using only premium, fresh, and natural ingredients. Free from artificial colorings, preservatives, or chemical additives. Packaged securely in food-grade wrapping to lock in absolute freshness, authentic texture, and rich aroma. Perfect for sharing or self-indulgence.",
        "makanan": "delectable gourmet culinary product prepared under strict hygiene standards using only premium, fresh, and natural ingredients. Free from artificial colorings, preservatives, or chemical additives. Packaged securely in food-grade wrapping to lock in absolute freshness, authentic texture, and rich aroma. Perfect for sharing or self-indulgence.",
        "food": "delectable gourmet culinary product prepared under strict hygiene standards using only premium, fresh, and natural ingredients. Free from artificial colorings, preservatives, or chemical additives. Packaged securely in food-grade wrapping to lock in absolute freshness, authentic texture, and rich aroma. Perfect for sharing or self-indulgence.",
        "elektronik": "advanced tech electronic gadget or accessory engineered for high efficiency, speed, and reliable performance. Built with premium, heat-resistant housing materials and a sleek, compact design. Equipped with smart surge protection and energy-saving technology, making it the perfect tool to support a modern digital lifestyle.",
        "gadget": "advanced tech electronic gadget or accessory engineered for high efficiency, speed, and reliable performance. Built with premium, heat-resistant housing materials and a sleek, compact design. Equipped with smart surge protection and energy-saving technology, making it the perfect tool to support a modern digital lifestyle.",
        "casing": "ultra-durable shockproof phone case designed with precision cuts for ports, cameras, and buttons. Made from a hybrid combination of flexible TPU bumper edges and a hard polycarbonate back plate to absorb high-impact drops. Features a scratch-resistant, anti-yellowing coating while maintaining a slim and stylish profile.",
        "rumah": "minimalist home organization or decor accessory crafted from eco-friendly, premium-grade durable wood or metal. Designed to maximize storage efficiency and add a clean, modern aesthetic to your living room, kitchen, or office space. Easy to assemble, highly stable, and built for long-term daily usage.",
        "home": "minimalist home organization or decor accessory crafted from eco-friendly, premium-grade durable wood or metal. Designed to maximize storage efficiency and add a clean, modern aesthetic to your living room, kitchen, or office space. Easy to assemble, highly stable, and built for long-term daily usage.",
    }

    matched_desc = ""
    # Try parent directory matching first
    for kw, desc in category_templates.items():
        if category and kw in category:
            matched_desc = desc
            break

    # If category matches nothing, try matching the product name keywords
    if not matched_desc:
        for kw, desc in category_templates.items():
            if kw in clean_name.lower():
                matched_desc = desc
                break

    # If still no match, generate a premium e-commerce descriptor
    if not matched_desc:
        matched_desc = "a premium-grade product designed with a minimalist aesthetic, utilizing high-quality materials to ensure excellent durability, structural integrity, and versatile functionality suitable for modern daily lifestyles and retail appeal."

    fallback_desc = (
        f"Product Name: '{clean_name}'.\n"
        f"Category/Folder: '{category.title() if category else 'General'}'.\n"
        f"Visual Description: {matched_desc}\n\n"
        "Please generate professional, highly persuasive e-commerce marketing copy, "
        "engaging style keywords, and trending hashtags in Indonesian or English based on this data "
        "to maximize online search engine visibility (SEO)."
    )
    
    log.info(f"[Pipeline] Rich local text fallback generated for category '{category}': '{fallback_desc}'")
    return fallback_desc



def process_image_pipeline(
    image_path:         str,
    status_cb:          Optional[Callable[[str], None]] = None,
    vision_cb:          Optional[Callable[[str], None]] = None,
    selected_platforms: Optional[List[str]]             = None,
    edit_image:         bool                            = False,
    image_style:        Optional[str]                   = None,
    num_edits:          int                             = 2,
) -> dict:
    """
    Full two-step pipeline for a single product image.

    Steps:
      1. Moondream local vision → raw description text
         → vision_cb(vision_raw) fired HERE so the UI updates immediately
      2. server_bridge.generate_seo() → {caption, hashtags}
      3. time.sleep(5) cooldown for Gemini rate-limit compliance

    Args:
        image_path:         Absolute OS path to the product image.
        status_cb:          Optional callable — receives status strings for
                            the UI live feed.
        vision_cb:          Optional callable — receives the raw Moondream
                            text immediately after vision scan, before Gemini
                            starts. Signature: vision_cb(raw_text: str).
        selected_platforms: List of platform names (e.g. ["instagram",
                            "tiktok"]) the user toggled ON in the UI.
                            Passed through to the result for the executor.
        edit_image:         Whether to generate image editing instructions.
        image_style:        The style to guide the image editing instructions.
        num_edits:          Number of prompt variations to generate.

    Returns:
        {
          "image_path":          str,
          "vision_raw":          str,    # raw Moondream output
          "caption":             str,    # Gemini-refined caption
          "hashtags":            str,    # Gemini-refined hashtags
          "image_edit_prompt":   list,   # list of generated image edit instructions
          "selected_platforms":  list,   # platforms to broadcast to
          "success":             bool,
          "error":               str | None,
        }
    """
    def _cb(msg: str):
        if status_cb:
            status_cb(msg)
        log.info(f"[Pipeline] {msg}")

    result = {
        "image_path":         image_path,
        "vision_raw":         "",
        "title":              "",
        "captions":           {},
        "hashtags":           "",
        "image_edit_prompt":  [],
        "ai_edited_images":   [],   # Paths to real Gemini-generated image edits
        "selected_platforms": selected_platforms or [],
        "success":            False,
        "error":              None,
    }

    try:
        # ── Step 1: Local Vision Scan ─────────────────────────────────────────
        _cb("Taking a closer look at your product's unique features...")
        vision_raw            = analyze_image_vision(image_path)
        result["vision_raw"] = vision_raw

        # ── Fire vision callback IMMEDIATELY so the UI can render it ──────────
        # This happens BEFORE the slow Gemini call so the user sees what
        # Moondream "saw" in real-time while SEO generation runs.
        if vision_cb:
            try:
                vision_cb(vision_raw)
            except Exception as vcb_err:
                log.warning(f"[Pipeline] vision_cb raised: {vcb_err}")

        # ── Emit raw Moondream output to the live status feed for debugging ────
        # The frontend watches for the __VISION_DEBUG__: prefix to inject the
        # text into the queue card's "🐛 DEBUG: Moondream Vision Output" box.
        # Remove this _cb call before shipping the production release.
        _cb(f"__VISION_DEBUG__:{vision_raw}")

        # ── Step 2: SEO Refinement via Server Bridge ──────────────────────────
        _cb("Crafting the perfect sales copy to engage your audience...")
        seo_data           = generate_seo(vision_raw, selected_platforms)
        result["title"]    = seo_data.get("title", "")
        result["captions"] = seo_data.get("captions", {})
        result["hashtags"] = seo_data.get("hashtags", "")

        # ── Step 2.5: AI Image Generation via Gemini (Optional) ──────────────────
        if edit_image and image_style:
            _cb(f"Generating image editing instructions with '{image_style}' style...")
            from server_bridge import generate_image_edit_prompt, generate_ai_edited_image

            edit_prompts = generate_image_edit_prompt(vision_raw, image_style, num_edits=num_edits)
            result["image_edit_prompt"] = edit_prompts

            # Tampilkan peringatan privasi pihak ketiga di feed status UI
            _cb(f"⚠️ PERINGATAN PRIVASI: Mengirim foto produk ke Gemini AI untuk proses pengeditan gaya '{image_style}'. Gambar akan diunggah ke pihak ketiga.")

            if num_edits >= 2 and edit_prompts:
                ai_images = []
                for i, ep in enumerate(edit_prompts):
                    suffix_labels = ["model", "studio"]
                    suffix = suffix_labels[i] if i < len(suffix_labels) else f"edit{i+1}"
                    _cb(f"Mengirim foto ke Gemini AI untuk '{suffix}' edit ({i+1}/{len(edit_prompts)})...")
                    ai_path = generate_ai_edited_image(
                        image_path,
                        ep,
                        output_suffix=f"{suffix}_{image_style}",
                    )
                    if ai_path:
                        ai_images.append(ai_path)
                        _cb(f"Gemini AI '{suffix}' edit complete.")
                    else:
                        _cb(
                            f"Gemini AI image edit tidak tersedia. Menggunakan filter lokal Pillow '{image_style}' sebagai cadangan..."
                        )
                        try:
                            local_path = apply_visual_style_to_image(image_path, image_style)
                            if local_path:
                                ai_images.append(local_path)
                        except Exception:
                            pass
                result["ai_edited_images"] = ai_images
            elif num_edits == 1 and edit_prompts:
                _cb("Mengirim foto ke Gemini AI untuk single edit...")
                ai_path = generate_ai_edited_image(
                    image_path,
                    edit_prompts[0],
                    output_suffix=f"edit_{image_style}",
                )
                if ai_path:
                    result["ai_edited_images"] = [ai_path]
                    _cb("Gemini AI edit complete.")
                else:
                    _cb(
                        "Gemini AI image edit tidak tersedia. Menggunakan filter lokal Pillow sebagai cadangan..."
                    )
                    try:
                        local_path = apply_visual_style_to_image(image_path, image_style)
                        if local_path:
                            result["ai_edited_images"] = [local_path]
                    except Exception:
                        pass

        # ── Cooldown: respect Gemini free-tier rate limit ────────────────────────
        # (5-second pause before the pipeline processes the next image)
        time.sleep(5)

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        log.error(f"[Pipeline] Unhandled pipeline error for '{image_path}': {e}")

    return result


def find_element_coordinates_visual(screenshot_path: str, element_description: str) -> Optional[tuple]:
    """
    Use local Gemma 4 VLM (via local llama-server) to locate an element in a screenshot
    and return its (x, y) coordinates.
    NO cloud fallback is used for browser-driving automation actions.
    Assumes standard 1280x800 browser resolution.
    """
    import base64
    
    # ── Ensure Local llama-server is Running ──────────────────────────────
    try:
        start_local_llama_server("gemma")
    except Exception as e:
        log.error(f"[Pipeline] Failed to ensure local llama-server is running: {e}")
        return None

    # ── Query Local llama-server for Coordinates ───────────────────────────
    reply = ""
    try:
        from PIL import Image
        import io
        
        with Image.open(screenshot_path) as img:
            orig_w, orig_h = img.size
            resized_img = img.resize((1280, 800), Image.Resampling.LANCZOS)
            
            # Save resized image to in-memory bytes buffer
            buffer = io.BytesIO()
            resized_img.save(buffer, format="JPEG")
            base64_data = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
        data_uri = f"data:image/jpeg;base64,{base64_data}"

        prompt = (
            f"Locate the element '{element_description}' on this web page. "
            f"Estimate its center coordinates on a 1280x800 viewport resolution. "
            f"Respond ONLY with a valid JSON object in this format: {{\"x\": <int>, \"y\": <int>}}"
        )

        payload = {
            "model": "gemma",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}}
                    ]
                }
            ],
            "temperature": 0.1
        }

        log.info(f"[Pipeline] Sending visual grounding query to local llama-server for '{element_description}'...")
        req = urllib.request.Request(
            "http://127.0.0.1:8080/v1/chat/completions",
            data=json.dumps(payload).encode('utf-8'),
            headers={"Content-Type": "application/json"}
        )
        
        # 300 seconds timeout for processing
        with urllib.request.urlopen(req, timeout=300) as resp:
            res_data = json.loads(resp.read().decode('utf-8'))
            reply = res_data["choices"][0]["message"]["content"].strip()
            log.info(f"[Pipeline] Raw VLM response received: {reply}")
            
            # Clean markdown formatting wraps
            if reply.startswith("```"):
                reply = re.sub(r"```[a-zA-Z]*", "", reply).strip()
                reply = reply.strip("`").strip()

            data = None
            # Parse Attempt 1: Direct JSON parse
            try:
                data = json.loads(reply)
            except Exception:
                pass

            # Parse Attempt 2: Clean single quotes to double quotes
            if data is None:
                try:
                    cleaned_reply = reply.replace("'", '"')
                    data = json.loads(cleaned_reply)
                except Exception:
                    pass

            # Parse Attempt 3: Regex match for x, y coordinates as fallback
            if data is None:
                try:
                    x_match = re.search(r'["\'\s]*x["\'\s]*[:=]\s*(\d+)', reply, re.IGNORECASE)
                    y_match = re.search(r'["\'\s]*y["\'\s]*[:=]\s*(\d+)', reply, re.IGNORECASE)
                    if x_match and y_match:
                        data = {"x": int(x_match.group(1)), "y": int(y_match.group(1))}
                except Exception:
                    pass

            if data is not None:
                x = int(data.get("x"))
                y = int(data.get("y"))
                
                if 0 <= x <= 1280 and 0 <= y <= 800:
                    scale_x = orig_w / 1280.0
                    scale_y = orig_h / 800.0
                    real_x = int(x * scale_x)
                    real_y = int(y * scale_y)
                    log.info(f"[Pipeline] Local Qwen VLM visual grounding successful for '{element_description}': ({x}, {y}) scaled to original dimensions ({real_x}, {real_y})")
                    return (real_x, real_y)
                else:
                    log.warning(f"[Pipeline] Local Qwen VLM returned coordinates out of bounds: ({x}, {y})")
            else:
                log.warning(f"[Pipeline] Failed to parse coordinates from raw VLM output.")
    except Exception as e:
        log.error(f"[Pipeline] Local Qwen VLM visual grounding failed for '{element_description}': {e}. Raw VLM reply: '{reply}'")

    return None


def find_element_coordinates_yolo(screenshot_path: str, element_description: str, page=None) -> Optional[tuple]:
    """
    Use local YOLOv8 UI field detection model (foduucom/web-form-ui-field-detection)
    to locate UI elements in the screenshot.
    Uses browser interaction (document.elementFromPoint) to query the actual text inside detected boxes
    to match the 'element_description' (like "Next", "Not Now", "Close", "Share").
    """
    import os
    import re
    import json
    import logging
    from huggingface_hub import hf_hub_download
    from ultralytics import YOLO
    from PIL import Image

    log = logging.getLogger("mula_go.pipeline")
    log.info(f"[YOLO] Finding element '{element_description}' in screenshot '{screenshot_path}'...")

    if not os.path.exists(screenshot_path):
        log.warning(f"[YOLO] Screenshot path does not exist: {screenshot_path}")
        return None

    # Load model
    try:
        model_path = hf_hub_download(repo_id="foduucom/web-form-ui-field-detection", filename="best.pt")
        model = YOLO(model_path)
    except Exception as e:
        log.error(f"[YOLO] Failed to load YOLO model: {e}")
        return None

    # Predict
    results = model.predict(screenshot_path, conf=0.15, verbose=False)
    if not results or len(results) == 0:
        log.warning("[YOLO] No detections found.")
        return None

    boxes = results[0].boxes
    if not boxes or len(boxes) == 0:
        log.warning("[YOLO] No bounding boxes detected.")
        return None

    # Determine screenshot dimensions and scale
    with Image.open(screenshot_path) as img:
        img_w, img_h = img.size

    # Viewport size from browser
    viewport_w, viewport_h = 1280, 800
    try:
        if page:
            viewport = page.viewport_size
            if viewport:
                viewport_w = viewport["width"]
                viewport_h = viewport["height"]
            else:
                dims = page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
                viewport_w = dims["w"]
                viewport_h = dims["h"]
    except Exception as e:
        log.warning(f"[YOLO] Failed to get page viewport size: {e}")

    scale_x = img_w / float(viewport_w)
    scale_y = img_h / float(viewport_h)

    candidates = []
    
    # Pre-parse element_description keywords
    desc_clean = element_description.lower()
    keywords = re.findall(r'\b\w+\b', desc_clean)

    # For each box, scale center coordinate to viewport dimensions
    for box in boxes:
        cls_id = int(box.cls[0].item())
        conf = float(box.conf[0].item())
        xyxy = box.xyxy[0].tolist() # x1, y1, x2, y2 on the image
        
        # Center in image coordinates
        img_cx = (xyxy[0] + xyxy[2]) / 2.0
        img_cy = (xyxy[1] + xyxy[3]) / 2.0
        
        # Center in viewport coordinates
        vp_cx = img_cx / scale_x
        vp_cy = img_cy / scale_y
        
        candidates.append({
            "cls_id": cls_id,
            "conf": conf,
            "img_cx": img_cx,
            "img_cy": img_cy,
            "vp_cx": vp_cx,
            "vp_cy": vp_cy,
            "width": (xyxy[2] - xyxy[0]) / scale_x,
            "height": (xyxy[3] - xyxy[1]) / scale_y
        })

    log.info(f"[YOLO] Found {len(candidates)} UI element candidates on the page.")

    matched_candidate = None
    best_score = -1.0

    if page and candidates:
        payload = [{"x": c["vp_cx"], "y": c["vp_cy"]} for c in candidates]
        try:
            js_code = """
            (points) => {
                return points.map(pt => {
                    const el = document.elementFromPoint(pt.x, pt.y);
                    if (!el) return null;
                    
                    const getInfo = (node) => {
                        if (!node) return {};
                        return {
                            text: (node.innerText || node.textContent || node.value || "").trim(),
                            ariaLabel: node.getAttribute("aria-label") || "",
                            placeholder: node.getAttribute("placeholder") || "",
                            id: node.id || "",
                            className: node.className || "",
                            tagName: node.tagName.toLowerCase(),
                            role: node.getAttribute("role") || "",
                            title: node.getAttribute("title") || ""
                        };
                    };
                    
                    const info = getInfo(el);
                    
                    let parentInfo = {};
                    if (el.parentElement) {
                        const p = el.parentElement;
                        const pTagName = p.tagName.toLowerCase();
                        const pRole = p.getAttribute("role") || "";
                        if (pTagName === "button" || pTagName === "a" || pRole === "button") {
                            parentInfo = getInfo(p);
                        } else {
                            parentInfo = {
                                text: "",
                                ariaLabel: p.getAttribute("aria-label") || "",
                                id: p.id || "",
                                className: p.className || ""
                            };
                        }
                    }
                    
                    return {
                        target: info,
                        parent: parentInfo
                    };
                });
            }
            """
            dom_infos = page.evaluate(js_code, payload)
            
            for idx, dom_info in enumerate(dom_infos):
                if not dom_info:
                    continue
                
                c = candidates[idx]
                target = dom_info["target"]
                parent = dom_info["parent"]
                
                all_text = " ".join([
                    target.get("text", ""),
                    target.get("ariaLabel", ""),
                    target.get("placeholder", ""),
                    target.get("id", ""),
                    target.get("className", ""),
                    target.get("role", ""),
                    target.get("title", ""),
                    parent.get("text", ""),
                    parent.get("ariaLabel", ""),
                    parent.get("id", ""),
                    parent.get("className", "")
                ]).lower()
                
                score = 0.0
                match_count = 0
                for kw in keywords:
                    if kw in all_text:
                        score += 1.0
                        match_count += 1
                        direct_text = (target.get("text", "") + " " + target.get("ariaLabel", "")).lower()
                        if kw in direct_text:
                            score += 0.5
                
                # Boost score if class matches button (cls_id == 4) and we are looking for a button
                if c["cls_id"] == 4:
                    score += 0.1
                
                # Boost based on confidence
                score += c["conf"] * 0.05
                
                if match_count > 0 and score > best_score:
                    best_score = score
                    matched_candidate = c
                    
                log.debug(f"[YOLO] Cand {idx} (cls:{c['cls_id']}): txt='{target.get('text')[:30]}', score={score:.2f}")
                
        except Exception as eval_err:
            log.error(f"[YOLO] Error evaluating DOM at coordinates: {eval_err}")

    if matched_candidate:
        real_x = int(matched_candidate["vp_cx"])
        real_y = int(matched_candidate["vp_cy"])
        log.info(f"[YOLO] Successfully matched '{element_description}' at viewport coords ({real_x}, {real_y}) [img: ({int(matched_candidate['img_cx'])}, {int(matched_candidate['img_cy'])})], conf: {matched_candidate['conf']:.2f}")
        return (real_x, real_y)

    # Fallback 2: Direct DOM text selector matching via Playwright
    if page:
        log.info(f"[YOLO] Fallback 2: YOLO did not detect elements matching keywords. Querying Playwright DOM for text...")
        try:
            for kw in keywords:
                if len(kw) < 2 or kw in ["button", "overlay", "or", "and", "the", "of"]:
                    continue
                locators = [
                    page.locator(f"button:has-text('{kw}')"),
                    page.locator(f"a:has-text('{kw}')"),
                    page.locator(f"[role='button']:has-text('{kw}')"),
                    page.locator(f"text='{kw}'")
                ]
                for loc in locators:
                    try:
                        all_locs = loc.all()
                        for l in all_locs:
                            if l.is_visible():
                                box = l.bounding_box()
                                if box:
                                    vp_cx = box["x"] + box["width"] / 2.0
                                    vp_cy = box["y"] + box["height"] / 2.0
                                    real_x = int(vp_cx)
                                    real_y = int(vp_cy)
                                    log.info(f"[YOLO] Fallback 2 successfully found element containing '{kw}' in DOM at viewport ({real_x}, {real_y})")
                                    return (real_x, real_y)
                    except Exception:
                        pass
        except Exception as dom_err:
            log.warning(f"[YOLO] DOM text fallback failed: {dom_err}")

    # Fallback 3: Last resort — select top-right button candidate as close button
    if candidates and any(k in desc_clean for k in ["close", "x", "dismiss", "not now"]):
        log.warning(f"[YOLO] Fallback 3: Selecting top-right button candidate for '{element_description}'...")
        tr_cand = None
        max_tr_score = -99999
        for c in candidates:
            if c["cls_id"] == 4: # Button
                tr_score = c["vp_cx"] - c["vp_cy"]
                if tr_score > max_tr_score:
                    max_tr_score = tr_score
                    tr_cand = c
        if tr_cand:
            real_x = int(tr_cand["vp_cx"])
            real_y = int(tr_cand["vp_cy"])
            log.info(f"[YOLO] Selected top-right button candidate at viewport coords ({real_x}, {real_y}) [img: ({int(tr_cand['img_cx'])}, {int(tr_cand['img_cy'])})], conf: {tr_cand['conf']:.2f}")
            return (real_x, real_y)

    log.warning(f"[YOLO] Could not find matched coordinates for '{element_description}'.")
    return None


def apply_visual_style_to_image(image_path: str, style: str) -> str:
    """
    Apply a visual enhancement style to a product image using Pillow.
    Saves the edited image as a temporary file and returns its path.
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    from PIL import Image, ImageEnhance, ImageFilter
    
    style = str(style).strip().lower()
    
    # Create temp directory for edited images
    temp_dir = os.path.join(_APP_DIR, "data", "sessions", "temp_edited")
    os.makedirs(temp_dir, exist_ok=True)
    
    # Open the image
    img = Image.open(image_path).convert("RGB")
    
    if style == "cheerful":
        # Brighten and boost saturation
        img = ImageEnhance.Brightness(img).enhance(1.2)
        img = ImageEnhance.Color(img).enhance(1.3)
        # Shift slightly warmer (increase red/green channel values)
        r, g, b = img.split()
        r = r.point(lambda i: min(255, int(i * 1.05)))
        g = g.point(lambda i: min(255, int(i * 1.02)))
        img = Image.merge("RGB", (r, g, b))
        
    elif style == "warm":
        # Soften highlights and shift warmer
        img = ImageEnhance.Brightness(img).enhance(1.05)
        img = ImageEnhance.Contrast(img).enhance(0.95)
        r, g, b = img.split()
        r = r.point(lambda i: min(255, int(i * 1.08)))
        g = g.point(lambda i: min(255, int(i * 1.03)))
        b = b.point(lambda i: int(i * 0.95))
        img = Image.merge("RGB", (r, g, b))
        
    elif style == "fresh":
        # Cool down and brighten
        img = ImageEnhance.Brightness(img).enhance(1.15)
        img = ImageEnhance.Color(img).enhance(1.1)
        r, g, b = img.split()
        r = r.point(lambda i: int(i * 0.95))
        g = g.point(lambda i: min(255, int(i * 1.05)))
        b = b.point(lambda i: min(255, int(i * 1.08)))
        img = Image.merge("RGB", (r, g, b))
        
    elif style == "clear":
        # Enhance contrast, sharpness
        img = ImageEnhance.Contrast(img).enhance(1.2)
        img = img.filter(ImageFilter.SHARPEN)
        
    elif style == "elegant":
        # Deeper colors, high contrast, slightly desaturated, moody feel
        img = ImageEnhance.Brightness(img).enhance(0.92)
        img = ImageEnhance.Contrast(img).enhance(1.25)
        img = ImageEnhance.Color(img).enhance(0.85)
        
    # Save the modified image in the temp directory
    filename = os.path.basename(image_path)
    name, ext = os.path.splitext(filename)
    new_filename = f"edited_{style}_{name}{ext}"
    dest_path = os.path.join(temp_dir, new_filename)
    
    img.save(dest_path, quality=95)
    log.info(f"[Pipeline] Applied '{style}' style enhancement: {image_path} -> {dest_path}")
    return dest_path
