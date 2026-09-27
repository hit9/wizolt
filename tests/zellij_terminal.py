"""A real Zellij client attached to a resizable PTY; no enclosing multiplexer."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path


class ZellijPane:
    def __init__(self, path: Path, runtime: Path):
        self.path = path
        self.session = f"wizolt-{os.getpid()}-{runtime.name}"
        self.env = dict(os.environ)
        for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID", "TMUX", "TMUX_PANE"):
            self.env.pop(key, None)
        self.env.update(
            TERM="xterm-256color",
            XDG_RUNTIME_DIR=str(runtime),
            ZELLIJ_SOCKET_DIR=str(runtime),
            XDG_CONFIG_HOME=str(path / "config"),
            XDG_CACHE_HOME=str(path / "cache"),
            XDG_DATA_HOME=str(path / "data"),
            PS1="READY> ",
        )
        # Zellij logs use TMPDIR, not XDG_DATA_HOME. Keep them in CI's failure artifacts.
        temporary = path / "tmp"
        temporary.mkdir()
        self.env["TMPDIR"] = str(temporary)
        self.config = path / "zellij.kdl"
        self.config.write_text(
            "pane_frames false\nsimplified_ui true\nshow_startup_tips false\n"
            "show_release_notes false\nsession_serialization false\n"
            'default_shell "sh"\nscroll_buffer_size 20000\nload_plugins clear-defaults=true {}\n'
        )
        self.layout = path / "layout.kdl"
        self.layout.write_text("layout { default_tab_template { children; }; pane; }\n")
        self.raw = bytearray()
        self.stopping = threading.Event()
        self.attach(100, 30, create=True)
        try:
            self._wait_for_startup()
            self.wait_for("READY>")
        except BaseException as error:
            (self.path / "startup-error.txt").write_text(str(error))
            try:
                self.close()
            except (OSError, subprocess.SubprocessError) as cleanup_error:
                error.add_note(f"Zellij cleanup failed: {cleanup_error}")
            raise

    def _wait_for_startup(self, timeout=15):
        # Do not query session discovery before the shell is rendered. In Zellij 0.45.1,
        # assert_socket() unlinks a socket if connect() races bind()/listen() and is refused.
        # Observing the attached PTY leaves server startup alone; wait_for then checks the
        # actual pane snapshot, so terminal escape sequences cannot satisfy that assertion.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            returncode = self.client.poll()
            if returncode is not None:
                raise AssertionError(f"Zellij client exited during startup: returncode={returncode}; client: {bytes(self.raw[-4000:])!r}")
            if b"READY>" in self.raw:
                return
            time.sleep(0.05)
        raise AssertionError(f"Zellij startup timed out after {timeout}s; returncode={self.client.poll()}; client: {bytes(self.raw[-4000:])!r}")

    def attach(self, width, height, *, create=False):
        self.master, slave = pty.openpty()
        # Zellij 0.45 reserves a native header/footer even without status-bar plugins.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height + 2, width, 0, 0))
        args = ["zellij", "--config", str(self.config)]
        if create:
            args += ["--session", self.session, "--new-session-with-layout", str(self.layout)]
        else:
            args += ["attach", self.session]

        self.client = subprocess.Popen(
            args,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=self.path,
            env=self.env,
            start_new_session=True,
        )
        os.close(slave)
        self.stopping.clear()
        self.reader = threading.Thread(target=self._drain, daemon=True)
        self.reader.start()

    def _drain(self):
        while not self.stopping.is_set():
            try:
                if select.select([self.master], [], [], 0.1)[0]:
                    chunk = os.read(self.master, 65536)
                    if not chunk:
                        return
                    self.raw.extend(chunk)
            except OSError:
                return

    def command(self, *args, check=True):
        result = subprocess.run(
            ["zellij", "--config", str(self.config), "--session", self.session, *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if check and result.returncode:
            raise AssertionError(f"zellij {args}: {result.stderr}")
        return result.stdout

    def action(self, *args):
        return self.command("action", *args)

    def capture(self, *, full=True):
        # dump-screen joins soft-wrapped viewport rows too. Subscriptions preserve physical
        # viewport rows (needed by the wide-glyph/tab test); scrollback is logical lines.
        # Do not synthesize history wrapping: that would test our model instead of Zellij.
        focused = next((p for p in self.geometry() if p["is_focused"] and not p["is_plugin"]), None)
        assert focused is not None, "Zellij has no focused terminal yet"
        args = ["zellij", "--session", self.session, "subscribe", "--pane-id", f"terminal_{focused['id']}", "--format", "json"]
        if full:
            args.append("--scrollback")
        process = subprocess.Popen(args, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            data = bytearray()
            deadline = time.monotonic() + 10
            while b"\n" not in data and time.monotonic() < deadline:
                if select.select([process.stdout], [], [], 0.1)[0]:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise AssertionError("Zellij subscription closed without a snapshot")
                    data.extend(chunk)
            if b"\n" not in data:
                raise AssertionError("Zellij subscription timed out")
            snapshot = json.loads(data.split(b"\n", 1)[0])
            assert snapshot["event"] == "pane_update" and snapshot["is_initial"], snapshot
            return [line.rstrip() for line in (snapshot.get("scrollback") or []) + snapshot["viewport"]]
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    def send(self, text):
        self.action("write-chars", text)
        self.action("write", "13")

    def keys(self, *keys):
        for key in keys:
            if key in {"C-u", "Enter", "Escape"}:
                self.action("write", str({"C-u": 21, "Enter": 13, "Escape": 27}[key]))
            else:
                self.literal(key)

    def literal(self, text):
        self.action("write-chars", text)

    def visible(self):
        return "\n".join(self.capture(full=False))

    def fresh_window(self):
        focused = next(p for p in self.geometry() if p["is_focused"] and not p["is_plugin"])
        self.action(
            "new-pane",
            "--pane-id",
            f"terminal_{focused['id']}",
            "--in-place",
            "--close-replaced-pane",
            "--cwd",
            str(self.path),
            "--",
            "env",
            "PS1=FRESH-READY> ",
            "sh",
        )

    def split(self, *, vertical=False):
        self.action("new-pane", "--direction", "down" if vertical else "right", "--no-focus", "--", "sh")

    def zoom(self):
        self.action("toggle-fullscreen")

    def resize(self, width, height):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", height + 2, width, 0, 0))
        os.kill(self.client.pid, signal.SIGWINCH)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            panes = [p for p in self.geometry() if not p["is_plugin"] and not p["is_suppressed"]]
            if panes and max(p["pane_x"] + p["pane_columns"] for p in panes) == width and max(p["pane_y"] + p["pane_rows"] for p in panes) == height + 1:
                return
            time.sleep(0.02)
        raise AssertionError(f"Zellij did not resize to {width}x{height}: {panes}")

    def wait_for(self, text, timeout=15):
        deadline = time.monotonic() + timeout
        last = ""
        last_error = None
        while time.monotonic() < deadline:
            try:
                last = "\n".join(self.capture(full=False))
                if text in last:
                    return last
            except (AssertionError, subprocess.TimeoutExpired) as error:
                # A query can race server startup. Keep the existing readiness deadline and
                # report the last query failure if the client never becomes ready.
                last_error = error
            time.sleep(0.05)
        raise AssertionError(f"missing {text!r}:\n{last}\nlast query error: {last_error}\nclient: {bytes(self.raw[-4000:])!r}")

    def geometry(self):
        # CI has observed list-panes exit successfully without a response. Retry only this
        # read-only query, never input/resize actions; malformed JSON and CLI errors stay loud.
        for attempt in range(5):
            response = self.action("list-panes", "--json")
            if response.strip():
                return json.loads(response)
            if attempt < 4:
                time.sleep(0.05 * (attempt + 1))
        raise AssertionError(f"Zellij {self.session}: list-panes returned empty output after 5 attempts")

    def detach(self):
        # CLI `action detach` targets its own ephemeral client. Send the default session-mode
        # detach keys to the actual attached client so the session survives without a viewer.
        os.write(self.master, b"\x0fd")
        self.client.wait(timeout=10)
        self._close_client()

    def _close_client(self):
        if self.client.poll() is None:
            self.client.terminate()
            try:
                self.client.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.client.kill()
                self.client.wait(timeout=5)
        self.stopping.set()
        self.reader.join(timeout=2)
        if self.master is not None:
            os.close(self.master)
            self.master = None

    def close(self):
        try:
            self.command("kill-session", self.session, check=False)
        finally:
            self._close_client()
            (self.path / "client.ansi").write_bytes(self.raw)
