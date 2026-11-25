"""Durable, trace-based reviews of completed runs."""
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from harness.skills.evolution import review_skills
from harness.skills.profiles import skill_profiles
from harness.skills.publication import (
    SkillPublicationConflict, preflight_skill, publish_new_skill, publish_skill_file, publish_skill_update,
)
from harness.storage.sqlite import now

logger = logging.getLogger(__name__)


class SkillReviewJobs:
    def __init__(self, store, llm, directory):
        self.store, self.llm, self.directory = store, llm, directory
        self.task = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name="skill-reviews")

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def _claim(self):
        token = uuid4().hex
        until = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT run_id FROM skill_review_jobs WHERE status='queued'
                OR (status='running' AND lease_until < ?) ORDER BY created_at LIMIT 1""", (now(),)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE skill_review_jobs SET status='running', lease_until=?, result=?, attempts=attempts+1 WHERE run_id=?",
                       (until, token, row['run_id']))
            return row['run_id'], token

    async def _process(self, run_id, token):
        run = self.store.get(run_id)
        events = self.store.events(run_id, include_deltas=False)
        environment = next((e['data'].get('capabilities', {}).get('environment') for e in events
                            if e['type'] == 'worker_bound'), None)
        profiles = skill_profiles(self.directory, environment)
        for profile in profiles:
            try:
                with open(profile['path'], encoding='utf-8') as document:
                    profile['content'] = document.read(32000)
            except (OSError, UnicodeError):
                pass
        previous = self.store.previous(run['session_id'], run_id)
        previous = [item for item in previous if item['status'] == 'completed']
        for index in range(len(previous) - 1, -1, -1):
            if any(event['type'] == 'skill_review' and event['data'].get('status') == 'queued'
                   for event in previous[index]['events']):
                previous = previous[index + 1:]
                break
        trace = {**run, 'events': events, 'previous': previous[-20:]}
        result = await asyncio.wait_for(review_skills(trace, profiles, self.llm.complete, environment=environment), timeout=180)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute("SELECT 1 FROM skill_review_jobs WHERE run_id=? AND status='running' AND result=?", (run_id, token)).fetchone()
            if not current:
                return
            if result.get('status') == 'proposed':
                try:
                    preflight_skill(self.directory, result['name'], result['action'], result['content'],
                                    result.get('base_content'), result.get('files', {}))
                    if result.get('action') == 'create':
                        publish_new_skill(self.directory, result['name'], result['content'])
                    for path, change in result.get('files', {}).items():
                        publish_skill_file(self.directory, result['name'], path, change['content'], change.get('base_content'))
                    if result.get('action') == 'update':
                        publish_skill_update(self.directory, result['name'], result['content'], result.get('base_content'))
                    result = {'status': 'completed', 'name': result['name'], 'action': result['action']}
                except SkillPublicationConflict:
                    result = {'status': 'failed', 'name': result['name'], 'reason': '技能文件在复盘期间发生变化，请重新复盘。'}
            # Review output stored with the run is metadata only. Skill content lives on disk.
            result = {key: value for key, value in result.items() if key not in {'content', 'base_content', 'files'}}
            db.execute("UPDATE skill_review_jobs SET status='done', result=?, lease_until=NULL WHERE run_id=?", (json.dumps(result, ensure_ascii=False), run_id))
            self.store._append(db, run_id, 'skill_review', {k: v for k, v in result.items() if k != 'content'})

    async def _loop(self):
        while True:
            job = self._claim()
            if not job:
                await asyncio.sleep(2)
                continue
            try:
                await self._process(*job)
            except asyncio.CancelledError:
                with self.store.connect() as db:
                    db.execute("UPDATE skill_review_jobs SET status='queued', lease_until=NULL WHERE run_id=? AND result=?", job)
                raise
            except Exception:
                logger.exception('Skill review failed for %s', job[0])
                with self.store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    changed = db.execute("UPDATE skill_review_jobs SET status='failed', result=NULL, lease_until=NULL WHERE run_id=? AND result=?", job)
                    if changed.rowcount:
                        self.store._append(db, job[0], 'skill_review', {'status': 'failed', 'reason': '技能复盘失败，请查看服务日志。'})
