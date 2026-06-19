import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
from appium_service import AppiumMcpClient, ensure_appium_server

logger = logging.getLogger("functions_router")
router = APIRouter(prefix="/functions", tags=["Dynamic Functions"])

class ExecuteFunctionRequest(BaseModel):
    function_name: str
    params: dict = {}

# ==========================================
# REGISTRY OF HELPER FUNCTIONS
# ==========================================

def extract_element_signatures(xml_string: str) -> list:
    """
    Extracts signatures (Class + ResourceID) from an XML string
    to represent the structural fingerprint of a screen.
    Uses lists to allow counting of multiple identical elements.
    """
    import xml.etree.ElementTree as ET
    try:
        # Wrap XML if it lacks a root or is just fragments
        if not xml_string.strip().startswith('<?xml') and not xml_string.strip().startswith('<hierarchy'):
            xml_string = f"<hierarchy>{xml_string}</hierarchy>"
        
        root = ET.fromstring(xml_string)
        signatures = []
        for elem in root.iter():
            cls = elem.attrib.get('class', '')
            res_id = elem.attrib.get('resource-id', '')
            # We skip bounds because scrolling/animations change them constantly
            if cls:
                signatures.append(f"{cls}:{res_id}")
        return signatures
    except Exception as e:
        return [f"error: {str(e)}"]

def is_same_screen(xml_a: str, xml_b: str, threshold: float = 0.90) -> dict:
    """
    Calculates the Jaccard similarity between two UI screens using Multisets (Counters) 
    to determine if they are structurally identical.
    """
    from collections import Counter
    
    list_a = extract_element_signatures(xml_a)
    list_b = extract_element_signatures(xml_b)
    
    # If both failed to parse or are empty, we cannot confidently compare
    if not list_a and not list_b:
        return {"is_same": True, "similarity": 1.0}
        
    # Ignore error strings from parsing in calculation
    if any(item.startswith("error:") for item in list_a) or any(item.startswith("error:") for item in list_b):
         return {"is_same": False, "similarity": 0.0, "error": "Failed to parse one or both XML strings"}
         
    counter_a = Counter(list_a)
    counter_b = Counter(list_b)
    
    # Multiset intersection size: sum of minimum occurrences of each element
    intersection_size = sum(min(counter_a[k], counter_b[k]) for k in counter_a.keys())
    
    # Multiset union size: sum of maximum occurrences of each element
    all_keys = set(counter_a.keys()).union(counter_b.keys())
    union_size = sum(max(counter_a[k], counter_b[k]) for k in all_keys)
    
    similarity = intersection_size / union_size if union_size > 0 else 0
    return {"is_same": float(similarity) >= float(threshold), "similarity": float(similarity)}


def get_skeletal_hash(xml_string: str) -> dict:
    """
    Extracts the strict structural skeleton of the UI (Parent->Child tree of classes)
    ignoring all text, bounds, and IDs. Returns an MD5 hash of this skeleton.
    Filters out the Android Keyboard and System UI so opening an input doesn't change the hash.
    """
    import xml.etree.ElementTree as ET
    import hashlib
    try:
        if not xml_string.strip().startswith('<?xml') and not xml_string.strip().startswith('<hierarchy'):
            xml_string = f"<hierarchy>{xml_string}</hierarchy>"
        
        root = ET.fromstring(xml_string)
        
        # Recursive function to build the skeleton string
        def build_skeleton(node):
            # 1. Ignore the Keyboard and System UI (Status Bar/Nav Bar)
            pkg = node.attrib.get('package', '')
            if pkg in [
                'com.google.android.inputmethod.latin',
                'com.android.inputmethod.latin',
                'com.android.systemui',
                'com.samsung.android.honeyboard',
                'com.sec.android.inputmethod',
                'com.swiftkey.swiftkeyproject',
                'com.touchtype.swiftkey'
            ]:
                return ""
                
            cls = node.attrib.get('class', node.tag)
            
            # 2. Get children
            child_skeletons = [build_skeleton(child) for child in node]
            # Remove empty strings (filtered children)
            child_skeletons = [c for c in child_skeletons if c]
            
            if child_skeletons:
                return f"{cls}({','.join(child_skeletons)})"
            return cls

        skeleton_str = build_skeleton(root)
        hash_val = hashlib.md5(skeleton_str.encode('utf-8')).hexdigest()
        return {"hash": hash_val, "skeleton_preview": skeleton_str[:100] + "..."}
    except Exception as e:
        return {"error": str(e)}


def compute_phash(image_source: str) -> str:
    """
    Converts a screenshot into a 64-bit perceptual hash (pHash).

    Args:
        image_source: Either an absolute file path to the image
                      OR a base64-encoded PNG/JPG string.

    Returns:
        A hex string representing the pHash (e.g. "f8d4a2c1b3e5f7a9").
        Returns "error:<reason>" if the image cannot be processed.

    How it works:
        1. Resize the image to 32x32 thumbnail
        2. Convert to grayscale
        3. Apply DCT (Discrete Cosine Transform)
        4. Take the top-left 8x8 block (64 values)
        5. Each bit = 1 if value > average, else 0
        → Result is a 64-bit fingerprint of the visual structure

    The pHash is robust to:
        - Different text content on the same screen
        - Minor color/brightness changes
        - Slight layout shifts
    """
    try:
        import imagehash
        from PIL import Image
        import io, os, base64

        if os.path.isfile(image_source):
            # Input is a file path
            img = Image.open(image_source).convert("RGB")
        else:
            # Input is base64-encoded image data
            image_bytes = base64.b64decode(image_source)
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        ph = imagehash.phash(img)
        return str(ph)  # returns hex string like "f8d4a2c1..."
    except Exception as e:
        return f"error:{e}"


def is_same_screen_phash(image_a: str, image_b: str, threshold: int = 10) -> dict:
    """
    Compares two screenshots using perceptual hash (pHash) Hamming distance.

    Args:
        image_a: File path or base64 string of screenshot A
        image_b: File path or base64 string of screenshot B
        threshold: Max Hamming distance to consider screens identical.
                   0  = pixel-perfect match
                   10 = robust to minor UI differences (recommended)
                   20 = very loose match

    Returns:
        {
            "is_same": bool,
            "hamming_distance": int,   # 0 (identical) to 64 (completely different)
            "hash_a": str,
            "hash_b": str
        }
    """
    try:
        import imagehash

        hash_a_str = compute_phash(image_a)
        hash_b_str = compute_phash(image_b)

        if hash_a_str.startswith("error:") or hash_b_str.startswith("error:"):
            return {"is_same": False, "error": f"pHash failed: {hash_a_str} | {hash_b_str}"}

        hash_a = imagehash.hex_to_hash(hash_a_str)
        hash_b = imagehash.hex_to_hash(hash_b_str)

        distance = hash_a - hash_b  # Hamming distance (0 = identical, 64 = opposite)
        return {
            "is_same": distance <= threshold,
            "hamming_distance": distance,
            "hash_a": hash_a_str,
            "hash_b": hash_b_str
        }
    except Exception as e:
        return {"is_same": False, "error": str(e)}


def is_same_screen_cv(image_path_a: str, image_path_b: str, threshold: float = 0.95) -> dict:
    """
    Compares two screenshots using Computer Vision (Structural Similarity Index - SSIM).
    Requires OpenCV and scikit-image.
    """
    try:
        import cv2
        from skimage.metrics import structural_similarity as ssim
    except ImportError:
        return {"error": "Missing libraries. Please run: pip install opencv-python scikit-image"}
        
    try:
        img_a = cv2.imread(image_path_a)
        img_b = cv2.imread(image_path_b)
        
        if img_a is None or img_b is None:
            return {"error": "Could not read one or both image files from disk."}
            
        # Resize to match if they are slightly different sizes (prevents crash)
        if img_a.shape != img_b.shape:
            img_b = cv2.resize(img_b, (img_a.shape[1], img_a.shape[0]))
            
        # Convert to grayscale to focus on structure rather than slight color shifts
        gray_a = cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY)
        gray_b = cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY)
        
        score, _ = ssim(gray_a, gray_b, full=True)
        return {"is_same": float(score) >= float(threshold), "similarity": float(score)}
    except Exception as e:
        return {"error": str(e)}


def parse_appium_xml(xml_string: str) -> dict:
    """
    Parses raw Appium XML into a clean, optimized JSON structure.
    Throws away non-interactable layout wrappers and empty nodes.
    This is the "Smart Way" to extract the UI map for AI.
    """
    import xml.etree.ElementTree as ET
    try:
        if not xml_string.strip().startswith('<?xml') and not xml_string.strip().startswith('<hierarchy'):
            xml_string = f"<hierarchy>{xml_string}</hierarchy>"
        
        root = ET.fromstring(xml_string)
        clean_elements = []
        
        for elem in root.iter():
            is_clickable = elem.attrib.get('clickable') == 'true'
            is_focusable = elem.attrib.get('focusable') == 'true'
            is_scrollable = elem.attrib.get('scrollable') == 'true'
            is_checkable = elem.attrib.get('checkable') == 'true'
            is_checked = elem.attrib.get('checked') == 'true'
            is_selected = elem.attrib.get('selected') == 'true'
            is_long_clickable = elem.attrib.get('long-clickable') == 'true'
            is_enabled = elem.attrib.get('enabled') == 'true'
            is_password = elem.attrib.get('password') == 'true'
            
            text = elem.attrib.get('text', '')
            desc = elem.attrib.get('content-desc', '')
            resource_id = elem.attrib.get('resource-id', '')
            
            # 1. Ignore the Keyboard and System UI completely
            pkg = elem.attrib.get('package', '')
            if pkg in ['com.google.android.inputmethod.latin', 'com.android.systemui', 'com.samsung.android.honeyboard']:
                continue
            
            # 2. Middle-Ground Filter:
            # Keep element if it has ANY interaction, ANY state, or ANY content/ID.
            has_interaction = is_clickable or is_scrollable or is_focusable or is_checkable or is_long_clickable
            has_state = is_checked or is_selected or (not is_enabled)
            has_content = bool(text or desc or resource_id)
            
            if has_interaction or has_state or has_content:
                node_data = {
                    "class": elem.attrib.get('class', ''),
                    "resource_id": resource_id,
                    "text": text,
                    "content_desc": desc,
                    "bounds": elem.attrib.get('bounds', ''),
                    "clickable": is_clickable,
                    "scrollable": is_scrollable,
                    "focusable": is_focusable,
                    "checkable": is_checkable,
                    "checked": is_checked,
                    "selected": is_selected,
                    "long_clickable": is_long_clickable,
                    "enabled": is_enabled,
                    "password": is_password
                }
                clean_elements.append(node_data)
                
        return {"total_extracted": len(clean_elements), "elements": clean_elements}
    except Exception as e:
        return {"error": str(e)}


def extract_text_ocr(image_path: str) -> dict:
    """
    Uses EasyOCR to extract all visible text and their bounding boxes from a screenshot.
    This acts as a fallback for games or Flutter apps where the Appium XML is blank.
    """
    try:
        import easyocr
        logger.debug(f"extract_text_ocr: Initializing EasyOCR reader for {image_path}...")
        # Initialize reader (English). Setting gpu=False for max compatibility.
        reader = easyocr.Reader(['en'], gpu=False)
        
        # Read text from image
        logger.debug("extract_text_ocr: Running OCR extraction...")
        result = reader.readtext(image_path)
        logger.debug(f"extract_text_ocr: Found {len(result)} text regions.")
        
        extracted_text = []
        for (bbox, text, prob) in result:
            top_left = bbox[0]
            bottom_right = bbox[2]
            
            x1, y1 = int(top_left[0]), int(top_left[1])
            x2, y2 = int(bottom_right[0]), int(bottom_right[1])
            
            extracted_text.append({
                "text": text,
                "confidence": float(prob),
                "bounds": f"[{x1},{y1}][{x2},{y2}]"
            })
            
        return {"total_text_nodes": len(extracted_text), "elements": extracted_text}
    except ImportError:
        return {"error": "Missing library. Please run: pip install easyocr"}
    except Exception as e:
        return {"error": str(e)}


def draw_set_of_marks(image_path: str, nodes: list) -> dict:
    """
    Draws red numbered bounding boxes over the screenshot based on the provided nodes list.
    Saves the image as 'annotated_screen.png' in the same directory.
    """
    import cv2
    import os
    import re
    
    try:
        img = cv2.imread(image_path)
        if img is None:
            return {"error": "Could not read image file."}
            
        annotated_elements = []
        logger.debug(f"draw_set_of_marks: Drawing {len(nodes)} boxes on {image_path}...")
        
        for idx, elem in enumerate(nodes):
            bounds_str = elem.get('bounds', '')
            if not bounds_str:
                continue
                
            # Extract x1, y1, x2, y2 using regex
            match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
            if not match:
                continue
                
            x1, y1, x2, y2 = map(int, match.groups())
            
            # Draw rectangle (Red)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 3)
            
            # Draw number label with background
            label = f"[{idx}]"
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.8
            thickness = 2
            
            (label_width, label_height), baseline = cv2.getTextSize(label, font, font_scale, thickness)
            cv2.rectangle(img, (x1, y1 - label_height - baseline), (x1 + label_width, y1), (0, 0, 255), cv2.FILLED)
            cv2.putText(img, label, (x1, y1 - baseline), font, font_scale, (255, 255, 255), thickness)
            
            elem_copy = elem.copy()
            elem_copy["som_index"] = idx
            annotated_elements.append(elem_copy)
            
        output_path = os.path.join(os.path.dirname(image_path), "annotated_screen.png")
        cv2.imwrite(output_path, img)
        
        return {
            "annotated_image_path": output_path,
            "elements": annotated_elements
        }
    except Exception as e:
        return {"error": str(e)}


def compress_nodes_to_string(nodes: list) -> str:
    """
    Compresses a list of JSON nodes into a highly token-optimized pipe-separated string.
    Dynamically captures any boolean/string attributes that are truthy.
    """
    lines = ["ID | Text | Bounds | Attributes"]
    
    for node in nodes:
        idx = node.get("som_index", "")
        text = str(node.get("text", "") or node.get("content_desc", "") or "").replace("\n", " ").strip()
        bounds = node.get("bounds", "")
        
        dynamic_attrs = []
        for key, value in node.items():
            if key not in ["som_index", "text", "content_desc", "bounds"] and value:
                dynamic_attrs.append(f"{key}={value}")
                
        attr_string = ", ".join(dynamic_attrs)
        lines.append(f"{idx} | {text} | {bounds} | {attr_string}")
        
    return "\n".join(lines)


def synthesize_screen_node(annotated_image_path: str, nodes: list, ollama_model: str = "llava") -> dict:
    """
    Sends the annotated image and JSON to local Ollama to generate the final semantic Node JSON.
    """
    import base64
    import httpx
    import json
    import hashlib
    
    logger.debug(f"synthesize_screen_node called with image: {annotated_image_path}")
    
    try:
        import cv2
        img = cv2.imread(annotated_image_path)
        if img is None:
            return {"error": "Failed to read annotated image for resizing."}
            
        # Resize image to speed up LLM processing. Max width 800px.
        max_width = 800
        height, width = img.shape[:2]
        if width > max_width:
            ratio = max_width / width
            new_dim = (max_width, int(height * ratio))
            img = cv2.resize(img, new_dim, interpolation=cv2.INTER_AREA)
            logger.debug(f"Resized image for LLM from {width}x{height} to {new_dim[0]}x{new_dim[1]}")
            
        # Encode to compressed JPEG format in memory
        success, buffer = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not success:
            return {"error": "Failed to encode image to JPEG."}
            
        base64_image = base64.b64encode(buffer).decode('utf-8')
            
        # Compress nodes to save massive amounts of LLM tokens
        compact_text = compress_nodes_to_string(nodes)
        
        logger.debug(f"Sending prompt and {len(nodes)} compressed nodes to Ollama model: {ollama_model}...")
        prompt = f"""
You are an expert Mobile UI Analyzer. Look at the attached screenshot with red numbered boxes and the following compressed string representing those nodes.

Compressed Nodes:
{compact_text}

Your task is to output a single, final JSON Object representing this Screen Node.
1. Determine a `screen_type` (e.g., 'Login Screen', 'Home Feed').
2. Identify if the screen has `Scroll` capability overall.
3. For the elements array, filter out useless ones. Keep the important ones and add a `semantics` field explaining what it does.
4. If you see repeated structures, add a `dynamic_list` property to them.

Output ONLY valid JSON matching this structure:
{{
  "screen_type": "string",
  "Scroll": boolean,
  "elements": [
    {{
       "...": "COPY EVERY SINGLE ORIGINAL PROPERTY EXACTLY AS IT APPEARED IN THE INPUT DATA (bounds, clickable, checked, etc.)",
       "semantics": "string describing the action or purpose"
    }}
  ]
}}
"""
        response = httpx.post(
            "http://localhost:11434/api/generate",
            json={
                "model": ollama_model,
                "prompt": prompt,
                "images": [base64_image],
                "stream": False,
                "format": "json",
                "options": {
                    "num_ctx": 16384
                }
            },
            timeout=300.0
        )
        
        if response.status_code != 200:
            return {"error": f"Ollama API failed with status {response.status_code}: {response.text}"}
            
        result_json = response.json()
        llm_output = result_json.get("response", "{}")
        
        try:
            node_data = json.loads(llm_output)
            # Add a screen hash for uniqueness
            node_data["screen_hash"] = hashlib.md5(llm_output.encode('utf-8')).hexdigest()
            return {"status": "success", "node": node_data}
        except json.JSONDecodeError:
            return {"error": "LLM did not return valid JSON", "raw_output": llm_output}
            
    except Exception as e:
        return {"error": str(e)}


def process_screen_to_semantic_node(xml_string: str, image_path: str, ollama_model: str = "llava") -> dict:
    """
    Orchestrator function that combines the entire pipeline:
    1. Parses the XML to get clean nodes.
    2. Draws Set-of-Marks on the screenshot using the nodes.
    3. Sends everything to the LLM to get the final Semantic Node.
    """
    logger.info(f"Starting ultimate orchestrator on image: {image_path}")
    try:
        # Step 1: Parse XML
        logger.info("Step 1: Parsing Appium XML...")
        parsed_result = parse_appium_xml(xml_string=xml_string)
        if "error" in parsed_result:
            logger.error(f"XML Parsing failed: {parsed_result['error']}")
            return {"error": f"Failed to parse XML: {parsed_result['error']}", "partial_result": None}
            
        nodes = parsed_result.get("elements", [])
        if not nodes:
            logger.warning("No elements found in XML.")
            return {"error": "No elements found in XML.", "partial_result": parsed_result}
        
        logger.info(f"Extracted {len(nodes)} nodes from XML.")
        
        # Step 2: Draw Set-of-Marks
        logger.info("Step 2: Drawing Set-of-Marks on screenshot...")
        som_result = draw_set_of_marks(image_path=image_path, nodes=nodes)
        if "error" in som_result:
            logger.error(f"Set-of-Marks failed: {som_result['error']}")
            return {"error": f"Failed to draw Set-of-Marks: {som_result['error']}", "partial_result": parsed_result}
            
        annotated_image_path = som_result.get("annotated_image_path")
        annotated_nodes = som_result.get("elements", [])
        logger.info(f"Successfully drew Set-of-Marks to: {annotated_image_path}")
        
        # Step 3: Synthesize with LLM
        logger.info(f"Step 3: Synthesizing Screen Node with LLM ({ollama_model})...")
        final_result = synthesize_screen_node(
            annotated_image_path=annotated_image_path,
            nodes=annotated_nodes,
            ollama_model=ollama_model
        )
        
        if "error" in final_result:
            logger.error(f"LLM Synthesis failed: {final_result['error']}")
            return {"error": f"LLM Synthesis failed: {final_result['error']}", "partial_result": som_result}
        else:
            logger.info("Orchestrator completed successfully! Generated Semantic Node.")
             
        return final_result
    except Exception as e:
        logger.exception("Pipeline failure")
        return {
            "error": f"Pipeline failure: {str(e)}", 
            "partial_result": locals().get('som_result', locals().get('parsed_result'))
        }


def infer_semantics(elem: dict) -> str:
    """
    Derives a human-readable semantics description from an element's attributes
    using pure rule-based logic — no LLM required.
    Priority order: content_desc > text > resource_id > class-based fallback.
    """
    label = (elem.get("content_desc") or "").strip(", ").strip()
    if not label:
        label = (elem.get("text") or "").strip()
    if not label:
        # Extract the last segment of the resource_id (e.g. "com.app:id/btn_login" -> "btn login")
        res = (elem.get("resource_id") or "").strip()
        if res:
            label = res.split("/")[-1].replace("_", " ").replace("-", " ")

    cls = (elem.get("class") or "").split(".")[-1]  # e.g. "TextView"

    # Build action prefix from interaction flags
    if elem.get("clickable") and elem.get("focusable"):
        action = "Tap"
    elif elem.get("clickable"):
        action = "Tap"
    elif elem.get("focusable") and "EditText" in cls:
        action = "Type into"
    elif elem.get("focusable"):
        action = "Focus"
    elif elem.get("scrollable"):
        action = "Scroll"
    elif elem.get("checkable"):
        action = "Toggle"
    elif elem.get("long_clickable"):
        action = "Long-press"
    else:
        action = None

    # Class-based noun when no label is available
    class_noun_map = {
        "EditText": "input field",
        "Button": "button",
        "ImageButton": "icon button",
        "ImageView": "image",
        "TextView": "label",
        "CheckBox": "checkbox",
        "Switch": "toggle switch",
        "RadioButton": "radio button",
        "ScrollView": "scrollable container",
        "RecyclerView": "list",
        "ListView": "list",
        "ProgressBar": "progress indicator",
        "ViewGroup": "container",
        "FrameLayout": "container",
        "LinearLayout": "container",
        "ConstraintLayout": "container",
    }
    noun = class_noun_map.get(cls, cls)

    if label and action:
        return f"{action} '{label}'"
    elif label:
        return f"{noun}: {label}"
    elif action:
        return f"{action} {noun}"
    else:
        return f"{noun} (non-interactive)"


def process_screen_without_llm(xml_string: str, image_path: str) -> dict:
    """
    Orchestrator function that runs the pipeline WITHOUT the LLM step:
    1. Parses the Appium XML to get clean nodes.
    2. Draws Set-of-Marks (numbered bounding boxes) on the screenshot.
    Returns the annotated image path, all extracted + indexed elements,
    and a skeletal hash for screen deduplication — no LLM required.
    """
    logger.info(f"Starting no-LLM orchestrator on image: {image_path}")
    try:
        # Step 1: Parse XML
        logger.info("Step 1: Parsing Appium XML...")
        parsed_result = parse_appium_xml(xml_string=xml_string)
        if "error" in parsed_result:
            logger.error(f"XML Parsing failed: {parsed_result['error']}")
            return {"error": f"Failed to parse XML: {parsed_result['error']}", "partial_result": None}

        nodes = parsed_result.get("elements", [])
        if not nodes:
            logger.warning("No elements found in XML.")
            return {"error": "No elements found in XML.", "partial_result": parsed_result}

        logger.info(f"Extracted {len(nodes)} nodes from XML.")

        # Step 2: Draw Set-of-Marks
        logger.info("Step 2: Drawing Set-of-Marks on screenshot...")
        som_result = draw_set_of_marks(image_path=image_path, nodes=nodes)
        if "error" in som_result:
            logger.error(f"Set-of-Marks failed: {som_result['error']}")
            return {"error": f"Failed to draw Set-of-Marks: {som_result['error']}", "partial_result": parsed_result}

        annotated_image_path = som_result.get("annotated_image_path")
        annotated_nodes = som_result.get("elements", [])
        logger.info(f"Successfully drew Set-of-Marks to: {annotated_image_path}")

        # Step 3 (rule-based): Stamp each element with a semantics description — no LLM
        logger.info("Step 3: Inferring rule-based semantics for each element...")
        for elem in annotated_nodes:
            elem["semantics"] = infer_semantics(elem)

        # Compute skeletal hash for deduplication (no LLM needed)
        skeletal_hash_result = get_skeletal_hash(xml_string=xml_string)

        logger.info("No-LLM Orchestrator completed successfully.")
        return {
            "status": "success",
            "annotated_image_path": annotated_image_path,
            "total_elements": len(annotated_nodes),
            "elements": annotated_nodes,
            "skeletal_hash": skeletal_hash_result.get("hash"),
            "skeleton_preview": skeletal_hash_result.get("skeleton_preview"),
        }
    except Exception as e:
        logger.exception("No-LLM Pipeline failure")
        return {
            "error": f"Pipeline failure: {str(e)}",
            "partial_result": locals().get('som_result', locals().get('parsed_result'))
        }


def fast_extract_interactive_nodes(xml_string: str, image_path: str) -> dict:
    """
    Lightning-fast orchestrator that skips the LLM. 
    1. Parses the Appium XML.
    2. Filters the nodes to ONLY keep clickable, focusable (editable), or long-clickable elements.
    3. Draws OpenCV Set-of-Marks and returns the annotated image and JSON.
    """
    logger.info(f"Starting fast interactive node extraction on image: {image_path}")
    try:
        # Step 1: Parse XML
        parsed_result = parse_appium_xml(xml_string=xml_string)
        if "error" in parsed_result:
            return {"error": parsed_result["error"]}
            
        all_nodes = parsed_result.get("elements", [])
        
        # Step 2: Filter for interactive elements
        interactive_nodes = []
        for node in all_nodes:
            if node.get("clickable") or node.get("focusable") or node.get("long_clickable"):
                interactive_nodes.append(node)
                
        if not interactive_nodes:
            return {"error": "No interactive elements found.", "elements": []}
            
        # Re-index the Set-of-Marks IDs to be sequential for the filtered list
        for idx, node in enumerate(interactive_nodes):
            node["som_index"] = idx
            
        # Step 3: Draw Set-of-Marks
        som_result = draw_set_of_marks(image_path=image_path, nodes=interactive_nodes)
        
        return som_result
    except Exception as e:
        logger.exception("Fast extraction failure")
        return {"error": str(e)}


def build_screen_node(xml_string: str, image_path: str, screen_type: str = "") -> dict:
    """
    Builds a fully-structured Screen Node in the AppMap schema:

    Node = {
        screen_hash          : MD5 fingerprint of the screen structure
        screen_type          : human label (pass your own, or leave blank for auto-guess)
        scroll               : True if any scrollable element exists
        annotated_image_path : path to the Set-of-Marks screenshot
        elements             : [ { ...all XML props..., semantics } ]
    }

    ALL original XML properties (bounds, clickable, scrollable, focusable,
    checkable, checked, selected, long_clickable, enabled, password) are kept
    intact — nothing is dropped or normalized.
    """
    logger.info(f"build_screen_node: building node for image={image_path}")

    # Re-use the no-LLM orchestrator to get annotated elements + hash
    pipeline = process_screen_without_llm(xml_string=xml_string, image_path=image_path)
    if "error" in pipeline:
        return {"error": pipeline["error"], "partial_result": pipeline.get("partial_result")}

    elements = pipeline.get("elements", [])

    # Auto-detect scroll capability from elements
    has_scroll = any(e.get("scrollable") for e in elements)

    # Auto-guess screen_type from clickable element labels if caller didn't supply one
    if not screen_type:
        labels = []
        for e in elements:
            if e.get("clickable"):
                label = (e.get("content_desc") or "").strip(", ").strip() or (e.get("text") or "").strip()
                if label:
                    labels.append(label)
        screen_type = f"Screen with: {', '.join(labels[:3])}" if labels else "Unknown Screen"

    return {
        "screen_hash": pipeline.get("skeletal_hash"),
        "screen_type": screen_type,
        "scroll": has_scroll,
        "annotated_image_path": pipeline.get("annotated_image_path"),
        "elements": elements,
    }


def build_edge(from_hash: str, to_hash: str, actions_in_from: list) -> dict:
    """
    Builds an Edge between two screen nodes.

    Edge = {
        from             : screen_hash of the source screen
        to               : screen_hash of the destination screen
        actions_in_from  : [
            {
                action_type : "tap" | "type" | "scroll" | "long_press" | "swipe"
                on_element  : som_index (int) of the element that was acted on
                input_value : text typed (only for "type" actions, else null)
            }
        ]
    }

    Example actions_in_from:
      [{"action_type": "tap", "on_element": 4, "input_value": null}]
    """
    return {
        "from": from_hash,
        "to": to_hash,
        "actions_in_from": [
            {
                "action_type": a.get("action_type", "tap"),
                "on_element": a.get("on_element"),         # som_index integer
                "input_value": a.get("input_value", None)  # text for EditText, else null
            }
            for a in (actions_in_from or [])
        ]
    }


def init_app_map(app_name: str, app_package: str, app_main_activity: str = "") -> dict:
    """
    Scaffolds the top-level AppMap dictionary.

    AppMap = {
        app_name          : display name of the app
        app_package       : e.g. "com.curtain.tracking"
        app_main_activity : e.g. "com.curtain.tracking.MainActivity"
        nodes             : []  <- add Node dicts from build_screen_node()
        edges             : []  <- add Edge dicts from build_edge()
    }
    """
    return {
        "app_name": app_name,
        "app_package": app_package,
        "app_main_activity": app_main_activity,
        "nodes": [],
        "edges": []
    }


def find_shortest_path(edges: list, start_hash: str, target_hash: str) -> Optional[list]:
    """
    Finds the shortest sequence of transition edges from start_hash to target_hash.
    Uses Breadth-First Search (BFS) for pathfinding.
    Returns a list of edge dicts if a path is found, otherwise None.
    """
    if start_hash == target_hash:
        return []
        
    from collections import deque
    queue = deque([(start_hash, [])])
    visited = {start_hash}
    
    while queue:
        curr, path = queue.popleft()
        
        # Find all outgoing edges from curr
        for edge in edges:
            if edge.get("from") == curr:
                nxt = edge.get("to")
                if nxt not in visited:
                    visited.add(nxt)
                    new_path = path + [edge]
                    if nxt == target_hash:
                        return new_path
                    queue.append((nxt, new_path))
    return None


async def navigate_graph(client: AppiumMcpClient, app_map: dict, start_hash: str, target_hash: str) -> bool:
    """
    Navigates the mobile app from start_hash to target_hash using the edges and nodes defined in app_map.
    Translates edge actions into concrete commands and executes them on the emulator.
    Returns True if navigation succeeded, False otherwise.
    """
    logger.info(f"navigate_graph: Attempting to navigate from {start_hash} -> {target_hash}")
    path = find_shortest_path(app_map.get("edges", []), start_hash, target_hash)
    if path is None:
        logger.warning(f"No path found in app map from {start_hash} to {target_hash}")
        return False
        
    logger.info(f"navigate_graph: Found path of length {len(path)}. Executing transition steps...")
    
    for edge in path:
        from_hash = edge.get("from")
        actions = edge.get("actions_in_from", [])
        
        # Locate the source node in the app_map nodes
        source_node = None
        for node in app_map.get("nodes", []):
            if node.get("screen_hash") == from_hash:
                source_node = node
                break
                
        if not source_node:
            logger.error(f"navigate_graph: Source node '{from_hash}' not found in app map nodes.")
            return False
            
        elements = source_node.get("elements", [])
        
        for act in actions:
            action_type = act.get("action_type")
            on_element_idx = act.get("on_element")
            input_val = act.get("input_value")
            
            if action_type == "back":
                logger.info("navigate_graph: Simulating system back button...")
                try:
                    await client.call_tool("back", {})
                except Exception as e:
                    logger.error(f"navigate_graph: Back call failed: {e}")
                    return False
                await wait_for_loading_indicators(client, max_wait_seconds=10.0)
                continue
                
            # Find the corresponding element in elements
            target_el = None
            for el in elements:
                if el.get("som_index") == on_element_idx:
                    target_el = el
                    break
                    
            if not target_el:
                logger.error(f"navigate_graph: Element with som_index={on_element_idx} not found in node '{from_hash}' elements.")
                return False
                
            # Convert edge action to concrete Appium command script
            commands = action_to_script(action_type, target_el, input_val)
            logger.info(f"navigate_graph: Executing command sequence: {commands}")
            success = await run_script(client, commands)
            if not success:
                logger.error("navigate_graph: Command execution failed.")
                return False
                
            await wait_for_loading_indicators(client, max_wait_seconds=10.0)
            
    logger.info("navigate_graph: Successfully navigated to target screen!")
    return True


def is_same_screen_node(xml_a: str, node: dict, threshold: float = 0.90) -> bool:
    """
    Calculates the Jaccard similarity between two UI screens: a live XML string (xml_a)
    and a stored app_map node elements list (node["elements"]).
    """
    from collections import Counter
    
    list_a = extract_element_signatures(xml_a)
    list_b = [f"{el.get('class')}:{el.get('resource_id')}" for el in node.get("elements", [])]
    
    # If both failed to parse or are empty, we cannot confidently compare
    if not list_a and not list_b:
        return True
        
    # Ignore error strings from parsing in calculation
    if any(item.startswith("error:") for item in list_a):
         return False
         
    counter_a = Counter(list_a)
    counter_b = Counter(list_b)
    
    # Multiset intersection size: sum of minimum occurrences of each element
    intersection_size = sum(min(counter_a[k], counter_b[k]) for k in counter_a.keys())
    
    # Multiset union size: sum of maximum occurrences of each element
    all_keys = set(counter_a.keys()).union(counter_b.keys())
    union_size = sum(max(counter_a[k], counter_b[k]) for k in all_keys)
    
    similarity = intersection_size / union_size if union_size > 0 else 0
    return float(similarity) >= float(threshold)


async def identify_current_screen(client: AppiumMcpClient, app_map: dict) -> Optional[str]:
    """
    Captures the current XML and screenshot from the device, calculates skeletal hash,
    and checks against all nodes in app_map using skeletal hash, Jaccard similarity.
    Returns the matching screen_hash, or None if no match is found.
    """
    try:
        resp = await client.call_tool("get_page_source", {})
        if resp.get("isError"):
            logger.error(f"identify_current_screen: get_page_source failed: {resp}")
            return None
        content = resp.get("content", [])
        xml_string = ""
        for item in content:
            if item.get("type") == "text":
                xml_string += item.get("text", "")
    except Exception as e:
        logger.error(f"identify_current_screen: Failed to get XML: {e}")
        return None

    if not xml_string:
        return None

    # 1. Skeletal Hash check
    hash_res = get_skeletal_hash(xml_string=xml_string)
    current_hash = hash_res.get("hash")
    if not current_hash:
        return None

    logger.info(f"identify_current_screen: Live Screen Skeletal Hash is {current_hash}")

    # Check for exact skeletal hash match
    for node in app_map.get("nodes", []):
        if node.get("screen_hash") == current_hash:
            logger.info(f"identify_current_screen: Exact skeletal hash match found: {current_hash}")
            return current_hash

    # 2. Check Jaccard similarity of element signatures
    for node in app_map.get("nodes", []):
        if is_same_screen_node(xml_string, node):
            matched_hash = node.get("screen_hash")
            logger.info(f"identify_current_screen: Screen matched via Jaccard similarity to node: {matched_hash}")
            return matched_hash

    logger.warning("identify_current_screen: No matching screen found in app map.")
    return None


async def navigate_to_last_stopped_screen(
    client: AppiumMcpClient,
    app_map: dict,
    current_screen_hash: Optional[str] = None,
    target_screen_hash: Optional[str] = None
) -> dict:
    """
    Navigates from the current screen (or dynamically detected current screen)
    to the target screen (or the last stopped crawl screen from app_map)
    using only the existing edges in the app_map.
    """
    if not target_screen_hash:
        target_screen_hash = app_map.get("last_stopped_screen_hash")
        if not target_screen_hash:
            logger.error("navigate_to_last_stopped_screen: No last stopped screen hash found in app_map metadata.")
            return {"status": "error", "message": "No last stopped screen hash found in app_map metadata."}

    if not current_screen_hash:
        logger.info("navigate_to_last_stopped_screen: Detecting current screen dynamically...")
        detected_hash = await identify_current_screen(client, app_map)
        if not detected_hash:
            logger.error("navigate_to_last_stopped_screen: Could not identify current screen from device UI structure.")
            return {"status": "error", "message": "Could not identify current screen from device UI structure."}
        current_screen_hash = detected_hash

    logger.info(f"navigate_to_last_stopped_screen: Starting transition from '{current_screen_hash}' -> '{target_screen_hash}'")

    if current_screen_hash == target_screen_hash:
        logger.info("navigate_to_last_stopped_screen: Already on the target screen.")
        return {"status": "success", "message": "Already on the target screen.", "path_length": 0}

    # Execute graph navigation
    success = await navigate_graph(client, app_map, current_screen_hash, target_screen_hash)
    if success:
        logger.info("navigate_to_last_stopped_screen: Navigation completed successfully!")
        return {"status": "success", "message": "Successfully navigated to target screen."}
    else:
        logger.error("navigate_to_last_stopped_screen: Navigation failed or no path exists between screens.")
        return {"status": "error", "message": "Navigation failed or no path exists between screens."}


# ==========================================
# AUTONOMOUS EXPLORATION PIPELINE ROUTE
# ==========================================

import asyncio
import os
import json
import base64
from typing import List, Dict, Optional, Any
from appium_service import AppiumMcpClient, ensure_appium_server

class ExplorePipelineRequest(BaseModel):
    deviceName: str = "Android Emulator"
    udid: str = "emulator-5554"
    appPackage: str
    appActivity: str
    deviceType: str = "local emulator"
    max_steps: int = 10
    use_ollama: bool = True
    prefill_data: Optional[dict] = None

async def prioritize_queue(elements: list, screen_type: str) -> list:
    """
    Deterministically prioritizes elements to explore without calling any LLM.
    Inputs are placed first, then standard interactive elements, and navigation/back elements last.
    """
    if not elements:
        return []
        
    inputs = []
    others = []
    navigation = []
    
    for el in elements:
        cls = (el.get("class") or "").lower()
        res_id = (el.get("resource_id") or "").lower()
        text = (el.get("text") or "").lower()
        desc = (el.get("content_desc") or "").lower()
        
        # Identify exit/back/cancel/logout elements to put at the end
        is_nav_exit = any(kw in res_id or kw in text or kw in desc for kw in [
            "back", "exit", "cancel", "logout", "close", "signout", "quit", "dismiss"
        ])
        
        is_input = "edittext" in cls or "input" in cls
        
        if is_nav_exit:
            navigation.append(el)
        elif is_input:
            inputs.append(el)
        else:
            others.append(el)
            
    return inputs + others + navigation


async def resolve_input_value(element: dict, prefill_data: Optional[dict] = None) -> str:
    """
    Deterministically resolves a realistic text input value for an EditText element.
    First checks prefill_data for matches, then falls back to heuristics based on element identifiers.
    """
    res_id = (element.get("resource_id") or "").lower()
    text_val = (element.get("text") or "").strip()
    text = text_val.lower()
    desc = (element.get("content_desc") or "").lower()
    cls = (element.get("class") or "").lower()
    
    # 1. Check if the element text already matches any prefill value
    if prefill_data:
        # Exact match check first
        for key, val in prefill_data.items():
            if str(val).strip() == text_val:
                logger.info(f"Field already contains prefill value '{val}' for key '{key}'")
                return str(val)
                
        # Matching keys check next
        for key, val in prefill_data.items():
            key_lower = key.lower()
            if (key_lower in res_id or 
                key_lower in desc or 
                key_lower in text):
                logger.info(f"Direct prefill match found for key '{key}': '{val}'")
                return str(val)
                
    # 2. Heuristics based on field attributes
    if "phone" in res_id or "phone" in desc or "phone" in text or "mobile" in res_id:
        return "9876543210"
    if "email" in res_id or "email" in desc or "email" in text or "mail" in res_id:
        return "test@example.com"
    if "password" in res_id or "password" in desc or "password" in text or "pwd" in res_id or "pass" in res_id:
        return "TestPassword123!"
    if "number" in res_id or "number" in desc or "age" in res_id or "zip" in res_id or "code" in res_id:
        return "12345"
    if "name" in res_id or "name" in desc or "name" in text:
        return "John Doe"
        
    return "Test Input"


def parse_bounds(bounds_str: str) -> Optional[tuple]:
    """Parse '[x1,y1][x2,y2]' into integers."""
    import re
    if not bounds_str:
        return None
    match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
    if match:
        return tuple(map(int, match.groups()))
    return None

def action_to_script(action_type: str, element: dict, input_value: Optional[str] = None) -> list:
    """
    Translates a deterministic action type and element bounds into concrete Appium MCP tool calls.
    Uses element selector (click_element) as the primary method, with coordinate-based tapping as a fallback.
    """
    commands = []
    res_id = element.get("resource_id")
    text = element.get("text")
    cls = element.get("class")
    bounds = element.get("bounds")
    
    # Calculate center coordinates
    coords = parse_bounds(bounds)
    cx, cy = None, None
    if coords:
        x1, y1, x2, y2 = coords
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        
    # Build selector for primary
    selector = ""
    if res_id:
        selector = f"id={res_id}"
    elif text:
        tag = cls if cls else "*"
        if "'" in text:
            selector = f'xpath=//{tag}[@text="{text}"]'
        else:
            selector = f"xpath=//{tag}[@text='{text}']"
    elif bounds:
        tag = cls if cls else "*"
        selector = f"xpath=//{tag}[@bounds='{bounds}']"
    else:
        tag = cls if cls else "*"
        selector = f"xpath=//{tag}"
        
    if action_type == "type":
        # 1. Click/focus the input field
        if cx is not None and cy is not None:
            commands.append({
                "tool": "click_element",
                "arguments": {"selector": selector},
                "fallback_tool": "tap_coordinate",
                "fallback_arguments": {"x": cx, "y": cy}
            })
        else:
            commands.append({
                "tool": "click_element",
                "arguments": {"selector": selector}
            })
            
        # 2. Type text using the focused element xpath fallback
        commands.append({
            "tool": "enter_text",
            "arguments": {"selector": selector, "text": input_value or "Test Input"},
            "fallback_tool": "enter_text",
            "fallback_arguments": {"selector": "xpath=//*[@focused='true']", "text": input_value or "Test Input"}
        })
    else:
        # Standard Tap action
        if cx is not None and cy is not None:
            commands.append({
                "tool": "click_element",
                "arguments": {"selector": selector},
                "fallback_tool": "tap_coordinate",
                "fallback_arguments": {"x": cx, "y": cy}
            })
        else:
            commands.append({
                "tool": "click_element",
                "arguments": {"selector": selector}
            })
    return commands


async def run_script(client: AppiumMcpClient, commands: list) -> bool:
    """
    Executes a sequence of Appium MCP commands.
    Falls back to coordinate tapping if a click_element command fails.
    """
    for cmd in commands:
        tool = cmd["tool"]
        args = cmd["arguments"]
        logger.info(f"Executing command: {tool} with args {args}")
        try:
            resp = await client.call_tool(tool, args)
            if resp.get("isError"):
                err_msg = resp.get("content", [{}])[0].get("text", "Unknown tool error")
                raise Exception(err_msg)
            await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"Command failed: {tool}({args}) - error: {e}")
            fallback_tool = cmd.get("fallback_tool")
            fallback_args = cmd.get("fallback_arguments")
            if fallback_tool and fallback_args:
                logger.info(f"Attempting fallback: {fallback_tool} with args {fallback_args}")
                try:
                    resp_fb = await client.call_tool(fallback_tool, fallback_args)
                    if resp_fb.get("isError"):
                        err_msg_fb = resp_fb.get("content", [{}])[0].get("text", "Unknown tool error")
                        raise Exception(err_msg_fb)
                    await asyncio.sleep(1.5)
                except Exception as fe:
                    logger.error(f"Fallback command also failed: {fallback_tool}({fallback_args}) - error: {fe}")
                    return False
            else:
                return False
    return True


async def wait_for_loading_indicators(client: AppiumMcpClient, max_wait_seconds: float = 60.0) -> None:
    """
    Polls the screen layout to wait for loading indicators (spinners, progress bars) to disappear,
    and also waits for the page structure/source to stabilize.
    """
    import xml.etree.ElementTree as ET
    import asyncio
    
    # 1. Initial delay to allow screen transition to start and keyboard to settle
    await asyncio.sleep(2.5)
    
    start_time = asyncio.get_event_loop().time()
    last_xml = None
    stable_count = 0
    
    while (asyncio.get_event_loop().time() - start_time) < max_wait_seconds:
        try:
            resp = await client.call_tool("get_page_source", {})
            content = resp.get("content", [])
            xml_text = "".join(item.get("text", "") for item in content if item.get("type") == "text").strip()
            
            if not xml_text:
                await asyncio.sleep(1.0)
                continue
                
            # Parse XML to search for loading indicators
            loader_found = False
            try:
                root = ET.fromstring(xml_text.encode("utf-8"))
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
            except Exception:
                # Fallback if XML is malformed
                xml_lower = xml_text.lower()
                if "progressbar" in xml_lower or "progressdialog" in xml_lower or any(kw in xml_lower for kw in ["loading", "spinner", "progress", "waiting"]):
                    loader_found = True
            
            if loader_found:
                logger.info("Loading spinner or progress bar detected. Waiting for it to finish...")
                stable_count = 0  # Reset stability counter
                await asyncio.sleep(1.5)
                continue
            
            # Layout stability check
            if last_xml and xml_text == last_xml:
                stable_count += 1
                if stable_count >= 2:
                    logger.info("Page layout stabilized.")
                    break
            else:
                stable_count = 0
                
            last_xml = xml_text
            await asyncio.sleep(1.0)
            
        except Exception as e:
            logger.warning(f"Error checking loading indicators: {e}")
            break


async def explore_pipeline(
    client: AppiumMcpClient,
    app_package: str,
    app_activity: str,
    max_steps: int = 10,
    prefill_data: Optional[dict] = None
) -> dict:
    """
    Autonomous state-aware crawl loop using the new pipeline logic.
    Constructs and returns the full app map structure cleanly using all local helper functions.
    """
    logger.info(f"Starting crawl for {app_package}/{app_activity}")
    
    app_map = init_app_map(app_name="Curtain Tracker", app_package=app_package, app_main_activity=app_activity)
    map_path = "/Users/preethichitte/Documents/artifacts/curtain_tracker_app_map.json"
    
    def save_current_map():
        try:
            if current_hash:
                app_map["last_stopped_screen_hash"] = current_hash
            os.makedirs(os.path.dirname(map_path), exist_ok=True)
            with open(map_path, "w", encoding="utf-8") as f:
                json.dump(app_map, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save app map: {e}")
            
    visited_screens = {}
    stack = []
    steps_taken = 0
    history_log = []
    last_action_commands = []
    
    # Track transition states for edge building
    last_screen_hash = None
    last_action_type = None
    last_som_idx = None
    last_input_value = None
    current_hash = None
    
    async def get_screenshot(step_idx: int) -> str:
        temp_dir = "./temp_screenshots"
        os.makedirs(temp_dir, exist_ok=True)
        img_path = os.path.join(temp_dir, f"step_{step_idx}.png")
        try:
            resp = await client.call_tool("take_screenshot", {})
            if resp.get("isError"):
                logger.error(f"take_screenshot returned error: {resp}")
                return ""
            content = resp.get("content", [])
            base64_data = ""
            for item in content:
                if item.get("type") == "image":
                    base64_data = item.get("data", "")
                    break
            if not base64_data:
                for item in content:
                    if item.get("type") == "text":
                        base64_data = item.get("text", "")
                        break
            if base64_data:
                if "," in base64_data:
                    base64_data = base64_data.split(",", 1)[1]
                img_bytes = base64.b64decode(base64_data)
                with open(img_path, "wb") as f:
                    f.write(img_bytes)
                return os.path.abspath(img_path)
        except Exception as e:
            logger.error(f"Failed to save screenshot at step {step_idx}: {e}")
        return ""

    async def get_xml() -> str:
        try:
            resp = await client.call_tool("get_page_source", {})
            if resp.get("isError"):
                logger.error(f"get_page_source returned error: {resp}")
                return ""
            content = resp.get("content", [])
            xml_text = ""
            for item in content:
                if item.get("type") == "text":
                    xml_text += item.get("text", "")
            return xml_text
        except Exception as e:
            logger.error(f"Failed to get page source: {e}")
            return ""

    async def replay_path(target_path: list):
        logger.info(f"Replaying path to target screen (path length={len(target_path)})...")
        try:
            await client.call_tool("terminate_app", {"appPackage": app_package})
        except Exception:
            pass
        await asyncio.sleep(2.0)
        try:
            await client.call_tool("activate_app", {"appPackage": app_package})
        except Exception:
            pass
        await asyncio.sleep(4.0)
        
        for cmd in target_path:
            await run_script(client, [cmd])
            await asyncio.sleep(1.5)

    # No app restart or activation — crawl starts directly from the current visible screen.
    logger.info(f"Starting crawl from the current screen of '{app_package}' — no reset, no restart.")

    for step in range(max_steps):
        steps_taken += 1
        logger.info(f"--- Crawl Loop Step {steps_taken} / {max_steps} ---")
        
        # Wait for loading screens to clear before capturing XML and screenshots
        await wait_for_loading_indicators(client, max_wait_seconds=60.0)
        
        try:
            pkg_resp = await client.call_tool("get_current_package", {})
            pkg_name = ""
            for item in pkg_resp.get("content", []):
                if item.get("type") == "text":
                    pkg_name = item.get("text", "").strip()
            if pkg_name and pkg_name != app_package:
                logger.warning(f"App escaped to package '{pkg_name}'. Relaunching target app...")
                await client.call_tool("back", {})
                await asyncio.sleep(2.0)
                
                pkg_resp2 = await client.call_tool("get_current_package", {})
                pkg_name2 = ""
                for item in pkg_resp2.get("content", []):
                    if item.get("type") == "text":
                        pkg_name2 = item.get("text", "").strip()
                if pkg_name2 and pkg_name2 != app_package:
                    await client.call_tool("activate_app", {"appPackage": app_package})
                    await asyncio.sleep(4.0)
        except Exception as e:
            logger.warning(f"Failed to check/recovery escape package: {e}")

        xml_string = await get_xml()
        if not xml_string:
            logger.error("Empty page source XML. App process or instrumentation might have crashed. Attempting recovery...")
            try:
                await client.call_tool("terminate_app", {"appPackage": app_package})
                await asyncio.sleep(2.0)
                await client.call_tool("activate_app", {"appPackage": app_package})
                await asyncio.sleep(5.0)
            except Exception as re:
                logger.error(f"Failed to relaunch app during recovery: {re}")
            continue
            
        screenshot_path = await get_screenshot(step)
        if not screenshot_path:
            logger.error("Empty screenshot path. Skipping step.")
            continue

        # 1. Screen structural hash deduplication
        hash_res = get_skeletal_hash(xml_string=xml_string)
        current_hash = hash_res.get("hash")
        logger.info(f"Current Screen Hash: {current_hash}")

        is_visited = current_hash in visited_screens
        matched_hash = current_hash if is_visited else None

        if not is_visited:
            # 2. Check Jaccard similarity of element signatures
            for v_hash, v_data in visited_screens.items():
                if "xml" in v_data:
                    sim_res = is_same_screen(xml_string, v_data["xml"])
                    if sim_res.get("is_same"):
                        logger.info(f"Screen structurally matches visited screen '{v_hash}' via Jaccard similarity ({sim_res.get('similarity'):.2f})")
                        is_visited = True
                        matched_hash = v_hash
                        break
                        
            # 3. Check Computer Vision SSIM image similarity
            if not is_visited and screenshot_path:
                for v_hash, v_data in visited_screens.items():
                    if "screenshot_path" in v_data and v_data["screenshot_path"]:
                        cv_res = is_same_screen_cv(screenshot_path, v_data["screenshot_path"])
                        if cv_res.get("is_same"):
                            logger.info(f"Screen visually matches visited screen '{v_hash}' via CV similarity ({cv_res.get('similarity'):.2f})")
                            is_visited = True
                            matched_hash = v_hash
                            break

        if is_visited and matched_hash:
            current_hash = matched_hash

        # 4. Edge building for the transition
        if last_screen_hash:
            edge = build_edge(
                from_hash=last_screen_hash,
                to_hash=current_hash,
                actions_in_from=[{"action_type": last_action_type, "on_element": last_som_idx, "input_value": last_input_value}]
            )
            # Avoid duplicate edges
            edge_exists = False
            for existing_edge in app_map["edges"]:
                if (existing_edge["from"] == edge["from"] and 
                    existing_edge["to"] == edge["to"] and 
                    existing_edge["actions_in_from"] == edge["actions_in_from"]):
                    edge_exists = True
                    break
            if not edge_exists:
                app_map["edges"].append(edge)
            last_screen_hash = None

        # 5. Stack management
        stack_idx = -1
        for idx, entry in enumerate(stack):
            if entry["skeletal_hash"] == current_hash:
                stack_idx = idx
                break
                
        if stack_idx != -1:
            logger.info(f"Aligned stack: returned to screen in stack at index {stack_idx}")
            stack = stack[:stack_idx + 1]
        elif not stack:
            stack.append({"skeletal_hash": current_hash, "path_to_reach": []})
        else:
            path_to_reach = list(stack[-1]["path_to_reach"])
            if last_action_commands:
                path_to_reach.extend(last_action_commands)
            stack.append({"skeletal_hash": current_hash, "path_to_reach": path_to_reach})
            logger.info(f"Pushed new screen to stack. New stack size: {len(stack)}")

        # 6. Screen analysis & registration
        if not is_visited:
            logger.info(f"Discovered new screen structure! Analyzing: {current_hash}")
            
            # Use fast_extract_interactive_nodes to label only interactive elements on screenshot
            analysis = fast_extract_interactive_nodes(xml_string=xml_string, image_path=screenshot_path)
            if "error" in analysis:
                logger.error(f"fast_extract_interactive_nodes failed: {analysis['error']}")
                continue
                
            elements = analysis.get("elements", [])
            screen_type = "Unknown Screen"
            clickable_labels = []
            for e in elements:
                if e.get("clickable"):
                    lbl = (e.get("content_desc") or "").strip() or (e.get("text") or "").strip()
                    if lbl:
                        clickable_labels.append(lbl)
            if clickable_labels:
                screen_type = f"Screen with {', '.join(clickable_labels[:3])}"
                
            interactive_elements = []
            for el in elements:
                cls_name = el.get("class", "")
                cls_short = cls_name.split('.')[-1]
                
                # Exclude layout/scroll containers without text or content description
                # EXCEPT when they are explicitly clickable or long_clickable
                is_container = "Layout" in cls_short or "ViewGroup" in cls_short or "ScrollView" in cls_short
                if is_container:
                    if not el.get("clickable") and not el.get("long_clickable"):
                        if not el.get("text") and not el.get("content_desc"):
                            continue
                
                # Exclude back buttons/arrows from forward exploration
                res_id = (el.get("resource_id") or "").lower()
                text = (el.get("text") or "").lower()
                desc = (el.get("content_desc") or "").lower()
                is_back = any(kw in res_id or kw in text or kw in desc for kw in [
                    "back", "prev", "return", "navigate_up"
                ])
                if is_back:
                    logger.info(f"Skipping back element from forward queue: {el.get('semantics') or text or desc}")
                    continue
                        
                interactive_elements.append(el)
                        
            logger.info("Prioritizing interactive elements queue...")
            prioritized_queue = await prioritize_queue(interactive_elements, screen_type)
            
            # Use build_screen_node (which runs process_screen_without_llm internally) to build the app map Node
            map_node = build_screen_node(xml_string=xml_string, image_path=screenshot_path, screen_type=screen_type)
            map_node["screen_hash"] = current_hash # ensure consistent hashes
            app_map["nodes"].append(map_node)
            save_current_map()
            
            visited_screens[current_hash] = {
                "screen_type": screen_type,
                "elements": elements,
                "queue": prioritized_queue,
                "visited_indices": set(),
                "path_to_reach": list(stack[-1]["path_to_reach"]) if stack else [],
                "xml": xml_string,
                "screenshot_path": screenshot_path,
                "annotated_image_path": analysis.get("annotated_image_path")
            }
            logger.info(f"Screen registered. {len(prioritized_queue)} interactive elements queued.")

        current_state = visited_screens[current_hash]
        
        next_element = None
        input_value = None
        while True:
            candidate = None
            for el in current_state["queue"]:
                som_idx = el.get("som_index")
                # Skip if already explored (double guard alongside visited_indices)
                if el.get("explored"):
                    continue
                if som_idx not in current_state["visited_indices"]:
                    candidate = el
                    break
            
            if not candidate:
                break
                
            som_idx = candidate.get("som_index")
            current_state["visited_indices"].add(som_idx)
            
            cls = candidate.get("class") or ""
            text = candidate.get("text") or ""
            is_input = "EditText" in cls or "Input" in cls or cls.endswith("EditText")
            
            if is_input:
                resolved_val = await resolve_input_value(candidate, prefill_data)
                if text.strip() == resolved_val.strip():
                    logger.info(f"Field with text '{text}' is already filled with correct value '{resolved_val}'. Skipping.")
                    continue
                input_value = resolved_val
            
            next_element = candidate
            break
            
        if next_element:
            som_idx = next_element.get("som_index")
            logger.info(f"Selected element to explore: som_index={som_idx}, resource_id={next_element.get('resource_id')}, text={next_element.get('text')}")
            
            cls = next_element.get("class") or ""
            res_id = next_element.get("resource_id") or ""
            text = next_element.get("text") or ""
            desc = next_element.get("content_desc") or ""
            bounds_str = next_element.get("bounds") or ""
            
            is_input = "EditText" in cls or "Input" in cls or cls.endswith("EditText")
            action_type = "type" if is_input else "tap"
            
            coords = parse_bounds(bounds_str)
            cx, cy = None, None
            if coords:
                x1, y1, x2, y2 = coords
                cx = (x1 + x2) // 2
                cy = (y1 + y2) // 2
                
            selector = ""
            if res_id:
                selector = f"id={res_id}"
            elif text:
                tag = cls if cls else "*"
                if "'" in text:
                    selector = f'xpath=//{tag}[@text="{text}"]'
                else:
                    selector = f"xpath=//{tag}[@text='{text}']"
            elif bounds_str:
                tag = cls if cls else "*"
                selector = f"xpath=//{tag}[@bounds='{bounds_str}']"
                
            action = {
                "action_type": action_type,
                "input_value": input_value if action_type == "type" else None,
                "accessibility_id": desc,
                "bounds": bounds_str,
                "xpath": selector if selector.startswith("xpath=") else "",
                "resource_id": res_id,
                "fallback_coordinates": {"x": cx, "y": cy} if (cx is not None and cy is not None) else None
            }
            
            logger.info(f"Generated action: {action}")
            
            commands = action_to_script(action_type, next_element, input_value)
            logger.info(f"Converted action to commands: {commands}")
            
            history_log.append({
                "step": steps_taken,
                "screen_hash": current_hash,
                "screen_type": current_state["screen_type"],
                "element": {
                    "som_index": som_idx,
                    "class": next_element.get("class"),
                    "resource_id": next_element.get("resource_id"),
                    "text": next_element.get("text"),
                    "semantics": next_element.get("semantics")
                },
                "action": action,
                "status": "executed"
            })
            
            # Save action parameters for edge building on next loop step
            last_screen_hash = current_hash
            last_action_type = action_type
            last_som_idx = som_idx
            last_input_value = input_value
            
            last_action_commands = commands
            success = await run_script(client, commands)
            if not success:
                logger.warning("Failed to execute action script successfully.")
            else:
                # Mark this element as explored so it is never re-clicked on return visits
                next_element["explored"] = True
                save_current_map()
                
        else:
            logger.info(f"All elements explored on screen: {current_hash}. Backtracking...")
            if len(stack) <= 1:
                logger.info("Stack has only 1 screen (Home) and it is fully explored. Crawling finished!")
                break
                
            stack.pop()
            parent_entry = stack[-1]
            parent_hash = parent_entry["skeletal_hash"]
            
            logger.info(f"Target backtrack screen: {parent_hash}")
            
            # Try to locate a visible back arrow/navigation button on screen first
            back_element = None
            for el in current_state.get("elements", []):
                if el.get("clickable"):
                    res_id = (el.get("resource_id") or "").lower()
                    text = (el.get("text") or "").lower()
                    desc = (el.get("content_desc") or "").lower()
                    cls = (el.get("class") or "").lower()
                    
                    if any(kw in res_id or kw in text or kw in desc for kw in [
                        "back", "prev", "return", "navigate_up", "close"
                    ]):
                        back_element = el
                        break
            
            back_clicked = False
            if back_element:
                logger.info(f"Found on-screen back button: {back_element.get('semantics') or back_element.get('text') or back_element.get('content_desc')}. Tapping it...")
                back_commands = action_to_script("tap", back_element)
                back_clicked = await run_script(client, back_commands)
                
            if not back_clicked:
                logger.info("No on-screen back button found or click failed. Attempting system back button...")
                try:
                    await client.call_tool("back", {})
                except Exception as be:
                    logger.warning(f"Failed to press system back: {be}")
                    
            # Wait for layout transition and keyboard/loading screens to settle
            await wait_for_loading_indicators(client, max_wait_seconds=15.0)
            
            post_back_xml = await get_xml()
            post_back_hash = get_skeletal_hash(xml_string=post_back_xml).get("hash")
            
            # Build backtrack edge
            if post_back_hash:
                back_edge = build_edge(
                    from_hash=current_hash,
                    to_hash=post_back_hash,
                    actions_in_from=[{"action_type": "back", "on_element": -1, "input_value": None}]
                )
                edge_exists = False
                for existing_edge in app_map["edges"]:
                    if (existing_edge["from"] == back_edge["from"] and 
                        existing_edge["to"] == back_edge["to"] and 
                        existing_edge["actions_in_from"] == back_edge["actions_in_from"]):
                        edge_exists = True
                        break
                if not edge_exists:
                    app_map["edges"].append(back_edge)
            
            if post_back_hash == parent_hash:
                logger.info("Backtrack succeeded! Reached parent screen.")
            else:
                logger.warning(f"Backtrack went to '{post_back_hash}' instead of target parent '{parent_hash}'. Attempting graph-based pathfinding navigation...")
                path_success = await navigate_graph(client, app_map, post_back_hash, parent_hash)
                if not path_success:
                    logger.warning("Graph-based navigation failed. Falling back to app restart and path replay...")
                    await replay_path(parent_entry["path_to_reach"])
                
            last_action_commands = []
            save_current_map()

    # 7. Save AppMap JSON strictly locally in documents artifacts folder
    map_path = "/Users/preethichitte/Documents/artifacts/curtain_tracker_app_map.json"
    try:
        if current_hash:
            app_map["last_stopped_screen_hash"] = current_hash
        os.makedirs(os.path.dirname(map_path), exist_ok=True)
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump(app_map, f, indent=2)
        logger.info(f"Successfully saved app map to {map_path}")
    except Exception as e:
        logger.error(f"Failed to save app map to disk: {e}")

    return {
        "status": "success",
        "steps_taken": steps_taken,
        "total_visited_screens": len(visited_screens),
        "history": history_log,
        "app_map": app_map,
        "visited_screens": [
            {
                "screen_hash": h,
                "screen_type": s["screen_type"],
                "total_elements": len(s["elements"]),
                "explored_indices": list(s["visited_indices"])
            }
            for h, s in visited_screens.items()
        ]
    }

@router.post("/explore_pipeline")
async def run_explore_pipeline(request: ExplorePipelineRequest):
    """
    Executes the autonomous exploration pipeline on the target device.
    """
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
            "deviceType": request.deviceType,
            # Attach to the currently running app without killing or resetting it
            "extraCapabilities": {
                "appium:noReset": True,
                "appium:dontStopAppOnReset": True,
                "appium:autoLaunch": False,
                "appium:ignoreHiddenApiPolicyError": True
            }
        }
        await client.call_tool("start_session", session_args)
        await asyncio.sleep(2.0)
        
        result = await explore_pipeline(
            client=client,
            app_package=request.appPackage,
            app_activity=request.appActivity,
            max_steps=request.max_steps,
            prefill_data=request.prefill_data
        )
        return result
        
    except Exception as e:
        logger.error(f"Error during explore_pipeline endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if client:
            try:
                await client.stop()
            except Exception:
                pass


class NavigateToLastStoppedRequest(BaseModel):
    deviceName: str = "Android Emulator"
    udid: str = "emulator-5554"
    appPackage: str
    appActivity: str
    deviceType: str = "local emulator"
    app_map_path: str = "/Users/preethichitte/Documents/artifacts/curtain_tracker_app_map.json"


@router.post("/navigate_to_last_stopped")
async def run_navigate_to_last_stopped(request: NavigateToLastStoppedRequest):
    """
    Identifies the current screen on the active device, reads the stored app map,
    and navigates to the last stopped crawl screen hash using BFS pathfinding.
    """
    client = None
    try:
        # Load the app map
        if not os.path.exists(request.app_map_path):
            raise HTTPException(
                status_code=404,
                detail=f"App map file not found at: {request.app_map_path}"
            )
        with open(request.app_map_path, "r", encoding="utf-8") as f:
            app_map = json.load(f)

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
        
        result = await navigate_to_last_stopped_screen(client=client, app_map=app_map)
        return result
        
    except Exception as e:
        logger.error(f"Error during navigate_to_last_stopped endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if client:
            try:
                await client.stop()
            except Exception:
                pass


# ==========================================
# REGISTRY OF HELPER FUNCTIONS
# ==========================================

# Map strings to the actual python functions
FUNCTION_REGISTRY = {
    "extract_element_signatures": extract_element_signatures,
    "is_same_screen": is_same_screen,
    "get_skeletal_hash": get_skeletal_hash,
    "is_same_screen_cv": is_same_screen_cv,
    "parse_appium_xml": parse_appium_xml,
    "extract_text_ocr": extract_text_ocr,
    "draw_set_of_marks": draw_set_of_marks,
    "synthesize_screen_node": synthesize_screen_node,
    "process_screen_to_semantic_node": process_screen_to_semantic_node,
    "process_screen_without_llm": process_screen_without_llm,
    "build_screen_node": build_screen_node,
    "build_edge": build_edge,
    "init_app_map": init_app_map,
    "fast_extract_interactive_nodes": fast_extract_interactive_nodes,
    "explore_pipeline": explore_pipeline,
    "find_shortest_path": find_shortest_path,
    "navigate_graph": navigate_graph,
    "is_same_screen_node": is_same_screen_node,
    "identify_current_screen": identify_current_screen,
    "navigate_to_last_stopped_screen": navigate_to_last_stopped_screen
}

# ==========================================
# ROUTER EXECUTE ENDPOINT
# ==========================================

@router.post("/execute")
async def execute_function(request: ExecuteFunctionRequest):
    """
    Dispatcher endpoint to execute registered functions dynamically for Screen State Comparison.
    
    ### Available Functions & Payloads:

    **1. Jaccard Similarity on XML (is_same_screen)**
    Counts UI elements and compares sets. Best for Appium DOMs.
    ```json
    {
      "function_name": "is_same_screen",
      "params": {
        "xml_a": "<hierarchy>...</hierarchy>",
        "xml_b": "<hierarchy>...</hierarchy>",
        "threshold": 0.90
      }
    }
    ```

    **2. Skeletal Hash (get_skeletal_hash)**
    Extracts the strict Parent->Child tree of layout classes ignoring all text and IDs.
    Returns an MD5 hash.
    ```json
    {
      "function_name": "get_skeletal_hash",
      "params": {
        "xml_string": "<hierarchy>...</hierarchy>"
      }
    }
    ```

    **3. Computer Vision SSIM (is_same_screen_cv)**
    Compares two screenshots pixel-by-pixel. (Requires OpenCV and scikit-image installed).
    ```json
    {
      "function_name": "is_same_screen_cv",
      "params": {
        "image_path_a": "/absolute/path/to/screenshot1.png",
        "image_path_b": "/absolute/path/to/screenshot2.png",
        "threshold": 0.95
      }
    }
    ```

    **4. Clean Appium XML to JSON (parse_appium_xml)**
    Parses raw Appium XML into a clean, optimized JSON structure. Throws away non-interactable layout wrappers and empty nodes.
    ```json
    {
      "function_name": "parse_appium_xml",
      "params": {
        "xml_string": "<hierarchy>...</hierarchy>"
      }
    }
    ```

    **5. Extract Text via OCR (extract_text_ocr)**
    Uses EasyOCR to read text directly from a screenshot.
    ```json
    {
      "function_name": "extract_text_ocr",
      "params": {
        "image_path": "/path/to/screenshot.png"
      }
    }
    ```

    **6. Draw Set-of-Marks (draw_set_of_marks)**
    Uses OpenCV to draw red numbered bounding boxes over the image.
    ```json
    {
      "function_name": "draw_set_of_marks",
      "params": {
        "image_path": "/path/to/screenshot.png",
        "nodes": [{"bounds": "[0,0][100,100]"}]
      }
    }
    ```

    **7. LLM Synthesize Screen Node (synthesize_screen_node)**
    Sends annotated screenshot and JSON to Ollama to generate the final semantic UI map.
    ```json
    {
      "function_name": "synthesize_screen_node",
      "params": {
        "annotated_image_path": "/path/to/annotated_screen.png",
        "nodes": [{"bounds": "[0,0][100,100]", "som_index": 0}],
        "ollama_model": "llava"
      }
    }
    ```

    **8. Ultimate Orchestrator (process_screen_to_semantic_node)**
    Runs the entire pipeline in one shot: XML parsing -> CV Set-of-Marks -> LLM Synthesis.
    ```json
    {
      "function_name": "process_screen_to_semantic_node",
      "params": {
        "xml_string": "<hierarchy>...</hierarchy>",
        "image_path": "/path/to/screenshot.png",
        "ollama_model": "llava"
      }
    }
    ```

    **8b. No-LLM Orchestrator (process_screen_without_llm)**
    Same pipeline as above but STOPS after Set-of-Marks — no LLM call.
    Returns the annotated image path, all indexed elements, and a skeletal hash.
    ```json
    {
      "function_name": "process_screen_without_llm",
      "params": {
        "xml_string": "<hierarchy>...</hierarchy>",
        "image_path": "/path/to/screenshot.png"
      }
    }
    ```

    **9. Fast Interactive Extractor (fast_extract_interactive_nodes)**
    Bypasses the LLM. Parses XML, filters ONLY for clickable/focusable (editable) nodes, and draws Set-of-Marks. Lightning fast.
    ```json
    {
      "function_name": "fast_extract_interactive_nodes",
      "params": {
        "xml_string": "<hierarchy>...</hierarchy>",
        "image_path": "/path/to/screenshot.png"
      }
    }
    ```
    """
    func = FUNCTION_REGISTRY.get(request.function_name)
    if not func:
        raise HTTPException(
            status_code=404, 
            detail=f"Function '{request.function_name}' not found. Available functions: {list(FUNCTION_REGISTRY.keys())}"
        )
    
    try:
        # Call the mapped function, unpacking the params dictionary into keyword arguments
        result = func(**request.params)
        return {
            "status": "success",
            "function": request.function_name,
            "result": result
        }
    except TypeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid parameters for function '{request.function_name}': {str(e)}")
    except Exception as e:
        logger.error(f"Error executing function '{request.function_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Execution failed: {str(e)}")


# ============================================================
# VISUAL EXPLORATION PIPELINE
# Screenshot → pHash dedup → OpenCV elements → EasyOCR text → tap
# ============================================================

# Module-level EasyOCR reader (loaded once, reused across calls)
_easyocr_reader = None

def _get_ocr_reader():
    """Lazily initialize EasyOCR reader (first call is slow ~5s, subsequent calls instant)."""
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr
        _easyocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _easyocr_reader


def extract_elements_visual(xml_string: str, screenshot_b64: str = "") -> list:
    """
    Extracts interactive elements from the XML accessibility tree using bounds coordinates.
    EasyOCR is used only as a fallback for elements with no text or content_desc in XML.

    Primary source: XML bounds="[x1,y1][x2,y2]" → exact pixel coordinates, 100% reliable.
    Fallback:       EasyOCR on the screenshot region → for icon-only elements with no label.

    Returns a list of element dicts:
      {
        "index": int,
        "cx": int, "cy": int,           # center tap coordinates
        "x": int, "y": int,             # top-left
        "w": int, "h": int,             # width / height
        "text": str,                    # from XML text / content_desc / OCR
        "class": str,
        "resource_id": str,
        "clickable": bool,
        "explored": False
      }
    """
    import re
    import xml.etree.ElementTree as ET

    SKIP_PACKAGES = {
        "com.google.android.inputmethod.latin",
        "com.android.inputmethod.latin",
        "com.android.systemui",
        "com.samsung.android.honeyboard",
        "com.sec.android.inputmethod",
    }
    SKIP_CLASSES = {
        "android.widget.FrameLayout",  # generic wrapper — skip unless clickable
    }
    BACK_HINTS = {"back", "navigate up", "go back", "arrow_back"}

    def parse_bounds(bounds_str: str):
        """'[x1,y1][x2,y2]' → (x1, y1, x2, y2) or None."""
        m = re.findall(r"\[(\d+),(\d+)\]", bounds_str)
        if len(m) == 2:
            x1, y1 = int(m[0][0]), int(m[0][1])
            x2, y2 = int(m[1][0]), int(m[1][1])
            return x1, y1, x2, y2
        return None

    elements = []
    seen_centers = []

    try:
        if not xml_string:
            return []
        if not xml_string.strip().startswith("<?xml") and not xml_string.strip().startswith("<hierarchy"):
            xml_string = f"<hierarchy>{xml_string}</hierarchy>"
        root = ET.fromstring(xml_string)
    except Exception as e:
        logger.warning(f"extract_elements_visual: XML parse failed: {e}")
        return []

    for node in root.iter():
        pkg   = node.attrib.get("package", "")
        cls   = node.attrib.get("class", "")
        res_id = node.attrib.get("resource-id", "")
        text  = (node.attrib.get("text") or "").strip()
        desc  = (node.attrib.get("content-desc") or "").strip()
        bounds_str = node.attrib.get("bounds", "")
        clickable    = node.attrib.get("clickable", "false") == "true"
        long_click   = node.attrib.get("long-clickable", "false") == "true"
        focusable    = node.attrib.get("focusable", "false") == "true"
        enabled      = node.attrib.get("enabled", "true") == "true"

        # --- Filters ---
        if pkg in SKIP_PACKAGES:
            continue
        if not enabled:
            continue
        if not (clickable or long_click or "EditText" in cls):
            continue
        if not bounds_str:
            continue

        parsed = parse_bounds(bounds_str)
        if not parsed:
            continue
        x1, y1, x2, y2 = parsed
        w, h = x2 - x1, y2 - y1

        # Skip elements too small to be meaningful
        if w < 10 or h < 10:
            continue

        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

        # Skip back-navigation elements
        label_lower = (text + " " + desc).lower()
        is_back_label = any(hint in label_lower for hint in BACK_HINTS)
        is_top_left_icon = (
            cx < 180 and cy < 300 and 
            (not label_lower or len(label_lower) <= 3 or any(c in label_lower for c in ["\ue5c4", "\uf1f8", "\ue314", "\ue5c8", "\ue112"]))
        )
        if is_back_label or is_top_left_icon:
            continue

        # Dedup: skip if center within 15px of existing element.
        # If the new element has text, but the existing one is empty, enrich it with the text/metadata.
        dup_found = False
        label = text or desc
        for existing in elements:
            if abs(cx - existing["cx"]) < 15 and abs(cy - existing["cy"]) < 15:
                dup_found = True
                if label and not existing["text"]:
                    existing["text"] = label
                    existing["class"] = cls
                    existing["resource_id"] = res_id
                    existing["bounds"] = bounds_str
                break
        if dup_found:
            continue
        seen_centers.append((cx, cy))

        label = text or desc  # prefer XML text, then content_desc

        elements.append({
            "index":       len(elements),
            "cx":          cx,
            "cy":          cy,
            "x":           x1,
            "y":           y1,
            "w":           w,
            "h":           h,
            "bounds":      bounds_str,
            "text":        label,
            "class":       cls,
            "resource_id": res_id,
            "clickable":   clickable,
            "explored":    False,
        })

    # --- EasyOCR fallback: enrich elements with no XML label ---
    unlabeled = [el for el in elements if not el["text"] and screenshot_b64]
    if unlabeled and screenshot_b64:
        try:
            import cv2, numpy as np, base64, io
            from PIL import Image

            img_bytes = base64.b64decode(screenshot_b64)
            img_np = np.array(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
            reader = _get_ocr_reader()

            for el in unlabeled:
                # Crop just this element's region and run OCR on it
                crop = img_np[el["y"]:el["y"]+el["h"], el["x"]:el["x"]+el["w"]]
                if crop.size == 0:
                    continue
                ocr_results = reader.readtext(crop, detail=1)
                parts = [
                    txt for (_, txt, conf) in ocr_results if conf >= 0.3
                ]
                if parts:
                    el["text"] = " ".join(parts).strip()
        except Exception as ocr_err:
            logger.warning(f"EasyOCR fallback failed: {ocr_err}")

    # Sort top-to-bottom, left-to-right (natural reading order)
    elements.sort(key=lambda e: (e["cy"], e["cx"]))
    for i, el in enumerate(elements):
        el["index"] = i

    logger.info(f"extract_elements_visual: {len(elements)} elements from XML bounds "
                f"({len(unlabeled)} used OCR fallback)")
    return elements


def save_annotated_screenshot(
    screenshot_b64: str,
    elements: list,
    save_path: str,
    screen_index: int,
) -> str:
    """
    Draws OpenCV bounding boxes + index labels + OCR text on the screenshot
    and saves it as a PNG file.

    Args:
        screenshot_b64: Base64-encoded screenshot
        elements:       List of element dicts from extract_elements_visual
        save_path:      Directory to save the annotated image in
        screen_index:   Screen number (used in the filename)

    Returns:
        Absolute path of the saved annotated image, or empty string on failure.
    """
    import cv2
    import numpy as np
    import base64, io
    from PIL import Image

    try:
        # Decode
        img_bytes = base64.b64decode(screenshot_b64)
        pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_np = np.array(pil_img)
        img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

        # Colour palette — cycles through for visual distinction
        palette = [
            (52, 152, 219),   # blue
            (46, 204, 113),   # green
            (231, 76,  60),   # red
            (155, 89, 182),   # purple
            (241, 196, 15),   # yellow
            (230, 126, 34),   # orange
            (26, 188, 156),   # teal
        ]

        for el in elements:
            idx   = el.get("index", 0)
            x, y  = el["x"], el["y"]
            w, h  = el["w"], el["h"]
            cx, cy = el["cx"], el["cy"]
            text  = el.get("text", "") or ""
            color = palette[idx % len(palette)]

            # Bounding box
            cv2.rectangle(img_bgr, (x, y), (x + w, y + h), color, 2)

            # Index badge (filled circle with number)
            badge_r = 14
            cv2.circle(img_bgr, (x + badge_r, y + badge_r), badge_r, color, -1)
            cv2.putText(
                img_bgr, str(idx),
                (x + badge_r - (6 if idx >= 10 else 4), y + badge_r + 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA
            )

            # OCR text label (truncated to 30 chars)
            if text:
                label = text[:30] + ("..." if len(text) > 30 else "")
                label_y = max(y - 6, 12)
                # Dark background for readability
                (lw, lh), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.4, 1)
                cv2.rectangle(img_bgr, (x, label_y - lh - 2), (x + lw + 4, label_y + 2), (30, 30, 30), -1)
                cv2.putText(
                    img_bgr, label, (x + 2, label_y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1, cv2.LINE_AA
                )

        # Save
        os.makedirs(save_path, exist_ok=True)
        filename = os.path.join(save_path, f"screen_{screen_index:03d}.png")
        cv2.imwrite(filename, img_bgr)
        logger.info(f"Annotated screenshot saved → {filename}")
        return os.path.abspath(filename)

    except Exception as e:
        logger.warning(f"save_annotated_screenshot failed: {e}")
        return ""


def _is_keyboard_open(xml_string: str) -> bool:
    """Returns True if the Android soft keyboard is currently visible in the XML hierarchy."""
    keyboard_packages = {
        "com.google.android.inputmethod.latin",
        "com.android.inputmethod.latin",
        "com.samsung.android.honeyboard",
        "com.sec.android.inputmethod",
        "com.swiftkey.swiftkeyproject",
        "com.touchtype.swiftkey",
    }
    return any(pkg in xml_string for pkg in keyboard_packages)


async def visual_explore_pipeline(
    client: "AppiumMcpClient",
    app_package: str,
    app_activity: str,
    max_steps: int,
    phash_threshold: int,
    output_path: str,
    screenshots_dir: str = "",
) -> dict:
    """
    DFS visual exploration loop. Saves standard app_map JSON structure with nodes and edges.
    """
    import re
    import datetime

    def _parse_bounds_str(b_str):
        m = re.findall(r"\[(\d+),(\d+)\]", b_str)
        if len(m) == 2:
            return int(m[0][0]), int(m[0][1]), int(m[1][0]), int(m[1][1])
        return 0, 0, 0, 0

    visual_map = {}     # { phash_str: { elements, xml_skeleton, raw_screenshot_path } }
    stack = []          # DFS stack of phash strings
    steps_taken = 0
    screens_found = 0

    # Create per-run annotated screenshots folder: <screenshots_dir>/<app_package>/run_<timestamp>/
    if screenshots_dir:
        run_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_pkg = app_package.replace(".", "_")
        run_folder = os.path.join(screenshots_dir, safe_pkg, f"run_{run_ts}")
        os.makedirs(run_folder, exist_ok=True)
        logger.info(f"Annotated screenshots will be saved to: {run_folder}")
    else:
        run_folder = ""

    # Load existing map if it exists (resume support)
    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                app_map = json.load(f)
            if not isinstance(app_map, dict) or "nodes" not in app_map or "edges" not in app_map:
                raise ValueError("JSON file does not contain nodes/edges structure")
            logger.info(f"Resumed visual app map with {len(app_map.get('nodes', []))} nodes from {output_path}")
            
            screens_found = len(app_map.get("nodes", []))
            for node in app_map.get("nodes", []):
                phash = node.get("screen_hash")
                if phash:
                    explore_elements = []
                    for idx, el in enumerate(node.get("elements", [])):
                        bounds_str = el.get("bounds", "")
                        x1, y1, x2, y2 = _parse_bounds_str(bounds_str)
                        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                        explore_elements.append({
                            "index": idx,
                            "cx": cx,
                            "cy": cy,
                            "x": x1,
                            "y": y1,
                            "w": x2 - x1,
                            "h": y2 - y1,
                            "text": el.get("text") or el.get("content_desc", ""),
                            "class": el.get("class", ""),
                            "resource_id": el.get("resource_id", ""),
                            "clickable": el.get("clickable", False),
                            "explored": el.get("explored", False)
                        })
                    
                    xml_string = node.get("xml", "")

                    visual_map[phash] = {
                        "phash": phash,
                        "xml": xml_string,
                        "raw_screenshot_path": node.get("raw_screenshot_path", ""),
                        "annotated_screenshot": node.get("annotated_image_path", ""),
                        "xml_skeleton": node.get("xml_skeleton", ""),
                        "elements": explore_elements
                    }
            
            # Load active_dfs_stack from JSON if present
            stack = app_map.get("active_dfs_stack", [])
            # Validate all elements in loaded stack exist in visual_map
            stack = [s for s in stack if s in visual_map]
            
            if not stack:
                last_hash = app_map.get("last_stopped_screen_hash")
                if last_hash and last_hash in visual_map:
                    stack = [last_hash]
                else:
                    stack = []
                    if app_map.get("nodes"):
                        stack = [app_map["nodes"][0]["screen_hash"]]
        except Exception as resume_err:
            logger.warning(f"Failed to resume app map: {resume_err}. Starting fresh.")
            app_map = init_app_map(app_name="Curtain Tracker", app_package=app_package, app_main_activity=app_activity)
            visual_map = {}
            stack = []
            screens_found = 0
    else:
        app_map = init_app_map(app_name="Curtain Tracker", app_package=app_package, app_main_activity=app_activity)
        visual_map = {}
        stack = []
        screens_found = 0

    def save_visual_app_map():
        try:
            if stack:
                app_map["last_stopped_screen_hash"] = stack[-1]
                app_map["active_dfs_stack"] = list(stack)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(app_map, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save visual app map: {e}")

    last_screen_phash = None
    last_element_idx = None
    last_action_type = None
    did_backtrack_last_step = False
    backtrack_start_phash = None

    for step in range(max_steps):
        steps_taken += 1
        logger.info(f"--- Visual Crawl Step {steps_taken}/{max_steps} ---")

        await asyncio.sleep(1.5)   # brief stabilisation wait

        # --- 0. Keyboard check & dismissal first ---
        xml_string = ""
        try:
            xml_resp = await client.call_tool("get_page_source", {})
            if xml_resp and not xml_resp.get("isError"):
                for item in xml_resp.get("content", []):
                    if item.get("type") == "text":
                        xml_string += item.get("text", "")
            
            if _is_keyboard_open(xml_string):
                logger.info("Keyboard detected — hiding keyboard before exploring")
                try:
                    await client.call_tool("hide_keyboard", {})
                except Exception:
                    pass
                await asyncio.sleep(1.5)
                # Refresh XML after closing keyboard
                xml_resp = await client.call_tool("get_page_source", {})
                xml_string = ""
                if xml_resp and not xml_resp.get("isError"):
                    for item in xml_resp.get("content", []):
                        if item.get("type") == "text":
                            xml_string += item.get("text", "")
        except Exception as e:
            logger.warning(f"Keyboard check/close failed: {e}")

        # --- 1. Package Escape Check & Recovery ---
        try:
            pkg_resp = await client.call_tool("get_current_package", {})
            pkg_name = ""
            if pkg_resp and not pkg_resp.get("isError"):
                for item in pkg_resp.get("content", []):
                    if item.get("type") == "text":
                        pkg_name = item.get("text", "").strip()
            
            IGNORE_REOPEN_PACKAGES = {
                "com.google.android.inputmethod.latin",
                "com.android.inputmethod.latin",
                "com.samsung.android.honeyboard",
                "com.sec.android.inputmethod",
                "com.swiftkey.swiftkeyproject",
                "com.touchtype.swiftkey",
                "com.android.permissioncontroller",
                "com.google.android.permissioncontroller",
                "android",
            }

            if pkg_name and pkg_name != app_package and pkg_name not in IGNORE_REOPEN_PACKAGES:
                logger.warning(f"App escaped to package '{pkg_name}'. Relaunching/Reactivating target app '{app_package}'...")
                
                try:
                    await client.call_tool("back", {})
                except Exception:
                    pass
                await asyncio.sleep(2.0)
                
                pkg_resp2 = await client.call_tool("get_current_package", {})
                pkg_name2 = ""
                if pkg_resp2 and not pkg_resp2.get("isError"):
                    for item in pkg_resp2.get("content", []):
                        if item.get("type") == "text":
                            pkg_name2 = item.get("text", "").strip()
                
                if pkg_name2 and pkg_name2 != app_package and pkg_name2 not in IGNORE_REOPEN_PACKAGES:
                    logger.warning(f"Still in package '{pkg_name2}'. Reactivating app '{app_package}'...")
                    await client.call_tool("activate_app", {"appPackage": app_package})
                    await asyncio.sleep(4.0)
                    
                    # Refresh XML after reactivation
                    xml_resp = await client.call_tool("get_page_source", {})
                    xml_string = ""
                    if xml_resp and not xml_resp.get("isError"):
                        for item in xml_resp.get("content", []):
                            if item.get("type") == "text":
                                xml_string += item.get("text", "")
        except Exception as e:
            logger.warning(f"Failed to check/recovery escape package: {e}")

        # --- 2. Take Screenshot ---
        try:
            ss_resp = await client.call_tool("take_screenshot", {})
            screenshot_b64 = ""
            if not ss_resp.get("isError"):
                content = ss_resp.get("content", [])
                for item in content:
                    if item.get("type") == "image":
                        screenshot_b64 = item.get("data", "")
                        break
                if not screenshot_b64:
                    for item in content:
                        if item.get("type") == "text":
                            screenshot_b64 = item.get("text", "")
                            break
                # Strip data-URI prefix if present
                if "," in screenshot_b64:
                    screenshot_b64 = screenshot_b64.split(",", 1)[1]
            if not screenshot_b64:
                logger.warning("Empty screenshot — skipping step")
                continue
        except Exception as e:
            logger.warning(f"take_screenshot failed: {e}")
            continue

        # Save present screenshot to a temp file for CV comparison
        curr_run_folder = run_folder or "./temp_screenshots"
        os.makedirs(curr_run_folder, exist_ok=True)
        temp_curr_path = os.path.join(curr_run_folder, "temp_present.png")
        try:
            import base64
            with open(temp_curr_path, "wb") as f:
                f.write(base64.b64decode(screenshot_b64))
        except Exception as e:
            logger.warning(f"Failed to save temp_present.png: {e}")
            temp_curr_path = ""

        # Calculate current pHash (still useful for dict keying)
        current_phash = compute_phash(screenshot_b64)
        if current_phash.startswith("error:"):
            logger.warning(f"pHash failed: {current_phash}")
            continue

        # --- 3. Compare with Visited Screens ---
        matched_phash = None

        # A. Check structure using Jaccard similarity of element signatures (if XML available)
        if xml_string:
            for known_phash in visual_map:
                known_xml = visual_map[known_phash].get("xml", "")
                if known_xml:
                    jaccard_res = is_same_screen(xml_string, known_xml, threshold=0.88)
                    if jaccard_res.get("is_same"):
                        logger.info(f"Screen structurally matches visited screen '{known_phash}' via Jaccard similarity ({jaccard_res.get('similarity', 0.0):.2f})")
                        matched_phash = known_phash
                        break

        if matched_phash is None:
            for known_phash in visual_map:
                known_raw_path = visual_map[known_phash].get("raw_screenshot_path", "")
                if temp_curr_path and known_raw_path and os.path.exists(known_raw_path):
                    cv_res = is_same_screen_cv(temp_curr_path, known_raw_path, threshold=0.90)
                    if cv_res.get("is_same"):
                        logger.info(f"Screen visually matches visited screen '{known_phash}' via CV similarity ({cv_res.get('similarity', 0.0):.2f})")
                        matched_phash = known_phash
                        break
                else:
                    # Fallback to pHash comparison if images are not on disk
                    result = is_same_screen_phash(screenshot_b64, visual_map[known_phash].get("screenshot_b64", ""), threshold=phash_threshold)
                    if result.get("is_same"):
                        logger.info(f"Screen matches visited screen '{known_phash}' via pHash fallback")
                        matched_phash = known_phash
                        break

        if matched_phash is None:
            # Genuinely new screen
            screens_found += 1
            logger.info(f"NEW SCREEN #{screens_found} detected — pHash: {current_phash}")

            # Save the raw screenshot to disk permanently
            raw_path = os.path.join(curr_run_folder, f"screen_{screens_found:03d}_raw.png")
            try:
                with open(raw_path, "wb") as f:
                    f.write(base64.b64decode(screenshot_b64))
            except Exception as e:
                logger.warning(f"Failed to save raw screenshot: {e}")
                raw_path = ""

            # Extract elements from XML bounds (+ EasyOCR for unlabeled ones)
            elements = extract_elements_visual(
                xml_string=xml_string,
                screenshot_b64=screenshot_b64,
            )
            logger.info(f"  Extracted {len(elements)} elements from XML bounds")

            # Save annotated screenshot for this new screen
            annotated_path = ""
            if run_folder:
                annotated_path = save_annotated_screenshot(
                    screenshot_b64=screenshot_b64,
                    elements=elements,
                    save_path=run_folder,
                    screen_index=screens_found,
                )

            # Get skeletal hash from XML for the stored JSON
            skel = get_skeletal_hash(xml_string) if xml_string else {}

            visual_map[current_phash] = {
                "phash": current_phash,
                "raw_screenshot_path": raw_path,
                "annotated_screenshot": annotated_path,
                "xml_skeleton": skel.get("skeleton_preview", ""),
                "elements": elements
            }

            # Build map_node
            map_node_elements = []
            for el in elements:
                map_node_elements.append({
                    "class": el["class"],
                    "resource_id": el["resource_id"],
                    "text": el["text"],
                    "content_desc": "",
                    "bounds": f"[{el['x']},{el['y']}][{el['x']+el['w']},{el['y']+el['h']}]",
                    "clickable": el["clickable"],
                    "scrollable": False,
                    "focusable": False,
                    "checkable": False,
                    "checked": False,
                    "selected": False,
                    "long_clickable": False,
                    "enabled": True,
                    "password": False,
                    "semantics": el["text"] or f"Button {el['index']}",
                    "explored": el.get("explored", False)
                })

            map_node = {
                "screen_hash": current_phash,
                "screen_type": f"Visual Screen {screens_found}",
                "scroll": False,
                "annotated_image_path": annotated_path,
                "raw_screenshot_path": raw_path,
                "xml": xml_string,
                "xml_skeleton": skel.get("skeleton_preview", ""),
                "elements": map_node_elements
            }
            app_map["nodes"].append(map_node)
            save_visual_app_map()
        else:
            current_phash = matched_phash

        # --- 3a. Stack management ---
        stack_idx = -1
        for idx, entry in enumerate(stack):
            if entry == current_phash:
                stack_idx = idx
                break
                
        if stack_idx != -1:
            logger.info(f"Aligned stack: returned to screen in stack at index {stack_idx}")
            stack = stack[:stack_idx + 1]
        else:
            stack.append(current_phash)
            logger.info(f"Pushed screen to stack. New stack size: {len(stack)}")

        # --- 3b. Backtrack loop break detection ---
        if did_backtrack_last_step:
            if current_phash == backtrack_start_phash:
                logger.info("Backtrack did not change the screen. Reached root and cannot backtrack further. Crawl complete.")
                break
            else:
                logger.info(f"Backtrack changed screen from {backtrack_start_phash} to {current_phash}. Continuing exploration.")
                did_backtrack_last_step = False
                backtrack_start_phash = None

        # --- 3c. Edge building for transition ---
        if last_screen_phash:
            edge = build_edge(
                from_hash=last_screen_phash,
                to_hash=current_phash,
                actions_in_from=[{
                    "action_type": last_action_type,
                    "on_element": last_element_idx,
                    "input_value": None
                }]
            )
            # Avoid duplicate edges
            edge_exists = False
            for existing_edge in app_map.get("edges", []):
                if (existing_edge["from"] == edge["from"] and 
                    existing_edge["to"] == edge["to"] and 
                    existing_edge["actions_in_from"] == edge["actions_in_from"]):
                    edge_exists = True
                    break
            if not edge_exists:
                app_map["edges"].append(edge)
                save_visual_app_map()
            last_screen_phash = None

        # --- 4. Pick next unexplored element ---
        screen_data = visual_map.get(current_phash, {})
        elements = screen_data.get("elements", [])
        next_el = None
        for el in elements:
            if not el.get("explored"):
                next_el = el
                break

        if next_el is None:
            # All elements on this screen explored → backtrack
            logger.info(f"All {len(elements)} elements explored. Backtracking...")
            
            # Save action parameters for backtrack edge building on next loop step
            last_screen_phash = current_phash
            last_element_idx = -1
            last_action_type = "back"
            
            # Press system back
            logger.info("Attempting system back button...")
            try:
                await client.call_tool(
                    "execute_script",
                    {"script": "mobile: pressKey", "args": [{"keycode": 4}]}
                )
            except Exception:
                try:
                    await client.call_tool("back", {})
                except Exception:
                    pass
            
            if len(stack) > 1:
                stack.pop()
            
            did_backtrack_last_step = True
            backtrack_start_phash = current_phash
            
            await asyncio.sleep(1.5)
            continue

        # --- 5. Tap the element ---
        cx, cy = next_el["cx"], next_el["cy"]
        el_text = next_el.get("text", "") or "(no text)"
        logger.info(f"Tapping element #{next_el['index']} '{el_text}' at ({cx}, {cy})")

        # Save action parameters for edge building on next loop step
        last_screen_phash = current_phash
        last_element_idx = next_el["index"]
        last_action_type = "tap"

        # Build selector for click_element
        res_id = next_el.get("resource_id")
        text = next_el.get("text")
        cls = next_el.get("class")
        bounds_str = next_el.get("bounds")
        
        selector = ""
        if res_id:
            selector = f"id={res_id}"
        elif text:
            tag = cls if cls else "*"
            if "'" in text:
                selector = f'xpath=//{tag}[@text="{text}"]'
            else:
                selector = f"xpath=//{tag}[@text='{text}']"
        elif bounds_str:
            tag = cls if cls else "*"
            selector = f"xpath=//{tag}[@bounds='{bounds_str}']"
        else:
            tag = cls if cls else "*"
            selector = f"xpath=//{tag}"

        clicked_successfully = False
        try:
            logger.info(f"Attempting native selector tap on '{selector}'")
            resp = await client.call_tool("click_element", {"selector": selector})
            if resp and not resp.get("isError"):
                clicked_successfully = True
                logger.info("Successfully tapped via selector")
        except Exception as sel_err:
            logger.warning(f"Selector tap failed: {sel_err}. Falling back to coordinate tap...")

        if not clicked_successfully:
            try:
                logger.info(f"Attempting fallback coordinate tap at ({cx}, {cy})")
                await client.call_tool("tap_coordinate", {"x": cx, "y": cy})
                clicked_successfully = True
                logger.info("Successfully tapped via coordinates")
            except Exception as coord_err:
                logger.warning(f"Coordinate tap also failed: {coord_err}")

        # Mark explored ONLY after tap attempts
        next_el["explored"] = True

        # Mark element as explored in app_map["nodes"] as well
        for node in app_map.get("nodes", []):
            if node["screen_hash"] == current_phash:
                if 0 <= next_el["index"] < len(node["elements"]):
                    node["elements"][next_el["index"]]["explored"] = True
        
        save_visual_app_map()
        
        # Wait for 1 second after tap action to allow screen transition
        await asyncio.sleep(1.0)
            
    return {
        "status": "completed",
        "steps_taken": steps_taken,
        "screens_found": screens_found,
        "output_path": output_path,
        "summary": {
            k: {
                "elements_total": len(v.get("elements", [])),
                "elements_explored": sum(1 for e in v.get("elements", []) if e.get("explored")),
            }
            for k, v in visual_map.items()
        }
    }


class VisualExplorePipelineRequest(BaseModel):
    deviceName: str = "Android Emulator"
    udid: str = "emulator-5554"
    appPackage: str
    appActivity: str = ".MainActivity"
    deviceType: str = "local emulator"
    max_steps: int = 150
    phash_threshold: int = 10     # Hamming distance <= this → same screen
    output_path: str = "/Users/preethichitte/Documents/artifacts/visual_app_map.json"
    # Folder where annotated screenshots are saved.
    # A subfolder named after the app package + run timestamp is created automatically.
    screenshots_dir: str = "/Users/preethichitte/Documents/artifacts/visual_screenshots"


@router.post("/visual_explore_pipeline")
async def run_visual_explore_pipeline(request: VisualExplorePipelineRequest):
    """
    Visual exploration pipeline.

    Uses pHash (perceptual hash) for screen deduplication, OpenCV contour detection
    for element discovery, and EasyOCR for reading text — no fragile XPath selectors.

    Flow per step:
      1. Screenshot → pHash → is this a new screen?
      2. If new: OpenCV + EasyOCR extract elements → register screen in JSON map
      3. Tap next unexplored element by (cx, cy) coordinate
      4. Keyboard open? → hide it first
      5. All elements done? → system BACK → continue DFS
      6. Persist visual_app_map.json after every tap
    """
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
            "deviceType": request.deviceType,
            "extraCapabilities": {
                "appium:noReset": True,
                "appium:dontStopAppOnReset": True,
                "appium:autoLaunch": False,
                "appium:ignoreHiddenApiPolicyError": True
            }
        }
        await client.call_tool("start_session", session_args)
        await asyncio.sleep(2.0)

        result = await visual_explore_pipeline(
            client=client,
            app_package=request.appPackage,
            app_activity=request.appActivity,
            max_steps=request.max_steps,
            phash_threshold=request.phash_threshold,
            output_path=request.output_path,
            screenshots_dir=request.screenshots_dir,
        )
        return result

    except Exception as e:
        logger.error(f"visual_explore_pipeline endpoint error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if client:
            try:
                await client.stop()
            except Exception:
                pass
