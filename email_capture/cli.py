from __future__ import annotations
import argparse,json,os,sys,time
from pathlib import Path
from .core import CaptureError,Profile,allocate,assert_messages,await_messages,backend,receipt

def read_json(value): return json.loads(Path(value[1:]).read_text() if value.startswith("@") else value)
def emit(value): print(json.dumps(value,separators=(",",":"),sort_keys=True))
def main(argv=None):
    p=argparse.ArgumentParser(prog="email-capture"); p.add_argument("--profile"); sub=p.add_subparsers(dest="command",required=True)
    a=sub.add_parser("allocate"); a.add_argument("--request",required=True)
    w=sub.add_parser("await"); w.add_argument("--allocation",required=True); w.add_argument("--timeout",type=float,default=30); w.add_argument("--count",type=int,default=1); w.add_argument("--not-before")
    s=sub.add_parser("assert"); s.add_argument("--messages",required=True); s.add_argument("--rules",required=True)
    r=sub.add_parser("release"); r.add_argument("--allocation",required=True)
    sub.add_parser("capabilities"); sub.add_parser("doctor")
    ns=p.parse_args(argv); started=time.monotonic()
    try:
        profile=Profile.load(ns.profile); b=backend(profile)
        if ns.command=="allocate": emit(allocate(read_json(ns.request),profile))
        elif ns.command=="await": emit(await_messages(b,read_json(ns.allocation),ns.timeout,ns.count,ns.not_before))
        elif ns.command=="assert": emit(assert_messages(read_json(ns.messages),read_json(ns.rules)))
        elif ns.command=="release": b.purge(read_json(ns.allocation)); emit(receipt("release","passed",started,mode=profile.mode,cleanup_state="complete"))
        elif ns.command=="capabilities": emit(b.capabilities())
        else: emit(receipt("doctor","passed" if b.health() else "failed",started,mode=profile.mode))
        return 0
    except CaptureError as e: emit(e.as_dict()); return 2
    except (OSError,ValueError,KeyError) as e: emit(CaptureError("CONFIG_INVALID",type(e).__name__).as_dict()); return 2
if __name__=="__main__": raise SystemExit(main())
