"""Gitea pull requests for push-mode delivery.

The API token lives in a FILE on disk (Project.pr_token_file) — it is read
here, at use, and only here: never logged, never persisted, never embedded
in a raised error. Only stdlib is used so no new dependency is needed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, NoReturn, TypeVar

log = logging.getLogger(__name__)

# HTTP error bodies can be huge; cap what we surface in story.error.
_BODY_CAP = 500

# Open-PR lookup: single request, capped page. No retry loop — the poll side
# has its own cadence and a transient miss here falls back to create_pull.
FIND_OPEN_PULL_LIMIT = 50

# Bounded retry for transient transport failures. Env-overridable so tests
# (and operators with a flaky Gitea) can shrink or grow the window. The
# mutators (create_pull / update_branch) run inside a SYNCHRONOUS human
# "approve", so the total sleep is capped hard: ~3 attempts with exponential
# backoff and never more than PR_RETRY_WINDOW seconds of waiting.
PR_MAX_ATTEMPTS = max(1, int(os.environ.get("FUBALA_PR_MAX_ATTEMPTS", "3")))
PR_BACKOFF_BASE = max(0.0, float(os.environ.get("FUBALA_PR_BACKOFF_BASE", "1.0")))
PR_RETRY_WINDOW = max(0.0, float(os.environ.get("FUBALA_PR_RETRY_WINDOW", "30.0")))


class PullRequestError(Exception):
    """User-presentable PR failure (message goes to story.error — must never
    contain the token)."""


class ProtectedFilesError(PullRequestError):
    """Gitea answered 405 because the merge touches protected files (or the
    token's account may not merge): a force_merge by an allowed account is
    the way through."""


class PullRequestConflictError(PullRequestError):
    """Gitea answered 409 Conflict — for create_pull this means a pull
    request for this head already exists. Callers catch this subclass to
    adopt the existing PR instead of failing the story."""


class TransientPullRequestError(PullRequestError):
    """A transport-shaped failure worth retrying: URLError, OSError, HTTP 5xx
    or 429. Raising the plain parent class means "permanent" — a 4xx
    (auth rejected, bad payload, 404…) is never retried."""


# Gitea misuses HTTP 500 for "the branch already carries the base" on
# POST …/pulls/{index}/update: its Actions layer answers exactly this JSON
# body under load. String matching is normally forbidden (stories.py never
# string-matches), but the state is only reported through this body, so the
# match is pinned to the exact text AND the PR index in the URL; anything
# else — including a Gitea that changes the wording — falls through to the
# plain transient path (today's noisy behaviour, never a wrong landing).
_UP_TO_DATE_RE = re.compile(r"^HeadBranch of PR (\d+) is up to date$")


class PullRequestUpToDateError(PullRequestError):
    """The branch-update POST hit Gitea's misused 500 "HeadBranch of PR
    <index> is up to date": the PR branch already contains everything in its
    base, so there is nothing to merge. Deliberately NOT a
    TransientPullRequestError — retrying cannot change this answer, so
    _with_retries propagates it on the first attempt and the caller (the
    retry-land recovery) re-runs CI on the unchanged head instead."""


T = TypeVar("T")


def _with_retries(operation: Callable[[], T], description: str) -> T:
    """Run `operation`, retrying TransientPullRequestError a bounded number
    of times with exponential backoff inside a hard total window.

    On exhaustion the error becomes a plain PullRequestError prefixed
    `transient network error (N attempts): …`, so story.error says both that
    it was retried and what the last attempt said. Permanent errors
    propagate untouched — retrying a 4xx is noise.
    """
    last: TransientPullRequestError | None = None
    attempts = 0
    deadline = time.monotonic() + PR_RETRY_WINDOW
    for attempt in range(1, PR_MAX_ATTEMPTS + 1):
        attempts = attempt
        try:
            return operation()
        except TransientPullRequestError as e:
            last = e
            message = str(e)
        if attempt >= PR_MAX_ATTEMPTS:
            break
        # only an exhausted WINDOW stops the retrying: a zero backoff base
        # (tests, operators who want immediate retries) must retry at once,
        # not give up after the first attempt
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        delay = min(PR_BACKOFF_BASE * (2 ** (attempt - 1)), remaining)
        log.warning(
            "%s failed transiently (attempt %d/%d): %s — retrying in %.1fs",
            description,
            attempt,
            PR_MAX_ATTEMPTS,
            message,
            delay,
        )
        if delay > 0:
            time.sleep(delay)
    raise PullRequestError(
        f"transient network error ({attempts} attempts): {last}"
    ) from last


def read_token(token_file: str) -> str:
    """Read the API token from its file, without ever echoing it back."""
    try:
        with open(token_file, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as e:
        raise PullRequestError(
            f"cannot read token file {token_file}: {e.strerror or e}"
        ) from e
    if not token:
        raise PullRequestError(f"token file {token_file} is empty")
    return token


def _raise_http_error(e: urllib.error.HTTPError, method: str, url: str) -> NoReturn:
    """Surface an HTTPError; 5xx/429 are transient, every other 4xx is not."""
    detail = ""
    try:
        detail = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — the body is best-effort context
        pass
    snippet = " ".join(detail.split())[:_BODY_CAP]
    message = f"Gitea returned {e.code} {e.reason} for {method} {url}"
    if snippet:
        message += f": {snippet}"
    if e.code == 500:
        # Gitea reports "the branch is already up to date" as a 500 with a
        # JSON `message` field (see PullRequestUpToDateError). Match the
        # exact text and the PR index in the URL; a non-JSON body never
        # matches, and any other 500 stays transient.
        index_match = re.search(r"/pulls/(\d+)/update", url)
        expected = (
            f"HeadBranch of PR {index_match.group(1)} is up to date"
            if index_match
            else None
        )
        if expected is not None:
            try:
                body = json.loads(detail)
            except ValueError:
                body = None
            reported = body.get("message") if isinstance(body, dict) else None
            if (
                isinstance(reported, str)
                and _UP_TO_DATE_RE.match(reported.strip())
                and reported.strip() == expected
            ):
                raise PullRequestUpToDateError(message) from e
    if e.code == 429 or e.code >= 500:
        raise TransientPullRequestError(message) from e
    if e.code == 409:
        # Contract between create_pull and the landing recovery in
        # stories.py: a 409 on PR creation means "already exists" — never
        # string-match the message.
        raise PullRequestConflictError(message) from e
    raise PullRequestError(message) from e


def _raise_transport_error(e: BaseException, url: str) -> NoReturn:
    """URLError/OSError — the Gitea we tried to reach is unreachable."""
    reason = getattr(e, "reason", None) or e
    raise TransientPullRequestError(f"cannot reach {url}: {reason}") from e


def create_pull(
    base_url: str,
    repo: str,
    token: str,
    head: str,
    base_branch: str,
    title: str,
    body: str = "",
    timeout: float = 30.0,
) -> tuple[str | None, int]:
    """Open a pull request on a Gitea instance; returns (html_url, index).

    html_url is None when Gitea's response leaves it out; run it through
    `public_pr_url` before storing it.

    POST {base_url}/api/v1/repos/{repo}/pulls with `Authorization: token …`
    (Gitea's token auth scheme). 201 with a pull request number is success; anything
    else is a PullRequestError whose message carries only the status and a
    truncated body — the token never appears in it.

    Transient transport failures (unreachable host, 5xx, 429) are retried a
    bounded number of times before giving up; a 4xx is reported at once.
    """
    url = f"{base_url.rstrip('/')}/api/v1/repos/{repo}/pulls"
    payload = json.dumps(
        {"title": title, "body": body, "head": head, "base": base_branch}
    ).encode("utf-8")

    def _attempt() -> tuple[str | None, int]:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"token {token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            _raise_http_error(e, "POST", url)
        except urllib.error.URLError as e:
            _raise_transport_error(e, url)
        except OSError as e:
            _raise_transport_error(e, url)

        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        html_url = (parsed or {}).get("html_url") if isinstance(parsed, dict) else None
        index = (parsed or {}).get("number") or (parsed or {}).get("id")
        if index is None:
            snippet = " ".join(raw.split())[:_BODY_CAP]
            raise PullRequestError(
                "Gitea response had no pull request number"
                + (f": {snippet}" if snippet else "")
            )
        return (str(html_url) if html_url else None), int(index)

    return _with_retries(_attempt, f"PR create for {repo}")


def public_pr_url(
    html_url: str | None,
    *,
    api_base: str,
    web_base: str | None,
    repo: str,
    index: int,
) -> str:
    """The PR URL to store and show.

    Gitea builds `html_url` from the host the request came in on, so a call
    through an in-cluster API base yields an internal URL. With `web_base`
    set, the path (and query) of `html_url` is re-hosted on it; without an
    `html_url` the URL is built from `web_base`. With `web_base` unset the
    `html_url` is returned unchanged, or built from `api_base` when missing.
    """
    if web_base:
        root = web_base.rstrip("/")
        if html_url:
            parts = urllib.parse.urlsplit(html_url)
            return root + parts.path + (f"?{parts.query}" if parts.query else "")
        return f"{root}/{repo}/pulls/{index}"
    if html_url:
        return html_url
    return f"{api_base.rstrip('/')}/{repo}/pulls/{index}"


# ---- read/poll side -------------------------------------------------------


def _request_json(
    url: str,
    token: str,
    *,
    method: str = "GET",
    timeout: float = 30.0,
    body: dict | None = None,
) -> Any:
    """Authed JSON request to the Gitea API.

    Shared error handling for every poll/mutate helper: the message carries
    only the method, url, status and a truncated body — the token never
    appears in a raised error (same hygiene as create_pull). Transport
    failures raise TransientPullRequestError so the mutators can retry them;
    the poll side deliberately does NOT retry (it has its own cadence and
    dwell logic).
    """
    headers = {
        "Accept": "application/json",
        "Authorization": f"token {token}",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers, method=method, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        _raise_http_error(e, method, url)
    except urllib.error.URLError as e:
        _raise_transport_error(e, url)
    except OSError as e:
        _raise_transport_error(e, url)
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        snippet = " ".join(raw.split())[:_BODY_CAP]
        raise PullRequestError(
            "Gitea response was not JSON"
            + (f": {snippet}" if snippet else "")
        ) from None


def get_pull(
    base_url: str,
    repo: str,
    token: str,
    index: int,
    timeout: float = 30.0,
) -> dict:
    """Fetch one pull request: {merged, mergeable, state, head_sha,
    merge_commit_sha, base_ref, merge_base}.

    GET {base_url}/api/v1/repos/{repo}/pulls/{index}. `mergeable` may be
    null while Gitea computes it async — callers must treat null as
    "unknown", never as a conflict. `head_sha` is the PR's CURRENT head
    commit (None when the payload carries no head) — CI on it is the only
    verdict that counts, because a branch update (retry-land) moves the
    head and any earlier sha goes stale. `merge_commit_sha` is set once
    the PR merged (None until then) — the reconcile sweep names it in its
    event.
    """
    url = f"{base_url.rstrip('/')}/api/v1/repos/{repo}/pulls/{index}"
    parsed = _request_json(url, token, timeout=timeout)
    if not isinstance(parsed, dict):
        raise PullRequestError(f"Gitea returned no pull request object for {url}")
    head = parsed.get("head")
    base = parsed.get("base")
    return {
        "merged": bool(parsed.get("merged")),
        "mergeable": parsed.get("mergeable"),
        "state": parsed.get("state"),
        "head_sha": head.get("sha") if isinstance(head, dict) else None,
        "merge_commit_sha": parsed.get("merge_commit_sha"),
        # the branch the PR wants to land in (None when the payload carries
        # no base) — the land-race recovery compares against its head
        "base_ref": base.get("ref") if isinstance(base, dict) else None,
        # the commit of the base branch the PR was last in sync with (None
        # when the payload has none) — real Gitea carries it here, not in
        # the compare payload
        "merge_base": parsed.get("merge_base") or None,
    }


def find_open_pull(
    base_url: str,
    repo: str,
    token: str,
    head: str,
    timeout: float = 30.0,
) -> tuple[str, int] | None:
    """Find the open pull request whose head branch is `head`.

    GET {base_url}/api/v1/repos/{repo}/pulls?state=open&limit=50 and match
    the entry whose `head.ref == head`; returns (html_url, number) or None.
    Used on create_pull's 409 ("already exists") to adopt the live PR
    instead of failing the story. Single shot, poll-side cadence: no retry
    loop — a transient miss falls through to the plain failure path.
    """
    quoted = urllib.parse.quote(str(repo), safe="/")
    query = urllib.parse.urlencode({"state": "open", "limit": FIND_OPEN_PULL_LIMIT})
    url = f"{base_url.rstrip('/')}/api/v1/repos/{quoted}/pulls?{query}"
    parsed = _request_json(url, token, timeout=timeout)
    if not isinstance(parsed, list):
        raise PullRequestError(f"Gitea returned no pull request list for {url}")
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        head_ref = (entry.get("head") or {}).get("ref") if isinstance(
            entry.get("head"), dict
        ) else None
        if head_ref != head:
            continue
        html_url = entry.get("html_url")
        index = entry.get("number") or entry.get("id")
        if not html_url or index is None:
            continue
        return str(html_url), int(index)
    return None


def pull_files(
    base_url: str,
    repo: str,
    token: str,
    index: int,
    timeout: float = 30.0,
) -> list[str]:
    """The changed file paths of a pull request (possibly truncated).

    GET {base_url}/api/v1/repos/{repo}/pulls/{index}/files — the same
    endpoint the land job reads to recognise a CI PR. Returns the
    `filename` of every entry; a Gitea that answers something unparsable
    yields an empty list (callers treat that as "not proven to be a CI PR"
    and keep their normal behaviour).
    """
    quoted = urllib.parse.quote(str(repo), safe="/")
    url = f"{base_url.rstrip('/')}/api/v1/repos/{quoted}/pulls/{int(index)}/files"
    parsed = _request_json(url, token, timeout=timeout)
    if not isinstance(parsed, list):
        return []
    names: list[str] = []
    for entry in parsed:
        if isinstance(entry, dict) and entry.get("filename"):
            names.append(str(entry["filename"]))
    return names


def update_branch(
    base_url: str,
    repo: str,
    token: str,
    index: int,
    timeout: float = 30.0,
) -> None:
    """Ask Gitea to update the PR's branch from its base (retry-land).

    POST {base_url}/api/v1/repos/{repo}/pulls/{index}/update?style=merge —
    the query pins the merge style (a merge commit, not a rebase of the
    branch's history), which is what the land-race recovery wants: the
    head branch gains main, and CI runs on the merge result. Raises
    PullRequestError on any non-2xx (e.g. the conflict cannot be auto-fixed).
    Transient transport failures are retried like create_pull's — except
    Gitea's misused 500 "HeadBranch of PR <index> is up to date", which
    surfaces at once as PullRequestUpToDateError: retrying cannot change
    that answer, and the retry-land recovery re-runs CI on the unchanged
    head instead.
    """
    quoted = urllib.parse.quote(str(repo), safe="/")
    query = urllib.parse.urlencode({"style": "merge"})
    url = (
        f"{base_url.rstrip('/')}/api/v1/repos/{quoted}/pulls/{int(index)}"
        f"/update?{query}"
    )

    def _attempt() -> None:
        _request_json(url, token, method="POST", timeout=timeout)

    _with_retries(_attempt, f"PR update for {repo}#{index}")


# ---- Actions side: re-run CI on an unchanged PR head ------------------------


def find_latest_workflow_run(
    base_url: str,
    repo: str,
    token: str,
    head_sha: str,
    timeout: float = 30.0,
) -> int | None:
    """The id of the latest Actions run for `head_sha`, or None for "no run".

    GET {base_url}/api/v1/repos/{repo}/actions/runs and pick the largest
    `id` among entries whose `head_sha` matches. Single shot — the caller
    (retry-land recovery) already has one deterministic recovery to do, and
    a transient miss here must not retry-loop inside a synchronous approve;
    a PullRequestError surfaces to the caller's generic failure handling.
    """
    quoted = urllib.parse.quote(str(repo), safe="/")
    url = f"{base_url.rstrip('/')}/api/v1/repos/{quoted}/actions/runs"
    parsed = _request_json(url, token, timeout=timeout)
    if not isinstance(parsed, dict):
        return None
    entries = parsed.get("workflow_runs")
    if not isinstance(entries, list):
        return None
    best: int | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("head_sha") != head_sha:
            continue
        run_id = entry.get("id")
        try:
            run_id = int(run_id)
        except (TypeError, ValueError):
            continue
        if best is None or run_id > best:
            best = run_id
    return best


def rerun_workflow_run(
    base_url: str,
    repo: str,
    token: str,
    run_id: int,
    timeout: float = 30.0,
) -> None:
    """Re-run an Actions workflow run (Gitea answers 201).

    POST {base_url}/api/v1/repos/{repo}/actions/runs/{run_id}/rerun — no
    response body on success; any non-2xx is a PullRequestError via the
    shared hygiene (token never in the message). Single shot like the run
    lookup: a 404/403 (Actions off, missing rights) surfaces at once for
    the caller's failure path."""
    quoted = urllib.parse.quote(str(repo), safe="/")
    url = (
        f"{base_url.rstrip('/')}/api/v1/repos/{quoted}/actions/runs/"
        f"{int(run_id)}/rerun"
    )
    _request_json(url, token, method="POST", timeout=timeout)


def create_pull_comment(
    base_url: str,
    repo: str,
    token: str,
    index: int,
    body: str,
    timeout: float = 30.0,
) -> None:
    """Post a comment on a pull request (also works on plain issues).

    POST {base_url}/api/v1/repos/{repo}/issues/{index}/comments with
    {"body": …} — Gitea routes PR comments through the issue endpoint.
    Raises PullRequestError on any non-2xx; transient transport failures are
    retried like create_pull's. Token-safe errors only (shared plumbing).
    """
    quoted = urllib.parse.quote(str(repo), safe="/")
    url = f"{base_url.rstrip('/')}/api/v1/repos/{quoted}/issues/{int(index)}/comments"
    payload = json.dumps({"body": body}).encode("utf-8")

    def _attempt() -> None:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"token {token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read()
        except urllib.error.HTTPError as e:
            _raise_http_error(e, "POST", url)
        except urllib.error.URLError as e:
            _raise_transport_error(e, url)
        except OSError as e:
            _raise_transport_error(e, url)

    _with_retries(_attempt, f"PR comment for {repo}#{index}")


def close_pull(
    base_url: str,
    repo: str,
    token: str,
    index: int,
    timeout: float = 30.0,
) -> None:
    """Close a pull request without merging it.

    PATCH {base_url}/api/v1/repos/{repo}/pulls/{index} with {"state":
    "closed"}. A 409 means the PR was ALREADY MERGED (Gitea refuses to close
    a merged pull) — callers superseding a story treat that as fine, so it
    surfaces as a plain PullRequestError they can catch and record. Transient
    transport failures are retried like create_pull's.
    """
    quoted = urllib.parse.quote(str(repo), safe="/")
    url = f"{base_url.rstrip('/')}/api/v1/repos/{quoted}/pulls/{int(index)}"
    payload = json.dumps({"state": "closed"}).encode("utf-8")

    def _attempt() -> None:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"token {token}",
            },
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read()
        except urllib.error.HTTPError as e:
            _raise_http_error(e, "PATCH", url)
        except urllib.error.URLError as e:
            _raise_transport_error(e, url)
        except OSError as e:
            _raise_transport_error(e, url)

    _with_retries(_attempt, f"PR close for {repo}#{index}")


# ---- reconcile side: is the branch already in the target? -----------------


def branch_head(
    base_url: str,
    repo: str,
    token: str,
    branch: str,
    timeout: float = 30.0,
) -> str | None:
    """The sha `branch` points at on the remote, or None when Gitea reports
    no such branch (never pushed, or deleted after a merge) — or when the
    lookup itself fails. The reconcile sweep treats None as "cannot prove"
    and skips, so a deleted branch never flips a story's status.

    GET {base_url}/api/v1/repos/{repo}/branches/{branch}. Used as a fallback
    for failed stories whose pushed_sha was never persisted.
    """
    quoted = urllib.parse.quote(str(branch), safe="/")
    url = f"{base_url.rstrip('/')}/api/v1/repos/{repo}/branches/{quoted}"
    try:
        parsed = _request_json(url, token, timeout=timeout)
    except PullRequestError as e:
        log.info("branch head lookup for %s failed: %s", branch, e)
        return None
    if not isinstance(parsed, dict):
        return None
    commit = parsed.get("commit")
    if not isinstance(commit, dict):
        return None
    sha = commit.get("id") or commit.get("sha")
    return str(sha) if sha else None


def branch_merged(
    base_url: str,
    repo: str,
    token: str,
    head: str,
    base: str,
    *,
    pr_index: int | None = None,
    timeout: float = 30.0,
) -> tuple[bool, str | None]:
    """Is `head` already contained in `base`? -> (contained, merge_sha|None)

    GET {base_url}/api/v1/repos/{repo}/compare/{base}...{head}: a head that
    is ZERO commits ahead of base is contained in it. Careful: "0 ahead"
    alone ALSO matches a branch that never carried a commit (head == base),
    so callers must separately prove the story produced a commit — the
    reconcile sweep does that with pushed_sha != base_ref.

    `merge_sha` is read off the pull request (get_pull's merge_commit_sha)
    when `pr_index` is given; it is None when there is no PR, when the PR
    is gone, or when Gitea has not recorded a merge commit.

    A compare the endpoint cannot serve (404 — deleted branch, unknown
    base) raises PullRequestError: "unprovable", which the sweep treats as
    SKIP (it logs and leaves the story failed) rather than as a verdict.
    Only the branch_head helper turns a 404 into None.
    """
    qbase = urllib.parse.quote(str(base), safe="/")
    qhead = urllib.parse.quote(str(head), safe="/")
    url = f"{base_url.rstrip('/')}/api/v1/repos/{repo}/compare/{qbase}...{qhead}"
    parsed = _request_json(url, token, timeout=timeout)
    if not isinstance(parsed, dict):
        return False, None
    commits = parsed.get("commits") or []
    try:
        ahead = int(parsed.get("total_commits", len(commits)))
    except (TypeError, ValueError):
        ahead = len(commits)
    if ahead != 0 or commits:
        return False, None
    merge_sha: str | None = None
    if pr_index is not None:
        try:
            info = get_pull(base_url, repo, token, int(pr_index), timeout=timeout)
        except PullRequestError as e:
            # the branch is provably contained; a missing/unreadable PR must
            # not undo that — the event just names no merge commit
            log.warning("merge sha lookup for PR %s failed: %s", pr_index, e)
            info = {}
        sha = info.get("merge_commit_sha")
        merge_sha = str(sha) if sha else None
    return True, merge_sha


def combined_status(
    base_url: str,
    repo: str,
    token: str,
    sha: str,
    timeout: float = 30.0,
) -> str | None:
    """The combined commit status of `sha`: "success" | "failure" | … or None.

    GET {base_url}/api/v1/repos/{repo}/commits/{sha}/status. None means
    "unknown / no statuses" — never treated as a failure.
    """
    url = f"{base_url.rstrip('/')}/api/v1/repos/{repo}/commits/{sha}/status"
    parsed = _request_json(url, token, timeout=timeout)
    if not isinstance(parsed, dict):
        return None
    state = parsed.get("status") or parsed.get("state")
    return str(state) if state else None


def all_statuses(
    base_url: str,
    repo: str,
    token: str,
    sha: str,
    timeout: float = 30.0,
) -> list:
    """The FULL per-context status list of `sha` (empty when Gitea has none).

    GET {base_url}/api/v1/repos/{repo}/commits/{sha}/statuses. The combined
    endpoint collapses to the worst state and hides per-context history, so
    a marker only a superseded context carries (the land-race recovery reads
    one) is invisible there — this list is the only place it lives.
    """
    url = f"{base_url.rstrip('/')}/api/v1/repos/{repo}/commits/{sha}/statuses"
    parsed = _request_json(url, token, timeout=timeout)
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


def latest_context_states(statuses: list) -> dict[str, str]:
    """The latest state per context of a `/statuses` list.

    Gitea lists every state a context ever had on the sha; the newest entry
    (by update time, then id) is the one that counts. Entries without a
    context are Gitea's "default" context."""
    best: dict[str, tuple[tuple[str, int], str]] = {}
    for entry in statuses:
        context = str(entry.get("context") or "default")
        state = str(entry.get("status") or entry.get("state") or "")
        try:
            ident = int(entry.get("id") or 0)
        except (TypeError, ValueError):
            ident = 0
        key = (str(entry.get("updated_at") or entry.get("created_at") or ""), ident)
        if context not in best or key > best[context][0]:
            best[context] = (key, state)
    return {context: state for context, (_, state) in best.items()}


def merge_pull(
    base_url: str,
    repo: str,
    token: str,
    index: int,
    head_sha: str,
    timeout: float = 30.0,
    *,
    force: bool = False,
) -> None:
    """Merge a pull request, pinned to the head the caller judged.

    POST {base_url}/api/v1/repos/{repo}/pulls/{index}/merge with
    {"Do": "merge", "head_commit_id": head_sha}: Gitea refuses when the
    branch moved since, so a stale green can never merge a new head. Any
    non-2xx raises PullRequestError, except an answer that says the PR is
    already merged (the CI land job may have won the race): that is success.
    With force=True the body also carries "force_merge": true (operator
    force-merge only). A 405 about protected files or a merge the account is
    not allowed to do raises ProtectedFilesError.
    Single shot, no retry: a merge is not something to repeat blindly."""
    quoted = urllib.parse.quote(str(repo), safe="/")
    url = f"{base_url.rstrip('/')}/api/v1/repos/{quoted}/pulls/{int(index)}/merge"
    payload: dict[str, object] = {"Do": "merge", "head_commit_id": head_sha}
    if force:
        payload["force_merge"] = True
    try:
        _request_json(
            url,
            token,
            method="POST",
            timeout=timeout,
            body=payload,
        )
    except PullRequestError as e:
        text = str(e)
        if re.search(r"already (been )?merged|has been merged", text, re.IGNORECASE):
            return
        if "Gitea returned 405" in text and re.search(
            r"protected files|not allowed to merge", text, re.IGNORECASE
        ):
            raise ProtectedFilesError(text) from e
        raise


def required_contexts(
    base_url: str,
    repo: str,
    token: str,
    branch: str,
    timeout: float = 30.0,
) -> list[str] | None:
    """The status contexts branch protection requires, or None.

    GET {base_url}/api/v1/repos/{repo}/branch_protections/{branch}. None
    means "no usable rule": protection missing, the token cannot read it
    (403/404), status checks not enabled, or an empty list. Callers then
    fall back to the stricter all-green rule, never to "anything goes"."""
    quoted = urllib.parse.quote(str(repo), safe="/")
    name = urllib.parse.quote(str(branch), safe="")
    url = f"{base_url.rstrip('/')}/api/v1/repos/{quoted}/branch_protections/{name}"
    try:
        parsed = _request_json(url, token, timeout=timeout)
    except PullRequestError:
        return None
    if not isinstance(parsed, dict) or not parsed.get("enable_status_check"):
        return None
    contexts = [str(c) for c in parsed.get("status_check_contexts") or [] if c]
    return contexts or None


def compare_merge_base(
    base_url: str,
    repo: str,
    token: str,
    base: str,
    head: str,
    timeout: float = 30.0,
) -> str | None:
    """The merge base of `base` and `head`, or None when the compare cannot
    serve one (unknown branch, deleted head, no merge_base in the payload).

    GET {base_url}/api/v1/repos/{repo}/compare/{base}...{head} and read the
    `merge_base` field. Used by the land-race recovery to prove the PR is
    behind its base (merge base != the base branch's own head); callers
    treat None as "cannot prove" and do not act on it.
    """
    qbase = urllib.parse.quote(str(base), safe="/")
    qhead = urllib.parse.quote(str(head), safe="/")
    url = f"{base_url.rstrip('/')}/api/v1/repos/{repo}/compare/{qbase}...{qhead}"
    try:
        parsed = _request_json(url, token, timeout=timeout)
    except PullRequestError as e:
        log.info("merge base lookup for %s...%s failed: %s", base, head, e)
        return None
    if not isinstance(parsed, dict):
        return None
    merge_base = parsed.get("merge_base")
    return str(merge_base) if merge_base else None


def status_description(
    base_url: str,
    repo: str,
    token: str,
    sha: str,
    timeout: float = 30.0,
) -> str | None:
    """Human context for a failed combined status: the description of the
    first non-success entry, or the overall state when there is none."""
    url = f"{base_url.rstrip('/')}/api/v1/repos/{repo}/commits/{sha}/status"
    parsed = _request_json(url, token, timeout=timeout)
    if not isinstance(parsed, dict):
        return None
    for entry in parsed.get("statuses") or []:
        if isinstance(entry, dict) and entry.get("status") not in (
            None, "success", "pending",
        ):
            return str(entry.get("description") or entry.get("status") or "")
    return str(parsed.get("status") or "") or None


def delete_branch(
    base_url: str,
    repo: str,
    token: str,
    branch: str,
    timeout: float = 30.0,
) -> None:
    """Delete a branch on the remote. A 404 (already gone) is success.

    DELETE {base_url}/api/v1/repos/{repo}/branches/{branch}."""
    quoted = urllib.parse.quote(str(branch), safe="/")
    url = f"{base_url.rstrip('/')}/api/v1/repos/{repo}/branches/{quoted}"
    try:
        _request_json(url, token, method="DELETE", timeout=timeout)
    except PullRequestError as e:
        if " 404 " in str(e):
            return
        raise


_LOG_TAIL_BYTES = 64 * 1024


def _request_text(url: str, token: str, timeout: float = 30.0) -> str:
    """Authed GET returning the raw body as text (the logs endpoint is not JSON)."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/plain", "Authorization": f"token {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except urllib.error.HTTPError as e:
        _raise_http_error(e, "GET", url)
    except urllib.error.URLError as e:
        _raise_transport_error(e, url)
    except OSError as e:
        _raise_transport_error(e, url)
    return data[-_LOG_TAIL_BYTES:].decode("utf-8", "replace")


def failed_job_logs(
    base_url: str,
    repo: str,
    token: str,
    head_sha: str,
    timeout: float = 30.0,
) -> str | None:
    """The log tails (last 64 KiB each) of the failed jobs of the latest
    Actions run for `head_sha`.

    None means the jobs/logs endpoints answer 404/405 (Gitea too old): the
    caller cannot classify. "" means no logs could be read for any other
    reason (no run on the first page, other error)."""
    quoted = urllib.parse.quote(str(repo), safe="/")
    root = f"{base_url.rstrip('/')}/api/v1/repos/{quoted}/actions"
    try:
        run_id = find_latest_workflow_run(base_url, repo, token, head_sha, timeout)
        if run_id is None:
            return ""
        parsed = _request_json(f"{root}/runs/{run_id}/jobs", token, timeout=timeout)
        jobs = parsed.get("jobs") if isinstance(parsed, dict) else None
        out: list[str] = []
        for job in jobs or []:
            if not isinstance(job, dict) or job.get("conclusion") != "failure":
                continue
            out.append(
                _request_text(f"{root}/jobs/{int(job['id'])}/logs", token, timeout)
            )
        return "\n".join(out)
    except PullRequestError as e:
        text = str(e)
        if " 404 " in text or " 405 " in text:
            return None
        log.info("failed job logs for %s unavailable: %s", head_sha, e)
        return ""
    except (KeyError, TypeError, ValueError) as e:
        log.info("failed job logs for %s malformed: %s", head_sha, e)
        return ""
