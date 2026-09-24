"""MCP OAuth login: the SDK's OAuth provider plus the browser hand-off and loopback callback it leaves to the client.

Imports the MCP SDK at module scope, so only `MCPManager` operations import this, off the loop."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import webbrowser
from collections.abc import AsyncGenerator, Callable
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER

from wizolt.base import run_blocking
from wizolt.mcp.tokens import MCPServerTokens

CALLBACK_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>wizolt</title></head>"
    "<body style='font-family:sans-serif'><p>{message}</p><p>You can close this tab and return to wizolt.</p></body></html>"
)


class LoopbackCallback:
    """A one-shot HTTP listener on 127.0.0.1 that receives the authorization server's redirect.

    The port is reserved when this is built, because the redirect URI it goes into is registered
    with the authorization server before the browser is opened."""

    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.redirect_uri = f"http://localhost:{self.port}/callback"
        self.server: asyncio.Server | None = None
        self.result: asyncio.Future[AuthorizationCodeResult] | None = None

    async def start(self) -> None:
        self.result = asyncio.get_running_loop().create_future()
        self.server = await asyncio.start_server(self.handle, sock=self.socket)

    async def wait(self, timeout: float) -> AuthorizationCodeResult:
        assert self.result is not None, "start() before wait()"
        try:
            return await asyncio.wait_for(self.result, timeout)
        except TimeoutError:
            raise TimeoutError(f"no authorization callback within {int(timeout)}s") from None
        finally:
            await self.close()

    async def close(self) -> None:
        # Runs while the operation is being cancelled, which anyio keeps re-delivering to an
        # unshielded await: release the port first, then wait out the connections shielded. The
        # server owns the socket once started, so it closes it; a closed socket first breaks that.
        server, self.server = self.server, None
        if server is not None:
            server.close()
        self.socket.close()
        if server is not None:
            with anyio.CancelScope(shield=True):
                await server.wait_closed()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), 10)
            with contextlib.suppress(TimeoutError):
                while (await asyncio.wait_for(reader.readline(), 10)) not in (b"\r\n", b"\n", b""):
                    pass
            parts = request_line.decode("latin-1").split()
            target = urlsplit(parts[1]) if len(parts) >= 2 else None
            if target is None or target.path != "/callback":
                self.respond(writer, "404 Not Found", "Not found.")
                return
            query = {key: values[0] for key, values in parse_qs(target.query).items()}
            if self.result is None or self.result.done():
                self.respond(writer, "409 Conflict", "This authorization was already handled.")
            elif error := query.get("error"):
                detail = query.get("error_description") or error
                self.result.set_exception(RuntimeError("authorization denied: " + detail))
                self.respond(writer, "400 Bad Request", "Authorization failed: " + detail)
            elif code := query.get("code"):
                self.result.set_result(AuthorizationCodeResult(code=code, state=query.get("state"), iss=query.get("iss")))
                self.respond(writer, "200 OK", "Authorization complete.")
            else:
                self.respond(writer, "400 Bad Request", "The callback carried no authorization code.")
            await writer.drain()
        except (OSError, TimeoutError, UnicodeDecodeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    @staticmethod
    def respond(writer: asyncio.StreamWriter, status: str, message: str) -> None:
        body = CALLBACK_PAGE.format(message=message).encode()
        head = f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        writer.write(head.encode() + body)


class WizoltOAuth(OAuthClientProvider):
    """The SDK provider bound to one server, its stored credentials, and wizolt's login policy.

    Only an interactive `/mcp connect` may start a browser login. Every other operation refreshes
    or fails with the message that tells the user to run it, so a background discovery never opens
    a browser nobody asked for.

    One provider serves one operation, and operations on a server run concurrently -- discovery
    lists tools and resources at once. Left alone, each would refresh an expired token with the
    same refresh token, and a server that rotates refresh tokens rejects every use after the first:
    the loser drops its credentials and fails as unauthenticated. `refresh_gate`, shared by every
    provider for the server, lets one refresh while the rest wait and then load what it stored."""

    def __init__(
        self,
        server_name: str,
        server_url: str,
        storage: MCPServerTokens,
        *,
        interactive: bool,
        notify: Callable[[str], None] | None,
        callback_timeout: float,
        refresh_gate: asyncio.Lock,
    ):
        self.server_name = server_name
        self.storage = storage
        self.refresh_gate = refresh_gate
        self.interactive = interactive
        self.notify = notify
        self.callback_timeout = callback_timeout
        # Only an interactive login ever redirects, so only it reserves a port. The placeholder
        # keeps registration metadata well-formed; nothing listens on it.
        self.callback = LoopbackCallback() if interactive else None
        redirect_uri = self.callback.redirect_uri if self.callback else "http://localhost/callback"
        metadata = OAuthClientMetadata(
            client_name="wizolt",
            redirect_uris=[redirect_uri],  # pyright: ignore[reportArgumentType] - pydantic coerces the str
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        super().__init__(server_url, metadata, storage, redirect_handler=self.redirect, callback_handler=self.receive)

    async def _initialize(self) -> None:
        # The SDK treats a reloaded token as unexpired until the server rejects it, and a rejection
        # starts a full browser login rather than a refresh. Restoring the stored absolute expiry
        # lets an expired access token take the refresh path instead.
        await super()._initialize()
        if self.context.current_tokens and self.context.current_tokens.expires_in:
            self.context.token_expiry_time = await self.storage.get_token_expiry()

    def needs_refresh(self) -> bool:
        return not self.context.is_token_valid() and self.context.can_refresh_token()

    async def _auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        async with self.context.lock:
            if not self._initialized:
                await self._initialize()
            stale = self.needs_refresh()
        if stale:
            async with self.refresh_gate:
                # Another operation may have refreshed while this one waited: its token is stored.
                await self._initialize()
                if self.needs_refresh():
                    self.context.protocol_version = request.headers.get(MCP_PROTOCOL_VERSION_HEADER)
                    await self._handle_refresh_response((yield await self._refresh_token()))
        # The SDK's flow now finds a valid token, or none -- a failed refresh clears it -- and
        # handles the request, including a 401, as it always does.
        flow = super()._auth_flow(request)
        try:
            outgoing = await anext(flow)
            while True:
                outgoing = await flow.asend((yield outgoing))
        except StopAsyncIteration:
            return
        finally:
            # httpx2 closes this flow once it has its response, so the SDK's is closed here, on
            # the same task. Left to the loop's generator finalizer it would release the SDK's
            # context lock from another task: "The current task is not holding this lock".
            await flow.aclose()

    async def redirect(self, authorization_url: str) -> None:
        if self.callback is None:
            raise RuntimeError("authentication required; run /mcp connect " + self.server_name)
        await self.callback.start()
        if self.notify:
            self.notify("Open this URL to authorize MCP server `" + self.server_name + "`:\n" + authorization_url)
        await run_blocking(lambda: webbrowser.open(authorization_url))

    async def receive(self) -> AuthorizationCodeResult:
        assert self.callback is not None, "only an interactive login redirects"
        return await self.callback.wait(self.callback_timeout)

    async def aclose(self) -> None:
        """Release the reserved callback port when the login never reached the browser."""
        if self.callback is not None:
            await self.callback.close()
