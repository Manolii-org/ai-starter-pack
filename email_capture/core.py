from __future__ import annotations
import hashlib, json, os, re, secrets, stat, time, urllib.error, urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Protocol

VERSION = "1.0"
ERRORS = {"CONFIG_INVALID","CAPABILITY_UNSUPPORTED","CAPTURE_INFRA_UNAVAILABLE","MESSAGE_TIMEOUT","MESSAGE_AMBIGUOUS","MESSAGE_REPLAYED","ASSERTION_FAILED","CLEANUP_INCOMPLETE","AUTHORIZATION_DENIED"}
SENSITIVE_KEYS = {"recipient","subject","body","html","text","url","token","headers","raw"}

class CaptureError(Exception):
    def __init__(self, code: str, safe_detail: str = "operation failed"):
        if code not in ERRORS: code = "CONFIG_INVALID"
        self.code, self.safe_detail = code, safe_detail
        super().__init__(f"{code}: {safe_detail}")
    def as_dict(self): return {"schema_version":VERSION,"code":self.code,"detail":self.safe_detail,"retryable":self.code in {"CAPTURE_INFRA_UNAVAILABLE","MESSAGE_TIMEOUT"}}

@dataclass(frozen=True)
class Profile:
    mode: str; backend: str; endpoint: str; environment: str; ttl_seconds: int = 900
    @classmethod
    def load(cls, path: str | None = None) -> "Profile":
        raw = json.loads(Path(path).read_text()) if path else {k.lower().removeprefix("email_capture_"):v for k,v in os.environ.items() if k.startswith("EMAIL_CAPTURE_")}
        version = str(raw.get("schema_version", VERSION))
        canonical = any(k in os.environ for k in ("EMAIL_CAPTURE_MODE","EMAIL_CAPTURE_BACKEND","EMAIL_CAPTURE_ENDPOINT"))
        legacy = any(k in os.environ for k in ("MAILDEV_URL","INBUCKET_URL","SMTP_HOST"))
        if version.split(".")[0] != "1" or canonical and legacy: raise CaptureError("CONFIG_INVALID","unsupported or mixed configuration")
        mode, backend = raw.get("mode","off"), raw.get("backend","memory")
        environment = raw.get("environment", os.environ.get("NODE_ENV","test"))
        if mode not in {"off","hermetic"} or backend not in {"memory","mailpit","maildev","inbucket"}: raise CaptureError("CONFIG_INVALID","unknown mode or backend")
        if mode != "off" and environment.lower() in {"prod","production","staging"}: raise CaptureError("CONFIG_INVALID","capture forbidden in deployed environment")
        return cls(mode,backend,raw.get("endpoint",""),environment,int(raw.get("ttl_seconds",900)))

class Backend(Protocol):
    def capabilities(self)->dict[str,Any]: ...
    def list(self, allocation:dict[str,Any])->list[dict[str,Any]]: ...
    def purge(self, allocation:dict[str,Any])->None: ...
    def health(self)->bool: ...

class HttpBackend:
    def __init__(self, profile:Profile): self.profile=profile
    def _json(self,path:str)->Any:
        try:
            with urllib.request.urlopen(self.profile.endpoint.rstrip("/")+path,timeout=10) as r: return json.load(r)
        except (OSError,urllib.error.URLError,json.JSONDecodeError) as e: raise CaptureError("CAPTURE_INFRA_UNAVAILABLE",type(e).__name__) from None
    def capabilities(self): return {"schema_version":VERSION,"backend":self.profile.backend,"allocation_scope":"recipient","cursor":"received_at_id","mime":True,"attachments":True,"purge_scope":"allocation"}
    def health(self):
        self.list({"recipient":"healthcheck.invalid"}); return True
    def purge(self,allocation): return None # logical release; never global-delete shared receiver
    def list(self,allocation):
        if self.profile.backend in {"mailpit","maildev"}:
            data=self._json("/api/v1/messages" if self.profile.backend=="mailpit" else "/email")
            rows=data.get("messages",data) if isinstance(data,dict) else data
            return [normalise_http(x,self.profile.backend) for x in rows if allocation["recipient"].lower() in json.dumps(x.get("To",x.get("to",x.get("envelope",{})))).lower()]
        if self.profile.backend=="inbucket":
            local=allocation["recipient"].split("@",1)[0]
            rows=self._json(f"/api/v1/mailbox/{local}")
            return [normalise_http(x,"inbucket") for x in rows]
        raise CaptureError("CAPABILITY_UNSUPPORTED","backend has no HTTP adapter")

class MemoryBackend:
    def __init__(self,profile:Profile): self.profile=profile; self.path=Path(profile.endpoint or os.environ.get("EMAIL_CAPTURE_MEMORY_FILE","/tmp/email-capture-memory.json"))
    def _read(self):
        if not self.path.exists(): return []
        try: return json.loads(self.path.read_text())
        except json.JSONDecodeError: raise CaptureError("CAPTURE_INFRA_UNAVAILABLE","invalid receiver state") from None
    def list(self,a): return [normalise_http(x,"memory") for x in self._read() if a["recipient"] in x.get("to",[])]
    def purge(self,a):
        rows=[x for x in self._read() if a["recipient"] not in x.get("to",[])]
        self.path.write_text(json.dumps(rows)); os.chmod(self.path,stat.S_IRUSR|stat.S_IWUSR)
    def capabilities(self): return {"schema_version":VERSION,"backend":"memory","allocation_scope":"recipient","cursor":"received_at_id","mime":True,"attachments":True,"purge_scope":"allocation"}
    def health(self): return True

def normalise_http(x:dict[str,Any],backend:str)->dict[str,Any]:
    received=x.get("received_at") or x.get("Created") or x.get("date") or datetime.now(timezone.utc).isoformat()
    opaque=hashlib.sha256(f'{backend}:{x.get("ID",x.get("id",x.get("key",received)))}'.encode()).hexdigest()[:24]
    headers=x.get("headers",{}) if isinstance(x.get("headers",{}),dict) else {}
    return {"schema_version":VERSION,"opaque_id":opaque,"received_at":received,"envelope":{"from":x.get("from",[]),"to":x.get("to",[])},"subject":x.get("subject",x.get("Subject","")),"headers":headers,"content":{"text_present":bool(x.get("text") or x.get("Text")),"html_present":bool(x.get("html") or x.get("HTML")),"text":x.get("text",x.get("Text","")),"html":x.get("html",x.get("HTML",""))},"attachments":[{"filename":a.get("filename",a.get("FileName","")),"content_type":a.get("content_type",a.get("ContentType","application/octet-stream")),"size":int(a.get("size",a.get("Size",0)))} for a in x.get("attachments",x.get("Attachments",[]))]}

def backend(profile:Profile)->Backend: return MemoryBackend(profile) if profile.backend=="memory" else HttpBackend(profile)
def _scope(req): return "\0".join(str(req.get(k,"")) for k in ("entity","repository","environment","run_id"))
def allocate(req:dict[str,Any],profile:Profile)->dict[str,Any]:
    for key in ("entity","repository","environment","run_id"): 
        if not req.get(key): raise CaptureError("CONFIG_INVALID",f"missing {key}")
    scope_hash=hashlib.sha256(_scope(req).encode()).hexdigest()
    registry=Path(os.environ.get("EMAIL_CAPTURE_ALLOCATION_REGISTRY", "/tmp/email-capture-allocations.json"))
    try:
        records=json.loads(registry.read_text()) if registry.exists() else {}
    except json.JSONDecodeError:
        raise CaptureError("CAPTURE_INFRA_UNAVAILABLE","invalid allocation registry") from None
    existing=records.get(scope_hash)
    if existing and float(existing.get("expires_at",0))>time.time(): return existing
    digest=secrets.token_hex(16)
    domain=req.get("domain","capture.test"); now=datetime.now(timezone.utc).isoformat()
    result={"schema_version":VERSION,"allocation_id":digest,"recipient":f"ec-{digest}@{domain}","created_at":now,"expires_at":time.time()+profile.ttl_seconds,"cursor":"0:","scope":{k:req[k] for k in ("entity","repository","environment","run_id")}}
    records={k:v for k,v in records.items() if float(v.get("expires_at",0))>time.time()}; records[scope_hash]=result
    registry.write_text(json.dumps(records)); os.chmod(registry,stat.S_IRUSR|stat.S_IWUSR)
    return result
def await_messages(b:Backend,a:dict[str,Any],timeout:float=30,count:int=1,not_before:str|None=None)->list[dict[str,Any]]:
    deadline=time.monotonic()+timeout; seen=set(); cursor=a.get("cursor","0:")
    while True:
        rows=sorted(b.list(a),key=lambda m:(m["received_at"],m["opaque_id"]))
        rows=[m for m in rows if (not not_before or m["received_at"]>=not_before)]
        ids=[m["opaque_id"] for m in rows]
        if len(ids)!=len(set(ids)): raise CaptureError("MESSAGE_REPLAYED","duplicate opaque message id")
        fresh=[m for m in rows if f'{m["received_at"]}:{m["opaque_id"]}'>cursor]
        if len(fresh)>count: raise CaptureError("MESSAGE_AMBIGUOUS","more messages than expected")
        if len(fresh)==count: return fresh
        if time.monotonic()>=deadline: raise CaptureError("MESSAGE_TIMEOUT","bounded wait expired")
        time.sleep(min(.25,max(0,deadline-time.monotonic())))
def extract(message:dict[str,Any],kind:str,name:str|None=None)->list[str]:
    content=(message["content"].get("text","")+"\n"+message["content"].get("html",""))
    if kind=="link": return sorted(set(re.findall(r'https?://[^\s<>"\']+',content)))
    if kind=="code": return sorted(set(re.findall(r'(?<!\d)\d{4,8}(?!\d)',content)))
    if kind=="header": return [str(v) for k,v in message.get("headers",{}).items() if k.lower()==str(name).lower()]
    raise CaptureError("CONFIG_INVALID","unknown extraction kind")
def assert_messages(messages:list[dict[str,Any]],rules:dict[str,Any])->dict[str,Any]:
    exact=int(rules.get("exact_count",1))
    if len(messages)!=exact: raise CaptureError("ASSERTION_FAILED","exact count mismatch")
    if rules.get("negative") and messages: raise CaptureError("ASSERTION_FAILED","bounded negative assertion failed")
    extracted=[]
    for e in rules.get("extract",[]):
        vals=extract(messages[0],e["kind"],e.get("name")) if messages else []
        if e.get("exact_count") is not None and len(vals)!=int(e["exact_count"]): raise CaptureError("ASSERTION_FAILED","extraction count mismatch")
        extracted.append({"kind":e["kind"],"count":len(vals)})
    return {"schema_version":VERSION,"result":"passed","message_count":len(messages),"assertions":len(rules.get("extract",[]))+1,"extractions":extracted}
def receipt(operation:str,result:str,started:float,**meta):
    allowed={k:v for k,v in meta.items() if k in {"mode","entity","repository","run_id","error_code","cleanup_state","assertion_count","message_count"}}
    return {"schema_version":VERSION,"operation":operation,"result":result,"duration_ms":round((time.monotonic()-started)*1000),**allowed}
