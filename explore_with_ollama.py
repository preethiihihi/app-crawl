import asyncio
import logging
import httpx
import json
import re
import os
import hashlib
from datetime import datetime
from typing import Optional
from appium_service import AppiumMcpClient, ensure_appium_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("explore_with_ollama")

# Global set to track visited screen fingerprints to prevent loops
visited_screens = set()

# Prefill values for common input fields to bypass validation gates
PREFILL_INPUTS = {
    "email": "jyoti.singh+11@tudip.com",
    "password": "Tudip@123",
    "phone": "+1234567890",
    "search": "curtains",
    "name": "QA Tester"
}

def get_screen_fingerprint(xml_text: str) -> str:
    """Generates an MD5 fingerprint by removing dynamic attributes from screen XML source."""
    if not xml_text:
        return "EMPTY"
    # Clean dynamic fields to get stable structure
    cleaned = xml_text
    cleaned = re.sub(r'text="[0-9]{1,2}:[0-9]{2}"', 'text=""', cleaned)  # strip time
    # Strip text attribute from EditText elements tag-by-tag to make fingerprint immune to typed input values
    def clean_tag(match):
        tag_content = match.group(0)
        if "EditText" in tag_content:
            return re.sub(r'\btext="[^"]*"', 'text=""', tag_content)
        return tag_content
    cleaned = re.sub(r'<[^>]+>', clean_tag, cleaned)
    cleaned = re.sub(r'selected="[^"]*"', '', cleaned)
    cleaned = re.sub(r'focused="[^"]*"', '', cleaned)
    cleaned = re.sub(r'checked="[^"]*"', '', cleaned)
    return hashlib.md5(cleaned.encode("utf-8")).hexdigest()

visited_nodes = set()

def register_new_node(node_id: str, screen_name: str) -> str:
    """Registers a node state and returns 'NEW' or 'DUPLICATE'."""
    if node_id in visited_nodes:
        return "DUPLICATE"
    visited_nodes.add(node_id)
    return "NEW"

async def get_compressed_page_source(client) -> str:
    """Fetches XML layout source using optimized compressed mobile: source script command, falling back to standard page source."""
    try:
        res = await client.call_tool("execute_script", {
            "script": "mobile: source",
            "args": [{"format": "xml", "compressed": True}]
        })
        live_xml = ""
        for item in res.get("content", []):
            if item.get("type") == "text":
                live_xml += item.get("text", "")
        if live_xml and not ("Error" in live_xml or "WebDriverError" in live_xml) and live_xml.strip().startswith("<"):
            return live_xml
    except Exception as e:
        logger.warning(f"execute_script(mobile: source) failed, falling back to get_page_source: {e}")
    
    # Fallback to get_page_source
    res = await client.call_tool("get_page_source", {})
    live_xml = ""
    for item in res.get("content", []):
        if item.get("type") == "text":
            live_xml += item.get("text", "")
    return live_xml

def append_telemetry(event_type: str, data: dict):
    """Appends crawl events to telemetry.jsonl in real-time."""
    try:
        record = {
            "timestamp": datetime.now().isoformat(),
            "event": event_type,
            **data
        }
        telemetry_path = os.path.join(os.getcwd(), "telemetry.jsonl")
        with open(telemetry_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.error(f"Failed to append to telemetry.jsonl: {e}")

async def save_screenshot(client, node_id: str) -> str:
    """Takes a screenshot and saves it as assets/screenshots/{node_id}.png."""
    try:
        import base64
        ss_dir = os.path.join(os.getcwd(), "assets", "screenshots")
        os.makedirs(ss_dir, exist_ok=True)
        screenshot_path = f"assets/screenshots/{node_id}.png"
        full_path = os.path.join(os.getcwd(), screenshot_path)
        
        res = await client.call_tool("take_screenshot", {})
        content = res.get("content", [])
        img_data = ""
        for item in content:
            if item.get("type") == "text":
                img_data += item.get("text", "")
                
        if "," in img_data:
            img_data = img_data.split(",", 1)[-1]
            
        if img_data:
            with open(full_path, "wb") as f:
                f.write(base64.b64decode(img_data))
            return screenshot_path
    except Exception as e:
        logger.warning(f"Failed to save screenshot: {e}")
    return "N/A"

async def handle_login_bypass(client, app_package: str):
    """Bypasses login validation barriers using the specified credentials."""
    try:
        live_xml = await get_compressed_page_source(client)
        if not live_xml:
            return
            
        elements = filter_xml_to_elements(live_xml)
        
        # Check for onboarding / permission skip/next/agree buttons first
        for el in elements:
            text_desc = (el.get("text", "") + " " + el.get("content_desc", "") + " " + el.get("resource_id", "")).lower()
            if any(kw in text_desc for kw in ["skip", "next", "get started", "agree", "accept", "allow"]):
                logger.info(f"⏭️ [ONBOARDING] Clicking onboarding/permission button: {text_desc.strip()}...")
                await client.call_tool("click_element", {"selector": el["selector"]})
                await asyncio.sleep(2.5)
                # Re-fetch page source
                live_xml = await get_compressed_page_source(client)
                if live_xml:
                    elements = filter_xml_to_elements(live_xml)
                break

        phone_input = None
        otp_input = None
        login_btn = None
        
        for el in elements:
            text_desc = (el.get("text", "") + " " + el.get("content_desc", "") + " " + el.get("resource_id", "")).lower()
            if el.get("is_input") or "edittext" in el.get("class", "").lower():
                if "phone" in text_desc or "mobile" in text_desc or "number" in text_desc:
                    phone_input = el
                elif "otp" in text_desc or "verification" in text_desc or "code" in text_desc:
                    otp_input = el
                elif not phone_input:
                    phone_input = el
                elif not otp_input:
                    otp_input = el
            elif "button" in el.get("class", "").lower() or el.get("clickable"):
                if any(kw in text_desc for kw in ["login", "sign", "submit", "continue", "verify", "otp"]):
                    login_btn = el
                    
        if phone_input:
            logger.info(f"🔑 [LOGIN BYPASS] Located phone input field. Entering credential...")
            await client.call_tool("enter_text", {"selector": phone_input["selector"], "text": "9014969320"})
            await asyncio.sleep(1.5)
            try:
                await client.call_tool("hide_keyboard", {})
            except Exception:
                pass
                
        if otp_input:
            logger.info(f"🔑 [LOGIN BYPASS] Located OTP input field. Entering credential...")
            await client.call_tool("enter_text", {"selector": otp_input["selector"], "text": "1000"})
            await asyncio.sleep(1.5)
            try:
                await client.call_tool("hide_keyboard", {})
            except Exception:
                pass
                
        if login_btn:
            logger.info(f"🔑 [LOGIN BYPASS] Clicking submit/login button...")
            await client.call_tool("click_element", {"selector": login_btn["selector"]})
            await asyncio.sleep(4.0)
            
            post_xml = await get_compressed_page_source(client)
            if post_xml:
                elements_now = filter_xml_to_elements(post_xml)
                for el in elements_now:
                    text_desc = (el.get("text", "") + " " + el.get("content_desc", "") + " " + el.get("resource_id", "")).lower()
                    if (el.get("is_input") or "edittext" in el.get("class", "").lower()) and ("otp" in text_desc or "code" in text_desc or "verification" in text_desc):
                        logger.info(f"🔑 [LOGIN BYPASS] Located OTP verification input. Entering OTP credential...")
                        await client.call_tool("enter_text", {"selector": el["selector"], "text": "1000"})
                        await asyncio.sleep(1.5)
                        try:
                            await client.call_tool("hide_keyboard", {})
                        except Exception:
                            pass
                        
                        for btn in elements_now:
                            btn_text_desc = (btn.get("text", "") + " " + btn.get("content_desc", "") + " " + btn.get("resource_id", "")).lower()
                            if ("button" in btn.get("class", "").lower() or btn.get("clickable")) and any(kw in btn_text_desc for kw in ["verify", "login", "submit", "continue"]):
                                logger.info(f"🔑 [LOGIN BYPASS] Clicking verify/login button...")
                                await client.call_tool("click_element", {"selector": btn["selector"]})
                                await asyncio.sleep(4.0)
                                break
                        break
    except Exception as e:
        logger.warning(f"Failed during login bypass execution: {e}")

def save_app_map(discovered_nodes: dict, history: list, app_package: str, device_name: str):
    """Saves/updates the app map incrementally to the workspace."""
    try:
        app_map = {
            "app_name": device_name,
            "package": app_package,
            "crawl_date": datetime.now().strftime("%Y-%m-%d"),
            "total_screens_discovered": len(discovered_nodes),
            "steps_taken": len(history),
            "nodes": list(discovered_nodes.values()),
            "history": history
        }
        map_path = os.path.join(os.getcwd(), "app_map.json")
        with open(map_path, "w") as f:
            json.dump(app_map, f, indent=2)
        logger.info(f"💾 [MAP SAVE] Simultaneously saved/updated app_map.json ({len(discovered_nodes)} screens, {len(history)} steps)")
    except Exception as e:
        logger.error(f"Failed to save app map simultaneously: {e}")

def filter_xml_to_elements(xml_text: str) -> list:
    """Parses layout XML and extracts interactable, non-duplicate nodes."""
    if not xml_text:
        return []
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text.strip().encode("utf-8"))
        parent_map = {c: p for p in root.iter() for c in p}
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
            class_name = attrs.get("class", tag).strip()
            bounds = attrs.get("bounds", "").strip()
            
            is_input = "EditText" in class_name or class_name.endswith("EditText")
            
            if clickable or focusable or is_input:
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
                    "clickable": clickable or focusable,
                    "is_input": is_input,
                    "bounds": bounds,
                    "selector": best_selector
                })
        return elements

    elements = []
    for node in root.iter():
        attrs = node.attrib
        clickable = attrs.get("clickable", "").strip().lower() == "true"
        focusable = attrs.get("focusable", "").strip().lower() == "true"
        resource_id = attrs.get("resource-id", "").strip()
        text = attrs.get("text", "").strip()
        content_desc = attrs.get("content-desc", "").strip()
        class_name = attrs.get("class", "").strip()
        bounds = attrs.get("bounds", "").strip()
        
        is_input = "EditText" in class_name or class_name.endswith("EditText")
        
        # Check if the element itself is interactable or has any clickable/focusable ancestor
        is_interactable = clickable or focusable or is_input
        if not is_interactable:
            curr = parent_map.get(node)
            while curr is not None:
                c_attrs = curr.attrib
                if c_attrs.get("clickable", "").strip().lower() == "true" or c_attrs.get("focusable", "").strip().lower() == "true":
                    is_interactable = True
                    break
                curr = parent_map.get(curr)
                
        if not is_interactable:
            continue
            
        # De-duplicate: if this node is a container with no text/desc, and it has children that are descriptive, ignore it.
        has_descriptive_children = False
        if not text and not content_desc:
            for child in node.iter():
                if child == node:
                    continue
                if child.attrib.get("text", "").strip() or child.attrib.get("content-desc", "").strip():
                    has_descriptive_children = True
                    break
        if has_descriptive_children:
            continue
            
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

def make_element_id(text: str, desc: str, resource_id: str, tag: str, index: int) -> str:
    """Generates a clean snake_case id for an element."""
    if resource_id and "/" in resource_id:
        res_suffix = resource_id.split("/")[-1]
        cleaned = re.sub(r'[^a-zA-Z0-9_]', '', res_suffix)
        if cleaned:
            return cleaned.lower()
    
    source_str = text or desc
    if source_str:
        cleaned = source_str.lower()
        cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', cleaned)
        words = cleaned.split()
        if words:
            return "_".join(words[:4])
            
    tag_clean = tag.split('.')[-1] if tag else "element"
    return f"{tag_clean.lower()}_{index}"

def build_selectors(text: str, desc: str, class_name: str, resource_id: str) -> dict:
    """Builds the selectors dictionary requested."""
    sels = {}
    if desc:
        sels["accessibility_id"] = desc
        sels["uiautomator"] = f'new UiSelector().description("{desc}")'
    if text:
        sels["text_locator"] = text
        if "uiautomator" not in sels:
            sels["uiautomator"] = f'new UiSelector().text("{text}")'
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
        index=idx
    )
    
    selectors = build_selectors(
        text=el.get("text", ""),
        desc=el.get("content_desc", ""),
        class_name=el.get("class", ""),
        resource_id=el.get("resource_id", "")
    )
    
    result = {
        "element_id": element_id,
        "bounds": bounds,
        "clickable": el.get("clickable", True),
        "element_type": element_type,
        "selectors": selectors
    }
    if fallback_coords:
        result["vision_fallback_coordinates"] = fallback_coords
    return result

async def classify_screen_with_ai(elements: list, current_screen_name: str) -> tuple:
    """Queries Ollama to get user-friendly screen_name and screen_type dynamically, falling back to heuristics."""
    if not elements:
        return current_screen_name, "General"
    
    clean_elements = []
    for idx, el in enumerate(elements[:15]):
        clean_elements.append({
            "text": el.get("text", ""),
            "desc": el.get("content_desc", ""),
            "class": el.get("class", "").split('.')[-1]
        })
        
    prompt = (
        f"We are crawling an Android application.\n"
        f"We just navigated to a screen. Technical placeholder name: '{current_screen_name}'.\n"
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
        res = await call_ollama_json(prompt)
        s_name = res.get("screen_name")
        s_type = res.get("screen_type")
        if s_name and s_type:
            return s_name, s_type
    except Exception:
        pass
    
    # Heuristic fallback if AI fails
    header_text = ""
    for el in elements[:5]:
        if el.get("text") and len(el.get("text").strip()) < 30:
            header_text = el.get("text").strip()
            break
            
    if header_text:
        lower_name = header_text.lower()
        screen_type = "General"
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
        return header_text, screen_type
        
    lower_name = current_screen_name.lower()
    if "setting" in lower_name:
        return "Settings", "Settings"
    elif "dashboard" in lower_name or "home" in lower_name or "root" in lower_name:
        return "Dashboard", "Home"
    return current_screen_name, "General"

async def prioritize_queue_with_ai(elements: list, current_screen_name: str, user_prompt: Optional[str] = None) -> list:
    """Asks Ollama to rank/reorder the queue of elements, falling back to heuristic sorting."""
    if not elements:
        return []
    
    # Heuristic element weight calculation for fast fallback and safe ordering
    def get_weight(el):
        text = (el.get("text", "") + " " + el.get("content_desc", "") + " " + el.get("resource_id", "")).lower()
        class_name = el.get("class", "").lower()
        if any(kw in text for kw in ["logout", "log out", "signout", "sign out", "delete account", "exit"]):
            return 100
        if any(kw in text for kw in ["menu", "nav", "drawer", "tab", "profile", "settings", "home"]):
            return 1
        if "button" in class_name or el.get("clickable"):
            return 10
        if el.get("is_input") or "edittext" in class_name:
            return 20
        return 50

    heuristic_elements = sorted(elements, key=get_weight)
    
    clean_list = []
    for idx, el in enumerate(elements):
        clean_list.append({
            "idx": idx,
            "class": el["class"].split('.')[-1],
            "resource_id": el["resource_id"].split('/')[-1] if el["resource_id"] else "",
            "text": el["text"][:30] if el["text"] else "",
            "desc": el["content_desc"][:30] if el["content_desc"] else "",
            "selector": el["selector"]
        })
        
    prompt = (
        f"You are a mobile app QA crawling agent.\n"
        f"Exploring screen name: '{current_screen_name}'.\n"
    )
    if user_prompt:
        prompt += f"The user provided this crawling objective/guidance: '{user_prompt}'\n"
        
    prompt += (
        f"Here is the complete queue of clickable/interactable elements to explore:\n"
        f"{json.dumps(clean_list, indent=2)}\n\n"
        f"Please rank these elements in the order they should be explored.\n"
        f"Prioritize elements likely to navigate to distinct new screens (like buttons, menu items, tabs).\n"
        f"Place inputs and local state buttons later. Exclude or deprioritize harmful actions (logouts, delete account).\n"
        f"Return a JSON object containing the ordered list of indices to explore:\n"
        f"{{\n"
        f"  \"ordered_indices\": [<index_1>, <index_2>, ...]\n"
        f"}}\n"
        f"Respond ONLY with this JSON block, no markdown wraps, no extra text."
    )
    
    try:
        res_json = await call_ollama_json(prompt)
        ordered_indices = res_json.get("ordered_indices", [])
        
        ordered_elements = []
        seen = set()
        for idx in ordered_indices:
            if idx is not None and 0 <= idx < len(elements) and idx not in seen:
                ordered_elements.append(elements[idx])
                seen.add(idx)
                
        for idx, el in enumerate(elements):
            if idx not in seen:
                ordered_elements.append(el)
                
        return ordered_elements
    except Exception as e:
        logger.warning(f"AI queue prioritization failed: {e}. Keeping heuristic order.")
        return heuristic_elements

async def call_ollama(prompt: str) -> str:
    """Helper to query the local Ollama model."""
    logger.info(f"🤖 [AI PROMPT]\n--- PROMPT START ---\n{prompt}\n--- PROMPT END ---")
    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(
            "http://127.0.0.1:11434/api/generate",
            json={
                "model": "llama3.2",
                "prompt": prompt,
                "stream": False
            },
            timeout=45.0
        )
        resp_text = response.json().get("response", "").strip()
        logger.info(f"🤖 [AI RESPONSE]\n--- RESPONSE START ---\n{resp_text}\n--- RESPONSE END ---")
        return resp_text

async def call_ollama_json(prompt: str) -> dict:
    """Helper to query local Ollama and guarantee parsed JSON dict returned."""
    resp = await call_ollama(prompt)
    try:
        cleaned = resp
        if "```json" in cleaned:
            cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in cleaned:
            cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
        parsed = json.loads(cleaned)
        logger.info(f"🤖 [AI PARSED JSON] Successfully parsed JSON structure: {parsed}")
        return parsed
    except Exception as e:
        logger.warning(f"🤖 [AI PARSING WARNING] Failed to parse JSON with standard json.loads: {e}. Attempting regex fallback parser.")
        # Fallback regex key-value extraction for robustness
        parsed = {}
        for line in resp.splitlines():
            line = line.strip()
            # Parse booleans
            bool_match = re.search(r'["\']?([a-zA-Z0-9_]+)["\']?\s*:\s*(true|false)', line, re.I)
            if bool_match:
                parsed[bool_match.group(1)] = bool_match.group(2).lower() == "true"
                continue
            # Parse numbers
            num_match = re.search(r'["\']?([a-zA-Z0-9_]+)["\']?\s*:\s*(\d+)', line)
            if num_match:
                parsed[num_match.group(1)] = int(num_match.group(2))
                continue
            # Parse strings
            str_match = re.search(r'["\']?([a-zA-Z0-9_]+)["\']?\s*:\s*["\']([^"\']+)["\']', line)
            if str_match:
                parsed[str_match.group(1)] = str_match.group(2)
                continue
        logger.info(f"🤖 [AI REGEX PARSED JSON] Fallback parsing results: {parsed}")
        return parsed

async def prioritize_elements_with_ai(elements: list, current_screen_name: str) -> list:
    """Asks Ollama to rank and filter SAFE interactable elements, skipping dynamic logins/logouts."""
    if not elements:
        return []
    
    clean_list = []
    for idx, el in enumerate(elements):
        clean_list.append({
            "idx": idx,
            "class": el["class"].split('.')[-1],
            "resource_id": el["resource_id"].split('/')[-1] if el["resource_id"] else "",
            "text": el["text"][:30] if el["text"] else "",
            "desc": el["content_desc"][:30] if el["content_desc"] else "",
            "selector": el["selector"]
        })
        
    prompt = (
        f"You are a mobile app QA crawling agent.\n"
        f"Exploring screen name: '{current_screen_name}'.\n"
        f"Here are the clickable elements visible on this layout:\n"
        f"{json.dumps(clean_list, indent=2)}\n\n"
        f"Please return a JSON object listing only elements that are SAFE to explore (e.g. click navigation, enter values).\n"
        f"Exclude elements that trigger destructive behaviors (like Delete Account), log out, or open external web links.\n"
        f"Prioritize elements likely to navigate to distinct sections.\n"
        f"Your JSON must strictly match this schema:\n"
        f"{{\n"
        f"  \"recommended_actions\": [\n"
        f"    {{\n"
        f"      \"idx\": <integer_index>,\n"
        f"      \"is_input\": <boolean>,\n"
        f"      \"description\": \"Brief context description\"\n"
        f"    }}\n"
        f"  ]\n"
        f"}}\n"
        f"Respond ONLY with this JSON block, no markdown wraps, no extra text."
    )
    
    try:
        res_json = await call_ollama_json(prompt)
        actions = []
        if isinstance(res_json, dict):
            actions = res_json.get("recommended_actions", [])
        elif isinstance(res_json, list):
            actions = res_json
        
        prioritized = []
        for act in actions:
            idx = act.get("idx")
            if idx is not None and 0 <= idx < len(elements):
                el = elements[idx]
                el["description"] = act.get("description", el["text"] or el["content_desc"] or el["resource_id"] or "element")
                prioritized.append(el)
                
        if prioritized:
            return prioritized
    except Exception as e:
        logger.warning(f"AI element prioritization failed: {e}. Falling back to default order.")
        
    for el in elements:
        el["description"] = el["text"] or el["content_desc"] or el["resource_id"] or "element"
    return elements

async def generate_input_value(identifier: str, app_package: str) -> str:
    """Resolves realistic input values for form fields using rules or AI generation."""
    lower_id = identifier.lower()
    for key, val in PREFILL_INPUTS.items():
        if key in lower_id:
            return val
            
    prompt = (
        f"We need a realistic test value to type into the input element: '{identifier}'.\n"
        f"The application package is '{app_package}'.\n"
        f"Please return a JSON object containing a single string property 'text_value' with the value to type.\n"
        f"Example: {{\n"
        f"  \"text_value\": \"test@example.com\"\n"
        f"}}\n"
        f"Respond ONLY with this JSON block, no markdown wraps, no extra text."
    )
    try:
        res = await call_ollama_json(prompt)
        val = res.get("text_value")
        if val:
            return val
    except Exception:
        pass
        
    if "email" in lower_id:
        return "test@example.com"
    elif "pass" in lower_id:
        return "Password123"
    return "Test Input"

async def check_is_new_screen(before_xml: str, after_xml: str, current_screen_name: str, clicked_element_desc: str) -> bool:
    """Asks Ollama (or utilizes element delta) to confirm if a layout shift represents a new structural screen."""
    before_elements = len(filter_xml_to_elements(before_xml))
    after_elements = len(filter_xml_to_elements(after_xml))
    
    prompt = (
        f"We are automated crawling a mobile application.\n"
        f"We interacted with target '{clicked_element_desc}' on screen '{current_screen_name}'.\n"
        f"Before action elements count: {before_elements}\n"
        f"After action elements count: {after_elements}\n\n"
        f"Did this action trigger navigation to a new screen, side menu drawer, modal popup, or dialog that presents new interactive options (like navigation links, settings, or forms)? If so, return true. If it is just selecting a checkbox, typing text, or triggering a validation warning on the same layout, return false.\n"
        f"Please return a JSON object matching this schema:\n"
        f"{{\n"
        f"  \"is_new_screen\": <boolean>\n"
        f"}}\n"
        f"Respond ONLY with this JSON block, no markdown wraps, no extra text."
    )
    
    try:
        res = await call_ollama_json(prompt)
        val = res.get("is_new_screen")
        if val is not None:
            return val
    except Exception:
        pass
        
    return True

async def run_ai_extrication_agent(client, target_screen_name: str, error_msg: str, app_package: str) -> bool:
    """AI recovery loop to resolve errors, dismiss modals, or restart package if stuck."""
    logger.warning(f"🚨 [AI TAKE-OVER] Entering AI agent recovery loop to resolve error: '{error_msg}'...")
    
    for step in range(1, 4):
        try:
            page_source = await get_compressed_page_source(client)
            if not page_source:
                continue
                
            current_elements = filter_xml_to_elements(page_source)
            clean_list = []
            for idx, el in enumerate(current_elements):
                clean_list.append({
                    "idx": idx,
                    "class": el["class"].split('.')[-1],
                    "resource_id": el["resource_id"].split('/')[-1] if el["resource_id"] else "",
                    "text": el["text"][:30] if el["text"] else "",
                    "desc": el["content_desc"][:30] if el["content_desc"] else "",
                    "selector": el["selector"]
                })
                
            prompt = (
                f"You are a mobile app automation recovery assistant.\n"
                f"We got stuck while trying to crawl the app.\n"
                f"Error message was: '{error_msg}'.\n"
                f"We want to be on the screen: '{target_screen_name}'.\n\n"
                f"Here are the elements currently visible on the screen:\n"
                f"{json.dumps(clean_list, indent=2)}\n\n"
                f"What action should we take to dismiss the error, dismiss a modal, close the keyboard, click a cancel/back button, or bypass this obstacle?\n"
                f"Choose one action:\n"
                f"- click (requires target_idx)\n"
                f"- back (tap the back button)\n"
                f"- hide_keyboard (dismiss the keyboard)\n"
                f"- restart (reboot the app)\n\n"
                f"Format your output as a JSON object matching this schema:\n"
                f"{{\n"
                f"  \"tool\": \"click, back, hide_keyboard, or restart\",\n"
                f"  \"target_idx\": <integer_index_required_if_tool_is_click>\n"
                f"}}\n"
                f"Respond ONLY with this JSON block, no markdown wraps, no extra text."
            )
            
            res = await call_ollama_json(prompt)
            tool = res.get("tool", "").lower().strip()
            
            logger.info(f"🤖 [AI TAKE-OVER] Step {step}/3: AI suggested tool '{tool}'")
            
            if tool == "click":
                target_idx = res.get("target_idx")
                if target_idx is not None and 0 <= target_idx < len(current_elements):
                    selector = current_elements[target_idx]["selector"]
                    await client.call_tool("click_element", {"selector": selector})
            elif tool == "back":
                await client.call_tool("back", {})
            elif tool == "hide_keyboard":
                try:
                    await client.call_tool("hide_keyboard", {})
                except Exception:
                    pass
            elif tool == "restart":
                await client.call_tool("terminate_app", {"appPackage": app_package})
                await asyncio.sleep(2.0)
                await client.call_tool("activate_app", {"appPackage": app_package})
                
            await asyncio.sleep(3.0)
            
            # Post-action verification
            post_xml = await get_compressed_page_source(client)
            if post_xml:
                post_fingerprint = get_screen_fingerprint(post_xml)
                if post_fingerprint in visited_screens:
                    logger.info("✅ [AI TAKE-OVER SUCCESS] Application successfully recovered back to a mapped state.")
                    return True
        except Exception as e:
            logger.warning(f"⚠️ [AI TAKE-OVER ERROR] Recovery step failed: {e}")
            
    return False

async def app_crawl(
    client,
    parent_fingerprint: Optional[str],
    parent_screen_name: Optional[str],
    current_screen_name: str,
    depth: int,
    app_package: str,
    max_depth: int,
    discovered_nodes: dict,
    executed_actions: set,
    history: list,
    steps_ref: list,
    max_steps: int,
    device_name: str = "Android Emulator",
    user_prompt: Optional[str] = None
) -> None:
    """Recursive DFS crawl of the application screen configurations."""
    if depth > max_depth:
        logger.warning(f"⚠️ [DEPTH LIMIT] Reached max crawl depth of {max_depth} at [{current_screen_name}]")
        return

    # Boundary check: make sure we are still inside target app package
    try:
        pkg_res = await client.call_tool("get_current_package", {})
        current_package = ""
        for item in pkg_res.get("content", []):
            if item.get("type") == "text":
                current_package += item.get("text", "")
        current_package = current_package.strip()
        
        if current_package and current_package != app_package:
            logger.warning(f"⚠️ [BOUNDARY WARN] Script dropped out to {current_package}. Restoring {app_package}...")
            await client.call_tool("activate_app", {"appPackage": app_package})
            await asyncio.sleep(3.0)
    except Exception as e:
        logger.warning(f"Failed to run app boundary package check: {e}")

    # Gather live screen state
    live_xml = await get_compressed_page_source(client)
    if not live_xml:
        logger.warning("Empty page source retrieved. Stopping recursion path.")
        return
        
    live_fingerprint = get_screen_fingerprint(live_xml)
    screen_width, screen_height = get_screen_dimensions(live_xml)
    live_elements = filter_xml_to_elements(live_xml)
    
    # Determine screen name and type via AI classification
    screen_name, screen_type = await classify_screen_with_ai(live_elements, current_screen_name)
    
    # Generate node ID base
    node_id_base = re.sub(r'[^a-zA-Z0-9]', '_', screen_name.lower())
    node_id = f"{node_id_base}_root" if depth == 0 else f"{node_id_base}_{live_fingerprint[:8].lower()}"
    
    # Live tool node registration
    node_status = register_new_node(node_id=node_id, screen_name=screen_name)
    
    parent_node_id = None
    if parent_fingerprint and parent_fingerprint in discovered_nodes:
        parent_node_id = discovered_nodes[parent_fingerprint]["node_id"]
    elif parent_fingerprint:
        parent_node_id = f"scr_{parent_fingerprint[:8].lower()}"

    if node_status == "DUPLICATE":
        # Format cyclic warning log
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        print(f"[{timestamp}] [WARN] [DUPLICATE_DETECTED] Node {node_id} already visited. Creating cyclic edge link.", flush=True)
        append_telemetry("DUPLICATE_DETECTED", {"node_id": node_id, "screen_name": screen_name})
        
        # Backtrack immediately
        await client.call_tool("back", {})
        await asyncio.sleep(3.0)
        return
        
    # NEW node found: take screenshot and register elements
    screenshot_path = await save_screenshot(client, node_id)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    print(f"[{timestamp}] [INFO] [NODE_DISCOVERED] ID: {node_id} | Name: {screen_name} | Depth: {depth} | Screenshot: {screenshot_path}", flush=True)
    append_telemetry("NODE_DISCOVERED", {
        "node_id": node_id,
        "screen_name": screen_name,
        "depth": depth,
        "screenshot": screenshot_path
    })
    
    # Format elements to requested nodes schema
    formatted_elements = [
        format_element_for_map(el, screen_width, screen_height, idx)
        for idx, el in enumerate(live_elements)
    ]
    
    discovered_nodes[live_fingerprint] = {
        "node_id": node_id,
        "screen_name": screen_name,
        "screen_type": screen_type,
        "parent_node": parent_node_id,
        "depth": depth,
        "elements": formatted_elements,
        "timestamp": datetime.now().isoformat(),
        "actions_taken": []
    }
    save_app_map(discovered_nodes, history, app_package, device_name)
    
    # AI Queue prioritization (ranks all elements, doesn't discard any)
    element_queue = await prioritize_queue_with_ai(live_elements, current_screen_name, user_prompt=user_prompt)
    
    # Click/Type loop - runs through the queue completely
    for target in element_queue:
        identifier = target.get("description") or target.get("text") or target.get("resource_id") or "unnamed"
        action_key = f"{live_fingerprint}_{target['class']}_{identifier}"
        
        if action_key in executed_actions:
            continue
        executed_actions.add(action_key)
        
        # Self-healing selector check: re-verify target presence on the current layout
        try:
            live_xml_now = await get_compressed_page_source(client)
            if live_xml_now:
                current_live_elements = filter_xml_to_elements(live_xml_now)
                best_match = None
                for candidate in current_live_elements:
                    if candidate["class"] == target["class"]:
                        if (target.get("text") and candidate.get("text") == target.get("text")) or \
                           (target.get("content_desc") and candidate.get("content_desc") == target.get("content_desc")) or \
                           (target.get("resource_id") and candidate.get("resource_id") == target.get("resource_id")):
                            best_match = candidate
                            break
                        if candidate["selector"] == target["selector"]:
                            best_match = candidate
                            break
                if best_match:
                    if best_match["selector"] != target["selector"]:
                        target["selector"] = best_match["selector"]
                else:
                    continue
        except Exception:
            pass
            
        steps_ref[0] += 1
        step = steps_ref[0]
        
        # Format element id for logging
        element_id = make_element_id(
            text=target.get("text", ""),
            desc=target.get("content_desc", ""),
            resource_id=target.get("resource_id", ""),
            tag=target.get("class", ""),
            index=0
        )
        action_type = "INPUT" if target.get("is_input") else "CLICK"
        
        # Log action execution in Stdout
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        print(f"[{timestamp}] [INFO] [ACTION_EXECUTE] Order: {step} | Action: {action_type} | Target Element: {element_id} | Selector: {target['selector']}", flush=True)
        append_telemetry("ACTION_EXECUTE", {
            "order": step,
            "action": action_type,
            "element_id": element_id,
            "selector": target["selector"]
        })
        
        try:
            # Handle Inputs
            if target.get("is_input"):
                input_val = await generate_input_value(identifier, app_package)
                await client.call_tool("enter_text", {"selector": target["selector"], "text": input_val})
                try:
                    await client.call_tool("hide_keyboard", {})
                except Exception:
                    pass
                await asyncio.sleep(1.5)
            # Handle Taps
            else:
                await client.call_tool("click_element", {"selector": target["selector"]})
                await asyncio.sleep(2.5)
                
            history.append({
                "step": step,
                "node_id": node_id,
                "decision": action_type,
                "reason": f"Interacted with {target['selector']}: {identifier}"
            })
            discovered_nodes[live_fingerprint]["actions_taken"].append({
                "step": step,
                "decision": action_type,
                "reason": f"Interacted with {target['selector']}: {identifier}"
            })
            save_app_map(discovered_nodes, history, app_package, device_name)
        except Exception as err:
            logger.error(f"   ❌ [ERROR] Interaction failed: {err}")
            recovered = await run_ai_extrication_agent(client, current_screen_name, str(err), app_package)
            if not recovered:
                continue
                
        # Validate transitions and recurse DFS
        try:
            post_xml = await get_compressed_page_source(client)
            if not post_xml:
                continue
                
            post_fingerprint = get_screen_fingerprint(post_xml)
            
            if post_fingerprint != live_fingerprint:
                is_new_page = await check_is_new_screen(live_xml, post_xml, current_screen_name, identifier)
                
                # Check for loop/cycle and recurses down if it's a new screen structure
                if is_new_page and "cancel" not in identifier.lower() and "back" not in identifier.lower():
                    next_screen_name = f"Screen_{re.sub(r'[^a-zA-Z0-9]', '_', identifier)}"
                    
                    await app_crawl(
                        client=client,
                        parent_fingerprint=live_fingerprint,
                        parent_screen_name=current_screen_name,
                        current_screen_name=next_screen_name,
                        depth=depth + 1,
                        app_package=app_package,
                        max_depth=max_depth,
                        discovered_nodes=discovered_nodes,
                        executed_actions=executed_actions,
                        history=history,
                        steps_ref=steps_ref,
                        max_steps=max_steps,
                        device_name=device_name,
                        user_prompt=user_prompt
                    )
                    
                    # Backtrack: physical recovery back to parent
                    await client.call_tool("back", {})
                    await asyncio.sleep(3.0)
                    
                    # Confirm we returned back to the parent state
                    verify_xml = await get_compressed_page_source(client)
                    if verify_xml:
                        verify_fingerprint = get_screen_fingerprint(verify_xml)
                        if verify_fingerprint != live_fingerprint:
                            logger.warning(f"   ⚠️ [BACKTRACK FAIL] Landed on wrong state: {verify_fingerprint[:8].upper()}. Rebooting app package...")
                            await client.call_tool("terminate_app", {"appPackage": app_package})
                            await asyncio.sleep(2.0)
                            await client.call_tool("activate_app", {"appPackage": app_package})
                            await asyncio.sleep(3.0)
                else:
                    logger.info("   ↔️ [STATE CHECK] Local state update handled. Continuing explorations.")
        except Exception as post_err:
            logger.error(f"Error checking state transition: {post_err}")

    # Exhausted all elements on this screen - Backtrack trigger
    parent_node_id_log = parent_node_id if parent_node_id else "None"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    print(f"[{timestamp}] [INFO] [BACKTRACK_TRIGGER] Path exhausted at Node {node_id}. Executing driver.back() to Parent {parent_node_id_log}", flush=True)
    append_telemetry("BACKTRACK_TRIGGER", {
        "node_id": node_id,
        "parent_node_id": parent_node_id_log
    })

async def main():
    # 1. Start Appium server dynamically if not running
    logger.info("Ensuring Appium Server is running...")
    ensure_appium_server(device_type="local emulator", port=4723)

    # 2. Start Appium MCP Client
    logger.info("Starting Appium MCP Client...")
    client = AppiumMcpClient(server_dir="/Users/preethichitte/Documents/mcp_appium_server")
    await client.start()

    discovered_nodes = {}
    executed_actions = set()
    history = []
    steps_ref = [0]
    max_steps = 5
    max_depth = 3

    try:
        # 3. Start Appium Session
        logger.info("Starting session on Android Emulator...")
        session_args = {
            "deviceName": "Android Emulator",
            "udid": "emulator-5554",
            "appPackage": "com.android.settings",
            "appActivity": "com.android.settings.Settings",
            "deviceType": "local emulator"
        }
        
        await client.call_tool("start_session", session_args)
        await asyncio.sleep(4.0)

        logger.info(f"Ensuring target app {session_args['appPackage']} is in foreground...")
        await client.call_tool("activate_app", {"appPackage": session_args["appPackage"]})
        await asyncio.sleep(3.0)

        # Clear global state tracking
        visited_screens.clear()
        visited_nodes.clear()

        # Try to bypass login barriers if present
        await handle_login_bypass(client, session_args["appPackage"])

        # 4. Initiate recursive DFS crawl starting at root page
        await app_crawl(
            client=client,
            parent_fingerprint=None,
            parent_screen_name=None,
            current_screen_name="Root_Screen",
            depth=0,
            app_package=session_args["appPackage"],
            max_depth=max_depth,
            discovered_nodes=discovered_nodes,
            executed_actions=executed_actions,
            history=history,
            steps_ref=steps_ref,
            max_steps=max_steps,
            device_name=session_args["deviceName"]
        )
        
        # 5. Save App Map structure
        app_map = {
            "app_name": "Android Emulator",
            "package": session_args["appPackage"],
            "crawl_date": datetime.now().strftime("%Y-%m-%d"),
            "total_screens_discovered": len(discovered_nodes),
            "steps_taken": len(history),
            "nodes": list(discovered_nodes.values()),
            "history": history
        }
        
        map_path = os.path.join(os.getcwd(), "app_map.json")
        with open(map_path, "w") as f:
            json.dump(app_map, f, indent=2)
            
        logger.info(f"App map saved successfully to {map_path}")

    except Exception as e:
        logger.error(f"Error during exploration: {e}", exc_info=True)
    finally:
        logger.info("Stopping MCP client...")
        await client.stop()

if __name__ == "__main__":
    asyncio.run(main())
