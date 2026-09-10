"""One-off: import tool-call traces from logs/session-<id>.md for sessions that predate the trace table."""
import re, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from munchkin.journal import Journal
from munchkin.config import LOG_DIR
j = Journal()
n = 0
for f in sorted(LOG_DIR.glob("session-*.md")):
    sid = int(f.stem.split("-")[1])
    if j.trace(sid):
        continue
    txt = f.read_text()
    blocks = re.split(r"^### ", txt, flags=re.M)[1:]
    for b in blocks:
        head, _, rest = b.partition("\n")
        m = re.match(r"(\w+) \(([\d.]+)s\)", head)
        if not m:
            continue
        name, secs = m.group(1), float(m.group(2))
        if rest.startswith("args: "):
            args, _, result = rest[6:].partition("\n\n")
        else:
            args, result = "", rest
        if "## Final" in result:
            result = result.split("## Final")[0]
        j.add_trace(sid, "tool", name, args.strip(), result.strip(), secs)
        n += 1
    s = j.session(sid)
    if s and s.get("summary"):
        j.add_trace(sid, "final", None, None, s["summary"])
print(f"backfilled {n} tool rows")
