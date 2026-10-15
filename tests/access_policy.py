"""LDAP DN не может склеить две разные группы в один permission mapping."""
import unittest
import hermetic  # noqa: F401 — раньше mop
from mop.server.access.policy import directory_dn_key


# HYPOTHESIS: split(',') принимает escaped comma за разделитель RDN и
# удаляет значимый пробел в CN, выдавая права от другой LDAP группы.
# SOLUTION: разделитель RDN определяется escape state, не raw split.
# RESULT: две red проверки стали green, AD case/formatting сохранены.
# STATUS: FIXED — see #633
class DirectoryNames(unittest.TestCase):
    def test_escaped_comma_retains_significant_value_space(self):
        self.assertNotEqual(directory_dn_key(r'CN=admin\, team,OU=groups,DC=example'),
            directory_dn_key(r'CN=admin\,team,OU=groups,DC=example'))

    def test_escaped_trailing_space_is_not_rdn_formatting(self):
        self.assertNotEqual(directory_dn_key('CN=admin\\ ,OU=groups'),
            directory_dn_key('CN=admin\\,OU=groups'))
        self.assertIn('\\ ', directory_dn_key('CN=admin\\ ,OU=groups'))

    def test_insignificant_spacing_and_ad_case_still_match(self):
        self.assertEqual(directory_dn_key(' CN=Team, OU=Groups, DC=Example '),
            directory_dn_key('cn=team,ou=groups,dc=example'))
        self.assertEqual(directory_dn_key(r'CN=value\\, OU=Groups'),
            directory_dn_key(r'cn=value\\,ou=groups'))


if __name__ == '__main__':
    unittest.main()
