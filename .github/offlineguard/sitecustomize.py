"""Block IP socket connections during public, synthetic-only backup tests.

The public GitHub CI must not connect to production or any other remote
storage endpoint. Loaded automatically with PYTHONPATH=.../offlineguard.
This is a test fail-safe, not a general-purpose sandbox.
"""
import socket

_OriginalSocket = socket.socket


class OfflineSocket(_OriginalSocket):
    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("OFFLINE_TEST_BLOCKED_NETWORK")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("OFFLINE_TEST_BLOCKED_NETWORK")
        return super().connect_ex(address)


socket.socket = OfflineSocket
