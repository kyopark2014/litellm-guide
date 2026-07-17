#!/usr/bin/env bash
# Forward ALB HTTP to a local loopback port for Claude Code Desktop
# (Desktop requires https, or http on loopback only).
#
# Usage:
#   ./install/loopback.sh
#   ./install/loopback.sh --port 4000
#   ./install/loopback.sh --upstream http://litellm-alb-….elb.amazonaws.com
#   PORT=8080 ./install/loopback.sh
#
# Claude Code Desktop baseUrl: http://127.0.0.1:<port>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_NAME="${STACK_NAME:-litellm}"
STATE_FILE="${STATE_FILE:-$SCRIPT_DIR/.state-${STACK_NAME}.json}"
PORT="${PORT:-4000}"
UPSTREAM="${UPSTREAM:-}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Options:
  -p, --port PORT          Local loopback port (default: 4000)
  -u, --upstream URL       Upstream LiteLLM URL (default: from state file)
  -s, --state FILE         State JSON path (default: $STATE_FILE)
  -h, --help               Show this help

Env:
  PORT, UPSTREAM, STATE_FILE, STACK_NAME

Claude Code Desktop baseUrl: http://127.0.0.1:<port>
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--port)
      PORT="$2"
      shift 2
      ;;
    -u|--upstream)
      UPSTREAM="$2"
      shift 2
      ;;
    -s|--state)
      STATE_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$UPSTREAM" ]]; then
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "State file not found: $STATE_FILE" >&2
    echo "Pass --upstream http://<alb-dns> or set STATE_FILE." >&2
    exit 1
  fi
  if command -v jq >/dev/null 2>&1; then
    UPSTREAM="$(jq -r '.url // empty' "$STATE_FILE")"
  else
    UPSTREAM="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('url',''))" "$STATE_FILE")"
  fi
  if [[ -z "$UPSTREAM" || "$UPSTREAM" == "null" ]]; then
    echo "Could not read url from $STATE_FILE" >&2
    exit 1
  fi
fi

# strip trailing slash
UPSTREAM="${UPSTREAM%/}"

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "Invalid port: $PORT" >&2
  exit 1
fi

echo "loopback: http://127.0.0.1:${PORT}  →  ${UPSTREAM}"
echo "Claude Code Desktop baseUrl: http://127.0.0.1:${PORT}"
echo "Ctrl+C to stop."
echo

exec python3 - "$UPSTREAM" "$PORT" <<'PY'
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import sys
import urllib.error
import urllib.request

UPSTREAM = sys.argv[1].rstrip("/")
PORT = int(sys.argv[2])

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self) -> None:
        url = UPSTREAM + self.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP
        }
        req = urllib.request.Request(
            url, data=body, method=self.command, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in HOP_BY_HOP:
                        self.send_header(k, v)
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except Exception as e:
            msg = str(e).encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_OPTIONS(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


server = ThreadingHTTPServer(("127.0.0.1", PORT), ProxyHandler)
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\nstopped.", file=sys.stderr)
finally:
    server.server_close()
PY
