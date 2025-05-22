# Jumpserver MCP FastAPI Application

This application provides a FastAPI interface to interact with Jumpserver, specifically to retrieve a list of servers and proxy WebSocket connections for terminal sessions, in a format compatible with a hypothetical MCP (Master Control Program).

## Prerequisites

- Python 3.7+
- Jumpserver instance with API access
- Node.js and npm (for `wscat` testing utility, optional)

## Setup

1.  **Clone the repository (if applicable) or ensure you have the `main.py` file with the application code.**

2.  **Create and activate a virtual environment:**

    It's highly recommended to use a virtual environment to manage project dependencies.

    ```bash
    python3 -m venv venv
    source venv/bin/activate 
    # On Windows, use: venv\Scripts\activate
    ```

3.  **Install dependencies:**

    For application:
    ```bash
    pip install fastapi uvicorn requests websockets
    ```
    For running tests:
    ```bash
    pip install pytest pytest-asyncio respx
    ```

4.  **Set environment variables:**

    Before running the application, you need to set the following environment variables. These are used to connect to your Jumpserver instance and configure the MCP application itself.

    ```bash
    # --- Jumpserver Connection ---
    # Replace with your Jumpserver URL (e.g., http://jumpserver.example.com)
    export JUMPSERVER_URL="http://your-jumpserver-url"
    # Replace with your Jumpserver API token
    export JUMPSERVER_API_TOKEN="your-api-token"      
    # Optional: Set JUMPSERVER_ORG_ID if you need to target a specific organization.
    # If not set, it defaults to "00000000-0000-0000-0000-000000000002".
    # export JUMPSERVER_ORG_ID="your-specific-org-id"

    # --- MCP Application Configuration ---
    # Hostname that this MCP application will use to construct the WebSocket URL it returns.
    # This should be the address clients (like Claude) will use to connect to this application's WebSocket.
    export MCP_WS_HOST="localhost" 
    # Port on which this MCP application (Uvicorn server) will listen.
    # This port is used for both HTTP API endpoints and WebSocket connections.
    export MCP_PORT="8000"         
    ```
    Replace placeholder values with your actual Jumpserver details.

## Running the Application

Once the setup is complete and environment variables are set, you can run the FastAPI application using Uvicorn. The application will serve HTTP traffic for API endpoints and WebSocket traffic for terminal sessions.

```bash
# Uvicorn will listen on 0.0.0.0 (all interfaces) and the port specified by MCP_PORT (default 8000)
uvicorn main:app --reload --host 0.0.0.0 --port ${MCP_PORT:-8000}
```

-   `main:app`: Tells Uvicorn to find the `app` object (our FastAPI instance) in the `main.py` file.
-   `--reload`: Enables auto-reloading when code changes. This is useful for development. Remove it for production.
-   `--host 0.0.0.0`: Makes the server accessible from other machines on your network.
-   `--port ${MCP_PORT:-8000}`: Runs the server on the port defined by the `MCP_PORT` environment variable, defaulting to 8000 if not set.

The HTTP API will be available at `http://<your-machine-ip>:${MCP_PORT}`.
The WebSocket URL returned by the `/connect` endpoint will be constructed using `MCP_WS_HOST` and `MCP_PORT` (e.g., `ws://${MCP_WS_HOST}:${MCP_PORT}/mcp/ws/{mcp_session_id}`).

## Testing the Endpoints

### Get Servers (`GET /mcp/api/v1/servers`)

This endpoint retrieves a list of servers from Jumpserver.

**Using `curl`:**

```bash
curl -X GET "http://${MCP_WS_HOST:-localhost}:${MCP_PORT:-8000}/mcp/api/v1/servers"
```

**Expected Success Response (Example):**
```json
{
    "servers": [
        {
            "id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxx1",
            "hostname": "server1.example.com",
            "ip": "192.168.1.10",
            "platform": "Linux",
            "os": "Ubuntu 20.04 LTS",
            "comment": "Main web server"
        }
        // ... more servers
    ]
}
```

### Connect to Server (`POST /mcp/api/v1/servers/{server_id}/connect`)

This endpoint initiates a connection to a specific server by creating a session and providing a WebSocket URL for terminal communication.

**Request Path Parameter:**
-   `server_id`: The ID of the server to connect to (obtained from the `/servers` endpoint).

**Request Body (JSON):**
```json
{
    "system_user_id": "your_system_user_id_on_jumpserver",
    "connection_type": "ssh" 
}
```
-   `system_user_id`: The ID of the system user (configured in Jumpserver for the asset) to use for the connection.
-   `connection_type` (optional): Defaults to "ssh". Can be other types supported by Jumpserver's Koko terminal (e.g., "rdp", "telnet", "vnc").

**Using `curl`:**

Replace `{server_id}` with an actual server ID and adjust the `system_user_id`. The URL for `curl` should use the host and port where Uvicorn is running (e.g., `localhost:8000` if `MCP_PORT=8000`).

```bash
curl -X POST "http://${MCP_WS_HOST:-localhost}:${MCP_PORT:-8000}/mcp/api/v1/servers/{server_id}/connect" \
     -H "Content-Type: application/json" \
     -d '{
           "system_user_id": "your_system_user_id",
           "connection_type": "ssh"
         }'
```

**Expected Success Response (Example):**

The `ws_url` will use the `MCP_WS_HOST` and `MCP_PORT` values.
```json
{
    "mcp_session_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "ws_url": "ws://localhost:8000/mcp/ws/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" 
}
```
-   `mcp_session_id`: A unique ID for this connection session.
-   `ws_url`: The WebSocket URL to connect to for the terminal session. This URL points to this MCP application's WebSocket endpoint.

### WebSocket Terminal (`/mcp/ws/{mcp_session_id}`)

This is the WebSocket endpoint that proxies communication to Jumpserver's Koko terminal. You connect to the `ws_url` returned by the `/connect` endpoint.

**Testing with a command-line WebSocket client (e.g., `wscat`):**

First, install `wscat` if you don't have it (requires Node.js/npm):
```bash
npm install -g wscat
```

Then, use the `ws_url` from the `/connect` response:
```bash
# Replace <ws_url_from_connect_response> with the actual ws_url
wscat -c "<ws_url_from_connect_response>" --subprotocol JMS-KOKO
```
- The `--subprotocol JMS-KOKO` argument is crucial as the Jumpserver Koko service (and our proxy) expects this subprotocol.

Once connected, if the proxy successfully connects to Jumpserver's Koko WebSocket, you should see the Jumpserver terminal prompt or connection messages. Type commands as you would in an SSH session.

**Alternatively, using a simple Python WebSocket client script:**

Create a file `ws_client.py`:
```python
import asyncio
import websockets
import sys
import os

async def connect_mcp_ws(uri):
    try:
        # Ensure the subprotocol is passed correctly
        async with websockets.connect(uri, subprotocols=["JMS-KOKO"]) as websocket:
            print(f"Successfully connected to {uri} with subprotocol JMS-KOKO")
            print("Type your commands and press Enter. Ctrl+D to close input.")
            
            async def send_input():
                while True:
                    try:
                        # Use asyncio.to_thread for blocking stdin readline
                        message = await asyncio.to_thread(sys.stdin.readline)
                        if not message: # Handle EOF (Ctrl+D)
                            print("Input EOF, closing client send task...")
                            # Optionally send a close signal if protocol requires
                            # await websocket.close() # Let server close or keep open as per protocol
                            break
                        await websocket.send(message) 
                    except websockets.exceptions.ConnectionClosedOK:
                        print("Connection closed gracefully by server (send_input).")
                        break
                    except websockets.exceptions.ConnectionClosedError as e:
                        print(f"Connection closed with error by server (send_input): {e}")
                        break
                    except Exception as e:
                        print(f"Error sending: {e}")
                        break
                print("Send input task finished.")


            async def receive_output():
                try:
                    while True:
                        response = await websocket.recv()
                        # Print raw output from server, sys.stdout handles encoding
                        sys.stdout.write(str(response))
                        sys.stdout.flush()
                except websockets.exceptions.ConnectionClosedOK:
                    sys.stdout.write("\nConnection closed gracefully by server (receive_output).\n")
                    sys.stdout.flush()
                except websockets.exceptions.ConnectionClosedError as e:
                    sys.stdout.write(f"\nConnection closed with error by server (receive_output): {e}\n")
                    sys.stdout.flush()
                except Exception as e:
                    sys.stdout.write(f"Error receiving: {e}\n")
                    sys.stdout.flush()
                print("Receive output task finished.")

            send_task = asyncio.create_task(send_input())
            receive_task = asyncio.create_task(receive_output())

            # Wait for either task to complete, then cancel the other
            done, pending = await asyncio.wait(
                [send_task, receive_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            
            # Ensure all tasks are awaited to prevent warnings and allow cleanup
            await asyncio.gather(*done, *pending, return_exceptions=True)
            print("All tasks finished.")

    except websockets.exceptions.InvalidStatusCode as e:
        print(f"Failed to connect: Invalid status code {e.status_code}. Is the WebSocket URL correct and server running?")
    except ConnectionRefusedError:
        print(f"Failed to connect: Connection refused. Is the server running at {uri}?")
    except Exception as e:
        print(f"Failed to connect or error during session: {type(e).__name__} - {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python ws_client.py <ws_url>")
        print("Example: python ws_client.py ws://localhost:8000/mcp/ws/your_session_id")
        sys.exit(1)
    
    # Basic TTY settings for more interactive session if on Unix-like system
    # This is a very simplified way; proper terminal emulation is complex.
    if os.name == 'posix':
        try:
            import tty, termios
            old_settings = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
        except (ImportError, termios.error, AttributeError) as e:
            print(f"Could not set TTY to raw mode: {e}. Input might behave unexpectedly.")
            old_settings = None # Ensure it's defined
    
    ws_server_url = sys.argv[1]
    try:
        asyncio.run(connect_mcp_ws(ws_server_url))
    finally:
        if os.name == 'posix' and 'old_settings' in locals() and old_settings is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
            print("\nRestored TTY settings.")
```

Run the client, replacing `<ws_url_from_connect_response>`:
```bash
python ws_client.py "<ws_url_from_connect_response>"
```
This script will connect to the WebSocket, attempt to set the terminal to raw mode (on Unix-like systems), send your terminal input (line by line or char by char depending on terminal mode), and print server output.
Check the application log (`uvicorn` output) for detailed logs of connection attempts to Jumpserver and data forwarding. These logs are crucial for troubleshooting.

**Key points for testing:**
- Ensure `JUMPSERVER_URL` points to your Jumpserver's web interface. The WebSocket connection will be proxied to its `/koko/ws/terminal/` endpoint.
- The `system_user_id` must be a valid system user ID associated with the asset in Jumpserver that you are trying to connect to.
- The `MCP_WS_HOST` and `MCP_PORT` environment variables control the `ws_url` returned by the `/connect` endpoint. `MCP_WS_HOST` must be reachable by the client that intends to use the WebSocket (e.g., your machine running `wscat` or the Python script).
- Pay close attention to the logs from the MCP application (Uvicorn) and any error messages from the client.

## Running Tests

Unit and integration tests are located in the `tests/` directory. Ensure you have the testing dependencies installed (see step 3 in Setup).

To run all tests:
```bash
python -m pytest tests/
```
Or simply:
```bash
pytest tests/
```

The tests cover:
- API endpoints (`/mcp/api/v1/servers`, `/mcp/api/v1/servers/{server_id}/connect`) with various scenarios including success and Jumpserver API failures (mocked using `respx`).
- WebSocket connection logic (`/mcp/ws/{mcp_session_id}`), including invalid session IDs and Jumpserver connection failures (mocked using `unittest.mock.patch` for `websockets.connect`).
- Direct testing of WebSocket message forwarding coroutines (`claude_to_jumpserver`, `jumpserver_to_claude`) for different disconnection and error cases.
```
