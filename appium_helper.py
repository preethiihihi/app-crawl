import os
import re
import json
import base64
import shutil
import logging
import subprocess
import asyncio
import hashlib
import httpx
from datetime import datetime
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional, Any
from appium_service import AppiumMcpClient, ensure_appium_server

logger = logging.getLogger("appium_helper")

def is_app_installed_via_adb(app_package: str, udid: Optional[str] = None) -> bool:
    """
    Checks via ADB if a package is already installed on the target device.
    """
    adb_path = shutil.which("adb")
    if not adb_path:
        sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
        adb_path = os.path.join(sdk_root, "platform-tools", "adb")
        if not os.path.exists(adb_path):
            return False
            
    cmd = [adb_path]
    if udid:
        cmd.extend(["-s", udid])
    cmd.extend(["shell", "pm", "path", app_package])
    
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10.0)
        return "package:" in res.stdout
    except Exception:
        return False


async def start_appium_session(
    device_name: str = "emulator-5554",
    udid: str = "emulator-5554",
    app_package: str = "com.kuberproject",
    app_activity: str = ".MainActivity",
    device_type: str = "local emulator",
    apk_path: Optional[str] = None
) -> AppiumMcpClient:
    """
    Ensures the Appium server is running, and starts the Appium MCP client session.
    If the app package is not already installed on the device, installs it via Appium appPath.
    """
    # 1. Start local Appium server if needed
    ensure_appium_server(device_type)

    # 2. Initialize the MCP client
    client = AppiumMcpClient(server_dir="testily-appium-mcp")
    await client.start()

    # 3. Build session arguments
    session_args = {
        "deviceName": device_name,
        "udid": udid,
        "appPackage": app_package,
        "appActivity": app_activity,
        "deviceType": device_type
    }
    
    # 4. Check if app is installed; if not, pass apk_path to install it
    is_installed = is_app_installed_via_adb(app_package, udid)
    if is_installed:
        logger.info(f"App package '{app_package}' is already installed. Starting app directly.")
    else:
        logger.info(f"App package '{app_package}' is not installed.")
        if apk_path and os.path.exists(apk_path):
            logger.info(f"Setting appPath for installation: {apk_path}")
            session_args["appPath"] = os.path.abspath(apk_path)
        else:
            logger.warning("No APK path available or file does not exist. Attempting direct launch anyway.")

    # 5. Start session in the MCP client
    logger.info(f"Starting Appium session via MCP tool: {session_args}")
    resp = await client.call_tool("start_session", session_args)
    logger.info(f"Appium session started response: {resp}")
    
    return client


async def download_screenshot_by_appium(client: AppiumMcpClient) -> bytes:
    """
    Takes a screenshot using Appium MCP tool and returns the binary image bytes.
    """
    resp = await client.call_tool("take_screenshot", {})
    content = resp.get("content", [])
    if not content:
        raise ValueError("No content returned in screenshot response.")
        
    base64_data = ""
    # 1. Look for type="image" first (which holds data)
    for item in content:
        if item.get("type") == "image":
            base64_data = item.get("data", "")
            break
            
    # 2. Fallback to type="text" if no type="image" is found (handling potential alternative formats)
    if not base64_data:
        for item in content:
            if item.get("type") == "text":
                text = item.get("text", "")
                if "base64" in text or len(text) > 100:  # standard text base64 data check
                    base64_data = text
                    break
                    
    if not base64_data:
        raise ValueError("No image/base64 data found in screenshot response.")
        
    # Strip base64 prefix if present
    if "base64," in base64_data:
        base64_data = base64_data.split("base64,")[-1]
        
    # Clean whitespace and decode
    base64_data = base64_data.strip()
    return base64.b64decode(base64_data)


async def get_page_source_by_appium(client: AppiumMcpClient) -> str:
    """
    Retrieves the XML page source structure of the current screen via Appium.
    """
    resp = await client.call_tool("get_page_source", {})
    content = resp.get("content", [])
    if not content:
        raise ValueError("No content returned in page source response.")
        
    for item in content:
        if item.get("type") == "text":
            return item.get("text", "")
            
    raise ValueError("No text layout found in page source response.")


def xml_to_json(xml_source: str) -> dict:
    """
    Recursively parses XML page source and returns a structured JSON dictionary.
    """
    try:
        root = ET.fromstring(xml_source.strip().encode("utf-8"))
        return _element_to_dict(root)
    except Exception as e:
        logger.error(f"Error parsing XML to JSON: {e}")
        return {}


def _element_to_dict(element: ET.Element) -> dict:
    node = {
        "tag": element.tag,
        "attributes": dict(element.attrib),
        "children": []
    }
    for child in element:
        node["children"].append(_element_to_dict(child))
    return node


def parse_bounds(bounds_str: str) -> tuple[int, int, int, int]:
    """
    Parses Android layout bounds string '[x1,y1][x2,y2]' into (x1, y1, x2, y2).
    """
    if not bounds_str:
        return 0, 0, 0, 0
    match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
    if match:
        return tuple(map(int, match.groups()))
    return 0, 0, 0, 0


def get_clickable_elements(
    xml_source: str, 
    screen_width: int = 1080, 
    screen_height: int = 2160
) -> List[Dict[str, Any]]:
    """
    Parses the layout XML, extracts all elements (clickable and non-clickable),
    capturing all structural properties and formatting selectors matching the user's schema.
    """
    try:
        root = ET.fromstring(xml_source.strip().encode("utf-8"))
    except Exception as e:
        logger.error(f"Error parsing XML for elements: {e}")
        return []
        
    elements_list = []
    class_counts = {}
    
    for node in root.iter():
        tag = node.tag
        if tag.lower() in ("hierarchy", "?xml", "!doctype"):
            continue
            
        attrib = node.attrib
        class_name = attrib.get("class", tag)
        short_type = class_name.split(".")[-1] if class_name else "Element"
        
        # Track counts of classes for unique ID fallbacks
        class_counts[short_type] = class_counts.get(short_type, 0) + 1
        class_idx = class_counts[short_type]
        
        text = attrib.get("text", "").strip()
        content_desc = attrib.get("content-desc", "").strip()
        resource_id = attrib.get("resource-id", "").strip()
        package_name = attrib.get("package", "").strip()
        bounds_str = attrib.get("bounds", "")
        
        clickable = attrib.get("clickable", "").lower() == "true"
        enabled = attrib.get("enabled", "").lower() != "false"
        displayed = attrib.get("displayed", "").lower() != "false"
        selected = attrib.get("selected", "").lower() == "true"
        checked = attrib.get("checked", "").lower() == "true"
        focusable = attrib.get("focusable", "").lower() == "true"
        
        # Parse coordinates
        x1, y1, x2, y2 = parse_bounds(bounds_str)
        
        # Skip zero-dimension elements
        if x1 == x2 or y1 == y2:
            continue
            
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2
        x_ratio = round(center_x / screen_width, 4) if screen_width else 0.0
        y_ratio = round(center_y / screen_height, 4) if screen_height else 0.0
        
        # Generate element_id
        element_id = ""
        if resource_id and "/" in resource_id:
            element_id = resource_id.split("/")[-1]
        elif content_desc:
            element_id = content_desc
        elif text:
            element_id = text
            
        if element_id:
            element_id = re.sub(r'[^a-zA-Z0-9_]', '_', element_id.lower()).strip("_")
            element_id = re.sub(r'_+', '_', element_id)
            if not element_id or element_id[0].isdigit():
                element_id = f"el_{element_id}"
        else:
            element_id = f"{short_type.lower()}_{class_idx}"
            
        # Build selectors
        uiautomator_sel = f'new UiSelector().className("{class_name}")'
        if resource_id:
            uiautomator_sel += f'.resourceId("{resource_id}")'
        if text:
            uiautomator_sel += f'.text("{text}")'
        if content_desc:
            uiautomator_sel += f'.description("{content_desc}")'
            
        xpath_sel = f"//{class_name}"
        conditions = []
        if resource_id:
            conditions.append(f'@resource-id="{resource_id}"')
        elif content_desc:
            conditions.append(f'@content-desc="{content_desc}"')
        elif text:
            conditions.append(f'@text="{text}"')
            
        if conditions:
            xpath_sel += f"[{' and '.join(conditions)}]"
        else:
            xpath_sel += f"[{class_idx}]"
            
        selectors = {
            "resource_id": resource_id,
            "accessibility_id": content_desc,
            "uiautomator": uiautomator_sel,
            "text_locator": text,
            "xpath": xpath_sel
        }

        # Build best_selector for Appium MCP tool consumption
        tag_name = class_name if class_name else "*"
        if text:
            escaped_text = text.replace('"', '\\"')
            best_selector = f'xpath=//{tag_name}[@text="{escaped_text}"]'
        elif content_desc:
            best_selector = f"~{content_desc}"
        elif resource_id:
            best_selector = f"id={resource_id}"
        else:
            best_selector = f"xpath=//{tag_name}[@bounds='{bounds_str}']"
            
        elements_list.append({
            "element_id": element_id,
            "resource_id": resource_id,
            "text": text,
            "content_desc": content_desc,
            "class_name": class_name,
            "package_name": package_name,
            "bounds": bounds_str,
            "clickable": clickable,
            "focusable": focusable,
            "is_input": "EditText" in class_name or class_name.endswith("EditText"),
            "selector": best_selector,
            "enabled": enabled,
            "displayed": displayed,
            "selected": selected,
            "checked": checked,
            "element_type": short_type,
            "navigation_target": None,
            "selectors": selectors,
            "center": {"x": center_x, "y": center_y},
            "tap_by_ratio": {"x_ratio": x_ratio, "y_ratio": y_ratio}
        })
        
    return elements_list


def get_screen_fingerprint(xml_text: str) -> str:
    """Generates an MD5 fingerprint representing only the structural blueprint of the screen."""
    if not xml_text:
        return "EMPTY"
        
    try:
        import xml.etree.ElementTree as ET
        
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
        logger.warning(f"Error parsing XML for fingerprinting: {e}. Falling back to regex cleaning.")
        cleaned = xml_text
        cleaned = re.sub(r'\btext=["\'][^"\']*["\']', 'text=""', cleaned)
        cleaned = re.sub(r'\bcontent-desc=["\'][^"\']*["\']', 'content-desc=""', cleaned)
        cleaned = re.sub(r'\b(focused|selected|checked|bounds|selection-start|selection-end|showing-hint)=["\'][^"\']*["\']', '', cleaned)
        return hashlib.md5(cleaned.encode("utf-8")).hexdigest()


async def save_run_metadata(
    supabase_url: str,
    supabase_key: str,
    run_id: str,
    app_name: str,
    app_package: str,
    app_activity: Optional[str] = None,
    app_metadata: Optional[dict] = None,
    app_map_url: Optional[str] = None,
    table_name: str = "crawl_runs"
) -> None:
    """
    Inserts or updates (upserts) the crawl run metadata in the crawl_runs table in Supabase.
    """
    if not supabase_url or not supabase_key:
        logger.warning("Supabase credentials not available. Skipping crawl_runs entry.")
        return

    url = f"{supabase_url.rstrip('/')}/rest/v1/{table_name}"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates"  # Upsert support in PostgREST
    }
    
    payload = {
        "run_id": run_id,
        "app_name": app_name,
        "app_package": app_package,
        "app_activity": app_activity or "",
        "app_metadata": app_metadata or {},
        "app_map_url": app_map_url or ""
    }
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=20.0)
            if resp.status_code not in (200, 201):
                logger.error(f"Failed to upsert run metadata in Supabase: {resp.status_code} - {resp.text}")
                raise Exception(f"Failed to save run metadata: {resp.text}")
            logger.info(f"Successfully saved run metadata for run_id={run_id} to Supabase table {table_name}")
    except Exception as e:
        logger.error(f"Error in save_run_metadata: {e}", exc_info=True)


async def save_screen_job(
    storage_provider: Any,
    screenshot_bytes: bytes,
    folder: str,
    filename: str,
    run_id: str,
    app_name: str,
    screen_name: str,
    page_element: dict,
    order_id: int,
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None,
    nodes_dict: Optional[dict] = None,
    fingerprint: Optional[str] = None,
    table_name: str = "crawled_screens"
) -> None:
    """
    Parallel job that uploads a screen screenshot to the storage provider,
    then saves the screenshot link and layout JSON to Supabase database.
    """
    try:
        # 1. Upload screenshot
        screenshot_url = ""
        if screenshot_bytes:
            logger.info(f"Uploading screenshot to storage in parallel job: {filename}...")
            screenshot_url = await storage_provider.upload_file(
                file_content=screenshot_bytes,
                folder=folder,
                filename=filename,
                mime_type="image/png"
            )
            logger.info(f"Screenshot uploaded to: {screenshot_url}")
            
            # Update local memory nodes dictionary
            if nodes_dict is not None and fingerprint is not None and fingerprint in nodes_dict:
                nodes_dict[fingerprint]["screenshot_url"] = screenshot_url
        
        # 2. Insert record to Supabase crawled_screens table
        if supabase_url and supabase_key:
            url = f"{supabase_url.rstrip('/')}/rest/v1/{table_name}"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            }
            
            payload = {
                "run_id": run_id,
                "app_name": app_name,
                "screen_name": screen_name,
                "ss_link": screenshot_url,
                "page_element": page_element,
                "order_id": order_id
            }
            
            logger.info(f"Inserting screen row for '{screen_name}' (order_id={order_id}) in Supabase...")
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, headers=headers, timeout=30.0)
                if resp.status_code not in (200, 201):
                    logger.error(f"Failed to insert screen row into Supabase: {resp.status_code} - {resp.text}")
                    raise Exception(f"Failed to insert screen row: {resp.text}")
                logger.info(f"Successfully inserted screen row for '{screen_name}' to Supabase table {table_name}")
        else:
            logger.warning("Supabase credentials not available for database insert in parallel job.")
    except Exception as e:
        logger.error(f"Error in save_screen_job for {screen_name}: {e}", exc_info=True)
        raise e


async def check_ollama_reachable(url: str = "http://127.0.0.1:11434") -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url.rstrip('/'), timeout=2.0)
            return resp.status_code == 200
    except Exception:
        return False

async def call_ollama(prompt: str, model: str = "llama3.2", url: str = "http://127.0.0.1:11434") -> str:
    """Helper to query the local Ollama model, checking if reachable first."""
    logger.info(f"🤖 [AI PROMPT]\n--- PROMPT START ---\n{prompt}\n--- PROMPT END ---")
    try:
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"{url.rstrip('/')}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False
                },
                timeout=45.0
            )
            resp_text = response.json().get("response", "").strip()
            logger.info(f"🤖 [AI RESPONSE]\n--- RESPONSE START ---\n{resp_text}\n--- RESPONSE END ---")
            return resp_text
    except Exception as e:
        logger.warning(f"Ollama call failed (ensure Ollama is running at {url}): {e}")
        raise e

async def call_ollama_json(prompt: str, model: str = "llama3.2", url: str = "http://127.0.0.1:11434") -> dict:
    """Helper to query local Ollama and guarantee parsed JSON dict returned."""
    try:
        resp = await call_ollama(prompt, model, url)
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
        parsed = {}
        for line in resp.splitlines():
            line = line.strip()
            bool_match = re.search(r'["\']?([a-zA-Z0-9_]+)["\']?\s*:\s*(true|false)', line, re.I)
            if bool_match:
                parsed[bool_match.group(1)] = bool_match.group(2).lower() == "true"
                continue
            num_match = re.search(r'["\']?([a-zA-Z0-9_]+)["\']?\s*:\s*(\d+)', line)
            if num_match:
                parsed[num_match.group(1)] = int(num_match.group(2))
                continue
            str_match = re.search(r'["\']?([a-zA-Z0-9_]+)["\']?\s*:\s*["\']([^"\']+)["\']', line)
            if str_match:
                parsed[str_match.group(1)] = str_match.group(2)
                continue
        logger.info(f"🤖 [AI REGEX PARSED JSON] Fallback parsing results: {parsed}")
        return parsed

async def classify_screen_with_ai(elements: list, current_screen_name: str, model: str = "llama3.2", url: str = "http://127.0.0.1:11434") -> tuple:
    """Queries Ollama to get user-friendly screen_name and screen_type dynamically, falling back to heuristics."""
    if not elements:
        return current_screen_name, "General"
    
    clean_elements = []
    for el in elements[:15]:
        clean_elements.append({
            "text": el.get("text", ""),
            "desc": el.get("content_desc", ""),
            "class": el.get("class_name", "").split('.')[-1] if el.get("class_name") else ""
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
        res = await call_ollama_json(prompt, model, url)
        s_name = res.get("screen_name")
        s_type = res.get("screen_type")
        if s_name and s_type:
            return s_name, s_type
    except Exception:
        pass
    
    return current_screen_name, "General"

async def prioritize_queue_with_ai(
    elements: list, 
    current_screen_name: str, 
    user_prompt: Optional[str] = None, 
    model: str = "llama3.2", 
    url: str = "http://127.0.0.1:11434"
) -> list:
    """Asks Ollama to rank/reorder the queue of elements, returning reordered elements list."""
    if not elements:
        return []
        
    clean_list = []
    for idx, el in enumerate(elements):
        clean_list.append({
            "idx": idx,
            "class": el.get("class_name", "").split('.')[-1] if el.get("class_name") else "",
            "resource_id": el.get("resource_id", "").split('/')[-1] if el.get("resource_id") else "",
            "text": el.get("text", "")[:30] if el.get("text") else "",
            "desc": el.get("content_desc", "")[:30] if el.get("content_desc") else "",
            "selector": el.get("selector", "")
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
        f"Prioritize elements likely to navigate to distinct new forward screens (like buttons, menu items, tabs).\n"
        f"Place inputs and local state buttons later.\n"
        f"Place backtracking/close/cancel/dismiss/exit buttons at the very bottom of the queue (lowest priority) so we don't return early before exploring the current screen.\n"
        f"Exclude or deprioritize harmful actions (logouts, delete account).\n"
        f"Return a JSON object containing the ordered list of indices to explore:\n"
        f"{{\n"
        f"  \"ordered_indices\": [<index_1>, <index_2>, ...]\n"
        f"}}\n"
        f"Respond ONLY with this JSON block, no markdown wraps, no extra text."
    )
    
    try:
        res_json = await call_ollama_json(prompt, model, url)
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
        logger.warning(f"AI queue prioritization failed: {e}. Keeping default heuristic sorting.")
        return elements

async def generate_input_value_with_ai(
    identifier: str, 
    app_package: str, 
    prefills: Optional[dict] = None,
    model: str = "llama3.2", 
    url: str = "http://127.0.0.1:11434"
) -> str:
    """Resolves realistic input values for form fields using rules or AI generation, considering available prefill values."""
    prefills_str = json.dumps(prefills or {}, indent=2)
    prompt = (
        f"We need a realistic test value to type into the input element: '{identifier}'.\n"
        f"The application package is '{app_package}'.\n"
        f"Here is a dictionary of configured prefill values we would prefer to use if they are relevant to this input field:\n"
        f"{prefills_str}\n\n"
        f"Please analyze if any key in the prefill dictionary matches or is relevant to the input element '{identifier}'.\n"
        f"If a relevant prefill value exists, select it. If not, generate a realistic value appropriate for the input.\n"
        f"Please return a JSON object containing a single string property 'text_value' with the value to type.\n"
        f"Example: {{\n"
        f"  \"text_value\": \"test@example.com\"\n"
        f"}}\n"
        f"Respond ONLY with this JSON block, no markdown wraps, no extra text."
    )
    try:
        res = await call_ollama_json(prompt, model, url)
        val = res.get("text_value")
        if val:
            return val
    except Exception:
        pass
    return "Test Input"

async def run_ai_extrication_agent(
    client, 
    target_screen_name: str, 
    error_msg: str, 
    app_package: str, 
    model: str = "llama3.2", 
    url: str = "http://127.0.0.1:11434"
) -> bool:
    """AI recovery loop to resolve errors, dismiss modals, or restart package if stuck."""
    logger.warning(f"🚨 [AI TAKE-OVER] Entering AI agent recovery loop to resolve error: '{error_msg}'...")
    try:
        page_source = await get_page_source_by_appium(client)
        if not page_source:
            return False
            
        current_elements = get_clickable_elements(page_source)
        clean_list = []
        for idx, el in enumerate(current_elements):
            clean_list.append({
                "idx": idx,
                "class": el.get("class_name", "").split('.')[-1] if el.get("class_name") else "",
                "resource_id": el.get("resource_id", "").split('/')[-1] if el.get("resource_id") else "",
                "text": el.get("text", "")[:30] if el.get("text") else "",
                "desc": el.get("content_desc", "")[:30] if el.get("content_desc") else "",
                "selector": el.get("selector", "")
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
        res = await call_ollama_json(prompt, model, url)
        tool = res.get("tool", "").lower().strip()
        logger.info(f"🤖 [AI TAKE-OVER] AI suggested tool '{tool}'")
        
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
        return True
    except Exception as e:
        logger.warning(f"⚠️ [AI TAKE-OVER ERROR] Recovery step failed: {e}")
        return False

def prioritize_elements_heuristically(elements: List[Dict[str, Any]], visited: List[str], fingerprint: str, allow_destructive: bool = False) -> List[Dict[str, Any]]:
    """Sorts elements for exploration using heuristic rules."""
    inputs = []
    choices = []
    primaries = []
    standards = []
    navigations = []
    destructives = []
    back_actions = []
    
    destructive_keywords = ["logout", "log out", "signout", "sign out", "delete", "clear", "quit"]
    back_keywords = ["back", "cancel", "close", "dismiss", "exit", "no thanks", "skip"]
    primary_keywords = ["next", "continue", "submit", "save", "done", "ok", "login", "register", "signin", "sign up"]
    navigation_keywords = ["tab", "menu", "drawer", "hamburger", "nav", "home", "profile", "settings"]

    for el in elements:
        visited_key = f"{fingerprint}_{el['element_id']}"
        if visited_key in visited:
            continue
            
        text = el.get("text", "").lower()
        desc = el.get("content_desc", "").lower()
        res_id = el.get("resource_id", "").lower()
        class_name = el.get("class_name", "")
        
        is_destructive = any(kw in text or kw in desc or kw in res_id for kw in destructive_keywords)
        if is_destructive:
            if allow_destructive:
                destructives.append(el)
            else:
                logger.info(f"Skipping potentially destructive element: {el['element_id']}")
            continue
            
        is_back = any(kw == text or kw == desc or kw in res_id for kw in back_keywords) or (text == "x" or desc == "x")
        if is_back:
            back_actions.append(el)
            continue

        if el.get("is_input") or "edittext" in class_name.lower():
            inputs.append(el)
            continue
            
        is_choice = any(cls in class_name for cls in ["CheckBox", "RadioButton", "Switch", "Spinner", "ToggleButton", "CheckedTextView"])
        if is_choice:
            choices.append(el)
            continue
            
        is_navigation = any(kw in text or kw in desc or kw in res_id for kw in navigation_keywords) or any(cls in class_name for cls in ["TabView", "TabBar", "NavigationBar", "BottomNavigation"])
        if is_navigation:
            navigations.append(el)
            continue
            
        is_primary = any(kw in text or kw in desc or kw in res_id for kw in primary_keywords)
        if is_primary:
            primaries.append(el)
        else:
            standards.append(el)
            
    return inputs + choices + primaries + standards + navigations + back_actions + destructives

def is_system_package(package_name: str) -> bool:
    """Checks if a package belongs to Android system, permission controllers or package installers."""
    if not package_name:
        return False
    pkg = package_name.lower()
    return any(p in pkg for p in [
        "com.android.permissioncontroller",
        "com.google.android.packageinstaller",
        "android"
    ])

async def handle_system_dialog(client: AppiumMcpClient) -> bool:
    """Scans screen source for system permission or standard alert dialog action buttons and clicks them."""
    try:
        xml = await get_page_source_by_appium(client)
        elements = get_clickable_elements(xml)
        
        allow_keywords = [
            "allow", "allow only while using the app", "while using the app", 
            "only this time", "ok", "accept", "continue", "grant", "yes"
        ]
        
        for el in elements:
            text = el.get("text", "").strip().lower()
            desc = el.get("content_desc", "").strip().lower()
            res_id = el.get("resource_id", "").strip().lower()
            
            if any(kw == text or kw == desc or kw in res_id for kw in allow_keywords):
                logger.info(f"System Dialog: Auto-clicking allow/accept element {el['element_id']} ({text or desc or res_id})")
                await client.call_tool("click_element", {"selector": el["selector"]})
                await asyncio.sleep(2.0)
                return True
                
        for el in elements:
            class_name = el.get("class_name", "")
            if "button" in class_name.lower():
                logger.info(f"System Dialog Fallback: Clicking first button element {el['element_id']}")
                await client.call_tool("click_element", {"selector": el["selector"]})
                await asyncio.sleep(2.0)
                return True
    except Exception as e:
        logger.warning(f"Error handling system dialog: {e}")
    return False

async def wait_for_loading_indicators(client: AppiumMcpClient, max_wait_seconds: float = 5.0) -> None:
    """Polls the page source to check if any loading spinner/progressbar is visible, waiting until they disappear."""
    start_time = datetime.now()
    while (datetime.now() - start_time).total_seconds() < max_wait_seconds:
        try:
            xml = await get_page_source_by_appium(client)
            if not xml:
                break
            root = ET.fromstring(xml.strip().encode("utf-8"))
            loader_found = False
            for node in root.iter():
                class_name = node.attrib.get("class", "")
                resource_id = node.attrib.get("resource-id", "").lower()
                desc = node.attrib.get("content-desc", "").lower()
                if "ProgressBar" in class_name or "ProgressDialog" in class_name:
                    loader_found = True
                    break
                if any(kw in resource_id or kw in desc for kw in ["loading", "spinner", "progress", "waiting"]):
                    loader_found = True
                    break
            if not loader_found:
                break
            logger.info("Dynamic Loader / Progress Bar detected. Waiting for loading to finish...")
            await asyncio.sleep(1.5)
        except Exception as e:
            logger.warning(f"Error checking loading indicators: {e}")
            break

def find_scrollable_container(xml_text: str) -> Optional[str]:
    """Parses XML and returns the selector of the first scrollable container."""
    try:
        root = ET.fromstring(xml_text.strip().encode("utf-8"))
        for node in root.iter():
            attrib = node.attrib
            class_name = attrib.get("class", "")
            scrollable = attrib.get("scrollable", "").lower() == "true"
            is_scrollable_class = any(cls in class_name for cls in [
                "ScrollView", "RecyclerView", "ListView", "GridView", "HorizontalScrollView"
            ])
            if scrollable or is_scrollable_class:
                resource_id = attrib.get("resource-id", "").strip()
                bounds_str = attrib.get("bounds", "").strip()
                if resource_id:
                    return f"id={resource_id}"
                elif bounds_str:
                    return f"xpath=//{class_name}[@bounds='{bounds_str}']"
                else:
                    return f"xpath=//{class_name}"
    except Exception as e:
        logger.warning(f"Error finding scrollable container: {e}")
    return None

async def perform_scroll_down(client: AppiumMcpClient) -> bool:
    """Executes a scroll down action (upward swipe) to reveal more content below the fold."""
    try:
        logger.info("Executing scroll down gesture...")
        resp = await client.call_tool("scroll", {"direction": "up"})
        logger.info(f"Scroll response: {resp}")
        await asyncio.sleep(1.5)
        return True
    except Exception as e:
        logger.error(f"Failed to scroll down: {e}")
        return False

def merge_new_elements(existing_elements: List[Dict[str, Any]], new_elements: List[Dict[str, Any]]) -> int:
    """Merges newly discovered elements after a scroll into the existing element list."""
    existing_selectors = {el.get("selector") for el in existing_elements if el.get("selector")}
    existing_bounds = {el.get("bounds") for el in existing_elements if el.get("bounds")}
    added_count = 0
    for el in new_elements:
        sel = el.get("selector")
        bnd = el.get("bounds")
        if (sel and sel in existing_selectors) or (bnd and bnd in existing_bounds):
            continue
        existing_elements.append(el)
        if sel:
            existing_selectors.add(sel)
        if bnd:
            existing_bounds.add(bnd)
        added_count += 1
    return added_count


async def crawl_app_map_appium(
    client: AppiumMcpClient,
    app_package: str,
    app_activity: Optional[str],
    project_name: str,
    run_folder: str,
    storage_provider: Any,
    max_steps: int = 15,
    prefill_data: Optional[dict] = None,
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None,
    app_name: Optional[str] = None,
    run_id: Optional[str] = None,
    use_ollama: bool = False,
    ollama_model: str = "llama3.2",
    ollama_url: str = "http://127.0.0.1:11434",
    user_prompt: Optional[str] = None,
    max_scrolls: int = 3,
    allow_destructive: bool = False
) -> dict:
    """
    Performs autonomous, multi-step stack-based crawling (DFS) using Appium MCP tools.
    Returns a dictionary containing:
        - "nodes": dict of fingerprint -> node_data
        - "visited_elements": list of visited element keys
        - "steps_taken": int
    """
    visited_elements = []  # format: "{fingerprint}_{element_id}"
    stack = []  # format: list of dicts {"node_id": str, "fingerprint": str, "navigation_path": list}
    nodes = {}  # format: fingerprint -> node dict
    last_action = None
    out_of_bounds_count = 0
    steps_taken = 0
    
    # Initialize variables for Supabase tracking
    discovered_screens_count = 0
    parallel_tasks = []
    
    actual_run_id = run_id
    if not actual_run_id:
        actual_run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        
    actual_app_name = app_name or app_package

    # Insert initial crawl_run record to Supabase
    if supabase_url and supabase_key:
        initial_metadata = {
            "max_steps": max_steps,
            "prefill_data": prefill_data or {},
            "status": "running",
            "started_at": str(datetime.now())
        }
        logger.info(f"Creating initial crawl run row for run_id={actual_run_id}...")
        try:
            await save_run_metadata(
                supabase_url=supabase_url,
                supabase_key=supabase_key,
                run_id=actual_run_id,
                app_name=actual_app_name,
                app_package=app_package,
                app_activity=app_activity,
                app_metadata=initial_metadata
            )
        except Exception as e:
            logger.error(f"Failed to insert initial crawl run: {e}. Crawling will continue, but db saves might fail due to foreign key constraints.")
    
    # 1. Helper to restart and replay path to recovery
    async def restart_and_replay(target_path: list):
        logger.info(f"Re-navigating by replaying path of length {len(target_path)}...")
        # Force-stop app
        try:
            await client.call_tool("terminate_app", {"appPackage": app_package})
        except Exception as e:
            logger.warning(f"Error terminating app during replay: {e}")
        await asyncio.sleep(1.5)
        
        # Restart app
        try:
            await client.call_tool("activate_app", {"appPackage": app_package})
        except Exception as e:
            logger.warning(f"Error activating app during replay: {e}")
        await asyncio.sleep(4.0)
        
        # Replay each action in path
        for idx, action in enumerate(target_path):
            action_type = action.get("type")
            selector = action.get("selector", "")
            bounds_str = action.get("bounds", "")
            
            # Get current screen layout
            try:
                xml_text = await get_page_source_by_appium(client)
            except Exception as e:
                logger.warning(f"Failed to get page source during replay step {idx}: {e}")
                continue
                
            elements = get_clickable_elements(xml_text)
            
            # Find matching element
            target_el = None
            for el in elements:
                if el.get("selector") == selector or el.get("bounds") == bounds_str:
                    target_el = el
                    break
                    
            if action_type == "click":
                clicked = False
                if target_el:
                    try:
                        await client.call_tool("click_element", {"selector": target_el["selector"]})
                        clicked = True
                    except Exception as ce:
                        logger.warning(f"Failed to click via selector during replay: {ce}")
                        
                if not clicked and bounds_str:
                    coords = parse_bounds(bounds_str)
                    if coords:
                        x1, y1, x2, y2 = coords
                        cx = (x1 + x2) // 2
                        cy = (y1 + y2) // 2
                        try:
                            await client.call_tool("tap_coordinate", {"x": cx, "y": cy})
                            clicked = True
                        except Exception as ce:
                            logger.warning(f"Failed to tap coordinates during replay: {ce}")
                await asyncio.sleep(2.0)
                
            elif action_type == "type":
                value = action.get("value", "")
                typed = False
                if target_el:
                    try:
                        await client.call_tool("clear_element", {"selector": target_el["selector"]})
                        await client.call_tool("enter_text", {"selector": target_el["selector"], "text": value})
                        await client.call_tool("hide_keyboard", {})
                        typed = True
                    except Exception as te:
                        logger.warning(f"Failed to enter text via selector during replay: {te}")
                        
                if not typed and bounds_str:
                    coords = parse_bounds(bounds_str)
                    if coords:
                        x1, y1, x2, y2 = coords
                        cx = (x1 + x2) // 2
                        cy = (y1 + y2) // 2
                        try:
                            await client.call_tool("tap_coordinate", {"x": cx, "y": cy})
                            await asyncio.sleep(0.5)
                            await client.call_tool("hide_keyboard", {})
                            typed = True
                        except Exception as ce:
                            logger.warning(f"Failed to tap coordinate during replay: {ce}")
                await asyncio.sleep(1.5)

    # 2. Helper to rebuild stack from target path
    def rebuild_stack_from_path(target_fingerprint: str):
        target_node = nodes.get(target_fingerprint)
        if not target_node:
            return
            
        path = target_node.get("navigation_path", [])
        new_stack = []
        
        # Find root node
        root_node = None
        root_fp = None
        for fp, node in nodes.items():
            if not node.get("navigation_path"):
                root_node = node
                root_fp = fp
                break
                
        if root_node:
            new_stack.append({
                "node_id": root_node["node_id"],
                "fingerprint": root_fp,
                "navigation_path": []
            })
            
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
                
        if new_stack and new_stack[-1]["fingerprint"] != target_fingerprint:
            new_stack[-1] = {
                "node_id": target_node["node_id"],
                "fingerprint": target_fingerprint,
                "navigation_path": list(path)
            }
            
        stack.clear()
        stack.extend(new_stack)

    # Helper to check if a screen fingerprint has unvisited elements
    def has_unvisited_elements(fp: str) -> bool:
        node = nodes.get(fp)
        if not node:
            return False
        
        ignored_classes = [
            "android.widget.ScrollView",
            "android.widget.HorizontalScrollView",
            "androidx.recyclerview.widget.RecyclerView",
            "android.widget.ListView",
            "android.widget.GridView"
        ]
        
        for el in node.get("elements", []):
            is_input = el.get("is_input", False)
            is_clickable = el.get("clickable", False) or el.get("focusable", False)
            if not (is_input or is_clickable) or not el.get("enabled", True):
                continue
            if any(ignored in el.get("class_name", "") for ignored in ignored_classes):
                continue
            res_id = el.get("resource_id", "")
            if "statusbar" in res_id.lower() or "navigationbar" in res_id.lower():
                continue
                
            visited_key = f"{fp}_{el['element_id']}"
            if visited_key not in visited_elements:
                return True
        return False

    # Prefills
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

    ignored_classes = [
        "android.widget.ScrollView",
        "android.widget.HorizontalScrollView",
        "androidx.recyclerview.widget.RecyclerView",
        "android.widget.ListView",
        "android.widget.GridView"
    ]

    ollama_is_active = False
    if use_ollama:
        ollama_is_active = await check_ollama_reachable(ollama_url)
        if not ollama_is_active:
            logger.warning(f"Ollama url '{ollama_url}' is not reachable. Falling back to Heuristic Mode.")

    scroll_tracker = {}

    # Main crawl loop
    for step in range(max_steps):
        steps_taken += 1
        logger.info(f"--- Crawl Step {step + 1} / {max_steps} ---")
        
        # 1. Get current package to check if out of bounds
        current_package = app_package
        try:
            pkg_resp = await client.call_tool("get_current_package", {})
            content = pkg_resp.get("content", [])
            for item in content:
                if item.get("type") == "text":
                    current_package = item.get("text", "").strip()
                    break
        except Exception as e:
            logger.warning(f"Failed to get current package: {e}")
            
        if current_package and current_package != app_package:
            # Check if it is a system dialog or permission popup
            if is_system_package(current_package):
                logger.info(f"System package/dialog '{current_package}' detected. Auto-handling system dialog...")
                handled = await handle_system_dialog(client)
                if handled:
                    # Do not increment out_of_bounds_count or restart
                    continue
            
            logger.warning(f"Out of bounds! Package: {current_package}. Expected: {app_package}")
            out_of_bounds_count += 1
            if out_of_bounds_count >= 2:
                logger.info("Out of bounds repeatedly. Restarting target app...")
                try:
                    await client.call_tool("terminate_app", {"appPackage": app_package})
                except Exception:
                    pass
                await asyncio.sleep(1.5)
                try:
                    await client.call_tool("activate_app", {"appPackage": app_package})
                except Exception:
                    pass
                await asyncio.sleep(4.0)
                out_of_bounds_count = 0
                last_action = None
            else:
                logger.info("Pressing back button to recover...")
                try:
                    await client.call_tool("back", {})
                except Exception:
                    pass
                await asyncio.sleep(1.5)
            continue
            
        out_of_bounds_count = 0
        
        # Wait for dynamic progress indicators to hide
        await wait_for_loading_indicators(client, max_wait_seconds=5.0)
        
        # 2. Get screen source and fingerprint
        try:
            xml_text = await get_page_source_by_appium(client)
        except Exception as e:
            logger.error(f"Failed to get page source: {e}")
            await asyncio.sleep(2.0)
            continue
            
        fingerprint = get_screen_fingerprint(xml_text)
        node_id = f"screen_{fingerprint}"
        
        # 3. Stack alignment
        if fingerprint in nodes:
            rebuild_stack_from_path(fingerprint)
            
        if not stack:
            stack.append({
                "node_id": node_id,
                "fingerprint": fingerprint,
                "navigation_path": []
            })
        else:
            current_stack_node = stack[-1]
            if current_stack_node["fingerprint"] != fingerprint:
                existing_idx = -1
                for i, item in enumerate(stack):
                    if item["fingerprint"] == fingerprint:
                        existing_idx = i
                        break
                if existing_idx != -1:
                    stack[:] = stack[:existing_idx + 1]
                else:
                    new_path = list(current_stack_node["navigation_path"])
                    if last_action:
                        new_path.append(last_action)
                    stack.append({
                        "node_id": node_id,
                        "fingerprint": fingerprint,
                        "navigation_path": new_path
                    })
                    
        current_stack_node = stack[-1]
        
        # 4. Discovered node registration
        if fingerprint not in nodes:
            logger.info(f"New screen discovered: {node_id}")
            try:
                screenshot_bytes = await download_screenshot_by_appium(client)
            except Exception as e:
                logger.warning(f"Failed to download screenshot: {e}")
                screenshot_bytes = b""
                
            all_elements = get_clickable_elements(xml_text)
            json_layout = xml_to_json(xml_text)
            
            screen_name = "Home Screen" if not current_stack_node["navigation_path"] else f"Screen_{fingerprint[:8]}"
            screen_type = "Home" if not current_stack_node["navigation_path"] else "General"
            
            if use_ollama and ollama_is_active:
                logger.info("Classifying screen with local Ollama AI...")
                try:
                    ai_name, ai_type = await classify_screen_with_ai(all_elements, screen_name, ollama_model, ollama_url)
                    screen_name = ai_name
                    screen_type = ai_type
                    logger.info(f"Ollama Screen Classification: '{screen_name}' (type: {screen_type})")
                except Exception as ai_err:
                    logger.warning(f"Ollama classification failed: {ai_err}")
            
            nodes[fingerprint] = {
                "node_id": node_id,
                "screen_name": screen_name,
                "screen_type": screen_type,
                "screenshot_url": "",  # Will be populated asynchronously by save_screen_job
                "elements": all_elements,
                "layout_tree": json_layout,
                "navigation_path": list(current_stack_node["navigation_path"])
            }
            
            discovered_screens_count += 1
            screenshot_filename = f"{fingerprint}.png"
            
            task = asyncio.create_task(
                save_screen_job(
                    storage_provider=storage_provider,
                    screenshot_bytes=screenshot_bytes,
                    folder=f"{run_folder}/screenshots",
                    filename=screenshot_filename,
                    run_id=actual_run_id,
                    app_name=actual_app_name,
                    screen_name=screen_name,
                    page_element={"elements": all_elements, "layout_tree": json_layout},
                    order_id=discovered_screens_count,
                    supabase_url=supabase_url,
                    supabase_key=supabase_key,
                    nodes_dict=nodes,
                    fingerprint=fingerprint
                )
            )
            parallel_tasks.append(task)
            
        node_data = nodes[fingerprint]
        
        # Print crawl state to stdout
        print("\n" + "="*70)
        print("                      APPIUM CRAWLER ACTIVE STATE")
        print("="*70)
        print(f"Target Package : {app_package}")
        print(f"Current Screen : {node_id} (Fingerprint: {fingerprint})")
        print(f"Visited Set    : {len(visited_elements)} elements")
        print("\n--- ACTIVE STACK ---")
        for idx, entry in enumerate(stack):
            marker = " -> " if idx == len(stack) - 1 else "    "
            print(f"{marker}[{idx}] {entry['node_id']}")
        print("="*70 + "\n")
        
        # Find interactive elements
        interactive_elements = []
        for el in node_data["elements"]:
            is_input = el.get("is_input", False)
            is_clickable = el.get("clickable", False) or el.get("focusable", False)
            if not (is_input or is_clickable) or not el.get("enabled", True):
                continue
            if any(ignored in el.get("class_name", "") for ignored in ignored_classes):
                continue
            res_id = el.get("resource_id", "")
            if "statusbar" in res_id.lower() or "navigationbar" in res_id.lower():
                continue
            interactive_elements.append(el)
            
        # Filter to unvisited elements only
        unvisited_interactive = []
        for el in interactive_elements:
            visited_key = f"{fingerprint}_{el['element_id']}"
            if visited_key not in visited_elements:
                unvisited_interactive.append(el)
                
        # Re-order/prioritize unvisited elements queue
        if unvisited_interactive:
            if use_ollama and ollama_is_active:
                logger.info("Prioritizing elements queue with local Ollama AI...")
                try:
                    unvisited_interactive = await prioritize_queue_with_ai(
                        unvisited_interactive,
                        node_data["screen_name"],
                        user_prompt,
                        ollama_model,
                        ollama_url
                    )
                except Exception as ai_err:
                    logger.warning(f"Ollama element prioritization failed: {ai_err}")
                    unvisited_interactive = prioritize_elements_heuristically(unvisited_interactive, visited_elements, fingerprint, allow_destructive)
            else:
                unvisited_interactive = prioritize_elements_heuristically(unvisited_interactive, visited_elements, fingerprint, allow_destructive)

        # 5. Process next unvisited element if available
        if unvisited_interactive:
            target_element = unvisited_interactive[0]
            el_id = target_element["element_id"]
            selector = target_element["selector"]
            bounds_str = target_element["bounds"]
            is_input = target_element.get("is_input") or "edittext" in target_element.get("class_name", "").lower()
            
            visited_key = f"{fingerprint}_{el_id}"
            visited_elements.append(visited_key)
            
            if is_input:
                val_to_type = None
                label_candidates = [
                    target_element.get("text", ""),
                    target_element.get("content_desc", ""),
                    target_element.get("resource_id", ""),
                    target_element.get("resource_id", "").split("/")[-1].lower(),
                    el_id.lower()
                ]
                
                # 1. Look for a direct match in prefills first
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
                        
                # 2. If no direct match and use_ollama is active, consult Ollama with prefills context
                if not val_to_type:
                    if use_ollama and ollama_is_active:
                        try:
                            val_to_type = await generate_input_value_with_ai(el_id, app_package, prefills, ollama_model, ollama_url)
                        except Exception:
                            pass
                            
                # 3. Ultimate fallback
                if not val_to_type:
                    val_to_type = "920"
                            
                last_action = {
                    "type": "type",
                    "selector": selector,
                    "bounds": bounds_str,
                    "value": val_to_type
                }
                
                logger.info(f"Prefilling input {el_id} with '{val_to_type}'...")
                try:
                    await client.call_tool("clear_element", {"selector": selector})
                    await client.call_tool("enter_text", {"selector": selector, "text": val_to_type})
                    await client.call_tool("hide_keyboard", {})
                except Exception as e:
                    logger.warning(f"Failed to enter text via Appium selector: {e}. Trying coordinate fallback...")
                    coords = parse_bounds(bounds_str)
                    if coords:
                        x1, y1, x2, y2 = coords
                        cx = (x1 + x2) // 2
                        cy = (y1 + y2) // 2
                        try:
                            await client.call_tool("tap_coordinate", {"x": cx, "y": cy})
                            await asyncio.sleep(0.5)
                            await client.call_tool("hide_keyboard", {})
                        except Exception as ce:
                            logger.error(f"Coordinate tap failed: {ce}")
                await asyncio.sleep(1.5)
                continue
            else:
                last_action = {
                    "type": "click",
                    "selector": selector,
                    "bounds": bounds_str
                }
                
                logger.info(f"Clicking element {el_id}...")
                clicked = False
                try:
                    await client.call_tool("click_element", {"selector": selector})
                    clicked = True
                except Exception as e:
                    logger.warning(f"Failed to click via Appium selector: {e}. Trying coordinate fallback...")
                    
                if not clicked and bounds_str:
                    coords = parse_bounds(bounds_str)
                    if coords:
                        x1, y1, x2, y2 = coords
                        cx = (x1 + x2) // 2
                        cy = (y1 + y2) // 2
                        try:
                            await client.call_tool("tap_coordinate", {"x": cx, "y": cy})
                            clicked = True
                        except Exception as ce:
                            logger.error(f"Coordinate tap failed: {ce}")
                await asyncio.sleep(2.0)
                continue
                
        # 6. Scroll down discovery before backtracking
        scroll_container_sel = find_scrollable_container(xml_text)
        current_scroll_count = scroll_tracker.get(fingerprint, 0)
        
        if scroll_container_sel and current_scroll_count < max_scrolls:
            logger.info(f"Scrollable container found: {scroll_container_sel}. Scrolling down (scroll {current_scroll_count + 1}/{max_scrolls})...")
            scrolled = await perform_scroll_down(client)
            if scrolled:
                scroll_tracker[fingerprint] = current_scroll_count + 1
                await wait_for_loading_indicators(client, max_wait_seconds=5.0)
                
                new_xml = await get_page_source_by_appium(client)
                new_elements = get_clickable_elements(new_xml)
                
                added = merge_new_elements(node_data["elements"], new_elements)
                logger.info(f"Scrolled down. Discovered {added} new elements on this screen.")
                if added > 0:
                    continue

        # 7. Backtrack if no unvisited elements and no scroll available
        logger.info("No unvisited elements on current screen. Backtracking...")
        target_ancestor_idx = -1
        for i in range(len(stack) - 2, -1, -1):
            ancestor = stack[i]
            if has_unvisited_elements(ancestor["fingerprint"]):
                target_ancestor_idx = i
                break
                
        if target_ancestor_idx == -1:
            logger.info("All discoverable paths fully crawled!")
            break
            
        popped_nodes = stack[target_ancestor_idx + 1:]
        stack[:] = stack[:target_ancestor_idx + 1]
        parent_node = stack[-1]
        parent_fingerprint = parent_node["fingerprint"]
        
        use_back = (len(popped_nodes) == 1)
        backtrack_success = False
        
        if use_back:
            logger.info("Attempting device back button backtrack...")
            try:
                await client.call_tool("back", {})
                await asyncio.sleep(1.5)
                back_xml = await get_page_source_by_appium(client)
                back_fp = get_screen_fingerprint(back_xml)
                if back_fp == parent_fingerprint:
                    backtrack_success = True
                    logger.info("Backtrack via back button successful!")
            except Exception as e:
                logger.warning(f"Failed back button backtrack: {e}")
                
        if not backtrack_success:
            logger.info("Backtrack via back button failed or skipped. Re-navigating via restart and replay...")
            await restart_and_replay(parent_node["navigation_path"])
            
        last_action = None
        
    # Await all parallel screen upload and save tasks before returning
    if parallel_tasks:
        logger.info(f"Waiting for {len(parallel_tasks)} parallel upload/save tasks to complete...")
        results = await asyncio.gather(*parallel_tasks, return_exceptions=True)
        for idx, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error(f"Parallel upload/save task {idx} failed: {res}")
        logger.info("All parallel upload/save tasks finished.")

    return {
        "nodes": nodes,
        "visited_elements": visited_elements,
        "steps_taken": steps_taken
    }
