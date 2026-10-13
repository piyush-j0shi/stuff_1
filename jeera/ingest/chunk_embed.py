import hashlib
import itertools

from jeera import db, llm

max_chars = 6000
long_message = 500
window_chars = 2000


def line(m):
    return f"[{m['posted_at']:%Y-%m-%d %H:%M}] {m['author']}: {m['text']}"


def make_chunk(ref, source, msgs, channel, title):
    people = sorted(set(m["author"] for m in msgs if m["author"]) | set(p for m in msgs for p in m["mentions"]))
    keys = sorted(set(k for m in msgs for k in m["jira_keys"]))
    body = title + "\n" + "\n".join(line(m) for m in msgs)
    return {"ref" : ref, "source" : source, "channel" : channel, "title" : title, "content" : body, "people" : people,
            "jira_keys" : keys, "posted_at" : msgs[0]["posted_at"], "url" : msgs[0]["url"]}


def split(msgs):
    # keep each piece under max_chars, cutting only between messages
    part, size = [], 0
    for m in msgs:
        if part and size + len(m["text"]) > max_chars:
            yield part
            part, size = [], 0
        part.append(m)
        size += len(m["text"]) + 40
    if part:
        yield part


def slack_chunks(conn):
    rows = conn.execute("""select m.*, c.name channel from slack_messages m join slack_conversations c on c.id = m.channel_id
        order by m.channel_id, m.posted_at, m.ts""").fetchall()
    out = []
    for channel, msgs in itertools.groupby(rows, key=lambda m: m["channel"]):
        msgs = list(msgs)
        replies = {}
        for m in msgs:
            if m["thread_ts"] and m["thread_ts"] != m["ts"]:
                replies.setdefault(m["thread_ts"], []).append(m)
        top = [m for m in msgs if not m["thread_ts"] or m["thread_ts"] == m["ts"]]
        window = []

        def flush():
            if window:
                title = f"#{channel} messages, {window[0]['posted_at']:%a %Y-%m-%d}"
                out.append(make_chunk(f"slack:{window[0]['channel_id']}:{window[0]['ts']}", "slack", list(window), channel, title))
                window.clear()

        for m in top:
            thread = replies.get(m["ts"])
            if thread or len(m["text"]) >= long_message:
                # a thread or a long post becomes its own chunk
                title = f"#{channel} thread, {m['posted_at']:%a %Y-%m-%d}"
                for n, part in enumerate(split([m] + (thread or []))):
                    ref = f"slack:{m['channel_id']}:{m['ts']}" + (f":{n}" if n else "")
                    out.append(make_chunk(ref, "slack", part, channel, title + (" (cont.)" if n else "")))
                continue
            # short standalone messages are grouped per channel per day
            if window and (m["posted_at"].date() != window[-1]["posted_at"].date() or sum(len(w["text"]) for w in window) > window_chars):
                flush()
            window.append(m)
        flush()
    return out


def jira_chunks(conn):
    issues = conn.execute("""select i.*, s.name sprint, p.name project
        from jira_issues i join jira_projects p on p.key = i.project_key left join jira_sprints s on s.id = i.sprint_id
        order by i.key""").fetchall()
    blocked_by, blocks = {}, {}
    for r in conn.execute("select * from jira_links"):
        blocked_by.setdefault(r["blocked_key"], []).append(r["blocker_key"])
        blocks.setdefault(r["blocker_key"], []).append(r["blocked_key"])
    comments = {}
    for c in conn.execute("select * from jira_comments order by created"):
        comments.setdefault(c["issue_key"], []).append(c)
    out = []
    for i in issues:
        k = i["key"]
        facts = [f"Project: {i['project']}", f"Status: {i['status']}", f"Owner: {i['owner']}", f"Sprint: {i['sprint'] or 'backlog'}",
                 f"Priority: {i['priority']}", f"Due: {i['due_date']}", f"Points: {i['points']}", f"Epic: {i['epic_key']}"]
        if i["flagged"]:
            facts.append("Flagged as impediment")
        if blocked_by.get(k):
            facts.append("Blocked by: " + ", ".join(blocked_by[k]))
        if blocks.get(k):
            facts.append("Blocks: " + ", ".join(blocks[k]))
        title = f"{k} {i['type']}: {i['summary']}"
        body = title + "\n" + " | ".join(facts) + "\n\n" + (i["description"] or "")
        cs = comments.get(k, [])
        if cs:
            body += "\n\nComments:\n" + "\n".join(f"- [{c['created']:%Y-%m-%d}] {c['author']}: {c['body']}" for c in cs)
        people = sorted(set([i["owner"]] + [c["author"] for c in cs]) - {None})
        keys = sorted(set([k] + blocked_by.get(k, []) + blocks.get(k, []) + ([i["epic_key"]] if i["epic_key"] else [])))
        out.append({"ref" : f"jira:{k}", "source" : "jira", "channel" : None, "title" : title, "content" : body[:max_chars * 2],
                    "people" : people, "jira_keys" : keys, "posted_at" : i["updated"], "url" : i["url"]})
    return out


def embed(texts):
    return llm.embedder.embed_documents(texts)


def sync():
    with db.connect() as conn:
        chunks = slack_chunks(conn) + jira_chunks(conn)
        for c in chunks:
            c["content_hash"] = hashlib.md5(c["content"].encode()).hexdigest()
            # a changed text clears the old embedding so it gets embedded again
            conn.execute("""insert into chunks (ref, source, channel, title, content, people, jira_keys, posted_at, url, content_hash)
                values (%(ref)s, %(source)s, %(channel)s, %(title)s, %(content)s, %(people)s, %(jira_keys)s, %(posted_at)s, %(url)s, %(content_hash)s)
                on conflict (ref) do update set source = excluded.source, channel = excluded.channel, title = excluded.title,
                content = excluded.content, people = excluded.people, jira_keys = excluded.jira_keys, posted_at = excluded.posted_at,
                url = excluded.url, content_hash = excluded.content_hash,
                embedding = case when chunks.content_hash = excluded.content_hash then chunks.embedding end""", c)
        conn.execute("delete from chunks where not (ref = any(%s))", ([c["ref"] for c in chunks],))
        todo = conn.execute("select id, content from chunks where embedding is null").fetchall()
        for n in range(0, len(todo), 64):
            batch = todo[n:n + 64]
            for row, vec in zip(batch, embed([r["content"] for r in batch])):
                conn.execute("update chunks set embedding = %s where id = %s", (vec, row["id"]))
            conn.commit()
        stats = {"chunks" : len(chunks), "slack" : sum(c["source"] == "slack" for c in chunks),
                 "jira" : sum(c["source"] == "jira" for c in chunks), "embedded" : len(todo)}
    print("chunks :", stats)
    return stats


if __name__ == "__main__":
    sync()
