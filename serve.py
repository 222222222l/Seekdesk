"""Local job discovery and application workbench."""

import argparse
import errno
import json
import os
import threading
import webbrowser
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from desk.engine import Engine
from desk.storage import Store, digest
from desk.web import DeskHandler, SERVICE_VERSION


@contextmanager
def instance(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "server.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("此数据目录已有工作台在运行，请打开已有窗口。")
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--data-dir", type=Path, default=Path(__file__).resolve().parent / ".workbench" / "personal"
    )
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    port_file = args.data_dir / "port"
    try:
        saved = int(port_file.read_text(encoding="ascii"))
    except (OSError, ValueError):
        saved = 8765
    port = args.port if args.port is not None else saved
    if args.open and port:
        address = f"http://127.0.0.1:{port}/"
        try:
            with urlopen(address + "api/health", timeout=0.5) as response:
                health = json.load(response)
            if health.get("workspace") == digest(str(args.data_dir.resolve())):
                if health.get("version") != SERVICE_VERSION:
                    raise SystemExit("旧版工作台服务仍在运行。请在原启动窗口按 Ctrl+C 退出，再运行「启动工作台.cmd」。")
                webbrowser.open(address)
                return
        except (OSError, ValueError):
            pass

    def listener():
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), DeskHandler)
        except OSError as exc:
            if (
                args.port is not None
                or exc.errno != errno.EADDRINUSE
                and getattr(exc, "winerror", None) != 10048
            ):
                raise
            return ThreadingHTTPServer(("127.0.0.1", 0), DeskHandler)

    with instance(args.data_dir), listener() as server:
        port_file.write_text(str(server.server_port), encoding="ascii")
        server.config_lock = threading.RLock()
        server.store = Store(args.data_dir)
        server.engine = Engine(server.store, server.config_lock)
        threading.Thread(target=server.engine.scheduler, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/"
        print(url, flush=True)
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.engine.stop.set()


if __name__ == "__main__":
    main()
