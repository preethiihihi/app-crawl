import os
import time
import shutil
import socket
import logging
import asyncio
import subprocess
import json
from typing import Dict, Any, List, Optional
# standard logging setup
logger = logging.getLogger("appium_service")

_PLACEHOLDER_VALUES = {
    "string",
    "null",
    "none",
    "undefined",
    "n/a",
    "na",
}


def is_meaningful_config_value(value: Any) -> bool:
    """Return True only for real session config values, not placeholders."""
    if value is None:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return False
        if normalized in _PLACEHOLDER_VALUES:
            return False
        return True
    return True

def get_connected_devices() -> List[str]:
    """Runs 'adb devices' and returns a list of online device UDIDs/serials."""
    try:
        adb_path = shutil.which("adb")
        if adb_path is None:
            sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
            adb_path = os.path.join(sdk_root, "platform-tools", "adb")
            if not os.path.exists(adb_path):
                logger.warning("adb not found in PATH or Android SDK root. Cannot scan for connected devices.")
                return []
        
        output = subprocess.check_output([adb_path, "devices"]).decode()
        devices = []
        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices
    except Exception as e:
        logger.warning(f"Failed to get connected devices via adb: {e}")
        return []

def get_current_package_and_activity() -> tuple[Optional[str], Optional[str]]:
    """Auto-detect currently running package and activity on ADB emulator/device."""
    try:
        adb_path = shutil.which("adb")
        if not adb_path:
            sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
            adb_path = os.path.join(sdk_root, "platform-tools", "adb")
            
        output = subprocess.check_output([adb_path, "shell", "dumpsys", "window", "windows"]).decode()
        for line in output.splitlines():
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                parts = line.split()
                for part in parts:
                    if "/" in part:
                        part = part.strip("{}").strip(",")
                        if "=" in part:
                            part = part.split("=")[-1]
                        pkg, act = part.split("/", 1)
                        return pkg.strip(), act.strip()
    except Exception as e:
        logger.warning(f"Failed to auto-detect package/activity via ADB: {e}")
    return None, None

def is_appium_running(port: int = 4723) -> bool:
    """Checks if there is a running process listening on the Appium port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except Exception:
        return False

def ensure_appium_server(device_type: str, port: int = 4723):
    """Spawns the Appium server dynamically if it is not already running."""
    device_type_lower = (device_type or "").lower().strip()
    if device_type_lower in ("docker-android", "cloud"):
        logger.info(f"Bypassing local Appium server startup on host for device type: '{device_type}'.")
        return

    if is_appium_running(port):
        logger.info(f"Appium server is already running on port {port}.")
        return

    logger.info(f"Appium server not detected on port {port}. Starting Appium dynamically...")
    
    # Path to Appium on the user's mac
    appium_path = "/Users/preethichitte/.nvm/versions/node/v20.19.5/bin/appium"
    if not os.path.exists(appium_path):
        appium_path = "appium"  # Fallback to standard PATH search

    try:
        log_file_path = os.path.join(os.getcwd(), "appium_server.log")
        log_file_path = os.path.abspath(log_file_path)
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        log_file = open(log_file_path, "a")
        
        process = subprocess.Popen(
            [appium_path, "--port", str(port), "--address", "127.0.0.1"],
            stdout=log_file,
            stderr=log_file,
            env=os.environ.copy()
        )
        logger.info(f"Appium process spawned dynamically (PID: {process.pid}) logging to {log_file_path}. Waiting for connection...")
        
        # Poll up to 15 seconds to ensure Appium finishes binding the port
        for _ in range(30):
            time.sleep(0.5)
            if is_appium_running(port):
                logger.info("Appium server has started and is listening successfully!")
                return
        
        logger.warning(f"Spawned Appium but it did not listen on port {port} in time.")
    except Exception as e:
        logger.error(f"Failed to auto-spawn Appium server: {e}", exc_info=True)
        raise RuntimeError(f"Could not automatically start Appium server: {e}")

def extract_package_activity(apk_path: str):
    """Extract package and launchable activity from an APK using aapt."""
    aapt_path = "aapt"
    if shutil.which("aapt") is None:
        sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
        build_tools_dir = os.path.join(sdk_root, "build-tools")
        if os.path.exists(build_tools_dir):
            aapt_files = []
            for root_dir, dirs, files in os.walk(build_tools_dir):
                if "aapt" in files:
                    aapt_files.append(os.path.join(root_dir, "aapt"))
            if aapt_files:
                aapt_files.sort(reverse=True)
                aapt_path = aapt_files[0]
                logger.info(f"Dynamically discovered aapt path: {aapt_path}")

    try:
        output = subprocess.check_output([aapt_path, "dump", "badging", apk_path]).decode()
        package, activity = None, None
        for line in output.splitlines():
            if line.startswith("package:"):
                package = line.split("name='")[1].split("'")[0]
            if line.startswith("launchable-activity:"):
                activity = line.split("name='")[1].split("'")[0]
        return package, activity
    except Exception as e:
        logger.warning(f"Failed to extract package/activity: {e}")
        return None, None


class AppiumMcpClient:
    def __init__(self, server_dir: str, env: Optional[Dict[str, str]] = None):
        if server_dir == "appium-android":
            server_dir = "/Users/preethichitte/Documents/mcp_appium_server"
        self.server_dir = server_dir
        self.env = env or {}
        self.process: Optional[asyncio.subprocess.Process] = None
        self.request_id = 1
        self.pending_responses: Dict[int, asyncio.Future] = {}
        self._read_task: Optional[asyncio.Task] = None
        self._err_task: Optional[asyncio.Task] = None

    async def start(self):
        """Spawns the Appium MCP server subprocess in STDIO mode."""
        subprocess_env = os.environ.copy()
        subprocess_env.update(self.env)
        subprocess_env["USE_STDIO"] = "true"
        
        is_npm_package = (
            self.server_dir == "appium-android" or
            self.server_dir == "@appium/mcp-driver" or
            not os.path.exists(self.server_dir)
        )
        
        if is_npm_package:
            pkg_name = "appium-mcp" if self.server_dir in ("appium-android", "@appium/mcp-driver", "appium-mcp") else self.server_dir
            logger.info(f"Spawning community Appium MCP Server via: npx -y {pkg_name}")
            self.process = await asyncio.create_subprocess_exec(
                "npx", "-y", pkg_name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=subprocess_env,
                limit=1024 * 1024 * 10  # 10MB limit
            )
        else:
            logger.info(f"Spawning local custom Appium MCP Server subprocess in {self.server_dir}...")
            self.process = await asyncio.create_subprocess_exec(
                "npx", "tsx", "server.js",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.server_dir,
                env=subprocess_env,
                limit=1024 * 1024 * 10  # 10MB limit
            )
        
        self._read_task = asyncio.create_task(self._read_stdout_loop())
        self._err_task = asyncio.create_task(self._read_stderr_loop())

        await asyncio.sleep(1.0)
        
        if self.process.returncode is not None:
            raise RuntimeError("Appium MCP Subprocess failed to start or exited immediately.")

        await self._handshake()
        logger.info("Appium MCP Server initialized successfully!")

    async def _handshake(self):
        """Performs the standard Model Context Protocol initialize handshake."""
        logger.info("Sending initialize request...")
        init_response = await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "fastapi-backend-agent",
                "version": "1.0.0"
            }
        })
        logger.info(f"Received initialize response: {init_response}")

        await self._send_notification("notifications/initialized")
        logger.info("Sent notifications/initialized notification.")

    async def _send_request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Sends a JSON-RPC request and awaits the corresponding response."""
        if not self.process or not self.process.stdin:
            raise RuntimeError("Subprocess is not running.")
            
        curr_id = self.request_id
        self.request_id += 1

        payload = {
            "jsonrpc": "2.0",
            "id": curr_id,
            "method": method,
            "params": params or {}
        }

        future = asyncio.get_running_loop().create_future()
        self.pending_responses[curr_id] = future

        message = json.dumps(payload) + "\n"
        self.process.stdin.write(message.encode())
        await self.process.stdin.drain()

        try:
            if timeout_seconds:
                return await asyncio.wait_for(future, timeout=timeout_seconds)
            return await future
        except asyncio.TimeoutError:
            self.pending_responses.pop(curr_id, None)
            raise TimeoutError(f"MCP request '{method}' timed out after {timeout_seconds} seconds.")
        except Exception as e:
            logger.error(f"Error awaiting future for request {curr_id}: {e}")
            raise

    async def _send_notification(self, method: str, params: Optional[Dict[str, Any]] = None):
        """Sends a JSON-RPC notification (no response expected)."""
        if not self.process or not self.process.stdin:
            raise RuntimeError("Subprocess is not running.")
            
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {}
        }
        message = json.dumps(payload) + "\n"
        self.process.stdin.write(message.encode())
        await self.process.stdin.drain()

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Lists available Appium tools on the MCP server."""
        response = await self._send_request("tools/list")
        return response.get("result", {}).get("tools", [])

    async def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Calls a specific Appium tool and returns the response."""
        response = await self._send_request("tools/call", {
            "name": name,
            "arguments": arguments
        }, timeout_seconds=timeout_seconds)
        return response.get("result", {})

    async def _read_stdout_loop(self):
        """Reads lines from the subprocess stdout stream and matches them to pending futures."""
        try:
            while self.process and not self.process.stdout.at_eof():
                line = await self.process.stdout.readline()
                if not line:
                    break
                
                decoded_line = line.decode().strip()
                if not decoded_line:
                    continue

                try:
                    data = json.loads(decoded_line)
                    if "id" in data:
                        resp_id = data["id"]
                        if resp_id in self.pending_responses:
                            future = self.pending_responses.pop(resp_id)
                            if "error" in data:
                                future.set_exception(Exception(data["error"]))
                            else:
                                future.set_result(data)
                        else:
                            logger.warning(f"Received JSON-RPC response with unrecognized id: {resp_id}")
                    else:
                        logger.debug(f"[Appium MCP JSON Event/Notification]: {data}")
                except json.JSONDecodeError:
                    logger.debug(f"[Appium MCP Raw stdout]: {decoded_line}")
                except Exception as e:
                    logger.error(f"Error handling line from server stdout: {e}. Line: {decoded_line}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Unexpected error in _read_stdout_loop: {e}", exc_info=True)

    async def _read_stderr_loop(self):
        """Streams stderr from the subprocess to Python logging console."""
        try:
            while self.process and not self.process.stderr.at_eof():
                line = await self.process.stderr.readline()
                if not line:
                    break
                logger.info(f"[Appium MCP Subprocess log] {line.decode().strip()}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading stderr: {e}")

    async def stop(self):
        """Gracefully terminates the Appium MCP subprocess."""
        logger.info("Stopping Appium MCP Subprocess...")
        if self._read_task:
            self._read_task.cancel()
        if self._err_task:
            self._err_task.cancel()
            
        for future in self.pending_responses.values():
            if not future.done():
                future.set_exception(RuntimeError("Connection closed before response was received."))
        self.pending_responses.clear()
        
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=3.0)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
        logger.info("Appium MCP Subprocess shut down successfully.")
