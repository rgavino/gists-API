"""HTTP API that lists a GitHub user's public gists.

GET /<user>                      -> JSON list of the user's public gists
GET /<user>?page=2&per_page=10   -> paginated (passed through to GitHub)
GET /healthz                     -> liveness check      
"""

import json
import logging
import os
import re
import signal
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # optional, raises rate limit
PORT = int(os.environ.get("PORT", "8080"))
TIMEOUT_SECONDS = 10

# GitHub usernames: alphanumeric or single hyphens, max 39 chars.
USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

log = logging.getLogger("gists")


class UpstreamError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def fetch_gists(user, page=1, per_page=30):
    """Return a summarised list of a user's public gists from the GitHub API."""
    query = urllib.parse.urlencode({"page": page, "per_page": per_page})
    url = f"{GITHUB_API}/users/{urllib.parse.quote(user)}/gists?{query}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gists-api",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    try:
        # The only non-fixed part of the scheme is GITHUB_API, which check_github_api() limits to https.
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310
            gists = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpstreamError(404, f"user '{user}' not found") from e
        log.warning(
            "GitHub returned %d for %s (ratelimit remaining=%s, reset=%s)",
            e.code, url, e.headers.get("x-ratelimit-remaining"), e.headers.get("x-ratelimit-reset"),
        )
        if e.code in (403, 429):
            raise UpstreamError(503, "GitHub API rate limit exceeded, try again later") from e
        raise UpstreamError(502, f"GitHub API returned {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        log.error("GitHub unreachable for %s: %s", url, getattr(e, "reason", e))
        raise UpstreamError(502, "GitHub API unreachable") from e

    return [
        {
            "id": g["id"],
            "description": g.get("description"),
            "url": g["html_url"],
            "files": sorted(g.get("files", {})),
            "created_at": g.get("created_at"),
            "updated_at": g.get("updated_at"),
        }
        for g in gists
    ]


def parse_positive_int(params, name, default, maximum=None):
    value = params.get(name, [str(default)])[0]
    if not value.isdigit() or int(value) < 1:
        raise ValueError(f"'{name}' must be a positive integer")
    return min(int(value), maximum) if maximum else int(value)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.strip("/")

        if path == "healthz":
            return self.send_json(200, {"status": "ok"})
        if not USERNAME_RE.match(path):
            return self.send_json(404, {"error": "not found, use /<github-username>"})

        try:
            params = urllib.parse.parse_qs(parsed.query)
            page = parse_positive_int(params, "page", 1)
            per_page = parse_positive_int(params, "per_page", 30, maximum=100)
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})

        try:
            self.send_json(200, fetch_gists(path, page, per_page))
        except UpstreamError as e:
            self.send_json(e.status, {"error": e.message})

    def send_json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)


def handle_sigterm(signum, frame):
    # `docker stop` sends SIGTERM; treat it like Ctrl+C so we shut down cleanly.
    raise KeyboardInterrupt


def check_github_api(url):
    # urlopen also follows file:// and other schemes, so a bad GITHUB_API could read local files.
    if not url.startswith("https://"):
        raise SystemExit(f"GITHUB_API must start with https://, got {url!r}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    check_github_api(GITHUB_API)
    signal.signal(signal.SIGTERM, handle_sigterm)
    # Must listen on all interfaces: inside a container, 127.0.0.1 is unreachable through the port mapping.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)  # noqa: S104
    server.daemon_threads = False  # let in-flight requests finish on shutdown
    log.info("listening on port %d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()  # waits for in-flight request threads
        log.info("stopped")


if __name__ == "__main__":
    main()
