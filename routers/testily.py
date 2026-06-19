import os
import re
import uuid
import logging
import hashlib
import json
from datetime import date
from typing import Optional
from fastapi import APIRouter, File, UploadFile, Form, HTTPException
from pydantic import BaseModel
from storage import get_storage_provider, SupabaseStorageProvider
from appium_helper import (
    start_appium_session,
    download_screenshot_by_appium,
    get_page_source_by_appium,
    get_clickable_elements,
    xml_to_json,
    crawl_app_map_appium,
    save_run_metadata
)

logger = logging.getLogger("testily_router")
router = APIRouter(prefix="/testily", tags=["Testily"])
storage_provider = get_storage_provider()

class GenerateMapRequest(BaseModel):
    project: str
    apk_filename: str
    deviceName: str = "Android Emulator"
    udid: str = "emulator-5554"
    appPackage: Optional[str] = None
    appActivity: Optional[str] = None
    deviceType: str = "local emulator"
    max_steps: int = 15
    prefill_data: Optional[dict] = None
    use_ollama: bool = False
    ollama_model: str = "llama3.2"
    ollama_url: str = "http://127.0.0.1:11434"
    user_prompt: Optional[str] = None
    max_scrolls: int = 3
    allow_destructive: bool = False

class GenerateMapRequestV2(BaseModel):
    project: str
    apk_filename: str
    deviceName: str = "Android Emulator"
    udid: str = "emulator-5554"
    appPackage: Optional[str] = None
    appActivity: Optional[str] = None
    deviceType: str = "local emulator"
    max_steps: int = 15
    prefill_data: Optional[dict] = None
    max_scrolls: int = 3
    allow_destructive: bool = False

class SaveMapSupabaseRequest(BaseModel):
    project: str
    app_package: str
    run_id: str
    app_map: dict

class SaveMapRequest(BaseModel):
    project: str
    app_package: str
    run_id: str
    app_map: dict


# 250MB limit
MAX_APK_SIZE = 250 * 1024 * 1024

@router.post("/app-upload")
async def upload_app(
    project: str = Form(...),
    file: UploadFile = File(...)
):
    """
    Accepts an APK file upload, validates it (extension, size, magic bytes),
    and uploads it to the configured storage provider under the specified project folder.
    """
    # 1. Sanitize project folder name to prevent traversal
    safe_project = os.path.basename(project).strip()
    if not safe_project or safe_project in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid project name")

    # 2. Validate file extension
    filename = file.filename or "unknown.apk"
    _, ext = os.path.splitext(filename.lower())
    if ext != ".apk":
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid file extension '{ext}'. Only '.apk' files are allowed."
        )

    # 3. Read content in chunks up to size limit to prevent memory exhaust
    contents = bytearray()
    total_read = 0
    
    while True:
        chunk = await file.read(8192)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > MAX_APK_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File size exceeds the maximum limit of {MAX_APK_SIZE // (1024 * 1024)}MB."
            )
        contents.extend(chunk)

    # 4. Verify magic bytes for zip/apk structure (PK\x03\x04)
    # APK files are fundamentally ZIP archives
    if len(contents) < 4 or contents[:4] != b"PK\x03\x04":
        raise HTTPException(
            status_code=400,
            detail="Invalid file content: The uploaded file is not a valid APK/ZIP archive."
        )

    # 5. Generate secure, unique filename to avoid Path Traversal and collisions
    secure_filename = f"{uuid.uuid4()}.apk"

    # Save a copy to the user's local Documents/apks folder first
    local_saved = False
    try:
        local_apks_dir = "/Users/preethichitte/Documents/apks"
        os.makedirs(local_apks_dir, exist_ok=True)
        local_apk_path = os.path.join(local_apks_dir, secure_filename)
        with open(local_apk_path, "wb") as f:
            f.write(contents)
        logger.info(f"Saved local copy of APK to {local_apk_path}")
        local_saved = True
    except Exception as le:
        logger.warning(f"Failed to save copy to Documents/apks: {le}")

    # Also save copy to local project uploads folder
    local_project_saved = False
    try:
        local_project_dir = os.path.join("uploads", safe_project)
        os.makedirs(local_project_dir, exist_ok=True)
        local_project_apk_path = os.path.join(local_project_dir, secure_filename)
        with open(local_project_apk_path, "wb") as f:
            f.write(contents)
        local_project_saved = True
    except Exception as le:
        logger.warning(f"Failed to save copy to project uploads: {le}")

    if not local_saved and not local_project_saved:
        raise HTTPException(
            status_code=500,
            detail="Failed to save APK locally on the server filesystem."
        )

    # Save mapping of original_filename -> secure_filename locally for easy resolution
    try:
        local_project_dir = os.path.join("uploads", safe_project)
        os.makedirs(local_project_dir, exist_ok=True)
        mappings_path = os.path.join(local_project_dir, "mappings.json")
        
        mappings = {}
        if os.path.exists(mappings_path):
            try:
                with open(mappings_path, "r", encoding="utf-8") as mf:
                    mappings = json.load(mf)
            except Exception:
                pass
                
        mappings[filename] = secure_filename
        with open(mappings_path, "w", encoding="utf-8") as mf:
            json.dump(mappings, mf, indent=2)
    except Exception as me:
        logger.warning(f"Failed to save filename mapping: {me}")

    # 6. Attempt upload to configured storage provider
    upload_url = ""
    try:
        logger.info(f"Uploading APK to storage provider: {secure_filename}...")
        upload_url = await storage_provider.upload_file(
            file_content=bytes(contents),
            folder=safe_project,
            filename=secure_filename,
            mime_type="application/vnd.android.package-archive"
        )
    except Exception as e:
        logger.warning(f"Storage provider upload failed: {e}. Falling back to local identifier.")
        # Default to local path if storage provider fails (e.g. Supabase 50MB file size limit)
        upload_url = f"/uploads/{safe_project}/{secure_filename}"

    return {
        "status": "success",
        "project": safe_project,
        "filename": secure_filename,
        "original_filename": filename,
        "url": upload_url
    }


@router.get("/apps")
async def list_apps(project: str):
    """
    Lists all uploaded apps (and their URLs/metadata) for a given project.
    """
    safe_project = os.path.basename(project).strip()
    if not safe_project or safe_project in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid project name")

    # 1. Try fetching from Supabase apps table if configured
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    
    if supabase_url and supabase_key:
        url = f"{supabase_url.rstrip('/')}/rest/v1/apps?project_id=eq.{safe_project}&select=*"
        headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}"
        }
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=headers, timeout=15.0)
                if resp.status_code == 200:
                    apps_data = resp.json()
                    apps_list = []
                    for item in apps_data:
                        path_str = item.get("path") or ""
                        filename = os.path.basename(path_str) if path_str else ""
                        apps_list.append({
                            "app_name": item.get("app_name") or filename,
                            "path": path_str,
                            "name": filename,
                            "original_filename": item.get("app_name") or filename,
                            "app_package": item.get("app_package") or "",
                            "main_activity": item.get("main_activity") or "",
                            "url": ""
                        })
                    if apps_list:
                        return {
                            "project": safe_project,
                            "apps": apps_list
                        }
                else:
                    logger.error(f"Supabase GET apps failed: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"Error querying apps from Supabase: {e}")

    # 2. Fallback to storage provider file listing
    try:
        files = await storage_provider.list_files(safe_project)
        
        # Try loading mappings
        mappings = {}
        mappings_path = os.path.join("uploads", safe_project, "mappings.json")
        if os.path.exists(mappings_path):
            try:
                with open(mappings_path, "r", encoding="utf-8") as mf:
                    mappings = json.load(mf)
            except Exception:
                pass
                
        # Inverse mapping: secure_filename -> original_filename
        inv_mappings = {v: k for k, v in mappings.items()}
        
        enriched_files = []
        for f in files:
            name = f.get("name", "")
            orig_name = inv_mappings.get(name, name)
            enriched_files.append({
                "app_name": orig_name,
                "path": os.path.join("uploads", safe_project, name),
                "name": name,
                "original_filename": orig_name,
                "app_package": "",
                "main_activity": "",
                "url": f.get("url", "")
            })
            
        return {
            "project": safe_project,
            "apps": enriched_files
        }
    except Exception as e:
        logger.error(f"Error listing apps for project '{safe_project}': {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list files: {str(e)}")


@router.post("/generate-map")
async def generate_map(request: GenerateMapRequest):
    """
    Starts an Appium session, installs the specified APK, runs the recursive DFS crawler,
    captures page screenshots and layouts, and compiles/uploads the final App Map JSON.
    Each run is isolated in its own folder grouped by package name and timestamp.
    """
    safe_project = os.path.basename(request.project).strip()
    if not safe_project or safe_project in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid project name")
        
    safe_apk_filename = os.path.basename(request.apk_filename).strip()
    if not safe_apk_filename or safe_apk_filename in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid APK filename")

    # Locate APK path
    apk_path = os.path.join("uploads", safe_project, safe_apk_filename)
    
    # If APK not found directly, try resolving via mappings.json or auto-detect if exactly one APK exists
    if not os.path.exists(apk_path):
        resolved_filename = None
        
        # 1. Try resolving via mappings.json
        mappings_path = os.path.join("uploads", safe_project, "mappings.json")
        if os.path.exists(mappings_path):
            try:
                with open(mappings_path, "r", encoding="utf-8") as mf:
                    mappings = json.load(mf)
                    if safe_apk_filename in mappings:
                        resolved_filename = mappings[safe_apk_filename]
                        logger.info(f"Resolved original filename '{safe_apk_filename}' to '{resolved_filename}' via mappings.json")
            except Exception as me:
                logger.warning(f"Error reading mappings.json: {me}")
                
        # 2. Try auto-detecting if there is exactly one APK in the directory
        if not resolved_filename:
            try:
                project_dir = os.path.join("uploads", safe_project)
                if os.path.exists(project_dir):
                    apk_files = [f for f in os.listdir(project_dir) if f.lower().endswith(".apk")]
                    if len(apk_files) == 1:
                        resolved_filename = apk_files[0]
                        logger.info(f"APK '{safe_apk_filename}' not found, but found exactly one APK '{resolved_filename}' in project. Using it.")
            except Exception as ae:
                logger.warning(f"Error auto-detecting single APK: {ae}")
                
        if resolved_filename:
            safe_apk_filename = resolved_filename
            apk_path = os.path.join("uploads", safe_project, safe_apk_filename)

    # Check if the file is located in the local Documents/apks folder
    if not os.path.exists(apk_path):
        local_apks_path = os.path.join("/Users/preethichitte/Documents/apks", safe_apk_filename)
        if os.path.exists(local_apks_path):
            apk_path = local_apks_path
            logger.info(f"Found APK in local apks directory: {apk_path}")

    if not os.path.exists(apk_path):
        raise HTTPException(
            status_code=404, 
            detail=f"APK file '{request.apk_filename}' not found in project '{safe_project}' storage or local apks folder."
        )

    # Resolve package and activity (auto-extract from APK if not specified)
    app_package = request.appPackage
    app_activity = request.appActivity
    
    if not app_package or app_package.strip() == "":
        from appium_service import extract_package_activity
        pkg, act = extract_package_activity(apk_path)
        if pkg:
            app_package = pkg
            logger.info(f"Auto-extracted appPackage: {app_package}")
        if act and (not app_activity or app_activity.strip() == ""):
            app_activity = act
            logger.info(f"Auto-extracted appActivity: {app_activity}")
            
    if not app_package:
        raise HTTPException(
            status_code=400,
            detail="appPackage must be specified or extractable from the APK."
        )

    # Sanitize package name and create run timestamp
    safe_package = re.sub(r'[^a-zA-Z0-9_\.]', '_', app_package).strip()
    from datetime import datetime
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Target folder: project_name/package_name/run_id
    run_folder = f"{safe_project}/{safe_package}/{run_id}"

    client = None
    try:
        # 1. Start Appium session and install app
        client = await start_appium_session(
            device_name=request.deviceName,
            udid=request.udid,
            app_package=app_package,
            app_activity=app_activity,
            device_type=request.deviceType,
            apk_path=apk_path
        )
        
        # Fetch Supabase environment variables
        supabase_url = os.environ.get("SUPABASE_URL")
        supabase_key = os.environ.get("SUPABASE_KEY")

        # 2. Run the recursive app crawler
        crawl_result = await crawl_app_map_appium(
            client=client,
            app_package=app_package,
            app_activity=app_activity,
            project_name=safe_project,
            run_folder=run_folder,
            storage_provider=storage_provider,
            max_steps=request.max_steps,
            prefill_data=request.prefill_data,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            app_name=request.deviceName,
            run_id=run_id,
            use_ollama=request.use_ollama,
            ollama_model=request.ollama_model,
            ollama_url=request.ollama_url,
            user_prompt=request.user_prompt,
            max_scrolls=request.max_scrolls,
            allow_destructive=request.allow_destructive
        )
        
        # 3. Format crawl results into the final App Map JSON format
        nodes_list = list(crawl_result["nodes"].values())
        
        app_map = {
            "app_name": request.deviceName,
            "package": app_package,
            "crawl_date": str(date.today()),
            "run_id": run_id,
            "total_screens_discovered": len(nodes_list),
            "steps_taken": crawl_result["steps_taken"],
            "nodes": nodes_list
        }
        
        # 4. Upload/Save the final App Map JSON to storage provider
        app_map_bytes = json.dumps(app_map, indent=2).encode("utf-8")
        app_map_url = await storage_provider.upload_file(
            file_content=app_map_bytes,
            folder=run_folder,
            filename="app_map.json",
            mime_type="application/json"
        )
        
        # 5. Also save the raw crawl state as requested (yes, user wants this)
        raw_state = {
            "app_package": app_package,
            "app_activity": app_activity,
            "visited_elements": crawl_result["visited_elements"],
            "nodes": crawl_result["nodes"]
        }
        raw_state_bytes = json.dumps(raw_state, indent=2).encode("utf-8")
        await storage_provider.upload_file(
            file_content=raw_state_bytes,
            folder=run_folder,
            filename="app_crawl_state.json",
            mime_type="application/json"
        )

        # 6. Update crawl_runs metadata in Supabase to set status as completed
        if supabase_url and supabase_key:
            final_metadata = {
                "max_steps": request.max_steps,
                "prefill_data": request.prefill_data or {},
                "status": "completed",
                "total_screens_discovered": len(nodes_list),
                "steps_taken": crawl_result["steps_taken"],
                "completed_at": str(datetime.now())
            }
            logger.info(f"Updating crawl run row as completed for run_id={run_id}...")
            try:
                await save_run_metadata(
                    supabase_url=supabase_url,
                    supabase_key=supabase_key,
                    run_id=run_id,
                    app_name=request.deviceName,
                    app_package=app_package,
                    app_activity=app_activity,
                    app_metadata=final_metadata,
                    app_map_url=app_map_url
                )
            except Exception as e:
                logger.error(f"Failed to update final crawl run metadata: {e}")
        
        return {
            "status": "success",
            "project": safe_project,
            "package": safe_package,
            "run_id": run_id,
            "app_map_url": app_map_url,
            "app_map": app_map
        }
        
    except Exception as e:
        logger.error(f"Error during app map generation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")
        
    finally:
        # Ensure we always close/cleanup the Appium session
        if client:
            try:
                await client.stop()
            except Exception as ce:
                logger.warning(f"Error closing Appium client: {ce}")


@router.post("/generate-map-v2")
async def generate_map_v2(request: GenerateMapRequestV2):
    """
    Starts an Appium session, installs the specified APK, runs the recursive DFS crawler (non-AI mode),
    captures page screenshots and layouts, and compiles/uploads the final App Map JSON.
    Each run is isolated in its own folder grouped by package name and timestamp.
    """
    safe_project = os.path.basename(request.project).strip()
    if not safe_project or safe_project in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid project name")
        
    safe_apk_filename = os.path.basename(request.apk_filename).strip()
    if not safe_apk_filename or safe_apk_filename in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid APK filename")

    # Locate APK path
    apk_path = os.path.join("uploads", safe_project, safe_apk_filename)
    
    # If APK not found directly, try resolving via mappings.json or auto-detect if exactly one APK exists
    if not os.path.exists(apk_path):
        resolved_filename = None
        
        # 1. Try resolving via mappings.json
        mappings_path = os.path.join("uploads", safe_project, "mappings.json")
        if os.path.exists(mappings_path):
            try:
                with open(mappings_path, "r", encoding="utf-8") as mf:
                    mappings = json.load(mf)
                    if safe_apk_filename in mappings:
                        resolved_filename = mappings[safe_apk_filename]
                        logger.info(f"Resolved original filename '{safe_apk_filename}' to '{resolved_filename}' via mappings.json")
            except Exception as me:
                logger.warning(f"Error reading mappings.json: {me}")
                
        # 2. Try auto-detecting if there is exactly one APK in the directory
        if not resolved_filename:
            try:
                project_dir = os.path.join("uploads", safe_project)
                if os.path.exists(project_dir):
                    apk_files = [f for f in os.listdir(project_dir) if f.lower().endswith(".apk")]
                    if len(apk_files) == 1:
                        resolved_filename = apk_files[0]
                        logger.info(f"APK '{safe_apk_filename}' not found, but found exactly one APK '{resolved_filename}' in project. Using it.")
            except Exception as ae:
                logger.warning(f"Error auto-detecting single APK: {ae}")
                
        if resolved_filename:
            safe_apk_filename = resolved_filename
            apk_path = os.path.join("uploads", safe_project, safe_apk_filename)

    # Check if the file is located in the local Documents/apks folder
    if not os.path.exists(apk_path):
        local_apks_path = os.path.join("/Users/preethichitte/Documents/apks", safe_apk_filename)
        if os.path.exists(local_apks_path):
            apk_path = local_apks_path
            logger.info(f"Found APK in local apks directory: {apk_path}")

    if not os.path.exists(apk_path):
        raise HTTPException(
            status_code=404, 
            detail=f"APK file '{request.apk_filename}' not found in project '{safe_project}' storage or local apks folder."
        )

    # Resolve package and activity (auto-extract from APK if not specified)
    app_package = request.appPackage
    app_activity = request.appActivity
    
    if not app_package or app_package.strip() == "":
        from appium_service import extract_package_activity
        pkg, act = extract_package_activity(apk_path)
        if pkg:
            app_package = pkg
            logger.info(f"Auto-extracted appPackage: {app_package}")
        if act and (not app_activity or app_activity.strip() == ""):
            app_activity = act
            logger.info(f"Auto-extracted appActivity: {app_activity}")
            
    if not app_package:
        raise HTTPException(
            status_code=400,
            detail="appPackage must be specified or extractable from the APK."
        )

    # Sanitize package name and create run timestamp
    safe_package = re.sub(r'[^a-zA-Z0-9_\.]', '_', app_package).strip()
    from datetime import datetime
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Target folder: project_name/package_name/run_id
    run_folder = f"{safe_project}/{safe_package}/{run_id}"

    client = None
    try:
        # 1. Start Appium session and install app
        client = await start_appium_session(
            device_name=request.deviceName,
            udid=request.udid,
            app_package=app_package,
            app_activity=app_activity,
            device_type=request.deviceType,
            apk_path=apk_path
        )
        
        # Fetch Supabase environment variables
        supabase_url = os.environ.get("SUPABASE_URL")
        supabase_key = os.environ.get("SUPABASE_KEY")

        # 2. Run the recursive app crawler (non-AI mode)
        crawl_result = await crawl_app_map_appium(
            client=client,
            app_package=app_package,
            app_activity=app_activity,
            project_name=safe_project,
            run_folder=run_folder,
            storage_provider=storage_provider,
            max_steps=request.max_steps,
            prefill_data=request.prefill_data,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            app_name=request.deviceName,
            run_id=run_id,
            use_ollama=False,
            ollama_model="",
            ollama_url="",
            user_prompt=None,
            max_scrolls=request.max_scrolls,
            allow_destructive=request.allow_destructive
        )
        
        # 3. Format crawl results into the final App Map JSON format
        nodes_list = list(crawl_result["nodes"].values())
        
        app_map = {
            "app_name": request.deviceName,
            "package": app_package,
            "crawl_date": str(date.today()),
            "run_id": run_id,
            "total_screens_discovered": len(nodes_list),
            "steps_taken": crawl_result["steps_taken"],
            "nodes": nodes_list
        }
        
        # 4. Upload/Save the final App Map JSON to storage provider
        app_map_bytes = json.dumps(app_map, indent=2).encode("utf-8")
        app_map_url = await storage_provider.upload_file(
            file_content=app_map_bytes,
            folder=run_folder,
            filename="app_map.json",
            mime_type="application/json"
        )
        
        # 5. Also save the raw crawl state
        raw_state = {
            "app_package": app_package,
            "app_activity": app_activity,
            "visited_elements": crawl_result["visited_elements"],
            "nodes": crawl_result["nodes"]
        }
        raw_state_bytes = json.dumps(raw_state, indent=2).encode("utf-8")
        await storage_provider.upload_file(
            file_content=raw_state_bytes,
            folder=run_folder,
            filename="app_crawl_state.json",
            mime_type="application/json"
        )

        # 6. Update crawl_runs metadata in Supabase to set status as completed
        if supabase_url and supabase_key:
            final_metadata = {
                "max_steps": request.max_steps,
                "prefill_data": request.prefill_data or {},
                "status": "completed",
                "total_screens_discovered": len(nodes_list),
                "steps_taken": crawl_result["steps_taken"],
                "completed_at": str(datetime.now())
            }
            logger.info(f"Updating crawl run row as completed for run_id={run_id}...")
            try:
                await save_run_metadata(
                    supabase_url=supabase_url,
                    supabase_key=supabase_key,
                    run_id=run_id,
                    app_name=request.deviceName,
                    app_package=app_package,
                    app_activity=app_activity,
                    app_metadata=final_metadata,
                    app_map_url=app_map_url
                )
            except Exception as e:
                logger.error(f"Failed to update final crawl run metadata: {e}")
        
        return {
            "status": "success",
            "project": safe_project,
            "package": safe_package,
            "run_id": run_id,
            "app_map_url": app_map_url,
            "app_map": app_map
        }
        
    except Exception as e:
        logger.error(f"Error during app map generation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")
        
    finally:
        # Ensure we always close/cleanup the Appium session
        if client:
            try:
                await client.stop()
            except Exception as ce:
                logger.warning(f"Error closing Appium client: {ce}")








class RunSearchRequest(BaseModel):
    app_package: Optional[str] = None
    project: Optional[str] = None
    limit: Optional[int] = 50


@router.post("/run")
async def list_crawl_runs_post(request: RunSearchRequest):
    """
    Retrieves the list of crawl runs for a specific app_package and/or project passed in the JSON payload,
    ordered by creation timestamp descending.
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    
    if not supabase_url or not supabase_key:
        raise HTTPException(
            status_code=400,
            detail="Supabase credentials are not configured in the environment."
        )
        
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}"
    }
    
    limit_val = request.limit if request.limit is not None else 50
    params = {
        "select": "*",
        "order": "created_at.desc",
        "limit": str(limit_val)
    }
    
    import httpx
    
    if request.project:
        apps_url = f"{supabase_url.rstrip('/')}/rest/v1/apps?project_id=eq.{request.project}&select=app_package"
        try:
            async with httpx.AsyncClient() as client:
                apps_resp = await client.get(apps_url, headers=headers, timeout=10.0)
                if apps_resp.status_code == 200:
                    apps_data = apps_resp.json()
                    packages = list(set([item["app_package"] for item in apps_data if item.get("app_package")]))
                    if not packages:
                        return {
                            "status": "success",
                            "total_runs": 0,
                            "runs": []
                        }
                    pkgs_str = ",".join(packages)
                    params["app_package"] = f"in.({pkgs_str})"
                else:
                    logger.error(f"Failed to fetch apps for project {request.project}: {apps_resp.status_code} - {apps_resp.text}")
        except Exception as e:
            logger.error(f"Error querying apps for project {request.project}: {e}")
            raise HTTPException(status_code=500, detail=f"Database error querying project apps: {str(e)}")

    if request.app_package:
        params["app_package"] = f"eq.{request.app_package}"
        
    url = f"{supabase_url.rstrip('/')}/rest/v1/crawl_runs"
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, params=params, timeout=15.0)
            if resp.status_code != 200:
                logger.error(f"Supabase GET runs failed: {resp.status_code} - {resp.text}")
                raise HTTPException(
                    status_code=resp.status_code if resp.status_code in (400, 401, 403, 404) else 500,
                    detail=f"Failed to retrieve runs from database: {resp.text}"
                )
                
            runs = resp.json()
            return {
                "status": "success",
                "total_runs": len(runs),
                "runs": runs
            }
    except httpx.RequestError as e:
        logger.error(f"HTTP request error querying runs: {e}")
        raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")
    except Exception as e:
        logger.error(f"Error retrieving runs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch runs: {str(e)}")


@router.get("/packages")
async def list_distinct_packages(
    project: Optional[str] = None
):
    """
    Retrieves the list of distinct application packages. Optionally filtered by project.
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    
    if not supabase_url or not supabase_key:
        raise HTTPException(
            status_code=400,
            detail="Supabase credentials are not configured in the environment."
        )
        
    if project:
        url = f"{supabase_url.rstrip('/')}/rest/v1/apps?project_id=eq.{project}&select=app_package,app_name"
    else:
        url = f"{supabase_url.rstrip('/')}/rest/v1/crawl_runs?select=app_package,app_name"
        
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}"
    }
    
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=15.0)
            if resp.status_code != 200:
                logger.error(f"Supabase GET packages failed: {resp.status_code} - {resp.text}")
                raise HTTPException(
                    status_code=resp.status_code if resp.status_code in (400, 401, 403, 404) else 500,
                    detail=f"Failed to retrieve packages: {resp.text}"
                )
                
            runs = resp.json()
            
            # De-duplicate package names in Python
            packages_map = {}
            for run in runs:
                pkg = run.get("app_package")
                name = run.get("app_name")
                if pkg:
                    # Keep the latest or first app_name encountered for the package
                    packages_map[pkg] = name or pkg
                    
            packages_list = [
                {"app_package": pkg, "app_name": name}
                for pkg, name in packages_map.items()
            ]
            
            return {
                "status": "success",
                "total_packages": len(packages_list),
                "packages": packages_list
            }
    except httpx.RequestError as e:
        logger.error(f"HTTP request error querying packages: {e}")
        raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")
    except Exception as e:
        logger.error(f"Error retrieving packages: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch packages: {str(e)}")


@router.get("/runs/{run_id}/screens")
async def get_run_screens(run_id: str):
    """
    Retrieves the list of screens/nodes for a specific crawl run from the 'crawled_screens' table.
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    
    if not supabase_url or not supabase_key:
        raise HTTPException(
            status_code=400,
            detail="Supabase credentials are not configured in the environment."
        )
        
    url = f"{supabase_url.rstrip('/')}/rest/v1/crawled_screens?run_id=eq.{run_id}&select=*&order=order_id.asc"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}"
    }
    
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=15.0)
            if resp.status_code != 200:
                logger.error(f"Supabase GET screens failed: {resp.status_code} - {resp.text}")
                raise HTTPException(
                    status_code=resp.status_code if resp.status_code in (400, 401, 403, 404) else 500,
                    detail=f"Failed to retrieve screens from database: {resp.text}"
                )
            
            screens = resp.json()
            return {
                "status": "success",
                "run_id": run_id,
                "total_screens": len(screens),
                "screens": screens
            }
    except httpx.RequestError as e:
        logger.error(f"HTTP request error querying screens: {e}")
        raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")
    except Exception as e:
        logger.error(f"Error retrieving screens: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch screens: {str(e)}")


@router.delete("/runs/{run_id}")
async def delete_crawl_run(run_id: str):
    """
    Deletes a specific crawl run by run_id, including its database entries in crawl_runs
    and crawled_screens, and its stored files in the storage provider.
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    # Prefer service role key for deletions to bypass RLS policies
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    
    if not supabase_url or not supabase_key:
        raise HTTPException(
            status_code=400,
            detail="Supabase credentials (SUPABASE_URL and either SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY) are not configured in the environment."
        )
        
    # Warn if using publishable key for delete which might be blocked by RLS
    is_using_anon = False
    if not os.environ.get("SUPABASE_SERVICE_ROLE_KEY") and os.environ.get("SUPABASE_KEY", "").startswith("sb_publishable_"):
        is_using_anon = True
        logger.warning(
            "Using a publishable/anon Supabase key for delete operation. "
            "Make sure Row Level Security (RLS) is disabled or has a policy that allows DELETE for the anon/public role, "
            "otherwise Supabase will silently ignore the deletion."
        )
        
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Prefer": "return=representation"
    }
    
    import httpx
    
    # 1. Delete from crawled_screens (child table referencing run_id)
    screens_delete_url = f"{supabase_url.rstrip('/')}/rest/v1/crawled_screens?run_id=eq.{run_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(screens_delete_url, headers=headers, timeout=15.0)
            logger.info(f"Supabase DELETE screens response: {resp.status_code} - {resp.text}")
            if resp.status_code not in (200, 204):
                logger.error(f"Supabase DELETE screens failed: {resp.status_code} - {resp.text}")
                raise HTTPException(
                    status_code=resp.status_code if resp.status_code in (400, 401, 403, 404) else 500,
                    detail=f"Failed to delete screens from database: {resp.text}"
                )
            
            # Check if any screens were deleted
            if resp.status_code == 200:
                try:
                    deleted_screens = resp.json()
                    if not deleted_screens and is_using_anon:
                        logger.warning(f"No rows deleted from crawled_screens. This is likely due to Row Level Security (RLS) policies blocking the DELETE operation for the publishable key.")
                except Exception:
                    pass
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error deleting screens from database for run {run_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete screens from database: {str(e)}")
        
    # 2. Delete from crawl_runs (parent table)
    runs_delete_url = f"{supabase_url.rstrip('/')}/rest/v1/crawl_runs?run_id=eq.{run_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(runs_delete_url, headers=headers, timeout=15.0)
            logger.info(f"Supabase DELETE run response: {resp.status_code} - {resp.text}")
            if resp.status_code not in (200, 204):
                logger.error(f"Supabase DELETE run failed: {resp.status_code} - {resp.text}")
                raise HTTPException(
                    status_code=resp.status_code if resp.status_code in (400, 401, 403, 404) else 500,
                    detail=f"Failed to delete run from database: {resp.text}"
                )
            
            # Check if run was deleted
            if resp.status_code == 200:
                try:
                    deleted_runs = resp.json()
                    if not deleted_runs:
                        msg = "No rows were deleted from crawl_runs. This is likely due to Supabase Row Level Security (RLS) policies blocking the DELETE operation for this API key. Please configure RLS DELETE policy for the anon/public role or provide the SUPABASE_SERVICE_ROLE_KEY."
                        logger.error(msg)
                        raise HTTPException(status_code=403, detail=msg)
                except Exception as ex:
                    if isinstance(ex, HTTPException):
                        raise ex
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error deleting run from database: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete run from database: {str(e)}")

    # 3. Delete from storage (clean up uploaded screenshots, app map, etc.)
    try:
        logger.info(f"Cleaning up storage assets for run_id={run_id}...")
        await storage_provider.delete_folder(run_id)
    except Exception as e:
        logger.warning(f"Failed to clean up storage files for run_id={run_id}: {e}. Database deletion succeeded.")
        return {
            "status": "partial_success",
            "message": f"Run {run_id} deleted from database, but storage cleanup encountered an issue: {str(e)}"
        }

    return {
        "status": "success",
        "message": f"Run {run_id} and all associated data deleted successfully."
    }


@router.post("/app-local-upload")
async def upload_app_local(
    project: str = Form(...),
    name: Optional[str] = Form(None),
    file: UploadFile = File(...)
):
    """
    Accepts an APK file upload, stores it in the local filesystem,
    extracts its package name and main activity, and records the
    metadata in the 'apps' table in Supabase.
    """
    # 1. Sanitize project ID to prevent path traversal
    safe_project_id = os.path.basename(project).strip()
    if not safe_project_id or safe_project_id in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid project ID")
        
    # 2. Validate file extension
    filename = file.filename or "unknown.apk"
    _, ext = os.path.splitext(filename.lower())
    if ext != ".apk":
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid file extension '{ext}'. Only '.apk' files are allowed."
        )

    # 3. Read content and validate size
    contents = bytearray()
    total_read = 0
    while True:
        chunk = await file.read(8192)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > MAX_APK_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File size exceeds the maximum limit of {MAX_APK_SIZE // (1024 * 1024)}MB."
            )
        contents.extend(chunk)

    # 4. Verify magic bytes for zip/apk structure
    if len(contents) < 4 or contents[:4] != b"PK\x03\x04":
        raise HTTPException(
            status_code=400,
            detail="Invalid file content: The uploaded file is not a valid APK/ZIP archive."
        )

    # 5. Generate secure, unique filename to avoid Path Traversal and collisions
    secure_filename = f"{uuid.uuid4()}.apk"
    
    # Save a copy to the user's local Documents/apks folder
    local_apks_dir = "/Users/preethichitte/Documents/apks"
    os.makedirs(local_apks_dir, exist_ok=True)
    local_apk_path = os.path.join(local_apks_dir, secure_filename)
    try:
        with open(local_apk_path, "wb") as f:
            f.write(contents)
    except Exception as le:
        logger.error(f"Failed to save local copy to Documents/apks: {le}")
        raise HTTPException(status_code=500, detail="Failed to save local copy of APK.")

    # Save to local uploads directory
    local_project_dir = os.path.join("uploads", safe_project_id)
    os.makedirs(local_project_dir, exist_ok=True)
    local_project_apk_path = os.path.join(local_project_dir, secure_filename)
    
    # Sandbox path validation
    resolved_base = os.path.realpath("uploads") + os.path.sep
    resolved_target = os.path.realpath(local_project_apk_path)
    if not resolved_target.startswith(resolved_base):
        raise HTTPException(status_code=400, detail="Path traversal attempt detected!")
        
    try:
        with open(local_project_apk_path, "wb") as f:
            f.write(contents)
    except Exception as le:
        logger.error(f"Failed to save copy to project uploads: {le}")
        raise HTTPException(status_code=500, detail="Failed to save project upload copy.")

    # 6. Extract package, main activity, and app label from the saved APK
    from appium_service import extract_apk_metadata
    app_package, main_activity, app_label = extract_apk_metadata(local_project_apk_path)
    
    if not app_package:
        app_package = "unknown.package"
    if not main_activity:
        main_activity = "unknown.MainActivity"

    # Use the name form parameter if provided, otherwise fallback to app label, then filename
    if name and name.strip():
        app_display_name = name.strip()
    elif app_label and app_label.strip():
        app_display_name = app_label.strip()
    else:
        app_display_name = filename

    # 7. Create/insert a record into the 'apps' table in Supabase
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    
    if not supabase_url or not supabase_key:
        logger.warning("Supabase credentials not configured. Skipping database record insertion.")
        return {
            "status": "success",
            "message": "APK uploaded successfully locally, but Supabase is not configured.",
            "app_name": app_display_name,
            "path": local_project_apk_path,
            "main_activity": main_activity,
            "project_id": safe_project_id,
            "app_package": app_package
        }

    url = f"{supabase_url.rstrip('/')}/rest/v1/apps"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    
    payload = {
        "app_name": app_display_name,
        "path": local_project_apk_path,
        "main_activity": main_activity,
        "project_id": safe_project_id,
        "app_package": app_package
    }
    
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=20.0)
            if resp.status_code not in (200, 201):
                logger.error(f"Failed to insert record into Supabase 'apps' table: {resp.status_code} - {resp.text}")
                return {
                    "status": "partial_success",
                    "message": f"APK uploaded locally, but failed to insert record in Supabase: {resp.text}",
                    "app_name": app_display_name,
                    "path": local_project_apk_path,
                    "main_activity": main_activity,
                    "project_id": safe_project_id,
                    "app_package": app_package
                }
            
            db_record = resp.json()
            return {
                "status": "success",
                "message": "APK uploaded locally and record created in Supabase 'apps' table.",
                "record": db_record
            }
    except Exception as e:
        logger.error(f"Error communicating with Supabase: {e}", exc_info=True)
        return {
            "status": "partial_success",
            "message": f"APK uploaded locally, but error occurred inserting record in Supabase: {str(e)}",
            "app_name": app_display_name,
            "path": local_project_apk_path,
            "main_activity": main_activity,
            "project_id": safe_project_id,
            "app_package": app_package
        }

