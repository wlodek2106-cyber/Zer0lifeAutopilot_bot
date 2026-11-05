import unittest

from access_control import admin_access_status


class AdminApiAuthorizationTests(unittest.TestCase):
    def test_owner_is_allowed(self):
        self.assertIsNone(admin_access_status(428821665, "428821665"))

    def test_non_owner_is_forbidden(self):
        self.assertEqual(admin_access_status(202, "428821665"), 403)

    def test_missing_or_invalid_identity_is_unauthorized(self):
        self.assertEqual(admin_access_status(None, "428821665"), 401)
        self.assertEqual(admin_access_status("not-an-id", "428821665"), 401)

    def test_missing_or_invalid_admin_configuration_fails_closed(self):
        self.assertEqual(admin_access_status(428821665, ""), 503)
        self.assertEqual(admin_access_status(428821665, "bad-admin-id"), 503)


if __name__ == "__main__":
    unittest.main()
