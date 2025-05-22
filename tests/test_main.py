import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock
import respx
import uuid
import websockets # Though not directly used in TestClient HTTP tests, good for context
import asyncio
import requests # For requests.exceptions

# Import objects from main.py
# Assuming main.py is in the parent directory or PYTHONPATH is set up correctly
# For this environment, we'll assume it's accessible
from main import app, active_sessions, McpServerList, ConnectResponse

# Fixture for TestClient
@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c

# Fixture to manage active_sessions
@pytest.fixture(autouse=True)
def clear_active_sessions_fixture():
    # Before each test
    original_sessions = dict(active_sessions)
    active_sessions.clear()
    yield
    # After each test
    active_sessions.clear()
    active_sessions.update(original_sessions)


# --- Tests for GET /mcp/api/v1/servers ---

@pytest.mark.asyncio
async def test_get_servers_success(client, respx_mock):
    jumpserver_url = "http://test-jumpserver.com"
    app.state.JUMPSERVER_URL = jumpserver_url # Override for test
    app.state.JUMPSERVER_API_TOKEN = "test_token"

    mock_asset_data = [
        {"id": "id1", "name": "server1", "ip": "1.1.1.1", "platform": {"name": "Linux"}, "os": "Ubuntu", "comment": "c1"},
        {"id": "id2", "hostname": "server2", "address": "2.2.2.2", "platform": "Windows", "os": "Win2019", "description": "c2"},
        {"id": "id3", "name": "server3", "ip": "3.3.3.3", "platform": "UnknownPlatform", "os": "UnknownOS", "comment": "c3", "extra_field": "should_be_ignored"}
    ]
    
    respx_mock.get(f"{jumpserver_url}/api/v1/assets/assets/").mock(return_value=requests.Response(200, json=mock_asset_data))

    response = client.get("/mcp/api/v1/servers")

    assert response.status_code == 200
    data = response.json()
    assert "servers" in data
    assert len(data["servers"]) == 3
    
    # Check transformation
    assert data["servers"][0]["hostname"] == "server1"
    assert data["servers"][0]["platform"] == "Linux"
    assert data["servers"][0]["comment"] == "c1"

    assert data["servers"][1]["hostname"] == "server2" # from hostname
    assert data["servers"][1]["ip"] == "2.2.2.2" # from address
    assert data["servers"][1]["platform"] == "Windows" # from platform (str)
    assert data["servers"][1]["comment"] == "c2" # from description

    # Restore original or clear, if app state was used for overriding
    # TestClient context manager should handle app state isolation generally for FastAPI's own state
    # but direct modifications like app.state.JUMPSERVER_URL need cleanup if not using proper fixture scope for it
    del app.state.JUMPSERVER_URL
    del app.state.JUMPSERVER_API_TOKEN


@pytest.mark.asyncio
async def test_get_servers_jumpserver_api_error_500(client, respx_mock):
    jumpserver_url = "http://test-jumpserver.com"
    app.state.JUMPSERVER_URL = jumpserver_url
    app.state.JUMPSERVER_API_TOKEN = "test_token"

    respx_mock.get(f"{jumpserver_url}/api/v1/assets/assets/").mock(return_value=requests.Response(500, json={"error": "server error"}))

    response = client.get("/mcp/api/v1/servers")

    assert response.status_code == 500
    assert "Failed to connect to Jumpserver" in response.json()["detail"]
    
    del app.state.JUMPSERVER_URL
    del app.state.JUMPSERVER_API_TOKEN


@pytest.mark.asyncio
async def test_get_servers_jumpserver_api_error_401(client, respx_mock):
    jumpserver_url = "http://test-jumpserver.com"
    app.state.JUMPSERVER_URL = jumpserver_url
    app.state.JUMPSERVER_API_TOKEN = "test_token"

    respx_mock.get(f"{jumpserver_url}/api/v1/assets/assets/").mock(return_value=requests.Response(401, json={"detail": "Unauthorized"}))

    response = client.get("/mcp/api/v1/servers")

    assert response.status_code == 500 # Our app should return 500 as it's a dependency failure
    assert "Failed to connect to Jumpserver" in response.json()["detail"]
    assert "401" in response.json()["detail"] # Ensure original status code is mentioned

    del app.state.JUMPSERVER_URL
    del app.state.JUMPSERVER_API_TOKEN


@pytest.mark.asyncio
async def test_get_servers_jumpserver_request_exception(client, respx_mock):
    jumpserver_url = "http://test-jumpserver.com"
    app.state.JUMPSERVER_URL = jumpserver_url
    app.state.JUMPSERVER_API_TOKEN = "test_token"

    respx_mock.get(f"{jumpserver_url}/api/v1/assets/assets/").mock(side_effect=requests.exceptions.ConnectTimeout("Connection timed out"))

    response = client.get("/mcp/api/v1/servers")

    assert response.status_code == 500
    assert "Failed to connect to Jumpserver" in response.json()["detail"]
    assert "Connection timed out" in response.json()["detail"]

    del app.state.JUMPSERVER_URL
    del app.state.JUMPSERVER_API_TOKEN

# --- Tests for POST /mcp/api/v1/servers/{server_id}/connect ---

@pytest.mark.asyncio
async def test_connect_server_success_with_token(client, respx_mock):
    jumpserver_url = "http://test-jumpserver.com"
    app.state.JUMPSERVER_URL = jumpserver_url
    app.state.JUMPSERVER_API_TOKEN = "test_token"
    app.state.MCP_WS_HOST = "mcp_host"
    app.state.MCP_PORT = 1234

    server_id = str(uuid.uuid4())
    system_user_id = str(uuid.uuid4())
    expected_jms_token = "test_jms_token_value"

    respx_mock.get(f"{jumpserver_url}/api/v1/users/connection-token/?user-only=1").mock(
        return_value=requests.Response(200, json={"token": expected_jms_token})
    )

    response = client.post(
        f"/mcp/api/v1/servers/{server_id}/connect",
        json={"system_user_id": system_user_id, "connection_type": "ssh"}
    )

    assert response.status_code == 200
    data = response.json()
    assert "mcp_session_id" in data
    assert "ws_url" in data
    
    mcp_session_id = data["mcp_session_id"]
    assert mcp_session_id in active_sessions
    session_details = active_sessions[mcp_session_id]
    
    assert session_details["server_id"] == server_id
    assert session_details["system_user_id"] == system_user_id
    assert session_details["jms_connection_token"] == expected_jms_token
    assert session_details["jumpserver_hostname"] == "test-jumpserver.com"
    assert data["ws_url"] == f"ws://mcp_host:1234/mcp/ws/{mcp_session_id}"

    del app.state.JUMPSERVER_URL
    del app.state.JUMPSERVER_API_TOKEN
    del app.state.MCP_WS_HOST
    del app.state.MCP_PORT


@pytest.mark.asyncio
async def test_connect_server_token_api_ok_no_token_key(client, respx_mock):
    jumpserver_url = "http://test-jumpserver.com"
    app.state.JUMPSERVER_URL = jumpserver_url
    app.state.JUMPSERVER_API_TOKEN = "test_token"

    server_id = str(uuid.uuid4())
    system_user_id = str(uuid.uuid4())

    # Jumpserver returns 200 OK but the JSON does not contain the 'token' key
    respx_mock.get(f"{jumpserver_url}/api/v1/users/connection-token/?user-only=1").mock(
        return_value=requests.Response(200, json={"message": "No token available right now"})
    )

    response = client.post(
        f"/mcp/api/v1/servers/{server_id}/connect",
        json={"system_user_id": system_user_id} # connection_type defaults to ssh
    )

    assert response.status_code == 200
    data = response.json()
    mcp_session_id = data["mcp_session_id"]
    assert mcp_session_id in active_sessions
    session_details = active_sessions[mcp_session_id]
    assert session_details["jms_connection_token"] is None # Key assertion

    del app.state.JUMPSERVER_URL
    del app.state.JUMPSERVER_API_TOKEN


@pytest.mark.asyncio
async def test_connect_server_token_api_error(client, respx_mock):
    jumpserver_url = "http://test-jumpserver.com"
    app.state.JUMPSERVER_URL = jumpserver_url
    app.state.JUMPSERVER_API_TOKEN = "test_token"

    server_id = str(uuid.uuid4())
    system_user_id = str(uuid.uuid4())

    # Jumpserver token API returns an error (e.g., 500)
    respx_mock.get(f"{jumpserver_url}/api/v1/users/connection-token/?user-only=1").mock(
        return_value=requests.Response(500, json={"error": "Internal Server Error"})
    )

    response = client.post(
        f"/mcp/api/v1/servers/{server_id}/connect",
        json={"system_user_id": system_user_id}
    )

    assert response.status_code == 200 # The /connect endpoint itself should still succeed
    data = response.json()
    mcp_session_id = data["mcp_session_id"]
    assert mcp_session_id in active_sessions
    session_details = active_sessions[mcp_session_id]
    assert session_details["jms_connection_token"] is None # Key assertion

    del app.state.JUMPSERVER_URL
    del app.state.JUMPSERVER_API_TOKEN


@pytest.mark.asyncio
async def test_connect_server_jumpserver_url_not_set(client):
    # Ensure JUMPSERVER_URL is not in app.state for this test
    if hasattr(app.state, 'JUMPSERVER_URL'):
        del app.state.JUMPSERVER_URL
    
    # Temporarily remove from os.environ if it's there to simulate it not being set at startup
    # This is tricky because main.py reads os.getenv at import time.
    # For TestClient, it's better to rely on how FastAPI loads config or mock it at the source.
    # The current main.py structure loads JUMPSERVER_URL from os.getenv at module level.
    # A robust way would be to use FastAPI's dependency injection for config.
    # For now, we'll assume that if it's not in app.state (if we were using it), it implies not configured.
    # The current code in main.py directly uses the global JUMPSERVER_URL.
    # We'll patch the global JUMPSERVER_URL used by the endpoint.

    server_id = str(uuid.uuid4())
    system_user_id = str(uuid.uuid4())

    with patch('main.JUMPSERVER_URL', None): # Patch the global directly used by the endpoint
        response = client.post(
            f"/mcp/api/v1/servers/{server_id}/connect",
            json={"system_user_id": system_user_id}
        )
    
    assert response.status_code == 500
    assert "Jumpserver URL or API token not configured" in response.json()["detail"]


# Note: Testing WebSocket endpoints with TestClient is more involved
# and typically requires `client.websocket_connect()`.
# The prompt asks for testing websocket_endpoint handler and forwarding coroutines.
# These will be in a separate section or file as per instruction "Test WebSocket Logic (in tests/test_ws.py or tests/test_main.py)"
# For now, this file test_main.py focuses on HTTP endpoints.

# Placeholder for future WebSocket tests if included in this file
# @pytest.mark.asyncio
# async def test_websocket_placeholder():
#    assert True

# It's better to move WebSocket specific tests to test_ws.py if they become complex.
# The task was to create test_main.py. WebSocket tests will follow.
# For now, this file contains tests for the HTTP endpoints.
# The instruction "Test WebSocket Logic (in tests/test_ws.py or tests/test_main.py)"
# suggests flexibility. Given the complexity, a separate test_ws.py might be cleaner.
# However, if simpler direct coroutine tests are done, they could be here.
# Let's add the direct coroutine tests here as requested.

from main import claude_to_jumpserver, jumpserver_to_claude, WebSocketDisconnect

@pytest.mark.asyncio
async def test_claude_to_jumpserver_coroutine():
    claude_ws_mock = AsyncMock()
    jumpserver_ws_mock = AsyncMock()
    
    # Simulate Claude sending two messages then disconnecting
    claude_ws_mock.receive_text.side_effect = ["test_msg1", "test_msg2", WebSocketDisconnect()]
    
    await claude_to_jumpserver(claude_ws_mock, jumpserver_ws_mock, "test_session_id_c2j")
    
    # Assert jumpserver_ws.send was called with the messages
    assert jumpserver_ws_mock.send.call_count == 2
    jumpserver_ws_mock.send.assert_any_call("test_msg1")
    jumpserver_ws_mock.send.assert_any_call("test_msg2")
    
    # Assert Jumpserver WebSocket was closed (because Claude disconnected)
    jumpserver_ws_mock.close.assert_called_once()


@pytest.mark.asyncio
async def test_jumpserver_to_claude_coroutine():
    claude_ws_mock = AsyncMock()
    jumpserver_ws_mock = AsyncMock()
    
    # Simulate Jumpserver sending two messages then its connection closing
    jumpserver_ws_mock.recv.side_effect = ["jumpserver_msg1", "jumpserver_msg2", websockets.exceptions.ConnectionClosedOK(rcvd=None, sent=None)]
    
    await jumpserver_to_claude(claude_ws_mock, jumpserver_ws_mock, "test_session_id_j2c")
    
    # Assert claude_ws.send_text was called with the messages
    assert claude_ws_mock.send_text.call_count == 2
    claude_ws_mock.send_text.assert_any_call("jumpserver_msg1")
    claude_ws_mock.send_text.assert_any_call("jumpserver_msg2")
    
    # Assert Claude's WebSocket was closed (because Jumpserver connection closed)
    claude_ws_mock.close.assert_called_once()


@pytest.mark.asyncio
async def test_claude_to_jumpserver_jumpserver_connection_closed():
    claude_ws_mock = AsyncMock()
    jumpserver_ws_mock = AsyncMock()

    # Simulate Claude sending a message, but Jumpserver connection closes when trying to send
    claude_ws_mock.receive_text.side_effect = ["test_msg1", "test_msg2"] # Claude tries to send two messages
    jumpserver_ws_mock.send.side_effect = websockets.exceptions.ConnectionClosedError(rcvd=None, sent=None) # First send fails

    await claude_to_jumpserver(claude_ws_mock, jumpserver_ws_mock, "test_id_c2j_js_closed")

    # Assert send was attempted once
    jumpserver_ws_mock.send.assert_called_once_with("test_msg1")
    # Assert Jumpserver connection was closed (it was already, but close() should be called in finally)
    jumpserver_ws_mock.close.assert_called_once()
    # Claude's receive_text should only be called once before the exception propagates
    claude_ws_mock.receive_text.assert_called_once()


@pytest.mark.asyncio
async def test_jumpserver_to_claude_claude_websocket_disconnect():
    claude_ws_mock = AsyncMock()
    jumpserver_ws_mock = AsyncMock()

    # Simulate Jumpserver sending a message, but Claude disconnects when trying to send to it
    jumpserver_ws_mock.recv.side_effect = ["jumpserver_msg1", "jumpserver_msg2"] # Jumpserver tries to send two messages
    claude_ws_mock.send_text.side_effect = WebSocketDisconnect() # First send to Claude fails

    # Mock client_state for FastAPI WebSocket
    claude_ws_mock.client_state = MagicMock() 
    # Start with connected state for the first attempt
    claude_ws_mock.client_state.value = websockets.WebSocketState.OPEN.value # Using websockets enum for value
    
    # Change state to disconnected after send_text fails to simulate actual disconnect
    async def mock_send_text_then_disconnect(data):
        claude_ws_mock.client_state.value = websockets.WebSocketState.DISCONNECTED.value
        raise WebSocketDisconnect()

    claude_ws_mock.send_text.side_effect = mock_send_text_then_disconnect

    await jumpserver_to_claude(claude_ws_mock, jumpserver_ws_mock, "test_id_j2c_claude_dc")

    # Assert send_text was attempted once
    claude_ws_mock.send_text.assert_called_once_with("jumpserver_msg1")
    # Assert Claude's connection was closed (it was already, but close() should be called in finally)
    # The close() in finally might not be reached if send_text raises WebSocketDisconnect and it's caught by FastAPI
    # The current jumpserver_to_claude does not catch WebSocketDisconnect from claude_ws.send_text
    # Let's check if it calls close. If the structure is try...finally, it should.
    # The current code:
    #   finally:
    #        if not claude_ws.client_state == fastapi.websockets.WebSocketState.DISCONNECTED:
    #             try: await claude_ws.close()
    # So, if it's already disconnected, it won't call close().
    claude_ws_mock.close.assert_not_called() # Because it's already disconnected
    
    # Jumpserver's recv should only be called once before the exception propagates
    jumpserver_ws_mock.recv.assert_called_once()

# The more complex tests for websocket_endpoint using TestClient's websocket_connect
# will be added next, potentially after creating a helper for managing app.state for JUMPSERVER_URL etc.
# For now, the direct coroutine tests are here.

# Fixture to consistently set and tear down app.state for Jumpserver URL and token
# This is better than setting/deleting in each test.
@pytest.fixture
def mock_app_config(monkeypatch):
    # Using monkeypatch to temporarily set environment variables if main.py uses them directly for defaults
    # Or to patch the global variables if they are set directly from os.getenv at module scope
    
    test_jumpserver_url = "http://test-jumpserver-fixture.com"
    test_jumpserver_token = "fixture_token"
    test_mcp_ws_host = "fixture_mcp_host"
    test_mcp_port = 5678

    # Patching the global variables in main module that are read from os.getenv
    monkeypatch.setattr('main.JUMPSERVER_URL', test_jumpserver_url)
    monkeypatch.setattr('main.JUMPSERVER_API_TOKEN', test_jumpserver_token)
    monkeypatch.setattr('main.JUMPSERVER_ORG_ID', "fixture_org_id") # If it's also used from module level
    monkeypatch.setattr('main.MCP_WS_HOST', test_mcp_ws_host)
    monkeypatch.setattr('main.MCP_PORT', test_mcp_port)
    
    # If main.app.state was used (as in earlier tests), this would be:
    # app.state.JUMPSERVER_URL = test_jumpserver_url
    # app.state.JUMPSERVER_API_TOKEN = test_jumpserver_token
    # yield
    # del app.state.JUMPSERVER_URL
    # del app.state.JUMPSERVER_API_TOKEN
    # This monkeypatch approach is cleaner if config is module-level.
    yield {
        "jumpserver_url": test_jumpserver_url,
        "jumpserver_token": test_jumpserver_token,
        "mcp_ws_host": test_mcp_ws_host,
        "mcp_port": test_mcp_port
    }
    # Monkeypatch automatically undoes its changes after the test.

# Re-writing one test to use the mock_app_config fixture
@pytest.mark.asyncio
async def test_get_servers_success_with_fixture(client, respx_mock, mock_app_config):
    mock_asset_data = [{"id": "id1", "name": "server1", "ip": "1.1.1.1", "platform": {"name": "Linux"}, "os": "Ubuntu", "comment": "c1"}]
    
    respx_mock.get(f"{mock_app_config['jumpserver_url']}/api/v1/assets/assets/").mock(return_value=requests.Response(200, json=mock_asset_data))

    response = client.get("/mcp/api/v1/servers")

    assert response.status_code == 200
    data = response.json()
    assert len(data["servers"]) == 1
    assert data["servers"][0]["hostname"] == "server1"

@pytest.mark.asyncio
async def test_connect_server_success_with_fixture(client, respx_mock, mock_app_config):
    server_id = str(uuid.uuid4())
    system_user_id = str(uuid.uuid4())
    expected_jms_token = "test_jms_token_value_fixture"

    respx_mock.get(f"{mock_app_config['jumpserver_url']}/api/v1/users/connection-token/?user-only=1").mock(
        return_value=requests.Response(200, json={"token": expected_jms_token})
    )

    response = client.post(
        f"/mcp/api/v1/servers/{server_id}/connect",
        json={"system_user_id": system_user_id, "connection_type": "ssh"}
    )

    assert response.status_code == 200
    data = response.json()
    mcp_session_id = data["mcp_session_id"]
    assert mcp_session_id in active_sessions
    session_details = active_sessions[mcp_session_id]
    
    assert session_details["jms_connection_token"] == expected_jms_token
    assert session_details["jumpserver_hostname"] == "test-jumpserver-fixture.com" # from mock_app_config
    assert data["ws_url"] == f"ws://{mock_app_config['mcp_ws_host']}:{mock_app_config['mcp_port']}/mcp/ws/{mcp_session_id}"

# Now for the websocket_endpoint tests using TestClient's websocket_connect

@pytest.mark.asyncio
async def test_websocket_endpoint_invalid_mcp_session_id(client, mock_app_config):
    # mock_app_config is used to ensure main.XYZ variables are patched
    # No specific Jumpserver HTTP calls are made here, so respx not strictly needed unless something changes
    
    invalid_session_id = str(uuid.uuid4())
    with client.websocket_connect(f"/mcp/ws/{invalid_session_id}", subprotocols=["JMS-KOKO"]) as websocket:
        # Expecting the server to close the connection
        # The TestClient's websocket_connect context manager might raise an exception if the server closes uncleanly
        # Or the receive call might raise.
        try:
            # Try to receive data, which should fail if connection is closed by server
            _ = websocket.receive_json() # Or receive_text, receive_bytes
        except websockets.exceptions.ConnectionClosed as e: # Check for specific close code
            assert e.code == 1008 # Policy Violation (or whatever code main.py uses)
        except Exception as e: # Catch broader exceptions if the close is not clean from TestClient's perspective
            # This path might be taken if the server closes very quickly
            # For now, let's assume ConnectionClosed is the primary way to detect this.
            # Depending on exact timing, TestClient might raise its own exception or a websockets one.
            # If the server sends a close frame, client.receive_...() should raise ConnectionClosed.
            # If the context manager itself raises, that's also a sign of closure.
            # The key is that it shouldn't connect and stay open.
            # Let's refine: FastAPI's TestClient WebSocketResponse handles this.
            # It will raise a WebSocketDisconnect exception if the server closes.
            pass # Expected if the connection closes before receive_json is called.
                 # The 'with' statement itself might handle the close.

    # A more direct way to check close code with TestClient is not straightforward.
    # We are asserting that it *does not* stay open.
    # If it stayed open, `websocket.receive_json(timeout=0.1)` would timeout.
    # If it closes, it should raise `WebSocketDisconnect`.

    # Let's try a different approach: use try-except around websocket_connect
    try:
        with client.websocket_connect(f"/mcp/ws/{invalid_session_id}", subprotocols=["JMS-KOKO"]) as websocket:
            # If it connects and stays open, this part will run. We can force a receive with timeout.
            websocket.receive_text(timeout=0.1) # Should not succeed
    except fastapi.testclient.WebSocketDisconnect as e:
        assert e.code == 1008
    except websockets.exceptions.WebSocketException as e: # Catching underlying library errors too
        # This might happen if FastAPI's TestClient wraps it.
        # For example, if the server doesn't follow the subprotocol, connect might fail.
        # But here we expect a specific close code after connection.
        # Check if 'e' contains the code, though it's not standard for all WebSocketException types.
        pytest.fail(f"WebSocketException caught, but expected WebSocketDisconnect: {e}")
    except TimeoutError: # From receive_text(timeout=0.1)
        pytest.fail("WebSocket stayed open with invalid session ID")


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
        "jumpserver_hostname": "test-jumpserver-fixture.com", # From mock_app_config
        "jms_connection_token": None,
    }

    # Configure the mock websockets.connect to simulate Jumpserver connection failure
    # The __aenter__ is part of the async context manager protocol
    mock_js_ws_context = AsyncMock()
    mock_js_ws_context.__aenter__.side_effect = websockets.exceptions.InvalidStatusCode(500, headers={})
    mock_websockets_connect.return_value = mock_js_ws_context

    try:
        with client.websocket_connect(f"/mcp/ws/{mcp_session_id}", subprotocols=["JMS-KOKO"]) as websocket:
            # Attempt to receive, expecting a close from our server
            websocket.receive_text(timeout=1) # Give some time for server to react and close
    except fastapi.testclient.WebSocketDisconnect as e:
        assert e.code == 1011 # Internal error, as defined in main.py's websocket_endpoint
    except TimeoutError:
        pytest.fail("WebSocket stayed open despite Jumpserver connection failure.")
    finally:
        if mcp_session_id in active_sessions: # Clean up if test failed before endpoint could
            del active_sessions[mcp_session_id]

# Simplified test for message proxying - focuses on the setup and one message each way
# Testing full bidirectional proxying with TestClient is complex due to its synchronous nature
# and the need to interleave send/receive operations on both ends.
# This test will verify that the connection is established and tasks seem to be wired up.
@pytest.mark.asyncio
@patch('websockets.connect') # Patch the actual websockets.connect
async def test_websocket_endpoint_successful_connection_and_basic_proxy(
    mock_websockets_connect, client, mock_app_config
):
    mcp_session_id = str(uuid.uuid4())
    active_sessions[mcp_session_id] = {
        "server_id": "test_server_id_proxy",
        "system_user_id": "test_sys_user_id_proxy",
        "connection_type": "ssh",
        "jumpserver_hostname": "test-jumpserver-fixture.com",
        "jms_connection_token": None,
    }

    # --- Mock Jumpserver WebSocket (js_ws) ---
    mock_js_ws = AsyncMock(spec=websockets.client.WebSocketClientProtocol)
    # Simulate Jumpserver sending one message then tasks being cancelled or connection closing
    # To break the loop in jumpserver_to_claude:
    mock_js_ws.recv.side_effect = ["data_from_jumpserver", asyncio.CancelledError()] 
    mock_js_ws.closed = False # Initially open

    # --- Mock websockets.connect context manager ---
    mock_js_ws_context = AsyncMock()
    mock_js_ws_context.__aenter__.return_value = mock_js_ws # This is js_ws
    mock_js_ws_context.__aexit__.return_value = None
    mock_websockets_connect.return_value = mock_js_ws_context

    try:
        with client.websocket_connect(f"/mcp/ws/{mcp_session_id}", subprotocols=["JMS-KOKO"]) as claude_ws:
            # Client (Claude) sends a message
            claude_ws.send_text("data_from_claude")
            
            # Server should receive "data_from_jumpserver" and send it to Claude
            received_from_server = claude_ws.receive_text(timeout=1)
            assert received_from_server == "data_from_jumpserver"
            
            # Check if js_ws.send was called with "data_from_claude"
            # This happens in an asyncio.create_task, so it might need a small delay
            await asyncio.sleep(0.01) # Allow the event loop to run the task
            mock_js_ws.send.assert_called_with("data_from_claude")

            # To properly terminate the test and ensure tasks are cleaned up:
            # We can send a "close" signal or rely on the side_effects (CancelledError)
            # The test structure here assumes the side_effects will cause the loops to terminate.
            # The with statement closing claude_ws should trigger WebSocketDisconnect in claude_to_jumpserver.
            
    except fastapi.testclient.WebSocketDisconnect as e:
        # This might be raised when the client (claude_ws) context manager exits,
        # if the server-side tasks haven't fully cleaned up yet or if server also closes.
        # For this test, we're okay if it disconnects cleanly.
        pass
    except TimeoutError:
        pytest.fail("WebSocket operation timed out during proxy test.")
    finally:
        if mcp_session_id in active_sessions:
            del active_sessions[mcp_session_id]

        # Assert that Jumpserver WebSocket was closed
        # This should be called in the __aexit__ of the main endpoint's async with block for js_ws
        # or in the finally block of claude_to_jumpserver/jumpserver_to_claude
        # Given the mock_js_ws.recv side_effect, jumpserver_to_claude will finish.
        # When claude_ws context exits, claude_to_jumpserver's receive_text will raise WebSocketDisconnect.
        # This should lead to js_ws.close() being called in claude_to_jumpserver's finally.
        await asyncio.sleep(0.01) # Ensure tasks have run their finally blocks
        mock_js_ws.close.assert_called_once()

# Final check on imports for main.py objects
# from main import app, active_sessions, McpServerList, ConnectResponse, claude_to_jumpserver, jumpserver_to_claude, WebSocketDisconnect
# This assumes main.py is structured to allow these direct imports for testing.
# If main.py uses if __name__ == "__main__": for uvicorn.run, these should be fine.
# The WebSocketDisconnect is from fastapi, not websockets library directly for the claude_ws.
from fastapi import WebSocketDisconnect as FastAPIWebSocketDisconnect # Alias if needed
# The test `test_claude_to_jumpserver_coroutine` uses `WebSocketDisconnect` which should be `FastAPIWebSocketDisconnect`
# if it's for the `claude_ws` mock which is a FastAPI `WebSocket`.
# Let's correct the direct coroutine tests to use the right disconnect exception type for claude_ws.

# Correcting coroutine tests for WebSocketDisconnect type if claude_ws is a FastAPI WebSocket mock
@pytest.mark.asyncio
async def test_claude_to_jumpserver_coroutine_fastapi_disconnect():
    claude_ws_mock = AsyncMock(spec=FastAPIWebSocketDisconnect) # More specific mock if needed
    claude_ws_mock.receive_text.side_effect = ["test_msg1", FastAPIWebSocketDisconnect(code=1000)]
    
    jumpserver_ws_mock = AsyncMock(spec=websockets.client.WebSocketClientProtocol)
    jumpserver_ws_mock.closed = False
    
    await claude_to_jumpserver(claude_ws_mock, jumpserver_ws_mock, "test_id_c2j_fd")
    
    jumpserver_ws_mock.send.assert_called_once_with("test_msg1")
    jumpserver_ws_mock.close.assert_called_once()

# The original test for jumpserver_to_claude used websockets.exceptions.ConnectionClosedOK for jumpserver_ws, which is correct.
# The test for claude_to_jumpserver_jumpserver_connection_closed is correct as jumpserver_ws_mock.send raises websockets.exceptions.ConnectionClosedError.

# The test test_jumpserver_to_claude_claude_websocket_disconnect:
# claude_ws_mock.send_text.side_effect = WebSocketDisconnect()
# This should be FastAPIWebSocketDisconnect if claude_ws is a FastAPI WebSocket mock.
@pytest.mark.asyncio
async def test_jumpserver_to_claude_claude_fastapi_websocket_disconnect():
    claude_ws_mock = AsyncMock(spec=FastAPIWebSocketDisconnect)
    # Simulate Jumpserver sending a message
    jumpserver_ws_mock = AsyncMock(spec=websockets.client.WebSocketClientProtocol)
    jumpserver_ws_mock.recv.side_effect = ["jumpserver_msg1", websockets.exceptions.ConnectionClosedOK(None, None)]
    jumpserver_ws_mock.closed = False

    # Mock client_state for FastAPI WebSocket
    claude_ws_mock.client_state = MagicMock()
    claude_ws_mock.client_state.value = websockets.WebSocketState.OPEN.value # Using value for comparison in main code

    # Simulate Claude disconnecting when MCP tries to send to it
    async def mock_send_text_then_disconnect(data):
        # In main.py, this exception is not caught by jumpserver_to_claude's try-except for websockets.exceptions
        # So it will propagate up.
        raise FastAPIWebSocketDisconnect(code=1000)

    claude_ws_mock.send_text.side_effect = mock_send_text_then_disconnect
    
    # We expect FastAPIWebSocketDisconnect to be raised by the coroutine
    with pytest.raises(FastAPIWebSocketDisconnect):
        await jumpserver_to_claude(claude_ws_mock, jumpserver_ws_mock, "test_id_j2c_claude_fd")

    # Assert send_text was attempted once
    claude_ws_mock.send_text.assert_called_once_with("jumpserver_msg1")
    
    # In this scenario, the finally block of jumpserver_to_claude might not call claude_ws.close()
    # because the exception FastAPIWebSocketDisconnect is raised by claude_ws.send_text() and propagates out,
    # potentially bypassing the normal flow to the finally block's claude_ws.close() call
    # if the calling handler (websocket_endpoint) catches it first or if the task is cancelled.
    # The main `websocket_endpoint` catches general Exception for logging, but then re-raises or closes.
    # Given the exception, the `finally` in `jumpserver_to_claude` for `claude_ws.close()` will be reached.
    # It will see that `claude_ws.client_state` is NOT DISCONNECTED (because it's a mock, we need to set it)
    # or it will try to close. Let's assume it will try to close.
    # However, the test is about the coroutine itself. If send_text raises, the coroutine stops.
    # The finally block in `jumpserver_to_claude` IS executed.
    # It will check `claude_ws.client_state`. If we don't mock it to be DISCONNECTED, it will call `close()`.
    claude_ws_mock.close.assert_called_once()
    
    jumpserver_ws_mock.recv.assert_called_once()
