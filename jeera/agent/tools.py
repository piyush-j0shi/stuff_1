import datetime as dt
import decimal
import functools
import json
import re
from typing import Literal

from langchain_core.tools import tool

from jeera import db, health, live
from jeera.ingest.chunk_embed import embed

open_issue = "i.type <> 'Epic' and i.status_category <> 'done'"


def as_json(f):
    # tools return plain rows, the model gets them as json
    @functools.wraps(f)
    def run(*args, **kwargs):
        return json.dumps(f(*args, **kwargs), default=lambda o: float(o) if isinstance(o, decimal.Decimal) else str(o))
    return run


def item(r):
    out = {"key" : r["key"], "summary" : r["summary"], "status" : r["status"], "owner" : r["owner"], "url" : r["url"]}
    for k in ("due_date", "points", "priority"):
        if r.get(k) is not None:
            out[k] = r[k]
    if r.get("flagged"):
        out["flagged"] = True
    return out


@tool(parse_docstring=True)
@as_json
def jira_search(jql: str, limit: int = 50):
    """Run any JQL query live against Jira and get matching issues (key, summary, type, status, owner, priority,
    due date, points, flagged, sprint, epic, url). Use for any question about which issues match some condition.
    Owners are labels: use labels = owner-john. Flagged: "Flagged" is not EMPTY. Sprints: sprint in openSprints().
    Examples:
    project = MOB AND statusCategory != Done AND labels = owner-john ORDER BY duedate
    duedate <= endOfWeek() AND statusCategory != Done
    updated >= -1d ORDER BY updated DESC

    Args:
        jql: The JQL query.
        limit: Max issues, default 50.
    """
    return live.jira_search(jql, limit)


@tool(parse_docstring=True)
@as_json
def get_issue(key: str):
    """Everything about one issue, live from Jira: description, status, owner, dates, links (blocks, blocked by,
    relates), comments, child issues, plus recent Slack messages that mention it.

    Args:
        key: Issue key like PAY-7, or a Jira link.
    """
    m = re.search(r"browse/([A-Za-z][A-Za-z0-9]+-\d+)", key) or re.search(r"\b([A-Za-z][A-Za-z0-9]*-\d+)\b", key)
    if not m:
        return {"error" : f"no issue key found in {key}"}
    k = m.group(1).upper()
    out = live.jira_issue(k)
    if "error" not in out:
        with db.connect() as conn:
            out["recent_slack_mentions"] = conn.execute("""select c.name channel, m.author, m.posted_at, left(m.text, 400) text, m.url
                from slack_messages m join slack_conversations c on c.id = m.channel_id
                where %s = any(m.jira_keys) order by m.posted_at desc limit 10""", (k,)).fetchall()
    return out


def channel_lookup(channel):
    with db.connect() as conn:
        return conn.execute("select id, name from slack_conversations where id = %(c)s or name = %(c)s", {"c" : channel.lstrip("#")}).fetchone()


@tool(parse_docstring=True)
@as_json
def read_slack(channel: str | None = None, link: str | None = None, thread_ts: str | None = None, since: str | None = None,
               until: str | None = None, person: str | None = None, limit: int = 150):
    """Read Slack messages in time order. Use for summaries, recaps, catching up, "what happened", "what did we discuss".
    - a Slack message link: reads that whole thread live
    - channel + thread_ts: reads that thread live
    - channel (name or id) + optional since/until/person: messages in that range (default last 7 days), threads kept together

    Args:
        channel: Channel name without # or channel id.
        link: Slack message link.
        thread_ts: Timestamp of the thread's first message.
        since: Date as YYYY-MM-DD.
        until: Date as YYYY-MM-DD.
        person: Person id in lowercase, e.g. john.
        limit: Max messages, default 150.
    """
    if link:
        p = live.parse_permalink(link)
        if not p:
            return {"error" : "not a slack message link"}
        root = p["thread_ts"] or p["ts"]
        return {"channel" : p["channel"], "thread" : live.slack_thread(p["channel"], root)}
    if not channel:
        return {"error" : "give a channel or a link"}
    found = channel_lookup(channel)
    channel_id = found["id"] if found else channel
    if thread_ts:
        return {"channel" : channel, "thread" : live.slack_thread(channel_id, thread_ts)}
    if not found:
        return {"channel" : channel, "messages" : live.slack_history(channel_id, since, until, limit)}
    since = since or (dt.date.today() - dt.timedelta(days=7)).isoformat()
    with db.connect() as conn:
        rows = conn.execute("""select m.posted_at, m.author, left(m.text, 600) text, m.thread_ts is not null and m.thread_ts <> m.ts is_reply, m.url
            from slack_messages m where m.channel_id = %(ch)s and m.posted_at >= %(since)s::date
            and (%(until)s::date is null or m.posted_at < %(until)s::date + 1) and (%(who)s::text is null or m.author = %(who)s)
            order by coalesce((select p.posted_at from slack_messages p where p.channel_id = m.channel_id and p.ts = m.thread_ts), m.posted_at), m.posted_at
            limit %(n)s""", {"ch" : channel_id, "since" : since, "until" : until, "who" : person, "n" : min(int(limit), 300)}).fetchall()
    return {"channel" : found["name"], "since" : since, "until" : until, "count" : len(rows), "messages" : rows}


@tool(parse_docstring=True)
@as_json
def search_discussions(query: str, channel: str | None = None, person: str | None = None, since: str | None = None,
                       until: str | None = None, source: Literal["slack", "jira"] | None = None, limit: int = 6):
    """Semantic search over all Slack threads and Jira issue text. Use to find where something was discussed, decisions,
    reasons, opinions, history. Filters are optional, only filter by channel or person when the question is about them.

    Args:
        query: What to look for, in plain words.
        channel: Channel name without #.
        person: Person id in lowercase, e.g. john.
        since: Date as YYYY-MM-DD.
        until: Date as YYYY-MM-DD.
        source: Only slack or only jira.
        limit: Max results, default 6, max 12.
    """
    vec = embed([query])[0]
    found = channel_lookup(channel) if channel else None
    args = {"v" : vec, "ch" : found["name"] if found else (channel.lstrip("#") if channel else None), "who" : person,
            "since" : since, "until" : until, "src" : source, "n" : min(int(limit or 6), 12)}
    with db.connect() as conn:
        return conn.execute("""select source, title, channel, posted_at, url, people, jira_keys, left(content, 1800) content,
            round((1 - (embedding <=> %(v)s::vector))::numeric, 3) similarity from chunks
            where (%(ch)s::text is null or channel = %(ch)s) and (%(who)s::text is null or %(who)s = any(people))
            and (%(since)s::date is null or posted_at >= %(since)s::date) and (%(until)s::date is null or posted_at < %(until)s::date + 1)
            and (%(src)s::text is null or source = %(src)s)
            order by embedding <=> %(v)s::vector limit %(n)s""", args).fetchall()


@tool(parse_docstring=True)
@as_json
def get_sprint_status(project: str | None = None):
    """Shortcut: active sprint per project with dates, days left, points done vs total, issues grouped by status and items at risk.

    Args:
        project: Project key: PAY, MOB or DATA. Omit for all projects.
    """
    today = dt.date.today()
    with db.connect() as conn:
        sprints = conn.execute("select * from jira_sprints where state = 'active' and (%(p)s::text is null or project_key = %(p)s) order by project_key",
                               {"p" : project}).fetchall()
        out = []
        for s in sprints:
            issues = conn.execute("select * from jira_issues i where sprint_id = %s and type <> 'Epic' order by key", (s["id"],)).fetchall()
            by_status = {}
            for i in issues:
                by_status.setdefault(i["status"], []).append(item(i))
            total = sum(float(i["points"] or 0) for i in issues)
            done = sum(float(i["points"] or 0) for i in issues if i["status_category"] == "done")
            at_risk = [item(i) for i in issues if i["status_category"] != "done" and (i["flagged"] or (i["due_date"] and i["due_date"] <= today + dt.timedelta(days=2)))]
            out.append({"project" : s["project_key"], "sprint" : s["name"], "goal" : s["goal"], "start" : s["start_date"].date(),
                        "end" : s["end_date"].date(), "days_left" : (s["end_date"].date() - today).days,
                        "points_total" : total, "points_done" : done, "issue_count" : len(issues),
                        "by_status" : by_status, "at_risk_due_soon_or_flagged" : at_risk})
        return out


@tool(parse_docstring=True)
@as_json
def find_blockers(project: str | None = None, person: str | None = None):
    """Shortcut: flagged impediments, overdue open items and blocking chains (which open issue blocks which).

    Args:
        project: Project key: PAY, MOB or DATA. Omit for all projects.
        person: Person id in lowercase, e.g. john.
    """
    today = dt.date.today()
    args = {"p" : project, "who" : person, "today" : today}
    scope = "(%(p)s::text is null or i.project_key = %(p)s) and (%(who)s::text is null or i.owner = %(who)s)"
    with db.connect() as conn:
        flagged = conn.execute(f"select * from jira_issues i where {open_issue} and flagged and {scope} order by key", args).fetchall()
        overdue = conn.execute(f"select * from jira_issues i where {open_issue} and due_date < %(today)s and {scope} order by due_date", args).fetchall()
        chains = conn.execute(f"""select i.*, array_agg(d.key order by d.key) blocked_keys from jira_links l
            join jira_issues i on i.key = l.blocker_key join jira_issues d on d.key = l.blocked_key
            where {open_issue} and d.status_category <> 'done'
            and (%(p)s::text is null or i.project_key = %(p)s or d.project_key = %(p)s)
            and (%(who)s::text is null or i.owner = %(who)s or d.owner = %(who)s)
            group by i.key order by i.key""", args).fetchall()
        blocked = {r["key"] : item(r) for r in conn.execute("select * from jira_issues where key = any(%s)",
                                                           ([k for c in chains for k in c["blocked_keys"]],))}
    return {"flagged_impediments" : [item(r) for r in flagged],
            "overdue_open_items" : [dict(item(r), days_overdue=(today - r["due_date"]).days) for r in overdue],
            "blocking_chains" : [{"blocker" : item(c), "blocks" : [blocked[k] for k in c["blocked_keys"]]} for c in chains]}


@tool(parse_docstring=True)
@as_json
def get_workload(person: str | None = None, project: str | None = None):
    """Shortcut: who is working on what. Per person: open items with their issues, story points, active sprint load,
    in progress, overdue, flagged, and the team average.

    Args:
        person: Person id in lowercase, e.g. john.
        project: Project key: PAY, MOB or DATA. Omit for all projects.
    """
    today = dt.date.today()
    args = {"who" : person, "p" : project, "today" : today}
    with db.connect() as conn:
        rows = conn.execute(f"""select p.id person, p.title,
            count(i.key) open_items,
            coalesce(sum(i.points), 0) open_points,
            count(i.key) filter (where s.state = 'active') in_active_sprint,
            coalesce(sum(i.points) filter (where s.state = 'active'), 0) active_sprint_points,
            count(i.key) filter (where i.status = 'In Progress') in_progress,
            count(i.key) filter (where i.due_date < %(today)s) overdue,
            count(i.key) filter (where i.flagged) flagged
            from people p left join jira_issues i on i.owner = p.id and {open_issue} and (%(p)s::text is null or i.project_key = %(p)s)
            left join jira_sprints s on s.id = i.sprint_id
            where (%(who)s::text is null or p.id = %(who)s)
            group by p.id, p.title order by active_sprint_points desc, open_items desc""", args).fetchall()
        items = conn.execute(f"""select i.*, s.state sprint_state from jira_issues i left join jira_sprints s on s.id = i.sprint_id
            where {open_issue} and (%(who)s::text is null or i.owner = %(who)s) and (%(p)s::text is null or i.project_key = %(p)s)
            order by due_date nulls last""", args).fetchall()
        avg = conn.execute(f"""select avg(pts) from (select sum(i.points) pts from jira_issues i join jira_sprints s on s.id = i.sprint_id
            where {open_issue} and s.state = 'active' and (%(p)s::text is null or i.project_key = %(p)s) group by i.owner) x""", args).fetchone()["avg"]
    for r in rows:
        r["items"] = [dict(item(i), in_active_sprint=i["sprint_state"] == "active") for i in items if i["owner"] == r["person"]]
    return {"team_avg_active_sprint_points" : round(float(avg or 0), 1), "people" : rows}


@tool(parse_docstring=True)
@as_json
def get_health(project: str | None = None):
    """Health score 0-100 per project with status (green/amber/red), the rules that took points off with the issues
    behind them, sprint progress and score history.

    Args:
        project: Project key: PAY, MOB or DATA. Omit for all projects.
    """
    return health.overview(project)


@tool(parse_docstring=True)
@as_json
def query_data(sql: str):
    """Read only SQL (one SELECT, max 100 rows) over the synced copy, for counts, totals and joins. Tables:
    jira_issues(key, project_key, type, summary, description, status, status_category[new|indeterminate|done], priority, owner,
    epic_key, sprint_id, points, start_date, due_date, flagged, created, updated, resolved, url),
    jira_sprints(id, project_key, name, state[active|closed|future], goal, start_date, end_date), jira_links(blocker_key, blocked_key),
    jira_comments(issue_key, author, body, created), people(id, name, title, slack_user_id),
    slack_messages(channel_id, ts, thread_ts, author, text, posted_at, jira_keys, mentions, url), slack_conversations(id, name, kind, purpose),
    health_scores(project_key, day, score).

    Args:
        sql: One SELECT statement.
    """
    if not re.match(r"^\s*(select|with)\b", sql, re.I) or ";" in sql.strip().rstrip(";"):
        return {"error" : "only a single select statement is allowed"}
    with db.connect() as conn:
        conn.execute("set transaction read only")
        conn.execute("set local statement_timeout = '5s'")
        try:
            rows = conn.execute(f"select * from ({sql.strip().rstrip(';')}) q limit 100").fetchall()
        except Exception as e:
            return {"error" : str(e).splitlines()[0]}
    return {"row_count" : len(rows), "rows" : rows}


tools = [jira_search, get_issue, read_slack, search_discussions, get_sprint_status, find_blockers, get_workload, get_health, query_data]
