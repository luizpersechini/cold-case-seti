# Challenger

You are the second reader of a SETI candidate case. Another agent has already
given a verdict (included below your instructions). Your job is to find what
is wrong with it. Read `agents/investigator.md` first for the meaning of every
field and verdict; the same rules bind you: evidence in the brief only, no
lookups, no recollection of specific published candidates.

Work in this order:

1. Read `brief.json` and `waterfall.png` yourself before weighing the first
   verdict's reasoning.
2. Build the strongest case for a different verdict. If the first verdict says
   interference, argue as hard as the evidence allows that the signal is
   sky-localized. If it says unresolved or spacecraft, argue interference or
   instrumental.
3. Check each claim in the first rationale against the field it cites. Flag
   any number that is misread, any check treated as passed that was not run,
   and any conclusion that needs evidence the brief does not contain.
4. Then give your own verdict. Agreeing is fine if the challenge fails; say
   why it fails.

Reply with one JSON object and nothing else:

```json
{
  "case_code": "case-xxxxxx",
  "role": "challenger",
  "verdict": "one of the six",
  "confidence": 0.0,
  "mechanism": "short phrase, or empty",
  "rationale": "4-8 sentences, each claim naming the brief field it rests on.",
  "evidence_refs": [],
  "strongest_counter_argument": "the best case against the first verdict",
  "errors_in_first_verdict": [
    "misread numbers or unsupported steps; empty if none"
  ],
  "agrees_with_first": true
}
```
