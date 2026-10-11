#!/usr/bin/env python3
"""Single-dog MT710 receiver and mobile dashboard. Python 3.11+."""
import argparse
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import socketserver
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from protocol import Frames, command, parse

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("dog-tracker")


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY, received_at REAL NOT NULL,
                recorded_at TEXT NOT NULL, event TEXT, voltage REAL,
                latitude REAL, longitude REAL, speed_kmh REAL,
                fix TEXT, source TEXT, cell TEXT, raw TEXT);
            CREATE INDEX IF NOT EXISTS reports_time ON reports(received_at);
            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
                sent_at REAL, finished_at REAL, text TEXT NOT NULL,
                status TEXT NOT NULL, reply TEXT);
        """)
        self.db.execute("UPDATE commands SET status='unknown', reply='Server restarted before confirmation' WHERE status='sent'")
        self.db.commit()

    def query(self, sql, params=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, params)]

    def report(self, report):
        keys = "recorded_at event voltage latitude longitude speed_kmh fix source cell raw".split()
        with self.lock:
            self.db.execute(f"INSERT INTO reports (received_at,{','.join(keys)}) VALUES ({','.join('?' for _ in range(len(keys)+1))})",
                            [time.time()] + [report[k] for k in keys])
            # Keep 30 days of history. Latest known coordinate survives gaps.
            self.db.execute("DELETE FROM reports WHERE received_at < ? AND id != (SELECT MAX(id) FROM reports WHERE latitude IS NOT NULL)", (time.time() - 30 * 86400,))
            self.db.commit()

    def enqueue(self, text):
        with self.lock:
            if self.db.execute("SELECT COUNT(*) FROM commands WHERE status IN ('queued','sent')").fetchone()[0] >= 20:
                raise ValueError("Command queue is full; cancel queued commands first")
            cursor = self.db.execute("INSERT INTO commands(created_at,text,status) VALUES (?,?,'queued')", (time.time(), text))
            self.db.commit()
            return cursor.lastrowid

    def update(self, id, status, reply=None):
        with self.lock:
            self.db.execute("UPDATE commands SET status=?, reply=?, finished_at=? WHERE id=?", (status, reply, time.time(), id))
            self.db.commit()

    def cancel(self, id):
        with self.lock:
            cursor = self.db.execute("UPDATE commands SET status='cancelled',finished_at=? WHERE id=? AND status='queued'", (time.time(), id))
            self.db.commit()
            return cursor.rowcount


class TrackerService:
    def __init__(self, store, imei):
        self.store, self.imei = store, imei
        self.lock = threading.RLock()
        self.connection = None
        self.connected_at = None
        self.inflight = None

    def attach(self, connection):
        with self.lock:
            if self.connection is not connection:
                if self.connection:
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                self.uncertain("Tracker reconnected before confirmation")
                self.connection = connection
                self.connected_at = time.time()

    def uncertain(self, reason):
        if self.inflight:
            self.store.update(self.inflight["id"], "unknown", reason)
            self.inflight = None

    def detach(self, connection):
        with self.lock:
            if self.connection is connection:
                self.uncertain("Connection closed before confirmation; read config to check")
                self.connection = None

    def dispatch(self, connection):
        with self.lock, self.store.lock:
            if self.connection is not connection:
                return
            if self.inflight:
                if time.time() - self.inflight["sent_at"] > 30:
                    self.uncertain("No reply within 30 seconds; read config to check")
                    connection.shutdown(socket.SHUT_RDWR)
                return
            rows = self.store.query("SELECT * FROM commands WHERE status='queued' ORDER BY id LIMIT 1")
            if not rows:
                return
            row = rows[0]
            row["sent_at"] = time.time()
            self.store.db.execute("UPDATE commands SET status='sent',sent_at=? WHERE id=?", (row["sent_at"], row["id"]))
            self.store.db.commit()
            self.inflight = row
            connection.sendall((row["text"] + "\r\n").encode("ascii"))

    def receive(self, message, connection):
        if message["imei"] != self.imei:
            raise ValueError("Unregistered tracker IMEI")
        self.attach(connection)
        with self.lock:
            if message["kind"] == "report":
                self.store.report(message)
            elif self.inflight:
                text = self.inflight["text"]
                reply = message["reply"]
                matches = reply.startswith(text + "#") if text.startswith("RCONF,") else reply in (text.split(",")[0] + ",OK", text.split(",")[0] + ",FS")
                if matches:
                    self.store.update(self.inflight["id"], "failed" if reply.endswith(",FS") else "confirmed", reply)
                    self.inflight = None
        self.dispatch(connection)

    def state(self):
        with self.lock:
            return {"connected": self.connection is not None, "connected_at": self.connected_at}


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 16


class TrackerHandler(socketserver.BaseRequestHandler):
    def handle(self):
        service = self.server.service
        frames = Frames()
        self.request.settimeout(0.5)
        self.request.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        started = time.monotonic()
        last_data = started
        identified = False
        try:
            while True:
                try:
                    data = self.request.recv(4096)
                    if not data:
                        break
                    last_data = time.monotonic()
                    for frame in frames.feed(data):
                        service.receive(parse(frame), self.request)
                        identified = True
                except socket.timeout:
                    pass
                if not identified and time.monotonic() - started > 15:
                    break
                # GPS-auto mode can report only once per hour with TCP kept up.
                if time.monotonic() - last_data > 7200:
                    break
                service.dispatch(self.request)
        except (ValueError, IndexError, UnicodeError, OSError) as error:
            LOG.info("Tracker connection closed: %s", error)
        finally:
            service.detach(self.request)


class HTTPHandler(BaseHTTPRequestHandler):
    server_version = "DogTracker/1.0"

    def log_message(self, format, *args):
        LOG.info("Web: " + format, *args)

    def respond(self, code, value, content_type="application/json"):
        data = json.dumps(value).encode() if content_type == "application/json" else value
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return hmac.compare_digest(token.encode(), self.server.token.encode())

    def do_GET(self):
        path = urlsplit(self.path).path
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "application/javascript"),
                  "/style.css": ("style.css", "text/css")}
        if path in assets:
            file, mime = assets[path]
            return self.respond(200, (ROOT / "static" / file).read_bytes(), mime)
        if not self.authorized():
            return self.respond(401, {"error": "Enter your access token"})
        store = self.server.service.store
        if path == "/api/state":
            latest = store.query("SELECT * FROM reports ORDER BY id DESC LIMIT 1")
            position = store.query("SELECT * FROM reports WHERE latitude IS NOT NULL ORDER BY recorded_at DESC,id DESC LIMIT 1")
            confirmed = store.query("SELECT * FROM commands WHERE status='confirmed' AND text LIKE 'MODE,%' ORDER BY id DESC LIMIT 1")
            return self.respond(200, {"name": self.server.dog_name, "imei": self.server.service.imei,
                "demo": self.server.demo, **self.server.service.state(),
                "latest": latest[0] if latest else None, "position": position[0] if position else None,
                "last_confirmed_mode": confirmed[0] if confirmed else None,
                "commands": store.query("SELECT * FROM commands ORDER BY id DESC LIMIT 30")})
        if path == "/api/history":
            rows = store.query("SELECT latitude,longitude,recorded_at,fix FROM reports WHERE received_at>? AND latitude IS NOT NULL AND fix='A' ORDER BY recorded_at DESC,id DESC LIMIT 2000", (time.time() - 86400,))
            return self.respond(200, list(reversed(rows)))
        self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if not self.authorized():
            return self.respond(401, {"error": "Invalid access token"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid request size")
            self.connection.settimeout(10)
            settings = json.loads(self.rfile.read(length))
            if not isinstance(settings, dict):
                raise ValueError("Expected a JSON object")
            path = urlsplit(self.path).path
            if path == "/api/commands":
                text = command(settings)
                id = self.server.service.store.enqueue(text)
                return self.respond(202, {"id": id, "text": text, "status": "queued"})
            if path == "/api/cancel":
                id = settings.get("id")
                if not isinstance(id, int):
                    raise ValueError("Invalid command ID")
                if not self.server.service.store.cancel(id):
                    raise ValueError("Only queued commands can be cancelled")
                return self.respond(200, {"status": "cancelled"})
            self.respond(404, {"error": "Not found"})
        except (ValueError, TypeError, socket.timeout) as error:
            self.respond(400, {"error": str(error)})


def demo_tracker(port, imei, stop):
    """Exercise the real receiver with a clearly labelled simulated dog."""
    import math
    index = 0
    while not stop.is_set():
        try:
            with socket.create_connection(("127.0.0.1", port)) as connection:
                connection.settimeout(1)
                buffer = b""
                while not stop.is_set():
                    angle = index / 12
                    lat = 50.087 + math.sin(angle) * .001
                    lon = 14.421 + math.cos(angle) * .0015
                    def nmea(value, width):
                        degrees = int(value)
                        return f"{degrees:0{width}d}{(value-degrees)*60:07.4f}"
                    now = time.gmtime()
                    body = f"GPRMC,{time.strftime('%H%M%S', now)}.00,A,{nmea(lat,2)},N,{nmea(lon,3)},E,2.5,90,{time.strftime('%d%m%y',now)},,,A"
                    checksum = 0
                    for char in body:
                        checksum ^= ord(char)
                    connection.sendall(f"#{imei}#MT710#0000#AUTO#1\r\n#3950${body}*{checksum:02X}\r\n##".encode())
                    index += 1
                    deadline = time.monotonic() + 5
                    while not stop.is_set() and time.monotonic() < deadline:
                        try:
                            data = connection.recv(4096)
                            if not data:
                                raise ConnectionError("Server disconnected")
                            buffer += data
                            while b"\r\n" in buffer:
                                line, buffer = buffer.split(b"\r\n", 1)
                                text = line.decode()
                                reply = text + "#DEMO configuration response" if text.startswith("RCONF,") else text.split(",")[0] + ",OK"
                                connection.sendall(f"#{imei}#REPLY#{reply}##".encode())
                        except socket.timeout:
                            pass
        except OSError:
            stop.wait(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--imei", default=os.getenv("TRACKER_IMEI"))
    parser.add_argument("--web-port", type=int, default=int(os.getenv("WEB_PORT", "8080")))
    parser.add_argument("--tracker-port", type=int, default=int(os.getenv("TRACKER_PORT", "7700")))
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "0.0.0.0"))
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.demo:
        args.imei = "123456789012345"
    if not args.imei or len(args.imei) != 15 or not args.imei.isascii() or not args.imei.isdigit():
        parser.error("Provide the tracker's 15-digit IMEI with --imei or TRACKER_IMEI (or use --demo)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    data = args.data_dir or ROOT / ("demo-data" if args.demo else "data")
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = os.getenv("DOGTRACK_TOKEN")
    if not token:
        token_path = data / "access-token"
        if not token_path.exists():
            with open(token_path, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
                file.write(secrets.token_urlsafe(24))
        token = token_path.read_text().strip()
        print(f"Access token: {token}", flush=True)
    if len(token) < 12 or not token.isascii():
        parser.error("DOGTRACK_TOKEN must contain at least 12 ASCII characters")
    store = Store(data / "tracker.sqlite3")
    # A data directory belongs to one tracker; prevent mixing real/different dogs.
    identity = data / "imei"
    if identity.exists() and identity.read_text().strip() != args.imei:
        parser.error("Data directory belongs to another IMEI; choose a different --data-dir")
    identity.write_text(args.imei)
    service = TrackerService(store, args.imei)
    tcp = TCPServer(("127.0.0.1" if args.demo else "0.0.0.0", args.tracker_port), TrackerHandler)
    tcp.service = service
    web = ThreadingHTTPServer((args.host, args.web_port), HTTPHandler)
    web.service, web.token = service, token
    web.dog_name, web.demo = os.getenv("DOG_NAME", "My dog"), args.demo
    threading.Thread(target=tcp.serve_forever, daemon=True).start()
    stop = threading.Event()
    if args.demo:
        threading.Thread(target=demo_tracker, args=(tcp.server_address[1], args.imei, stop), daemon=True).start()
    print(f"Dashboard: http://localhost:{web.server_address[1]} | MT710 TCP: {tcp.server_address[1]}", flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        tcp.shutdown()
        tcp.server_close()
        web.server_close()


if __name__ == "__main__":
    main()
