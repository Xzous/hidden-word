#!/usr/bin/env python3
"""
Local WiFi relay server for Detective (The Hidden Word) game.
Serves the HTML file and provides a polling-based message relay API.
No external dependencies — stdlib only.

Usage:
    python serve.py

Then open the printed URL on each player's phone (same WiFi network).
"""

import http.server
import json
import os
import socket
import threading
import time
import urllib.parse

PORT = 8080
ROOM_TIMEOUT = 30 * 60      # 30 minutes — auto-delete inactive rooms
MSG_TTL = 60                 # Keep messages for 60 seconds only
CLEANUP_INTERVAL = 30        # Run cleanup every 30 seconds

# In-memory store
# rooms = { code: { players: { name: last_poll_time }, host: name, msgs: [(ts, from, to, msg), ...], created: time } }
rooms = {}
rooms_lock = threading.Lock()


def get_local_ip():
    """Get the local WiFi IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't actually send anything — just forces the OS to pick the right interface
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def cleanup_rooms():
    """Periodically remove stale rooms and old messages."""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        with rooms_lock:
            dead = [code for code, room in rooms.items()
                    if now - room["created"] > ROOM_TIMEOUT
                    and all(now - t > ROOM_TIMEOUT for t in room["players"].values())]
            for code in dead:
                del rooms[code]
            # Trim old messages in surviving rooms
            for room in rooms.values():
                cutoff = (now - MSG_TTL) * 1000  # msgs use ms timestamps
                room["msgs"] = [m for m in room["msgs"] if m[0] > cutoff]


class Handler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler for serving the game and the relay API."""

    def log_message(self, format, *args):
        # Quieter logging — only show API calls
        msg = format % args
        if "/api/" in msg:
            print(f"  {msg}")

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            # Serve hidden_word.html
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hidden_word.html")
            try:
                with open(html_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Local-Server", "true")
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_json({"error": "hidden_word.html not found"}, 404)
        else:
            # Let the default handler serve other static files
            super().do_GET()

    def do_POST(self):
        if self.path != "/api/room":
            self.send_json({"error": "Not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except (json.JSONDecodeError, ValueError):
            self.send_json({"error": "Invalid JSON"}, 400)
            return

        action = body.get("action", "")
        room_code = body.get("room", "").upper()

        if action == "create":
            name = body.get("name", "").strip()
            if not room_code or not name:
                self.send_json({"error": "Missing room or name"}, 400)
                return
            with rooms_lock:
                if room_code in rooms:
                    self.send_json({"error": "Room already exists"}, 409)
                    return
                rooms[room_code] = {
                    "players": {name: time.time()},
                    "host": name,
                    "msgs": [],
                    "created": time.time(),
                }
            print(f"  [ROOM] Created: {room_code} by {name}")
            self.send_json({"ok": True})

        elif action == "join":
            name = body.get("name", "").strip()
            if not room_code or not name:
                self.send_json({"error": "Missing room or name"}, 400)
                return
            with rooms_lock:
                if room_code not in rooms:
                    self.send_json({"error": "Room not found"}, 404)
                    return
                room = rooms[room_code]
                if name in room["players"]:
                    # Allow rejoin — just update timestamp
                    room["players"][name] = time.time()
                else:
                    room["players"][name] = time.time()
            print(f"  [ROOM] {name} joined {room_code}")
            self.send_json({"ok": True, "host": False})

        elif action == "send":
            from_name = body.get("from", "").strip()
            to_name = body.get("to", "")
            msg = body.get("msg", {})
            if not room_code or not from_name:
                self.send_json({"error": "Missing fields"}, 400)
                return
            now_ms = int(time.time() * 1000)
            with rooms_lock:
                if room_code not in rooms:
                    self.send_json({"error": "Room not found"}, 404)
                    return
                room = rooms[room_code]
                # Route __HOST__ to the room's host name
                if to_name == "__HOST__":
                    to_name = room["host"]
                room["msgs"].append((now_ms, from_name, to_name, msg))
            self.send_json({"ok": True})

        elif action == "poll":
            name = body.get("name", "").strip()
            since = body.get("since", 0)
            if not room_code or not name:
                self.send_json({"error": "Missing fields"}, 400)
                return
            with rooms_lock:
                if room_code not in rooms:
                    self.send_json({"error": "Room not found"}, 404)
                    return
                room = rooms[room_code]
                room["players"][name] = time.time()
                # Find messages for this player since the given timestamp
                result = []
                max_ts = since
                for ts, frm, to, msg in room["msgs"]:
                    if ts <= since:
                        continue
                    # Message is for this player if: broadcast (*) or targeted to them
                    if to == "*" or to == name:
                        # Don't send a player their own broadcast messages
                        if frm == name and to == "*":
                            continue
                        result.append({"ts": ts, "from": frm, "to": to, "msg": msg})
                    if ts > max_ts:
                        max_ts = ts
            self.send_json({"ok": True, "msgs": result, "ts": max_ts})

        elif action == "leave":
            name = body.get("name", "").strip()
            if not room_code or not name:
                self.send_json({"error": "Missing fields"}, 400)
                return
            with rooms_lock:
                if room_code in rooms:
                    room = rooms[room_code]
                    room["players"].pop(name, None)
                    if not room["players"]:
                        del rooms[room_code]
                        print(f"  [ROOM] Deleted empty room: {room_code}")
            self.send_json({"ok": True})

        else:
            self.send_json({"error": f"Unknown action: {action}"}, 400)


def main():
    ip = get_local_ip()

    print()
    print("=" * 56)
    print("   DETECTIVE - Local WiFi Server")
    print("=" * 56)
    print()
    print(f"   Local IP:  {ip}")
    print(f"   Port:      {PORT}")
    print()
    print(f"   >>> Open this URL on each player's phone: <<<")
    print()
    print(f"       http://{ip}:{PORT}")
    print()
    print("=" * 56)
    print()
    print("   Make sure all devices are on the same WiFi network.")
    print("   Press Ctrl+C to stop the server.")
    print()

    # Start cleanup thread
    cleaner = threading.Thread(target=cleanup_rooms, daemon=True)
    cleaner.start()

    # Start HTTP server on all interfaces
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n   Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
