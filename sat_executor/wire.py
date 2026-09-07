"""wire.py: TCP socket RPC server for sat_executor."""

import json
import socket
import socketserver
from sat_executor.core import SATExecutorCore

CORE = SATExecutorCore()


class SATExecutorHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            data_str = self.request.recv(8192).decode("utf-8").strip()
            if not data_str:
                return
            req = json.loads(data_str)
            res = CORE.handle_command(req)
            self.request.sendall(json.dumps(res).encode("utf-8"))
        except Exception as e:
            err_resp = {"ok": False, "error": str(e)}
            self.request.sendall(json.dumps(err_resp).encode("utf-8"))


def start_server(host: str = "127.0.0.1", port: int = 9876):
    server = socketserver.TCPServer((host, port), SATExecutorHandler)
    server.allow_reuse_address = True
    print(f"[*] sat_executor TCP server listening on {host}:{port}...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[*] Shutting down sat_executor...")
        server.server_close()
