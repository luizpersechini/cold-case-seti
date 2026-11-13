"""Run the two blinded readers (investigator, then challenger) on the Claude
API, one case at a time, under a token cap. Each reader gets the instructions
as system prompt and the brief plus plot as the message; no tools.

The API key is read from .env in the project root (ANTHROPIC_API_KEY=...) and
used only inside this process. Every call is logged to cases/spend.jsonl.

usage: python -m coldcase.readers [--dry-run] [--cap-tokens N] [--pilot | CODE ...]
"""

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone

from . import ledger
from .remote import ROOT

MODEL = os.environ.get("COLDCASE_MODEL", "claude-sonnet-5-5")
BRIEFS, VERDICTS = ROOT / "cases" / "briefs", ROOT / "cases" / "verdicts"
SPEND = ROOT / "cases" / "spend.jsonl"
DEFAULT_CAP = 2_000_000
INLINE = (
    "\n\nIn this run the files are attached to the message instead of sitting in a "
    "folder: the plot is the image and brief.json follows it as text."
)


def api_key():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def spent():
    if not SPEND.exists():
        return 0
    return sum(
        json.loads(x)["total_tokens"]
        for x in SPEND.read_text().splitlines()
        if x.strip()
    )


def prompts():
    inv = (ROOT / "agents" / "investigator.md").read_text()
    cha = (ROOT / "agents" / "challenger.md").read_text()
    return {
        "investigator": inv + INLINE,
        "challenger": cha + INLINE + "\n\n---\n\nThe file agents/investigator.md "
        "referred to above is reproduced here:\n\n" + inv,
    }


def message(code, role):
    folder = BRIEFS / code
    img = base64.standard_b64encode((folder / "waterfall.png").read_bytes()).decode()
    text = "brief.json:\n" + (folder / "brief.json").read_text()
    if role == "challenger":
        first = json.loads((VERDICTS / f"{code}_investigator.json").read_text())
        first = {k: v for k, v in first.items() if k not in ("usage", "agent")}
        text += "\n\nThe first verdict you are challenging:\n" + json.dumps(
            first, indent=1
        )
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": img,
                    },
                },
                {"type": "text", "text": text},
            ],
        }
    ]


def parse(text):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in reply")
    return json.loads(text[a : b + 1])


def read_case(client, code, role, system, source="manual"):
    msgs = message(code, role)
    for attempt in (1, 2):
        # Thinking is on by default for this model and is billed as output; it
        # used about 5k tokens per reply in a test and truncated the answer.
        # Readers write their reasoning in the reply, so it is off unless asked.
        # (This model rejects type "disabled" and asks for "between_tools".)
        extra = {} if os.environ.get("COLDCASE_THINKING") else {"thinking": {"type": "between_tools"}}
        resp = client.messages.create(
            model=MODEL, max_tokens=3000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=msgs, **extra)
        u = resp.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        usage["total_tokens"] = sum(usage.values())
        with open(SPEND, "a") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "code": code,
                        "role": role,
                        "model": MODEL,
                        "source": source,
                        **usage,
                    }
                )
                + "\n"
            )
        text = "".join(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("reply cut off at the output limit")
        try:
            v = parse(text)
            break
        except ValueError:
            if attempt == 2:
                raise
            msgs = msgs + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "Reply again with only the JSON object."},
            ]
    v.update(case_code=code, role=role, agent=MODEL, usage=usage)
    VERDICTS.mkdir(parents=True, exist_ok=True)
    (VERDICTS / f"{code}_{role}.json").write_text(json.dumps(v, indent=1))
    return v


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    cap = DEFAULT_CAP
    if "--cap-tokens" in args:
        cap = int(args[args.index("--cap-tokens") + 1])
    key = json.loads((BRIEFS / "KEY.json").read_text())
    if "--pilot" in args:
        codes = sorted(c for c, p in key.items() if "/pilot/" in p)
    else:
        codes = [a for a in args if a.startswith("case-")]
    codes = [c for c in codes if (BRIEFS / c / "brief.json").exists()]
    system = prompts()
    if dry:
        chars = [
            len(message(c, "investigator")[0]["content"][1]["text"]) for c in codes
        ]
        est = sum(len(system[r]) for r in system) / 3.5 + 2 * (
            sum(chars) / max(1, len(chars)) / 3.5 + 1800 + 900
        )
        print(
            f"model={MODEL} cases={len(codes)} key_present={api_key() is not None} "
            f"spent_so_far={spent()} cap={cap}"
        )
        print(
            f"rough estimate: {est / 1000:.0f}k tokens per case for both readers before "
            f"caching, {est * len(codes) / 1e6:.2f}M for this run"
        )
        return
    k = api_key()
    if not k:
        sys.exit("no ANTHROPIC_API_KEY in .env; add it and run again")
    import anthropic

    client = anthropic.Anthropic(api_key=k)
    led = ledger.connect()
    for code in codes:
        for role in ("investigator", "challenger"):
            out = VERDICTS / f"{code}_{role}.json"
            if out.exists():
                continue
            if spent() + 25_000 > cap:
                sys.exit(f"token cap reached ({spent()} of {cap}); stopping")
            t0 = time.time()
            try:
                v = read_case(client, code, role, system[role])
            except Exception as e:  # logged, case left for a later run
                print(f"{code} {role} FAILED {type(e).__name__}: {e}", flush=True)
                break
            with open(key[code]) as fh:
                v["case_id"] = json.load(fh)["id"]
            try:
                ledger.add_verdict(led, v)
            except Exception as e:
                print(f"{code} {role} verdict not recorded: {e}", flush=True)
            print(
                f"{code} {role:12} {v.get('verdict'):16} conf={v.get('confidence')} "
                f"tokens={v['usage']['total_tokens']} {time.time() - t0:.0f}s",
                flush=True,
            )
    print(f"total tokens spent so far: {spent()}")


if __name__ == "__main__":
    main()
