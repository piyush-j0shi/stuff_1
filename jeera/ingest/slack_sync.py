import datetime as dt
import re

from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from jeera import config, db

client = WebClient(config.slack_bot_token)
client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=8))

keep_subtypes = (None, "bot_message", "thread_broadcast", "file_share")


def paged(method, key, **kw):
    cursor = None
    while True:
        r = method(cursor=cursor, limit=200, **kw)
        yield from r[key]
        cursor = r.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return


def clean(text, user_names):
    # slack markup to plain text: <@U123> to @name, <url|label> to label
    text = re.sub(r"<@(\w+)>", lambda m: "@" + user_names.get(m.group(1), m.group(1)), text)
    text = re.sub(r"<[^>|]+\|([^>]+)>", r"\1", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"\*@(\w+)\*", r"@\1", text)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def to_row(m, channel, ctx):
    meta = (m.get("metadata") or {})
    seed = meta.get("event_payload") if meta.get("event_type") == "seed_message" else None
    if seed:
        author = seed["author"]
        posted_at = dt.datetime.fromisoformat(seed["original_ts"])
    else:
        author = ctx["user_ids"].get(m.get("user")) or m.get("username") or m.get("user")
        posted_at = dt.datetime.fromtimestamp(float(m["ts"]), dt.timezone.utc)
    text = clean(m.get("text", ""), ctx["user_names"])
    keys = sorted(set(re.findall(ctx["key_pattern"], text)))
    mentions = sorted(set(p for p in re.findall(r"@(\w+)", text) if p in ctx["people"]))
    url = f"{ctx['base_url']}archives/{channel}/p{m['ts'].replace('.', '')}"
    if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
        url += f"?thread_ts={m['thread_ts']}&cid={channel}"
    return (channel, m["ts"], m.get("thread_ts"), author, text, posted_at, keys, mentions,
            m.get("reply_count", 0), m.get("latest_reply"), url)


def skip(m, ctx):
    # skip system messages and the bot's own answers, but keep seeded persona posts
    if m.get("subtype") not in keep_subtypes:
        return True
    seeded = (m.get("metadata") or {}).get("event_type") == "seed_message"
    return m.get("bot_id") == ctx["bot_id"] and not seeded


def save(conn, row):
    conn.execute("""insert into slack_messages (channel_id, ts, thread_ts, author, text, posted_at, jira_keys, mentions,
        reply_count, latest_reply, url) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (channel_id, ts) do update set thread_ts = excluded.thread_ts, author = excluded.author,
        text = excluded.text, posted_at = excluded.posted_at, jira_keys = excluded.jira_keys, mentions = excluded.mentions,
        reply_count = excluded.reply_count, latest_reply = excluded.latest_reply, url = excluded.url""", row)


def context(conn):
    auth = client.auth_test()
    people = {r["id"] : r for r in conn.execute("select * from people")}
    by_slack = {p["slack_user_id"] : p["id"] for p in people.values() if p["slack_user_id"]}
    user_names = {}
    for u in paged(client.users_list, "members"):
        user_names[u["id"]] = by_slack.get(u["id"]) or u.get("name")
    return {"bot_id" : auth["bot_id"], "base_url" : auth["url"], "people" : people, "user_ids" : by_slack,
            "user_names" : user_names, "key_pattern" : r"\b(?:" + "|".join(config.jira_projects) + r")-\d+\b"}


def sync():
    stats = {"channels" : 0, "messages" : 0, "threads_fetched" : 0}
    with db.connect() as conn:
        ctx = context(conn)
        channels = [c for c in paged(client.conversations_list, "channels", types="public_channel,private_channel",
                                     exclude_archived=True) if c.get("is_member")]
        for c in channels:
            kind = "group" if c["is_private"] else "channel"
            conn.execute("""insert into slack_conversations (id, name, kind, purpose) values (%s, %s, %s, %s)
                on conflict (id) do update set name = excluded.name, kind = excluded.kind, purpose = excluded.purpose""",
                (c["id"], c["name"], kind, c.get("purpose", {}).get("value")))
            known = {r["ts"] : r["latest_reply"] for r in conn.execute(
                "select ts, latest_reply from slack_messages where channel_id = %s and ts = thread_ts", (c["id"],))}
            seen = []
            for m in paged(client.conversations_history, "messages", channel=c["id"], include_all_metadata=True):
                if skip(m, ctx):
                    continue
                save(conn, to_row(m, c["id"], ctx))
                seen.append(m["ts"])
                # only refetch threads that got new replies since the last sync
                if m.get("reply_count") and known.get(m["ts"]) != m.get("latest_reply"):
                    for r in paged(client.conversations_replies, "messages", channel=c["id"], ts=m["ts"], include_all_metadata=True):
                        if r["ts"] != m["ts"] and not skip(r, ctx):
                            save(conn, to_row(r, c["id"], ctx))
                            seen.append(r["ts"])
                    stats["threads_fetched"] += 1
                else:
                    seen += [r["ts"] for r in conn.execute(
                        "select ts from slack_messages where channel_id = %s and thread_ts = %s and ts <> thread_ts", (c["id"], m["ts"]))]
            # forget messages that were deleted in slack
            conn.execute("delete from slack_messages where channel_id = %s and not (ts = any(%s))", (c["id"], seen))
            conn.commit()
            stats["channels"] += 1
            stats["messages"] += len(seen)
            print(f"#{c['name']} : {len(seen)} messages", flush=True)
        conn.execute("delete from slack_conversations where not (id = any(%s))", ([c["id"] for c in channels],))
        conn.execute("delete from slack_messages where not (channel_id = any(%s))", ([c["id"] for c in channels],))
    print("slack sync :", stats)
    return stats


if __name__ == "__main__":
    sync()
