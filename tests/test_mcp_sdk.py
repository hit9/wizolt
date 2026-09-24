"""MCPManager against real MCP servers built with the SDK, not stand-ins.

The other MCP tests fake the client's results, which cannot catch a disagreement with the SDK's own
objects: its models are snake_case in Python and camelCase on the wire, and a reader that guesses
the wrong one silently gets nothing. These run the manager's whole path -- client, transport,
result shapes -- against a server the SDK serves in-process, over stdio, or over HTTP through an
ASGI transport, so no test opens a network port except the OAuth loopback callback."""

import asyncio
import base64
import contextlib
import os
import secrets
import sys
import textwrap
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as ServerToolError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.types import ImageContent, ToolAnnotations
from pydantic import BaseModel

from wizolt.base import ToolError
from wizolt.config import Config
from wizolt.session import Session, bootstrap_features

URL = "http://127.0.0.1:8000/mcp"


class Total(BaseModel):
    total: int


def build_server(**kwargs) -> MCPServer:
    server = MCPServer("fixture", **kwargs)

    @server.tool(description="Echo text back.", annotations=ToolAnnotations(read_only_hint=True))
    def echo(text: str) -> str:
        return "echo: " + text

    @server.tool(description="Deletes everything.")
    def wipe() -> str:
        return "wiped"

    @server.tool(description="Adds two numbers.", annotations=ToolAnnotations(destructive_hint=False))
    def add(a: int, b: int) -> Total:
        return Total(total=a + b)

    @server.tool(description="Fails the way a tool reports failure.")
    def limited() -> str:
        raise ServerToolError("rate limited, retry in 5s")

    @server.tool(description="Returns a picture.")
    def picture() -> list[ImageContent]:
        return [ImageContent(type="image", data=base64.b64encode(b"png").decode(), mime_type="image/png")]

    @server.resource("docs://guide.md", description="How to call add.", mime_type="text/markdown")
    def guide() -> str:
        return "# add\nPass two integers."

    @server.resource("bin://logo.png", description="Logo", mime_type="image/png")
    def logo() -> bytes:
        return b"\x89PNG"

    return server


def session(tmp_path, servers: dict) -> Session:
    s = Session(cwd=str(tmp_path), config=Config.from_dict({"mcp": servers}))
    bootstrap_features(s)
    return s


# ---------------------------------------------------------------------------
# In-process: the SDK's client and result objects, no transport
# ---------------------------------------------------------------------------


@pytest.fixture
def inproc(tmp_path, monkeypatch):
    s = session(tmp_path, {"fixture": {"url": URL}})
    server = build_server()
    monkeypatch.setattr(s.mcp, "_transport", lambda *_args: server)
    return s


async def test_discovery_reads_the_sdks_tool_and_resource_models(inproc):
    assert await inproc.mcp.connect_server("fixture") == "MCP server connected: fixture; tools=5; resources=2"

    tools = {tool.name: tool for tool in inproc.mcp.tools["fixture"]}
    assert tools["echo"].input_schema["required"] == ["text"]
    assert tools["add"].output_schema["properties"]["total"]["type"] == "integer"
    assert tools["echo"].output_schema["properties"]  # a str result is wrapped as structured output
    assert {(res.uri, res.mime_type) for res in inproc.mcp.resources["fixture"]} == {
        ("docs://guide.md", "text/markdown"),
        ("bin://logo.png", "image/png"),
    }


async def test_annotations_decide_confirmation_by_their_wire_names(inproc):
    """The SDK names the hints read_only_hint/destructive_hint; the policy reads readOnlyHint."""
    await inproc.mcp.connect_server("fixture")

    assert inproc.mcp.tool_needs_confirmation("fixture", "echo") is False  # read-only
    assert inproc.mcp.tool_needs_confirmation("fixture", "add") is False  # not destructive
    assert inproc.mcp.tool_needs_confirmation("fixture", "wipe") is True  # unannotated


async def test_calls_return_text_structured_and_image_results(inproc):
    await inproc.mcp.connect_server("fixture")

    assert await inproc.mcp.call_tool("fixture", "echo", {"text": "hi"}) == '<MCPCall server="fixture" tool="echo">\necho: hi\n</MCPCall>'
    assert await inproc.mcp.call_tool_structured("fixture", "add", {"a": 2, "b": 3}) == {"total": 5}
    picture = await inproc.mcp.call_tool("fixture", "picture", {})
    assert '"mimeType": "image/png"' in picture and "mime_type" not in picture and "null" not in picture


async def test_a_failing_tool_fails_the_call_with_the_servers_message(inproc):
    await inproc.mcp.connect_server("fixture")

    with pytest.raises(ToolError, match=r"^MCP call failed: .*rate limited, retry in 5s$"):
        await inproc.mcp.call_tool("fixture", "limited", {})
    with pytest.raises(ToolError, match="MCP call failed: .*Unknown tool"):
        await inproc.mcp.call_tool("fixture", "missing", {})


async def test_resources_read_as_text_or_a_binary_marker(inproc):
    await inproc.mcp.connect_server("fixture")

    assert await inproc.mcp.read_resource("fixture", "docs://guide.md") == (
        '<MCPResource server="fixture" uri="docs://guide.md">\n# add\nPass two integers.\n</MCPResource>'
    )
    blob = await inproc.mcp.read_resource("fixture", "bin://logo.png")
    assert '<binary mimeType="image/png"' in blob
    with pytest.raises(ToolError, match="MCP resource read failed"):
        await inproc.mcp.read_resource("fixture", "docs://absent.md")


# ---------------------------------------------------------------------------
# stdio: a real subprocess and the environment it is given
# ---------------------------------------------------------------------------


async def test_stdio_server_runs_with_configured_env_over_the_inherited_one(tmp_path, monkeypatch):
    script = tmp_path / "server.py"
    script.write_text(
        textwrap.dedent(
            """
            import os
            from mcp.server.mcpserver import MCPServer

            server = MCPServer("stdio")

            @server.tool(description="Reads an environment variable.")
            def env(name: str) -> str:
                return os.environ.get(name, "<unset>")

            server.run()
            """
        )
    )
    monkeypatch.setenv("WIZOLT_INHERITED", "from-shell")
    s = session(tmp_path, {"local": {"command": sys.executable, "args": [str(script)], "env": {"WIZOLT_TOKEN": "t0k"}}})

    assert await s.mcp.connect_server("local") == "MCP server connected: local; tools=1; resources=0"
    assert "t0k" in await s.mcp.call_tool("local", "env", {"name": "WIZOLT_TOKEN"})
    assert "from-shell" in await s.mcp.call_tool("local", "env", {"name": "WIZOLT_INHERITED"})


async def test_a_timed_out_or_closed_stdio_call_reaps_the_server_process(tmp_path):
    """The manager returns from a timeout, or from close(), only once the client has unwound, so
    the server process it started is gone rather than left to the interpreter's exit."""
    script = tmp_path / "server.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, sys, time
            from mcp.server.mcpserver import MCPServer

            server = MCPServer("slow")

            @server.tool(description="Records its pid, then hangs.")
            def hang() -> str:
                with open(sys.argv[1], "a") as pids:
                    pids.write(f"{os.getpid()}\\n")
                time.sleep(60)
                return "never"

            server.run()
            """
        )
    )
    pids = tmp_path / "pids"
    s = session(tmp_path, {"slow": {"command": sys.executable, "args": [str(script), str(pids)]}})
    s.settings.shell_timeout = 2
    assert (await s.mcp.connect_server("slow")).startswith("MCP server connected")

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    with pytest.raises(ToolError, match="timed out after 2s"):
        await s.mcp.call_tool("slow", "hang", {})
    first = int(pids.read_text().split()[0])
    assert not alive(first)

    # close() lands mid-call, and the call's own 2s deadline lands during the teardown it starts:
    # the second cancellation must not stop the kill, or close() waits out the tool's 60s.
    call = asyncio.create_task(s.mcp.call_tool("slow", "hang", {}))
    while len(pids.read_text().split()) < 2:
        await asyncio.sleep(0.05)
    started = time.monotonic()
    await s.mcp.close()
    assert time.monotonic() - started < 10
    with pytest.raises((asyncio.CancelledError, ToolError)):
        await call
    assert not alive(int(pids.read_text().split()[1]))


async def test_a_stdio_command_that_does_not_exist_is_a_server_error(tmp_path):
    s = session(tmp_path, {"local": {"command": str(tmp_path / "no-such-server")}})

    result = await s.mcp.connect_server("local")

    assert result.startswith("MCP server error: local: ")
    assert "TaskGroup" not in result


# ---------------------------------------------------------------------------
# HTTP: the streamable transport against the SDK's ASGI app
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def serve_http(monkeypatch, server: MCPServer, *, seen: list | None = None):
    """Route the manager's HTTP client to `server`'s app in-process, with its session manager running."""
    import mcp.shared._httpx_utils

    app = server.streamable_http_app()

    async def recording_app(scope, receive, send):
        if seen is not None and scope["type"] == "http":
            seen.append((scope["path"], {key.decode(): value.decode() for key, value in scope["headers"]}))
        await app(scope, receive, send)

    def http_client(headers=None, timeout=None, auth=None):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=recording_app), headers=headers, auth=auth, timeout=timeout)

    monkeypatch.setattr(mcp.shared._httpx_utils, "create_mcp_http_client", http_client)
    async with server.session_manager.run():
        yield


async def test_http_server_receives_the_configured_auth_headers(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_TOKEN", "s3cret")
    monkeypatch.setenv("FIXTURE_TENANT", "acme")
    s = session(tmp_path, {"fixture": {"url": URL, "bearer_token_env_var": "FIXTURE_TOKEN", "env_http_headers": {"X-Tenant": "FIXTURE_TENANT"}}})
    seen: list = []

    async with serve_http(monkeypatch, build_server(), seen=seen):
        assert await s.mcp.connect_server("fixture") == "MCP server connected: fixture; tools=5; resources=2"
        assert await s.mcp.call_tool_structured("fixture", "add", {"a": 1, "b": 1}) == {"total": 2}

    posts = [headers for path, headers in seen if path == "/mcp"]
    assert posts and all(headers.get("authorization") == "Bearer s3cret" and headers.get("x-tenant") == "acme" for headers in posts)


@pytest.mark.parametrize(("status", "body", "shown"), [
    (401, b"nope", "HTTP 401 Unauthorized"),
    (502, b"<html>bad gateway</html>", "HTTP 502 Bad Gateway"),
    (400, b'{"jsonrpc": "2.0", "id": null, "error": {"code": -32600, "message": "tenant header missing"}}', "tenant header missing"),
])
async def test_an_http_error_names_its_status_or_the_servers_own_message(tmp_path, monkeypatch, status, body, shown):
    """The SDK reports a bare error status as "Server returned an error response"; a wrong token
    and an outage must not read the same. A JSON-RPC error in the body keeps its own message."""
    import mcp.shared._httpx_utils

    content_type = b"application/json" if body.startswith(b"{") else b"text/plain"

    async def refusing_app(scope, receive, send):
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", content_type)]})
            await send({"type": "http.response.body", "body": body})

    def http_client(headers=None, timeout=None, auth=None):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=refusing_app), headers=headers, auth=auth)

    monkeypatch.setattr(mcp.shared._httpx_utils, "create_mcp_http_client", http_client)
    s = session(tmp_path, {"fixture": {"url": URL}})

    assert await s.mcp.connect_server("fixture") == "MCP server error: fixture: " + shown


# ---------------------------------------------------------------------------
# OAuth: discovery, registration, the browser hand-off, and the loopback callback
# ---------------------------------------------------------------------------


class MemoryAuthServer:
    """A minimal in-memory authorization server for the SDK's auth routes; it approves every login."""

    def __init__(self, *, access_ttl: int = 3600):
        self.access_ttl = access_ttl
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}
        self.refreshes = 0

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params: AuthorizationParams):
        code = secrets.token_urlsafe(8)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return str(params.redirect_uri) + "?" + urlencode({"code": code, "state": params.state or ""})

    async def load_authorization_code(self, client, authorization_code):
        return self.codes.get(authorization_code)

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        return self.issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token):
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.refreshes += 1
        self.refresh.pop(refresh_token.token, None)
        return self.issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token):
        access = self.access.get(token)
        return access if access and (access.expires_at is None or access.expires_at > time.time()) else None

    async def revoke_token(self, token):
        self.access.pop(getattr(token, "token", ""), None)
        self.refresh.pop(getattr(token, "token", ""), None)

    def issue(self, client_id, scopes, resource) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(8), secrets.token_urlsafe(8)
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes, expires_at=int(time.time()) + self.access_ttl, resource=resource)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes, resource=resource)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.access_ttl, refresh_token=refresh, scope=" ".join(scopes) or None)

    def forget_everything_issued(self) -> None:
        self.access.clear()
        self.refresh.clear()


def oauth_server(auth: MemoryAuthServer) -> MCPServer:
    settings = AuthSettings(
        issuer_url="http://127.0.0.1:8000",  # pyright: ignore[reportArgumentType]
        resource_server_url=URL,  # pyright: ignore[reportArgumentType]
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )
    return build_server(auth_server_provider=auth, auth=settings)


class Browser:
    """Stands in for the user's browser: approves the login and follows the redirect home.

    `webbrowser.open` runs on a worker thread, so the authorization server's app is driven on the
    test's loop and the redirect is followed to wizolt's real loopback listener over a socket."""

    def __init__(self, loop: asyncio.AbstractEventLoop, auth: MemoryAuthServer):
        self.loop, self.auth, self.opened = loop, auth, []

    def open(self, url: str) -> bool:
        self.opened.append(url)
        query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        client = asyncio.run_coroutine_threadsafe(self.auth.get_client(query["client_id"]), self.loop).result(5)
        params = AuthorizationParams(
            state=query.get("state"),
            scopes=query["scope"].split() if query.get("scope") else None,
            code_challenge=query["code_challenge"],
            redirect_uri=query["redirect_uri"],  # pyright: ignore[reportArgumentType]
            redirect_uri_provided_explicitly=True,
            resource=query.get("resource"),
        )
        callback = asyncio.run_coroutine_threadsafe(self.auth.authorize(client, params), self.loop).result(5)
        callback_url = urlsplit(callback)._replace(netloc="127.0.0.1:" + str(urlsplit(callback).port)).geturl()
        httpx2.get(callback_url, timeout=5)
        return True


async def test_interactive_connect_logs_in_through_the_browser_and_stores_the_login(tmp_path, monkeypatch):
    import webbrowser

    auth = MemoryAuthServer()
    browser = Browser(asyncio.get_running_loop(), auth)
    monkeypatch.setattr(webbrowser, "open", browser.open)
    s = session(tmp_path, {"fixture": {"url": URL, "auth": "oauth"}})
    notices: list[str] = []

    async with serve_http(monkeypatch, oauth_server(auth)):
        assert await s.mcp.connect_server("fixture") == "MCP server authentication required: fixture; run /mcp connect fixture interactively"
        result = await s.mcp.connect_server("fixture", interactive=True, notify=notices.append)
        assert result == "MCP server connected: fixture; tools=5; resources=2"
        assert "echo: hi" in await s.mcp.call_tool("fixture", "echo", {"text": "hi"})

        # A later session loads the stored login: no browser, no second registration.
        again = session(tmp_path, {"fixture": {"url": URL, "auth": "oauth", "auto_connect": True}})
        await again.mcp.discover_auto()
        assert again.mcp.server_issue("fixture") is None and len(again.mcp.tools["fixture"]) == 5

    assert len(browser.opened) == 1 and len(auth.clients) == 1
    assert notices == ["Open this URL to authorize MCP server `fixture`:\n" + browser.opened[0]]
    (client,) = auth.clients.values()
    assert str(client.redirect_uris[0]).startswith("http://localhost:") and client.client_name == "wizolt"


async def test_expired_access_token_is_refreshed_after_a_restart_without_a_browser(tmp_path, monkeypatch):
    """Discovery lists tools and resources at once, each with its own provider. The server rotates
    refresh tokens, so both refreshing would spend one token twice and fail the second: exactly one
    refresh happens, and both listings arrive."""
    import webbrowser

    auth = MemoryAuthServer(access_ttl=2)
    browser = Browser(asyncio.get_running_loop(), auth)
    monkeypatch.setattr(webbrowser, "open", browser.open)
    s = session(tmp_path, {"fixture": {"url": URL, "auth": "oauth"}})

    async with serve_http(monkeypatch, oauth_server(auth)):
        assert (await s.mcp.connect_server("fixture", interactive=True)).startswith("MCP server connected")
        # Past the access token's lifetime; the stored absolute expiry is all a new session has.
        monkeypatch.setattr(time, "time", lambda real=time.time: real() + 60)
        restarted = session(tmp_path, {"fixture": {"url": URL, "auth": "oauth"}})
        assert await restarted.mcp.connect_server("fixture") == "MCP server connected: fixture; tools=5; resources=2"

    assert auth.refreshes == 1
    assert len(browser.opened) == 1


async def test_a_revoked_login_asks_for_connect_instead_of_opening_a_browser(tmp_path, monkeypatch):
    import webbrowser

    auth = MemoryAuthServer()
    browser = Browser(asyncio.get_running_loop(), auth)
    monkeypatch.setattr(webbrowser, "open", browser.open)
    s = session(tmp_path, {"fixture": {"url": URL, "auth": "oauth"}})

    async with serve_http(monkeypatch, oauth_server(auth)):
        await s.mcp.connect_server("fixture", interactive=True)
        auth.forget_everything_issued()
        restarted = session(tmp_path, {"fixture": {"url": URL, "auth": "oauth"}})
        result = await restarted.mcp.connect_server("fixture")

        assert result == "MCP server error: fixture: authentication required; run /mcp connect fixture"
        assert len(browser.opened) == 1

        # And an interactive connect replaces the rejected credentials with a fresh login.
        assert (await restarted.mcp.connect_server("fixture", interactive=True)).startswith("MCP server connected")
        assert len(browser.opened) == 2
