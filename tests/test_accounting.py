"""Pure local ownership/parser tests; never invokes Slurm or subprocesses."""
import importlib.util
from functools import partial
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'service' / 'accounting.py'
SPEC = importlib.util.spec_from_file_location('e2e_accounting', str(SOURCE))
accounting = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(accounting)
parse_records = partial(accounting.parse_records, expected_user='test-student')


class AccountingCase(unittest.TestCase):
    def test_blank_step_user_inherits_parent_even_when_step_comes_first(self):
        raw = '28100.2||run.sh|COMPLETED|0:0|12|2|cpu=2|14M\n28100|test-student|job|RUNNING|0:0|15|20|cpu=20|\n'
        step = parse_records(raw, ['28100'], queried_at=123)[0]
        self.assertEqual(step['allocation_job_id'], '28100')
        self.assertEqual(step['step_id'], '2')
        self.assertEqual(step['state'], 'COMPLETED')
        self.assertEqual(step['reported_user'], None)
        self.assertEqual(step['user'], 'test-student')
        self.assertEqual(step['ownership_source'], 'allocation_user')
        self.assertEqual(step['observed_at'], 123)
        self.assertEqual(step['max_rss'], '14M')

    def test_registered_id_alone_does_not_prove_blank_step_ownership(self):
        raw = '28100.2||run.sh|COMPLETED|0:0|12|2|cpu=2|14M\n'
        self.assertEqual(parse_records(raw, ['28100']), [])
        trusted = parse_records(raw, ['28100'], own_job_id='28100')
        self.assertEqual(trusted[0]['ownership_source'], 'current_slurm_allocation')

    def test_foreign_or_unregistered_records_are_never_accepted(self):
        raw = ('28100|other|job|RUNNING|0:0|10|20|cpu=20|\n'
               '28100.2||run.sh|COMPLETED|0:0|1|2|cpu=2|\n'
               '28101|test-student|job|COMPLETED|0:0|1|1|cpu=1|\n')
        self.assertEqual(parse_records(raw, ['28100'], own_job_id='28100'), [])

    def test_explicit_foreign_step_is_rejected_despite_verified_parent(self):
        raw = ('28100|test-student|job|RUNNING|0:0|10|20|cpu=20|\n'
               '28100.2|other|run.sh|COMPLETED|0:0|1|2|cpu=2|\n')
        self.assertEqual(len(parse_records(raw, ['28100'])), 1)

    def test_cancelled_state_and_zero_resources_preserved_without_invention(self):
        raw = ('28100|test-student|job|CANCELLED by 20721|0:0|0|8||\n'
               '28100.batch||batch|CANCELLED+|0:15|0|0||0\n')
        records = parse_records(raw, ['28100'])
        self.assertEqual(records[0]['state'], 'CANCELLED')
        self.assertEqual(records[0]['max_rss'], None)
        self.assertEqual(records[1]['step_id'], 'batch')
        self.assertEqual(records[1]['exit_code'], '0:15')
        self.assertEqual(records[1]['allocated_cpus'], '0')
        self.assertEqual(records[1]['max_rss'], '0')

    def test_malformed_lines_and_partial_registration_are_ignored(self):
        self.assertEqual(parse_records('garbage\n28100.2|too|short\n', ['28100']), [])
        temporary_root = SOURCE.parent.parent / 'tests'
        with tempfile.TemporaryDirectory(dir=str(temporary_root)) as temp:
            root = Path(temp)
            root.resolve().relative_to(temporary_root.resolve())
            (root / '28101.json').write_text('{"job_id":"28101"}')
            (root / '28102.json').write_text('{"job_id":"28103"}')
            (root / '28104.json').write_text('{')
            (root / 'not-a-job.json').write_text('{"job_id":"999"}')
            self.assertEqual(accounting.registered_jobs(root, '28100'), ['28100', '28101'])


if __name__ == '__main__':
    unittest.main()
