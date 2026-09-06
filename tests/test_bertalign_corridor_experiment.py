import unittest
from unittest import TestCase

import numpy as np

try:
    # Experimental corridor driver; the ``scripts/`` helpers are local WIP and
    # are not shipped in the repo, so this import (and the whole experiment
    # module) is absent on a clean checkout / CI. Skip visibly there rather
    # than failing collection.
    from scripts.bertalign_corridor_experiment import align_pass, two_pass_corridors

    _EXPERIMENT_AVAILABLE = True
except ImportError:
    _EXPERIMENT_AVAILABLE = False


@unittest.skipUnless(
    _EXPERIMENT_AVAILABLE,
    "scripts.bertalign_corridor_experiment (local experiment driver) unavailable",
)
class TwoPassExperimentTests(TestCase):
    def test_one_to_four_recovery(self):
        links = align_pass(np.ones((1,2)),np.ones((4,2)),5,.83)
        self.assertEqual(links[0][:4],(0,1,0,4))

    def test_never_leaves_body_corridor(self):
        vectors=np.eye(8)
        links=two_pass_corridors(vectors,vectors,[(0,0),(2,2),(5,5),(7,7)],{"pivot":[2,6],"target":[2,6]})
        self.assertTrue(links)
        self.assertTrue(all(3<=a<b<=5 and 3<=c<d<=5 for a,b,c,d,_ in links))

    def test_no_unbounded_tail_recovery(self):
        v=np.ones((4,2))
        self.assertEqual(two_pass_corridors(v,v,[(1,1)],{"pivot":[0,4],"target":[0,4]}),[])

    def test_rejects_crossed_anchors(self):
        v=np.eye(4)
        with self.assertRaises(ValueError):
            two_pass_corridors(v,v,[(1,2),(2,1)],{"pivot":[0,4],"target":[0,4]})
