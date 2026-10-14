import json
import re

import requests
from psycopg.types.json import Jsonb

from jeera import config, db

session = requests.Session()
session.auth = (config.jira_email, config.jira_api_token)
session.headers.update({"Accept" : "application/json"})


def get(path, **params):
    r = session.get(config.jira_base_url + path, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def post(path, body):
    r = session.post(config.jira_base_url + path, json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def call(method, path, body=None):
    # writes, some of them answer with an empty body
    r = session.request(method, config.jira_base_url + path, json=body, timeout=60)
    r.raise_for_status()
    return r.json() if r.content else {}


def field_ids():
    # custom field ids differ per jira site, so look them up by name
    names = {"Sprint" : "sprint", "Flagged" : "flagged", "Story point estimate" : "points", "Story Points" : "points", "Start date" : "start"}
    out = {}
    for f in get("/rest/api/3/field"):
        if f["name"] in names and names[f["name"]] not in out:
            out[names[f["name"]]] = f["id"]
    return out


def adf_text(node):
    # turn jira's rich text json into plain text
    if not node:
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    inner = "".join(adf_text(c) for c in node.get("content", []))
    if kind == "heading":
        return "## " + inner + "\n"
    if kind == "listItem":
        return "- " + inner.strip() + "\n"
    if kind == "codeBlock":
        return "```\n" + inner + "\n```\n"
    if kind in ("paragraph", "bulletList", "orderedList"):
        return inner + "\n"
    if kind == "hardBreak":
        return "\n"
    return inner


def board_for(project_key):
    boards = get("/rest/agile/1.0/board", projectKeyOrId=project_key)["values"]
    return boards[0]["id"] if boards else None


def sprints_for(board_id):
    out, start = [], 0
    while True:
        page = get(f"/rest/agile/1.0/board/{board_id}/sprint", startAt=start, maxResults=50)
        out += page["values"]
        if page.get("isLast", True):
            return out
        start += len(page["values"])


def search(jql, fields):
    token = None
    while True:
        body = {"jql" : jql, "fields" : fields, "maxResults" : 100}
        if token:
            body["nextPageToken"] = token
        page = post("/rest/api/3/search/jql", body)
        yield from page["issues"]
        token = page.get("nextPageToken")
        if not token:
            return


def pick_sprint(sprints, done):
    # open sprint first, otherwise the last closed one for finished work
    for state in ("active", "future"):
        hit = [s for s in sprints if s["state"] == state]
        if hit:
            return hit[0]["id"]
    closed = [s for s in sprints if s["state"] == "closed"]
    return closed[-1]["id"] if closed and done else None


def owner_of(fields):
    for label in fields.get("labels") or []:
        if label.startswith("owner-"):
            return label[6:]
    who = fields.get("assignee")
    return who["displayName"].lower() if who else None


def parse_comment(c):
    # seeded comments look like "[john] text"
    body = adf_text(c.get("body")).strip()
    m = re.match(r"\[(\w+)\]\s*(.*)", body, re.S)
    if m:
        return m.group(1), m.group(2)
    return c.get("author", {}).get("displayName", "").lower(), body


def load_people(conn):
    people = json.loads(config.people_file.read_text())
    for p in people:
        conn.execute("""insert into people (id, name, title, email, slack_user_id) values (%s, %s, %s, %s, %s)
            on conflict (id) do update set name = excluded.name, title = excluded.title, email = excluded.email,
            slack_user_id = coalesce(excluded.slack_user_id, people.slack_user_id)""",
            (p["id"], p["name"], p["title"], p["email"] or None, p["slack_user_id"]))
    return len(people)


def sync():
    ids = field_ids()
    wanted = ["summary", "description", "status", "issuetype", "priority", "labels", "assignee", "duedate", "parent",
              "issuelinks", "created", "updated", "resolutiondate", "comment"] + list(ids.values())
    stats = {"projects" : 0, "sprints" : 0, "issues" : 0, "links" : 0, "comments" : 0}
    with db.connect() as conn:
        run_id = conn.execute("insert into sync_runs (source) values ('jira') returning id").fetchone()["id"]
        stats["people"] = load_people(conn)

        for key in config.jira_projects:
            project = get(f"/rest/api/3/project/{key}")
            board_id = board_for(key)
            conn.execute("""insert into jira_projects (key, name, board_id) values (%s, %s, %s)
                on conflict (key) do update set name = excluded.name, board_id = excluded.board_id""",
                (key, project["name"], board_id))
            stats["projects"] += 1
            for s in sprints_for(board_id) if board_id else []:
                conn.execute("""insert into jira_sprints (id, project_key, name, state, goal, start_date, end_date, complete_date)
                    values (%s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (id) do update set name = excluded.name, state = excluded.state, goal = excluded.goal,
                    start_date = excluded.start_date, end_date = excluded.end_date, complete_date = excluded.complete_date""",
                    (s["id"], key, s["name"], s["state"], s.get("goal"), s.get("startDate"), s.get("endDate"), s.get("completeDate")))
                stats["sprints"] += 1

        seen, links, comments = [], [], []
        jql = "project in (" + ",".join(config.jira_projects) + ") order by key"
        for issue in search(jql, wanted):
            f = issue["fields"]
            key = issue["key"]
            seen.append(key)
            sprints = f.get(ids.get("sprint")) or []
            done = f["status"]["statusCategory"]["key"] == "done"
            conn.execute("""insert into jira_issues (key, project_key, type, summary, description, status, status_category,
                priority, owner, epic_key, sprint_id, sprint_ids, points, start_date, due_date, flagged, labels,
                created, updated, resolved, url)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (key) do update set type = excluded.type, summary = excluded.summary,
                description = excluded.description, status = excluded.status, status_category = excluded.status_category,
                priority = excluded.priority, owner = excluded.owner, epic_key = excluded.epic_key,
                sprint_id = excluded.sprint_id, sprint_ids = excluded.sprint_ids, points = excluded.points,
                start_date = excluded.start_date, due_date = excluded.due_date, flagged = excluded.flagged,
                labels = excluded.labels, created = excluded.created, updated = excluded.updated,
                resolved = excluded.resolved, url = excluded.url""",
                (key, key.split("-")[0], f["issuetype"]["name"], f["summary"], adf_text(f.get("description")).strip(),
                 f["status"]["name"], f["status"]["statusCategory"]["key"], (f.get("priority") or {}).get("name"),
                 owner_of(f), (f.get("parent") or {}).get("key"), pick_sprint(sprints, done), [s["id"] for s in sprints],
                 f.get(ids.get("points")), f.get(ids.get("start")), f.get("duedate"), bool(f.get(ids.get("flagged"))),
                 f.get("labels") or [], f.get("created"), f.get("updated"), f.get("resolutiondate"),
                 f"{config.jira_base_url}/browse/{key}"))
            for ln in f.get("issuelinks") or []:
                if ln["type"]["name"] != "Blocks":
                    continue
                # an outward link means this issue blocks the other one
                if "outwardIssue" in ln:
                    links.append((key, ln["outwardIssue"]["key"]))
                else:
                    links.append((ln["inwardIssue"]["key"], key))
            for c in (f.get("comment") or {}).get("comments", []):
                author, body = parse_comment(c)
                comments.append((c["id"], key, author, body, c["created"]))

        # replace links and comments for the synced issues, drop issues that no longer exist
        conn.execute("delete from jira_links where blocker_key = any(%s) or blocked_key = any(%s)", (seen, seen))
        for blocker, blocked in set(links):
            conn.execute("insert into jira_links values (%s, %s) on conflict do nothing", (blocker, blocked))
        conn.execute("delete from jira_comments where issue_key = any(%s)", (seen,))
        for row in comments:
            conn.execute("insert into jira_comments values (%s, %s, %s, %s, %s)", row)
        conn.execute("delete from jira_issues where project_key = any(%s) and not (key = any(%s))", (config.jira_projects, seen))

        stats.update({"issues" : len(seen), "links" : len(set(links)), "comments" : len(comments)})
        conn.execute("update sync_runs set finished = now(), stats = %s where id = %s", (Jsonb(stats), run_id))
    print("jira sync :", stats)
    return stats


if __name__ == "__main__":
    sync()
