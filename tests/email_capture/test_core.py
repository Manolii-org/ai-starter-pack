import json,os,tempfile,time,unittest
from pathlib import Path
from email_capture.core import *
class CaptureTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); self.p=Profile("hermetic","memory",self.t.name+"/mail.json","test",1); self.b=MemoryBackend(self.p)
  self.req={"schema_version":"1.0","entity":"manolii","repository":"repo","environment":"ci","run_id":"42"}; self.a=allocate(self.req,self.p)
 def tearDown(self): self.t.cleanup()
 def test_idempotent_high_entropy_allocation(self):
  self.assertEqual(self.a,allocate(self.req,self.p)); self.assertRegex(self.a["recipient"],r"ec-[0-9a-f]{32}@")
 def test_isolation_release_and_normalisation(self):
  Path(self.p.endpoint).write_text(json.dumps([{"id":"1","to":[self.a["recipient"]],"subject":"secret","text":"Code 123456 https://example.test/x"},{"id":"2","to":["other@capture.test"]}]))
  rows,cursor=await_messages(self.b,self.a,.1); self.assertEqual(self.a["cursor"],cursor); self.assertEqual(len(rows),1); self.assertEqual(extract(rows[0],"code"),["123456"]); self.assertEqual(len(extract(rows[0],"link")),1)
  self.assertEqual(assert_messages(rows,{"exact_count":1,"extract":[{"kind":"code","exact_count":1}]})["result"],"passed")
  self.b.purge(self.a); self.assertEqual(len(json.loads(Path(self.p.endpoint).read_text())),1)
 def test_timeout_ambiguity_replay(self):
  with self.assertRaisesRegex(CaptureError,"MESSAGE_TIMEOUT"): await_messages(self.b,self.a,.01)
  Path(self.p.endpoint).write_text(json.dumps([{"id":"1","to":[self.a["recipient"]]},{"id":"2","to":[self.a["recipient"]]}]))
  with self.assertRaisesRegex(CaptureError,"MESSAGE_AMBIGUOUS"): await_messages(self.b,self.a,.01)
  Path(self.p.endpoint).write_text(json.dumps([{"id":"1","to":[self.a["recipient"]]},{"id":"1","to":[self.a["recipient"]]}]))
  with self.assertRaisesRegex(CaptureError,"MESSAGE_REPLAYED"): await_messages(self.b,self.a,.01,count=2)
 def test_fail_closed_and_safe_receipt(self):
  old=dict(os.environ)
  try:
   os.environ.update(EMAIL_CAPTURE_MODE="hermetic",EMAIL_CAPTURE_BACKEND="memory",EMAIL_CAPTURE_ENVIRONMENT="production")
   with self.assertRaisesRegex(CaptureError,"CONFIG_INVALID"): Profile.load()
  finally: os.environ.clear(); os.environ.update(old)
  out=receipt("await","passed",time.monotonic(),recipient="no",subject="no",message_count=1); self.assertNotIn("recipient",out); self.assertNotIn("subject",out)
 def test_cli_writes_sensitive_messages_to_private_file(self):
  import subprocess
  Path(self.p.endpoint).write_text(json.dumps([{"id":"1","to":[self.a["recipient"]],"text":"secret 123456"}]))
  profile=Path(self.t.name)/"profile.json"; profile.write_text(json.dumps({"schema_version":"1.0","mode":"hermetic","backend":"memory","endpoint":self.p.endpoint,"environment":"test"}))
  allocation=Path(self.t.name)/"allocation.json"; allocation.write_text(json.dumps(self.a)); output=Path(self.t.name)/"messages.json"
  run=subprocess.run(["bin/email-capture","--profile",str(profile),"await","--allocation","@"+str(allocation),"--timeout",".1","--output",str(output)],capture_output=True,text=True,check=True)
  self.assertNotIn("secret",run.stdout); self.assertEqual(output.stat().st_mode & 0o777,0o600)
 def test_mode_off_rejects_backend(self):
  with self.assertRaisesRegex(CaptureError,"CAPABILITY_UNSUPPORTED"): backend(Profile("off","memory","","production"))
 def test_ttl_contract(self): self.assertLessEqual(self.a["expires_at"]-time.time(),1.01)
if __name__=="__main__": unittest.main()
