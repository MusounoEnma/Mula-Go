"""
server_bridge.py — MULA GO Server Bridge
=========================================

Central abstraction layer for all AI / SEO generation requests.
NO other module should call an external AI service directly.

Architecture Modes
------------------
  MODE A (current): DIRECT
      Calls the Gemini API directly using the key below.
      Simple, no server needed.

  MODE B (future):  REMOTE
      Replace the body of `generate_seo()` with a single
      requests.post() to your own VPS/domain. The caller
      (pipeline.py) does not need to change at all.

To switch from MODE A → MODE B:
  1. Comment out the "── MODE A" block inside generate_seo().
  2. Uncomment the "── MODE B" block below it.
  3. Set REMOTE_ENDPOINT to your live URL.
  4. Remove GEMINI_API_KEY if you want (optional — it moves server-side).

Owner Notes
-----------
  - Only change this file when updating SEO generation logic.
  - pipeline.py and main.py are NOT aware of which mode is active.
  - The JSON schema returned by generate_seo() must always be:
      {"caption": str, "hashtags": str}
"""

import json
import logging

log = logging.getLogger("mula_go.server_bridge")

# ─── Configuration ─────────────────────────────────────────────────────────────

def load_env_key() -> str:
    """Load Gemini API key from local .env file securely."""
    import os
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(base_dir, ".env")
        if os.path.isfile(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line_clean = line.strip()
                    if line_clean and not line_clean.startswith("#") and "=" in line_clean:
                        k, v = line_clean.split("=", 1)
                        if k.strip() == "GEMINI_API_KEY":
                            return v.strip().strip('"').strip("'")
    except Exception as e:
        log.error(f"[ServerBridge] Failed to load .env file: {e}")
    return ""

# ── MODE A: Direct Gemini key (currently active) ───────────────────────────────
# To rotate the key, change only the GEMINI_API_KEY value inside the local `.env` file.
GEMINI_API_KEY         = load_env_key()
GEMINI_MODEL           = "gemini-flash-latest"
GEMINI_IMAGE_GEN_MODEL = "gemini-3.1-flash-image"   # Model that supports IMAGE response modality

# ── MODE B: Remote VPS endpoint (future) ──────────────────────────────────────
# Uncomment and fill in when your backend is live.
# REMOTE_ENDPOINT = "https://api.mulago.com/v1/generate_seo"
# REMOTE_SECRET   = "your-shared-hmac-secret-here"   # optional request signing

# ─── SEO Prompt Template ──────────────────────────────────────────────────────

_FALLBACK_RESULT = {
    "title":    "Product Spotlight",
    "hashtags": "product shop buy sale deal fashion lifestyle trending style quality",
    "captions": {
        "tiktok": "Discover this amazing product — quality you can see and feel! #shop",
        "instagram": "Discover this amazing product — quality you can see and feel.",
        "twitter": "Discover this amazing product — quality you can see and feel.",
        "ebay": "Detailed description of this premium quality product.",
        "amazon": "Premium quality product. Durable materials. Perfect for daily use.",
        "alibaba": "High-quality wholesale product. Bulk ordering options available."
    }
}


def _parse_json(text: str):
    text = text.strip()
    if "```json" in text:
        try:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        except IndexError:
            pass
    elif "```" in text:
        try:
            text = text.split("```", 1)[1].rsplit("```", 1)[0].strip()
        except IndexError:
            pass
    return json.loads(text, strict=False)


# ─── Public Interface ─────────────────────────────────────────────────────────

def generate_seo(raw_description: str, platforms: list = None) -> dict:
    """
    Convert a raw product description into structured SEO metadata customized per platform.

    Args:
        raw_description: Plain-text output from the local VLM vision scan.
        platforms: List of social/marketplace platform keys currently connected in the hub.

    Returns:
        dict with keys "title" (str), "hashtags" (str), and "captions" (dict).
        On any failure, returns a safe fallback so the pipeline never crashes.
    """
    if not raw_description or not raw_description.strip():
        log.warning("[ServerBridge] Empty description received — returning fallback.")
        return _FALLBACK_RESULT.copy()

    # Use default list if none provided
    if not platforms:
        platforms = ["tiktok", "instagram", "twitter"]

    # Normalize platforms to lowercase strings
    platforms = [str(p).strip().lower() for p in platforms if p]

    # Generate custom prompt instructions for the platforms
    platform_instructions = []
    for p in platforms:
        if p == "tiktok":
            platform_instructions.append("  - tiktok: Short, high-energy, casual tone with emojis and a strong call-to-action.")
        elif p == "instagram":
            platform_instructions.append("  - instagram: Aesthetic, clean, engaging caption focusing on lifestyle/visual appeal with emojis.")
        elif p == "twitter" or p == "x":
            platform_instructions.append("  - twitter: Conversational, punchy, very short caption (strict limit of 240 characters).")
        elif p == "ebay":
            platform_instructions.append("  - ebay: Structured, detailed product specs, materials, and retail appeal.")
        elif p == "amazon":
            platform_instructions.append("  - amazon: Bullet points highlighting key features, specifications, and buyer benefits.")
        elif p == "alibaba":
            platform_instructions.append("  - alibaba: Professional B2B wholesale description highlighting materials, supply details, and wholesale bulk order appeal.")
        else:
            platform_instructions.append(f"  - {p}: High-converting marketing copy suitable for this platform's specific target audience and formatting.")

    platforms_str = "\n".join(platform_instructions)

    # Construct the JSON captions schema block
    captions_schema_parts = []
    for p in platforms:
        captions_schema_parts.append(f'    "{p}": "<custom caption/description for {p}>"')
    captions_schema_str = ",\n".join(captions_schema_parts)

    prompt = f"""\
You are an expert e-commerce copywriter and SEO specialist.
Based on the product description below, generate:
  1. A short, catchy, SEO-optimized product title (max 50 characters).
  2. A space-separated list of 20 relevant, trending hashtags (no # symbol needed).
  3. A customized caption/description for each of the following platforms matching their specific vibe and style constraints:
{platforms_str}

Product description (from local AI scan):
\"\"\"{raw_description.strip()}\"\"\"

Respond ONLY with a valid JSON object in this exact format, no explanation:
{{
  "title": "<your product title here>",
  "hashtags": "<tag1 tag2 tag3 ... tag20>",
  "captions": {{
{captions_schema_str}
  }}
}}
"""

    # ── MODE A: Direct Gemini call via REST API (primary) & SDK (fallback) ─────
    raw_json = None
    try:
        import urllib.request
        
        # We use gemini-2.5-flash which is extremely fast and fully supported by the v1beta endpoint
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json"
            }
        }
        
        log.info("[ServerBridge] Attempting to call Gemini REST API directly (timeout=20s)...")
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            raw_json = resp_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            log.info("[ServerBridge] Direct Gemini REST API call successful.")

    except Exception as rest_err:
        log.warning(f"[ServerBridge] Direct REST API call failed: {rest_err}. Falling back to SDK...")
        try:
            import google.generativeai as genai  # pip install google-generativeai

            genai.configure(api_key=GEMINI_API_KEY)
            model  = genai.GenerativeModel(GEMINI_MODEL)

            response = model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.7,
                    max_output_tokens=1024,
                    response_mime_type="application/json",
                ),
            )
            raw_json = response.text.strip()
            log.info("[ServerBridge] SDK fallback call successful.")
        except Exception as sdk_err:
            log.error(f"[ServerBridge] Both REST API and SDK calls failed. SDK error: {sdk_err}")

    # Process and validate the raw_json if generated
    if raw_json:
        try:
            result = _parse_json(raw_json)

            # Validate required keys
            if "captions" not in result or "hashtags" not in result:
                raise ValueError("Response JSON missing required keys.")

            # Ensure title is present
            if "title" not in result:
                result["title"] = "Product Spotlight"

            # Normalise hashtags: ensure they start with '#'
            tags = result["hashtags"].strip()
            formatted_tags = " ".join(
                f"#{t.lstrip('#')}" for t in tags.split()
            )
            result["hashtags"] = formatted_tags

            # Ensure all requested platforms have a caption, otherwise fallback
            if not isinstance(result.get("captions"), dict):
                result["captions"] = {}
            for p in platforms:
                if p not in result["captions"]:
                    fallback = result.get("caption") or _FALLBACK_RESULT["captions"].get(p) or "Discover this amazing product — quality you can see and feel."
                    result["captions"][p] = fallback

            log.info("[ServerBridge] MODE A — SEO generated successfully via Gemini.")
            return result

        except json.JSONDecodeError as e:
            log.error(f"[ServerBridge] Gemini returned non-JSON: {e}. Raw response: {raw_json[:100]}")
        except Exception as e:
            log.error(f"[ServerBridge] Failed to parse/validate Gemini response: {e}")

    # Build a customized fallback dict with correct keys on any failure
    log.warning("[ServerBridge] Returning safe local fallback SEO result.")
    fallback_res = _FALLBACK_RESULT.copy()
    fallback_res["captions"] = {p: _FALLBACK_RESULT["captions"].get(p, "Discover this amazing product.") for p in platforms}
    return fallback_res

    # ── MODE B: Remote VPS call (uncomment to activate) ───────────────────────
    # try:
    #     import requests
    #
    #     payload  = {"raw_description": raw_description.strip()}
    #     headers  = {"X-Mula-Secret": REMOTE_SECRET, "Content-Type": "application/json"}
    #     response = requests.post(
    #         REMOTE_ENDPOINT,
    #         json=payload,
    #         headers=headers,
    #         timeout=30,
    #     )
    #     response.raise_for_status()
    #     result = response.json()
    #
    #     if "caption" not in result or "hashtags" not in result:
    #         raise ValueError("Remote response missing required keys.")
    #
    #     log.info("[ServerBridge] MODE B — SEO generated via remote VPS.")
    #     return result
    #
    # except Exception as e:
    #     log.error(f"[ServerBridge] Remote call failed: {e}")
    #     return _FALLBACK_RESULT.copy()


def generate_image_edit_prompt(raw_description: str, style: str, num_edits: int = 2) -> list:
    """
    Generate multiple image editing prompts (variations) based on product description and style preset.
    Prompt 1: Add a contextually appropriate human model using/consuming the product.
    Prompt 2: Studio HD enhancements with atmospheric lighting and particle effects.
    Returns:
        list of strings (prompts).
    """
    style = str(style).strip().lower()
    num_edits = max(1, min(2, int(num_edits)))

    style_guidelines = {
        "elegant": "Adjust colors to deep, rich tones with high contrast. Use soft, directional lighting with minimal shadows to create a sophisticated, premium luxury aesthetic. Add subtle floating golden light particles in the air.",
        "cheerful": "Brighten the overall image, increase color saturation, and introduce warm, sunny lighting to create a vibrant, positive, and cheerful atmosphere. Add subtle warm dust sparkles floating around.",
        "clear": "Enhance sharpness, remove background distractions/noise, adjust white balance for accurate color rendering, and maximize detail and clarity. Enhance macro surface textures.",
        "warm": "Shift the color temperature towards golden, amber, and warm tones. Soften harsh highlights to create a cozy, comfortable, and inviting vibe. Add soft rising steam or glowing amber dust particles.",
        "fresh": "Cool down the color temperature slightly, boost green and blue tones, and raise exposure to create a clean, crisp, and fresh look. Add tiny floating fresh water mist droplets."
    }

    global_guideline = style_guidelines.get(style, "Enhance colors, lighting, and clarity to showcase the product beautifully.")

    # Truncate raw description to 200 characters to conserve API token limit
    short_desc = raw_description.strip()[:200]

    prompt = f"""\
Generate {num_edits} image edit prompts in a JSON string array for: "{short_desc}"
Preset: {style.upper()} ({global_guideline})
Requirements:
1. Model: Add a suitable model wearing/using/consuming the product.
2. Studio: No humans. Focus on studio lighting, macro texture, and matching particles (e.g. steam, mist, sparks).
Format: ["prompt1", "prompt2"]
"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(GEMINI_MODEL)

        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.7,
                max_output_tokens=1024,
                response_mime_type="application/json",
            ),
        )
        raw_json = response.text.strip()
        prompts = _parse_json(raw_json)
        if isinstance(prompts, list):
            return [str(p).strip() for p in prompts][:num_edits]
    except Exception as e:
        raw_resp = locals().get("raw_json", "Not available")
        log.error(f"[ServerBridge] Failed to generate image edit prompts: {e}. Raw response: {raw_resp}")

    # Fallback values
    fallback_prompts = []
    if num_edits >= 1:
        fallback_prompts.append(f"A lifestyle photo of a professional model wearing or using this product in a suitable environment matching the {style} style.")
    if num_edits >= 2:
        fallback_prompts.append(f"Studio product photography with professional lighting, macro focus on details, and subtle floating particles matching the {style} guideline: {global_guideline}")
    return fallback_prompts


def generate_ai_edited_image(
    image_path: str,
    edit_prompt: str,
    output_suffix: str = "ai_edited",
) -> str | None:
    """
    Send a product image to Gemini with an edit instruction and retrieve
    the AI-generated result as a saved image file.

    Uses the google-genai SDK with response_modalities=["IMAGE"] so Gemini
    actually returns pixel data rather than just a text description.

    Args:
        image_path:    Absolute path to the source product image.
        edit_prompt:   Natural-language instruction for Gemini
                       (e.g. "Add a female model wearing this garment in a studio").
        output_suffix: Short label appended to the saved filename
                       (e.g. "model", "studio").

    Returns:
        Absolute path to the saved AI-edited image, or None on any failure.
    """
    import os

    if not os.path.exists(image_path):
        log.warning(f"[ServerBridge] Source image not found: {image_path}")
        return None

    # ── Resolve output directory ────────────────────────────────────────────
    import sys
    if getattr(sys, "frozen", False):
        _app_dir = os.path.dirname(sys.executable)
    else:
        _app_dir = os.path.dirname(os.path.abspath(__file__))

    temp_dir = os.path.join(_app_dir, "data", "sessions", "temp_edited")
    os.makedirs(temp_dir, exist_ok=True)

    filename = os.path.basename(image_path)
    name, _ext = os.path.splitext(filename)
    out_path = os.path.join(temp_dir, f"{output_suffix}_{name}.png")

    # ── Use google-genai SDK (required for IMAGE response modality) ─────────
    try:
        from google import genai as new_genai
        from google.genai import types as new_types
        from PIL import Image

        client = new_genai.Client(api_key=GEMINI_API_KEY)

        # Load image and convert to JPEG bytes for the API
        import io
        source_img = Image.open(image_path).convert("RGB")
        buf = io.BytesIO()
        source_img.save(buf, format="JPEG", quality=90)
        image_bytes = buf.getvalue()

        log.info(f"[ServerBridge] Sending image to Gemini ({GEMINI_IMAGE_GEN_MODEL}) for AI editing...")
        log.info(f"[ServerBridge] Prompt: {edit_prompt[:100]}...")

        response = client.models.generate_content(
            model=GEMINI_IMAGE_GEN_MODEL,
            contents=[
                new_types.Content(
                    role="user",
                    parts=[
                        new_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                        new_types.Part.from_text(text=edit_prompt),
                    ],
                )
            ],
            config=new_types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
            ),
        )

        # Extract image bytes from the response parts
        for part in response.candidates[0].content.parts:
            if hasattr(part, "inline_data") and part.inline_data:
                image_bytes = part.inline_data.data
                with open(out_path, "wb") as f:
                    f.write(image_bytes)
                log.info(f"[ServerBridge] AI-edited image saved: {out_path}")
                return out_path

        log.warning("[ServerBridge] Gemini response contained no image data in any part.")
        # Log text response if any for debugging
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                log.info(f"[ServerBridge] Gemini text reply (no image): {part.text[:200]}")

    except ImportError:
        log.error("[ServerBridge] google-genai SDK not installed. Run: python -m pip install google-genai")
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
            log.warning(
                "[ServerBridge] Gemini image generation quota exceeded — "
                "AI image editing requires an active Google Cloud billing account. "
                "Falling back to Pillow-based style enhancement. "
                "To enable AI editing: https://aistudio.google.com → Billing"
            )
        else:
            log.error(f"[ServerBridge] Gemini image generation failed: {e}")

    return None


def health_check() -> dict:
    """
    Quick sanity check — verifies the bridge can reach the AI service.
    Useful for debugging without running the full pipeline.

    Returns:
        {"ok": bool, "mode": "direct" | "remote", "model": str, "error": str | None}
    """
    try:
        import google.generativeai as genai

        genai.configure(api_key=GEMINI_API_KEY)
        model    = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content("Say 'ok' in one word.")
        return {
            "ok":    True,
            "mode":  "direct",
            "model": GEMINI_MODEL,
            "reply": response.text.strip(),
            "error": None,
        }
    except Exception as e:
        return {
            "ok":    False,
            "mode":  "direct",
            "model": GEMINI_MODEL,
            "error": str(e),
        }
