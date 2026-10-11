import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol import Frames, command, parse
from server import HTTPHandler, Store, TCPServer, TrackerHandler, TrackerService, ThreadingHTTPServer

IMEI = "862255061947757"
GPS = f"#{IMEI}#MT710#0000#AUTO#1\r\n#3815$GPRMC,123318.00,A,2238.8946,N,11402.0635,E,,,100124,,,A*5C\r\n##"
INVALID = f"#{IMEI}#MT710#0000#AUTO#1\r\n#3815#460,00,1D29,156153D$GPRMC,121831.00,V,,,,,,,100124,,,A*7C\r\n##"
# The PDF prints *06 for this example, but the sentence's XOR is 4F.
WIFI = f"#{IMEI}#MT710#0000#WIFI#1\r\n#3815#460,00,262C,11F1$WIFI,095147.00,V,,,,,,,,,,,241223*4F\r\n##"


def wait_for(fn):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError("Condition did not become true")


class ProtocolTests(unittest.TestCase):
    def test_manufacturer_examples(self):
        report = parse(GPS)
        self.assertAlmostEqual(report["latitude"], 22.6482433)
        self.assertAlmostEqual(report["longitude"], 114.0343917)
        self.assertEqual(report["recorded_at"], "2024-01-10T12:33:18+00:00")
        self.assertEqual(report["voltage"], 3.815)
        self.assertIsNone(parse(INVALID)["latitude"])
        self.assertEqual(parse(INVALID)["cell"], "460,00,1D29,156153D")
        self.assertEqual(parse(WIFI)["source"], "WIFI")

    def test_fragmented_and_combined_frames(self):
        frames = Frames()
        self.assertEqual(frames.feed(GPS[:50].encode()), [])
        result = frames.feed((GPS[50:] + "\r\n" + INVALID).encode())
        self.assertEqual([parse(frame)["fix"] for frame in result], ["A", "V"])
        with self.assertRaises(ValueError):
            Frames().feed(b"x" * 9000)

    def test_corrupt_and_invalid_coordinates(self):
        with self.assertRaises(ValueError):
            parse(GPS.replace("*5C", "*00"))
        from protocol import coordinate
        for value, hemisphere in [("2260.0", "N"), ("9100.0", "N"), ("nan", "N"), ("2238", "")]:
            with self.assertRaises(ValueError):
                coordinate(value, hemisphere, True)
        self.assertLess(coordinate("2238.8946", "S", True), 0)

    def test_command_ranges_and_units(self):
        self.assertEqual(command({"kind": "mode", "mode": 0, "period": 30, "still_hours": 2}), "MODE,0,30,2")
        self.assertEqual(command({"kind": "mode", "mode": 2, "period": 10}), "MODE,2,10,0,1")
        self.assertEqual(command({"kind": "mode", "mode": 3, "period": 24}), "MODE,3,24")
        for mode, period in [(1, 9), (1, 601), (2, 1), (3, 25), (7, 1441), (6, 30), (True, 30)]:
            with self.assertRaises(ValueError):
                command({"kind": "mode", "mode": mode, "period": period})


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "tracker.sqlite3")
        self.service = TrackerService(self.store, IMEI)
        self.tcp = TCPServer(("127.0.0.1", 0), TrackerHandler)
        self.tcp.service = self.service
        self.web = ThreadingHTTPServer(("127.0.0.1", 0), HTTPHandler)
        self.web.service, self.web.token = self.service, "test-token-long-enough"
        self.web.dog_name, self.web.demo = "Test dog", False
        self.threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (self.tcp, self.web)]
        for thread in self.threads:
            thread.start()
        self.connections = []

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        wait_for(lambda: not self.service.state()["connected"])
        for server in (self.tcp, self.web):
            server.shutdown()
            server.server_close()
        self.store.db.close()
        self.temp.cleanup()

    def api(self, path, body=None, token=None):
        headers = {"Authorization": "Bearer " + (self.web.token if token is None else token)}
        request = Request(f"http://127.0.0.1:{self.web.server_port}{path}",
                          data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def connect(self):
        connection = socket.create_connection(self.tcp.server_address)
        connection.settimeout(3)
        self.connections.append(connection)
        connection.sendall(GPS[:70].encode())
        connection.sendall(GPS[70:].encode())
        wait_for(lambda: self.service.state()["connected"])
        return connection

    def status(self, id):
        return self.store.query("SELECT status FROM commands WHERE id=?", (id,))[0]["status"]

    def test_browser_to_device_and_history(self):
        with self.assertRaises(HTTPError) as error:
            self.api("/api/state", token="wrong")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        id = self.api("/api/commands", {"kind": "mode", "mode": 3, "period": 2})["id"]
        self.assertEqual(self.status(id), "queued")
        connection = self.connect()
        self.assertEqual(connection.recv(4096), b"MODE,3,2\r\n")
        self.assertEqual(self.status(id), "sent")
        connection.sendall(f"#{IMEI}#REPLY#LEP,OK##".encode())
        time.sleep(.05)
        self.assertEqual(self.status(id), "sent")
        connection.sendall(f"#{IMEI}#REPLY#MODE,OK##".encode())
        wait_for(lambda: self.status(id) == "confirmed")
        connection.sendall((INVALID + WIFI).encode())
        wait_for(lambda: len(self.store.query("SELECT * FROM reports")) == 3)
        state = self.api("/api/state")
        self.assertEqual(state["position"]["fix"], "A")
        self.assertEqual(state["latest"]["source"], "WIFI")
        self.assertEqual(len(self.api("/api/history")), 1)
        self.assertEqual(state["last_confirmed_mode"]["text"], "MODE,3,2")

    def test_dashboard_allows_map_tile_referrer(self):
        with urlopen(f"http://127.0.0.1:{self.web.server_port}/", timeout=3) as response:
            self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")

    def test_queue_cancellation_rejection_and_disconnect(self):
        id = self.api("/api/commands", {"kind": "mode", "mode": 1, "period": 30})["id"]
        self.api("/api/cancel", {"id": id})
        self.assertEqual(self.status(id), "cancelled")
        connection = self.connect()
        id = self.api("/api/commands", {"kind": "last_position", "enabled": 1})["id"]
        self.assertEqual(connection.recv(4096), b"LEP,1\r\n")
        connection.sendall(f"#{IMEI}#REPLY#LEP,FS##".encode())
        wait_for(lambda: self.status(id) == "failed")
        id = self.api("/api/commands", {"kind": "mode", "mode": 0, "period": 30, "still_hours": 1})["id"]
        self.assertEqual(connection.recv(4096), b"MODE,0,30,1\r\n")
        connection.close()
        wait_for(lambda: self.status(id) == "unknown")
        connection = self.connect()
        self.assertEqual(self.status(id), "unknown")

    def test_read_config_is_correlated_by_page(self):
        connection = self.connect()
        id = self.api("/api/commands", {"kind": "read_config", "page": 2})["id"]
        self.assertEqual(connection.recv(4096), b"RCONF,2\r\n")
        connection.sendall(f"#{IMEI}#REPLY#RCONF,1#wrong page##".encode())
        time.sleep(.05)
        self.assertEqual(self.status(id), "sent")
        connection.sendall(f"#{IMEI}#REPLY#RCONF,2#MODE:0,30,1##".encode())
        wait_for(lambda: self.status(id) == "confirmed")

    def test_unknown_imei_and_invalid_web_command(self):
        connection = socket.create_connection(self.tcp.server_address)
        self.connections.append(connection)
        connection.settimeout(3)
        connection.sendall(GPS.replace(IMEI, "111111111111111").encode())
        self.assertEqual(connection.recv(1), b"")
        self.assertEqual(self.store.query("SELECT * FROM reports"), [])
        with self.assertRaises(HTTPError) as error:
            self.api("/api/commands", {"kind": "mode", "mode": 3, "period": 0})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_timeout_and_restart_do_not_resend(self):
        connection = self.connect()
        id = self.api("/api/commands", {"kind": "mode", "mode": 1, "period": 30})["id"]
        connection.recv(4096)
        with self.service.lock:
            self.service.inflight["sent_at"] -= 31
        wait_for(lambda: self.status(id) == "unknown")
        self.assertEqual(connection.recv(1), b"")
        id = self.store.enqueue("MODE,1,60")
        with self.store.lock:
            self.store.db.execute("UPDATE commands SET status='sent' WHERE id=?", (id,))
            self.store.db.commit()
        restarted = Store(Path(self.temp.name) / "tracker.sqlite3")
        self.assertEqual(restarted.query("SELECT status FROM commands WHERE id=?", (id,))[0]["status"], "unknown")
        restarted.db.close()


if __name__ == "__main__":
    unittest.main()
