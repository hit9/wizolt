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
from wizolt.mcp import MCPManager
from wizolt.session import Session, bootstrap_features

URL = "http://127.0.0.1:8000/mcp"


class Total(BaseModel):
    total: int


@pytest.fixture(autouse=True)
async def no_unhandled_loop_errors():
    """Fail a test whose MCP traffic leaves something for the loop's exception handler.

    That is where a leaked async generator surfaces -- finalized on another task, releasing a lock
    it did not take -- and a task whose exception nobody retrieved. Neither fails a test by itself,
    and both reach the TUI as "Unhandled exception in event loop". Collecting garbage and letting
    the loop run finalizers makes them happen before the test ends rather than in a later one."""
    import gc

    loop = asyncio.get_running_loop()
    errors: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(f"{context.get('message')}: {context.get('exception')!r}"))
    try:
        yield
        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0.01)
        assert errors == []
    finally:
        loop.set_exception_handler(previous)


@pytest.fixture(params=["modern", "legacy"])
def era(request) -> str:
    """Which protocol generation the fixture server speaks.

    The SDK's own server speaks the sessionless 2026-07-28 protocol, which the client prefers, so
    left alone every test would skip the `initialize` handshake -- the one nearly every deployed
    server still uses. A legacy server is one that does not know `server/discover`."""
    return request.param


def build_server(era: str = "modern", **kwargs) -> MCPServer:
    server = MCPServer("fixture", **kwargs)
    if era == "legacy":
        server._lowlevel_server._request_handlers.pop("server/discover")

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
async def inproc(tmp_path, monkeypatch, era):
    """The manager against the fixture server: in-process for the modern protocol, and over HTTP
    for the legacy one, since the SDK's in-process connection skips version negotiation."""
    s = session(tmp_path, {"fixture": {"url": URL}})
    server = build_server(era)
    if era == "modern":
        monkeypatch.setattr(s.mcp, "_transport", lambda *_args: server)
        yield s
        return
    async with serve_http(monkeypatch, server):
        yield s


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


class StreamingASGITransport(httpx2.AsyncBaseTransport):
    """An in-process transport that returns at the response's headers and streams its body.

    httpx2.ASGITransport buffers a whole response first, so a legacy server's never-ending GET
    event stream never returns -- and under OAuth the auth flow driving that GET holds the SDK's
    lock the whole time, stalling every other request. A real socket returns at the headers."""

    def __init__(self, app):
        self.app = app

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = b"".join([part async for part in request.stream])  # pyright: ignore[reportGeneralTypeIssues]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "root_path": "",
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "client": ("127.0.0.1", 50000),
            "server": (request.url.host, request.url.port or 80),
        }
        started: asyncio.Future = asyncio.get_running_loop().create_future()
        chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        disconnected = asyncio.Event()
        sent_body = False

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start" and not started.done():
                started.set_result(message)
            elif message["type"] == "http.response.body":
                if message.get("body"):
                    await chunks.put(message["body"])
                if not message.get("more_body", False):
                    await chunks.put(None)

        async def run():
            try:
                await self.app(scope, receive, send)
            except Exception as error:  # noqa: BLE001 - handed to the waiting request
                if not started.done():
                    started.set_exception(error)
            finally:
                await chunks.put(None)

        task = asyncio.ensure_future(run())
        try:
            start = await started
        except BaseException:
            # The request was abandoned before the app answered: hang up, as a socket closing would.
            disconnected.set()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise

        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self):
                while (chunk := await chunks.get()) is not None:
                    yield chunk

            async def aclose(self):
                # A client hanging up is a disconnect the app sees, not its task torn down: the
                # app finishes its own bookkeeping, as it would behind a real server.
                disconnected.set()
                await asyncio.wait({task}, timeout=2)
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task

        return httpx2.Response(start["status"], headers=start.get("headers", []), stream=Body(), request=request)


class Stall:
    """While set, the MCP endpoint takes requests and never answers them: a server gone quiet, or a
    network that swallows packets. Auth routes keep answering."""

    def __init__(self):
        self.on = False


@contextlib.asynccontextmanager
async def serve_http(monkeypatch, server: MCPServer, *, seen: list | None = None, stall: Stall | None = None):
    """Route the manager's HTTP client to `server`'s app in-process, with its session manager running."""
    import mcp.shared._httpx_utils

    app = server.streamable_http_app()

    async def recording_app(scope, receive, send):
        if seen is not None and scope["type"] == "http":
            seen.append((scope["path"], {key.decode(): value.decode() for key, value in scope["headers"]}))
        if stall is not None and stall.on and scope["type"] == "http" and scope["path"] == "/mcp":
            await asyncio.Event().wait()
        await app(scope, receive, send)

    def http_client(headers=None, timeout=None, auth=None):
        return httpx2.AsyncClient(transport=StreamingASGITransport(recording_app), headers=headers, auth=auth, timeout=timeout)

    monkeypatch.setattr(mcp.shared._httpx_utils, "create_mcp_http_client", http_client)
    # The session manager runs in a task of its own: its anyio scope must be exited by the task
    # that entered it, and a fixture's setup and teardown run on different tasks.
    running, stop = asyncio.Event(), asyncio.Event()

    async def run_session_manager():
        async with server.session_manager.run():
            running.set()
            await stop.wait()

    manager = asyncio.create_task(run_session_manager())
    await running.wait()
    try:
        yield
    finally:
        stop.set()
        await manager


async def test_http_server_receives_the_configured_auth_headers(tmp_path, monkeypatch, era):
    monkeypatch.setenv("FIXTURE_TOKEN", "s3cret")
    monkeypatch.setenv("FIXTURE_TENANT", "acme")
    s = session(tmp_path, {"fixture": {"url": URL, "bearer_token_env_var": "FIXTURE_TOKEN", "env_http_headers": {"X-Tenant": "FIXTURE_TENANT"}}})
    seen: list = []

    async with serve_http(monkeypatch, build_server(era), seen=seen):
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

    def __init__(self, *, access_ttl: int | None = 3600, rotate: bool = True, issue_refresh: bool = True):
        self.access_ttl = access_ttl
        self.rotate = rotate
        self.issue_refresh = issue_refresh
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
        if self.rotate:
            self.refresh.pop(refresh_token.token, None)
        token = self.issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource, refresh=self.rotate)
        return token

    async def load_access_token(self, token):
        access = self.access.get(token)
        return access if access and (access.expires_at is None or access.expires_at > time.time()) else None

    async def revoke_token(self, token):
        self.access.pop(getattr(token, "token", ""), None)
        self.refresh.pop(getattr(token, "token", ""), None)

    def issue(self, client_id, scopes, resource, *, refresh: bool = True) -> OAuthToken:
        """A new access token, and a refresh token unless this server keeps (or never gives) one."""
        access = secrets.token_urlsafe(8)
        expires_at = int(time.time()) + self.access_ttl if self.access_ttl is not None else None
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes, expires_at=expires_at, resource=resource)
        refresh_token = None
        if refresh and self.issue_refresh:
            refresh_token = secrets.token_urlsafe(8)
            self.refresh[refresh_token] = RefreshToken(token=refresh_token, client_id=client_id, scopes=scopes, resource=resource)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.access_ttl, refresh_token=refresh_token, scope=" ".join(scopes) or None)

    def forget_everything_issued(self) -> None:
        self.access.clear()
        self.refresh.clear()


def oauth_server(auth: MemoryAuthServer, era: str = "modern", *, issuer: str = "http://127.0.0.1:8000", scopes: list[str] | None = None, registration: bool = True) -> MCPServer:
    """The fixture server behind the SDK's auth routes. A separate `issuer` puts the authorization
    server on its own origin; the ASGI transport routes every host to this one app regardless."""
    settings = AuthSettings(
        issuer_url=issuer,  # pyright: ignore[reportArgumentType]
        resource_server_url=URL,  # pyright: ignore[reportArgumentType]
        client_registration_options=ClientRegistrationOptions(enabled=registration, valid_scopes=scopes, default_scopes=scopes),
        required_scopes=scopes,
        # Refuse tokens issued for another resource, as a careful provider does: the login must
        # name this server as the resource (RFC 8707) wherever the protocol asks for it.
        validate_token_resource=True,
    )
    return build_server(era, auth_server_provider=auth, auth=settings)


@contextlib.asynccontextmanager
async def login_server(monkeypatch, era: str, auth: MemoryAuthServer | None = None, *, stall: Stall | None = None, **server_options):
    """Serve an OAuth-protected fixture and stand a scripted browser in for the user's."""
    import webbrowser

    auth = auth or MemoryAuthServer()
    browser = Browser(asyncio.get_running_loop(), auth)
    monkeypatch.setattr(webbrowser, "open", browser.open)
    async with serve_http(monkeypatch, oauth_server(auth, era, **server_options), stall=stall):
        yield auth, browser


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


def oauth_session(tmp_path, **options) -> Session:
    return session(tmp_path, {"fixture": {"url": URL, "auth": "oauth", **options}})


CONNECTED = "MCP server connected: fixture; tools=5; resources=2"


async def test_interactive_connect_logs_in_through_the_browser_and_stores_the_login(tmp_path, monkeypatch, era):
    s = oauth_session(tmp_path)
    notices: list[str] = []

    async with login_server(monkeypatch, era) as (auth, browser):
        assert await s.mcp.connect_server("fixture") == "MCP server authentication required: fixture; run /mcp connect fixture interactively"
        assert await s.mcp.connect_server("fixture", interactive=True, notify=notices.append) == CONNECTED
        assert "echo: hi" in await s.mcp.call_tool("fixture", "echo", {"text": "hi"})

        # A later session loads the stored login: no browser, no second registration.
        again = oauth_session(tmp_path, auto_connect=True)
        await again.mcp.discover_auto()
        assert again.mcp.server_issue("fixture") is None and len(again.mcp.tools["fixture"]) == 5

    assert len(browser.opened) == 1 and len(auth.clients) == 1
    assert notices == ["Open this URL to authorize MCP server `fixture`:\n" + browser.opened[0]]
    (client,) = auth.clients.values()
    assert str(client.redirect_uris[0]).startswith("http://localhost:") and client.client_name == "wizolt"


@pytest.mark.parametrize("rotate", [True, False], ids=["rotating", "reused-refresh-token"])
async def test_expired_access_token_is_refreshed_after_a_restart_without_a_browser(tmp_path, monkeypatch, era, rotate):
    """Guards the refresh race. Discovery lists tools and resources at once, each with its own
    provider; a server that rotates refresh tokens rejects the second use of one, so both
    refreshing lost the resources or failed discovery. Exactly one refresh, both listings."""
    s = oauth_session(tmp_path)

    async with login_server(monkeypatch, era, MemoryAuthServer(access_ttl=2, rotate=rotate)) as (auth, browser):
        assert await s.mcp.connect_server("fixture", interactive=True) == CONNECTED
        # Past the access token's lifetime; the stored absolute expiry is all a new session has.
        monkeypatch.setattr(time, "time", lambda real=time.time: real() + 60)
        assert await oauth_session(tmp_path).mcp.connect_server("fixture") == CONNECTED
        # And the refreshed login is itself stored: the next restart needs no refresh at all.
        assert await oauth_session(tmp_path).mcp.connect_server("fixture") == CONNECTED

    assert auth.refreshes == 1
    assert len(browser.opened) == 1


async def test_an_expired_login_without_a_refresh_token_asks_for_connect(tmp_path, monkeypatch, era):
    s = oauth_session(tmp_path)

    async with login_server(monkeypatch, era, MemoryAuthServer(access_ttl=2, issue_refresh=False)) as (auth, browser):
        assert await s.mcp.connect_server("fixture", interactive=True) == CONNECTED
        monkeypatch.setattr(time, "time", lambda real=time.time: real() + 60)

        assert await oauth_session(tmp_path).mcp.connect_server("fixture") == "MCP server error: fixture: authentication required; run /mcp connect fixture"
    assert len(browser.opened) == 1 and auth.refreshes == 0


async def test_a_token_without_an_expiry_is_used_until_the_server_refuses_it(tmp_path, monkeypatch, era):
    s = oauth_session(tmp_path)

    async with login_server(monkeypatch, era, MemoryAuthServer(access_ttl=None)) as (auth, browser):
        assert await s.mcp.connect_server("fixture", interactive=True) == CONNECTED
        monkeypatch.setattr(time, "time", lambda real=time.time: real() + 86_400)
        assert await oauth_session(tmp_path).mcp.connect_server("fixture") == CONNECTED
    assert len(browser.opened) == 1 and auth.refreshes == 0


async def test_a_revoked_login_asks_for_connect_instead_of_opening_a_browser(tmp_path, monkeypatch, era):
    s = oauth_session(tmp_path)

    async with login_server(monkeypatch, era) as (auth, browser):
        await s.mcp.connect_server("fixture", interactive=True)
        auth.forget_everything_issued()
        restarted = oauth_session(tmp_path)

        assert await restarted.mcp.connect_server("fixture") == "MCP server error: fixture: authentication required; run /mcp connect fixture"
        assert len(browser.opened) == 1

        # And an interactive connect replaces the rejected credentials with a fresh login.
        assert await restarted.mcp.connect_server("fixture", interactive=True) == CONNECTED
        assert len(browser.opened) == 2


async def test_login_follows_an_authorization_server_on_its_own_origin_and_requests_its_scopes(tmp_path, monkeypatch, era):
    """Hosted providers put the authorization server elsewhere and require scopes; the login finds
    it through the resource metadata and asks for what the server says it needs."""
    s = oauth_session(tmp_path)

    async with login_server(monkeypatch, era, issuer="http://127.0.0.1:9000", scopes=["mcp:tools"]) as (auth, browser):
        assert await s.mcp.connect_server("fixture", interactive=True) == CONNECTED

    (opened,) = browser.opened
    assert opened.startswith("http://127.0.0.1:9000/authorize?")
    assert parse_qs(urlsplit(opened).query)["scope"] == ["mcp:tools"]
    assert all(token.scopes == ["mcp:tools"] for token in auth.access.values())


async def test_a_server_that_refuses_registration_fails_the_login_with_a_reason(tmp_path, monkeypatch, era):
    s = oauth_session(tmp_path)

    async with login_server(monkeypatch, era, registration=False) as (_auth, browser):
        result = await s.mcp.connect_server("fixture", interactive=True)

    assert result.startswith("MCP OAuth authentication failed for fixture: ")
    assert "timed out" not in result and "TaskGroup" not in result
    assert browser.opened == []


async def test_a_login_nobody_completes_times_out_and_leaves_nothing_behind(tmp_path, monkeypatch, era):
    """A headless machine: no browser opens, so no callback arrives. The URL is still shown, the
    login ends at the deadline, and the loopback port is released for the next attempt."""
    import webbrowser

    s = oauth_session(tmp_path)
    monkeypatch.setattr(MCPManager, "LOGIN_TIMEOUT", 2)
    s.settings.shell_timeout = 1
    notices: list[str] = []

    async with login_server(monkeypatch, era):
        monkeypatch.setattr(webbrowser, "open", lambda _url: False)
        result = await s.mcp.connect_server("fixture", interactive=True, notify=notices.append)

    # The URL was shown, so the message says how to try again, not that there was no URL.
    assert result == "MCP OAuth authentication failed for fixture: MCP call timed out after 2s\nRun /mcp connect fixture to try again."
    assert len(notices) == 1 and notices[0].startswith("Open this URL to authorize MCP server `fixture`:\nhttp://127.0.0.1:8000/authorize?")
    port = int(parse_qs(urlsplit(notices[0].split("\n", 1)[1]).query)["redirect_uri"][0].rsplit(":", 1)[1].split("/")[0])
    with contextlib.closing(__import__("socket").socket()) as probe:
        probe.bind(("127.0.0.1", port))  # free again


async def test_a_manual_login_may_take_longer_than_shell_timeout(tmp_path, monkeypatch, era):
    """Guards the headless login that timed out after the callback arrived.

    On a headless machine the user opens the link on another computer, signs in, and brings the
    redirect back by hand. The login used to share `shell_timeout` (60s by default) with the
    request it runs inside, so a callback that arrived late in that window was received -- the
    loopback page said "Authorization complete" -- and the login still failed as a timeout. It has
    its own deadline now: here the callback arrives after `shell_timeout` and the login succeeds."""
    import webbrowser

    s = oauth_session(tmp_path)
    s.settings.shell_timeout = 1
    monkeypatch.setattr(MCPManager, "LOGIN_TIMEOUT", 10)

    async with login_server(monkeypatch, era) as (_auth, browser):
        urls: list[str] = []
        monkeypatch.setattr(webbrowser, "open", lambda url: urls.append(url) or False)  # headless
        login = asyncio.create_task(s.mcp.connect_server("fixture", interactive=True))
        while not urls:
            await asyncio.sleep(0.05)
        await asyncio.sleep(2)  # signing in elsewhere, then pasting the redirect into curl
        await asyncio.to_thread(browser.open, urls[0])

        assert await login == CONNECTED


async def test_an_oauth_request_abandoned_mid_flight_leaves_no_error_in_the_loop(tmp_path, monkeypatch, era):
    """Guards the crash reported as "The current task is not holding this lock".

    A deadline or a cancel that lands while an OAuth-authenticated request is in flight makes
    httpx2 close the auth flow. wizolt's flow wraps the SDK's, which holds its context lock across
    the request; left unclosed it was finalized on another task, and the release raised there.
    `no_unhandled_loop_errors` is what fails this test."""
    stall = Stall()
    s = oauth_session(tmp_path)

    async with login_server(monkeypatch, era, stall=stall):
        assert await s.mcp.connect_server("fixture", interactive=True) == CONNECTED
        stall.on = True
        s.settings.shell_timeout = 1

        with pytest.raises(ToolError, match="timed out"):
            await s.mcp.call_tool("fixture", "echo", {"text": "hi"})
        call = asyncio.create_task(s.mcp.call_tool("fixture", "echo", {"text": "hi"}))
        await asyncio.sleep(0.2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        restarted = oauth_session(tmp_path)
        restarted.settings.shell_timeout = 1
        assert await restarted.mcp.connect_server("fixture") == "MCP server error: fixture: MCP call timed out after 1s"


async def test_overlapping_interactive_connects_log_in_once(tmp_path, monkeypatch, era):
    """`/mcp connect` twice, or a batch naming a server twice from two places: one browser login,
    and the second connect uses it rather than discarding it for a login of its own."""
    s = oauth_session(tmp_path)

    async with login_server(monkeypatch, era) as (_auth, browser):
        first, second = await asyncio.gather(
            s.mcp.connect_server("fixture", interactive=True),
            s.mcp.connect_server("fixture", interactive=True),
        )

    assert (first, second) == (CONNECTED, CONNECTED)
    assert len(browser.opened) == 1


async def test_a_call_during_a_login_is_refused_with_the_connect_hint(tmp_path, monkeypatch, era):
    import threading

    s = oauth_session(tmp_path)
    s.mcp.tools["fixture"] = []  # listed earlier in the session, before the login was replaced

    async with login_server(monkeypatch, era) as (_auth, browser):
        opened = threading.Event()
        proceed = threading.Event()
        approve = browser.open

        def slow_user(url):
            opened.set()
            proceed.wait(5)
            return approve(url)

        monkeypatch.setattr(__import__("webbrowser"), "open", slow_user)
        login = asyncio.create_task(s.mcp.connect_server("fixture", interactive=True))
        await asyncio.to_thread(opened.wait, 5)

        with pytest.raises(ToolError, match=r"requires authentication; run /mcp connect fixture"):
            await s.mcp.call_tool("fixture", "echo", {"text": "hi"})
        proceed.set()
        assert await login == CONNECTED
        assert "echo: hi" in await s.mcp.call_tool("fixture", "echo", {"text": "hi"})

