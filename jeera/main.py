import logging

from apscheduler.schedulers.background import BackgroundScheduler
from slack_bolt.adapter.socket_mode import SocketModeHandler

from jeera import config, health
from jeera.ingest import chunk_embed, jira_sync, slack_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("jeera")


def sync_all():
    # jira first, then slack, then refresh the search index
    for step in (jira_sync.sync, slack_sync.sync, chunk_embed.sync):
        try:
            step()
        except Exception:
            log.exception("sync step failed : %s", step.__module__)


def refresh_index():
    from jeera import slack_app

    if slack_app.pending.is_set():
        slack_app.pending.clear()
        chunk_embed.sync()


def main():
    from jeera import slack_app

    scheduler = BackgroundScheduler()
    scheduler.add_job(sync_all, "interval", minutes=15, max_instances=1, coalesce=True)
    scheduler.add_job(refresh_index, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(health.snapshot, "cron", hour=0, minute=5)
    scheduler.add_job(slack_app.post_daily_report, "cron", day_of_week="mon-fri", hour=9, minute=0)
    scheduler.start()
    log.info("scheduler : sync every 15 min, daily report 09:00 to #%s", config.report_channel)

    log.info("slack : connecting as %s", slack_app.bot_user)
    SocketModeHandler(slack_app.app, config.slack_app_token).start()


if __name__ == "__main__":
    main()
