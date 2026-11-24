import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to exercise the browser JavaScript")
class WebRequestErrorsTest(unittest.TestCase):
    def test_version_mismatch_and_validation_messages(self):
        app_js = Path(__file__).resolve().parents[1] / "harness/api/web/app.js"
        script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function requestErrorMessage(');
const end = source.indexOf('\nasync function request(', start);
if (start < 0 || end < 0) throw new Error('request error handler not found');
const context = {};
vm.runInNewContext(source.slice(start, end), context);
const message = context.requestErrorMessage;
const oldRoute = {detail: [{loc: ['path', 'asset'], type: 'literal_error'}]};
const invalid = {detail: [{loc: ['body', 'api_key'], type: 'string_too_long',
                           input: 'sensitive-token', msg: 'sensitive-token'}]};
console.log(JSON.stringify([
  message('/ui/providers', 422, oldRoute),
  message('/ui/providers', 404, {}),
  message('/ui/capabilities', 422, oldRoute),
  message('/ui/providers', 422, {detail: [{loc: ['body', 'model'], type: 'missing'}]}),
  message('/ui/providers', 422, invalid),
  message('/v1/runs', 422, oldRoute),
]));
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(app_js)],
                                check=True, capture_output=True, text=True)
        old_422, old_404, old_capabilities, missing, secret, unrelated = json.loads(result.stdout)
        for message in (old_422, old_404, old_capabilities):
            self.assertIn("页面与服务端版本不一致", message)
            self.assertIn("先处理进行中或暂停的任务", message)
            self.assertIn("重启 Mini Harness", message)
        self.assertIn("model", missing)
        self.assertIn("缺少必填", missing)
        self.assertIn("api_key", secret)
        self.assertNotIn("sensitive-token", secret)
        self.assertNotIn("重启", unrelated)
