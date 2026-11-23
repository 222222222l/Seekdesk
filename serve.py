"""Read-only loopback static server. No jobs, credentials, or profiles reach this server."""
from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
         "/app.js": ("app.js", "text/javascript"), "/core.js": ("core.js", "text/javascript"),
         "/fixture.js": ("fixture.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'none'; "
          "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.serve()
    def do_HEAD(self):
        self.serve(head=True)
    def do_POST(self):
        self.send_error(405)
    def serve(self, head=False):
        port = self.server.server_port
        if self.headers.get('Host', '').lower() not in {f'localhost:{port}', f'127.0.0.1:{port}'}:
            self.send_error(400)
            return
        if self.path not in FILES:
            self.send_error(404)
            return
        filename, content_type = FILES[self.path]
        data = (ROOT / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", POLICY)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(data)
    def log_message(self, _format, *_args):
        pass

if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port} (local-only; Ctrl+C to stop)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
