import os
import pathlib

from dotenv import load_dotenv

root = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(root / ".env")

database_url = os.environ["DATABASE_URL"]

jira_base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
jira_email = os.environ["JIRA_EMAIL"]
jira_api_token = os.environ["JIRA_API_TOKEN"]
jira_projects = [p.strip() for p in os.environ.get("JIRA_PROJECTS", "PAY,MOB,DATA").split(",") if p.strip()]

slack_bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
slack_app_token = os.environ.get("SLACK_APP_TOKEN", "")

llm_api_key = os.environ.get("LLM_API_KEY", "")
llm_model = os.environ.get("LLM_MODEL", "anthropic/claude-haiku-5.5")
embedding_api_key = os.environ.get("EMBEDDING_API_KEY") or llm_api_key
embedding_model = os.environ.get("EMBEDDING_MODEL", "openai/text-embedding-3-small")
openrouter_url = "https://openrouter.ai/api/v1"

report_channel = os.environ.get("REPORT_CHANNEL", "eng-leads")

people_file = root / "data" / "people.json"
