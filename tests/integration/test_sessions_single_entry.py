"""One Recent Journeys row per session, with a stable first-user-message title."""
import unittest
from storage.memory import MemoryStore


class SessionListingTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(':memory:')

    def tearDown(self):
        self.store.close()

    def test_one_row_per_session_and_stable_preview(self):
        self.store.add_message('u', 'session-a', 'user', 'Zurich plan')
        self.store.add_message('u', 'session-a', 'assistant', 'First answer')
        self.store.add_message('u', 'session-a', 'user', 'A second question')
        self.store.add_message('u', 'session-a', 'assistant', 'Second answer')
        sessions = self.store.sessions('u')
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['preview'], 'Zurich plan')

    def test_full_history_survives_refresh(self):
        for n in range(63):
            self.store.add_message('u', 'session-a', 'user', f'Message #{n}')
        self.assertEqual(len(self.store.messages('u', 'session-a')), 40)
        history = self.store.messages('u', 'session-a', limit=None)
        self.assertEqual(len(history), 63)
        self.assertEqual(history[0]['content'], 'Message #0')
        self.assertEqual(history[-1]['content'], 'Message #62')

    def test_order_and_isolation(self):
        self.store.add_message('u', 'session-a', 'user', 'First trip')
        self.store.add_message('u', 'session-b', 'user', 'Second trip')
        self.assertEqual([item['session_id'] for item in self.store.sessions('u')], ['session-b', 'session-a'])
        self.store.add_message('other', 'session-a', 'user', 'Private trip')
        self.assertEqual(len(self.store.sessions('u')), 2)
        self.assertEqual(self.store.sessions('other')[0]['preview'], 'Private trip')


if __name__ == '__main__':
    unittest.main()
