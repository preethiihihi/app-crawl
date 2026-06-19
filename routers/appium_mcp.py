import asyncio
import logging
import httpx
import json
import re
import os
import hashlib
from datetime import datetime
from typing import Optional, Union
from fastapi import APIRouter, File, UploadFile, Form
from pydantic import BaseModel
from appium_service import AppiumMcpClient, ensure_appium_server

logger = logging.getLogger("appium_mcp_router")

# Load .env file if it exists in the current workspace directory
if os.path.exists(".env"):
    try:
        with open(".env", "r", encoding="utf-8") as env_file:
            for line in env_file:
                clean_line = line.strip()
                if clean_line and not clean_line.startswith("#") and "=" in clean_line:
                    key, val = clean_line.split("=", 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")
    except Exception as e:
        logger.warning(f"Failed to load .env file: {e}")

router = APIRouter()

class PromptRequest(BaseModel):
    prompt: str

class RoboCrawlRequest(BaseModel):
    deviceName: str = "Android Emulator"
    udid: str = "emulator-5554"
    appPackage: str = "com.android.settings"
    appActivity: str = "com.android.settings.Settings"
    deviceType: str = "local emulator"

    model_config = {"extra": "allow"}


class CrawlStepRequest(BaseModel):
    reset_state: bool = False
    app_package: Optional[str] = None
    app_activity: Optional[str] = None
    prefill_data: Optional[dict] = None
    max_steps: int = 1

    model_config = {"extra": "allow"}


class GenerateScriptRequest(BaseModel):
    app_map: dict
    test_steps: str
    deviceName: str = "emulator-5554"
    appPackage: str = "com.kuberproject"
    appActivity: str = ".MainActivity"
    appiumUrl: str = "http://127.0.0.1:4723"

    model_config = {"extra": "allow"}


class ExecuteScriptRequest(BaseModel):
    script: str
    app_map: Optional[Union[dict, str]] = None
    test_steps: Optional[str] = None
    deviceName: str = "emulator-5554"
    appPackage: str = "com.kuberproject"
    appActivity: str = ".MainActivity"
    appiumUrl: str = "http://127.0.0.1:4723"

    model_config = {"extra": "allow"}


class RepairScriptRequest(BaseModel):
    app_map: Union[dict, str]
    test_steps: str
    script: str
    error_message: str
    deviceName: str = "emulator-5554"
    appPackage: str = "com.kuberproject"
    appActivity: str = ".MainActivity"
    appiumUrl: str = "http://127.0.0.1:4723"
    current_screen_elements: Optional[list] = None

    model_config = {"extra": "allow"}



def compress_app_map_for_llm(app_map: Optional[Union[dict, str]], query_text: Optional[str] = None) -> str:
    """Simplifies the app map to the minimal required keys, filters nodes by query_text, and serializes compactly."""
    if not app_map:
        return "{}"
    
    # If it is a string representing a file path, or serialized json, try parsing it
    if isinstance(app_map, str):
        try:
            if os.path.exists(app_map):
                with open(app_map, 'r', encoding='utf-8') as f:
                    app_map = json.load(f)
            else:
                app_map = json.loads(app_map)
        except Exception:
            return app_map

    if not isinstance(app_map, dict):
        return str(app_map)

    nodes = app_map.get("nodes", [])
    
    # Identify which nodes match the query text
    matched_node_ids = set()
    if query_text and nodes:
        query_lower = str(query_text).lower()
        clean_query = query_lower.replace(" ", "").replace("_", "")
        for node in nodes:
            node_id = str(node.get("node_id", "")).lower()
            screen_name = str(node.get("screen_name", "")).lower()
            clean_screen = screen_name.replace(" ", "").replace("_", "")
            if (node_id in query_lower or 
                screen_name in query_lower or 
                clean_screen in clean_query):
                matched_node_ids.add(node.get("node_id"))
                
    # If no nodes matched (e.g. general script generating or names didn't align),
    # keep only the Home screen elements or the first screen elements to prevent token blowup.
    if not matched_node_ids and nodes and len(nodes) > 2:
        home_node = None
        for node in nodes:
            if str(node.get("screen_type", "")).lower() == "home":
                home_node = node
                break
        if home_node:
            matched_node_ids.add(home_node.get("node_id"))
        else:
            matched_node_ids.add(nodes[0].get("node_id"))

    compressed_nodes = []
    for node in nodes:
        # Only process if the screen matches the query or falls under the default subset.
        # This prevents sending details of 20+ unrelated screens, which blows up token sizes.
        is_relevant = (not query_text) or (node.get("node_id") in matched_node_ids) or (len(nodes) <= 2)
        if not is_relevant:
            continue
            
        node_copy = {
            "node_id": node.get("node_id"),
            "screen_name": node.get("screen_name"),
            "screen_type": node.get("screen_type"),
            "elements": []
        }
        
        for el in node.get("elements", []):
            # Only keep elements that are clickable, or input fields, or have selectors
            is_interactable = el.get("clickable") or "EditText" in el.get("element_type", "") or el.get("selectors")
            if not is_interactable:
                continue
                
            el_copy = {
                "element_id": el.get("element_id"),
                "element_type": el.get("element_type"),
                "clickable": el.get("clickable")
            }
            if el.get("text"):
                el_copy["text"] = el.get("text")
            if el.get("content_desc"):
                el_copy["content_desc"] = el.get("content_desc")
            if el.get("selectors"):
                sels = el.get("selectors", {})
                el_copy["selectors"] = {k: v for k, v in sels.items() if k != "uiautomator"}
            node_copy["elements"].append(el_copy)
            
        compressed_nodes.append(node_copy)
    
    compact_map = {
        "app_name": app_map.get("app_name", "App"),
        "package": app_map.get("package"),
        "nodes": compressed_nodes
    }
    return json.dumps(compact_map, separators=(',', ':'))


def compress_elements_for_llm(elements: Optional[list]) -> str:
    """Simplifies layout elements to the minimal required keys and serializes compactly."""
    if not elements:
        return "[]"
    compressed = []
    for el in elements:
        el_copy = {
            "element_id": el.get("element_id"),
            "element_type": el.get("element_type"),
            "bounds": el.get("bounds"),
            "clickable": el.get("clickable")
        }
        if el.get("text"):
            el_copy["text"] = el.get("text")
        if el.get("content_desc"):
            el_copy["content_desc"] = el.get("content_desc")
        if el.get("selectors"):
            sels = el.get("selectors", {})
            el_copy["selectors"] = {k: v for k, v in sels.items() if k != "uiautomator"}
        if el.get("selector"):
            el_copy["selector"] = el.get("selector")
        compressed.append(el_copy)
    return json.dumps(compressed, separators=(',', ':'))


def compress_visual_elements_for_llm(visual_elements: Optional[list]) -> str:
    """Simplifies visual OCR elements to the minimal required keys and serializes compactly."""
    if not visual_elements:
        return "[]"
    compressed = []
    for el in visual_elements:
        compressed.append({
            "id": el.get("visual_id"),
            "text": el.get("text"),
            "center": el.get("center")
        })
    return json.dumps(compressed, separators=(',', ':'))


async def call_groq_generate(prompt: str, groq_key: str, format_json: bool = False) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {groq_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1
    }
    if format_json:
        payload["response_format"] = {"type": "json_object"}
        
    model_name = "llama-3.3-70b-versatile"
    max_attempts = 3
    
    for attempt in range(1, max_attempts + 1):
        try:
            payload["model"] = model_name
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, headers=headers, timeout=60.0)
                if response.status_code == 200:
                    resp_json = response.json()
                    return resp_json["choices"][0]["message"]["content"].strip()
                elif response.status_code == 429 or "rate_limit" in response.text:
                    if "tokens per day" in response.text or "TPD" in response.text or "daily" in response.text:
                        if model_name == "llama-3.3-70b-versatile" and attempt < max_attempts:
                            logger.warning("Groq Daily Token Limit (TPD) reached for llama-3.3-70b-versatile. Switching to llama-3.1-8b-instant...")
                            model_name = "llama-3.1-8b-instant"
                            await asyncio.sleep(1.5)
                            continue
                    if attempt < max_attempts:
                        logger.warning(f"Groq Rate Limit hit (attempt {attempt}/{max_attempts}). Retrying in 7 seconds...")
                        await asyncio.sleep(7.0)
                        continue
                    else:
                        raise Exception(f"Groq rate limit exceeded: {response.text}")
                elif response.status_code in (413, 400) or "too large" in response.text:
                    if model_name == "llama-3.3-70b-versatile" and attempt < max_attempts:
                        logger.warning(f"Groq request size/error on {model_name}. Switching to llama-3.1-8b-instant...")
                        model_name = "llama-3.1-8b-instant"
                        await asyncio.sleep(1.5)
                        continue
                    else:
                        raise Exception(f"Groq API token/limit error {response.status_code}: {response.text}")
                else:
                    raise Exception(f"Groq API error {response.status_code}: {response.text}")
        except Exception as e:
            if attempt == max_attempts:
                raise e
            logger.warning(f"Groq attempt {attempt} failed: {e}. Retrying in 2 seconds...")
            await asyncio.sleep(2.0)


async def call_gemini_generate(prompt: str, gemini_key: str, format_json: bool = False) -> str:
    model = "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1
        }
    }
    if format_json:
        payload["generationConfig"]["responseMimeType"] = "application/json"
        
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=60.0)
                if response.status_code == 200:
                    resp_data = response.json()
                    return resp_data["candidates"][0]["content"]["parts"][0]["text"].strip()
                elif response.status_code == 429:
                    if attempt < max_attempts:
                        logger.warning(f"Gemini Rate Limit (429) hit. Retrying in 5 seconds (attempt {attempt}/{max_attempts})...")
                        await asyncio.sleep(5.0)
                        continue
                    else:
                        raise Exception("Gemini rate limit exceeded after maximum retries.")
                else:
                    raise Exception(f"Gemini API error {response.status_code}: {response.text}")
        except Exception as e:
            if attempt == max_attempts:
                raise e
            logger.warning(f"Gemini call failed on attempt {attempt}: {e}. Retrying in 2 seconds...")
            await asyncio.sleep(2.0)


async def call_ollama_vision(prompt: str, image_path: Optional[str], format_json: bool = False) -> str:
    """Queries the local Ollama vision model (llava) with a text prompt and screenshot image."""
    import base64
    import os
    
    url = "http://127.0.0.1:11434/api/generate"
    
    img_b64 = ""
    abs_image_path = None
    if image_path:
        abs_image_path = image_path
        if not os.path.isabs(image_path):
            abs_image_path = os.path.join(os.getcwd(), image_path)
            
        if os.path.exists(abs_image_path):
            try:
                with open(abs_image_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode("utf-8")
            except Exception as e:
                logger.warning(f"Failed to read image at {abs_image_path}: {e}")
                
    # Print JSON and Image details to the console/terminal as requested by user
    print("\n=================== OLLAMA VISION REQUEST ===================")
    print(f"MODEL: llava:latest")
    print(f"IMAGE PATH: {abs_image_path or 'N/A'}")
    if img_b64:
        print(f"IMAGE SIZE (Base64 length): {len(img_b64)} characters")
    else:
        print("IMAGE SIZE: N/A (no image provided or failed to load)")
    print("PROMPT TEXT (containing JSON elements):")
    print(prompt)
    print("=============================================================\n")
    
    payload = {
        "model": "llava",
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": 65536,
            "temperature": 0.1
        }
    }
    if img_b64:
        payload["images"] = [img_b64]
        
    if format_json:
        payload["format"] = "json"
        
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, timeout=300.0)
        if response.status_code == 200:
            resp_text = response.json().get("response", "").strip()
            print("\n=================== OLLAMA VISION RESPONSE ===================")
            print(resp_text)
            print("==============================================================\n")
            return resp_text
        else:
            raise Exception(f"Ollama vision model returned error: {response.status_code} - {response.text}")


async def call_ollama_generate(prompt: str, format_json: bool = False) -> str:
    url = "http://127.0.0.1:11434/api/generate"
    model_name = os.environ.get("OLLAMA_MODEL", "llama3.2")
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": 16384,
            "temperature": 0.1
        }
    }
    if format_json:
        payload["format"] = "json"
        
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, timeout=300.0)
        if response.status_code == 200:
            return response.json().get("response", "").strip()
        else:
            raise Exception(f"Ollama returned error: {response.status_code} - {response.text}")


async def call_llm_generate(prompt: str, format_json: bool = False) -> str:
    """Helper to route LLM calls. If PREFER_OLLAMA is 'true' (default), attempts local Ollama first,
    otherwise checks for Groq and Gemini before falling back.
    """
    prefer_ollama = os.environ.get("PREFER_OLLAMA", "true").lower() == "true"
    model_name = os.environ.get("OLLAMA_MODEL", "llama3.2")
    
    if prefer_ollama:
        try:
            logger.info(f"Attempting generation using local Ollama ({model_name})...")
            return await call_ollama_generate(prompt, format_json)
        except Exception as e:
            logger.warning(f"Local Ollama failed: {e}. Falling back to other providers...")
            
    groq_key = os.environ.get("GROQ_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    if groq_key:
        try:
            logger.info("Attempting generation using Groq API...")
            return await call_groq_generate(prompt, groq_key, format_json)
        except Exception as e:
            logger.warning(f"Groq API failed completely: {e}. Falling back to next provider...")
            
    if gemini_key:
        try:
            logger.info("Attempting generation using Gemini API...")
            return await call_gemini_generate(prompt, gemini_key, format_json)
        except Exception as e:
            logger.warning(f"Gemini API failed: {e}. Falling back to Ollama...")

    # Final fallback if Ollama wasn't tried first
    if not prefer_ollama:
        try:
            logger.info(f"Attempting generation using local Ollama ({model_name})...")
            return await call_ollama_generate(prompt, format_json)
        except Exception as e:
            logger.error(f"Local Ollama fallback failed: {e}")
        
    raise Exception("All configured LLM providers failed.")


@router.post("/generate")
async def generate_text(request: PromptRequest):
    try:
        resp = await call_llm_generate(request.prompt)
        return {"response": resp}
    except Exception as e:
        return {"status": "error", "message": str(e)}



def get_screen_fingerprint(xml_text: str) -> str:
    """Generates an MD5 fingerprint representing only the structural blueprint of the screen."""
    if not xml_text:
        return "EMPTY"
        
    try:
        import xml.etree.ElementTree as ET
        import hashlib
        
        # Parse XML
        root = ET.fromstring(xml_text.strip().encode("utf-8"))
        
        # Helper to recursively clean nodes
        def clean_node(node):
            # 1. Remove dynamic attributes that change during selection/interaction
            attrs_to_remove = [
                "focused", "selected", "checked", "bounds", 
                "index", "instance", "selection-start", "selection-end",
                "displayed", "password-visible", "showing-hint"
            ]
            for attr in attrs_to_remove:
                if attr in node.attrib:
                    del node.attrib[attr]
            
            # 2. Clear text and content-desc of ALL nodes to make sure
            # typed values, greetings, clocks, and dynamic text don't change the fingerprint.
            node.attrib["text"] = ""
            if "content-desc" in node.attrib:
                node.attrib["content-desc"] = ""
                    
            # 3. Filter out keyboard/inputmethod nodes from children
            children_to_keep = []
            for child in list(node):
                pkg = child.attrib.get("package", "")
                if "inputmethod" in pkg.lower() or "keyboard" in pkg.lower():
                    continue
                clean_node(child)
                children_to_keep.append(child)
                
            node[:] = children_to_keep

        clean_node(root)
        
        cleaned_xml_bytes = ET.tostring(root, encoding="utf-8")
        return hashlib.md5(cleaned_xml_bytes).hexdigest()
        
    except Exception as e:
        import hashlib
        cleaned = xml_text
        cleaned = re.sub(r'\btext=["\'][^"\']*["\']', 'text=""', cleaned)
        cleaned = re.sub(r'\bcontent-desc=["\'][^"\']*["\']', 'content-desc=""', cleaned)
        cleaned = re.sub(r'\b(focused|selected|checked|bounds|selection-start|selection-end|showing-hint)=["\'][^"\']*["\']', '', cleaned)
        return hashlib.md5(cleaned.encode("utf-8")).hexdigest()

async def classify_screen_with_ai(elements: list, default_name: str) -> tuple[str, str]:
    """Queries Ollama to get user-friendly screen_name and screen_type dynamically, falling back to heuristics."""
    if not elements:
        return default_name, "General"
    
    clean_elements = []
    for el in elements[:15]:
        clean_elements.append({
            "text": el.get("text", ""),
            "desc": el.get("content_desc", ""),
            "class": el.get("class", "").split('.')[-1]
        })
        
    prompt = (
        f"We are crawling an Android application.\n"
        f"We just navigated to a screen. Technical fallback name: '{default_name}'.\n"
        f"Here are prominent elements visible on this screen layout:\n"
        f"{json.dumps(clean_elements, indent=2)}\n\n"
        f"Please categorize this screen and return a JSON object matching this schema:\n"
        f"{{\n"
        f"  \"screen_name\": \"User-friendly name (e.g., 'Dashboard', 'Login', 'Network Settings')\",\n"
        f"  \"screen_type\": \"One of: 'Home', 'Settings', 'Form', 'List', 'Detail', 'Dialog', or 'General'\"\n"
        f"}}\n"
        f"Respond ONLY with this JSON block, no markdown wraps, no extra text."
    )
    
    try:
        raw_response = await call_llm_generate(prompt, format_json=True)
        res_json = json.loads(raw_response)
        s_name = res_json.get("screen_name")
        s_type = res_json.get("screen_type")
        if s_name and s_type:
            return s_name, s_type
    except Exception as e:
        logger.warning(f"AI screen classification failed: {e}. Falling back to heuristics.")

    # Heuristic fallback if AI fails
    header_text = ""
    for el in elements[:5]:
        val = el.get("text") or el.get("content_desc")
        if val and len(val.strip()) < 30:
            header_text = val.strip()
            break

    screen_name = header_text or default_name
    screen_type = "General"
    if header_text:
        lower_name = header_text.lower()
        if any(kw in lower_name for kw in ["setting", "preference"]):
            screen_type = "Settings"
        elif any(kw in lower_name for kw in ["dashboard", "home", "main"]):
            screen_type = "Home"
        elif any(kw in lower_name for kw in ["login", "sign", "auth", "register", "verify"]):
            screen_type = "Form"
        elif any(kw in lower_name for kw in ["list", "search", "directory", "highlight"]):
            screen_type = "List"
        elif any(kw in lower_name for kw in ["detail", "info", "profile"]):
            screen_type = "Detail"
            
    return screen_name, screen_type

def filter_xml_to_elements(xml_text: str) -> list:
    """Parses layout XML and extracts only text elements, buttons, and text input fields."""
    if not xml_text:
        return []

    def is_allowed(class_name: str, text: str, content_desc: str, hint: str = "") -> bool:
        class_lower = (class_name or "").lower()
        text_clean = (text or "").strip()
        desc_clean = (content_desc or "").strip()
        hint_clean = (hint or "").strip()
        
        # 1. Text category: has non-empty text, content description, or hint placeholder
        if text_clean or desc_clean or hint_clean:
            return True
            
        # 2. Button category: class name matches typical button/control types
        button_keywords = ["button", "checkbox", "radio", "switch", "toggle"]
        if any(kw in class_lower for kw in button_keywords):
            return True
            
        # 3. Input category: class name matches typical input types
        input_keywords = ["edittext", "input", "search"]
        if any(kw in class_lower for kw in input_keywords):
            return True
            
        return False

    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text.strip().encode("utf-8"))
    except Exception:
        # Regex fallback parser
        import re
        node_pattern = re.compile(r'<([a-zA-Z0-9_\.\-]+)\b([^>]*)/?>', re.DOTALL)
        attr_pattern = re.compile(r'(\S+)\s*=\s*["\'](.*?)["\']', re.DOTALL)
        elements = []
        for index, match in enumerate(node_pattern.finditer(xml_text)):
            tag = match.group(1)
            if tag.lower() in ("?xml", "!doctype", "hierarchy"):
                continue
            attrs = {k: v for k, v in attr_pattern.findall(match.group(2))}
            
            clickable = attrs.get("clickable", "").strip().lower() == "true"
            focusable = attrs.get("focusable", "").strip().lower() == "true"
            resource_id = attrs.get("resource-id", "").strip()
            text = attrs.get("text", "").strip()
            content_desc = attrs.get("content-desc", "").strip()
            hint = attrs.get("hint", "").strip()
            class_name = attrs.get("class", tag).strip()
            bounds = attrs.get("bounds", "").strip()
            
            if not is_allowed(class_name, text, content_desc, hint):
                continue
                
            is_input = "EditText" in class_name or class_name.endswith("EditText")
            
            if text:
                best_selector = f"xpath=//{class_name}[@text='{text}']"
            elif content_desc:
                best_selector = f"~{content_desc}"
            elif resource_id:
                best_selector = f"id={resource_id}"
            else:
                best_selector = f"xpath=//{class_name}[@bounds='{bounds}']"
            
            elements.append({
                "class": class_name,
                "resource_id": resource_id,
                "text": text,
                "content_desc": content_desc,
                "hint": hint,
                "clickable": clickable or focusable,
                "is_input": is_input,
                "bounds": bounds,
                "selector": best_selector
            })
        return elements

    elements = []
    for node in root.iter():
        tag_name = node.tag
        if tag_name.lower() in ("?xml", "!doctype", "hierarchy"):
            continue
        attrs = node.attrib
        clickable = attrs.get("clickable", "").strip().lower() == "true"
        focusable = attrs.get("focusable", "").strip().lower() == "true"
        resource_id = attrs.get("resource-id", "").strip()
        text = attrs.get("text", "").strip()
        content_desc = attrs.get("content-desc", "").strip()
        hint = attrs.get("hint", "").strip()
        class_name = attrs.get("class", "").strip() or tag_name
        bounds = attrs.get("bounds", "").strip()
        
        if not is_allowed(class_name, text, content_desc, hint):
            continue
            
        is_input = "EditText" in class_name or class_name.endswith("EditText")
        
        tag = class_name if class_name else "*"
        if text:
            if '"' in text:
                best_selector = f"xpath=//{tag}[@text='{text}']"
            else:
                best_selector = f'xpath=//{tag}[@text="{text}"]'
        elif content_desc:
            best_selector = f"~{content_desc}"
        elif resource_id:
            best_selector = f"id={resource_id}"
        else:
            best_selector = f"xpath=//{tag}[@bounds='{bounds}']"
            
        elements.append({
            "class": class_name,
            "resource_id": resource_id,
            "text": text,
            "content_desc": content_desc,
            "hint": hint,
            "clickable": clickable or focusable,
            "is_input": is_input,
            "bounds": bounds,
            "selector": best_selector
        })
    return elements

def get_screen_dimensions(xml_text: str) -> tuple:
    """Helper to parse screen width and height from hierarchy XML."""
    width, height = 1080, 2160
    if not xml_text:
        return width, height
    hierarchy_match = re.search(r'<hierarchy[^>]*\bwidth=["\'](\d+)["\']\s+height=["\'](\d+)["\']', xml_text)
    if hierarchy_match:
        return int(hierarchy_match.group(1)), int(hierarchy_match.group(2))
    bounds_match = re.findall(r'bounds=["\']\[0,0\]\[(\d+),(\d+)\]["\']', xml_text)
    if bounds_match:
        return int(bounds_match[0][0]), int(bounds_match[0][1])
    return width, height

def get_bounds_center_ratio(bounds_str: str, screen_width: int, screen_height: int) -> dict:
    """Calculates x/y ratios of bounds center relative to screen size."""
    if not bounds_str:
        return None
    match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
    if match:
        x1, y1, x2, y2 = map(int, match.groups())
        center_x = (x1 + x2) / 2
        center_y = (y1 + y2) / 2
        return {
            "tap_by_ratio": {
                "x_ratio": round(center_x / screen_width, 4) if screen_width else 0.0,
                "y_ratio": round(center_y / screen_height, 4) if screen_height else 0.0
            }
        }
    return None

def clean_element_type(class_name: str) -> str:
    """Map class name to type (Button, EditText, etc)."""
    if not class_name:
        return "Element"
    short_class = class_name.split('.')[-1]
    if short_class == "EditText":
        return "EditText"
    elif short_class == "Button":
        return "Button"
    elif short_class == "TextView":
        return "TextView"
    elif short_class == "ImageView":
        return "ImageView"
    return short_class

def make_element_id(text: str, desc: str, resource_id: str, tag: str, index: int, hint: str = "") -> str:
    """Generates a clean snake_case id for an element."""
    if resource_id and "/" in resource_id:
        res_suffix = resource_id.split("/")[-1]
        cleaned = re.sub(r'[^a-zA-Z0-9_]', '', res_suffix)
        if cleaned:
            return cleaned.lower()
    
    source_str = text or desc or hint
    if source_str:
        cleaned = source_str.lower()
        cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', cleaned)
        words = cleaned.split()
        if words:
            return "_".join(words[:4])
            
    tag_clean = tag.split('.')[-1] if tag else "element"
    return f"{tag_clean.lower()}_{index}"

def build_selectors(text: str, desc: str, class_name: str, resource_id: str, hint: str = "") -> dict:
    """Builds the selectors dictionary requested."""
    sels = {}
    if desc:
        sels["accessibility_id"] = desc
        sels["uiautomator"] = f'new UiSelector().description("{desc}")'
    if text:
        sels["text_locator"] = text
        if "uiautomator" not in sels:
            sels["uiautomator"] = f'new UiSelector().text("{text}")'
    elif hint:
        sels["hint_locator"] = hint
        if "uiautomator" not in sels:
            sels["uiautomator"] = f'new UiSelector().text("{hint}")'
    if resource_id:
        sels["resource_id"] = resource_id
        if "uiautomator" not in sels:
            sels["uiautomator"] = f'new UiSelector().resourceId("{resource_id}")'
    if "uiautomator" not in sels and class_name:
        sels["uiautomator"] = f'new UiSelector().className("{class_name}")'
    return sels

def format_element_for_map(el: dict, screen_width: int, screen_height: int, idx: int) -> dict:
    """Formats element to the requested nodes schema."""
    bounds = el.get("bounds", "")
    element_type = clean_element_type(el.get("class", ""))
    
    fallback_coords = None
    if bounds:
        ratio_dict = get_bounds_center_ratio(bounds, screen_width, screen_height)
        if ratio_dict:
            fallback_coords = ratio_dict
            
    element_id = make_element_id(
        text=el.get("text", ""),
        desc=el.get("content_desc", ""),
        resource_id=el.get("resource_id", ""),
        tag=el.get("class", ""),
        index=idx,
        hint=el.get("hint", "")
    )
    
    selectors = build_selectors(
        text=el.get("text", ""),
        desc=el.get("content_desc", ""),
        class_name=el.get("class", ""),
        resource_id=el.get("resource_id", ""),
        hint=el.get("hint", "")
    )
    
    result = {
        "element_id": element_id,
        "bounds": bounds,
        "clickable": el.get("clickable", True),
        "element_type": element_type,
        "text": el.get("text", ""),
        "content_desc": el.get("content_desc", ""),
        "hint": el.get("hint", ""),
        "selectors": selectors
    }
    if fallback_coords:
        result["vision_fallback_coordinates"] = fallback_coords
    return result

@router.post("/robo_crawl")
async def robo_crawl(request: RoboCrawlRequest):
    """Starts Appium session, fetches screen XML, generates fingerprint, and returns structured node map."""
    client = None
    try:
        ensure_appium_server(request.deviceType)
        client = AppiumMcpClient(server_dir="appium-android")
        await client.start()

        session_args = {
            "deviceName": request.deviceName,
            "udid": request.udid,
            "appPackage": request.appPackage,
            "appActivity": request.appActivity,
            "deviceType": request.deviceType
        }
        await client.call_tool("start_session", session_args)
        await asyncio.sleep(4.0)

        try:
            await client.call_tool("execute_script", {
                "script": "mobile: startActivity",
                "args": [
                    {
                        "appPackage": request.appPackage,
                        "appActivity": request.appActivity
                    }
                ]
            })
            await asyncio.sleep(3.0)
        except Exception as e:
            logger.warning(f"Failed to force startActivity: {e}. Trying activate_app as fallback.")
            await client.call_tool("activate_app", {"appPackage": request.appPackage})
            await asyncio.sleep(3.0)

        res = await client.call_tool("get_page_source", {})
        live_xml = ""
        for item in res.get("content", []):
            if item.get("type") == "text":
                live_xml += item.get("text", "")

        if not live_xml:
            return {"status": "error", "message": "Failed to retrieve page source XML"}

        fingerprint = get_screen_fingerprint(live_xml)
        screen_width, screen_height = get_screen_dimensions(live_xml)
        elements = filter_xml_to_elements(live_xml)
        formatted_elements = [
            format_element_for_map(el, screen_width, screen_height, idx)
            for idx, el in enumerate(elements)
        ]

        default_name = request.appPackage.split(".")[-1]
        screen_name, screen_type = await classify_screen_with_ai(elements, default_name)
        clean_name = re.sub(r'[^a-zA-Z0-9]', '_', screen_name.lower())
        node_id = f"{clean_name}_root" if screen_type == "Home" else f"{clean_name}_{fingerprint[:8]}"

        node_data = {
            "node_id": node_id,
            "screen_name": screen_name,
            "screen_type": screen_type,
            "parent_node": None,
            "depth": 0,
            "fingerprint": fingerprint,
            "elements": formatted_elements
        }

        return {
            "status": "success",
            "raw_xml": live_xml,
            "node": node_data
        }

    except Exception as e:
        logger.error(f"Error during robo_crawl: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        if client:
            try:
                await client.stop()
            except Exception:
                pass

async def get_current_screen_elements_via_adb() -> Optional[list]:
    """Uses ADB to dump current screen XML and returns a list of formatted elements."""
    import subprocess
    import shutil
    try:
        adb_path = shutil.which("adb")
        if not adb_path:
            sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
            adb_path = os.path.join(sdk_root, "platform-tools", "adb")
            if not os.path.exists(adb_path):
                logger.warning("adb binary not found, skipping screen XML dump")
                return None

        # Dump UI hierarchy XML on the device
        dump_res = subprocess.run([adb_path, "shell", "uiautomator", "dump", "/sdcard/window_dump_temp.xml"], capture_output=True, text=True)
        if dump_res.returncode != 0:
            logger.warning(f"Failed to dump UI hierarchy: {dump_res.stderr}")
            return None

        # Pull XML file to local directory
        xml_path = os.path.join(os.getcwd(), "window_dump_temp.xml")
        pull_xml_res = subprocess.run([adb_path, "pull", "/sdcard/window_dump_temp.xml", xml_path], capture_output=True, text=True)
        if pull_xml_res.returncode != 0:
            logger.warning(f"Failed to pull UI hierarchy XML: {pull_xml_res.stderr}")
            return None

        # Read the XML
        with open(xml_path, "r", encoding="utf-8") as f:
            xml_text = f.read()

        # Clean up temporary XML file from SD card and local disk
        subprocess.run([adb_path, "shell", "rm", "/sdcard/window_dump_temp.xml"], stdout=subprocess.DEVNULL)
        if os.path.exists(xml_path):
            try:
                os.remove(xml_path)
            except Exception:
                pass

        if not xml_text:
            return None

        screen_width, screen_height = get_screen_dimensions(xml_text)
        elements = filter_xml_to_elements(xml_text)
        formatted_elements = [
            format_element_for_map(el, screen_width, screen_height, idx)
            for idx, el in enumerate(elements)
        ]
        return formatted_elements
    except Exception as e:
        logger.error(f"Error dumping screen elements via ADB: {e}")
        return None

_easyocr_reader = None

def get_ocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr
        # Initialize easyocr reader locally for english text detection
        _easyocr_reader = easyocr.Reader(['en'], gpu=False)
    return _easyocr_reader

def get_visual_elements_via_ocr_cv(
    screenshot_path: str,
    crop_bounds: str = None,
    include_ocr: bool = True,
    include_opencv: bool = False
) -> tuple[list[dict], str]:
    """Uses OpenCV and EasyOCR to parse visual elements (text and non-text shapes) and generates an annotated screenshot."""
    import cv2
    import os
    import re
    
    try:
        img = cv2.imread(screenshot_path)
        if img is None:
            logger.warning(f"Failed to read screenshot at {screenshot_path}")
            return [], ""
            
        x_offset, y_offset = 0, 0
        img_for_ocr = img
        
        # Crop the image if crop_bounds is specified (format: [x1,y1][x2,y2])
        if crop_bounds:
            match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', crop_bounds.strip())
            if match:
                x1, y1, x2, y2 = map(int, match.groups())
                img_for_ocr = img[y1:y2, x1:x2]
                x_offset, y_offset = x1, y1
                logger.info(f"Cropping OCR/CV to bounds: {crop_bounds} (offset x={x_offset}, y={y_offset})")
        
        h, w = img_for_ocr.shape[:2]
        
        visual_elements = []
        
        # 1. Run EasyOCR text detection if requested
        if include_ocr and h > 0 and w > 0:
            reader = get_ocr_reader()
            # 50% scaling for 4x speed optimization
            scale = 0.5
            img_resized = cv2.resize(img_for_ocr, (int(w * scale), int(h * scale)))
            results = reader.readtext(img_resized)
            
            for idx, (bbox, text, confidence) in enumerate(results):
                # Scale coordinates back up to original size and add offsets
                x_coords = [int(p[0] / scale) + x_offset for p in bbox]
                y_coords = [int(p[1] / scale) + y_offset for p in bbox]
                x1, y1 = min(x_coords), min(y_coords)
                x2, y2 = max(x_coords), max(y_coords)
                cx = (x1 + x2) // 2
                cy = (y1 + y2) // 2
                
                visual_elements.append({
                    "visual_id": f"visual_{idx}",
                    "text": text,
                    "confidence": round(float(confidence), 3),
                    "bounds": f"[{x1},{y1}][{x2},{y2}]",
                    "center": {"x": cx, "y": cy}
                })
                
                # Draw green rectangle for OCR text bounding box
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(img, f"[{idx}]", (x1, max(y1 - 5, 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

        # 2. Run OpenCV contour detection for non-text graphics/shapes if requested
        if include_opencv and h > 0 and w > 0:
            gray = cv2.cvtColor(img_for_ocr, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)
            edged = cv2.Canny(blurred, 50, 150)
            contours, _ = cv2.findContours(edged.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            for idx, c in enumerate(contours):
                rx, ry, rw, rh = cv2.boundingRect(c)
                # Filter out noise (too small) or the entire card/screen border (too big)
                if rw < 15 or rh < 15 or rw > w - 20 or rh > h - 20:
                    continue
                    
                # Absolute coordinates
                cx1 = rx + x_offset
                cy1 = ry + y_offset
                cx2 = rx + rw + x_offset
                cy2 = ry + rh + y_offset
                ccx = cx1 + rw // 2
                ccy = cy1 + rh // 2
                
                # Check overlap with existing OCR elements
                is_overlapping = False
                for el in visual_elements:
                    el_match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', el["bounds"])
                    if el_match:
                        ex1, ey1, ex2, ey2 = map(int, el_match.groups())
                        ix1 = max(cx1, ex1)
                        iy1 = max(cy1, ey1)
                        ix2 = min(cx2, ex2)
                        iy2 = min(cy2, ey2)
                        if ix1 < ix2 and iy1 < iy2:
                            is_overlapping = True
                            break
                            
                if not is_overlapping:
                    visual_elements.append({
                        "visual_id": f"graphic_shape_{idx}",
                        "text": "[Icon/Graphic]",
                        "confidence": 1.0,
                        "bounds": f"[{cx1},{cy1}][{cx2},{cy2}]",
                        "center": {"x": ccx, "y": ccy}
                    })
                    
                    # Draw blue rectangle for OpenCV detected graphic shapes
                    cv2.rectangle(img, (cx1, cy1), (cx2, cy2), (255, 0, 0), 2)
                    cv2.putText(img, f"[CV_{idx}]", (cx1, max(cy1 - 5, 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)

        # Save annotated image in the same directory
        dir_name = os.path.dirname(screenshot_path)
        base_name = os.path.basename(screenshot_path)
        annotated_filename = f"annotated_{base_name}"
        annotated_path = os.path.join(dir_name, annotated_filename)
        cv2.imwrite(annotated_path, img)
        
        # Relative path for API response
        relative_annotated_path = f"assets/screenshots/{annotated_filename}"
        
        return visual_elements, relative_annotated_path
    except Exception as e:
        logger.error(f"Error in OCR/CV visual parsing: {e}", exc_info=True)
        return [], ""

class CaptureAdbRequest(BaseModel):
    screen_name: str = "Manual_Screen"
    isForm: bool = False

    model_config = {"extra": "allow"}

async def capture_screen_hybrid(
    screen_name: str = "Screen", 
    is_form: bool = False, 
    form_data: dict = None,
    include_ocr: bool = False,
    include_opencv: bool = False,
    skip_ai_classification: bool = True,
    crop_bounds: str = None
) -> dict:
    """Helper that dumps XML, captures screenshot, runs OCR/CV visual parsing, and handles form filling if requested."""
    import subprocess
    import shutil
    import os
    import re
    import asyncio

    extra_data = form_data or {}
    adb_path = shutil.which("adb")
    if not adb_path:
        sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
        adb_path = os.path.join(sdk_root, "platform-tools", "adb")
        if not os.path.exists(adb_path):
            raise Exception("adb binary not found in PATH or Android SDK root")

    # 1. Dump UI hierarchy XML on the device
    dump_res = subprocess.run([adb_path, "shell", "uiautomator", "dump", "/sdcard/window_dump.xml"], capture_output=True, text=True)
    if dump_res.returncode != 0:
        raise Exception(f"Failed to dump UI hierarchy: {dump_res.stderr}")

    # 2. Pull XML file to local directory
    xml_path = os.path.join(os.getcwd(), "window_dump.xml")
    pull_xml_res = subprocess.run([adb_path, "pull", "/sdcard/window_dump.xml", xml_path], capture_output=True, text=True)
    if pull_xml_res.returncode != 0:
        raise Exception(f"Failed to pull UI hierarchy XML: {pull_xml_res.stderr}")

    # 3. Read the XML
    with open(xml_path, "r", encoding="utf-8") as f:
        xml_text = f.read()

    # Clean up temporary XML file from SD card and local disk
    subprocess.run([adb_path, "shell", "rm", "/sdcard/window_dump.xml"], stdout=subprocess.DEVNULL)
    if os.path.exists(xml_path):
        os.remove(xml_path)

    if not xml_text:
        raise Exception("Empty XML content retrieved")

    # 4. Form handling: if isForm is True, fill all input fields and re-dump
    if is_form:
        initial_elements = filter_xml_to_elements(xml_text)
        input_elements = [el for el in initial_elements if el.get("is_input")]
        
        if input_elements or extra_data:
            logger.info(f"Form handling active. Found {len(input_elements)} input field(s) to edit. Custom inputs: {extra_data}")
            
            # A. Handle EditText fields
            for el in input_elements:
                bounds_str = el.get("bounds", "")
                if not bounds_str:
                    continue
                bounds_match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
                if bounds_match:
                    x1, y1, x2, y2 = map(int, bounds_match.groups())
                    center_x = (x1 + x2) // 2
                    center_y = (y1 + y2) // 2
                    
                    # Click input field to focus it
                    subprocess.run([adb_path, "shell", "input", "tap", str(center_x), str(center_y)], stdout=subprocess.DEVNULL)
                    await asyncio.sleep(0.5)
                    
                    # Clear existing text using MOVE_END followed by backspaces
                    clear_cmd = [adb_path, "shell", "input", "keyevent", "123"] + ["67"] * 40
                    subprocess.run(clear_cmd, stdout=subprocess.DEVNULL)
                    await asyncio.sleep(0.2)
                    
                    # Resolve label candidates for matching
                    label_candidates = []
                    res_id = el.get("resource_id", "")
                    if res_id:
                        label_candidates.append(res_id)
                        clean_id = res_id.split('/')[-1] if '/' in res_id else res_id
                        label_candidates.append(clean_id)
                        label_candidates.append(clean_id.replace('_', ' ').replace('-', ' '))
                    if el.get("content_desc"):
                        label_candidates.append(el.get("content_desc"))
                    if el.get("text"):
                        label_candidates.append(el.get("text"))
                        
                    # Scan backwards in initial_elements to locate the preceding TextView/Label
                    matching_in_all = -1
                    for k, item in enumerate(initial_elements):
                        if item.get("selector") == el.get("selector") and item.get("bounds") == el.get("bounds"):
                            matching_in_all = k
                            break
                    if matching_in_all > 0:
                        for k in range(matching_in_all - 1, -1, -1):
                            prev_item = initial_elements[k]
                            if not prev_item.get("is_input"):
                                label_text = prev_item.get("text") or prev_item.get("content_desc")
                                if label_text and len(label_text.strip()) > 1:
                                    label_candidates.append(label_text)
                                    break
                                    
                    logger.info(f"Input field '{el.get('selector')}' label candidates: {label_candidates}")
                    
                    # Check custom input keys for a match
                    matched_val = None
                    for key, val in extra_data.items():
                        key_lower = key.lower().strip()
                        for cand in label_candidates:
                            cand_lower = cand.lower().strip()
                            if key_lower in cand_lower or cand_lower in key_lower:
                                matched_val = str(val)
                                logger.info(f"Matched custom request key '{key}' -> value '{val}' for input '{el.get('selector')}'")
                                break
                        if matched_val is not None:
                            break
                            
                    if matched_val is not None:
                        val = matched_val
                    else:
                        identifier = el.get("text") or el.get("content_desc") or el.get("resource_id") or "input"
                        identifier_lower = identifier.lower()
                        if "email" in identifier_lower:
                            val = "test@example.com"
                        elif "pass" in identifier_lower:
                            val = "Password123"
                        elif "phone" in identifier_lower:
                            val = "+1234567890"
                        else:
                            val = "QA Tester"
                    
                    # Type the text (replace space with %s, escape quotes)
                    adb_text = val.replace(" ", "%s").replace('"', '\\"').replace("'", "\\'")
                    subprocess.run([adb_path, "shell", "input", "text", adb_text], stdout=subprocess.DEVNULL)
                    await asyncio.sleep(0.5)

            # B. Handle Clicking of Selectors / Spinner options / Checkboxes matching custom values
            for el in initial_elements:
                if el.get("is_input"):
                    continue
                text_or_desc = (el.get("text") or el.get("content_desc") or "").strip()
                if not text_or_desc:
                    continue
                
                for key, val in extra_data.items():
                    if str(val).lower().strip() == text_or_desc.lower():
                        bounds_str = el.get("bounds", "")
                        if bounds_str:
                            bounds_match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
                            if bounds_match:
                                x1, y1, x2, y2 = map(int, bounds_match.groups())
                                cx = (x1 + x2) // 2
                                cy = (y1 + y2) // 2
                                logger.info(f"Clicking matching selector view: '{text_or_desc}' for request value '{val}'")
                                subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
                                await asyncio.sleep(0.8)
            
            # Hide keyboard using Escape keyevent 111 so the screenshot is clean
            subprocess.run([adb_path, "shell", "input", "keyevent", "111"], stdout=subprocess.DEVNULL)
            await asyncio.sleep(1.0)
            
            # Re-dump XML representing the updated/edited form layout
            dump_res_2 = subprocess.run([adb_path, "shell", "uiautomator", "dump", "/sdcard/window_dump.xml"], capture_output=True, text=True)
            if dump_res_2.returncode == 0:
                xml_path_2 = os.path.join(os.getcwd(), "window_dump.xml")
                pull_xml_res_2 = subprocess.run([adb_path, "pull", "/sdcard/window_dump.xml", xml_path_2], capture_output=True, text=True)
                if pull_xml_res_2.returncode == 0:
                    with open(xml_path_2, "r", encoding="utf-8") as f:
                        xml_text = f.read()
                    subprocess.run([adb_path, "shell", "rm", "/sdcard/window_dump.xml"], stdout=subprocess.DEVNULL)
                    if os.path.exists(xml_path_2):
                        os.remove(xml_path_2)

    # 5. Take screenshot on device
    screenshot_filename = f"manual_{screen_name.lower()}.png"
    subprocess.run([adb_path, "shell", "screencap", "-p", f"/sdcard/{screenshot_filename}"], stdout=subprocess.DEVNULL)

    # 6. Pull screenshot locally to assets/screenshots/
    ss_dir = os.path.join(os.getcwd(), "assets", "screenshots")
    os.makedirs(ss_dir, exist_ok=True)
    local_screenshot_path = os.path.join(ss_dir, screenshot_filename)
    pull_ss_res = subprocess.run([adb_path, "pull", f"/sdcard/{screenshot_filename}", local_screenshot_path], capture_output=True, text=True)
    
    # Clean up screenshot on device
    subprocess.run([adb_path, "shell", "rm", f"/sdcard/{screenshot_filename}"], stdout=subprocess.DEVNULL)

    relative_screenshot_path = f"assets/screenshots/{screenshot_filename}" if pull_ss_res.returncode == 0 else "N/A"

    # 7. Parse and format the elements
    screen_width, screen_height = get_screen_dimensions(xml_text)
    elements = filter_xml_to_elements(xml_text)
    
    formatted_elements = [
        format_element_for_map(el, screen_width, screen_height, idx)
        for idx, el in enumerate(elements)
    ]
    
    # Retrieve visual elements and annotated screenshot via EasyOCR & OpenCV
    visual_elements, annotated_screenshot_path = [], "N/A"
    if (include_ocr or include_opencv) and pull_ss_res.returncode == 0:
        visual_elements, annotated_screenshot_path = get_visual_elements_via_ocr_cv(
            local_screenshot_path,
            crop_bounds=crop_bounds,
            include_ocr=include_ocr,
            include_opencv=include_opencv
        )

    if skip_ai_classification:
        screen_name_final = screen_name
        screen_type = "General"
    else:
        screen_name_final, screen_type = await classify_screen_with_ai(elements, screen_name)
        
    node_id = f"{re.sub(r'[^a-zA-Z0-9]', '_', screen_name_final.lower())}_manual"

    return {
        "node_id": node_id,
        "screen_name": screen_name_final,
        "screen_type": screen_type,
        "screenshot_path": relative_screenshot_path,
        "annotated_screenshot_path": annotated_screenshot_path,
        "raw_xml": xml_text,
        "elements": formatted_elements,
        "visual_elements": visual_elements
    }

@router.post("/capture_adb")
async def capture_adb(request: CaptureAdbRequest):
    """Bypasses Appium entirely, dumps accessibility XML and screenshots directly via ADB shell, and parses them into JSON.
    If isForm is True, it automatically fills any input fields on the screen using ADB and generates an updated layout map.
    Accepts arbitrary extra fields in request JSON representing form inputs to be filled."""
    try:
        # Extract extra form fields from request body for custom value matching
        extra_data = {}
        if hasattr(request, "model_extra") and request.model_extra:
            extra_data = request.model_extra
        elif hasattr(request, "__dict__"):
            extra_data = {k: v for k, v in request.__dict__.items() if k not in ("screen_name", "isForm")}

        screen_data = await capture_screen_hybrid(
            screen_name=request.screen_name,
            is_form=request.isForm,
            form_data=extra_data
        )

        return {
            "status": "success",
            "raw_xml": screen_data["raw_xml"],
            "screen_map": {
                "node_id": screen_data["node_id"],
                "screen_name": screen_data["screen_name"],
                "screen_type": screen_data["screen_type"],
                "screenshot_path": screen_data["screenshot_path"],
                "annotated_screenshot_path": screen_data["annotated_screenshot_path"],
                "elements": screen_data["elements"],
                "visual_elements": screen_data["visual_elements"]
            }
        }

    except Exception as e:
        logger.error(f"Error during ADB capture: {e}")
        return {"status": "error", "message": str(e)}


async def call_ollama_to_generate_script(
    app_map: dict,
    test_steps: str,
    deviceName: str,
    appPackage: str,
    appActivity: str,
    appiumUrl: str
) -> str:
    # Parse Appium URL to get hostname, port, path
    hostname = "127.0.0.1"
    port = 4723
    path = "/"
    try:
        from urllib.parse import urlparse
        parsed_url = urlparse(appiumUrl)
        hostname = parsed_url.hostname or "127.0.0.1"
        port = parsed_url.port or 4723
        path = parsed_url.path or "/"
    except Exception:
        pass

    prompt = (
        f"CRITICAL: You are an Android Appium UI Automator 2 script generator.\n"
        f"You MUST generate an Appium Android test script using UI Automator 2 capabilities, platformName ('Android'), the target appPackage, appActivity, and the remote connection options provided.\n"
        f"DO NOT write web browser test code (like Chrome or Firefox testing a website). This is not a web app test, it is a native Android app test.\n\n"
        f"You are a professional QA automation engineer.\n"
        f"Your task is to write a runnable Node.js (WebdriverIO) test script based on the following inputs.\n\n"
        f"--- App Map JSON ---\n"
        f"{compress_app_map_for_llm(app_map, test_steps)}\n\n"
        f"--- Test Case Steps ---\n"
        f"{test_steps}\n\n"
        f"--- Appium Config ---\n"
        f"Hostname: {hostname}\n"
        f"Port: {port}\n"
        f"Path: {path}\n"
        f"Device Name: {deviceName}\n"
        f"App Package: {appPackage}\n"
        f"App Activity: {appActivity}\n\n"
        f"--- Required Structure & Style ---\n"
        f"Write Node.js script using ESM import for WebdriverIO. DO NOT use TypeScript type annotations or interfaces. Use pure JavaScript:\n"
        f"```javascript\n"
        f"import {{ remote }} from 'webdriverio';\n\n"
        f"(async () => {{\n"
        f"    const caps = {{\n"
        f"        platformName: 'Android',\n"
        f"        'appium:automationName': 'UiAutomator2',\n"
        f"        'appium:deviceName': '{deviceName}',\n"
        f"        'appium:appPackage': '{appPackage}',\n"
        f"        'appium:appActivity': '{appActivity}',\n"
        f"        'appium:newCommandTimeout': 240,\n"
        f"        'appium:noReset': true,\n"
        f"        'appium:settings[allowInvisibleElements]': true,\n"
        f"        'appium:disableSuppressAccessibilityService': true\n"
        f"    }};\n\n"
        f"    const driver = await remote({{\n"
        f"        hostname: '{hostname}',\n"
        f"        port: {port},\n"
        f"        path: '{path}',\n"
        f"        capabilities: caps\n"
        f"    }});\n\n"
        f"    try {{\n"
        f"        console.log('Successfully connected to the active application session...');\n"
        f"        await driver.pause(1000);\n\n"
        f"        // Step-by-step automation commands go here...\n"
        f"        // Map each user test step to elements in the App Map nodes.\n"
        f"        // Example: \n"
        f"        // const navMenu = await driver.$('//*[@content-desc=\"Open navigation menu\"]');\n"
        f"        // await navMenu.waitForDisplayed({{ timeout: 5000 }});\n"
        f"        // await navMenu.click();\n\n"
        f"    }} catch (error) {{\n"
        f"        console.error('Automation execution failure encountered:', error);\n"
        f"    }} finally {{\n"
        f"        await driver.deleteSession();\n"
        f"        console.log('Appium session ended.');\n"
        f"    }}\n"
        f"}})();\n"
        f"```\n\n"
        f"--- Robustness & Flake Prevention Guidelines ---\n"
        f"1. **Timing & Animation Pauses**: Emulators are slow. Always insert `await driver.pause(1500);` immediately after performing clicks that open side drawers, navigation menus, or load new screens to let animations complete.\n"
        f"2. **Menu Click Verification**: If a click is supposed to open a drawer or menu (e.g. 'Open navigation menu'), verify if the drawer contents are visible (e.g. check if the next element like 'Holiday list' is displayed). If it is not, perform a fallback click on the menu button or wait longer.\n"
        f"3. **Alternative/Fallback Locators**: Do not rely on a single locator strategy. If a primary selector fails (e.g., using accessibility ID), use a conditional fallback (e.g. check if element exists, otherwise find by text contains or class name index).\n"
        f"4. **Locator Guidelines**:\n"
        f"   - For accessibility descriptions/content-desc: use xpath `driver.$('//*[@content-desc=\"...\"]')` or `driver.$('~...')`.\n"
        f"   - For text: use `driver.$('android=new UiSelector().className(\"...\").textContains(\"...\")')` or `driver.$('//*[@text=\"...\"]')`.\n"
        f"5. Output ONLY the JavaScript code block starting with ```javascript and ending with ```. Do not add markdown explanation before or after the block."
    )
    
    return await call_llm_generate(prompt)


def extract_javascript_code(raw_text: str) -> str:
    match = re.search(r'```javascript\s*(.*?)\s*```', raw_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match_js = re.search(r'```js\s*(.*?)\s*```', raw_text, re.DOTALL)
    if match_js:
        return match_js.group(1).strip()
    match_any = re.search(r'```\s*(.*?)\s*```', raw_text, re.DOTALL)
    if match_any:
        return match_any.group(1).strip()
    return raw_text.strip()


@router.post("/generate_script")
async def generate_script(request: GenerateScriptRequest):
    """Generates an Appium WebdriverIO Node.js test script from the given app map and test steps using Ollama."""
    try:
        raw_script = await call_ollama_to_generate_script(
            app_map=request.app_map,
            test_steps=request.test_steps,
            deviceName=request.deviceName,
            appPackage=request.appPackage,
            appActivity=request.appActivity,
            appiumUrl=request.appiumUrl
        )
        clean_script = extract_javascript_code(raw_script)
        return {
            "status": "success",
            "script": clean_script,
            "raw_response": raw_script
        }
    except Exception as e:
        import traceback
        tb_str = traceback.format_exc()
        logger.error(f"Error during script generation: {e}\n{tb_str}")
        return {"status": "error", "message": str(e), "traceback": tb_str}


@router.post("/generate_script_from_file")
async def generate_script_from_file(
    app_map_file: UploadFile = File(...),
    test_steps: str = Form(...),
    deviceName: str = Form("emulator-5554"),
    appPackage: str = Form("com.kuberproject"),
    appActivity: str = Form(".MainActivity"),
    appiumUrl: str = Form("http://127.0.0.1:4723")
):
    """Generates an Appium WebdriverIO Node.js test script by uploading an app map JSON file and providing test steps."""
    try:
        content = await app_map_file.read()
        app_map = json.loads(content.decode("utf-8"))
        
        raw_script = await call_ollama_to_generate_script(
            app_map=app_map,
            test_steps=test_steps,
            deviceName=deviceName,
            appPackage=appPackage,
            appActivity=appActivity,
            appiumUrl=appiumUrl
        )
        clean_script = extract_javascript_code(raw_script)
        return {
            "status": "success",
            "script": clean_script,
            "raw_response": raw_script
        }
    except Exception as e:
        import traceback
        tb_str = traceback.format_exc()
        logger.error(f"Error during file script generation: {e}\n{tb_str}")
        return {"status": "error", "message": str(e), "traceback": tb_str}


def sanitize_script(script_str: str) -> str:
    if not script_str:
        return ""
    clean_js = script_str.strip()
    
    # 1. Check if the string is wrapped in double quotes and contains other json properties (e.g. copy-paste error)
    if clean_js.startswith('"') and '":' in clean_js:
        try:
            wrapped = "{" + f'"code": {clean_js}' + "}"
            parsed = json.loads(wrapped)
            if "code" in parsed:
                clean_js = parsed["code"]
        except Exception:
            pass
            
    # 2. Check if the entire string is a JSON object
    if clean_js.startswith('{'):
        try:
            parsed = json.loads(clean_js)
            if isinstance(parsed, dict):
                if "script" in parsed:
                    clean_js = parsed["script"]
                elif "raw_response" in parsed:
                    clean_js = extract_javascript_code(parsed["raw_response"])
        except Exception:
            pass
            
    # 3. Clean up escaping if it's double-quoted
    if clean_js.startswith('"') and clean_js.endswith('"'):
        try:
            clean_js = json.loads(clean_js)
        except Exception:
            # Fallback manual unescaping
            clean_js = clean_js[1:-1].replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
            
    # 4. Extract from markdown code blocks
    if "```javascript" in clean_js or "```js" in clean_js:
        clean_js = extract_javascript_code(clean_js)
        
    # 5. Clean up TypeScript type annotations on variable declarations
    clean_js = re.sub(
        r'\b(const|let|var)\s+(\w+)\s*:\s*[a-zA-Z0-9_\.\[\]<>|,\(\)\s]+(\s*=)',
        r'\1 \2\3',
        clean_js
    )
    
    # 6. Clean up 'import type' declarations
    clean_js = re.sub(r'\bimport\s+type\b', 'import', clean_js)
        
    return clean_js.strip()


async def run_and_auto_heal_script(
    script: str,
    app_map: Optional[Union[dict, str]],
    test_steps: Optional[str],
    deviceName: str,
    appPackage: str,
    appActivity: str,
    appiumUrl: str
) -> dict:
    import tempfile
    import subprocess
    
    # Resolve app_map if it's a file path string
    resolved_app_map = app_map
    if isinstance(app_map, str):
        try:
            if os.path.exists(app_map):
                with open(app_map, 'r', encoding='utf-8') as f:
                    resolved_app_map = json.load(f)
            else:
                logger.warning(f"Provided app_map file path does not exist: {app_map}")
                resolved_app_map = None
        except Exception as e:
            logger.error(f"Failed to load app_map from file path '{app_map}': {e}")
            resolved_app_map = None

    # Ensure Appium server is running
    try:
        ensure_appium_server("local emulator")
    except Exception as e:
        logger.warning(f"Could not guarantee Appium server is running: {e}")

    current_script = sanitize_script(script)
    max_attempts = 5
    attempts_history = []
    
    for attempt in range(1, max_attempts + 1):
        logger.info(f"Execution attempt {attempt}/{max_attempts}...")
        
        # Write current_script to a temp file
        temp_fd, temp_path = tempfile.mkstemp(suffix=".mjs", dir=os.getcwd())
        try:
            with os.fdopen(temp_fd, 'w', encoding='utf-8') as tmp:
                tmp.write(current_script)
                
            run_res = subprocess.run(
                ["node", temp_path],
                capture_output=True,
                text=True,
                timeout=90.0
            )
            
            # Check if there is an execution failure signature in output, even if exit code is 0 (due to catch block)
            has_error = (
                run_res.returncode != 0 or 
                "Automation execution failure encountered" in (run_res.stderr or "") or 
                "Automation execution failure encountered" in (run_res.stdout or "")
            )
            
            if not has_error:
                logger.info(f"Script executed successfully on attempt {attempt}!")
                return {
                    "status": "success",
                    "attempts_taken": attempt,
                    "repaired": attempt > 1,
                    "final_script": current_script,
                    "returncode": run_res.returncode,
                    "stdout": run_res.stdout,
                    "stderr": run_res.stderr,
                    "history": attempts_history
                }
                
            # If it failed, record logs
            error_msg = f"Stdout:\n{run_res.stdout}\n\nStderr:\n{run_res.stderr}"
            logger.warning(f"Attempt {attempt} failed.\n{error_msg}")
            
            attempts_history.append({
                "attempt": attempt,
                "script": current_script,
                "stdout": run_res.stdout,
                "stderr": run_res.stderr
            })
            
            # Check if we should/can try auto-repairing
            if attempt == max_attempts:
                logger.info("Reached maximum execution attempts. Aborting.")
                break
                
            if not resolved_app_map or not test_steps:
                logger.info("Script failed, but no app_map or test_steps provided for auto-healing. Skipping retries.")
                break
                
            # Trigger auto-repair for the next attempt
            logger.info(f"Triggering AI repair fallback for next attempt...")
            current_screen_data = None
            try:
                current_screen_data = await capture_screen_hybrid(screen_name=f"failure_attempt_{attempt}")
                logger.info(f"Successfully captured hybrid screen elements (XML + {len(current_screen_data.get('visual_elements', []))} OCR elements) at failure moment.")
            except Exception as e:
                logger.warning(f"Could not capture active hybrid screen elements at failure moment: {e}")

            raw_repaired = await call_ollama_to_repair_script(
                app_map=resolved_app_map,
                test_steps=test_steps,
                script=current_script,
                error_message=error_msg,
                deviceName=deviceName,
                appPackage=appPackage,
                appActivity=appActivity,
                appiumUrl=appiumUrl,
                current_screen_data=current_screen_data
            )
            current_script = extract_javascript_code(raw_repaired)
            
        except subprocess.TimeoutExpired:
            logger.error(f"Execution timed out on attempt {attempt}.")
            attempts_history.append({
                "attempt": attempt,
                "script": current_script,
                "status": "timeout",
                "message": "Script execution timed out after 90 seconds."
            })
            if attempt == max_attempts or not resolved_app_map or not test_steps:
                break
            # Try repairing even on timeout
            logger.info(f"Triggering AI repair fallback after timeout...")
            current_screen_data = None
            try:
                current_screen_data = await capture_screen_hybrid(screen_name=f"timeout_attempt_{attempt}")
                logger.info(f"Successfully captured hybrid screen elements at timeout moment.")
            except Exception as e:
                logger.warning(f"Could not capture active hybrid screen elements at timeout moment: {e}")

            raw_repaired = await call_ollama_to_repair_script(
                app_map=resolved_app_map,
                test_steps=test_steps,
                script=current_script,
                error_message="TimeoutExpired: The script execution timed out after 90 seconds.",
                deviceName=deviceName,
                appPackage=appPackage,
                appActivity=appActivity,
                appiumUrl=appiumUrl,
                current_screen_data=current_screen_data
            )
            current_script = extract_javascript_code(raw_repaired)
        except Exception as e:
            logger.error(f"Error during attempt {attempt}: {e}")
            return {
                "status": "error",
                "message": str(e),
                "history": attempts_history
            }
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    # If it fails all attempts
    return {
        "status": "failed",
        "attempts_taken": max_attempts,
        "repaired": max_attempts > 1,
        "final_script": current_script,
        "message": f"Script failed to execute successfully after {max_attempts} attempts.",
        "history": attempts_history
    }


@router.post("/execute_script")
async def execute_script(request: ExecuteScriptRequest):
    """Saves the provided WebdriverIO javascript to a temporary ES module file and executes it via Node.js.
    If execution fails and app_map/test_steps are provided, automatically calls Ollama to repair the script and retries execution.
    """
    return await run_and_auto_heal_script(
        script=request.script,
        app_map=request.app_map,
        test_steps=request.test_steps,
        deviceName=request.deviceName,
        appPackage=request.appPackage,
        appActivity=request.appActivity,
        appiumUrl=request.appiumUrl
    )


@router.post("/execute_script_from_file")
async def execute_script_from_file(
    app_map_file: UploadFile = File(...),
    script: str = Form(...),
    test_steps: str = Form(...),
    deviceName: str = Form("emulator-5554"),
    appPackage: str = Form("com.kuberproject"),
    appActivity: str = Form(".MainActivity"),
    appiumUrl: str = Form("http://127.0.0.1:4723")
):
    """Executes the WebdriverIO Javascript script.
    If execution fails, automatically parses the uploaded app_map_file and runs Ollama repair fallback retry loop.
    """
    try:
        content = await app_map_file.read()
        app_map = json.loads(content.decode("utf-8"))
    except Exception as e:
        logger.warning(f"Failed to parse uploaded app_map_file: {e}")
        app_map = None

    return await run_and_auto_heal_script(
        script=script,
        app_map=app_map,
        test_steps=test_steps,
        deviceName=deviceName,
        appPackage=appPackage,
        appActivity=appActivity,
        appiumUrl=appiumUrl
    )


async def call_ollama_to_repair_script(
    app_map: dict,
    test_steps: str,
    script: str,
    error_message: str,
    deviceName: str,
    appPackage: str,
    appActivity: str,
    appiumUrl: str,
    current_screen_data: Optional[dict] = None
) -> str:
    # Parse Appium URL to get hostname, port, path
    hostname = "127.0.0.1"
    port = 4723
    path = "/"
    try:
        from urllib.parse import urlparse
        parsed_url = urlparse(appiumUrl)
        hostname = parsed_url.hostname or "127.0.0.1"
        port = parsed_url.port or 4723
        path = parsed_url.path or "/"
    except Exception:
        pass

    screen_context = ""
    if current_screen_data:
        elements = current_screen_data.get("elements", [])
        visual_elements = current_screen_data.get("visual_elements", [])
        
        screen_context = (
            f"--- Active Screen Elements at Failure Moment ---\n"
            f"These elements were dynamically captured on the emulator screen at the exact moment of failure:\n"
            f"{compress_elements_for_llm(elements)}\n\n"
        )
        if visual_elements:
            screen_context += (
                f"--- OCR/Visual Elements at Failure Moment (Detected directly from Screenshot image) ---\n"
                f"These words were visually read from the screenshot at coordinates:\n"
                f"{compress_visual_elements_for_llm(visual_elements)}\n\n"
            )

    prompt = (
        f"CRITICAL: You are an Android Appium UI Automator 2 test repair agent.\n"
        f"The failed script is an Appium Android script. You MUST preserve the test logic, but ensure the remote connection options and capabilities are correctly configured to point to the Appium server.\n"
        f"DO NOT write web browser code, Chrome code, or Selenium-style code. DO NOT change the target to a website.\n\n"
        f"You are a professional test automation debug engineer.\n"
        f"A WebdriverIO test script failed during execution on the Android emulator.\n"
        f"Your task is to analyze the failure, look at the App Map layout nodes, and modify the script to fix the issue.\n\n"
        f"--- Original Test Case Steps ---\n"
        f"{test_steps}\n\n"
        f"--- Failed WebdriverIO Script ---\n"
        f"{script}\n\n"
        f"--- Stderr / Error Message ---\n"
        f"{error_message}\n\n"
        f"--- Appium Config ---\n"
        f"Hostname: {hostname}\n"
        f"Port: {port}\n"
        f"Path: {path}\n"
        f"Device Name: {deviceName}\n"
        f"App Package: {appPackage}\n"
        f"App Activity: {appActivity}\n\n"
        f"--- App Map JSON ---\n"
        f"{compress_app_map_for_llm(app_map, f'{test_steps}\n{script}')}\n\n"
        f"{screen_context}"
        f"--- Instructions ---\n"
        f"1. **Rebuild Driver Options from Scratch**: Do not preserve the broken `remote(...)` configuration from the failed script. You MUST rebuild it using this exact structure:\n"
        f"   ```javascript\n"
        f"   import {{ remote }} from 'webdriverio';\n\n"
        f"   (async () => {{\n"
        f"       const caps = {{\n"
        f"           platformName: 'Android',\n"
        f"           'appium:automationName': 'UiAutomator2',\n"
        f"           'appium:deviceName': '{deviceName}',\n"
        f"           'appium:appPackage': '{appPackage}',\n"
        f"           'appium:appActivity': '{appActivity}',\n"
        f"           'appium:newCommandTimeout': 240,\n"
        f"           'appium:noReset': true,\n"
        f"           'appium:settings[allowInvisibleElements]': true,\n"
        f"           'appium:disableSuppressAccessibilityService': true\n"
        f"       }};\n\n"
        f"       const driver = await remote({{\n"
        f"           hostname: '{hostname}',\n"
        f"           port: {port},\n"
        f"           path: '{path}',\n"
        f"           capabilities: caps\n"
        f"       }});\n"
        f"       // ... rest of script logic ...\n"
        f"   ```\n"
        f"2. **Analyze Failure Context**: Read the error message to identify which locator or command failed (e.g., driver.click is not a function, or element timeout).\n"
        f"3. **Timing & Animation Pauses**: Emulators are slow. Always insert `await driver.pause(1500);` immediately after performing clicks that open side drawers, navigation menus, or transition screens to let animations complete before searching for the next element.\n"
        f"4. **Inspect Active Screen Elements**: Look closely at the 'Active Screen Elements at Failure Moment'. If the elements list shows that the expected screen is not open (e.g., the drawer menu items are not present), it means the previous click didn't register or needs a retry/fallback selector.\n"
        f"5. **Conditional Retry / Fallbacks**: Do not rely on a single locator strategy. Add checking logic: if the target element is not displayed, try re-clicking the trigger button or using alternative selectors (e.g., xpath description vs text Contains fallback).\n"
        f"6. **Locator Guidelines**: When resolving or generating element locators:\n"
        f"   - For accessibility descriptions / content-desc (like accessibility_id): use xpath `driver.$('//*[@content-desc=\"...\"]')` or `driver.$('~...')`.\n"
        f"   - For text / hint_locator / placeholder text (like text or hint_locator): use `driver.$('//*[@text=\"...\"]')` or `driver.$('android=new UiSelector().text(\"...\")')`.\n"
        f"   - For resource_id: use `driver.$('id=...')` or `driver.$('android=new UiSelector().resourceId(\"...\")')`.\n"
        f"7. **Maintain ESM Format**: Maintain the Node.js ESM format using try/catch/finally block. DO NOT use any TypeScript types or type annotations. Write pure vanilla JavaScript only.\n"
        f"8. **Direct Response**: Respond ONLY with the modified, fully complete, executable JavaScript code inside a single ```javascript code block. No conversational preamble or postscript."
    )
    
    image_path = None
    if current_screen_data:
        image_path = current_screen_data.get("annotated_screenshot_path") or current_screen_data.get("screenshot_path")
        
    return await call_ollama_vision(prompt, image_path, format_json=False)


@router.post("/repair_script")
async def repair_script(request: RepairScriptRequest):
    """Takes a failed script, the error traceback/stderr, the original steps, and the app map, and generates a repaired WebdriverIO script."""
    # Resolve app_map if it's a file path string
    resolved_app_map = request.app_map
    if isinstance(resolved_app_map, str):
        try:
            if os.path.exists(resolved_app_map):
                with open(resolved_app_map, 'r', encoding='utf-8') as f:
                    resolved_app_map = json.load(f)
            else:
                resolved_app_map = {}
        except Exception as e:
            logger.error(f"Failed to load app_map from file path: {e}")
            resolved_app_map = {}

    try:
        raw_script = await call_ollama_to_repair_script(
            app_map=resolved_app_map,
            test_steps=request.test_steps,
            script=sanitize_script(request.script),
            error_message=request.error_message,
            deviceName=request.deviceName,
            appPackage=request.appPackage,
            appActivity=request.appActivity,
            appiumUrl=request.appiumUrl,
            current_screen_elements=request.current_screen_elements
        )
        clean_script = extract_javascript_code(raw_script)
        return {
            "status": "success",
            "script": clean_script,
            "raw_response": raw_script
        }
    except Exception as e:
        logger.error(f"Error during script repair: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/repair_script_from_file")
async def repair_script_from_file(
    app_map_file: UploadFile = File(...),
    script: str = Form(...),
    error_message: str = Form(...),
    test_steps: str = Form(...),
    deviceName: str = Form("emulator-5554"),
    appPackage: str = Form("com.kuberproject"),
    appActivity: str = Form(".MainActivity"),
    appiumUrl: str = Form("http://127.0.0.1:4723"),
    current_screen_elements_json: Optional[str] = Form(None)
):
    """Takes a failed script, the error traceback/stderr, the original steps, and parses the uploaded app map file
    to generate a repaired WebdriverIO script without performing any on-device execution.
    """
    try:
        content = await app_map_file.read()
        app_map = json.loads(content.decode("utf-8"))
    except Exception as e:
        logger.warning(f"Failed to parse uploaded app_map_file: {e}")
        app_map = {}

    current_screen_elements = None
    if current_screen_elements_json:
        try:
            current_screen_elements = json.loads(current_screen_elements_json)
        except Exception:
            pass

    try:
        raw_script = await call_ollama_to_repair_script(
            app_map=app_map,
            test_steps=test_steps,
            script=sanitize_script(script),
            error_message=error_message,
            deviceName=deviceName,
            appPackage=appPackage,
            appActivity=appActivity,
            appiumUrl=appiumUrl,
            current_screen_elements=current_screen_elements
        )
        clean_script = extract_javascript_code(raw_script)
        return {
            "status": "success",
            "script": clean_script,
            "raw_response": raw_script
        }
    except Exception as e:
        logger.error(f"Error during script repair: {e}")
        return {"status": "error", "message": str(e)}

class StepByStepExecuteRequest(BaseModel):
    testcase_step: str
    screen_name: str = "Step_Execution"
    
    model_config = {"extra": "allow"}

@router.post("/execute_step_by_step")
async def execute_step_by_step(request: StepByStepExecuteRequest):
    """Captures the current screen elements (XML & OCR), sends them to the LLM to decide on the action,
    and executes it on the device via ADB commands."""
    try:
        import subprocess
        import shutil
        import os
        import json
        import asyncio

        # 1. Capture current screen state (XML + OCR + annotated screenshot)
        screen_data = await capture_screen_hybrid(screen_name=request.screen_name)
        
        # 2. Extract elements list and visual OCR elements
        elements = screen_data.get("elements", [])
        visual_elements = screen_data.get("visual_elements", [])
        
        # Format elements and OCR text for the LLM
        clean_elements = []
        for el in elements:
            clean_elements.append({
                "element_id": el.get("element_id"),
                "type": el.get("element_type"),
                "text": el.get("semantics", {}).get("text"),
                "desc": el.get("semantics", {}).get("content_desc"),
                "resource_id": el.get("semantics", {}).get("resource_id"),
                "center": el.get("center_coordinates")
            })

        prompt = (
            f"You are a mobile automation agent executing test case steps.\n"
            f"Your current step goal is: \"{request.testcase_step}\"\n\n"
            f"Here is the list of structural elements detected on the screen:\n"
            f"{compress_elements_for_llm(elements)}\n\n"
            f"Here is the list of visual/text elements read directly from the screen via OCR:\n"
            f"{compress_visual_elements_for_llm(visual_elements)}\n\n"
            f"Please decide what action to perform next to achieve the step goal.\n"
            f"Return a JSON object conforming to this schema:\n"
            f"{{\n"
            f"  \"action\": \"one of: 'click', 'type', 'back', 'wait', or 'done'\",\n"
            f"  \"explanation\": \"Brief explanation of what element you chose and why\",\n"
            f"  \"target_coordinates\": {{\n"
            f"     \"x\": <int_x_coordinate>,\n"
            f"     \"y\": <int_y_coordinate>\n"
            f"  }},\n"
            f"  \"text_value\": \"the string value to type (ONLY if action is 'type')\"\n"
            f"}}\n"
            f"Respond ONLY with this JSON block, no markdown wraps, no extra text."
        )

        # 3. Query local Ollama vision model to make the decision
        image_path = screen_data.get("annotated_screenshot_path") or screen_data.get("screenshot_path")
        raw_response = await call_ollama_vision(prompt, image_path, format_json=True)
        decision = json.loads(raw_response)
        
        action = decision.get("action", "wait")
        explanation = decision.get("explanation", "")
        coords = decision.get("target_coordinates")
        text_value = decision.get("text_value", "")
        
        # 4. Resolve ADB path
        adb_path = shutil.which("adb")
        if not adb_path:
            sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
            adb_path = os.path.join(sdk_root, "platform-tools", "adb")
            if not os.path.exists(adb_path):
                return {"status": "error", "message": "adb binary not found in PATH or Android SDK root"}

        execution_details = {}
        
        # 5. Execute decided action via ADB
        if action == "click" and coords:
            x, y = coords.get("x"), coords.get("y")
            if x is not None and y is not None:
                subprocess.run([adb_path, "shell", "input", "tap", str(x), str(y)], stdout=subprocess.DEVNULL)
                execution_details = {"tapped_coordinates": {"x": x, "y": y}}
                await asyncio.sleep(1.5)
                
        elif action == "type" and coords:
            x, y = coords.get("x"), coords.get("y")
            if x is not None and y is not None and text_value:
                # Tap to focus
                subprocess.run([adb_path, "shell", "input", "tap", str(x), str(y)], stdout=subprocess.DEVNULL)
                await asyncio.sleep(0.5)
                # Clear existing input
                clear_cmd = [adb_path, "shell", "input", "keyevent", "123"] + ["67"] * 40
                subprocess.run(clear_cmd, stdout=subprocess.DEVNULL)
                await asyncio.sleep(0.2)
                # Input new text
                adb_text = str(text_value).replace(" ", "%s").replace('"', '\\"').replace("'", "\\'")
                subprocess.run([adb_path, "shell", "input", "text", adb_text], stdout=subprocess.DEVNULL)
                # Hide keyboard
                subprocess.run([adb_path, "shell", "input", "keyevent", "111"], stdout=subprocess.DEVNULL)
                execution_details = {
                    "typed_coordinates": {"x": x, "y": y},
                    "value_typed": text_value
                }
                await asyncio.sleep(1.5)
                
        elif action == "back":
            subprocess.run([adb_path, "shell", "input", "keyevent", "4"], stdout=subprocess.DEVNULL)
            execution_details = {"keyevent": "BACK (4)"}
            await asyncio.sleep(1.5)
            
        elif action == "wait":
            await asyncio.sleep(3.0)
            execution_details = {"waited": "3.0s"}

        # 6. Capture the screen state AFTER execution to verify the results
        new_screen_data = await capture_screen_hybrid(screen_name=f"{request.screen_name}_after_action")

        return {
            "status": "success",
            "decision": {
                "action": action,
                "explanation": explanation,
                "details": execution_details
            },
            "new_screen_state": {
                "node_id": new_screen_data["node_id"],
                "screen_name": new_screen_data["screen_name"],
                "screenshot_path": new_screen_data["screenshot_path"],
                "annotated_screenshot_path": new_screen_data["annotated_screenshot_path"],
                "visual_elements": new_screen_data["visual_elements"]
            }
        }

    except Exception as e:
        logger.error(f"Error executing step-by-step: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

async def explode_element_by_bounds(bounds_str: str, include_opencv: bool = False) -> list[dict]:
    """
    Takes the bounds of a grouped element, captures the screen, crops it to the bounds,
    runs OCR, and optionally runs OpenCV contour detection to locate non-text visual icons/graphics.
    Returns a list of exploded child components with absolute coordinates.
    """
    import subprocess
    import shutil
    import cv2
    import os
    import re
    
    # 1. Resolve ADB path
    adb_path = shutil.which("adb")
    if not adb_path:
        sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
        adb_path = os.path.join(sdk_root, "platform-tools", "adb")
        if not os.path.exists(adb_path):
            raise Exception("adb binary not found in PATH or Android SDK root")
            
    # 2. Parse bounds string
    match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str.strip())
    if not match:
        raise Exception(f"Invalid bounds format: {bounds_str}. Expected '[x1,y1][x2,y2]'.")
        
    x1, y1, x2, y2 = map(int, match.groups())
    
    # 3. Take a screenshot
    screenshot_filename = "temp_explode.png"
    ss_dir = os.path.join(os.getcwd(), "assets", "screenshots")
    os.makedirs(ss_dir, exist_ok=True)
    local_screenshot_path = os.path.join(ss_dir, screenshot_filename)
    
    # Capture on device
    subprocess.run([adb_path, "shell", "screencap", "-p", f"/sdcard/{screenshot_filename}"], stdout=subprocess.DEVNULL)
    # Pull to local
    subprocess.run([adb_path, "pull", f"/sdcard/{screenshot_filename}", local_screenshot_path], stdout=subprocess.DEVNULL)
    # Clean up on device
    subprocess.run([adb_path, "shell", "rm", f"/sdcard/{screenshot_filename}"], stdout=subprocess.DEVNULL)
    
    if not os.path.exists(local_screenshot_path):
        raise Exception("Failed to capture screenshot from device.")
        
    # 4. Read image and crop it
    img = cv2.imread(local_screenshot_path)
    if img is None:
        raise Exception("Failed to read captured screenshot.")
        
    # Ensure crop coordinates are within image dimensions
    h_img, w_img = img.shape[:2]
    x1, x2 = max(0, min(x1, w_img)), max(0, min(x2, w_img))
    y1, y2 = max(0, min(y1, h_img)), max(0, min(y2, h_img))
    
    cropped_img = img[y1:y2, x1:x2]
    
    # Save cropped screenshot for visual verification
    crop_filename = f"cropped_{x1}_{y1}_{x2}_{y2}.png"
    crop_path = os.path.join(ss_dir, crop_filename)
    cv2.imwrite(crop_path, cropped_img)
    
    # Clean up temp_explode screenshot
    if os.path.exists(local_screenshot_path):
        os.remove(local_screenshot_path)
        
    # 5. Run OCR on cropped image
    # Downscale for 4x speedup
    h_crop, w_crop = cropped_img.shape[:2]
    if h_crop == 0 or w_crop == 0:
        return []
        
    scale = 0.5
    img_resized = cv2.resize(cropped_img, (int(w_crop * scale), int(h_crop * scale)))
    
    reader = get_ocr_reader()
    results = reader.readtext(img_resized)
    
    # 6. Format results into individual child elements
    child_elements = []
    for idx, (bbox, text, confidence) in enumerate(results):
        # Scale coordinates back up and add parent offsets (x1, y1)
        x_coords = [int(p[0] / scale) + x1 for p in bbox]
        y_coords = [int(p[1] / scale) + y1 for p in bbox]
        cx1, cy1 = min(x_coords), min(y_coords)
        cx2, cy2 = max(x_coords), max(y_coords)
        cx = (cx1 + cx2) // 2
        cy = (cy1 + cy2) // 2
        
        # Clean text to make a nice element_id
        clean_id = text.lower()
        clean_id = re.sub(r'[^a-z0-9\s]', '', clean_id)
        clean_id = "_".join(clean_id.split()[:4]) or f"child_{idx}"
        
        child_elements.append({
            "element_id": f"{clean_id}_{idx}",
            "text": text,
            "confidence": round(float(confidence), 3),
            "bounds": f"[{cx1},{cy1}][{cx2},{cy2}]",
            "center": [cx, cy]
        })
        
    # 7. Optionally run OpenCV Contour Detection to locate non-text shapes/icons
    if include_opencv:
        gray = cv2.cvtColor(cropped_img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edged = cv2.Canny(blurred, 50, 150)
        contours, _ = cv2.findContours(edged.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        for idx, c in enumerate(contours):
            rx, ry, rw, rh = cv2.boundingRect(c)
            # Filter out noise (too small) or the entire card border (too big)
            if rw < 15 or rh < 15 or rw > w_crop - 20 or rh > h_crop - 20:
                continue
                
            # Absolute coordinates
            cx1 = rx + x1
            cy1 = ry + y1
            cx2 = rx + rw + x1
            cy2 = ry + rh + y1
            ccx = cx1 + rw // 2
            ccy = cy1 + rh // 2
            
            # Intersection/Overlap check with existing OCR text elements
            is_overlapping = False
            for el in child_elements:
                el_match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', el["bounds"])
                if el_match:
                    ex1, ey1, ex2, ey2 = map(int, el_match.groups())
                    ix1 = max(cx1, ex1)
                    iy1 = max(cy1, ey1)
                    ix2 = min(cx2, ex2)
                    iy2 = min(cy2, ey2)
                    if ix1 < ix2 and iy1 < iy2:
                        is_overlapping = True
                        break
                        
            if not is_overlapping:
                child_elements.append({
                    "element_id": f"graphic_shape_{idx}",
                    "text": "[Icon/Graphic]",
                    "confidence": 1.0,
                    "bounds": f"[{cx1},{cy1}][{cx2},{cy2}]",
                    "center": [ccx, ccy]
                })
                
    return child_elements


class AppCrawlStateManager:
    def __init__(self, state_path: str = "app_crawl_state.json", map_path: str = "app_map.json"):
        import os
        self.state_path = os.path.abspath(state_path)
        self.map_path = os.path.abspath(map_path)
        self.state = {
            "app_package": "",
            "app_activity": "",
            "visited_elements": [],
            "stack": [],
            "nodes": {},
            "last_action": None
        }
        self.load()

    def load(self):
        import json
        import os
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
            except Exception as e:
                logger.error(f"Error loading crawl state: {e}")

    def save(self):
        import json
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving crawl state: {e}")
            
    def reset(self, app_package: str, app_activity: str):
        self.state = {
            "app_package": app_package,
            "app_activity": app_activity,
            "visited_elements": [],
            "stack": [],
            "nodes": {},
            "last_action": None
        }
        self.save()


def get_clickable_elements_for_crawl(xml_text: str) -> list[dict]:
    import xml.etree.ElementTree as ET
    import re
    if not xml_text:
        return []
        
    elements = []
    try:
        root = ET.fromstring(xml_text.strip().encode("utf-8"))
        iterator = root.iter()
    except Exception:
        return []
        
    for idx, node in enumerate(iterator):
        tag_name = node.tag
        if tag_name.lower() in ("?xml", "!doctype", "hierarchy"):
            continue
        attrs = node.attrib
        clickable = attrs.get("clickable", "").strip().lower() == "true"
        focusable = attrs.get("focusable", "").strip().lower() == "true"
        enabled = attrs.get("enabled", "").strip().lower() == "true"
        class_name = attrs.get("class", "").strip() or tag_name
        
        is_input = "EditText" in class_name or class_name.endswith("EditText")
        is_clickable = clickable or focusable
        
        # Filter for clickable/interactive items OR input fields
        if not (is_clickable or is_input) or not enabled:
            continue
            
        # Exclude container views that shouldn't be clicked directly
        ignored_classes = [
            "android.widget.ScrollView",
            "android.widget.HorizontalScrollView",
            "androidx.recyclerview.widget.RecyclerView",
            "android.widget.ListView",
            "android.widget.GridView"
        ]
        if any(ignored in class_name for ignored in ignored_classes):
            continue
            
        resource_id = attrs.get("resource-id", "").strip()
        text = attrs.get("text", "").strip()
        content_desc = attrs.get("content-desc", "").strip()
        bounds = attrs.get("bounds", "").strip()
        
        # Skip standard system status bar
        if "statusbar" in resource_id.lower() or "navigationbar" in resource_id.lower():
            continue
            
        # Build selector
        tag = class_name if class_name else "*"
        if text:
            escaped_text = text.replace('"', '\\"')
            best_selector = f'xpath=//{tag}[@text="{escaped_text}"]'
        elif content_desc:
            best_selector = f"~{content_desc}"
        elif resource_id:
            best_selector = f"id={resource_id}"
        else:
            best_selector = f"xpath=//{tag}[@bounds='{bounds}']"
            
        # Unique element ID
        clean_name = text or content_desc or (resource_id.split("/")[-1] if resource_id else "")
        clean_name = re.sub(r'[^a-zA-Z0-9]', '_', clean_name.lower())
        clean_name = "_".join(clean_name.split()[:3])
        if not clean_name:
            clean_name = f"el_{class_name.split('.')[-1].lower()}"
            
        element_id = f"{clean_name}_{idx}"
        
        elements.append({
            "element_id": element_id,
            "class": class_name,
            "resource_id": resource_id,
            "text": text,
            "content_desc": content_desc,
            "bounds": bounds,
            "selector": best_selector,
            "is_input": is_input
        })
    return elements


def parse_bounds(bounds_str: str) -> list[int] | None:
    import re
    if not bounds_str:
        return None
    match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str.strip())
    if match:
        return list(map(int, match.groups()))
    return None


def sync_to_app_map(state_manager: AppCrawlStateManager):
    import json
    from datetime import datetime
    
    map_data = {
        "app_name": "Android Emulator",
        "package": state_manager.state.get("app_package", "unknown"),
        "crawl_date": datetime.today().strftime('%Y-%m-%d'),
        "total_screens_discovered": len(state_manager.state["nodes"]),
        "steps_taken": len(state_manager.state["visited_elements"]),
        "nodes": []
    }
    
    for fingerprint, node_info in state_manager.state["nodes"].items():
        elements_map = []
        for idx, el in enumerate(node_info["elements"]):
            coords = parse_bounds(el["bounds"])
            cx, cy = 0, 0
            if coords:
                x1, y1, x2, y2 = coords
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                
            elements_map.append({
                "element_id": el["element_id"],
                "bounds": el["bounds"],
                "clickable": True,
                "element_type": el["class"].split(".")[-1],
                "selectors": {
                    "uiautomator": f"new UiSelector().className(\"{el['class']}\")"
                },
                "vision_fallback_coordinates": {
                    "tap_by_ratio": {
                        "x_ratio": round(cx / 1080.0, 4),
                        "y_ratio": round(cy / 1920.0, 4)
                    }
                }
            })
            
        map_data["nodes"].append({
            "node_id": node_info["node_id"],
            "screen_name": node_info["node_id"],
            "screen_type": "General",
            "elements": elements_map,
            "navigation_path": node_info["navigation_path"]
        })
        
    try:
        with open(state_manager.map_path, "w", encoding="utf-8") as f:
            json.dump(map_data, f, indent=2)
    except Exception as e:
        logger.error(f"Error syncing to app_map: {e}")


def rebuild_stack_from_path(state_manager, target_fingerprint: str):
    """Reconstructs the navigation stack dynamically to match a known node's path prefix.
    Prevents stack corruption when the app restarts in a logged-in state or state is restored.
    """
    nodes = state_manager.state.get("nodes", {})
    target_node = nodes.get(target_fingerprint)
    if not target_node:
        return
        
    path = target_node.get("navigation_path", [])
    new_stack = []
    
    # 1. Find the root node (navigation_path = [])
    # If target_node itself has empty path, it is the root
    if not path:
        new_stack.append({
            "node_id": target_node["node_id"],
            "fingerprint": target_fingerprint,
            "navigation_path": []
        })
    else:
        # Otherwise, look for a root node.
        # Prefer the root node that matches the target app package, or just pick the first root node.
        root_node = None
        for fp, node in nodes.items():
            if not node.get("navigation_path"):
                if "launcher" not in node["node_id"]:
                    root_node = node
                    break
        if not root_node:
            for fp, node in nodes.items():
                if not node.get("navigation_path"):
                    root_node = node
                    break
        if root_node:
            new_stack.append({
                "node_id": root_node["node_id"],
                "fingerprint": root_node["fingerprint"],
                "navigation_path": []
            })
            
    if not new_stack:
        return
        
    # 2. Find intermediate nodes by matching prefix paths
    current_path = []
    for action in path:
        current_path.append(action)
        found = False
        for fp, node in nodes.items():
            if node.get("navigation_path") == current_path:
                new_stack.append({
                    "node_id": node["node_id"],
                    "fingerprint": fp,
                    "navigation_path": list(current_path)
                })
                found = True
                break
        if not found:
            new_stack.append({
                "node_id": f"screen_placeholder_{len(current_path)}",
                "fingerprint": "UNKNOWN",
                "navigation_path": list(current_path)
            })
            
    # 3. Ensure target node is at the top with correct fingerprint
    if new_stack and new_stack[-1]["fingerprint"] != target_fingerprint:
        new_stack[-1] = {
            "node_id": target_node["node_id"],
            "fingerprint": target_fingerprint,
            "navigation_path": list(path)
        }
        
    state_manager.state["stack"] = new_stack


async def crawl_step_logic(reset_state: bool = False, app_package: str = None, app_activity: str = None, prefill_data: dict = None) -> dict:
    import subprocess
    import shutil
    import os
    import asyncio
    import json
    import re
    import hashlib
    
    state_manager = AppCrawlStateManager()
    if not reset_state and not app_package and state_manager.state.get("app_package"):
        app_package = state_manager.state.get("app_package")
        app_activity = state_manager.state.get("app_activity")
        
    # 1. Resolve ADB
    adb_path = shutil.which("adb")
    if not adb_path:
        sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
        adb_path = os.path.join(sdk_root, "platform-tools", "adb")
        if not os.path.exists(adb_path):
            raise Exception("adb binary not found in PATH or Android SDK root")
            
    # Auto-detect package and activity if default/not provided or not installed
    is_installed = False
    if app_package and app_package not in ("com.kuberproject", "com.curtain.tracking", "default", ""):
        try:
            check_res = subprocess.run([adb_path, "shell", "pm", "path", app_package], capture_output=True, text=True)
            if "package:" in check_res.stdout:
                is_installed = True
        except Exception:
            pass

    if not is_installed:
        logger.info(f"Target app package '{app_package}' is not specified, not installed, or matches default. Detecting active app on emulator...")
        try:
            focus_res = subprocess.run([adb_path, "shell", "dumpsys", "window", "visible-apps"], capture_output=True, text=True)
            if focus_res.returncode != 0:
                focus_res = subprocess.run([adb_path, "shell", "dumpsys", "window"], capture_output=True, text=True)
            
            focus_out = focus_res.stdout or ""
            match = re.search(r'mCurrentFocus=Window\{[a-fA-F0-9]+\s+\S+\s+([^/\s}]+)/([^}\s]+)\}', focus_out)
            if not match:
                match = re.search(r'mFocusedApp=ActivityRecord\{[a-fA-F0-9]+\s+\S+\s+([^/\s}]+)/([^}\s]+)', focus_out)
            if not match:
                match = re.search(r'([\w\.]+)/([\w\.]+)', focus_out)
                
            if match:
                detected_package = match.group(1).strip()
                detected_activity = match.group(2).strip()
                if "launcher" not in detected_package.lower() and "systemui" not in detected_package.lower():
                    logger.info(f"Auto-detected active app package: '{detected_package}', activity: '{detected_activity}'")
                    app_package = detected_package
                    app_activity = detected_activity
        except Exception as e:
            logger.warning(f"Failed to auto-detect active app package: {e}")

    if not app_package:
        raise Exception("Target app package was not specified and no active third-party app was detected on the emulator. Please make sure the app is running in the foreground.")
        
    if reset_state or not state_manager.state.get("app_package"):
        state_manager.reset(app_package, app_activity)
        
    # Helper to restart app and replay path
    async def restart_and_replay(target_path: list) -> str:
        logger.info(f"Re-navigating by replaying path of length {len(target_path)}...")
        # Kill app
        subprocess.run([adb_path, "shell", "am", "force-stop", app_package], stdout=subprocess.DEVNULL)
        await asyncio.sleep(1.5)
        # Restart app
        if app_activity:
            subprocess.run([adb_path, "shell", "am", "start", "-n", f"{app_package}/{app_activity}"], stdout=subprocess.DEVNULL)
        else:
            subprocess.run([adb_path, "shell", "monkey", "-p", app_package, "-c", "android.intent.category.LAUNCHER", "1"], stdout=subprocess.DEVNULL)
        await asyncio.sleep(4.0)
        
        # Replay each action in path
        for action in target_path:
            action_type = action.get("type")
            selector = action.get("selector", "")
            bounds_str = action.get("bounds", "")
            
            if action_type == "click":
                # Capture current XML
                xml_temp_path = os.path.join(os.getcwd(), "temp_replay_dump.xml")
                subprocess.run([adb_path, "shell", "uiautomator", "dump", "/sdcard/temp_replay_dump.xml"], stdout=subprocess.DEVNULL)
                subprocess.run([adb_path, "pull", "/sdcard/temp_replay_dump.xml", xml_temp_path], stdout=subprocess.DEVNULL)
                subprocess.run([adb_path, "shell", "rm", "/sdcard/temp_replay_dump.xml"], stdout=subprocess.DEVNULL)
                
                clicked = False
                if os.path.exists(xml_temp_path):
                    try:
                        with open(xml_temp_path, "r", encoding="utf-8") as f:
                            xml_text = f.read()
                        os.remove(xml_temp_path)
                        
                        elements = get_clickable_elements_for_crawl(xml_text)
                        for el in elements:
                            if el.get("selector") == selector or el.get("bounds") == bounds_str:
                                coords = parse_bounds(el.get("bounds"))
                                if coords:
                                    x1, y1, x2, y2 = coords
                                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                                    subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
                                    clicked = True
                                    break
                    except Exception:
                        pass
                
                if not clicked and bounds_str:
                    coords = parse_bounds(bounds_str)
                    if coords:
                        x1, y1, x2, y2 = coords
                        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                        subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
                        clicked = True
                        
                await asyncio.sleep(2.0)
            elif action_type == "type":
                value = action.get("value", "")
                # Capture current XML
                xml_temp_path = os.path.join(os.getcwd(), "temp_replay_dump.xml")
                subprocess.run([adb_path, "shell", "uiautomator", "dump", "/sdcard/temp_replay_dump.xml"], stdout=subprocess.DEVNULL)
                subprocess.run([adb_path, "pull", "/sdcard/temp_replay_dump.xml", xml_temp_path], stdout=subprocess.DEVNULL)
                subprocess.run([adb_path, "shell", "rm", "/sdcard/temp_replay_dump.xml"], stdout=subprocess.DEVNULL)
                
                typed = False
                if os.path.exists(xml_temp_path):
                    try:
                        with open(xml_temp_path, "r", encoding="utf-8") as f:
                            xml_text = f.read()
                        os.remove(xml_temp_path)
                        
                        elements = get_clickable_elements_for_crawl(xml_text)
                        for el in elements:
                            if el.get("selector") == selector or el.get("bounds") == bounds_str:
                                coords = parse_bounds(el.get("bounds"))
                                if coords:
                                    x1, y1, x2, y2 = coords
                                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                                    # Tap to focus
                                    subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
                                    await asyncio.sleep(0.5)
                                    # Clear field
                                    subprocess.run([adb_path, "shell", "input", "keyevent", "123"] + ["67"] * 40, stdout=subprocess.DEVNULL)
                                    await asyncio.sleep(0.2)
                                    # Type value
                                    adb_text = str(value).replace(" ", "%s").replace('"', '\\"').replace("'", "\\'")
                                    subprocess.run([adb_path, "shell", "input", "text", adb_text], stdout=subprocess.DEVNULL)
                                    await asyncio.sleep(0.5)
                                    # Hide keyboard
                                    subprocess.run([adb_path, "shell", "input", "keyevent", "111"], stdout=subprocess.DEVNULL)
                                    typed = True
                                    break
                    except Exception:
                        pass
                
                if not typed and bounds_str:
                    coords = parse_bounds(bounds_str)
                    if coords:
                        x1, y1, x2, y2 = coords
                        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                        subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
                        await asyncio.sleep(0.5)
                        subprocess.run([adb_path, "shell", "input", "keyevent", "123"] + ["67"] * 40, stdout=subprocess.DEVNULL)
                        await asyncio.sleep(0.2)
                        adb_text = str(value).replace(" ", "%s").replace('"', '\\"').replace("'", "\\'")
                        subprocess.run([adb_path, "shell", "input", "text", adb_text], stdout=subprocess.DEVNULL)
                        await asyncio.sleep(0.5)
                        subprocess.run([adb_path, "shell", "input", "keyevent", "111"], stdout=subprocess.DEVNULL)
                        typed = True
                        
                await asyncio.sleep(1.5)
        return "SUCCESS"

    # Step 1: Capture current screen XML
    xml_path = os.path.join(os.getcwd(), "temp_crawl_dump.xml")
    dump_res = subprocess.run([adb_path, "shell", "uiautomator", "dump", "/sdcard/temp_crawl_dump.xml"], capture_output=True, text=True)
    if dump_res.returncode != 0:
        return {"status": "error", "message": f"Failed to dump UI hierarchy: {dump_res.stderr}"}
        
    pull_res = subprocess.run([adb_path, "pull", "/sdcard/temp_crawl_dump.xml", xml_path], capture_output=True, text=True)
    if pull_res.returncode != 0:
        return {"status": "error", "message": f"Failed to pull UI hierarchy: {pull_res.stderr}"}
        
    with open(xml_path, "r", encoding="utf-8") as f:
        xml_text = f.read()
    if os.path.exists(xml_path):
        os.remove(xml_path)
    subprocess.run([adb_path, "shell", "rm", "/sdcard/temp_crawl_dump.xml"], stdout=subprocess.DEVNULL)
    
    # Step 2: Fingerprint screen and determine current node ID
    fingerprint = get_screen_fingerprint(xml_text)
    node_id = f"screen_{fingerprint}"
    
    # Auto-detect current active package/activity to detect out-of-bounds transitions
    current_package = app_package
    current_activity = app_activity
    try:
        focus_res = subprocess.run([adb_path, "shell", "dumpsys", "window", "visible-apps"], capture_output=True, text=True)
        if focus_res.returncode != 0:
            focus_res = subprocess.run([adb_path, "shell", "dumpsys", "window"], capture_output=True, text=True)
        focus_out = focus_res.stdout or ""
        match = re.search(r'mCurrentFocus=Window\{[a-fA-F0-9]+\s+\S+\s+([^/\s}]+)/([^}\s]+)\}', focus_out)
        if not match:
            match = re.search(r'mFocusedApp=ActivityRecord\{[a-fA-F0-9]+\s+\S+\s+([^/\s}]+)/([^}\s]+)', focus_out)
        if not match:
            match = re.search(r'([\w\.]+)/([\w\.]+)', focus_out)
        if match:
            current_package = match.group(1).strip()
            current_activity = match.group(2).strip()
    except Exception as e:
        logger.warning(f"Failed to get active package: {e}")

    # Out of bounds check: if we transitioned to a non-target app (e.g. system calendar, settings, launcher)
    if current_package != app_package:
        logger.warning(f"Out of bounds! Current package is '{current_package}', target package is '{app_package}'. attempting recovery...")
        out_of_bounds_count = state_manager.state.get("out_of_bounds_count", 0)
        if out_of_bounds_count >= 2:
            logger.info("Repeatedly out of bounds. Force restarting target app...")
            subprocess.run([adb_path, "shell", "am", "force-stop", app_package], stdout=subprocess.DEVNULL)
            await asyncio.sleep(1.5)
            if app_activity:
                subprocess.run([adb_path, "shell", "am", "start", "-n", f"{app_package}/{app_activity}"], stdout=subprocess.DEVNULL)
            else:
                subprocess.run([adb_path, "shell", "monkey", "-p", app_package, "-c", "android.intent.category.LAUNCHER", "1"], stdout=subprocess.DEVNULL)
            await asyncio.sleep(4.0)
            state_manager.state["out_of_bounds_count"] = 0
            state_manager.save()
            return {
                "status": "recovering",
                "message": f"Out of bounds package '{current_package}' detected repeatedly. Restarted target app.",
                "visited_count": len(state_manager.state["visited_elements"])
            }
        else:
            logger.info("Pressing device back key to recover to target app...")
            subprocess.run([adb_path, "shell", "input", "keyevent", "4"], stdout=subprocess.DEVNULL)
            await asyncio.sleep(1.5)
            state_manager.state["out_of_bounds_count"] = out_of_bounds_count + 1
            state_manager.save()
            return {
                "status": "recovering",
                "message": f"Out of bounds package '{current_package}' detected. Pressed back.",
                "visited_count": len(state_manager.state["visited_elements"])
            }
            
    # Reset out of bounds count if we are in bounds
    if state_manager.state.get("out_of_bounds_count", 0) > 0:
        state_manager.state["out_of_bounds_count"] = 0
        state_manager.save()

    # Dynamic Stack Synchronization:
    # If the screen is already known, rebuild/align the stack to match its navigation path.
    # This heals mismatches caused by session caching, app restarts, or cached stale stacks.
    if fingerprint in state_manager.state.get("nodes", {}):
        rebuild_stack_from_path(state_manager, fingerprint)
        
    if not state_manager.state["stack"]:
        state_manager.state["stack"].append({
            "node_id": node_id,
            "fingerprint": fingerprint,
            "navigation_path": []
        })
        
    current_stack_node = state_manager.state["stack"][-1]
    
    if current_stack_node["fingerprint"] != fingerprint:
        existing_idx = -1
        for i, item in enumerate(state_manager.state["stack"]):
            if item["fingerprint"] == fingerprint:
                existing_idx = i
                break
                
        if existing_idx != -1:
            state_manager.state["stack"] = state_manager.state["stack"][:existing_idx + 1]
            current_stack_node = state_manager.state["stack"][-1]
        else:
            last_action = state_manager.state.get("last_action")
            new_path = list(current_stack_node["navigation_path"])
            if last_action:
                new_path.append(last_action)
                
            state_manager.state["stack"].append({
                "node_id": node_id,
                "fingerprint": fingerprint,
                "navigation_path": new_path
            })
            current_stack_node = state_manager.state["stack"][-1]

    if fingerprint not in state_manager.state["nodes"]:
        elements = get_clickable_elements_for_crawl(xml_text)
        state_manager.state["nodes"][fingerprint] = {
            "node_id": node_id,
            "fingerprint": fingerprint,
            "elements": elements,
            "navigation_path": list(current_stack_node["navigation_path"])
        }
        
    node_data = state_manager.state["nodes"][fingerprint]
    
    # Print beautiful active state summary to terminal console
    print("\n" + "="*70)
    print("                      APP CRAWLER ACTIVE STATE")
    print("="*70)
    print(f"Target Package : {state_manager.state.get('app_package')}")
    print(f"Current Screen : {node_id} (Fingerprint: {fingerprint})")
    print(f"Visited Set    : {len(state_manager.state['visited_elements'])} elements")
    print("\n--- ACTIVE STACK ---")
    for idx, entry in enumerate(state_manager.state["stack"]):
        marker = " -> " if idx == len(state_manager.state["stack"]) - 1 else "    "
        print(f"{marker}[{idx}] {entry['node_id']}")
    
    print("\n--- ELEMENTS ON CURRENT SCREEN ---")
    for el in node_data["elements"]:
        el_id = el["element_id"]
        visited = f"{fingerprint}_{el_id}" in state_manager.state["visited_elements"]
        status_tag = "[X] VISITED  " if visited else "[ ] UNVISITED"
        type_tag = "[INPUT]" if el.get("is_input") else "[CLICK]"
        text_desc = el.get("text") or el.get("content_desc") or (el.get("resource_id").split("/")[-1] if el.get("resource_id") else "") or "[No text/desc]"
        # Escape newlines in text_desc for single line printing
        text_desc = text_desc.replace("\n", " ")
        print(f"  {status_tag} {type_tag} ID: {el_id} | Label: {text_desc} | Bounds: {el['bounds']}")
    print("="*70 + "\n")
    
    # Helper to check if a node fingerprint has unvisited elements
    def has_unvisited_elements(mgr, fp: str) -> bool:
        node = mgr.state.get("nodes", {}).get(fp)
        if not node:
            return False
        for el in node.get("elements", []):
            visited_key = f"{fp}_{el['element_id']}"
            if visited_key not in mgr.state["visited_elements"]:
                return True
        return False

    # 1. Prefill unvisited input fields first
    default_prefills = {
        "phone": "9014969320",
        "email": "test@example.com",
        "username": "Kuber User",
        "password": "Password123",
        "code": "920",
        "search": "920"
    }
    custom_prefills = prefill_data or {}
    prefills = {**default_prefills, **custom_prefills}
    
    unvisited_inputs = [el for el in node_data["elements"] if el.get("is_input") and f"{fingerprint}_{el['element_id']}" not in state_manager.state["visited_elements"]]
    
    if unvisited_inputs:
        target_input = unvisited_inputs[0]
        el_id = target_input["element_id"]
        selector = target_input["selector"]
        bounds_str = target_input["bounds"]
        coords = parse_bounds(bounds_str)
        
        # Determine value to prefill
        label_candidates = [
            target_input.get("text", ""),
            target_input.get("content_desc", ""),
            target_input.get("resource_id", ""),
            target_input.get("resource_id", "").split("/")[-1].lower()
        ]
        
        val_to_type = "920" # Default fallback
        matched = False
        for key, val in prefills.items():
            key_lower = key.lower()
            for cand in label_candidates:
                if cand and key_lower in cand.lower():
                    val_to_type = str(val)
                    matched = True
                    break
            if matched:
                break
                
        visited_key = f"{fingerprint}_{el_id}"
        state_manager.state["visited_elements"].append(visited_key)
        
        action = {
            "type": "type",
            "selector": selector,
            "bounds": bounds_str,
            "value": val_to_type
        }
        state_manager.state["last_action"] = action
        
        if coords:
            x1, y1, x2, y2 = coords
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            # Focus field
            subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
            await asyncio.sleep(0.5)
            # Clear field
            clear_cmd = [adb_path, "shell", "input", "keyevent", "123"] + ["67"] * 40
            subprocess.run(clear_cmd, stdout=subprocess.DEVNULL)
            await asyncio.sleep(0.2)
            # Type value
            adb_text = val_to_type.replace(" ", "%s").replace('"', '\\"').replace("'", "\\'")
            subprocess.run([adb_path, "shell", "input", "text", adb_text], stdout=subprocess.DEVNULL)
            await asyncio.sleep(0.5)
            # Hide keyboard
            subprocess.run([adb_path, "shell", "input", "keyevent", "111"], stdout=subprocess.DEVNULL)
            await asyncio.sleep(0.5)
            action_desc = f"Prefilled input field {el_id} with '{val_to_type}' at ({cx}, {cy})"
        else:
            action_desc = f"Tried prefilling input field {el_id} but bounds parse failed"
            
        state_manager.save()
        sync_to_app_map(state_manager)
        
        return {
            "status": "prefilling",
            "current_screen": node_id,
            "action_taken": action_desc,
            "stack_depth": len(state_manager.state["stack"]),
            "visited_count": len(state_manager.state["visited_elements"])
        }
        
    target_element = None
    for el in node_data["elements"]:
        if el.get("is_input"):
            continue
        visited_key = f"{fingerprint}_{el['element_id']}"
        if visited_key not in state_manager.state["visited_elements"]:
            target_element = el
            break
            
    if target_element:
        el_id = target_element["element_id"]
        selector = target_element["selector"]
        bounds_str = target_element["bounds"]
        coords = parse_bounds(bounds_str)
        
        visited_key = f"{fingerprint}_{el_id}"
        state_manager.state["visited_elements"].append(visited_key)
        
        action = {
            "type": "click",
            "selector": selector,
            "bounds": bounds_str
        }
        state_manager.state["last_action"] = action
        
        if coords:
            x1, y1, x2, y2 = coords
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
            await asyncio.sleep(2.0)
            action_desc = f"Clicked element {el_id} at ({cx}, {cy})"
        else:
            action_desc = f"Tried clicking element {el_id} but bounds parse failed"
            
        state_manager.save()
        sync_to_app_map(state_manager)
        
        return {
            "status": "exploring",
            "current_screen": node_id,
            "action_taken": action_desc,
            "stack_depth": len(state_manager.state["stack"]),
            "visited_count": len(state_manager.state["visited_elements"])
        }
        
    else:
        # No unvisited elements on current screen. Find the first ancestor in the stack that does.
        target_ancestor_idx = -1
        for i in range(len(state_manager.state["stack"]) - 2, -1, -1):
            ancestor = state_manager.state["stack"][i]
            ancestor_fp = ancestor["fingerprint"]
            if has_unvisited_elements(state_manager, ancestor_fp):
                target_ancestor_idx = i
                break
                
        if target_ancestor_idx == -1:
            # All ancestor branches fully crawled
            state_manager.state["stack"] = state_manager.state["stack"][:1]
            state_manager.state["last_action"] = None
            state_manager.save()
            return {
                "status": "completed",
                "message": "All discoverable paths from home screen have been fully crawled.",
                "visited_count": len(state_manager.state["visited_elements"])
            }
            
        popped_nodes = state_manager.state["stack"][target_ancestor_idx + 1:]
        state_manager.state["stack"] = state_manager.state["stack"][:target_ancestor_idx + 1]
        parent_node = state_manager.state["stack"][-1]
        parent_fingerprint = parent_node["fingerprint"]
        
        # Try device back if we only popped one screen
        use_back = (len(popped_nodes) == 1)
        backtrack_success = False
        
        if use_back:
            subprocess.run([adb_path, "shell", "input", "keyevent", "4"], stdout=subprocess.DEVNULL)
            await asyncio.sleep(1.5)
            
            xml_back_path = os.path.join(os.getcwd(), "temp_back_dump.xml")
            subprocess.run([adb_path, "shell", "uiautomator", "dump", "/sdcard/temp_back_dump.xml"], stdout=subprocess.DEVNULL)
            subprocess.run([adb_path, "pull", "/sdcard/temp_back_dump.xml", xml_back_path], stdout=subprocess.DEVNULL)
            subprocess.run([adb_path, "shell", "rm", "/sdcard/temp_back_dump.xml"], stdout=subprocess.DEVNULL)
            
            if os.path.exists(xml_back_path):
                try:
                    with open(xml_back_path, "r", encoding="utf-8") as f:
                        back_xml_text = f.read()
                    os.remove(xml_back_path)
                    
                    new_fingerprint = get_screen_fingerprint(back_xml_text)
                    if new_fingerprint == parent_fingerprint:
                        backtrack_success = True
                except Exception:
                    pass
                    
        if backtrack_success:
            state_manager.state["last_action"] = None
            state_manager.save()
            return {
                "status": "backtracking",
                "current_screen": parent_node["node_id"],
                "action_taken": f"Pressed device back button (Backtrack Success to {parent_node['node_id']})",
                "stack_depth": len(state_manager.state["stack"]),
                "visited_count": len(state_manager.state["visited_elements"])
            }
        else:
            await restart_and_replay(parent_node["navigation_path"])
            state_manager.state["last_action"] = None
            state_manager.save()
            return {
                "status": "backtracking",
                "current_screen": parent_node["node_id"],
                "action_taken": f"App restarted and stack path replayed (Backtrack Self-Healed to {parent_node['node_id']})",
                "stack_depth": len(state_manager.state["stack"]),
                "visited_count": len(state_manager.state["visited_elements"])
            }


@router.post("/crawl_step")
async def crawl_step_endpoint(request: CrawlStepRequest):
    """Exposes the multi-step autonomous crawling logic over a standard FastAPI HTTP POST endpoint.
    This acts as a reliable channel that bypasses strict JSON-RPC client-side timeouts.
    """
    try:
        results = []
        current_reset = request.reset_state
        for step in range(request.max_steps):
            res = await crawl_step_logic(
                reset_state=current_reset,
                app_package=request.app_package,
                app_activity=request.app_activity,
                prefill_data=request.prefill_data
            )
            results.append(res)
            
            if res.get("status") == "completed":
                break
                
            current_reset = False
            if step < request.max_steps - 1:
                await asyncio.sleep(1.2)
                
        return {
            "status": "success",
            "results": results if len(results) > 1 else results[0]
        }
    except Exception as e:
        import traceback
        tb_str = traceback.format_exc()
        logger.error(f"Error during API crawl step: {e}\n{tb_str}")
        return {"status": "error", "message": str(e), "traceback": tb_str}


class CrawlMapAiRequest(BaseModel):
    reset_state: bool = False
    app_package: Optional[str] = None
    app_activity: Optional[str] = None
    prefill_data: Optional[dict] = None
    max_steps: int = 1
    user_prompt: Optional[str] = None

    model_config = {"extra": "allow"}


async def crawl_step_logic_ai(
    reset_state: bool = False,
    app_package: str = None,
    app_activity: str = None,
    prefill_data: dict = None,
    user_prompt: str = None
) -> dict:
    import subprocess
    import shutil
    import os
    import asyncio
    import json
    import re
    import hashlib
    
    state_manager = AppCrawlStateManager()
    if not reset_state and not app_package and state_manager.state.get("app_package"):
        app_package = state_manager.state.get("app_package")
        app_activity = state_manager.state.get("app_activity")
        
    # 1. Resolve ADB
    adb_path = shutil.which("adb")
    if not adb_path:
        sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
        adb_path = os.path.join(sdk_root, "platform-tools", "adb")
        if not os.path.exists(adb_path):
            raise Exception("adb binary not found in PATH or Android SDK root")
            
    # Auto-detect package and activity if default/not provided or not installed
    is_installed = False
    if app_package and app_package not in ("com.kuberproject", "com.curtain.tracking", "default", ""):
        try:
            check_res = subprocess.run([adb_path, "shell", "pm", "path", app_package], capture_output=True, text=True)
            if "package:" in check_res.stdout:
                is_installed = True
        except Exception:
            pass

    if not is_installed:
        logger.info(f"Target app package '{app_package}' is not specified, not installed, or matches default. Detecting active app on emulator...")
        try:
            focus_res = subprocess.run([adb_path, "shell", "dumpsys", "window", "visible-apps"], capture_output=True, text=True)
            if focus_res.returncode != 0:
                focus_res = subprocess.run([adb_path, "shell", "dumpsys", "window"], capture_output=True, text=True)
            
            focus_out = focus_res.stdout or ""
            match = re.search(r'mCurrentFocus=Window\{[a-fA-F0-9]+\s+\S+\s+([^/\s}]+)/([^}\s]+)\}', focus_out)
            if not match:
                match = re.search(r'mFocusedApp=ActivityRecord\{[a-fA-F0-9]+\s+\S+\s+([^/\s}]+)/([^}\s]+)', focus_out)
            if not match:
                match = re.search(r'([\w\.]+)/([\w\.]+)', focus_out)
                
            if match:
                detected_package = match.group(1).strip()
                detected_activity = match.group(2).strip()
                if "launcher" not in detected_package.lower() and "systemui" not in detected_package.lower():
                    logger.info(f"Auto-detected active app package: '{detected_package}', activity: '{detected_activity}'")
                    app_package = detected_package
                    app_activity = detected_activity
        except Exception as e:
            logger.warning(f"Failed to auto-detect active app package: {e}")

    if not app_package:
        raise Exception("Target app package was not specified and no active third-party app was detected on the emulator. Please make sure the app is running in the foreground.")
        
    if reset_state or not state_manager.state.get("app_package"):
        state_manager.reset(app_package, app_activity)
        
    # Helper to restart app and replay path
    async def restart_and_replay(target_path: list) -> str:
        logger.info(f"Re-navigating by replaying path of length {len(target_path)}...")
        # Kill app
        subprocess.run([adb_path, "shell", "am", "force-stop", app_package], stdout=subprocess.DEVNULL)
        await asyncio.sleep(1.5)
        # Restart app
        if app_activity:
            subprocess.run([adb_path, "shell", "am", "start", "-n", f"{app_package}/{app_activity}"], stdout=subprocess.DEVNULL)
        else:
            subprocess.run([adb_path, "shell", "monkey", "-p", app_package, "-c", "android.intent.category.LAUNCHER", "1"], stdout=subprocess.DEVNULL)
        await asyncio.sleep(4.0)
        
        # Replay each action in path
        for action in target_path:
            action_type = action.get("type")
            selector = action.get("selector", "")
            bounds_str = action.get("bounds", "")
            
            if action_type == "click":
                # Capture current XML
                xml_temp_path = os.path.join(os.getcwd(), "temp_replay_dump.xml")
                subprocess.run([adb_path, "shell", "uiautomator", "dump", "/sdcard/temp_replay_dump.xml"], stdout=subprocess.DEVNULL)
                subprocess.run([adb_path, "pull", "/sdcard/temp_replay_dump.xml", xml_temp_path], stdout=subprocess.DEVNULL)
                subprocess.run([adb_path, "shell", "rm", "/sdcard/temp_replay_dump.xml"], stdout=subprocess.DEVNULL)
                
                clicked = False
                if os.path.exists(xml_temp_path):
                    try:
                        with open(xml_temp_path, "r", encoding="utf-8") as f:
                            xml_text = f.read()
                        os.remove(xml_temp_path)
                        
                        elements = get_clickable_elements_for_crawl(xml_text)
                        for el in elements:
                            if el.get("selector") == selector or el.get("bounds") == bounds_str:
                                coords = parse_bounds(el.get("bounds"))
                                if coords:
                                    x1, y1, x2, y2 = coords
                                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                                    subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
                                    clicked = True
                                    break
                    except Exception:
                        pass
                
                if not clicked and bounds_str:
                    coords = parse_bounds(bounds_str)
                    if coords:
                        x1, y1, x2, y2 = coords
                        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                        subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
                        clicked = True
                        
                await asyncio.sleep(2.0)
            elif action_type == "type":
                value = action.get("value", "")
                # Capture current XML
                xml_temp_path = os.path.join(os.getcwd(), "temp_replay_dump.xml")
                subprocess.run([adb_path, "shell", "uiautomator", "dump", "/sdcard/temp_replay_dump.xml"], stdout=subprocess.DEVNULL)
                subprocess.run([adb_path, "pull", "/sdcard/temp_replay_dump.xml", xml_temp_path], stdout=subprocess.DEVNULL)
                subprocess.run([adb_path, "shell", "rm", "/sdcard/temp_replay_dump.xml"], stdout=subprocess.DEVNULL)
                
                typed = False
                if os.path.exists(xml_temp_path):
                    try:
                        with open(xml_temp_path, "r", encoding="utf-8") as f:
                            xml_text = f.read()
                        os.remove(xml_temp_path)
                        
                        elements = get_clickable_elements_for_crawl(xml_text)
                        for el in elements:
                            if el.get("selector") == selector or el.get("bounds") == bounds_str:
                                coords = parse_bounds(el.get("bounds"))
                                if coords:
                                    x1, y1, x2, y2 = coords
                                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                                    # Tap to focus
                                    subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
                                    await asyncio.sleep(0.5)
                                    # Clear field
                                    subprocess.run([adb_path, "shell", "input", "keyevent", "123"] + ["67"] * 40, stdout=subprocess.DEVNULL)
                                    await asyncio.sleep(0.2)
                                    # Type value
                                    adb_text = str(value).replace(" ", "%s").replace('"', '\\"').replace("'", "\\'")
                                    subprocess.run([adb_path, "shell", "input", "text", adb_text], stdout=subprocess.DEVNULL)
                                    await asyncio.sleep(0.5)
                                    # Hide keyboard
                                    subprocess.run([adb_path, "shell", "input", "keyevent", "111"], stdout=subprocess.DEVNULL)
                                    typed = True
                                    break
                    except Exception:
                        pass
                
                if not typed and bounds_str:
                    coords = parse_bounds(bounds_str)
                    if coords:
                        x1, y1, x2, y2 = coords
                        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                        subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
                        await asyncio.sleep(0.5)
                        subprocess.run([adb_path, "shell", "input", "keyevent", "123"] + ["67"] * 40, stdout=subprocess.DEVNULL)
                        await asyncio.sleep(0.2)
                        adb_text = str(value).replace(" ", "%s").replace('"', '\\"').replace("'", "\\'")
                        subprocess.run([adb_path, "shell", "input", "text", adb_text], stdout=subprocess.DEVNULL)
                        await asyncio.sleep(0.5)
                        subprocess.run([adb_path, "shell", "input", "keyevent", "111"], stdout=subprocess.DEVNULL)
                        typed = True
                        
                await asyncio.sleep(1.5)
        return "SUCCESS"

    # Step 1: Capture current screen XML
    xml_path = os.path.join(os.getcwd(), "temp_crawl_dump.xml")
    dump_res = subprocess.run([adb_path, "shell", "uiautomator", "dump", "/sdcard/temp_crawl_dump.xml"], capture_output=True, text=True)
    if dump_res.returncode != 0:
        return {"status": "error", "message": f"Failed to dump UI hierarchy: {dump_res.stderr}"}
        
    pull_res = subprocess.run([adb_path, "pull", "/sdcard/temp_crawl_dump.xml", xml_path], capture_output=True, text=True)
    if pull_res.returncode != 0:
        return {"status": "error", "message": f"Failed to pull UI hierarchy: {pull_res.stderr}"}
        
    with open(xml_path, "r", encoding="utf-8") as f:
        xml_text = f.read()
    if os.path.exists(xml_path):
        os.remove(xml_path)
    subprocess.run([adb_path, "shell", "rm", "/sdcard/temp_crawl_dump.xml"], stdout=subprocess.DEVNULL)
    
    # Step 2: Fingerprint screen and determine current node ID
    fingerprint = get_screen_fingerprint(xml_text)
    node_id = f"screen_{fingerprint}"
    
    # Auto-detect current active package/activity to detect out-of-bounds transitions
    current_package = app_package
    current_activity = app_activity
    try:
        focus_res = subprocess.run([adb_path, "shell", "dumpsys", "window", "visible-apps"], capture_output=True, text=True)
        if focus_res.returncode != 0:
            focus_res = subprocess.run([adb_path, "shell", "dumpsys", "window"], capture_output=True, text=True)
        focus_out = focus_res.stdout or ""
        match = re.search(r'mCurrentFocus=Window\{[a-fA-F0-9]+\s+\S+\s+([^/\s}]+)/([^}\s]+)\}', focus_out)
        if not match:
            match = re.search(r'mFocusedApp=ActivityRecord\{[a-fA-F0-9]+\s+\S+\s+([^/\s}]+)/([^}\s]+)', focus_out)
        if not match:
            match = re.search(r'([\w\.]+)/([\w\.]+)', focus_out)
        if match:
            current_package = match.group(1).strip()
            current_activity = match.group(2).strip()
    except Exception as e:
        logger.warning(f"Failed to get active package: {e}")

    # Out of bounds check: if we transitioned to a non-target app
    if current_package != app_package:
        logger.warning(f"Out of bounds! Current package is '{current_package}', target package is '{app_package}'. attempting recovery...")
        out_of_bounds_count = state_manager.state.get("out_of_bounds_count", 0)
        if out_of_bounds_count >= 2:
            logger.info("Repeatedly out of bounds. Force restarting target app...")
            subprocess.run([adb_path, "shell", "am", "force-stop", app_package], stdout=subprocess.DEVNULL)
            await asyncio.sleep(1.5)
            if app_activity:
                subprocess.run([adb_path, "shell", "am", "start", "-n", f"{app_package}/{app_activity}"], stdout=subprocess.DEVNULL)
            else:
                subprocess.run([adb_path, "shell", "monkey", "-p", app_package, "-c", "android.intent.category.LAUNCHER", "1"], stdout=subprocess.DEVNULL)
            await asyncio.sleep(4.0)
            state_manager.state["out_of_bounds_count"] = 0
            state_manager.save()
            return {
                "status": "recovering",
                "message": f"Out of bounds package '{current_package}' detected repeatedly. Restarted target app.",
                "visited_count": len(state_manager.state["visited_elements"])
            }
        else:
            logger.info("Pressing device back key to recover to target app...")
            subprocess.run([adb_path, "shell", "input", "keyevent", "4"], stdout=subprocess.DEVNULL)
            await asyncio.sleep(1.5)
            state_manager.state["out_of_bounds_count"] = out_of_bounds_count + 1
            state_manager.save()
            return {
                "status": "recovering",
                "message": f"Out of bounds package '{current_package}' detected. Pressed back.",
                "visited_count": len(state_manager.state["visited_elements"])
            }
            
    # Reset out of bounds count if we are in bounds
    if state_manager.state.get("out_of_bounds_count", 0) > 0:
        state_manager.state["out_of_bounds_count"] = 0
        state_manager.save()

    # Dynamic Stack Synchronization
    if fingerprint in state_manager.state.get("nodes", {}):
        rebuild_stack_from_path(state_manager, fingerprint)
        
    if not state_manager.state["stack"]:
        state_manager.state["stack"].append({
            "node_id": node_id,
            "fingerprint": fingerprint,
            "navigation_path": []
        })
        
    current_stack_node = state_manager.state["stack"][-1]
    
    if current_stack_node["fingerprint"] != fingerprint:
        existing_idx = -1
        for i, item in enumerate(state_manager.state["stack"]):
            if item["fingerprint"] == fingerprint:
                existing_idx = i
                break
                
        if existing_idx != -1:
            state_manager.state["stack"] = state_manager.state["stack"][:existing_idx + 1]
            current_stack_node = state_manager.state["stack"][-1]
        else:
            last_action = state_manager.state.get("last_action")
            new_path = list(current_stack_node["navigation_path"])
            if last_action:
                new_path.append(last_action)
                
            state_manager.state["stack"].append({
                "node_id": node_id,
                "fingerprint": fingerprint,
                "navigation_path": new_path
            })
            current_stack_node = state_manager.state["stack"][-1]

    if fingerprint not in state_manager.state["nodes"]:
        elements = get_clickable_elements_for_crawl(xml_text)
        state_manager.state["nodes"][fingerprint] = {
            "node_id": node_id,
            "fingerprint": fingerprint,
            "elements": elements,
            "navigation_path": list(current_stack_node["navigation_path"])
        }
        
    node_data = state_manager.state["nodes"][fingerprint]
    
    # Helper to check if a node fingerprint has unvisited elements
    def has_unvisited_elements(mgr, fp: str) -> bool:
        node = mgr.state.get("nodes", {}).get(fp)
        if not node:
            return False
        for el in node.get("elements", []):
            visited_key = f"{fp}_{el['element_id']}"
            if visited_key not in mgr.state["visited_elements"]:
                return True
        return False

    # Print beautiful active state summary to terminal console
    print("\n" + "="*70)
    print("                   APP CRAWLER AI ACTIVE STATE")
    print("="*70)
    print(f"Target Package : {state_manager.state.get('app_package')}")
    print(f"Current Screen : {node_id} (Fingerprint: {fingerprint})")
    print(f"Visited Set    : {len(state_manager.state['visited_elements'])} elements")
    print("\n--- ACTIVE STACK ---")
    for idx, entry in enumerate(state_manager.state["stack"]):
        marker = " -> " if idx == len(state_manager.state["stack"]) - 1 else "    "
        print(f"{marker}[{idx}] {entry['node_id']}")
    print("="*70 + "\n")

    # Ask the AI to choose the next best step to explore
    formatted_els = []
    for el in node_data["elements"]:
        el_id = el["element_id"]
        visited = f"{fingerprint}_{el_id}" in state_manager.state["visited_elements"]
        formatted_els.append({
            "element_id": el_id,
            "type": "input field" if el.get("is_input") else "clickable",
            "text": el.get("text") or "",
            "content_description": el.get("content_desc") or "",
            "resource_id": el.get("resource_id") or "",
            "visited_status": "ALREADY VISITED" if visited else "UNVISITED"
        })
        
    ai_prompt = (
        "You are an AI mobile app crawling agent decision maker.\n"
        f"We are crawling the Android application package: '{app_package}'.\n"
        f"Current Screen Node ID: '{node_id}' (Fingerprint: {fingerprint})\n"
    )
    if user_prompt:
        ai_prompt += f"User's goal/guidance: '{user_prompt}'\n"
        
    ai_prompt += (
        "\nHere are the interactable elements currently visible on the screen:\n"
        f"{json.dumps(formatted_els, indent=2)}\n\n"
        "Please choose the next best step to explore the application.\n"
        "Guidelines:\n"
        "1. Prioritize UNVISITED elements to discover new screens and paths.\n"
        "2. If you choose an input field, select action 'type' and provide a realistic value (e.g. valid phone, email, username) in 'type_value'.\n"
        "3. Avoid clicking 'logout', 'exit', or destructive buttons unless there are no other unvisited elements.\n"
        "4. If all elements on this screen are ALREADY VISITED, or there are no useful actions, choose action 'back' to backtrack.\n"
        "5. If the app is stuck or in an error state, choose action 'restart' or 'back'.\n"
        "6. If you believe all discoverable parts of the app have been fully explored, choose action 'completed'.\n"
        "\n"
        "Respond ONLY with a JSON object matching this schema:\n"
        "{\n"
        "  \"action\": \"click\" | \"type\" | \"back\" | \"restart\" | \"completed\",\n"
        "  \"target_id\": \"element_id of the chosen target element (required for click and type)\",\n"
        "  \"type_value\": \"text to enter (required if action is type)\",\n"
        "  \"reason\": \"brief explanation of why this action was chosen\"\n"
        "}\n"
        "Do not include any extra explanation or markdown block markers. Just return the JSON object."
    )

    action = "back"
    target_id = None
    type_value = None
    reason = "Fallback"
    
    try:
        raw_resp = await call_llm_generate(ai_prompt, format_json=True)
        cleaned_resp = raw_resp.strip()
        if "```json" in cleaned_resp:
            cleaned_resp = cleaned_resp.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in cleaned_resp:
            cleaned_resp = cleaned_resp.split("```", 1)[1].split("```", 1)[0].strip()
            
        res_json = json.loads(cleaned_resp)
        action = res_json.get("action", "back").lower().strip()
        target_id = res_json.get("target_id")
        type_value = res_json.get("type_value")
        reason = res_json.get("reason", "")
        logger.info(f"AI Decision: action={action}, target_id={target_id}, reason={reason}")
    except Exception as e:
        logger.warning(f"Failed to parse AI decision: {e}. Falling back to heuristics.")
        action = "heuristic_fallback"

    # Action Execution Route
    if action == "completed":
        state_manager.state["stack"] = state_manager.state["stack"][:1]
        state_manager.state["last_action"] = None
        state_manager.save()
        return {
            "status": "completed",
            "message": f"AI declared exploration completed. Reason: {reason}",
            "visited_count": len(state_manager.state["visited_elements"])
        }
        
    elif action == "restart":
        subprocess.run([adb_path, "shell", "am", "force-stop", app_package], stdout=subprocess.DEVNULL)
        await asyncio.sleep(1.5)
        if app_activity:
            subprocess.run([adb_path, "shell", "am", "start", "-n", f"{app_package}/{app_activity}"], stdout=subprocess.DEVNULL)
        else:
            subprocess.run([adb_path, "shell", "monkey", "-p", app_package, "-c", "android.intent.category.LAUNCHER", "1"], stdout=subprocess.DEVNULL)
        await asyncio.sleep(4.0)
        state_manager.state["last_action"] = None
        state_manager.save()
        return {
            "status": "restarting",
            "current_screen": node_id,
            "action_taken": f"Forced restart via AI request. Reason: {reason}",
            "stack_depth": len(state_manager.state["stack"]),
            "visited_count": len(state_manager.state["visited_elements"])
        }

    target_element = None
    if action in ("click", "type") and target_id:
        for el in node_data["elements"]:
            if el["element_id"] == target_id:
                target_element = el
                break

    # Fallback to heuristics if AI target is invalid or missing
    if (action in ("click", "type")) and not target_element:
        logger.warning(f"AI target_id '{target_id}' was invalid. Falling back to heuristic selection.")
        unvisited_inputs = [el for el in node_data["elements"] if el.get("is_input") and f"{fingerprint}_{el['element_id']}" not in state_manager.state["visited_elements"]]
        if unvisited_inputs:
            target_element = unvisited_inputs[0]
            action = "type"
            type_value = "920"
        else:
            for el in node_data["elements"]:
                if el.get("is_input"):
                    continue
                visited_key = f"{fingerprint}_{el['element_id']}"
                if visited_key not in state_manager.state["visited_elements"]:
                    target_element = el
                    action = "click"
                    break

    if not target_element and action in ("click", "type"):
        action = "back"

    if action == "click" and target_element:
        el_id = target_element["element_id"]
        selector = target_element["selector"]
        bounds_str = target_element["bounds"]
        coords = parse_bounds(bounds_str)
        
        visited_key = f"{fingerprint}_{el_id}"
        state_manager.state["visited_elements"].append(visited_key)
        
        state_action = {
            "type": "click",
            "selector": selector,
            "bounds": bounds_str
        }
        state_manager.state["last_action"] = state_action
        
        if coords:
            x1, y1, x2, y2 = coords
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
            await asyncio.sleep(2.0)
            action_desc = f"Clicked element {el_id} at ({cx}, {cy}). Reason: {reason}"
        else:
            action_desc = f"Tried clicking element {el_id} but bounds parse failed"
            
        state_manager.save()
        sync_to_app_map(state_manager)
        
        return {
            "status": "exploring",
            "current_screen": node_id,
            "action_taken": action_desc,
            "stack_depth": len(state_manager.state["stack"]),
            "visited_count": len(state_manager.state["visited_elements"])
        }

    elif action == "type" and target_element:
        el_id = target_element["element_id"]
        selector = target_element["selector"]
        bounds_str = target_element["bounds"]
        coords = parse_bounds(bounds_str)
        
        val_to_type = type_value or "920"
        
        visited_key = f"{fingerprint}_{el_id}"
        state_manager.state["visited_elements"].append(visited_key)
        
        state_action = {
            "type": "type",
            "selector": selector,
            "bounds": bounds_str,
            "value": val_to_type
        }
        state_manager.state["last_action"] = state_action
        
        if coords:
            x1, y1, x2, y2 = coords
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            subprocess.run([adb_path, "shell", "input", "tap", str(cx), str(cy)], stdout=subprocess.DEVNULL)
            await asyncio.sleep(0.5)
            clear_cmd = [adb_path, "shell", "input", "keyevent", "123"] + ["67"] * 40
            subprocess.run(clear_cmd, stdout=subprocess.DEVNULL)
            await asyncio.sleep(0.2)
            adb_text = str(val_to_type).replace(" ", "%s").replace('"', '\\"').replace("'", "\\'")
            subprocess.run([adb_path, "shell", "input", "text", adb_text], stdout=subprocess.DEVNULL)
            await asyncio.sleep(0.5)
            subprocess.run([adb_path, "shell", "input", "keyevent", "111"], stdout=subprocess.DEVNULL)
            await asyncio.sleep(0.5)
            action_desc = f"Typed '{val_to_type}' into field {el_id} at ({cx}, {cy}). Reason: {reason}"
        else:
            action_desc = f"Tried typing into {el_id} but bounds parse failed"
            
        state_manager.save()
        sync_to_app_map(state_manager)
        
        return {
            "status": "prefilling",
            "current_screen": node_id,
            "action_taken": action_desc,
            "stack_depth": len(state_manager.state["stack"]),
            "visited_count": len(state_manager.state["visited_elements"])
        }

    else:
        # Action is 'back' or fallback 'back'
        target_ancestor_idx = -1
        for i in range(len(state_manager.state["stack"]) - 2, -1, -1):
            ancestor = state_manager.state["stack"][i]
            ancestor_fp = ancestor["fingerprint"]
            if has_unvisited_elements(state_manager, ancestor_fp):
                target_ancestor_idx = i
                break
                
        if target_ancestor_idx == -1:
            state_manager.state["stack"] = state_manager.state["stack"][:1]
            state_manager.state["last_action"] = None
            state_manager.save()
            return {
                "status": "completed",
                "message": f"All discoverable paths from home screen have been fully crawled. Reason: {reason}",
                "visited_count": len(state_manager.state["visited_elements"])
            }
            
        popped_nodes = state_manager.state["stack"][target_ancestor_idx + 1:]
        state_manager.state["stack"] = state_manager.state["stack"][:target_ancestor_idx + 1]
        parent_node = state_manager.state["stack"][-1]
        parent_fingerprint = parent_node["fingerprint"]
        
        use_back = (len(popped_nodes) == 1)
        backtrack_success = False
        
        if use_back:
            subprocess.run([adb_path, "shell", "input", "keyevent", "4"], stdout=subprocess.DEVNULL)
            await asyncio.sleep(1.5)
            
            xml_back_path = os.path.join(os.getcwd(), "temp_back_dump.xml")
            subprocess.run([adb_path, "shell", "uiautomator", "dump", "/sdcard/temp_back_dump.xml"], stdout=subprocess.DEVNULL)
            subprocess.run([adb_path, "pull", "/sdcard/temp_back_dump.xml", xml_back_path], stdout=subprocess.DEVNULL)
            subprocess.run([adb_path, "shell", "rm", "/sdcard/temp_back_dump.xml"], stdout=subprocess.DEVNULL)
            
            if os.path.exists(xml_back_path):
                try:
                    with open(xml_back_path, "r", encoding="utf-8") as f:
                        back_xml_text = f.read()
                    os.remove(xml_back_path)
                    
                    new_fingerprint = get_screen_fingerprint(back_xml_text)
                    if new_fingerprint == parent_fingerprint:
                        backtrack_success = True
                except Exception:
                    pass
                    
        if backtrack_success:
            state_manager.state["last_action"] = None
            state_manager.save()
            return {
                "status": "backtracking",
                "current_screen": parent_node["node_id"],
                "action_taken": f"AI backtrack: Pressed device back button (Backtrack Success to {parent_node['node_id']}). Reason: {reason}",
                "stack_depth": len(state_manager.state["stack"]),
                "visited_count": len(state_manager.state["visited_elements"])
            }
        else:
            await restart_and_replay(parent_node["navigation_path"])
            state_manager.state["last_action"] = None
            state_manager.save()
            return {
                "status": "backtracking",
                "current_screen": parent_node["node_id"],
                "action_taken": f"AI backtrack: App restarted and stack path replayed (Backtrack Self-Healed to {parent_node['node_id']}). Reason: {reason}",
                "stack_depth": len(state_manager.state["stack"]),
                "visited_count": len(state_manager.state["visited_elements"])
            }


@router.post("/crawl_map_ai")
async def crawl_map_ai_endpoint(request: CrawlMapAiRequest):
    """Exposes the AI-decision driven mobile app crawler over a FastAPI HTTP POST endpoint."""
    try:
        results = []
        current_reset = request.reset_state
        for step in range(request.max_steps):
            res = await crawl_step_logic_ai(
                reset_state=current_reset,
                app_package=request.app_package,
                app_activity=request.app_activity,
                prefill_data=request.prefill_data,
                user_prompt=request.user_prompt
            )
            results.append(res)
            
            if res.get("status") == "completed":
                break
                
            current_reset = False
            if step < request.max_steps - 1:
                await asyncio.sleep(1.2)
                
        return {
            "status": "success",
            "results": results if len(results) > 1 else results[0]
        }
    except Exception as e:
        import traceback
        tb_str = traceback.format_exc()
        logger.error(f"Error during API AI crawl map step: {e}\n{tb_str}")
        return {"status": "error", "message": str(e), "traceback": tb_str}

