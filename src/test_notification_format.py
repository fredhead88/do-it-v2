"""Formatter contract: strings stay human text; instants convert to Israel time."""
from datetime import datetime, timezone
import unittest
import notification_policy as policy


class FormatTests(unittest.TestCase):
    def test_labels_fields_and_optional_values(self):
        for label in ('[YOU]', '[DOWN]', '[MERGED]', '[DAILY]'):
            text = policy.format_message(label, 'Change ready', 'Confirmed https://example.org/a', 'Review it', 'Tomorrow', 'Waiting')
            self.assertTrue(text.startswith(label + ' Change ready'))
            for clause in ('Confirmed https://example.org/a', 'You: Review it', 'By: Tomorrow', 'Doing: Waiting'):
                self.assertIn(clause, text)
        text = policy.format_message('[YOU]', 'Ready', 'Checked', 'Read', '', None)
        self.assertNotIn('By:', text)
        self.assertNotIn('Doing:', text)

    def test_refusals(self):
        for label in (None, '', 'YOU', '[UNKNOWN]'):
            with self.assertRaises(ValueError):
                policy.format_message(label, 'Ready', 'Checked', 'Read', None, None)
        for field in range(3):
            for invalid in ('', ' ', 'You: ', 'at 12:00 UTC', '2026-01-15T12:00:00+00:00', '12:00Z',
                            'file:///a', 'http://localhost/a', 'https://127.0.0.1/a', '/home/operator/report', '~/report'):
                fields = ['Ready', 'Checked', 'Read']; fields[field] = invalid
                with self.assertRaises(ValueError, msg=repr(invalid)):
                    policy.format_message('[YOU]', *fields, None, None)
        for field in ('by', 'doing'):
            with self.assertRaises(ValueError):
                policy.format_message('[YOU]', 'Ready', 'Checked', 'Read', **{field: '12:00 UTC'})

    def test_datetime_conversion(self):
        for month, hour in ((1, 14), (7, 15)):
            text = policy.format_message('[YOU]', 'Ready', 'Checked', 'Read', datetime(2026, month, 15, 12, tzinfo=timezone.utc), None)
            self.assertIn(f'By: 2026-{month:02}-15 {hour}:00', text)
        with self.assertRaises(ValueError):
            policy.format_message('[YOU]', 'Ready', 'Checked', 'Read', datetime(2026, 1, 15), None)


if __name__ == '__main__':
    unittest.main()
