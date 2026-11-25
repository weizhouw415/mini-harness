import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from harness.storage.sqlite import RunStore
from harness.skills.jobs import SkillReviewJobs


class ReviewJobsTest(unittest.IsolatedAsyncioTestCase):
    async def test_review_updates_skill_and_stores_only_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root)
            run = store.create('complex work', None, None)
            store.transition(run['id'], {'queued'}, 'running')
            store.transition(run['id'], {'running'}, 'completed', output='done', skill_review=True)
            self.assertEqual(store.events(run['id'])[-1]['data']['status'], 'queued')
            skills = root / 'skills'
            skills.mkdir()
            existing = skills / 'existing'
            existing.mkdir()
            document = existing / 'SKILL.md'
            original = '---\nname: existing\ndescription: Existing workflow\n---\n\nOriginal instructions.\n'
            updated = '---\nname: existing\ndescription: Existing workflow\n---\n\nImproved instructions.\n'
            document.write_text(original)
            jobs = SkillReviewJobs(store, SimpleNamespace(complete=AsyncMock()), skills)
            proposal = {'status': 'proposed', 'action': 'update', 'name': 'existing',
                        'content': updated, 'base_content': original}
            with patch('harness.skills.jobs.review_skills', new=AsyncMock(return_value=proposal)):
                await jobs._process(*jobs._claim())
            self.assertEqual(document.read_text(), updated)
            with store.connect() as db:
                result = json.loads(db.execute('SELECT result FROM skill_review_jobs').fetchone()[0])
            self.assertNotIn('content', result)
            self.assertNotIn('base_content', result)
            self.assertEqual(store.events(run['id'])[-1]['data']['status'], 'completed')

    async def test_completed_review_creates_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root)
            run = store.create('complex task', None, None)
            store.transition(run['id'], {'queued'}, 'running')
            store.transition(run['id'], {'running'}, 'completed', skill_review=True)
            jobs = SkillReviewJobs(store, SimpleNamespace(complete=AsyncMock()), root / 'skills')
            proposal = {'status': 'proposed', 'action': 'create', 'name': 'workflow',
                        'content': '---\nname: workflow\ndescription: A reusable workflow\n---\n\n# Workflow\n\nVerify the result.\n'}
            with patch('harness.skills.jobs.review_skills', new=AsyncMock(return_value=proposal)):
                await jobs._process(*jobs._claim())
            self.assertTrue((root / 'skills' / 'workflow' / 'SKILL.md').exists())
            self.assertEqual(store.events(run['id'])[-1]['data']['status'], 'completed')

    async def test_stale_lease_cannot_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root)
            run = store.create('task', None, None)
            store.transition(run['id'], {'queued'}, 'running')
            store.transition(run['id'], {'running'}, 'completed', skill_review=True)
            jobs = SkillReviewJobs(store, SimpleNamespace(complete=AsyncMock()), root / 'skills')
            run_id, token = jobs._claim()
            with store.connect() as db:
                db.execute("UPDATE skill_review_jobs SET result='new-owner' WHERE run_id=?", (run_id,))
            proposal = {'status': 'proposed', 'action': 'create', 'name': 'new', 'content': 'candidate'}
            with patch('harness.skills.jobs.review_skills', new=AsyncMock(return_value=proposal)), patch('harness.skills.jobs.publish_new_skill') as publish:
                await jobs._process(run_id, token)
                publish.assert_not_called()

class ReviewEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_candidate_endpoint(self):
        import httpx
        from harness.api.server import create_app
        from harness.config import Settings

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Settings(data_dir=Path(tmp), api_token='token', worker_secret='secret'), local_ui=True)
            store = app.state.store
            run = store.create('task', None, None)
            store.transition(run['id'], {'queued'}, 'running')
            store.transition(run['id'], {'running'}, 'completed', skill_review=True)
            candidate = {'status': 'proposed', 'name': 'example', 'content': 'candidate'}
            with store.connect() as db:
                db.execute("UPDATE skill_review_jobs SET status='done', result=? WHERE run_id=?", (json.dumps(candidate), run['id']))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
                url = f"/v1/runs/{run['id']}/skill-review"
                self.assertEqual((await client.get(url)).status_code, 401)
                response = await client.get(url, headers={'Authorization': 'Bearer token'})
                self.assertEqual(response.json(), candidate)
