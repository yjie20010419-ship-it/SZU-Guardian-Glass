"""Keep one guardian per user: a second launch wakes the first one instead."""

from __future__ import annotations

import getpass
import re
from typing import Callable

from PySide6.QtNetwork import QLocalServer, QLocalSocket

MESSAGE_SHOW = b"show"
MESSAGE_AUTOSTART = b"autostart"


def default_key() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - any lookup failure falls back to a fixed key
        user = "user"
    return "SZUNetworkGuardian-" + re.sub(r"[^A-Za-z0-9_.-]", "_", user)


class SingleInstance:
    def __init__(self, key: str | None = None):
        self.key = key or default_key()
        self.server: QLocalServer | None = None

    def notify_existing(self, message: bytes = MESSAGE_SHOW, timeout_ms: int = 600) -> bool:
        """Tell an already-running guardian about this launch.

        Returns True when one answered, meaning this process should exit.
        """
        socket = QLocalSocket()
        socket.connectToServer(self.key)
        if not socket.waitForConnected(timeout_ms):
            return False
        socket.write(message)
        socket.flush()
        socket.waitForBytesWritten(timeout_ms)
        socket.disconnectFromServer()
        return True

    def listen(self, on_message: Callable[[bytes], None]) -> bool:
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self.key):
            # A crashed instance can leave a stale endpoint behind.
            QLocalServer.removeServer(self.key)
            if not server.listen(self.key):
                return False

        def accept() -> None:
            while server.hasPendingConnections():
                connection = server.nextPendingConnection()
                buffer = bytearray()
                state = {"done": False}

                def collect(connection=connection, buffer=buffer) -> None:
                    buffer.extend(bytes(connection.readAll().data()))

                def finish(connection=connection, buffer=buffer, state=state) -> None:
                    if state["done"]:
                        return
                    state["done"] = True
                    collect()
                    connection.deleteLater()
                    message = bytes(buffer).strip()
                    if message:
                        on_message(message)

                connection.readyRead.connect(collect)
                connection.disconnected.connect(finish)
                # The sender may already have written and hung up.
                collect()
                if connection.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                    finish()

        server.newConnection.connect(accept)
        self.server = server
        return True
