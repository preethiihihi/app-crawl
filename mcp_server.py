import os
import sys
import json
import asyncio
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("Hybrid-Appium-ADB-MCP")

# Add the workspace root and routers path to PYTHONPATH to allow imports
workspace_root = os.path.dirname(os.path.abspath(__file__))
sys.path.append(workspace_root)
sys.path.append(os.path.join(workspace_root, "routers"))

from routers.appium_mcp import capture_screen_hybrid

# ==========================================
# TOOL 1: Capture Screen (Semantics + Vision)
# ==========================================
def parse_bounds_to_center(bounds_str: str) -> list[int] | None:
    """Parses bounds string '[x1,y1][x2,y2]' into absolute center coordinates [cx, cy]."""
    if not bounds_str:
        return None
    import re
    match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
    if match:
        x1, y1, x2, y2 = map(int, match.groups())
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        return [cx, cy]
    return None

@mcp.tool()
async def capture_screen(
    screen_name: str = "Claude_Screen",
    include_ocr: bool = False,
    include_opencv: bool = False,
    skip_ai_classification: bool = True,
    crop_bounds: str = None
) -> str:
    """
    Captures the current screen layout, visual OCR elements, OpenCV detected shapes, and screenshot path.
    Returns a JSON string containing the structural (XML) and visual (OCR/CV) elements with absolute coordinate mappings.
    """
    try:
        data = await capture_screen_hybrid(
            screen_name=screen_name,
            include_ocr=include_ocr,
            include_opencv=include_opencv,
            skip_ai_classification=skip_ai_classification,
            crop_bounds=crop_bounds
        )
        
        structural_elements = []
        for el in data.get("elements", []):
            bounds = el.get("bounds", "")
            center = parse_bounds_to_center(bounds)
            resource_id = el.get("selectors", {}).get("resource_id", "")
            structural_elements.append({
                "element_id": el.get("element_id"),
                "text": el.get("text", ""),
                "content_desc": el.get("content_desc", ""),
                "resource_id": resource_id,
                "bounds": bounds,
                "center": center
            })
            
        ocr_visual_elements = []
        for el in data.get("visual_elements", []):
            cx = el.get("center", {}).get("x")
            cy = el.get("center", {}).get("y")
            ocr_visual_elements.append({
                "id": el.get("visual_id"),
                "text": el.get("text"),
                "bounds": el.get("bounds"),
                "center": [cx, cy] if cx is not None and cy is not None else None
            })

        return json.dumps({
            "screen_name": data.get("screen_name"),
            "screen_type": data.get("screen_type"),
            "screenshot_path": data.get("screenshot_path"),
            "annotated_screenshot_path": data.get("annotated_screenshot_path"),
            "structural_elements": structural_elements,
            "ocr_visual_elements": ocr_visual_elements
        }, indent=2)
    except Exception as e:
        return f"Error capturing screen: {str(e)}"

# ==========================================
# TOOL 2: Click Coordinate
# ==========================================
@mcp.tool()
async def click_coordinate(x: int, y: int) -> str:
    """Clicks the specified x, y coordinate on the emulator screen."""
    import subprocess
    import shutil
    
    adb_path = shutil.which("adb") or os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")
    cmd = [adb_path, "shell", "input", "tap", str(x), str(y)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL)
    return f"Successfully clicked coordinate ({x}, {y})"

# ==========================================
# TOOL 3: Type Text
# ==========================================
@mcp.tool()
async def type_text(x: int, y: int, text_value: str) -> str:
    """Clicks the coordinate (x, y) to focus, clears it, and types the specified text value."""
    import subprocess
    import shutil
    
    adb_path = shutil.which("adb") or os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")
    
    # Tap to focus
    subprocess.run([adb_path, "shell", "input", "tap", str(x), str(y)], stdout=subprocess.DEVNULL)
    await asyncio.sleep(0.5)
    
    # Clear field
    clear_cmd = [adb_path, "shell", "input", "keyevent", "123"] + ["67"] * 40
    subprocess.run(clear_cmd, stdout=subprocess.DEVNULL)
    await asyncio.sleep(0.2)
    
    # Type text
    adb_text = text_value.replace(" ", "%s").replace('"', '\\"').replace("'", "\\'")
    subprocess.run([adb_path, "shell", "input", "text", adb_text], stdout=subprocess.DEVNULL)
    
    # Hide keyboard
    subprocess.run([adb_path, "shell", "input", "keyevent", "111"], stdout=subprocess.DEVNULL)
    return f"Successfully typed '{text_value}' at ({x}, {y})"

# ==========================================
# TOOL 4: Press Back Button
# ==========================================
@mcp.tool()
async def press_back() -> str:
    """Simulates pressing the device Back button."""
    import subprocess
    import shutil
    
    adb_path = shutil.which("adb") or os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")
    subprocess.run([adb_path, "shell", "input", "keyevent", "4"], stdout=subprocess.DEVNULL)
    return "Pressed Back button"

# ==========================================
# TOOL 5: Compress JSON File
# ==========================================
@mcp.tool()
async def compress_json_file(file_path: str, query_text: str = None) -> str:
    """
    Reads a JSON file from disk (such as an app_map.json or element layout list)
    and compresses it to significantly reduce the token size for the LLM.
    Optionally filters by query_text (for app maps) to focus only on relevant screens.
    """
    resolved_path = file_path
    if not os.path.isabs(resolved_path):
        workspace_path = os.path.join(workspace_root, resolved_path)
        if os.path.exists(workspace_path):
            resolved_path = workspace_path
        else:
            resolved_path = os.path.abspath(resolved_path)
            
    if not os.path.exists(resolved_path):
        return f"Error: File not found at {file_path} (resolved as {resolved_path})"
        
    try:
        with open(resolved_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        # Check if it looks like an app map
        if isinstance(data, dict) and ("nodes" in data or "app_name" in data):
            from routers.appium_mcp import compress_app_map_for_llm
            return compress_app_map_for_llm(data, query_text=query_text)
            
        # Check if it looks like a list of layout elements
        elif isinstance(data, list):
            from routers.appium_mcp import compress_elements_for_llm
            return compress_elements_for_llm(data)
            
        # Default: minify standard JSON
        else:
            return json.dumps(data, separators=(',', ':'))
    except Exception as e:
        return f"Error compressing JSON file: {str(e)}"

# ==========================================
# TOOL 6: Explode Grouped Element
# ==========================================
@mcp.tool()
async def explode_element(bounds: str, include_opencv: bool = False) -> str:
    """
    Takes the absolute bounds of a grouped layout component (e.g. '[0,741][1344,1353]'),
    crops the screen to these coordinates, runs fast local OCR, and optionally runs OpenCV
    contour detection to find non-text shapes/icons. Returns the child components in JSON.
    """
    try:
        from routers.appium_mcp import explode_element_by_bounds
        elements = await explode_element_by_bounds(bounds, include_opencv=include_opencv)
        return json.dumps(elements, indent=2)
    except Exception as e:
        return f"Error exploding element: {str(e)}"

# ==========================================
# TOOL 7: Autonomous Crawl Step
# ==========================================
@mcp.tool()
async def crawl_step(
    reset_state: bool = False,
    app_package: str = "com.curtain.tracking",
    app_activity: str = "com.curtain.tracking.MainActivity",
    prefill_data: dict = None,
    max_steps: int = 1
) -> str:
    """
    Performs one or more steps of the layout-based DFS app crawl.
    Filters clickable elements, pre-fills inputs if matched, clicks unvisited targets, and backtracks using a stack.
    If backtracking fails, it closes and restarts the app, then replays the stack path.
    """
    try:
        from routers.appium_mcp import crawl_step_logic
        
        results = []
        current_reset = reset_state
        for step in range(max_steps):
            res = await crawl_step_logic(
                reset_state=current_reset,
                app_package=app_package,
                app_activity=app_activity,
                prefill_data=prefill_data
            )
            results.append(res)
            
            # If the app completed crawling, stop the loop
            if res.get("status") == "completed":
                break
                
            current_reset = False
            # Wait for transitions and layout rendering on subsequent steps
            if step < max_steps - 1:
                await asyncio.sleep(1.2)
                
        return json.dumps(results if len(results) > 1 else results[0], indent=2)
    except Exception as e:
        return f"Error executing crawl step: {str(e)}"

if __name__ == "__main__":
    mcp.run()
