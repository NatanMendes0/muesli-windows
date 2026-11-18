"""Category persistence and privacy disclosure tests."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from muesli import api, db  # noqa: E402


class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "test.db")
        if hasattr(db._local, "conn"):
            db._local.conn.close()
            del db._local.conn
        db.init()

    def tearDown(self):
        if hasattr(db._local, "conn"):
            db._local.conn.close()
            del db._local.conn
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_category_create_rename_assign_and_delete_unassigns_meetings(self):
        category_id = db.create_category("  Trabalho  ")
        self.assertIsNotNone(category_id)
        self.assertIsNone(db.create_category("trabalho"))
        self.assertIsNone(db.create_category("  "))

        meeting_id = db.create_meeting("Alinhamento", "auto", category_id=category_id)
        self.assertEqual(db.get_meeting(meeting_id)["category_id"], category_id)
        self.assertEqual(db.list_meetings()[0]["category_id"], category_id)
        db._local.conn.close()
        del db._local.conn
        db.init()
        self.assertEqual((db.get_meeting(meeting_id) or {}).get("category_id"), category_id)
        self.assertEqual(db.list_categories()[0]["id"], category_id)
        other_category_id = db.create_category("Pessoal")
        self.assertFalse(db.rename_category(other_category_id, " trabalho "))
        self.assertTrue(db.rename_category(category_id, "Projetos"))
        self.assertEqual({c["id"]: c["name"] for c in db.list_categories()}[category_id], "Projetos")

        db.delete_category(category_id)
        self.assertEqual([c["id"] for c in db.list_categories()], [other_category_id])
        self.assertIsNone(db.get_meeting(meeting_id)["category_id"])

    def test_category_assignment_rejects_unknown_category(self):
        meeting_id = db.create_meeting("Sem categoria", "auto", category_id=12345)
        self.assertIsNone(db.get_meeting(meeting_id)["category_id"])

    def test_category_assignment_ignores_a_serialized_click_event(self):
        meeting_id = db.create_meeting("Sem categoria", "auto", category_id={"type": "click"})
        self.assertIsNone((db.get_meeting(meeting_id) or {}).get("category_id"))

    def test_delete_waits_until_the_recording_transcription_has_drained(self):
        meeting_id = db.create_meeting("Encerrando", "auto")
        db.end_meeting(meeting_id, 20)
        backend = api.Api()
        backend._draining.add(meeting_id)
        self.assertFalse(backend.delete_meeting(meeting_id)["ok"])
        self.assertIsNotNone(db.get_meeting(meeting_id))
        backend._draining.clear()
        self.assertTrue(backend.delete_meeting(meeting_id)["ok"])
        self.assertIsNone(db.get_meeting(meeting_id))


if __name__ == "__main__":
    unittest.main()
