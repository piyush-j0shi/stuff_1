import datetime as dt
import logging
import re
import threading
import time

from slack_bolt import App

from jeera import config, db, health, live, reports
from jeera.agent import loop
from jeera.ingest import slack_sync

log = logging.getLogger("jeera")
# socket mode needs no signing secret
app = App(token=config.slack_bot_token, request_verification_enabled=False)
me = app.client.auth_test()
bot_user, bot_id = me["user_id"], me["bot_id"]
thinking = "_:hourglass_flowing_sand: Looking into it..._"
# set when new messages arrive, the scheduler then refreshes the search index
pending = threading.Event()
status_emoji = {"green" : ":large_green_circle:", "amber" : ":large_yellow_circle:", "red" : ":red_circle:"}


def person_for(user_id):
    # map a slack user to our people table, fall back to their slack name
    with db.connect() as conn:
        row = conn.execute("select id from people where slack_user_id = %s", (user_id,)).fetchone()
    if row:
        return row["id"]
    try:
        return app.client.users_info(user=user_id)["user"]["name"]
    except Exception:
        return user_id


def strip_mention(text):
    return re.sub(rf"<@{bot_user}>", "", text or "").strip()


def author_of(m):
    seed = (m.get("metadata") or {}).get("event_payload") or {}
    return seed.get("author") or m.get("username") or person_for(m.get("user"))


def to_history(msgs, current_ts):
    # earlier messages become the chat history, the bot's own answers as assistant turns
    out = []
    for m in msgs:
        if m["ts"] == current_ts or m.get("text") == thinking:
            continue
        if m.get("bot_id") == bot_id and not m.get("metadata"):
            role, text = "assistant", m.get("text", "")
        else:
            role, text = "user", f"{author_of(m)}: {strip_mention(m.get('text'))}"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + text
        else:
            out.append({"role" : role, "content" : text})
    while out and out[0]["role"] == "assistant":
        out.pop(0)
    return out[-12:]


def history_for(channel, thread_ts, current_ts, is_dm):
    try:
        if thread_ts:
            msgs = app.client.conversations_replies(channel=channel, ts=thread_ts, limit=40, include_all_metadata=True)["messages"]
        elif is_dm:
            # in a dm without a thread, use the last few hours of the conversation
            oldest = (dt.datetime.now() - dt.timedelta(hours=3)).timestamp()
            msgs = list(reversed(app.client.conversations_history(channel=channel, oldest=str(oldest), limit=12)["messages"]))
        else:
            return []
    except Exception as e:
        log.warning("history failed : %s", e)
        return []
    return to_history(msgs, current_ts)


def parts(text, size=3500):
    # slack rejects long messages, so split at line breaks
    out, cur = [], ""
    lines = [line[i:i + size] for line in text.split("\n") for i in range(0, max(len(line), 1), size)]
    for line in lines:
        if cur and len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        cur = (cur + "\n" + line) if cur else line
    return out + [cur] if cur else out


def send(client, channel, placeholder_ts, thread_ts, text):
    # first part replaces the placeholder, the rest follows in the same thread
    pieces = parts(text)
    client.chat_update(channel=channel, ts=placeholder_ts, text=pieces[0])
    for p in pieces[1:]:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=p, unfurl_links=False)


def answer(event, client, is_dm=False):
    started = time.time()
    channel, ts = event["channel"], event["ts"]
    question = strip_mention(event.get("text"))
    # in channels answer in a thread, in dms answer inline unless already in a thread
    thread_ts = event.get("thread_ts") or (None if is_dm else ts)
    if not question:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Ask me about sprints, blockers, workload, health or anything discussed in Slack.")
        return
    # show the loader before any other work
    placeholder = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=thinking)
    shown = time.time()
    asker = person_for(event.get("user"))
    history = history_for(channel, event.get("thread_ts"), ts, is_dm)
    log.info("question from %s in %s : %s", asker, channel, question)
    log.info("timing : slack delivery %.1fs, loader %.1fs, context %.1fs", started - float(ts), shown - started, time.time() - shown)
    where = {"channel_id" : channel, "thread_ts" : event.get("thread_ts"), "is_dm" : is_dm}
    out = loop.run(question, history=history, asker=asker, where=where)
    try:
        send(client, channel, placeholder["ts"], thread_ts, out["answer"] or "I could not find an answer.")
    except Exception as e:
        log.exception("posting answer failed")
        client.chat_update(channel=channel, ts=placeholder["ts"], text=f"Sorry, I could not post the answer ({e}).")
    if thread_ts:
        with db.connect() as conn:
            conn.execute("insert into bot_threads (channel_id, thread_ts) values (%s, %s) on conflict do nothing", (channel, thread_ts))
    log.info("answered in %ss with %s", out["seconds"], [t["tool"] for t in out["tools"]])


@app.event("app_mention")
def on_mention(event, client):
    answer(event, client)


def store(event):
    # save channel messages as they arrive so answers never wait for the next sync
    try:
        with db.connect() as conn:
            slack_sync.save(conn, slack_sync.to_row(event, event["channel"], live.slack_ctx()))
        pending.set()
    except Exception as e:
        log.warning("live store failed : %s", e)


@app.event("message")
def on_message(event, client):
    if event.get("subtype") or event.get("bot_id"):
        return
    if event.get("channel_type") in ("channel", "group"):
        store(event)
    if event.get("channel_type") == "im":
        answer(event, client, is_dm=True)
        return
    # follow ups in a thread the bot already answered, mentions are handled by on_mention
    thread_ts = event.get("thread_ts")
    if not thread_ts or f"<@{bot_user}>" in (event.get("text") or ""):
        return
    with db.connect() as conn:
        known = conn.execute("select 1 from bot_threads where channel_id = %s and thread_ts = %s", (event["channel"], thread_ts)).fetchone()
    if known:
        answer(event, client)


def health_blocks(project=None):
    data = sorted(health.overview(project), key=lambda h: h["score"])
    blocks = [{"type" : "header", "text" : {"type" : "plain_text", "text" : f"Project health, {dt.date.today():%a %b %d}"}}]
    for h in data:
        s = h["sprint"] or {}
        line = f"{status_emoji[h['status']]} *{h['project']}*  *{h['score']}*/100"
        if s:
            line += f"\n{s['sprint']}: {s['points_done_pct']}% of points done, {s['time_elapsed_pct']}% of time used, ends {s['end']:%b %d}"
        reasons = []
        for r in h["reasons"]:
            keys = ", ".join(f"<{i['url']}|{i['key']}>" for i in r.get("items", [])[:5])
            reasons.append(f"-{r['points_lost']} {r['rule'].replace('_', ' ')}" + (f" ({keys})" if keys else f" ({r.get('detail', '')})"))
        blocks.append({"type" : "section", "text" : {"type" : "mrkdwn", "text" : line}})
        if reasons:
            blocks.append({"type" : "context", "elements" : [{"type" : "mrkdwn", "text" : " · ".join(reasons)}]})
        blocks.append({"type" : "divider"})
    blocks.append({"type" : "context", "elements" : [{"type" : "mrkdwn", "text" : "Score starts at 100, points come off for overdue, flagged, blocked, urgent bugs and sprint lag. Ask me why for details."}]})
    text = ", ".join(f"{h['project']} {h['score']}" for h in data)
    return blocks, text


@app.command("/health")
def on_health(ack, command, respond):
    ack()
    project = (command.get("text") or "").strip().upper() or None
    if project and project not in config.jira_projects:
        respond(f"Unknown project {project}. Try one of {', '.join(config.jira_projects)} or leave it empty.")
        return
    blocks, text = health_blocks(project)
    respond(response_type="in_channel", blocks=blocks, text=f"Project health: {text}")


@app.command("/daily-report")
def on_daily_report(ack, command, respond):
    ack(text="_:hourglass_flowing_sand: Putting together the daily report, this takes about 30 seconds..._")
    where = {"channel_id" : command["channel_id"], "is_dm" : command.get("channel_name") == "directmessage"}
    text, _ = reports.daily_report(asker=person_for(command["user_id"]), where=where)
    for piece in parts(text):
        respond(response_type="in_channel", replace_original=False, text=piece)


def channel_id(name):
    for c in app.client.conversations_list(types="public_channel,private_channel", limit=200, exclude_archived=True)["channels"]:
        if c["name"] == name:
            return c["id"]
    return None


def post_daily_report():
    target = channel_id(config.report_channel)
    text, _ = reports.daily_report(where={"channel_id" : target})
    for piece in parts(text):
        app.client.chat_postMessage(channel=target, text=piece, unfurl_links=False)
    log.info("daily report posted to #%s", config.report_channel)
