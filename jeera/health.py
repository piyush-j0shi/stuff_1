import datetime as dt
import json

from psycopg.types.json import Jsonb

from jeera import db


def item(r):
    return {"key" : r["key"], "summary" : r["summary"], "status" : r["status"], "owner" : r["owner"], "due" : r["due_date"],
            "url" : r["url"]}


def compute(conn, project, today=None):
    # rule based score: start at 100 and take points off for each problem, capped per rule
    today = today or dt.date.today()
    sprint = conn.execute("select * from jira_sprints where project_key = %s and state = 'active' order by start_date desc limit 1",
                          (project,)).fetchone()
    open_items = conn.execute("""select * from jira_issues where project_key = %s and type <> 'Epic' and status_category <> 'done'
        and (sprint_id = %s or due_date is not null)""", (project, sprint["id"] if sprint else None)).fetchall()
    blocked = conn.execute("""select distinct d.* from jira_links l join jira_issues b on b.key = l.blocker_key
        join jira_issues d on d.key = l.blocked_key
        where d.project_key = %s and b.status_category <> 'done' and d.status_category <> 'done'""", (project,)).fetchall()

    overdue = [r for r in open_items if r["due_date"] and r["due_date"] < today]
    flagged = [r for r in open_items if r["flagged"]]
    urgent = [r for r in open_items if r["type"] == "Bug" and r["priority"] in ("Highest", "High")]
    rules = [
        ("overdue", overdue, 6, 20),
        ("flagged", flagged, 5, 10),
        ("blocked", blocked, 3, 15),
        ("urgent_bugs", urgent, 4, 8),
    ]
    score, reasons = 100, []
    for name, items, each, cap in rules:
        lost = min(len(items) * each, cap)
        if lost:
            score -= lost
            reasons.append({"rule" : name, "points_lost" : lost, "count" : len(items), "items" : [item(r) for r in items]})

    progress = None
    if sprint:
        issues = conn.execute("select points, status_category from jira_issues where sprint_id = %s and type <> 'Epic'",
                              (sprint["id"],)).fetchall()
        total = sum(float(r["points"] or 0) for r in issues)
        done = sum(float(r["points"] or 0) for r in issues if r["status_category"] == "done")
        start, end = sprint["start_date"].date(), sprint["end_date"].date()
        time_pct = min(max((today - start).days / max((end - start).days, 1), 0), 1)
        done_pct = done / total if total else 0
        progress = {"sprint" : sprint["name"], "start" : start, "end" : end, "time_elapsed_pct" : round(time_pct * 100),
                    "points_done_pct" : round(done_pct * 100), "points_done" : done, "points_total" : total}
        # behind schedule means less work done than time used, some lag is normal mid sprint
        gap = time_pct - done_pct
        lost = min(round((gap - 0.2) * 50), 20) if gap > 0.2 else 0
        if lost:
            score -= lost
            reasons.append({"rule" : "behind_schedule", "points_lost" : lost,
                            "detail" : f"{progress['points_done_pct']}% of points done with {progress['time_elapsed_pct']}% of sprint time used"})

    score = max(score, 0)
    status = "green" if score >= 75 else "amber" if score >= 50 else "red"
    return {"project" : project, "score" : score, "status" : status, "sprint" : progress, "reasons" : reasons}


def snapshot():
    # store today's score per project so trends can be shown later
    with db.connect() as conn:
        out = []
        for p in conn.execute("select key from jira_projects order by key").fetchall():
            h = compute(conn, p["key"])
            conn.execute("""insert into health_scores (project_key, day, score, details) values (%s, %s, %s, %s)
                on conflict (project_key, day) do update set score = excluded.score, details = excluded.details""",
                (p["key"], dt.date.today(), h["score"], Jsonb(h, dumps=lambda o: json.dumps(o, default=str))))
            out.append(h)
            print(p["key"], ":", h["score"], h["status"], [(r["rule"], r["points_lost"]) for r in h["reasons"]])
    return out


if __name__ == "__main__":
    snapshot()


def overview(project=None):
    # current score per project plus the last two weeks of history
    with db.connect() as conn:
        keys = [project] if project else [r["key"] for r in conn.execute("select key from jira_projects order by key")]
        out = []
        for k in keys:
            h = compute(conn, k)
            h["history"] = conn.execute("select day, score from health_scores where project_key = %s order by day desc limit 14", (k,)).fetchall()
            out.append(h)
    return out
