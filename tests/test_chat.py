"""Chat pipeline test: builds a meeting in the DB, asks a question, checks grounding."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from muesli import chatlogic, db  # noqa: E402

db.init()
mid = db.create_meeting("Chat test meeting", "auto")
segs = [
    (0, 4, "Them", "We decided to launch the new dashboard in March."),
    (4, 9, "Them", "Sarah will send the pricing proposal to the client by Friday."),
    (9, 14, "Me", "I am worried about the database migration timeline."),
    (14, 19, "Them", "Let's schedule a follow up call next Tuesday to review the budget."),
]
for t0, t1, sp, tx in segs:
    db.add_segment(mid, t0, t1, sp, tx)
db.update_meeting(mid, notes_md="- pricing\n- migration worries")

res = chatlogic.answer("Who is sending the pricing proposal and when?", mid)
print("provider:", res["provider"], "grounded:", res["grounded"])
print("answer:", res["text"][:400])
ok = "sarah" in res["text"].lower() and "friday" in res["text"].lower()
print("PASS" if ok else "SOFT-FAIL (check answer above)")
db.delete_meeting(mid)
sys.exit(0 if ok else 1)
