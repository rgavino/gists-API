# Gists API

A small HTTP API that returns a GitHub user's public gists.

`GET /<user>` → JSON list of that user's public gists, fetched from the [GitHub Gists API](https://docs.github.com/en/rest/gists/gists?apiVersion=2022-11-28#list-gists-for-a-user).

Written in Python using only the standard library. There are no dependencies to install.

## Quick start (Docker)

```sh
docker build -t gists-api .
docker run --rm -p 8080:8080 gists-api
```

Then:

```sh
curl http://localhost:8080/octocat
```

## Run without Docker

Requires Python 3 (tested with 3.12). No `pip install` needed.

```sh
python app.py          # listens on http://localhost:8080
```

## Run the tests

```sh
python -m unittest -v
```

- **10 tests use a fake GitHub API.** They're fast and need no network. They cover the response format, pagination, input validation, error mapping and logging.
- **1 test calls the real GitHub API for `octocat`.** Set `SKIP_INTEGRATION=1` to skip it when offline.

The tests are also included in the image:

```sh
docker run --rm gists-api python -m unittest -v
```

## API

| Request | Response |
|---|---|
| `GET /<user>` | `200` with a list of the user's public gists |
| `GET /<user>?page=2&per_page=10` | Paginated; passed through to GitHub (`per_page` defaults to 30, max 100) |
| `GET /healthz` | `200 {"status": "ok"}`: liveness check (does not call GitHub) |

Example response:

```json
[
  {
    "id": "6cad326836d38bd3a7ae",
    "description": "Hello world!",
    "url": "https://gist.github.com/octocat/6cad326836d38bd3a7ae",
    "files": ["hello_world.rb"],
    "created_at": "2014-10-01T16:19:34Z",
    "updated_at": "2026-10-13T10:22:12Z"
  }
]
```

Errors are returned as JSON (`{"error": "..."}`):

| Status | Meaning |
|--------|------------------------------------------------------------------------------------|
| `400`  | Invalid `page` / `per_page`                                                        |
| `404`  | Unknown GitHub user, or path is not a valid GitHub username (GitHub is not called) |
| `502`  | GitHub returned an error or could not be reached (10s timeout)                     |
| `503`  | GitHub rate limit exceeded; retry later                                            |

## Configuration

All settings are environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | Port to listen on |
| `GITHUB_TOKEN` | *(unset)* | Optional. Raises the GitHub rate limit from 60 to 5,000 requests/hour |
| `GITHUB_API` | `https://api.github.com` | GitHub API base URL |

```sh
docker run --rm -p 8080:8080 -e GITHUB_TOKEN=<token> gists-api
```

## Design notes

- **Standard library only.** For a single endpoint, a framework would add more setup than it saves.
- **Summarised response.** The API returns the useful fields rather than GitHub's full payload, so clients don't depend on GitHub's response format.
- **Usernames are checked before calling GitHub.** Junk requests (bots, `favicon.ico`) don't use up the rate limit, and the check stops a crafted path from reaching a different GitHub URL.
- **Clear failures.** Calls to GitHub time out after 10s, errors map to distinct status codes, and GitHub failures are logged with rate-limit details (`x-ratelimit-remaining`, `x-ratelimit-reset`).
- **Container.** The image is based on `python:3.12-slim`, runs as a non-root user, has a `HEALTHCHECK`, writes unbuffered logs to stdout, and shuts down cleanly on `docker stop` (SIGTERM), letting requests already in progress finish.

## Production considerations / next steps

These were left out to keep the solution simple, but they're what I'd add before running this for real.

### Structured logging
Logs are plain text, which is fine for `docker logs` but awkward for log platforms (Loki, ELK, Datadog). I'd emit one JSON object per line, with fields such as `path`, `status`, `duration_ms`, `upstream_status` and a request ID. Logs could then be searched and graphed, for example "503s by hour" or "slowest users".

### Metrics and SLOs
There are no metrics. I'd add a Prometheus `/metrics` endpoint with:
- request count and response time, by status code;
- calls to GitHub, broken down by result (success, 4xx, 5xx, timeout);
- requests remaining before GitHub's rate limit (from `x-ratelimit-remaining`).

These support an SLO such as "99% of requests succeed in under 500ms", with alerts on how fast that target is being missed rather than on single errors. An alert on the rate-limit gauge would warn before the limit is hit instead of after.

### Caching and conditional requests
Every request makes one GitHub call, so without a token the service can handle only 60 requests an hour. Two improvements:
- **Short-lived cache.** Cache responses per user and page for around 60 seconds. Repeated requests for the same user then cost one GitHub call per minute. The trade-offs are data up to 60s old, a separate cache in each copy of the app, and a need to limit the cache's size. If there are several copies, a shared cache such as Redis is the next step.
- **Conditional requests.** Store the `ETag` GitHub returns and send it back in an `If-None-Match` header. If nothing has changed, GitHub replies `304 Not Modified`, which [does not count against the rate limit](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests-if-appropriate). This pairs well with the cache: once an entry expires, it can be checked cheaply rather than fetched again.

### Retries
There are deliberately no retries. Retrying blindly during a GitHub outage adds load and uses up the rate limit faster. If added, they'd be:
- only for 5xx errors and network failures, never 4xx;
- with exponential backoff and jitter (increasing, randomised delays between attempts);
- capped at 1–2 attempts within the overall request timeout.

When rate-limited, the API could also return a `Retry-After` header calculated from `x-ratelimit-reset`, so clients know when to come back.

### Other
- **Pagination links.** Pass GitHub's `Link` header through so clients can follow the next and last pages.
- **Production server.** `http.server` is fine for this exercise. Under real load I'd move to a framework running on gunicorn or uvicorn.
- **Image hardening.** Use a multi-stage build so tests aren't in the runtime image, pin the base image by digest, and scan it for vulnerabilities in CI.
- **CI.** A pipeline that runs the tests and `docker build` on every push.


### References
- **List gists for a user** https://docs.github.com/en/rest/gists/gists?apiVersion=2022-11-28#list-gists-for-a-user
- **Rate limits for the REST API** https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- **Best practices for using the REST API** https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api
- **Using pagination in the REST API** https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api
- **API versions** https://docs.github.com/en/rest/about-the-rest-api/api-versions
