#!/usr/bin/env python3
"""
Detectives — Game Server

Serves the game and relays messages between players.
Auto-creates a public URL via ngrok so anyone can join from anywhere.

Usage:
    python serve.py

Requires: pip install pyngrok
"""

import http.server
import json
import os
import socket
import threading
import time
import urllib.parse

PORT = 8080
ROOM_TIMEOUT = 30 * 60
MSG_TTL = 60
CLEANUP_INTERVAL = 30

rooms = {}
rooms_lock = threading.Lock()


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def cleanup_rooms():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        with rooms_lock:
            dead = [code for code, room in rooms.items()
                    if now - room["created"] > ROOM_TIMEOUT
                    and all(now - t > ROOM_TIMEOUT for t in room["players"].values())]
            for code in dead:
                del rooms[code]
            for room in rooms.values():
                cutoff = (now - MSG_TTL) * 1000
                room["msgs"] = [m for m in room["msgs"] if m[0] > cutoff]


class Handler(http.server.SimpleHTTPRequestHandler):

    def log_message(self, format, *args):
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
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hidden_word.html")
            try:
                with open(html_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_json({"error": "hidden_word.html not found"}, 404)
        else:
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
                result = []
                max_ts = since
                for ts, frm, to, msg in room["msgs"]:
                    if ts <= since:
                        continue
                    if to == "*" or to == name:
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

    # Start cleanup thread
    cleaner = threading.Thread(target=cleanup_rooms, daemon=True)
    cleaner.start()

    # Try to create a public ngrok tunnel
    public_url = None
    try:
        from pyngrok import ngrok
        tunnel = ngrok.connect(PORT, "http")
        public_url = tunnel.public_url
    except Exception as e:
        print(f"  [!] ngrok not available: {e}")
        print(f"  [!] Install with: pip install pyngrok")
        print()

    print()
    print("=" * 58)
    print("   DETECTIVES - Game Server")
    print("=" * 58)
    print()
    if public_url:
        print(f"   PUBLIC URL (share this!):")
        print()
        print(f"       {public_url}")
        print()
        print(f"   Anyone with this link can join from anywhere.")
    else:
        print(f"   LOCAL ONLY: http://{ip}:{PORT}")
        print()
        print(f"   (Same WiFi network only)")
    print()
    print("=" * 58)
    print()
    print("   Press Ctrl+C to stop the server.")
    print()

    # Start HTTP server
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n   Server stopped.")
        server.server_close()
        if public_url:
            try:
                ngrok.kill()
            except Exception:
                pass


if __name__ == "__main__":
    main()
