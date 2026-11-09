"""Shared HTTPX pooling with one absolute deadline across DNS and socket I/O.

HTTPX 0.28/httpcore 1.x expose per-operation inactivity timeouts. Recompute the
remaining wall-clock allowance for every socket operation instead. Linux's main
thread also uses the invocation alarm; other threads use a lazily reused, owned
DNS helper because socket.getaddrinfo itself has no portable timeout.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import atexit
import ipaddress
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

import httpcore
import httpx

from .timeutil import execution_deadline

RPC_DEADLINE = ContextVar("model_rpc_deadline", default=None)
RPC_OBSERVATION = ContextVar("model_rpc_observation", default=None)


def remaining(timeout=None):
    deadline = RPC_DEADLINE.get()
    if deadline is None:
        return timeout
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("RPC wall-clock deadline reached")
    return seconds if timeout is None else min(timeout, seconds)


@contextmanager
def deadline_scope(deadline, observation=None):
    deadline = min(deadline, RPC_DEADLINE.get() or float("inf"))
    token = RPC_DEADLINE.set(deadline)
    observed = RPC_OBSERVATION.set(observation)
    try:
        with execution_deadline(remaining()):
            yield
    finally:
        RPC_OBSERVATION.reset(observed)
        RPC_DEADLINE.reset(token)


# This child imports only the standard library, receives only DNS names/ports,
# and never receives the API credential or HTTP body. It is reused, not started
# for each RPC. A blocked resolver is killed and reaped by its owning transport.
_RESOLVER_CODE = r'''
import json, socket, sys
connection = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
stream = connection.makefile("rwb", buffering=0)
stream.write((sys.argv[2] + "\n").encode())
for line in stream:
    host, port = json.loads(line)
    try:
        result = {"addresses": socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except OSError as exc:
        result = {"error": str(exc), "errno": exc.errno}
    stream.write((json.dumps(result) + "\n").encode())
'''


class _Resolver:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.connection = None
        self.buffer = b""
        atexit.register(self.close)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            self.process = None
        self.buffer = b""

    def _line(self):
        while b"\n" not in self.buffer:
            self.connection.settimeout(remaining())
            data = self.connection.recv(65536)
            if not data:
                raise OSError("RPC DNS helper closed")
            self.buffer += data
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line

    def _start(self):
        entered = time.monotonic()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            secret = os.urandom(16).hex()
            # Do not inherit provider, Tencent, COS or Feishu credentials.
            environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
            self.process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-u", "-c", _RESOLVER_CODE, str(listener.getsockname()[1]), secret],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            listener.settimeout(remaining())
            self.connection, _ = listener.accept()
            if self._line().decode() != secret:
                raise OSError("RPC DNS helper handshake failed")
        observation = RPC_OBSERVATION.get()
        if observation is not None:
            observation["dns_helper_startup_ms"] = round((time.monotonic() - entered) * 1000, 3)

    def resolve(self, host, port):
        acquired = self.lock.acquire(timeout=remaining())
        if not acquired:
            raise TimeoutError("RPC DNS deadline reached")
        try:
            try:
                if self.process is None or self.process.poll() is not None:
                    self.close()
                    self._start()
                self.connection.settimeout(remaining())
                self.connection.sendall((json.dumps([host, port]) + "\n").encode())
                result = json.loads(self._line())
                remaining()
            except BaseException:
                self.close()
                raise
            if "error" in result:
                raise socket.gaierror(result["errno"], result["error"])
            return [(family, kind, protocol, canon, tuple(address)) for family, kind, protocol, canon, address in result["addresses"]]
        finally:
            self.lock.release()


class DeadlineStream(httpcore.NetworkStream):
    def __init__(self, sock):
        self.socket = sock

    def read(self, max_bytes, timeout=None):
        try:
            self.socket.settimeout(remaining(timeout))
            data = self.socket.recv(max_bytes)
            remaining()
            return data
        except socket.timeout as exc:
            raise httpcore.ReadTimeout("RPC response read deadline reached") from exc
        except OSError as exc:
            raise httpcore.ReadError(str(exc)) from exc

    def write(self, buffer, timeout=None):
        try:
            view = memoryview(buffer)
            while view:
                self.socket.settimeout(remaining(timeout))
                sent = self.socket.send(view)
                if not sent:
                    raise OSError("RPC socket closed during write")
                view = view[sent:]
            remaining()
        except socket.timeout as exc:
            raise httpcore.WriteTimeout("RPC request write deadline reached") from exc
        except OSError as exc:
            raise httpcore.WriteError(str(exc)) from exc

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        try:
            self.socket.settimeout(remaining(timeout))
            self.socket = ssl_context.wrap_socket(self.socket, server_hostname=server_hostname)
            remaining()
            return self
        except socket.timeout as exc:
            self.close()
            raise httpcore.ConnectTimeout("RPC TLS deadline reached") from exc
        except OSError as exc:
            self.close()
            raise httpcore.ConnectError(str(exc)) from exc

    def close(self):
        self.socket.close()

    def get_extra_info(self, info):
        if info == "ssl_object" and isinstance(self.socket, ssl.SSLSocket):
            return self.socket._sslobj
        if info == "socket":
            return self.socket
        if info == "client_addr":
            return self.socket.getsockname()
        if info == "server_addr":
            return self.socket.getpeername()
        if info == "is_readable":
            import select
            return bool(select.select([self.socket], [], [], 0)[0])
        return None


class DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self):
        self.resolver = _Resolver()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            remaining(timeout)
            try:
                ipaddress.ip_address(host)
                numeric = True
            except ValueError:
                numeric = False
            alarm = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
            if numeric or alarm or RPC_DEADLINE.get() is None:
                addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM,
                                               flags=socket.AI_NUMERICHOST if numeric else 0)
            else:
                addresses = self.resolver.resolve(host, port)
            last_error = None
            for family, kind, protocol, _, address in addresses:
                remaining(timeout)
                sock = socket.socket(family, kind, protocol)
                try:
                    for option in socket_options or ():
                        sock.setsockopt(*option)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    if local_address is not None:
                        sock.bind((local_address, 0))
                    sock.settimeout(remaining(timeout))
                    sock.connect(address)
                    remaining()
                    return DeadlineStream(sock)
                except BaseException as exc:
                    sock.close()
                    if not isinstance(exc, OSError):
                        raise
                    last_error = exc
            raise last_error or OSError("no RPC endpoint addresses")
        except socket.timeout as exc:
            raise httpcore.ConnectTimeout("RPC connection deadline reached") from exc
        except OSError as exc:
            raise httpcore.ConnectError(str(exc)) from exc


class DeadlineTransport(httpx.HTTPTransport):
    def __init__(self):
        # Reuse HTTPX 0.28's adapter unchanged, supplying its only constructor
        # state (_pool) with httpcore's public network_backend interface. Avoid
        # constructing a second SSL context or an unused default pool.
        self.backend = DeadlineBackend()
        self._pool = httpcore.ConnectionPool(ssl_context=ssl.create_default_context(),
                                             network_backend=self.backend, retries=0)

    def close(self):
        try:
            super().close()
        finally:
            self.backend.resolver.close()


def create_client():
    # Production uses the official endpoint directly. Environment proxy mounts
    # would bypass the deadline transport, so this client intentionally has none.
    client = httpx.Client(transport=DeadlineTransport(), trust_env=False)
    atexit.register(client.close)
    return client
