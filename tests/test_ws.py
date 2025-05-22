import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import WebSocketDisconnect as FastAPIWebSocketDisconnect
from fastapi.testclient import TestClient
import websockets # For exception types and WebSocketState enum

# Import objects from main.py
from main import app, active_sessions, claude_to_jumpserver, jumpserver_to_claude
import uuid # For generating session IDs

# Fixture for TestClient (can be shared if tests are in the same directory structure)
@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c

# Fixture to manage active_sessions, ensuring it's clean for each test
@pytest.fixture(autouse=True)
def clear_active_sessions_fixture():
    original_sessions = dict(active_sessions)
    active_sessions.clear()
    yield
    active_sessions.clear()
    active_sessions.update(original_sessions)

# Fixture to consistently set and tear down app.state for Jumpserver URL and token
# This patches the module-level config variables in main.py
@pytest.fixture
def mock_app_config(monkeypatch):
    test_jumpserver_url = "http://test-jumpserver-fixture.com"
    test_jumpserver_token = "fixture_token"
    test_mcp_ws_host = "fixture_mcp_host"
    test_mcp_port = 5678
    test_jumpserver_org_id = "fixture_org_id"

    monkeypatch.setattr('main.JUMPSERVER_URL', test_jumpserver_url)
    monkeypatch.setattr('main.JUMPSERVER_API_TOKEN', test_jumpserver_token)
    monkeypatch.setattr('main.JUMPSERVER_ORG_ID', test_jumpserver_org_id)
    monkeypatch.setattr('main.MCP_WS_HOST', test_mcp_ws_host)
    monkeypatch.setattr('main.MCP_PORT', test_mcp_port)
    
    yield {
        "jumpserver_url": test_jumpserver_url,
        "jumpserver_token": test_jumpserver_token,
        "mcp_ws_host": test_mcp_ws_host,
        "mcp_port": test_mcp_port,
        "jumpserver_hostname": "test-jumpserver-fixture.com" # Parsed from URL
    }

# --- Tests for WebSocket endpoint (/mcp/ws/{mcp_session_id}) ---

@pytest.mark.asyncio
async def test_websocket_endpoint_invalid_mcp_session_id(client, mock_app_config):
    invalid_session_id = str(uuid.uuid4())
    try:
        # TestClient's websocket_connect is synchronous in how it's called, but interacts with async app
        with client.websocket_connect(f"/mcp/ws/{invalid_session_id}", subprotocols=["JMS-KOKO"]) as websocket:
            # This block should ideally not be entered if server closes connection immediately.
            # However, TestClient might establish connection before server logic fully executes and closes.
            # Attempting a receive operation will reveal the closure.
            websocket.receive_text(timeout=0.5) # Expecting this to fail due to closure
            pytest.fail("WebSocket stayed open with invalid session ID")
    except FastAPIWebSocketDisconnect as e:
        assert e.code == 1008 # Policy Violation, as defined in main.py
    except websockets.exceptions.InvalidStatusCode as e:
        # This can happen if the server rejects the subprotocol or other handshake issues
        # For this specific test, we expect 1008 from our app logic.
        pytest.fail(f"Unexpected InvalidStatusCode: {e}. Expected clean close with 1008.")


@pytest.mark.asyncio
@patch('websockets.connect') # Patch the actual websockets.connect used by main.py
async def test_websocket_endpoint_jumpserver_connection_failure_invalid_status(
    mock_websockets_connect, client, mock_app_config
):
    mcp_session_id = str(uuid.uuid4())
    active_sessions[mcp_session_id] = {
        "server_id": "test_server_id",
        "system_user_id": "test_sys_user_id",
        "connection_type": "ssh",
        "jumpserver_hostname": mock_app_config["jumpserver_hostname"],
        "jms_connection_token": None,
    }

    mock_js_ws_context = AsyncMock()
    mock_js_ws_context.__aenter__.side_effect = websockets.exceptions.InvalidStatusCode(503, headers={}) # e.g., Service Unavailable
    mock_websockets_connect.return_value = mock_js_ws_context

    try:
        with client.websocket_connect(f"/mcp/ws/{mcp_session_id}", subprotocols=["JMS-KOKO"]) as websocket:
            websocket.receive_text(timeout=1) # Expecting failure
            pytest.fail("WebSocket stayed open despite Jumpserver connection failure.")
    except FastAPIWebSocketDisconnect as e:
        assert e.code == 1011 # Internal error, as defined in main.py's websocket_endpoint
    except TimeoutError: # If receive_text times out
        pytest.fail("WebSocket receive timed out. Expected disconnect due to Jumpserver connection failure.")
    finally:
        # Ensure session is cleaned up for future tests if something went wrong
        if mcp_session_id in active_sessions:
            del active_sessions[mcp_session_id]


@pytest.mark.asyncio
@patch('websockets.connect')
async def test_websocket_endpoint_successful_connection_and_basic_proxy(
    mock_websockets_connect, client, mock_app_config
):
    mcp_session_id = str(uuid.uuid4())
    active_sessions[mcp_session_id] = {
        "server_id": "test_server_id_proxy",
        "system_user_id": "test_sys_user_id_proxy",
        "connection_type": "ssh",
        "jumpserver_hostname": mock_app_config["jumpserver_hostname"],
        "jms_connection_token": None,
    }

    mock_js_ws = AsyncMock(spec=websockets.client.WebSocketClientProtocol)
    # To make loops in forwarding coroutines break after one message:
    mock_js_ws.recv.side_effect = ["data_from_jumpserver", asyncio.CancelledError()]
    mock_js_ws.closed = False 

    mock_js_ws_context = AsyncMock()
    mock_js_ws_context.__aenter__.return_value = mock_js_ws
    mock_js_ws_context.__aexit__.return_value = None # Simulate clean exit
    mock_websockets_connect.return_value = mock_js_ws_context

    try:
        with client.websocket_connect(f"/mcp/ws/{mcp_session_id}", subprotocols=["JMS-KOKO"]) as claude_ws:
            # Mimic Claude sending data
            claude_ws.send_text("data_from_claude")
            
            # Expect to receive proxied data from Jumpserver
            received_by_claude = claude_ws.receive_text(timeout=1)
            assert received_by_claude == "data_from_jumpserver"
            
            # Allow time for the claude_to_jumpserver task to process
            await asyncio.sleep(0.05) 
            mock_js_ws.send.assert_called_with("data_from_claude")
            
            # Claude client closes the connection (by exiting the 'with' block)
            # This will trigger WebSocketDisconnect in `claude_to_jumpserver`
            # which should then close `jumpserver_ws_mock`.
    except TimeoutError:
        pytest.fail("WebSocket operation timed out during proxy test.")
    except FastAPIWebSocketDisconnect:
        # This is an expected outcome when the client-side 'with' block exits
        pass 
    finally:
        await asyncio.sleep(0.05) # Allow tasks to complete their cleanup
        mock_js_ws.close.assert_called_once() # Check Jumpserver WS was closed
        if mcp_session_id in active_sessions:
            del active_sessions[mcp_session_id]


# --- Tests for forwarding coroutines directly ---

@pytest.mark.asyncio
async def test_claude_to_jumpserver_coroutine():
    # Mock for FastAPI's WebSocket
    claude_ws_mock = AsyncMock(spec=FastAPIWebSocketDisconnect) # Using as a stand-in for FastAPI WebSocket type
    claude_ws_mock.receive_text.side_effect = ["test_msg1", "test_msg2", FastAPIWebSocketDisconnect(code=1000)]
    
    # Mock for websockets library's WebSocketClientProtocol
    jumpserver_ws_mock = AsyncMock(spec=websockets.client.WebSocketClientProtocol)
    jumpserver_ws_mock.closed = False # Simulate it's initially open

    await claude_to_jumpserver(claude_ws_mock, jumpserver_ws_mock, "test_session_id_c2j")
    
    assert jumpserver_ws_mock.send.call_count == 2
    jumpserver_ws_mock.send.assert_any_call("test_msg1")
    jumpserver_ws_mock.send.assert_any_call("test_msg2")
    jumpserver_ws_mock.close.assert_called_once()


@pytest.mark.asyncio
async def test_jumpserver_to_claude_coroutine():
    claude_ws_mock = AsyncMock(spec=FastAPIWebSocketDisconnect)
    # Mock client_state for FastAPI WebSocket, assuming it's checked before close
    claude_ws_mock.client_state = MagicMock()
    claude_ws_mock.client_state.value = websockets.WebSocketState.OPEN.value # Simulate open initially

    jumpserver_ws_mock = AsyncMock(spec=websockets.client.WebSocketClientProtocol)
    jumpserver_ws_mock.recv.side_effect = [
        "jumpserver_msg1", 
        "jumpserver_msg2", 
        websockets.exceptions.ConnectionClosedOK(rcvd=None, sent=None) # Jumpserver closes
    ]
    jumpserver_ws_mock.closed = False # Will be True after ConnectionClosedOK is processed by library

    await jumpserver_to_claude(claude_ws_mock, jumpserver_ws_mock, "test_session_id_j2c")
    
    assert claude_ws_mock.send_text.call_count == 2
    claude_ws_mock.send_text.assert_any_call("jumpserver_msg1")
    claude_ws_mock.send_text.assert_any_call("jumpserver_msg2")
    
    # Since Jumpserver connection closed, Claude's WebSocket should be closed by the coroutine's finally block
    claude_ws_mock.close.assert_called_once()


@pytest.mark.asyncio
async def test_claude_to_jumpserver_when_jumpserver_send_fails():
    claude_ws_mock = AsyncMock(spec=FastAPIWebSocketDisconnect)
    claude_ws_mock.receive_text.side_effect = ["test_msg1", FastAPIWebSocketDisconnect(code=1000)] # Claude sends one msg then disconnects
    
    jumpserver_ws_mock = AsyncMock(spec=websockets.client.WebSocketClientProtocol)
    jumpserver_ws_mock.send.side_effect = websockets.exceptions.ConnectionClosedError(rcvd=None, sent=None) # Sending to Jumpserver fails
    jumpserver_ws_mock.closed = True # Reflects that it's already closed or effectively so

    # We expect the coroutine to catch ConnectionClosedError and log, then try to close jumpserver_ws
    # The WebSocketDisconnect from claude_ws.receive_text will eventually stop the loop.
    await claude_to_jumpserver(claude_ws_mock, jumpserver_ws_mock, "test_c2j_js_send_fail")

    jumpserver_ws_mock.send.assert_called_once_with("test_msg1")
    # close() is called in finally, even if send fails or it's already closed
    jumpserver_ws_mock.close.assert_called_once()


@pytest.mark.asyncio
async def test_jumpserver_to_claude_when_claude_send_fails():
    claude_ws_mock = AsyncMock(spec=FastAPIWebSocketDisconnect)
    claude_ws_mock.send_text.side_effect = FastAPIWebSocketDisconnect(code=1001) # Sending to Claude fails
    # Mock client_state for the finally block check
    claude_ws_mock.client_state = MagicMock() 
    # It will be OPEN when send_text is called, then the disconnect exception is raised.
    # The finally block in jumpserver_to_claude will see it as OPEN unless the exception itself changes the mock's state.
    # For this test, we assume it remains OPEN from the mock's perspective until close() is called or state is changed.
    claude_ws_mock.client_state.value = websockets.WebSocketState.OPEN.value

    jumpserver_ws_mock = AsyncMock(spec=websockets.client.WebSocketClientProtocol)
    jumpserver_ws_mock.recv.side_effect = ["jumpserver_msg1", websockets.exceptions.ConnectionClosedOK(None, None)]
    jumpserver_ws_mock.closed = False

    # The FastAPIWebSocketDisconnect will propagate from send_text
    with pytest.raises(FastAPIWebSocketDisconnect):
        await jumpserver_to_claude(claude_ws_mock, jumpserver_ws_mock, "test_j2c_claude_send_fail")

    jumpserver_ws_mock.recv.assert_called_once() # Should attempt to receive once
    claude_ws_mock.send_text.assert_called_once_with("jumpserver_msg1")
    
    # The finally block in jumpserver_to_claude will be executed.
    # It will attempt to close claude_ws if state is not DISCONNECTED.
    # Since send_text raised FastAPIWebSocketDisconnect, the state might not reflect DISCONNECTED
    # on the mock unless explicitly set. The close() should be called.
    claude_ws_mock.close.assert_called_once()
```

The file `tests/test_main.py` now contains tests for HTTP endpoints using the `mock_app_config` fixture for cleaner configuration management.

The file `tests/test_ws.py` has been created and contains:
1.  Tests for the `websocket_endpoint` handler:
    *   `test_websocket_endpoint_invalid_mcp_session_id`: Checks if the server closes the WebSocket with code 1008 for an unknown session ID.
    *   `test_websocket_endpoint_jumpserver_connection_failure_invalid_status`: Mocks `websockets.connect` to raise `InvalidStatusCode` (simulating Jumpserver being down or returning an error). Asserts that the client's WebSocket is closed with code 1011.
    *   `test_websocket_endpoint_successful_connection_and_basic_proxy`: Mocks a successful connection to Jumpserver and tests a simple bidirectional message flow (client sends one message, Jumpserver sends one message). It verifies that the messages are proxied correctly and that the Jumpserver WebSocket is closed upon client disconnection.

2.  Direct tests for the forwarding coroutines (`claude_to_jumpserver` and `jumpserver_to_claude`):
    *   `test_claude_to_jumpserver_coroutine`: Simulates Claude sending messages and then disconnecting. Verifies messages are sent to the Jumpserver mock and Jumpserver mock is closed.
    *   `test_jumpserver_to_claude_coroutine`: Simulates Jumpserver sending messages and then its connection closing. Verifies messages are sent to the Claude mock and Claude mock is closed.
    *   `test_claude_to_jumpserver_when_jumpserver_send_fails`: Tests scenario where sending to Jumpserver fails (e.g., Jumpserver connection drops).
    *   `test_jumpserver_to_claude_when_claude_send_fails`: Tests scenario where sending to Claude fails (e.g., Claude client disconnects abruptly).

These tests cover various scenarios including successful operations, error conditions, and disconnections for both the HTTP endpoints and WebSocket functionalities.
The use of `autouse` fixture for `active_sessions` ensures it's reset for each test. The `mock_app_config` fixture standardizes the patching of module-level configuration variables in `main.py`.

The next step is to update `README.md`.
