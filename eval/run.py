import json
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jeera import db
from jeera.agent import loop

questions = json.loads((pathlib.Path(__file__).parent / "questions.json").read_text())


def plain(text):
    # drop link urls so checks only see the visible text
    return re.sub(r"<[^|>]+\|([^>]+)>", r"\1", text or "").lower()


def where_for(q):
    # questions can pretend to be asked inside a channel or a thread
    if "channel" not in q:
        return None
    with db.connect() as conn:
        found = conn.execute("select id from slack_conversations where name = %s", (q["channel"],)).fetchone()
    return {"channel_id" : found["id"], "thread_ts" : q.get("thread_ts"), "is_dm" : False}


def check(q):
    out = loop.run(q["question"], asker="piyush", where=where_for(q))
    text = plain(out["answer"])
    missing = [alts for alts in q["expect"] if not any(a in text for a in alts)]
    return q, out, missing


def main():
    nums = [int(a) for a in sys.argv[1:] if a.isdigit()]
    only = nums[0] if nums else None
    picked = [q for n, q in enumerate(questions, 1) if only in (None, n)]
    passed, cost_in, cost_out = 0, 0, 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(check, picked))
    for n, (q, out, missing) in enumerate(results, 1):
        ok = not missing and not out["error"]
        passed += ok
        cost_in += out["tokens_in"]
        cost_out += out["tokens_out"]
        print(f"{'PASS' if ok else 'FAIL'} [{q['feature']}] {q['question']}")
        print("  tools :", ", ".join(t["tool"] for t in out["tools"]), "| seconds :", out["seconds"])
        if missing or "-v" in sys.argv:
            print("  missing :", missing, "| error :", out["error"])
            print("  " + plain(out["answer"]).replace("\n", "\n  "))
        print()
    print(f"passed : {passed}/{len(results)} | tokens : {cost_in} in / {cost_out} out")


if __name__ == "__main__":
    main()
