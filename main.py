import os
import uvicorn
import requests
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
import time
import asyncio
import websockets # Library for WebSocket client
from urllib.parse import urlparse
import logging

# Setup logging
# logging.basicConfig(level=logging.INFO) # Commented out to allow Uvicorn to control logging
logger = logging.getLogger(__name__)

# Configuration
JUMPSERVER_URL = os.getenv("JUMPSERVER_URL")
JUMPSERVER_API_TOKEN = os.getenv("JUMPSERVER_API_TOKEN")
JUMPSERVER_ORG_ID = os.getenv("JUMPSERVER_ORG_ID", "00000000-0000-0000-0000-000000000002")
MCP_WS_HOST = os.getenv("MCP_WS_HOST", "localhost") # Host for MCP's own WebSocket URL
MCP_PORT = int(os.getenv("MCP_PORT", "8000")) # Port for MCP's own HTTP/WebSocket URL


app = FastAPI()

# In-memory store for active WebSocket sessions and their details
active_sessions = {}

class McpServer(BaseModel):
    id: str
    hostname: str
    ip: str
    platform: str
    os: str
    comment: str

class McpServerList(BaseModel):
    servers: List[McpServer]

class ConnectRequest(BaseModel):
    system_user_id: str
    connection_type: Optional[str] = "ssh"

class ConnectResponse(BaseModel):
    mcp_session_id: str
    ws_url: str


@app.get("/mcp/api/v1/servers", response_model=McpServerList)
async def get_servers():
    if not JUMPSERVER_URL or not JUMPSERVER_API_TOKEN:
        logger.error("Jumpserver URL or API token not configured")
        raise HTTPException(status_code=500, detail="Jumpserver URL or API token not configured")

    headers = {
        "Authorization": f"Token {JUMPSERVER_API_TOKEN}",
        "X-JMS-ORG": JUMPSERVER_ORG_ID,
    }
    
    jumpserver_api_url = f"{JUMPSERVER_URL.rstrip('/')}/api/v1/assets/assets/"
    logger.info(f"Fetching servers from Jumpserver API: {jumpserver_api_url}")

    try:
        response = requests.get(jumpserver_api_url, headers=headers)
        logger.info(f"Jumpserver API response status: {response.status_code}")
        response.raise_for_status()  # Raise an exception for HTTP errors
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Jumpserver: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to connect to Jumpserver: {e}")

    jumpserver_assets = response.json()
    mcp_servers = []

    for asset in jumpserver_assets:
        mcp_servers.append(McpServer(
            id=asset.get("id", "N/A"),
            hostname=asset.get("name", asset.get("hostname", "N/A")),
            ip=asset.get("ip", asset.get("address", "N/A")),
            platform=asset.get("platform", {}).get("name", asset.get("platform", "N/A")),
            os=asset.get("os", "N/A"),
            comment=asset.get("comment", asset.get("description", "N/A")),
        ))

    return McpServerList(servers=mcp_servers)


@app.post("/mcp/api/v1/servers/{server_id}/connect", response_model=ConnectResponse)
async def connect_to_server(server_id: str, connect_request: ConnectRequest):
    if not JUMPSERVER_URL or not JUMPSERVER_API_TOKEN:
        logger.error("Jumpserver URL or API token not configured for connect endpoint")
        raise HTTPException(status_code=500, detail="Jumpserver URL or API token not configured")

    jms_connection_token = None
    # Step 1: Attempt to get a Jumpserver connection token
    token_url = f"{JUMPSERVER_URL.rstrip('/')}/api/v1/users/connection-token/?user-only=1"
    headers = {
        "Authorization": f"Token {JUMPSERVER_API_TOKEN}",
        "X-JMS-ORG": JUMPSERVER_ORG_ID,
    }
    logger.info(f"Attempting to get Jumpserver connection token from: {token_url}")
    try:
        response = requests.get(token_url, headers=headers)
        logger.info(f"Connection token response status: {response.status_code}")
        logger.info(f"Connection token response headers: {response.headers}")
        logger.info(f"Connection token response body: {response.text}")
        if response.status_code == 200:
            token_data = response.json()
            if "token" in token_data:
                jms_connection_token = token_data["token"]
                logger.info(f"Successfully obtained jms_connection_token: {jms_connection_token}")
            else:
                logger.warning("Jumpserver connection token endpoint returned 200 OK but no 'token' key in response.")
        else:
            logger.warning(f"Failed to obtain Jumpserver connection token. Status: {response.status_code}, Body: {response.text}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error requesting Jumpserver connection token: {e}")

    # Step 2: Prepare for MCP WebSocket session
    mcp_session_id = str(uuid.uuid4())
    
    parsed_url = urlparse(JUMPSERVER_URL)
    jumpserver_hostname = parsed_url.hostname
    if not jumpserver_hostname:
        logger.error(f"Could not parse hostname from JUMPSERVER_URL: {JUMPSERVER_URL}")
        raise HTTPException(status_code=500, detail="Could not parse Jumpserver hostname")

    active_sessions[mcp_session_id] = {
        "server_id": server_id,
        "system_user_id": connect_request.system_user_id,
        "connection_type": connect_request.connection_type,
        "jumpserver_hostname": jumpserver_hostname,
        "jms_connection_token": jms_connection_token,
    }
    logger.info(f"Created MCP session {mcp_session_id} for server {server_id}")

    # Use environment variables for host and port, or defaults
    # Ensure this reflects the address where this FastAPI app is accessible
    ws_url = f"ws://{MCP_WS_HOST}:{MCP_PORT}/mcp/ws/{mcp_session_id}"
    
    return ConnectResponse(mcp_session_id=mcp_session_id, ws_url=ws_url)


async def claude_to_jumpserver(claude_ws: WebSocket, jumpserver_ws, mcp_session_id: str):
    try:
        while True:
            data = await claude_ws.receive_text()
            logger.info(f"[{mcp_session_id}] C->J: {data[:100]}") # Log first 100 chars
            await jumpserver_ws.send(data)
    except WebSocketDisconnect:
        logger.info(f"[{mcp_session_id}] Claude disconnected.")
    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"[{mcp_session_id}] Jumpserver connection closed by Jumpserver (claude_to_jumpserver): {e}")
    except Exception as e:
        logger.error(f"[{mcp_session_id}] Error in claude_to_jumpserver: {e}")
    finally:
        if not jumpserver_ws.closed:
            await jumpserver_ws.close()
        logger.info(f"[{mcp_session_id}] claude_to_jumpserver task finished.")


async def jumpserver_to_claude(claude_ws: WebSocket, jumpserver_ws, mcp_session_id: str):
    try:
        while True:
            data = await jumpserver_ws.recv()
            logger.info(f"[{mcp_session_id}] J->C: {data[:100] if isinstance(data, str) else data[:10]}") # Log first 100 chars or 10 bytes
            if isinstance(data, str):
                await claude_ws.send_text(data)
            elif isinstance(data, bytes):
                await claude_ws.send_bytes(data) # Should not happen with Koko as it uses text
    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"[{mcp_session_id}] Jumpserver connection closed by Jumpserver (jumpserver_to_claude): {e}")
    except WebSocketDisconnect: # Should not happen on this side, but good to handle
        logger.info(f"[{mcp_session_id}] Claude disconnected (jumpserver_to_claude).")
    except Exception as e:
        logger.error(f"[{mcp_session_id}] Error in jumpserver_to_claude: {e}")
    finally:
        if not claude_ws.client_state == fastapi.websockets.WebSocketState.DISCONNECTED:
             try:
                await claude_ws.close()
             except RuntimeError as e: # Can happen if already closed
                logger.warning(f"[{mcp_session_id}] Error closing Claude WS (already closed?): {e}")
        logger.info(f"[{mcp_session_id}] jumpserver_to_claude task finished.")


@app.websocket("/mcp/ws/{mcp_session_id}")
async def websocket_endpoint(websocket: WebSocket, mcp_session_id: str):
    await websocket.accept()
    logger.info(f"Accepted WebSocket connection for MCP session: {mcp_session_id}")

    session_details = active_sessions.get(mcp_session_id)
    if not session_details:
        logger.warning(f"MCP session {mcp_session_id} not found. Closing WebSocket.")
        await websocket.close(code=1008) # Policy Violation or similar
        return

    server_id = session_details["server_id"]
    system_user_id = session_details["system_user_id"]
    connection_type = session_details["connection_type"]
    jumpserver_hostname = session_details["jumpserver_hostname"]
    jms_connection_token = session_details["jms_connection_token"] # May be None

    # Construct Jumpserver WebSocket URL
    # Example: wss://{jumpserver_hostname}/koko/ws/terminal/?target_id={server_id}&type={connection_type}&system_user_id={system_user_id}&_={int(time.time() * 1000)}
    # The `_` parameter is a cache buster, usually current timestamp in ms
    timestamp_ms = int(time.time() * 1000)
    target_ws_url = (
        f"wss://{jumpserver_hostname}/koko/ws/terminal/"
        f"?target_id={server_id}&type={connection_type}"
        f"&system_user_id={system_user_id}&_={timestamp_ms}"
    )

    # Decide whether to add the jms_connection_token as a query parameter
    # For now, we will not add it, as per instruction "initially, try connecting without adding this token"
    # If it were to be added, it might look like:
    # if jms_connection_token:
    #     target_ws_url += f"&token={jms_connection_token}" # The parameter name 'token' is a guess
    #     logger.info(f"[{mcp_session_id}] Attempting to use jms_connection_token in Jumpserver WebSocket URL.")
    # else:
    #     logger.info(f"[{mcp_session_id}] No jms_connection_token available for Jumpserver WebSocket URL.")
    logger.info(f"[{mcp_session_id}] Not adding jms_connection_token to Jumpserver WebSocket URL as per initial instructions.")


    logger.info(f"[{mcp_session_id}] Connecting to Jumpserver WebSocket: {target_ws_url}")
    jumpserver_ws = None
    try:
        # Connect to Jumpserver WebSocket
        # Note: websockets.connect() is an async context manager
        async with websockets.connect(
            target_ws_url, 
            subprotocols=["JMS-KOKO"]
        ) as js_ws:
            jumpserver_ws = js_ws # Assign to outer scope for finally block
            logger.info(f"[{mcp_session_id}] Successfully connected to Jumpserver WebSocket.")
            
            # Start forwarding tasks
            task1 = asyncio.create_task(claude_to_jumpserver(websocket, jumpserver_ws, mcp_session_id))
            task2 = asyncio.create_task(jumpserver_to_claude(websocket, jumpserver_ws, mcp_session_id))
            
            # Wait for either task to complete
            # Using asyncio.wait for more control over when tasks finish
            done, pending = await asyncio.wait(
                [task1, task2], 
                return_when=asyncio.FIRST_COMPLETED
            )

            for task in pending:
                task.cancel() # Cancel the other task
            
            logger.info(f"[{mcp_session_id}] One of the forwarding tasks completed. Shutting down connection.")

    except websockets.exceptions.InvalidStatusCode as e:
        logger.error(f"[{mcp_session_id}] Jumpserver WebSocket connection failed (InvalidStatusCode): {e}. Status: {e.status_code}, Headers: {e.headers}")
        await websocket.close(code=1011) # Internal error
    except websockets.exceptions.WebSocketException as e:
        logger.error(f"[{mcp_session_id}] Jumpserver WebSocket connection failed (WebSocketException): {e}")
        await websocket.close(code=1011)
    except ConnectionRefusedError as e:
        logger.error(f"[{mcp_session_id}] Jumpserver WebSocket connection failed (ConnectionRefusedError): {e}")
        await websocket.close(code=1011)
    except Exception as e:
        logger.error(f"[{mcp_session_id}] An unexpected error occurred during Jumpserver WebSocket setup or proxying: {e}")
        if not websocket.client_state == fastapi.websockets.WebSocketState.DISCONNECTED:
            await websocket.close(code=1011)
    finally:
        logger.info(f"[{mcp_session_id}] Cleaning up WebSocket connection and session.")
        if jumpserver_ws and not jumpserver_ws.closed:
            await jumpserver_ws.close()
        if not websocket.client_state == fastapi.websockets.WebSocketState.DISCONNECTED:
            # This might try to close an already closed connection if error originated from client
            try:
                await websocket.close(code=1000)
            except RuntimeError as e:
                logger.warning(f"[{mcp_session_id}] Error closing Claude WS (already closed?): {e}")

        if mcp_session_id in active_sessions:
            del active_sessions[mcp_session_id]
            logger.info(f"[{mcp_session_id}] Removed session from active_sessions.")
        logger.info(f"[{mcp_session_id}] WebSocket endpoint processing finished.")


if __name__ == "__main__":
    if not JUMPSERVER_URL or not JUMPSERVER_API_TOKEN:
        logger.error("Error: JUMPSERVER_URL and JUMPSERVER_API_TOKEN environment variables must be set.")
        print("Error: JUMPSERVER_URL and JUMPSERVER_API_TOKEN environment variables must be set.")
        print("Example usage:")
        print("  export JUMPSERVER_URL=\"http://your-jumpserver-url\"")
        print("  export JUMPSERVER_API_TOKEN=\"your-api-token\"")
        print("  export JUMPSERVER_ORG_ID=\"your-org-id\"  # Optional, defaults to ...0002")
        print("  export MCP_WS_HOST=\"localhost\" # Optional, host for MCP's WS URL, defaults to localhost")
        print("  export MCP_PORT=\"8000\"      # Optional, port for MCP's HTTP/WS URL, defaults to 8000")
        print("\nThen run the application:")
        # The host for uvicorn should be 0.0.0.0 to be accessible, MCP_WS_HOST is for constructing the WS URL
        uvicorn_host = "0.0.0.0" 
        print(f"  uvicorn main:app --reload --host {uvicorn_host} --port {MCP_PORT}")
    else:
        uvicorn_host = "0.0.0.0" # Listen on all interfaces
        logger.info(f"Starting Uvicorn server on {uvicorn_host}:{MCP_PORT}")
        # Uvicorn uses MCP_PORT for its HTTP server, which also serves WebSockets on the same port
        uvicorn.run(app, host=uvicorn_host, port=MCP_PORT)
