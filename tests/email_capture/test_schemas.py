import json,unittest
from pathlib import Path
class SchemaTests(unittest.TestCase):
 def test_draft_and_closed(self):
  files=list(Path("schemas/email-capture").glob("*.json")); self.assertEqual(len(files),8)
  for f in files:
   d=json.loads(f.read_text()); self.assertEqual(d["$schema"],"https://json-schema.org/draft/2020-12/schema"); self.assertFalse(d["additionalProperties"]); self.assertIn("required",d)
 def test_major_version_pattern(self):
  for f in Path("schemas/email-capture").glob("*.json"):
   d=json.loads(f.read_text()); self.assertEqual(d["properties"]["schema_version"]["pattern"],"^1\\.")
if __name__=="__main__": unittest.main()
