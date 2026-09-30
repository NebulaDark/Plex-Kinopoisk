"""Wait until the host owns the address used by Docker's published port."""

import os
import socket
import time


address = os.environ.get("BIND_ADDRESS", "127.0.0.1")
deadline = time.monotonic() + 90

while True:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
        break
    except OSError as error:
        if time.monotonic() >= deadline:
            raise SystemExit(f"Address {address} is still unavailable: {error}") from error
        time.sleep(1)
