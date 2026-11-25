from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from harness.skills.publication import SkillPublicationConflict, publish_new_skill


class SkillPublicationTests(unittest.TestCase):
    def test_publish_and_do_not_replace(self):
        with TemporaryDirectory() as path:
            root = Path(path)
            content = '---\nname: sample\ndescription: Sample workflow\n---\n# Steps\n'
            publish_new_skill(root, 'sample', content)
            self.assertEqual((root / 'sample' / 'SKILL.md').read_text(), content)
            with self.assertRaises(SkillPublicationConflict):
                publish_new_skill(root, 'sample', content + 'overwrite')
            self.assertEqual((root / 'sample' / 'SKILL.md').read_text(), content)

    def test_reject_invalid_and_symlink(self):
        with TemporaryDirectory() as path:
            root = Path(path)
            with self.assertRaises(ValueError):
                publish_new_skill(root, '../outside', 'bad')
            with self.assertRaises(ValueError):
                publish_new_skill(root, 'sample', '---\nname: wrong\ndescription: x\n---\nbody')
            (root / 'sample').symlink_to(root, target_is_directory=True)
            with self.assertRaises(SkillPublicationConflict):
                publish_new_skill(root, 'sample', '---\nname: sample\ndescription: x\n---\nbody')
