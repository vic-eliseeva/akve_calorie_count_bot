"""Verify the default sheet and that Railway image includes Google Sheets code."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from google_sheets import DEFAULT_SPREADSHEET_ID, sheets_from_env


class DeploymentConfigurationTests(unittest.TestCase):
    def test_preconfigured_akve_sheet_without_env_override(self):
        self.assertEqual(DEFAULT_SPREADSHEET_ID, '1w6s6N5IpNHtuYUHBT6QO3PJ2ZDsLVysq4iHIQXLZg1A')
        with patch.dict(os.environ, {'GOOGLE_SHEET_ID': '', 'GOOGLE_SERVICE_ACCOUNT_JSON': '', 'GOOGLE_SERVICE_ACCOUNT_FILE': ''}):
            with patch('google_sheets.GoogleSheets') as client:
                sheets_from_env()
                self.assertEqual(client.call_args.args[0], DEFAULT_SPREADSHEET_ID)
                self.assertEqual(client.call_args.kwargs['ingredients_tab'], 'Bot Ingredients')
                self.assertEqual(client.call_args.kwargs['daily_tab'], 'Bot Daily Totals')

    def test_dockerfile_includes_google_sheets(self):
        dockerfile = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
        self.assertIn('google_sheets.py', dockerfile)

if __name__ == '__main__':
    unittest.main()
