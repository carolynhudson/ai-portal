import json, pathlib
p = pathlib.Path("tests/ui/spec.json")
s = p.read_text()
start = s.index('"name": "projects"')
i = s.rindex("{", 0, start)
depth = 0
for j in range(i, len(s)):
    if s[j] == "{":
        depth += 1
    elif s[j] == "}":
        depth -= 1
        if depth == 0:
            end = j + 1
            break
seg = s[i:end]

def arr_bounds(seg, key):
    k = seg.index(key)
    o = seg.index("[", k)
    depth = 0
    for j in range(o, len(seg)):
        if seg[j] == "[":
            depth += 1
        elif seg[j] == "]":
            depth -= 1
            if depth == 0:
                return o, j
    raise AssertionError

def insert_into(seg, key, items_text):
    o, c = arr_bounds(seg, key)
    inner = seg[o + 1:c].rstrip()
    if inner.endswith(","):
        inner = inner[:-1]
    add = (",\n" + items_text) if inner.strip() else items_text
    return seg[:o + 1] + inner + add + seg[c:]

def new_key(seg, key_text):
    k = seg.index("{")
    return seg[:k + 1] + "\n      " + key_text.rstrip(",") + "," + seg[k + 1:]

VIS = "\n".join(f'        "{x}"' + ("," if x != "#proj-question-reject" else "") for x in [
    "#proj-op", "#proj-op-name", "#proj-op-meta", "#proj-op-state", "#proj-ctx-fill",
    "#proj-ctx-text", "#proj-log", "#proj-prompt", "#proj-send", "#proj-quit",
    "#proj-question", "#proj-question-reply", "#proj-question-reject"])
seg = insert_into(seg, '"expectVisible"', VIS)

REQ = "\n".join(f'        "{x}",' for x in ["/api/projects/launch", "/api/projects/session",
                                            "/api/harness/session/prompt"])[:-1]
seg = insert_into(seg, '"expectRequests"', REQ)

INERT = "\n".join(f'        "{x}"' + ("," if x != "#proj-question-reject" else "") for x in [
    "#proj-prompt", "#proj-send", "#proj-quit", "#proj-question-reply", "#proj-question-reject"])
assert '"expectInert": []' in seg
seg = seg.replace('"expectInert": []', '"expectInert": [\n' + INERT + "\n      ]")

seg = insert_into(seg, '"requireConfirmFor"', '        "^/api/projects/quit$"')

def obj(path, body, comment=None):
    parts = []
    if comment:
        parts.append('          "_comment": [\n' + "\n".join(f'            "{c}",' for c in comment).rstrip(",") + "\n          ],")
    parts.append(f'          "path": "{path}",')
    parts.append('          "status": 200,')
    parts.append('          "body": ' + json.dumps(body, indent=10).replace("\n", "\n").strip())
    return "        {\n" + "\n".join(parts) + "\n        }"

LAUNCH = obj("^/api/projects/launch$", {"ok": True, "name": "ui-registered-app", "session": "ses_ui_launch",
                                        "recorded": True, "personality": "ui-tiny-coder", "reason": None},
             ["Queue 32: the launch's answer and the session it opened. The sandbox has no host, so the",
              "operational state can only be exercised against the host's answer supplied here: launch",
              "records the pair and hands back a session id, and the session read answers with tokens,",
              "the ceiling the serving personality's models are served at, the transcript, and the host's",
              "busy flag. Every context assertion below is read against these numbers, so a page that",
              "invented the ceiling from the host's model list, or drew a bar with no denominator, fails",
              "here. The ceiling is the portal's fact, never the host's."])
VIEW = obj("^/api/projects/session$", {"ok": True, "found": True, "project": "ui-registered-app",
                                       "session": "ses_ui_launch", "personality": "ui-tiny-coder",
                                       "model": "tiny/ui-tiny-coder:q8", "agent": "build", "busy": False,
                                       "context_used": 600, "context_limit": 4096,
                                       "transcript": [{"role": "user", "text": "add a --dry-run flag"},
                                                      {"role": "assistant", "text": "added --dry-run; tests pass"}],
                                       "reason": None})
PROMPT = obj("^/api/harness/session/prompt$", {"ok": True, "accepted": True, "message": "msg_ui_1",
                                               "reason": None, "target": "ui-registered-app"})
QUIT = obj("^/api/projects/quit$", {"ok": True, "project": "ui-registered-app", "session": "ses_ui_launch",
                                    "detail": None})
o, c = arr_bounds(seg, '"fixtures"')
seg = seg[:c] + ",\n        " + ",\n        ".join([LAUNCH, VIEW, PROMPT, QUIT]) + seg[c:]

def case(note, choose_text, click, fill=None, enabled=None):
    parts = ['          "_comment": [\n' + "\n".join(f'            "{c}",' for c in note).rstrip(",") + "\n          ],",
             '          "select": "#proj-personality",',
             '          "choose": {\n            "text": "' + choose_text + '"\n          },']
    if fill:
        parts.append('          "then_fill": [\n            {\n              "id": "' + fill[0] +
                     '",\n              "value": "' + fill[1] + '"\n            }\n          ],')
    parts.append('          "then_click": [\n' + "\n".join(f'            "{c}"' for c in click) + "\n          ]")
    if enabled:
        parts[-1] += ",\n          \"expectEnabled\": [\n" + "\n".join(f'            "{e}",' for e in enabled).rstrip(",") + "\n          ]"
    return "        {\n" + "\n".join(parts) + "\n        }"

CASES = ",\n".join([
    case(["The handoff (queue 32): Launch is not a sentence on the list page, it is the page's switch",
          "into the session it opened. The row's button, the personality named by the select, and the",
          "operational view becoming the page are asserted together, because the defect this queue is",
          "about is precisely a launch that reports success and leaves the operator on the list holding",
          "a session id."],
         "ui-tiny-coder", ["#proj-rows button[data-launch]"],
         enabled=["#proj-send", "#proj-quit"]),
    case(["The send box is the page's own verb, aimed at the session the page is showing: it names the",
          "project, not a session id the operator copied. Text typed into it, then Send, must reach the",
          "agent — read below as the host's answer and the transcript that grows."],
         "ui-tiny-coder", ["#proj-send"], fill=("proj-prompt", "run the tests and report")),
    case(["Quit leaves the operational state and returns to the list, with the session id in the",
          "sentence — the session stays open on the host, so the page to go to next is the harness",
          "page. Quit is confirm-gated, so the sweep's dialog-refused pass must leave the view up and",
          "the accepted pass must land the exit."],
         "ui-tiny-coder", ["#proj-quit"]),
])
seg = new_key(seg, '"selectCases": [\n' + CASES + "\n      ],")

def txt(comment, selector, field, value, minc=1, extra=""):
    parts = ['          "_comment": [\n' + "\n".join(f'            "{c}",' for c in comment).rstrip(",") + "\n          ],",
             f'          "selector": "{selector}",',
             f'          "{field}": "{value}",']
    parts.append('          "after_select": true,')
    parts.append(f'          "minCount": {minc}')
    return "        {\n" + "\n".join(parts) + "\n        }"

TEXTS = ",\n".join([
    txt(["The operational view names the project whose session it shows and the serving pair, off the",
         "session read. A view that switched the markup and lost the facts is the message-only launch",
         "again, in a nicer box."], "#proj-op-meta", "contains", "served by ui-tiny-coder"),
    txt(["tokens over the ceiling the serving personality's models are served at — the portal's own",
         "fact, 600 of 4096 printed as 600 of 4k. A meter without a denominator is refused by the",
         "core, so these numbers are the rule, not decoration."], "#proj-ctx-text", "matches",
        "600 of 4k tokens"),
    txt(["the transcript on the surface the operator watches while the agent works: what was typed and",
         "what the agent said, in the host's order."], "#proj-log .proj-bubble", "matches",
        "add a --dry-run flag|added --dry-run", 2),
    txt(["the send box is not decorative: the text reaches the session and the answer is the host's own",
         "words — the message id it created. A send box that goes quiet is this queue's named failure,",
         "so it is asserted as visible text."], "#proj-op-out", "matches",
        r"sent\\. the host created message msg_ui_1"),
])
o, c = arr_bounds(seg, '"expectText"')
inner = seg[o + 1:c].rstrip()
assert inner.endswith("}"), inner[-40:]
seg = seg[:o + 1] + inner + ",\n" + TEXTS + seg[c:]

out = s[:i] + seg + s[end:]
json.loads(out)
p.write_text(out)
print("spec extended")
