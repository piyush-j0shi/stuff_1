import re
from typing import Literal

import requests
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from jeera import config, db, live
from jeera.agent.tools import as_json
from jeera.ingest import jira_sync

# every write waits for the asker to approve it in slack, see loop.py
link_types = {"blocks" : "Blocks", "relates to" : "Relates", "duplicates" : "Duplicate"}


def adf(text):
    # plain text to jira's rich text, one paragraph per block of lines
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return {"type" : "doc", "version" : 1, "content" : [{"type" : "paragraph", "content" : [{"type" : "text", "text" : p}]} for p in paras]}


def known_person(person):
    with db.connect() as conn:
        return conn.execute("select 1 from people where id = %s", (person,)).fetchone() is not None


def url(key):
    return f"{config.jira_base_url}/browse/{key}"


def jira(method, path, body=None):
    try:
        return jira_sync.call(method, path, body)
    except requests.HTTPError as e:
        raise RuntimeError(live.jira_error(e)["error"]) from e


@tool(parse_docstring=True)
@as_json
def create_issue(project: str, summary: str, type: Literal["Task", "Bug", "Story", "Feature", "Epic"] = "Task",
                 description: str | None = None, owner: str | None = None,
                 priority: Literal["Highest", "High", "Medium", "Low", "Lowest"] | None = None,
                 due_date: str | None = None, points: float | None = None, epic: str | None = None, active_sprint: bool = False):
    """Create a new Jira issue. The person is asked to approve it before it is created.

    Args:
        project: Project key: PAY, MOB or DATA.
        summary: Short title of the issue.
        type: Issue type, default Task.
        description: Longer description in plain text.
        owner: Person id in lowercase, e.g. john.
        priority: Priority name.
        due_date: Date as YYYY-MM-DD.
        points: Story points.
        epic: Key of the parent epic, e.g. PAY-1.
        active_sprint: Put the issue in the project's active sprint.
    """
    if owner and not known_person(owner):
        return {"error" : f"unknown person {owner}"}
    ids = live.jira_ids()
    fields = {"project" : {"key" : project.upper()}, "summary" : summary, "issuetype" : {"name" : type},
              "labels" : ["created-by-jeera"] + ([f"owner-{owner}"] if owner else [])}
    if description:
        fields["description"] = adf(description)
    if priority:
        fields["priority"] = {"name" : priority}
    if due_date:
        fields["duedate"] = due_date
    if points is not None and ids.get("points"):
        fields[ids["points"]] = points
    if epic:
        fields["parent"] = {"key" : epic.upper()}
    key = jira("POST", "/rest/api/3/issue", {"fields" : fields})["key"]
    out = {"created" : key, "url" : url(key)}
    if active_sprint:
        with db.connect() as conn:
            sprint = conn.execute("select id, name from jira_sprints where project_key = %s and state = 'active'", (project.upper(),)).fetchone()
        if sprint:
            jira("POST", f"/rest/agile/1.0/sprint/{sprint['id']}/issue", {"issues" : [key]})
            out["sprint"] = sprint["name"]
    return out


@tool(parse_docstring=True)
@as_json
def add_comment(key: str, text: str, runtime: ToolRuntime):
    """Add a comment to a Jira issue. The person is asked to approve it before it is posted.

    Args:
        key: Issue key like PAY-7.
        text: The comment in plain text.
    """
    # the token is one account, so the comment says who asked for it
    who = runtime.context.asker or "jeera"
    jira("POST", f"/rest/api/3/issue/{key.upper()}/comment", {"body" : adf(f"[{who}] {text}")})
    return {"commented" : key.upper(), "url" : url(key.upper())}


@tool(parse_docstring=True)
@as_json
def update_issue(key: str, summary: str | None = None, description: str | None = None, owner: str | None = None,
                 priority: Literal["Highest", "High", "Medium", "Low", "Lowest"] | None = None, due_date: str | None = None,
                 points: float | None = None, flagged: bool | None = None, epic: str | None = None):
    """Change fields of a Jira issue: summary, description, owner, priority, due date, points, flag or epic.
    Only pass the fields that should change. The person is asked to approve it first. For status use move_issue.

    Args:
        key: Issue key like PAY-7.
        summary: New title.
        description: New description in plain text, replaces the old one.
        owner: New owner, person id in lowercase, e.g. john.
        priority: New priority name.
        due_date: New due date as YYYY-MM-DD.
        points: New story points.
        flagged: True to flag as an impediment, False to clear the flag.
        epic: Key of the new parent epic.
    """
    key = key.upper()
    ids = live.jira_ids()
    fields = {}
    if summary:
        fields["summary"] = summary
    if description:
        fields["description"] = adf(description)
    if priority:
        fields["priority"] = {"name" : priority}
    if due_date:
        fields["duedate"] = due_date
    if points is not None and ids.get("points"):
        fields[ids["points"]] = points
    if flagged is not None and ids.get("flagged"):
        fields[ids["flagged"]] = [{"value" : "Impediment"}] if flagged else None
    if epic:
        fields["parent"] = {"key" : epic.upper()}
    if owner:
        # owners are owner-* labels in this jira
        if not known_person(owner):
            return {"error" : f"unknown person {owner}"}
        labels = jira("GET", f"/rest/api/3/issue/{key}?fields=labels")["fields"]["labels"]
        fields["labels"] = [l for l in labels if not l.startswith("owner-")] + [f"owner-{owner}"]
    if not fields:
        return {"error" : "nothing to change"}
    jira("PUT", f"/rest/api/3/issue/{key}", {"fields" : fields})
    return {"updated" : key, "fields" : sorted(fields), "url" : url(key)}


@tool(parse_docstring=True)
@as_json
def move_issue(key: str, status: str):
    """Move a Jira issue to another status, e.g. In Progress, In Review or Done. The person is asked to approve it first.

    Args:
        key: Issue key like PAY-7.
        status: Target status name.
    """
    key = key.upper()
    moves = jira("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]
    hit = next((t for t in moves if status.lower() in (t["name"].lower(), t["to"]["name"].lower())), None)
    if not hit:
        return {"error" : f"cannot move {key} to {status}", "possible" : [t["to"]["name"] for t in moves]}
    jira("POST", f"/rest/api/3/issue/{key}/transitions", {"transition" : {"id" : hit["id"]}})
    return {"moved" : key, "status" : hit["to"]["name"], "url" : url(key)}


@tool(parse_docstring=True)
@as_json
def link_issues(from_key: str, kind: Literal["blocks", "relates to", "duplicates"], to_key: str):
    """Link two Jira issues, read as "from_key kind to_key", e.g. PAY-3 blocks PAY-8. The person is asked to approve it first.

    Args:
        from_key: First issue key.
        kind: How they relate.
        to_key: Second issue key.
    """
    # jira's api puts the issue that "blocks" on the inward side
    jira("POST", "/rest/api/3/issueLink", {"type" : {"name" : link_types[kind]}, "inwardIssue" : {"key" : from_key.upper()},
                                           "outwardIssue" : {"key" : to_key.upper()}})
    return {"linked" : f"{from_key.upper()} {kind} {to_key.upper()}"}


def describe(name, a):
    # one line per change for the approval card
    if name == "create_issue":
        extra = [f"owner {a['owner']}" if a.get("owner") else "", f"priority {a['priority']}" if a.get("priority") else "",
                 f"due {a['due_date']}" if a.get("due_date") else "", f"{a['points']:g} points" if a.get("points") is not None else "",
                 f"epic {a['epic']}" if a.get("epic") else "", "in the active sprint" if a.get("active_sprint") else ""]
        text = f"Create a *{a.get('type', 'Task')}* in *{a['project'].upper()}*: \"{a['summary']}\""
        text += "".join(f", {e}" for e in extra if e)
        if a.get("description"):
            text += f"\n> {a['description'][:300]}"
        return text
    if name == "add_comment":
        return f"Comment on *{a['key'].upper()}*:\n> {a['text'][:500]}"
    if name == "update_issue":
        shown = {"flagged" : lambda v: "flag it" if v else "clear the flag", "description" : lambda v: "new description"}
        changes = [shown[k](v) if k in shown else f"{k.replace('_', ' ')} to {v}" for k, v in a.items() if k != "key" and v is not None]
        return f"Update *{a['key'].upper()}*: " + ", ".join(changes)
    if name == "move_issue":
        return f"Move *{a['key'].upper()}* to *{a['status']}*"
    if name == "link_issues":
        return f"Link *{a['from_key'].upper()}* {a['kind']} *{a['to_key'].upper()}*"
    return f"{name} {a}"


tools = [create_issue, add_comment, update_issue, move_issue, link_issues]
