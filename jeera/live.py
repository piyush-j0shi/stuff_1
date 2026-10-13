import datetime as dt
import functools
import itertools
import re

import requests

from jeera import config, db
from jeera.ingest import jira_sync, slack_sync

base_fields = ["summary", "status", "issuetype", "priority", "labels", "assignee", "duedate", "parent", "created", "updated", "resolutiondate"]


@functools.lru_cache(maxsize=1)
def jira_ids():
    return jira_sync.field_ids()


@functools.lru_cache(maxsize=1)
def slack_ctx():
    with db.connect() as conn:
        return slack_sync.context(conn)


def compact(issue):
    # the fields the agent needs from a raw jira issue
    ids, f = jira_ids(), issue["fields"]
    sprints = f.get(ids.get("sprint")) or []
    current = [s for s in sprints if s["state"] in ("active", "future")] or sprints[-1:]
    out = {"key" : issue["key"], "summary" : f["summary"], "type" : f["issuetype"]["name"], "status" : f["status"]["name"],
           "owner" : jira_sync.owner_of(f), "priority" : (f.get("priority") or {}).get("name"), "due_date" : f.get("duedate"),
           "start_date" : f.get(ids.get("start")), "points" : f.get(ids.get("points")), "flagged" : bool(f.get(ids.get("flagged"))),
           "sprint" : f"{current[0]['name']} ({current[0]['state']})" if current else "backlog",
           "epic" : (f.get("parent") or {}).get("key"), "updated" : (f.get("updated") or "")[:10],
           "url" : f"{config.jira_base_url}/browse/{issue['key']}"}
    return {k : v for k, v in out.items() if v not in (None, False, "")}


def jira_error(e):
    try:
        body = e.response.json()
        return {"error" : "; ".join(body.get("errorMessages", [])) or str(body.get("errors"))}
    except Exception:
        return {"error" : str(e)}


def jira_search(jql, limit=50):
    fields = base_fields + list(jira_ids().values())
    try:
        issues = list(itertools.islice(jira_sync.search(jql, fields), min(int(limit), 100)))
    except requests.HTTPError as e:
        return jira_error(e)
    return {"jql" : jql, "count" : len(issues), "issues" : [compact(i) for i in issues]}


def jira_issue(key):
    fields = base_fields + list(jira_ids().values()) + ["description", "comment", "issuelinks", "subtasks"]
    try:
        issue = jira_sync.get(f"/rest/api/3/issue/{key}", fields=",".join(fields))
    except requests.HTTPError as e:
        return jira_error(e)
    f = issue["fields"]
    links = []
    for ln in f.get("issuelinks") or []:
        other = ln.get("outwardIssue") or ln.get("inwardIssue")
        relation = ln["type"]["outward"] if "outwardIssue" in ln else ln["type"]["inward"]
        links.append({"relation" : f"this issue {relation}", "key" : other["key"], "summary" : other["fields"]["summary"],
                      "status" : other["fields"]["status"]["name"]})
    comments = []
    for c in (f.get("comment") or {}).get("comments", []):
        author, body = jira_sync.parse_comment(c)
        comments.append({"author" : author, "created" : c["created"][:10], "body" : body})
    out = compact(issue)
    out.update({"description" : jira_sync.adf_text(f.get("description")).strip(), "links" : links, "comments" : comments})
    children = jira_search(f"parent = {key} order by key")
    if children.get("issues"):
        out["children"] = children["issues"]
    return out


def parse_permalink(url):
    # https://x.slack.com/archives/C123/p1700000000123456?thread_ts=1700000000.000100
    m = re.search(r"archives/(\w+)/p(\d{10})(\d{6})", url or "")
    if not m:
        return None
    thread = re.search(r"thread_ts=([\d.]+)", url)
    return {"channel" : m.group(1), "ts" : f"{m.group(2)}.{m.group(3)}", "thread_ts" : thread.group(1) if thread else None}


def message(m, channel):
    row = slack_sync.to_row(m, channel, slack_ctx())
    return {"author" : row[3], "posted_at" : row[5].strftime("%Y-%m-%d %H:%M"), "text" : row[4][:800],
            "is_reply" : bool(row[2] and row[2] != row[1]), "url" : row[10]}


def slack_thread(channel, ts):
    ctx = slack_ctx()
    msgs = slack_sync.paged(slack_sync.client.conversations_replies, "messages", channel=channel, ts=ts, include_all_metadata=True)
    return [message(m, channel) for m in msgs if not slack_sync.skip(m, ctx)]


def slack_history(channel, since=None, until=None, limit=150):
    # straight from slack, for conversations that are not synced such as dms
    kw = {"channel" : channel, "include_all_metadata" : True}
    if since:
        kw["oldest"] = str(dt.datetime.fromisoformat(since).timestamp())
    if until:
        kw["latest"] = str((dt.datetime.fromisoformat(until) + dt.timedelta(days=1)).timestamp())
    msgs = list(itertools.islice(slack_sync.paged(slack_sync.client.conversations_history, "messages", **kw), limit))
    ctx = slack_ctx()
    return [message(m, channel) for m in reversed(msgs) if not slack_sync.skip(m, ctx)]
