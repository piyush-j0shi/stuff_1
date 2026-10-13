import datetime as dt

from jeera.agent import loop

daily_prompt = """Write today's daily engineering report for leadership, using your tools for every fact.
Use these sections with *bold* titles:
*Health* - one line per project: score, status emoji (:large_green_circle: / :large_yellow_circle: / :red_circle:) and the main reason.
*Top risks and blockers* - at most 5 bullets, most serious first, with owner.
*Overdue* - every overdue open item with owner and days overdue.
*Sprint progress* - one line per project: points done of total, days left.
*From Slack in the last 2 days* - at most 4 bullets of notable decisions or escalations, with links.
Keep it under 30 lines. No intro or closing sentence."""


def daily_report(asker=None, where=None):
    out = loop.run(daily_prompt, asker=asker, where=where)
    title = f":newspaper: *Daily engineering report, {dt.date.today():%a %b %d}*"
    return title + "\n\n" + out["answer"], out
