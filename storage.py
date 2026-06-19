import os
import logging
import httpx
from typing import List, Dict

logger = logging.getLogger("storage")

class BaseStorageProvider:
    async def upload_file(self, file_content: bytes, folder: str, filename: str, mime_type: str) -> str:
        """Uploads file to a folder and returns the access/download URL."""
        raise NotImplementedError()

    async def list_files(self, folder: str) -> List[Dict[str, str]]:
        """Lists files in a folder, returning their names and access/download URLs."""
        raise NotImplementedError()

    async def delete_folder(self, folder: str) -> None:
        """Deletes all files in the folder (and the folder itself if applicable)."""
        raise NotImplementedError()


class LocalStorageProvider(BaseStorageProvider):
    def __init__(self, base_dir: str = "uploads"):
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)
        # TODO(security): Ensure local uploads directory is non-executable.
        # Under normal conditions, FastAPI static mount or file systems should not execute files.

    async def upload_file(self, file_content: bytes, folder: str, filename: str, mime_type: str) -> str:
        # Sanitize folder and filename using basename to prevent directory traversal
        safe_folder = os.path.basename(folder)
        safe_filename = os.path.basename(filename)
        
        target_dir = os.path.join(self.base_dir, safe_folder)
        os.makedirs(target_dir, exist_ok=True)
        
        target_path = os.path.join(target_dir, safe_filename)
        
        # Verify target_path is within base_dir to enforce sandbox
        resolved_base = os.path.realpath(self.base_dir) + os.path.sep
        resolved_target = os.path.realpath(target_path)
        if not resolved_target.startswith(resolved_base):
            raise ValueError("Path traversal attempt detected!")
            
        with open(target_path, "wb") as f:
            f.write(file_content)
            
        # Return local path identifier
        return f"/uploads/{safe_folder}/{safe_filename}"

    async def list_files(self, folder: str) -> List[Dict[str, str]]:
        safe_folder = os.path.basename(folder)
        target_dir = os.path.join(self.base_dir, safe_folder)
        if not os.path.exists(target_dir):
            return []
            
        resolved_base = os.path.realpath(self.base_dir) + os.path.sep
        resolved_target = os.path.realpath(target_dir)
        if not resolved_target.startswith(resolved_base):
            raise ValueError("Path traversal attempt detected!")
            
        files = []
        for name in os.listdir(target_dir):
            file_path = os.path.join(target_dir, name)
            if os.path.isfile(file_path):
                files.append({
                    "name": name,
                    "url": f"/uploads/{safe_folder}/{name}"
                })
        return files

    async def delete_folder(self, folder: str) -> None:
        safe_folder = os.path.basename(folder)
        target_dir = os.path.join(self.base_dir, safe_folder)
        if not os.path.exists(target_dir):
            return
            
        resolved_base = os.path.realpath(self.base_dir) + os.path.sep
        resolved_target = os.path.realpath(target_dir)
        if not resolved_target.startswith(resolved_base):
            raise ValueError("Path traversal attempt detected!")
            
        import shutil
        shutil.rmtree(target_dir)
        logger.info(f"Deleted local directory: {target_dir}")


class SupabaseStorageProvider(BaseStorageProvider):
    def __init__(self, supabase_url: str, supabase_key: str, bucket: str):
        self.supabase_url = supabase_url.rstrip("/")
        self.supabase_key = supabase_key
        self.bucket = bucket
        self.headers = {
            "Authorization": f"Bearer {self.supabase_key}",
            "apikey": self.supabase_key
        }
        self.bucket_ensured = False

    async def ensure_bucket(self):
        """Attempts to create the bucket if it does not exist."""
        if self.bucket_ensured:
            return
        url = f"{self.supabase_url}/storage/v1/bucket"
        payload = {
            "id": self.bucket,
            "name": self.bucket,
            "public": True
        }
        headers = {
            **self.headers,
            "Content-Type": "application/json"
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, headers=headers, timeout=10.0)
                if resp.status_code == 200:
                    logger.info(f"Created Supabase bucket: {self.bucket}")
                elif resp.status_code == 409 or "already exists" in resp.text:
                    logger.info(f"Supabase bucket '{self.bucket}' already exists.")
                else:
                    logger.warning(f"Could not ensure bucket exists: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.warning(f"Exception trying to ensure bucket exists: {e}")
        finally:
            self.bucket_ensured = True

    async def upload_file(self, file_content: bytes, folder: str, filename: str, mime_type: str) -> str:
        await self.ensure_bucket()
        
        # Sanitize paths to prevent traversal
        safe_folder = os.path.basename(folder)
        safe_filename = os.path.basename(filename)
        path_in_bucket = f"{safe_folder}/{safe_filename}"
        
        url = f"{self.supabase_url}/storage/v1/object/{self.bucket}/{path_in_bucket}"
        
        headers = {
            **self.headers,
            "Content-Type": mime_type,
            "x-upsert": "true"
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, content=file_content, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Supabase upload failed: {resp.status_code} - {resp.text}")
                raise Exception(f"Failed to upload to Supabase: {resp.text}")
            
            # Return the public URL for the file
            public_url = f"{self.supabase_url}/storage/v1/object/public/{self.bucket}/{path_in_bucket}"
            return public_url

    async def list_files(self, folder: str) -> List[Dict[str, str]]:
        await self.ensure_bucket()
        
        safe_folder = os.path.basename(folder)
        url = f"{self.supabase_url}/storage/v1/object/list/{self.bucket}"
        
        payload = {
            "prefix": f"{safe_folder}/",
            "limit": 100,
            "offset": 0,
            "sortBy": {
                "column": "name",
                "order": "asc"
            }
        }
        
        headers = {
            **self.headers,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=30.0)
            if resp.status_code != 200:
                logger.error(f"Supabase list failed: {resp.status_code} - {resp.text}")
                raise Exception(f"Failed to list from Supabase: {resp.text}")
                
            data = resp.json()
            files = []
            for item in data:
                name = item.get("name")
                if name:
                    # Supabase list returns names relative to the bucket or prefix depending on depth.
                    # Usually it's just the file name inside the prefixed folder.
                    path_in_bucket = f"{safe_folder}/{name}"
                    public_url = f"{self.supabase_url}/storage/v1/object/public/{self.bucket}/{path_in_bucket}"
                    files.append({
                        "name": name,
                        "url": public_url
                      })
            return files

    async def delete_folder(self, folder: str) -> None:
        await self.ensure_bucket()
        safe_folder = os.path.basename(folder)
        
        # List files first to get their names
        files = await self.list_files(safe_folder)
        if not files:
            return
            
        # The prefixes must be relative to the bucket.
        # E.g. "run_id/filename.png"
        prefixes = [f"{safe_folder}/{f['name']}" for f in files]
        
        url = f"{self.supabase_url}/storage/v1/object/{self.bucket}"
        headers = {
            **self.headers,
            "Content-Type": "application/json"
        }
        payload = {
            "prefixes": prefixes
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.request("DELETE", url, json=payload, headers=headers, timeout=30.0)
            if resp.status_code != 200:
                logger.error(f"Supabase delete failed: {resp.status_code} - {resp.text}")
                raise Exception(f"Failed to delete files from Supabase: {resp.text}")
            logger.info(f"Successfully deleted {len(prefixes)} files from Supabase bucket '{self.bucket}' under folder '{safe_folder}'")


class FirebaseStorageProvider(BaseStorageProvider):
    def __init__(self, key_path: str, bucket_name: str):
        try:
            from google.cloud import storage
        except ImportError:
            raise ImportError(
                "The 'google-cloud-storage' package is required to use Firebase storage. "
                "Please run: pip install google-cloud-storage"
            )
        
        self.key_path = key_path
        self.bucket_name = bucket_name.replace("gs://", "").rstrip("/")
        
        if os.path.exists(key_path):
            self.client = storage.Client.from_service_account_json(key_path)
        else:
            logger.warning(f"Firebase key file not found at '{key_path}'. Attempting default credentials...")
            self.client = storage.Client()
            
        self.bucket = self.client.bucket(self.bucket_name)

    async def upload_file(self, file_content: bytes, folder: str, filename: str, mime_type: str) -> str:
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._upload_sync, file_content, folder, filename, mime_type)

    def _upload_sync(self, file_content: bytes, folder: str, filename: str, mime_type: str) -> str:
        safe_folder = os.path.basename(folder)
        safe_filename = os.path.basename(filename)
        blob_path = f"{safe_folder}/{safe_filename}"
        
        blob = self.bucket.blob(blob_path)
        blob.upload_from_string(file_content, content_type=mime_type)
        
        # Format public Firebase direct URL
        import urllib.parse
        encoded_path = urllib.parse.quote(blob_path, safe='')
        download_url = f"https://firebasestorage.googleapis.com/v0/b/{self.bucket_name}/o/{encoded_path}?alt=media"
        return download_url

    async def list_files(self, folder: str) -> List[Dict[str, str]]:
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._list_sync, folder)

    def _list_sync(self, folder: str) -> List[Dict[str, str]]:
        safe_folder = os.path.basename(folder)
        prefix = f"{safe_folder}/"
        
        blobs = self.bucket.list_blobs(prefix=prefix)
        files = []
        
        import urllib.parse
        for blob in blobs:
            name = blob.name[len(prefix):]
            if name and not name.endswith("/"):
                encoded_path = urllib.parse.quote(blob.name, safe='')
                download_url = f"https://firebasestorage.googleapis.com/v0/b/{self.bucket_name}/o/{encoded_path}?alt=media"
                files.append({
                    "name": name,
                    "url": download_url
                })
        return files

    async def delete_folder(self, folder: str) -> None:
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._delete_folder_sync, folder)

    def _delete_folder_sync(self, folder: str) -> None:
        safe_folder = os.path.basename(folder)
        prefix = f"{safe_folder}/"
        blobs = self.bucket.list_blobs(prefix=prefix)
        deleted_count = 0
        for blob in blobs:
            blob.delete()
            deleted_count += 1
        if deleted_count > 0:
            logger.info(f"Successfully deleted {deleted_count} blobs from Firebase bucket under folder '{safe_folder}'")


def get_storage_provider() -> BaseStorageProvider:
    provider_type = os.environ.get("STORAGE_PROVIDER", "").lower()
    
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    bucket = os.environ.get("SUPABASE_BUCKET", "apks")
    
    firebase_key = os.environ.get("FIREBASE_KEY_PATH", "firebase-key.json")
    firebase_bucket = os.environ.get("FIREBASE_BUCKET")
    
    if provider_type == "firebase" or (not provider_type and firebase_bucket):
        if not firebase_bucket:
            raise ValueError("FIREBASE_BUCKET must be set to use firebase storage provider.")
        logger.info(f"Initializing FirebaseStorageProvider for bucket '{firebase_bucket}'")
        return FirebaseStorageProvider(firebase_key, firebase_bucket)
    elif provider_type == "supabase" or (not provider_type and url and key):
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set to use supabase storage provider.")
        logger.info("Initializing SupabaseStorageProvider")
        return SupabaseStorageProvider(url, key, bucket)
    else:
        logger.info("Initializing LocalStorageProvider")
        return LocalStorageProvider()

