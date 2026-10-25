from __future__ import annotations

import threading
import time

import httpx
import pytest
import uvicorn

from env.db import init_db
from env.faults.manager import fault_manager
from env.seed.generator import seed_company_data
from env.server import app

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8001
BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"


class UvicornTestServer(uvicorn.Server):
    """Server runner that allows clean shutdown in tests."""

    def install_signal_handlers(self) -> None:
        pass


@pytest.fixture(scope="session", autouse=True)
def live_server():
    """Start mock environment on 127.0.0.1:8001 in background thread."""
    init_db()
    seed_company_data()
    fault_manager.reset_faults()

    config = uvicorn.Config(app, host=SERVER_HOST, port=SERVER_PORT, log_level="error")
    server = UvicornTestServer(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Poll until server is ready
    ready = False
    for _ in range(50):
        try:
            with httpx.Client(timeout=1.0) as client:
                res = client.get(f"{BASE_URL}/__truth/state")
                if res.status_code == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.1)

    if not ready:
        raise RuntimeError("Live mock server failed to start on 127.0.0.1:8001")

    yield

    server.should_exit = True
    thread.join(timeout=2.0)


@pytest.fixture(autouse=True)
def reset_env_state():
    """Reset faults before each test."""
    fault_manager.reset_faults()


@pytest.fixture(autouse=True)
async def cleanup_browser():
    """Close any active browser session between tests."""
    yield
    try:
        from core.tools.browser import _global_browser_manager
        if _global_browser_manager is not None:
            await _global_browser_manager.close()
    except Exception:
        pass
