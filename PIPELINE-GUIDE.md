# Pipeline set-up guide (rehearsal notes)

> Private prep notes, excluded from git via `.git/info/exclude`.
> Purpose: be able to build the CI pipeline from an empty file, in small green steps, explaining each one out loud.

## What you're building

```
                 ┌──────────┐
 push / PR ───►  │  lint    │──┐
                 ├──────────┤  ├──► ┌──────────────────────────────────────────┐
                 │  test    │──┘    │ build: image → tests in image → smoke    │
                 ├──────────┤       └──────────────────────────────────────────┘
                 │integration│ (non-blocking, real GitHub)
                 └──────────┘
```

| Job | Purpose | Blocks merge? |
|---|---|---|
| `lint` | `ruff check` + hadolint | Yes |
| `test` | 10 unit tests, fake GitHub | Yes |
| `integration` | 1 test, real GitHub | No |
| `build` | Image tagged with SHA, tests inside it, smoke test | Yes |

**The principle to say out loud:** *fastest feedback first, prove the artifact, don't let things outside our control block us.*

---

## Step 0 – Prove it locally first

Never debug YAML for something you haven't run by hand.

```powershell
# Tests (fast path)
$env:SKIP_INTEGRATION = "1"; python -m unittest -v; Remove-Item Env:SKIP_INTEGRATION

# Lint
python -m pip install --user ruff
python -m ruff check .
cmd /c "docker run --rm -i hadolint/hadolint:v2.12.0 < Dockerfile"; $LASTEXITCODE   # no output + 0 = pass

# Build and smoke test
docker build -t gists-api:local .
docker run --rm -e SKIP_INTEGRATION=1 gists-api:local python -m unittest
docker run -d --name app-smoke -p 18080:8080 gists-api:local
curl.exe -s http://localhost:18080/healthz            # {"status": "ok"}
curl.exe -s -o NUL -w "%{http_code}" http://localhost:18080/not--valid   # 404
docker rm -f app-smoke
```

> In PowerShell 5.1 `curl` is an alias for `Invoke-WebRequest`; use `curl.exe`.

**Say:** *"Before I write any YAML I'll run each stage by hand, so the pipeline is just automating things I know work."*

---

## Step 1 – Branch

```powershell
git switch solution
git pull
git switch -c pipeline
```

**Say:** *"I'll work on a branch and raise a PR, so the change is reviewed and the pipeline proves itself on the PR before it lands."*

---

## Step 2 – Smallest useful pipeline: just the tests

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [solution]
  pull_request:
  workflow_dispatch:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Unit tests (fake GitHub API)
        run: python -m unittest -v
        env:
          SKIP_INTEGRATION: "1"
```

Commit, push, open the PR, watch it go green:

```powershell
git add .github/workflows/ci.yml
git commit -m "Add CI: run unit tests"
git push -u origin pipeline
gh pr create --base solution --title "Add CI pipeline" --body "WIP"
gh pr checks --watch
```

**Why each bit:**
- `pull_request` – feedback *before* merge. `push` to `solution` – re-check what lands. `workflow_dispatch` – manual "Run workflow" button.
- No `push` on every branch: with a PR open that would run everything twice.
- Python 3.12 – same as the Dockerfile base image; test what you ship.
- `SKIP_INTEGRATION=1` – the blocking tests must be fast and deterministic.

**Say:** *"First I want the thinnest thing that's green end to end, then I'll add to it."*

---

## Step 3 – Add lint

Add a job above `test`:

```yaml
  # Fastest checks first, so mistakes fail in seconds.
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Lint Python
        run: pipx run ruff check .
      - name: Lint Dockerfile
        run: docker run --rm -i hadolint/hadolint:v2.12.0 < Dockerfile
```

**Why:**
- `pipx run ruff` – no requirements file needed for a zero-dependency app.
- hadolint pinned to `v2.12.0` – results don't change when a new release appears.
- Jobs run **in parallel** by default, so lint adds no wall-clock time.
- **Not** `ruff format --check`: it would reformat both files. *"I'd agree a formatter with the team, then reformat in one commit."*

---

## Step 4 – Lock it down

Add at top level (under `on:`):

```yaml
# Least privilege: the pipeline only needs to read the repo.
permissions:
  contents: read

# A newer push to the same branch cancels the older run.
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

**Why:** least-privilege token limits blast radius if a compromised action runs; concurrency saves minutes and you only wait for the latest code.

---

## Step 5 – Build the artifact and test *it*

Add at top level:

```yaml
env:
  IMAGE: gists-api:${{ github.sha }}
```

Add the job:

```yaml
  build:
    needs: [lint, test]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5

      - name: Build image
        run: docker build -t "$IMAGE" .

      - name: Unit tests inside the image
        run: docker run --rm -e SKIP_INTEGRATION=1 "$IMAGE" python -m unittest -v
```

**Why:**
- `needs:` – don't spend time building if cheap checks fail.
- SHA tag – every image traces to one commit; `latest` is ambiguous. Leads to *build once, promote the same artifact*.
- Tests inside the image – proves the **artifact**, not just the source (catches a missing `COPY`, wrong Python version).

---

## Step 6 – Smoke test the running container

Append to `build`:

```yaml
      - name: Start container
        run: docker run -d --name app -p 8080:8080 "$IMAGE"

      - name: Smoke test
        run: |
          for i in $(seq 1 10); do
            curl -fsS http://localhost:8080/healthz && break
            sleep 1
          done
          echo
          curl -fsS http://localhost:8080/healthz | grep -q '"ok"'
          # Invalid username is rejected without calling GitHub.
          test "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/not--valid)" = "404"

      - name: Container logs
        if: always()
        run: docker logs app || true
```

**Why:**
- Retry loop – the app takes a moment to start (we saw "Empty reply from server" locally).
- `/not--valid` → 404 – proves routing and validation without depending on GitHub.
- `if: always()` – when it fails, the app logs are right there in the job.

---

## Step 7 – Integration test, non-blocking

```yaml
  # Calls the real GitHub API, so it can fail for reasons outside our control
  # (outage, rate limit). It reports, but does not block the build.
  # Unauthenticated: the Actions token is an app token, which GitHub rejects
  # on the user gists endpoint with a 403.
  integration:
    runs-on: ubuntu-latest
    continue-on-error: true
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Integration test (real GitHub API)
        run: python -m unittest -v
```

**Why:** a red build you can't fix trains people to ignore red builds. Note `build` does **not** `need` this job.

**Trade-off to admit:** non-blocking checks get ignored. Better long-term: run on a schedule with an alert, or as a post-deploy smoke test against staging.

---

## Step 8 – Write the PR description

Explain *what*, *why*, and *what's next*. The description on PR #1 is the model: table of jobs, design reasons, next steps. This is the "communicate in writing" criterion.

---

## Pitfalls we actually hit

| Symptom | Cause | Fix |
|---|---|---|
| `gh pr create` → "Could not resolve to a Repository … EqualExperts-Assignments/…" | `gh` guessed the original repo | `gh repo set-default rgavino/equal-experts` (or `--repo`) |
| Integration test: 503 "rate limit exceeded" but `ratelimit remaining=4999` | Passed `secrets.GITHUB_TOKEN`; it's a GitHub **App** token and the gists endpoint returns 403 for it | Run the test unauthenticated |
| …and the app said "rate limit" for a 403 that wasn't one | **App bug:** every 403 is mapped to rate limit (`app.py`) | Section 4: fix test-first (403 with remaining > 0 → 502) |
| "Node.js 20 is deprecated" warning | Old action versions | `checkout@v5`, `setup-python@v6` |
| `Empty reply from server` on first `/healthz` | Container still starting | Retry loop |
| `LF will be replaced by CRLF` on commit | Windows line endings | Harmless |
| `gh` not recognised | Terminal opened before install | Restart VS Code, or reload `$env:Path` |
| `failed to get console mode for stdin` running hadolint | PowerShell pipe into `docker run -i` (harmless warning) | `cmd /c "docker run --rm -i hadolint/hadolint:v2.12.0 < Dockerfile"` |
| `docker run -v "${PWD}:/work"` shows an empty folder | Docker Desktop isn't sharing the E: drive | Pipe/redirect the file instead, or enable the drive in Docker Desktop settings |

---

## Known weaknesses (say these before they ask)

- Actions pinned by **tag** not **commit SHA** – tags can be moved (supply chain). Fix: SHA pins + Dependabot.
- No caching; hadolint pulled each run – fine at ~30s.
- No vulnerability scan yet.
- Image isn't pushed anywhere – can't deploy or promote yet.
- No **branch protection** – nothing *enforces* green checks before merge.
- Tests ship inside the runtime image – multi-stage build would fix.

---

## Next steps (the interview agenda)

In this order, and why:

1. **Branch protection** – require `lint`, `test`, `build` to pass. Cheapest win.
2. **Push to GHCR** – needs `permissions: packages: write` on that job only; push only on `solution`, not PRs.
3. **Scan the image** (Trivy) – fail on fixable CRITICAL/HIGH.
4. **Deploy to staging** + smoke test there (the integration test belongs here).
5. **Production** behind a GitHub Environment with required reviewers; same image digest as staging.
6. **Monitoring** – `/metrics`, SLO, alerts (sections 4 and 6).

Sketch for step 2 (untested, for discussion):

```yaml
  publish:
    needs: [build]
    if: github.ref == 'refs/heads/solution'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v5
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - run: |
          IMAGE=ghcr.io/${{ github.repository }}:${{ github.sha }}
          docker build -t "$IMAGE" .
          docker push "$IMAGE"
```

> Talking point: this rebuilds the image. Better is to build once and push the exact image that was tested (save/load it as an artifact, or push from the `build` job after the smoke test).

---

## Rehearsal drill

1. `git switch solution; git switch -c pipeline-practice`
2. Start a 30-minute timer.
3. Rebuild `ci.yml` from empty, Steps 2 → 7, **narrating out loud** as if pairing.
4. Push after each step and watch it go green (or keep it local and compare with `git diff pipeline -- .github`).
5. Afterwards: `git switch pipeline; git branch -D pipeline-practice` (and delete the remote branch if you pushed it).

Aim: under 30 minutes, every step explained in one or two sentences.
