"""Reject accidental external sockets, including errors swallowed by background tasks.

HTTP mocks and loopback integration servers remain real. This guards the pytest process;
subprocess drivers must separately disable unrelated background checks.
"""

import ipaddress


class NetworkGuard:
    def __init__(self):
        self.active = False
        self.attempts: list[str] = []

    def audit(self, event, arguments):
        if not self.active:
            return
        if event in ("socket.getaddrinfo", "socket.gethostbyname"):
            host = arguments[0]
        elif event == "socket.connect" and isinstance(arguments[1], tuple):
            host = arguments[1][0]
        else:
            return  # Unix-domain sockets and non-network audit events.
        if isinstance(host, bytes):
            host = host.decode("ascii", errors="replace")
        if host in (None, "", "localhost"):
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        message = f"External network blocked: {host}; mock the transport or background check"
        self.attempts.append(message)
        raise OSError(message)
