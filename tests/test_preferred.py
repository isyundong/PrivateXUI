import ipaddress
import unittest

from preferred import BUILTIN_NETWORK, builtin_candidates


class PreferredTests(unittest.TestCase):
    def test_offline_candidates_belong_to_cloudflare_public_range(self):
        candidates = builtin_candidates()
        self.assertEqual(len(candidates), 6)
        self.assertEqual(len({item['address'] for item in candidates}), 6)
        network = ipaddress.ip_network(BUILTIN_NETWORK)
        for item in candidates:
            address = ipaddress.ip_address(item['address'])
            self.assertIn(address, network)
            self.assertTrue(address.is_global)
            self.assertIn('候选', item['name'])

    def test_candidate_mutations_do_not_change_other_deployments(self):
        changed = builtin_candidates()
        changed[0]['address'] = '192.0.2.1'
        self.assertNotEqual(builtin_candidates()[0]['address'], '192.0.2.1')


if __name__ == '__main__':
    unittest.main()
