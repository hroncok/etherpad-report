#!/usr/bin/env python3
"""
Fetch PR/MR activity from Pagure, GitHub, GitLab, and Forgejo,
and emit a single JSON array combining all items.

Tracks three kinds of activity:
  authored  — PRs/MRs the user opened
  reviewed  — PRs/MRs the user reviewed (comments, approvals, flags)
  merged    — PRs/MRs the user merged (but did not author)

Environment variables:
  DAYS              lookback window in days (default: 7)

  PAGURE_USER       Pagure username (default: churchyard, fallback: USER_NAME)
  PAGURE_NAMESPACES comma-separated namespace filter (default: rpms, fallback: NS_FILTER as single value)
  MAX_PAGES         datagrepper paging cap (default: 20)

  GH_USER           GitHub username (default: hroncok)
  GITHUB_TOKEN      optional, increases rate limits

  GL_USER           GitLab username (default: hroncok)
  GITLAB_TOKEN      optional, reads private projects and raises limits

  FORGEJO_USER      Forgejo username (default: churchyard)
  FORGEJO_TOKEN     optional but recommended (Anubis anti-bot may block without it)
  FORGEJO_URL       Forgejo instance URL (default: https://forge.fedoraproject.org)
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode

import urllib3


# ── Logging ──────────────────────────────────────────────────────────

def warn(msg: str) -> None:
    print(f"WARN: {msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)


def info(msg: str) -> None:
    print(f"INFO: {msg}", file=sys.stderr, flush=True)


# ── HTTP helpers ─────────────────────────────────────────────────────

def make_pool() -> urllib3.PoolManager:
    return urllib3.PoolManager(
        num_pools=10,
        retries=urllib3.Retry(
            total=3, connect=3, read=3,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
        ),
        timeout=urllib3.Timeout(connect=5.0, read=30.0),
    )


def get_json(pool: urllib3.PoolManager, url: str,
             headers: dict[str, str] | None = None) -> Any:
    r = pool.request("GET", url, headers=headers or {},
                     preload_content=True, redirect=True)
    if r.status >= 400:
        raise RuntimeError(f"GET {url} → HTTP {r.status}")
    return json.loads(r.data.decode("utf-8", errors="replace"))


def get_text(pool: urllib3.PoolManager, url: str,
             headers: dict[str, str] | None = None) -> str:
    r = pool.request("GET", url, headers=headers or {},
                     preload_content=True, redirect=True)
    if r.status >= 400:
        raise RuntimeError(f"GET {url} → HTTP {r.status}")
    return r.data.decode("utf-8", errors="replace")


# ── Shared utilities ─────────────────────────────────────────────────

def since_iso(days: int) -> str:
    dt = (datetime.now(timezone.utc) - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def since_ymd(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def count_diff(patch: str) -> tuple[int, int]:
    adds = dels = 0
    for line in patch.splitlines():
        if line.startswith(("diff ", "index ", "+++", "---")):
            continue
        if line.startswith("+"):
            adds += 1
        elif line.startswith("-"):
            dels += 1
    return adds, dels


def extract_commit_subjects(patch: str) -> list[str]:
    subjects = []
    for line in patch.splitlines():
        if line.startswith("Subject: "):
            subjects.append(line[len("Subject: "):].strip())
    return subjects


# ── Data model ───────────────────────────────────────────────────────

@dataclasses.dataclass
class Activity:
    forge: str
    prefix: str
    repo: str
    title: str
    url: str
    state: str
    activity: str          # "authored" | "reviewed" | "merged"
    target_branch: str = ""
    source_branch: str = ""
    merged_at: str = ""
    adds: int = 0
    dels: int = 0
    total: int = 0
    body: str = ""
    commits: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["total"] = d["adds"] + d["dels"]
        return d


# ── Pagure (src.fedoraproject.org) ───────────────────────────────────

class PagureForge:
    DATAGREPPER = "https://apps.fedoraproject.org/datagrepper/v2/search"
    API = "https://src.fedoraproject.org/api/0"

    def __init__(self, pool: urllib3.PoolManager, user: str, days: int,
                 namespaces: list[str], max_pages: int):
        self.pool = pool
        self.user = user
        self.days = days
        self.namespaces = namespaces
        self.max_pages = max_pages

    # -- datagrepper helpers --

    def _dg_search(self, topic: str) -> list[dict]:
        delta = self.days * 86400
        base_q: dict[str, str] = {
            "delta": str(delta),
            "topic": topic,
            "rows_per_page": "100",
            "order": "desc",
            "chrome": "false",
        }
        first_url = f"{self.DATAGREPPER}?{urlencode({**base_q, 'page': '1'})}"
        first = get_json(self.pool, first_url,
                         {"Accept": "application/json"})
        pages = min(
            int((first or {}).get("pages")
                or (first or {}).get("pagination", {}).get("pages")
                or 1),
            self.max_pages)
        all_msgs: list[dict] = []
        for pg_data in [first] + [
            get_json(self.pool,
                     f"{self.DATAGREPPER}?{urlencode({**base_q, 'page': str(i)})}",
                     {"Accept": "application/json"})
            for i in range(2, pages + 1)
        ]:
            for msg in (pg_data or {}).get("raw_messages", []):
                if isinstance(msg, dict):
                    all_msgs.append(msg)
        return all_msgs

    def _pr_url_from_msg(self, msg: dict) -> str | None:
        body = msg.get("body") or {}
        pr = body.get("pullrequest") if isinstance(body, dict) else None
        if not isinstance(pr, dict):
            return None
        project = pr.get("project") or {}
        ns = str(project.get("namespace") or "")
        if ns and ns not in self.namespaces:
            return None
        url = pr.get("full_url")
        return str(url) if isinstance(url, str) and url else None

    def _pr_author_from_msg(self, msg: dict) -> str | None:
        body = msg.get("body") or {}
        pr = body.get("pullrequest") if isinstance(body, dict) else None
        if not isinstance(pr, dict):
            return None
        user = pr.get("user")
        if isinstance(user, dict):
            return str(user.get("name") or "")
        return None

    def _pr_agent_from_msg(self, msg: dict) -> str | None:
        body = msg.get("body") or {}
        agent = body.get("agent") if isinstance(body, dict) else None
        return str(agent) if agent else None

    def _pr_status_from_msg(self, msg: dict) -> str | None:
        body = msg.get("body") or {}
        pr = body.get("pullrequest") if isinstance(body, dict) else None
        if not isinstance(pr, dict):
            return None
        return str(pr.get("status") or "")

    def _is_authored_by_user(self, msg: dict) -> bool:
        body = msg.get("body") or {}
        pr = body.get("pullrequest") if isinstance(body, dict) else None
        if not isinstance(pr, dict):
            return False
        author = ((pr.get("user") or {}).get("name")) if isinstance(pr.get("user"), dict) else None
        from_fullname = ((pr.get("repo_from") or {}).get("fullname")) if isinstance(pr.get("repo_from"), dict) else ""
        return (
            author == self.user
            or (isinstance(from_fullname, str)
                and from_fullname.startswith(f"forks/{self.user}/"))
        )

    def _fetch_pr_detail(self, url: str) -> Activity | None:
        parts = url.rstrip("/").split("/")
        try:
            ns, name = parts[3], parts[4]
            pr_id = re.sub(r"[^0-9].*$", "", parts[6])
        except (IndexError, ValueError):
            return None
        api_url = f"{self.API}/{ns}/{name}/pull-request/{pr_id}"
        try:
            pr = get_json(self.pool, api_url, {"Accept": "application/json"})
        except Exception:
            warn(f"pagure: cannot fetch {api_url}")
            pr = None
        if not isinstance(pr, dict):
            return None
        repo_short = str((pr.get("project") or {}).get("name") or name)
        title = str(pr.get("title") or "")
        status = str(pr.get("status") or "")
        branch = str(pr.get("branch") or "")
        full_url = str(pr.get("full_url") or url)
        body_text = str(pr.get("initial_comment") or "")

        patch = ""
        for patch_url in [f"{url}.patch", f"{url}?format=patch"]:
            try:
                patch = get_text(self.pool, patch_url)
                break
            except Exception:
                continue
        if not patch:
            warn(f"pagure: cannot fetch patch for {url}")
        adds, dels = count_diff(patch)
        commits = extract_commit_subjects(patch)

        return Activity(
            forge="pagure", prefix="[F]", repo=repo_short,
            title=title, url=full_url, state=status,
            activity="authored",
            target_branch=branch,
            adds=adds, dels=dels, total=adds + dels,
            body=body_text, commits=commits,
        )

    # -- public interface --

    def fetch(self) -> list[Activity]:
        items: list[Activity] = []
        seen_urls: set[str] = set()

        # Authored PRs
        info("pagure: fetching authored PRs…")
        for msg in self._dg_search(
                "org.fedoraproject.prod.pagure.pull-request.new"):
            if not self._is_authored_by_user(msg):
                continue
            url = self._pr_url_from_msg(msg)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            item = self._fetch_pr_detail(url)
            if item:
                items.append(item)

        authored_urls = set(seen_urls)

        # Merged PRs (by user, not self-authored)
        info("pagure: fetching merged PRs…")
        for msg in self._dg_search(
                "org.fedoraproject.prod.pagure.pull-request.closed"):
            if self._pr_status_from_msg(msg) != "Merged":
                continue
            agent = self._pr_agent_from_msg(msg)
            if agent != self.user:
                continue
            url = self._pr_url_from_msg(msg)
            if not url or url in authored_urls:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            item = self._fetch_pr_detail(url)
            if item:
                item.activity = "merged"
                items.append(item)

        merged_urls = set(seen_urls) - authored_urls

        # Reviewed PRs (comments by user, not self-authored, not already merged-by-user)
        info("pagure: fetching reviewed PRs…")
        for topic in [
            "org.fedoraproject.prod.pagure.pull-request.comment.added",
            "org.fedoraproject.prod.pagure.pull-request.flag.added",
            "org.fedoraproject.prod.pagure.pull-request.flag.updated",
        ]:
            for msg in self._dg_search(topic):
                agent = self._pr_agent_from_msg(msg)
                if agent != self.user:
                    continue
                url = self._pr_url_from_msg(msg)
                if not url or url in seen_urls:
                    continue
                author = self._pr_author_from_msg(msg)
                if author == self.user:
                    continue
                seen_urls.add(url)
                item = self._fetch_pr_detail(url)
                if item:
                    item.activity = "reviewed"
                    items.append(item)

        return items


# ── GitHub ───────────────────────────────────────────────────────────

class GitHubForge:
    SEARCH = "https://api.github.com/search/issues"
    EVENTS = "https://api.github.com/users/{user}/events"

    def __init__(self, pool: urllib3.PoolManager, user: str, days: int,
                 token: str | None):
        self.pool = pool
        self.user = user
        self.days = days
        self.headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _search_prs(self, query: str) -> list[dict]:
        prs: list[dict] = []
        page = 1
        while True:
            q = urlencode({
                "q": query,
                "per_page": "100",
                "sort": "created",
                "order": "desc",
                "page": str(page),
            })
            resp = get_json(self.pool, f"{self.SEARCH}?{q}", self.headers)
            hits = resp.get("items", []) if isinstance(resp, dict) else []
            if not hits:
                break
            for hit in hits:
                pr_api = (hit.get("pull_request") or {}).get("url")
                if not pr_api:
                    continue
                try:
                    pr = get_json(self.pool, pr_api, self.headers)
                except Exception:
                    warn(f"github: cannot fetch {pr_api}")
                    continue
                if isinstance(pr, dict):
                    prs.append(pr)
            page += 1
        return prs

    def _pr_to_activity(self, pr: dict, activity: str) -> Activity:
        html_url = str(pr.get("html_url") or "")
        base = pr.get("base") or {}
        repo = (base.get("repo") or {}) if isinstance(base, dict) else {}
        repo_full = str(repo.get("full_name") or "") if isinstance(repo, dict) else ""
        target = str(base.get("ref") or "") if isinstance(base, dict) else ""
        head = pr.get("head") or {}
        source = str(head.get("ref") or "") if isinstance(head, dict) else ""

        commits_msgs: list[str] = []
        commits_api = str(pr.get("url") or "") + "/commits"
        if commits_api.startswith("http"):
            try:
                cdata = get_json(self.pool, commits_api, self.headers)
                if isinstance(cdata, list):
                    for c in cdata:
                        m = ((c or {}).get("commit") or {}).get("message")
                        if isinstance(m, str) and m.strip():
                            commits_msgs.append(m.strip())
            except Exception:
                warn(f"github: cannot fetch commits for {html_url}")

        adds = int(pr.get("additions") or 0)
        dels = int(pr.get("deletions") or 0)

        return Activity(
            forge="github", prefix="[U]", repo=repo_full,
            title=str(pr.get("title") or ""),
            url=html_url,
            state=str(pr.get("state") or ""),
            activity=activity,
            merged_at=str(pr.get("merged_at") or ""),
            target_branch=target,
            source_branch=source,
            adds=adds, dels=dels, total=adds + dels,
            body=str(pr.get("body") or ""),
            commits=commits_msgs,
        )

    def _fetch_merged_via_events(self, authored_urls: set[str],
                                 reviewed_urls: set[str]) -> list[Activity]:
        items: list[Activity] = []
        seen: set[str] = set()
        page = 1
        while page <= 10:
            url = self.EVENTS.format(user=self.user) + f"?per_page=100&page={page}"
            try:
                events = get_json(self.pool, url, self.headers)
            except Exception:
                break
            if not isinstance(events, list) or not events:
                break
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") != "PullRequestEvent":
                    continue
                payload = ev.get("payload") or {}
                if payload.get("action") != "closed":
                    continue
                pr = payload.get("pull_request") or {}
                if not pr.get("merged"):
                    continue
                html_url = str(pr.get("html_url") or "")
                if not html_url or html_url in seen:
                    continue
                if html_url in authored_urls or html_url in reviewed_urls:
                    continue
                author = ((pr.get("user") or {}).get("login") or "").lower()
                if author == self.user.lower():
                    continue
                seen.add(html_url)
                items.append(self._pr_to_activity(pr, "merged"))
            page += 1
        return items

    def fetch(self) -> list[Activity]:
        items: list[Activity] = []
        ymd = since_ymd(self.days)

        # Authored
        info("github: fetching authored PRs…")
        authored_prs = self._search_prs(
            f"is:pr author:{self.user} created:>={ymd}")
        authored_urls: set[str] = set()
        for pr in authored_prs:
            act = self._pr_to_activity(pr, "authored")
            items.append(act)
            authored_urls.add(act.url)

        # Reviewed (excluding self-authored)
        info("github: fetching reviewed PRs…")
        reviewed_prs = self._search_prs(
            f"is:pr reviewed-by:{self.user} -author:{self.user} created:>={ymd}")
        reviewed_urls: set[str] = set()
        for pr in reviewed_prs:
            act = self._pr_to_activity(pr, "reviewed")
            items.append(act)
            reviewed_urls.add(act.url)

        # Merged (via events API, excluding authored and reviewed)
        info("github: fetching merged PRs via events…")
        items.extend(self._fetch_merged_via_events(authored_urls, reviewed_urls))

        return items


# ── GitLab ───────────────────────────────────────────────────────────

class GitLabForge:
    BASE = "https://gitlab.com/api/v4"

    def __init__(self, pool: urllib3.PoolManager, user: str, days: int,
                 token: str | None):
        self.pool = pool
        self.user = user
        self.days = days
        self.headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
            self.headers["PRIVATE-TOKEN"] = token
        self.author_id: int | None = None

    def _resolve_user_id(self) -> int | None:
        if self.author_id is not None:
            return self.author_id
        url = f"{self.BASE}/users?{urlencode({'username': self.user})}"
        users = get_json(self.pool, url, self.headers)
        if isinstance(users, list) and users:
            self.author_id = int(users[0].get("id") or 0) or None
        return self.author_id

    def _list_mrs(self, extra_params: dict[str, str]) -> list[dict]:
        iso = since_iso(self.days)
        mrs: list[dict] = []
        page = 1
        while True:
            q = {
                "scope": "all",
                "state": "all",
                "updated_after": iso,
                "order_by": "created_at",
                "sort": "desc",
                "per_page": "100",
                "page": str(page),
                **extra_params,
            }
            url = f"{self.BASE}/merge_requests?{urlencode(q)}"
            resp = get_json(self.pool, url, self.headers)
            if not isinstance(resp, list) or not resp:
                break
            mrs.extend(r for r in resp if isinstance(r, dict))
            page += 1
        return mrs

    def _mr_to_activity(self, mr: dict, activity: str) -> Activity:
        web_url = str(mr.get("web_url") or "")
        references = mr.get("references") or {}
        project_path = ""
        if isinstance(references, dict):
            full_ref = references.get("full") or ""
            if isinstance(full_ref, str) and "!" in full_ref:
                project_path = full_ref.split("!")[0]
        if not project_path and web_url.startswith("https://gitlab.com/"):
            tmp = web_url[len("https://gitlab.com/"):]
            project_path = tmp.split("/-/merge_requests/")[0]

        repo_short = ""
        if isinstance(references, dict):
            short_ref = str(references.get("short") or "")
            repo_short = short_ref.split("!")[0].split("/")[-1]
        if not repo_short:
            repo_short = project_path.split("/")[-1] if project_path else ""

        # Diff stats from patch
        patch = ""
        for suffix in [".patch", ".diff"]:
            try:
                patch = get_text(self.pool, web_url + suffix)
                break
            except Exception:
                continue
        if not patch:
            warn(f"gitlab: cannot fetch patch for {web_url}")
        adds, dels = count_diff(patch)

        # Commits
        iid = mr.get("iid")
        commits_msgs: list[str] = []
        if project_path and iid:
            enc = quote(project_path, safe="")
            capi = f"{self.BASE}/projects/{enc}/merge_requests/{iid}/commits"
            try:
                cdata = get_json(self.pool, capi, self.headers)
                if isinstance(cdata, list):
                    for c in cdata:
                        if isinstance(c, dict) and c.get("message"):
                            commits_msgs.append(str(c["message"]).strip())
            except Exception:
                warn(f"gitlab: cannot fetch commits for {web_url}")

        prefix = "[R]" if web_url.startswith("https://gitlab.com/redhat/") else "[U]"

        return Activity(
            forge="gitlab", prefix=prefix, repo=repo_short,
            title=str(mr.get("title") or ""),
            url=web_url,
            state=str(mr.get("state") or ""),
            activity=activity,
            target_branch=str(mr.get("target_branch") or ""),
            source_branch=str(mr.get("source_branch") or ""),
            adds=adds, dels=dels, total=adds + dels,
            body=str(mr.get("description") or ""),
            commits=commits_msgs,
        )

    def fetch(self) -> list[Activity]:
        uid = self._resolve_user_id()
        if not uid:
            warn("gitlab: cannot resolve user ID, skipping")
            return []

        items: list[Activity] = []
        seen_urls: set[str] = set()

        # Authored
        info("gitlab: fetching authored MRs…")
        for mr in self._list_mrs({"author_id": str(uid)}):
            act = self._mr_to_activity(mr, "authored")
            if act.url not in seen_urls:
                seen_urls.add(act.url)
                items.append(act)
        authored_urls = set(seen_urls)

        # Reviewed (reviewer_id, excluding self-authored)
        info("gitlab: fetching reviewed MRs…")
        for mr in self._list_mrs({"reviewer_id": str(uid)}):
            web_url = str(mr.get("web_url") or "")
            if web_url in authored_urls:
                continue
            author = mr.get("author") or {}
            if isinstance(author, dict) and author.get("id") == uid:
                continue
            act = self._mr_to_activity(mr, "reviewed")
            if act.url not in seen_urls:
                seen_urls.add(act.url)
                items.append(act)

        # Merged (via reviewed MRs that are now merged, filter by merge_user)
        # GitLab has no "merged_by" query param, but we can check MRs
        # we reviewed that ended up merged by us, plus use the
        # authenticated user's scope if token is available.
        info("gitlab: fetching merged MRs…")
        if self.headers.get("PRIVATE-TOKEN"):
            # With a token, we can list MRs we participated in
            for mr in self._list_mrs({"scope": "all", "state": "merged"}):
                web_url = str(mr.get("web_url") or "")
                if web_url in seen_urls:
                    continue
                merge_user = (mr.get("merge_user")
                              or mr.get("merged_by") or {})
                if not isinstance(merge_user, dict):
                    continue
                if merge_user.get("id") != uid:
                    continue
                author = mr.get("author") or {}
                if isinstance(author, dict) and author.get("id") == uid:
                    continue
                act = self._mr_to_activity(mr, "merged")
                seen_urls.add(act.url)
                items.append(act)
        else:
            info("gitlab: skipping merged MRs (no token, query too broad)")

        return items


# ── Forgejo (forge.fedoraproject.org) ────────────────────────────────

class ForgejoForge:
    def __init__(self, pool: urllib3.PoolManager, user: str, days: int,
                 token: str | None,
                 base_url: str = "https://forge.fedoraproject.org"):
        self.pool = pool
        self.user = user
        self.days = days
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v1"
        self.headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            self.headers["Authorization"] = f"token {token}"

    def _fetch_activity_feed(self) -> list[dict]:
        events: list[dict] = []
        page = 1
        while page <= 10:
            url = (f"{self.api}/users/{self.user}/activities/feeds"
                   f"?only-performed-by=true&page={page}&limit=50")
            try:
                resp = get_json(self.pool, url, self.headers)
            except Exception as e:
                if page == 1:
                    warn(f"forgejo: cannot fetch activity feed: {e}")
                break
            if not isinstance(resp, list) or not resp:
                break
            events.extend(r for r in resp if isinstance(r, dict))
            page += 1
        return events

    def _fetch_pr_detail(self, owner: str, repo: str,
                         pr_number: int) -> dict | None:
        url = f"{self.api}/repos/{owner}/{repo}/pulls/{pr_number}"
        try:
            pr = get_json(self.pool, url, self.headers)
            return pr if isinstance(pr, dict) else None
        except Exception:
            warn(f"forgejo: cannot fetch {url}")
            return None

    def _pr_to_activity(self, owner: str, repo: str,
                        pr: dict, activity: str) -> Activity:
        html_url = str(pr.get("html_url")
                       or f"{self.base_url}/{owner}/{repo}/pulls/{pr.get('number')}")
        adds = int(pr.get("additions") or 0)
        dels = int(pr.get("deletions") or 0)

        commits_msgs: list[str] = []
        # Forgejo PR response may not include commits directly;
        # fetch them if we have the API URL
        number = pr.get("number")
        if number:
            capi = f"{self.api}/repos/{owner}/{repo}/pulls/{number}/commits"
            try:
                cdata = get_json(self.pool, capi, self.headers)
                if isinstance(cdata, list):
                    for c in cdata:
                        m = ((c or {}).get("commit") or {}).get("message")
                        if isinstance(m, str) and m.strip():
                            commits_msgs.append(m.strip())
            except Exception:
                pass

        prefix = "[F]"

        return Activity(
            forge="forgejo", prefix=prefix, repo=repo,
            title=str(pr.get("title") or ""),
            url=html_url,
            state=str(pr.get("state") or ""),
            activity=activity,
            target_branch=str(pr.get("base", {}).get("ref") or ""),
            source_branch=str(pr.get("head", {}).get("ref") or ""),
            merged_at=str(pr.get("merged_at") or ""),
            adds=adds, dels=dels, total=adds + dels,
            body=str(pr.get("body") or ""),
            commits=commits_msgs,
        )

    def fetch(self) -> list[Activity]:
        items: list[Activity] = []
        seen_urls: set[str] = set()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.days)

        info("forgejo: fetching activity feed…")
        events = self._fetch_activity_feed()

        # Group PR events by (repo, number) → classify activity
        pr_events: dict[tuple[str, str, int], str] = {}
        for ev in events:
            created = ev.get("created")
            if isinstance(created, str):
                try:
                    ts = datetime.fromisoformat(
                        created.replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue
                except ValueError:
                    pass

            op_type = str(ev.get("op_type") or "")
            repo_obj = ev.get("repo") or {}
            if not isinstance(repo_obj, dict):
                continue
            full_name = str(repo_obj.get("full_name") or "")
            if "/" not in full_name:
                continue
            owner, repo_name = full_name.split("/", 1)

            content = ev.get("content") or {}
            if not isinstance(content, dict):
                continue
            pr_number = content.get("number")
            if not isinstance(pr_number, int) or pr_number <= 0:
                # try to get it from the ID field used in some event types
                continue

            key = (owner, repo_name, pr_number)

            if op_type in ("create_pull_request",):
                pr_events.setdefault(key, "authored")
            elif op_type in ("merge_pull_request",):
                # Only count as "merged" if not self-authored
                existing = pr_events.get(key)
                if existing != "authored":
                    pr_events[key] = "merged"
            elif op_type in ("comment_pull", "approve_pull_request",
                             "reject_pull_request"):
                existing = pr_events.get(key)
                if existing not in ("authored", "merged"):
                    pr_events[key] = "reviewed"

        # Fetch details for each PR
        for (owner, repo_name, pr_number), activity in pr_events.items():
            pr = self._fetch_pr_detail(owner, repo_name, pr_number)
            if not pr:
                continue
            # Skip reviews on own PRs
            if activity != "authored":
                pr_author = ((pr.get("user") or {}).get("login") or "").lower()
                if pr_author == self.user.lower():
                    continue
            act = self._pr_to_activity(owner, repo_name, pr, activity)
            if act.url not in seen_urls:
                seen_urls.add(act.url)
                items.append(act)

        return items


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    days = int(os.environ.get("DAYS", "7"))

    pagure_user = (os.environ.get("PAGURE_USER")
                   or os.environ.get("USER_NAME")
                   or "churchyard")
    raw_ns = (os.environ.get("PAGURE_NAMESPACES")
              or os.environ.get("NS_FILTER")
              or "rpms")
    pagure_ns = [s.strip() for s in raw_ns.split(",") if s.strip()]
    max_pages = int(os.environ.get("MAX_PAGES", "20"))

    gh_user = os.environ.get("GH_USER", "hroncok")
    gh_token = os.environ.get("GITHUB_TOKEN") or None

    gl_user = os.environ.get("GL_USER", "hroncok")
    gl_token = os.environ.get("GITLAB_TOKEN") or None

    fj_user = os.environ.get("FORGEJO_USER", "churchyard")
    fj_token = os.environ.get("FORGEJO_TOKEN") or None
    fj_url = os.environ.get("FORGEJO_URL",
                            "https://forge.fedoraproject.org")

    pool = make_pool()
    all_items: list[Activity] = []
    had_error = False

    forges: list[tuple[str, Any]] = [
        ("pagure", lambda: PagureForge(pool, pagure_user, days,
                                       pagure_ns, max_pages).fetch()),
        ("github", lambda: GitHubForge(pool, gh_user, days,
                                       gh_token).fetch()),
        ("gitlab", lambda: GitLabForge(pool, gl_user, days,
                                       gl_token).fetch()),
    ]
    forges.append(
        ("forgejo", lambda: ForgejoForge(pool, fj_user, days,
                                         fj_token, fj_url).fetch())
    )

    for name, fetch_fn in forges:
        try:
            results = fetch_fn()
            info(f"{name}: {len(results)} items")
            all_items.extend(results)
        except Exception as e:
            error(f"{name}: {e}")
            had_error = True

    sys.stdout.write(json.dumps(
        [item.to_dict() for item in all_items],
        ensure_ascii=False, indent=4))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
