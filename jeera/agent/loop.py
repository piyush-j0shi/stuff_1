import dataclasses
import logging
import re
import sys
import time

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolErrorMiddleware, dynamic_prompt, wrap_tool_call
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage
from psycopg.types.json import Jsonb

from jeera import db, llm
from jeera.agent import prompts, tools

log = logging.getLogger("jeera")
# ten rounds of tool calls plus the final answer
max_model_calls = 11


@dataclasses.dataclass
class Context:
    asker: str | None = None
    where: dict | None = None


@dynamic_prompt
def system_prompt(request):
    return prompts.system(request.runtime.context.asker, request.runtime.context.where)


@wrap_tool_call
def timed(request, handler):
    started = time.time()
    result = handler(request)
    log.info("tool %s %s : %s chars in %.1fs", request.tool_call["name"], request.tool_call["args"], len(str(getattr(result, "content", ""))), time.time() - started)
    return result


agent = create_agent(llm.model, tools.tools, name="jeera", context_schema=Context, middleware=[
    ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="error"),
    system_prompt,
    timed,
    # failed tools go back to the model as an error message so it can retry or explain
    ToolErrorMiddleware(on_error=lambda e, request: f"error : {type(e).__name__}: {e}"),
])


def polish(text):
    # link bare jira keys and fix markdown that slack does not render
    with db.connect() as conn:
        urls = {r["key"] : r["url"] for r in conn.execute("select key, url from jira_issues")}
    text = re.sub(r"<url\|([^>]+)>", r"\1", text)
    text = re.sub(r"\[(<[^>]+>)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)
    text = re.sub(r"^(\s*)[-*] ", r"\1• ", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^#+\s*(.+)$", r"*\1*", text, flags=re.M)
    return re.sub(r"(?<![|/\w-])([A-Z]+-\d+)\b(?![^<]*>)", lambda m: f"<{urls[m.group(1)]}|{m.group(1)}>" if m.group(1) in urls else m.group(1), text)


def tools_used(messages):
    args = {c["id"] : c["args"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls}
    return [{"tool" : m.name, "args" : args.get(m.tool_call_id), "chars" : len(m.content)} for m in messages if isinstance(m, ToolMessage)]


def run(question, history=None, asker=None, where=None):
    started = time.time()
    usage = UsageMetadataCallbackHandler()
    used, answer, error = [], None, None
    try:
        out = agent.invoke({"messages" : (history or []) + [{"role" : "user", "content" : question}]},
                           {"callbacks" : [usage]}, context=Context(asker, where))
        used = tools_used(out["messages"])
        answer = polish(out["messages"][-1].text)
    except ModelCallLimitExceededError:
        error = f"no answer after {max_model_calls} model calls"
        answer = "That needed more steps than I am allowed. Please try a narrower question."
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        answer = "Sorry, something went wrong while answering that. Please try again."
    seconds = round(time.time() - started, 1)
    tokens_in = sum(u.get("input_tokens", 0) for u in usage.usage_metadata.values())
    tokens_out = sum(u.get("output_tokens", 0) for u in usage.usage_metadata.values())
    with db.connect() as conn:
        conn.execute("""insert into agent_runs (asker, channel, question, answer, tools, seconds, tokens_in, tokens_out, error)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (asker, (where or {}).get("channel_id", "terminal"), question, answer, Jsonb(used), seconds, tokens_in, tokens_out, error))
    return {"answer" : answer, "tools" : used, "seconds" : seconds, "tokens_in" : tokens_in, "tokens_out" : tokens_out, "error" : error}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    out = run(" ".join(sys.argv[1:]), asker="piyush")
    print()
    print(out["answer"])
    print()
    print("seconds :", out["seconds"], "| tokens :", out["tokens_in"], "/", out["tokens_out"], "| error :", out["error"])
