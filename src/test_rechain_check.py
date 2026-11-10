"""Cumulative stack regression tests using the existing resolver's real fixtures."""
import json
import tempfile
import unittest

from test_rechain import Fixture, git, response
import rechain
import rechain_check


class StackTests(unittest.TestCase):
    def test_ac1_order_and_mechanical_tree(self):
        with tempfile.TemporaryDirectory() as root:
            fx = Fixture(root)
            git(fx.repo, 'checkout', '-b', 'code', fx.base)
            fx.write('code.txt', 'code\n')
            code = ('code', 'code', fx.commit('code'))
            git(fx.repo, 'checkout', 'master')
            a, b = fx.branch('a'), fx.branch('b')
            before = (git(fx.repo, 'show-ref'), git(fx.repo, 'status', '--porcelain'),
                      git(fx.repo, 'rev-parse', 'HEAD'))
            steps = rechain_check.stack(fx.repo, fx.base, [code, a, b])
            self.assertEqual([s.spec for s in steps], ['code', 'a', 'b'])
            self.assertEqual([s.outcome for s in steps], ['clean', 'clean', 'rechained'])
            tip = steps[-1].tip
            self.assertEqual(rechain.text(fx.repo, tip, 'code.txt'), 'code\n')
            self.assertEqual(rechain.revision(rechain.text(fx.repo, tip, rechain.VERSIONS+'b.py')), ('b', 'a'))
            self.assertEqual(rechain.routed_head(fx.repo, tip), 'b')
            self.assertEqual(rechain.policy(rechain.text(fx.repo, tip, rechain.MANIFEST))[1]['allowedCurrentRevisions'], ['h0', 'a', 'b'])
            self.assertEqual(steps[-1].metadata['old_parent'], 'h0')
            self.assertEqual(steps[-1].metadata['new_parent'], 'a')
            self.assertEqual(before, (git(fx.repo, 'show-ref'), git(fx.repo, 'status', '--porcelain'), git(fx.repo, 'rev-parse', 'HEAD')))
            self.assertFalse(fx.events)  # No dispatch/event writer is supplied to the stacker.
            response('AC1', dict(outcomes=[s.outcome for s in steps], parent='a', head='b', caller_unchanged=True))

    def test_ac1_conflict_decline_and_stop(self):
        for unsafe in (False, True):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as root:
                fx = Fixture(root)
                def mutate_a(f):
                    if not unsafe:
                        f.write('outside.txt', 'a\n')
                def mutate_b(f):
                    if unsafe:
                        f.write(rechain.VERSIONS+'extra.py', "revision='extra'\ndown_revision='b'\n")
                    else:
                        f.write('outside.txt', 'b\n')
                if unsafe:
                    a, b = fx.branch('a'), fx.branch('b', mutation=mutate_b)
                else:
                    entries = []
                    for name, mutate in [('a', mutate_a), ('b', mutate_b)]:
                        git(fx.repo, 'checkout', '-B', 'code-'+name, fx.base)
                        mutate(fx)
                        entries.append((name, 'code-'+name, fx.commit(name)))
                    a, b = entries
                c = fx.branch('c')
                steps = rechain_check.stack(fx.repo, fx.base, [a, b, c])
                self.assertEqual(len(steps), 2)
                self.assertEqual(steps[-1].outcome, 'declined' if unsafe else 'content-conflict')
                self.assertEqual(steps[-1].tip, steps[0].tip)
                self.assertTrue(steps[-1].reason)
                if not unsafe:
                    self.assertIn('outside.txt', steps[-1].paths)
                response('AC1-stop', dict(unsafe=unsafe, outcomes=[s.outcome for s in steps], attempted=2))

    def test_ac1_unavailable_objects_are_not_conflicts(self):
        with tempfile.TemporaryDirectory() as root:
            fx = Fixture(root)
            with self.assertRaises(rechain_check.Undetermined):
                rechain_check.stack(fx.repo, fx.base, [('bad', 'bad', 'missing')])


if __name__ == '__main__':
    unittest.main(verbosity=2)
