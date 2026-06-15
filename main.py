import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from fastapi import FastAPI
from routers.appium_mcp import router as appium_mcp_router

app = FastAPI(title="FastAPI Ollama Integration")

@app.get("/")
def read_root():
    return {"message": "Hello World"}

# Register the Appium MCP and Ollama endpoints
app.include_router(appium_mcp_router)
