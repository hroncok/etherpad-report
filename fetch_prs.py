#!/usr/bin/env python3
"""
Fetch PR/MR data from Pagure (src.fedoraproject.org), GitHub, and GitLab.com,
and emit a single JSON array combining all items.

Environment variables (compatible with existing scripts):
- DAYS: lookback window in days (default: 7)

GitHub:
- GH_USER: GitHub username (default: hroncok)
- GITHUB_TOKEN: optional token to increase rate limits

GitLab:
- GL_USER: GitLab username (default: hroncok)
- GITLAB_TOKEN: optional token to read private/internal projects and raise limits

Pagure:
- USER_NAME: Pagure username (default: churchyard)
- NS_FILTER: Pagure namespace filter (default: rpms)
- MAX_PAGES: cap for datagrepper paging (default: 20)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlencode

import urllib3


# ---------- HTTP helpers ----------

def log_warn(msg: str) -> None:
    sys.stderr.write(f"WARN: {msg}\n")
    sys.stderr.flush()


def log_error(msg: str) -> None:
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.stderr.flush()


def make_pool() -> urllib3.PoolManager:
    return urllib3.PoolManager(
        num_pools=10,
        retries=urllib3.Retry(
            total=3, connect=3, read=3, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504]
        ),
        timeout=urllib3.Timeout(connect=5.0, read=30.0),
    )


def http_get_json(pool: urllib3.PoolManager, url: str, headers: Optional[Dict[str, str]] = None) -> object:
    r = pool.request("GET", url, headers=headers or {}, preload_content=True, redirect=True)
    if r.status >= 400:
        raise RuntimeError(f"GET {url} -> HTTP {r.status}")
    data = r.data.decode("utf-8", errors="replace")
    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON from {url}: {e}") from e


def http_get_text(pool: urllib3.PoolManager, url: str, headers: Optional[Dict[str, str]] = None) -> str:
    r = pool.request("GET", url, headers=headers or {}, preload_content=True, redirect=True)
    if r.status >= 400:
        raise RuntimeError(f"GET {url} -> HTTP {r.status}")
    return r.data.decode("utf-8", errors="replace")


# ---------- Shared utilities ----------

def iso_utc_days_ago_start_of_day(days: int) -> str:
    dt = (datetime.now(timezone.utc) - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ymd_utc_days_ago(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%d")


def count_patch_adds_dels(patch_text: str) -> Tuple[int, int]:
    adds = 0
    dels = 0
    for line in patch_text.splitlines():
        if line.startswith(("diff ", "index ", "+++", "---")):
            continue
        if line.startswith("+") and not line.startswith("++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("--"):
            dels += 1
    return adds, dels


# ---------- GitLab ----------

def fetch_gitlab(pool: urllib3.PoolManager, gl_user: str, days: int, token: Optional[str]) -> List[Dict]:
    base_list = "https://gitlab.com/api/v4/merge_requests"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["PRIVATE-TOKEN"] = token

    # Resolve author id (more reliable than author_username for global listing)
    users_url = f"https://gitlab.com/api/v4/users?{urlencode({'username': gl_user})}"
    users_json = http_get_json(pool, users_url, headers)
    if not isinstance(users_json, list) or not users_json:
        return []
    author_id = users_json[0].get("id")
    if not author_id:
        return []

    since_iso = iso_utc_days_ago_start_of_day(days)
    page = 1
    per_page = 100
    items: List[Dict] = []

    while True:
        q = {
            "author_id": str(author_id),
            "scope": "all",
            "state": "all",
            "updated_after": since_iso,
            "order_by": "created_at",
            "sort": "desc",
            "per_page": str(per_page),
            "page": str(page),
        }
        url = f"{base_list}?{urlencode(q)}"
        resp = http_get_json(pool, url, headers)
        if not isinstance(resp, list) or not resp:
            break
        for mr in resp:
            if not isinstance(mr, dict):
                continue
            web_url = str(mr.get("web_url") or "")
            title = str(mr.get("title") or "")
            description = str(mr.get("description") or "")
            state = str(mr.get("state") or "")
            target_branch = str(mr.get("target_branch") or "")
            source_branch = str(mr.get("source_branch") or "")
            iid = mr.get("iid")
            references = mr.get("references") or {}
            project_path = ""
            if isinstance(references, dict):
                full_ref = references.get("full") or ""
                if isinstance(full_ref, str) and "!" in full_ref:
                    project_path = full_ref.split("!")[0]
            if not project_path and web_url.startswith("https://gitlab.com/"):
                tmp = web_url[len("https://gitlab.com/") :]
                project_path = tmp.split("/-/merge_requests/")[0]
            repo_short = ""
            short_ref = (references.get("short") if isinstance(references, dict) else "") or ""
            if isinstance(short_ref, str):
                repo_short = short_ref.split("!")[0].split("/")[-1]
            if not repo_short:
                repo_short = project_path.split("/")[-1] if project_path else ""

            # Diff stats
            patch_text = ""
            try:
                patch_text = http_get_text(pool, f"{web_url}.patch")
            except Exception:
                log_warn(f"gitlab: failed to fetch patch for {web_url} (.patch), trying .diff")
                try:
                    patch_text = http_get_text(pool, f"{web_url}.diff")
                except Exception:
                    log_warn(f"gitlab: failed to fetch patch/diff for {web_url}, size set to 0")
                    patch_text = ""
            adds, dels = count_patch_adds_dels(patch_text)

            # Commits
            enc_path = quote(project_path, safe="")
            commits_api = f"https://gitlab.com/api/v4/projects/{enc_path}/merge_requests/{iid}/commits"
            commits_msgs: List[str] = []
            try:
                commits_json = http_get_json(pool, commits_api, headers)
                if isinstance(commits_json, list):
                    for c in commits_json:
                        if isinstance(c, dict) and c.get("message"):
                            commits_msgs.append(str(c["message"]))
            except Exception:
                log_warn(f"gitlab: failed to fetch commits for {web_url}")
                commits_msgs = []

            prefix = "[R]" if web_url.startswith("https://gitlab.com/redhat/") else "[U]"
            items.append(
                {
                    "forge": "gitlab",
                    "prefix": prefix,
                    "repo": repo_short,
                    "title": title,
                    "url": web_url,
                    "state": state,
                    "target_branch": target_branch,
                    "source_branch": source_branch,
                    "adds": int(adds),
                    "dels": int(dels),
                    "total": int(adds) + int(dels),
                    "body": description,
                    "commits": [m for m in (msg.strip() for msg in commits_msgs) if m],
                }
            )
        page += 1
    return items


# ---------- GitHub ----------

def fetch_github(pool: urllib3.PoolManager, gh_user: str, days: int, token: Optional[str]) -> List[Dict]:
    base_search = "https://api.github.com/search/issues"
    accept = "application/vnd.github+json"
    common_q = {
        "q": f"is:pr author:{gh_user} created:>={ymd_utc_days_ago(days)}",
        "per_page": "100",
        "sort": "created",
        "order": "desc",
    }
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    page = 1
    items: List[Dict] = []
    while True:
        q = dict(common_q, page=str(page))
        url = f"{base_search}?{urlencode(q)}"
        resp = http_get_json(pool, url, headers)
        pr_api_urls: List[str] = []
        if isinstance(resp, dict) and isinstance(resp.get("items"), list):
            for it in resp["items"]:
                pr_url = (it.get("pull_request") or {}).get("url") if isinstance(it, dict) else None
                if isinstance(pr_url, str) and pr_url:
                    pr_api_urls.append(pr_url)
        if not pr_api_urls:
            break
        for pr_api in pr_api_urls:
            try:
                pr_json = http_get_json(pool, pr_api, headers)
            except Exception:
                log_warn(f"github: failed to fetch PR metadata {pr_api}")
                continue
            if not isinstance(pr_json, dict):
                continue
            html_url = str(pr_json.get("html_url") or "")
            title = str(pr_json.get("title") or "")
            body = str(pr_json.get("body") or "")
            state = str(pr_json.get("state") or "")
            merged_at = pr_json.get("merged_at") or ""
            adds = int(pr_json.get("additions") or 0)
            dels = int(pr_json.get("deletions") or 0)
            base = pr_json.get("base") or {}
            repo_full = ""
            target_branch = ""
            if isinstance(base, dict):
                repo = base.get("repo") or {}
                if isinstance(repo, dict):
                    repo_full = str(repo.get("full_name") or "")
                target_branch = str(base.get("ref") or "")

            # Commits
            commits_api = f"{pr_api}/commits"
            commits_msgs: List[str] = []
            try:
                commits_json = http_get_json(pool, commits_api, headers)
                if isinstance(commits_json, list):
                    for c in commits_json:
                        msg = ((c or {}).get("commit") or {}).get("message") if isinstance(c, dict) else None
                        if isinstance(msg, str) and msg:
                            commits_msgs.append(msg)
            except Exception:
                log_warn(f"github: failed to fetch commits for {html_url or pr_api}")
                commits_msgs = []

            items.append(
                {
                    "forge": "github",
                    "prefix": "[U]",
                    "repo": repo_full,
                    "title": title,
                    "url": html_url,
                    "state": state,
                    "merged_at": merged_at or "",
                    "target_branch": target_branch,
                    "adds": adds,
                    "dels": dels,
                    "total": adds + dels,
                    "body": body,
                    "commits": [m for m in (msg.strip() for msg in commits_msgs) if m],
                }
            )
        page += 1
    return items


# ---------- Pagure ----------

def fetch_pagure(pool: urllib3.PoolManager, user_name: str, days: int, ns_filter: str, max_pages: int) -> List[Dict]:
    delta = days * 24 * 60 * 60
    base = "https://apps.fedoraproject.org/datagrepper/v2/search"
    qbase = {
        "delta": str(delta),
        "topic": "org.fedoraproject.prod.pagure.pull-request.new",
        "rows_per_page": "100",
        "order": "desc",
        "chrome": "false",
    }

    pages = 1
    first_url = f"{base}?{urlencode(dict(qbase, page='1'))}"
    try:
        first_json = http_get_json(pool, first_url, {"Accept": "application/json"})
    except Exception:
        first_json = None
    if isinstance(first_json, dict):
        pages = int(first_json.get("pages") or first_json.get("pagination", {}).get("pages") or 1)
    if pages > max_pages:
        pages = max_pages

    # Fetch pages
    all_pages: List[object] = []
    if first_json is not None:
        all_pages.append(first_json)
    for i in range(2, pages + 1):
        url = f"{base}?{urlencode(dict(qbase, page=str(i)))}"
        try:
            page_json = http_get_json(pool, url, {"Accept": "application/json"})
        except Exception:
            log_warn(f"pagure: failed to fetch datagrepper page {i}")
            page_json = None
        if page_json is not None:
            all_pages.append(page_json)

    # Extract PR URLs authored by the user in the namespace
    pr_urls: List[str] = []
    for pg in all_pages:
        raw_messages = (pg or {}).get("raw_messages") if isinstance(pg, dict) else None
        if not isinstance(raw_messages, list):
            continue
        for msg in raw_messages:
            body = (msg or {}).get("body") if isinstance(msg, dict) else None
            pr = (body or {}).get("pullrequest") if isinstance(body, dict) else None
            if not isinstance(pr, dict):
                continue
            project = pr.get("project") or {}
            if not (isinstance(project, dict) and str(project.get("namespace") or "") == ns_filter):
                continue
            author_name = ((pr.get("user") or {}).get("name")) if isinstance(pr.get("user"), dict) else None
            from_fullname = ((pr.get("repo_from") or {}).get("fullname")) if isinstance(pr.get("repo_from"), dict) else ""
            authored = (author_name == user_name) or (isinstance(from_fullname, str) and from_fullname.startswith(f"forks/{user_name}/"))
            if not authored:
                continue
            full_url = pr.get("full_url")
            if isinstance(full_url, str) and full_url:
                pr_urls.append(full_url)
    pr_urls = sorted(set(pr_urls))
    if not pr_urls:
        return []

    items: List[Dict] = []
    for url in pr_urls:
        parts = url.split("/")
        try:
            ns, name = parts[3], parts[4]
            pr_id = re.sub(r"[^0-9].*$", "", parts[6])
        except Exception:
            continue
        api = f"https://src.fedoraproject.org/api/0/{ns}/{name}/pull-request/{pr_id}"
        try:
            prjson = http_get_json(pool, api, {"Accept": "application/json"})
        except Exception:
            log_warn(f"pagure: failed to fetch PR metadata {api}")
            prjson = None
        repo_short = str((prjson or {}).get("project", {}).get("name") or name)
        title = str((prjson or {}).get("title") or "")
        status = str((prjson or {}).get("status") or "")
        branch = str((prjson or {}).get("branch") or "")
        full_url = str((prjson or {}).get("full_url") or url)
        initial_comment = str((prjson or {}).get("initial_comment") or "")

        # Patch for diff stats and commit subjects
        patch_text = ""
        try:
            patch_text = http_get_text(pool, f"{url}.patch")
        except Exception:
            try:
                patch_text = http_get_text(pool, f"{url}?format=patch")
            except Exception:
                log_warn(f"pagure: failed to fetch patch for {url}, size set to 0")
                patch_text = ""
        adds, dels = count_patch_adds_dels(patch_text)
        # commit subjects from 'Subject:' lines
        commits_msgs = []
        for line in patch_text.splitlines():
            if line.startswith("Subject: "):
                commits_msgs.append(line[len("Subject: ") :])

        items.append(
            {
                "forge": "pagure",
                "prefix": "[F]",
                "repo": repo_short,
                "title": title,
                "url": full_url,
                "state": status,
                "target_branch": branch,
                "adds": adds,
                "dels": dels,
                "total": adds + dels,
                "body": initial_comment,
                "commits": [m for m in (msg.strip() for msg in commits_msgs) if m],
            }
        )
    return items


# ---------- Main ----------

def main() -> int:
    days = int(os.environ.get("DAYS", "7"))

    # GitLab
    gl_user = os.environ.get("GL_USER", "hroncok")
    gl_token = os.environ.get("GITLAB_TOKEN") or None

    # GitHub
    gh_user = os.environ.get("GH_USER", "hroncok")
    gh_token = os.environ.get("GITHUB_TOKEN") or None

    # Pagure
    fp_user = os.environ.get("USER_NAME", "churchyard")
    ns_filter = os.environ.get("NS_FILTER", "rpms")
    max_pages = int(os.environ.get("MAX_PAGES", "20"))

    pool = make_pool()

    all_items: List[Dict] = []
    had_fatal_error = False
    try:
        all_items.extend(fetch_pagure(pool, fp_user, days, ns_filter, max_pages))
    except Exception as e:
        log_error(f"pagure: {e}")
        had_fatal_error = True
    try:
        all_items.extend(fetch_github(pool, gh_user, days, gh_token))
    except Exception as e:
        log_error(f"github: {e}")
        had_fatal_error = True
    try:
        all_items.extend(fetch_gitlab(pool, gl_user, days, gl_token))
    except Exception as e:
        log_error(f"gitlab: {e}")
        had_fatal_error = True

    sys.stdout.write(json.dumps(all_items, ensure_ascii=False, indent=4))
    sys.stdout.flush()
    # If any forge failed entirely, signal non-zero exit to report as broken
    return 1 if had_fatal_error else 0


if __name__ == "__main__":
    raise SystemExit(main())


