import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.appium_mcp import router as appium_mcp_router
from routers.testily import router as testily_router

app = FastAPI(title="FastAPI Ollama Integration")

# CORS Middleware Configuration
# TODO(security): Restrict allow_origins to trusted domains in production environments.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"message": "Hello World"}

# Register the Appium MCP and Ollama endpoints
app.include_router(appium_mcp_router)
app.include_router(testily_router)

from routers.functions import router as functions_router
app.include_router(functions_router)

