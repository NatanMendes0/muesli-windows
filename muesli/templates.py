"""Note templates. Builtins mirror common meeting formats; custom ones live in SQLite."""
from . import db

BUILTIN = [
    {
        "id": "auto",
        "name": "Auto",
        "builtin": True,
        "description": "Let the AI pick the structure that fits the conversation.",
        "sections": [
            {"title": "Summary", "hint": "Three to six sentences on what the meeting was about and how it went."},
            {"title": "Key points", "hint": "The most important points discussed, grouped by topic."},
            {"title": "Decisions", "hint": "Anything that was decided or agreed."},
            {"title": "Action items", "hint": "Tasks with owner and deadline when mentioned."},
        ],
    },
    {
        "id": "one_on_one",
        "name": "1:1",
        "builtin": True,
        "description": "Personal check-in between two people.",
        "sections": [
            {"title": "How things are going", "hint": "Mood, wins, general updates."},
            {"title": "Topics discussed", "hint": "Main discussion threads."},
            {"title": "Feedback", "hint": "Feedback given or received, both directions."},
            {"title": "Action items", "hint": "Commitments made by either person."},
        ],
    },
    {
        "id": "standup",
        "name": "Standup",
        "builtin": True,
        "description": "Daily team sync.",
        "sections": [
            {"title": "Done", "hint": "What was completed since last time, per person if possible."},
            {"title": "In progress", "hint": "What people are working on now."},
            {"title": "Blockers", "hint": "Anything blocking progress and who can unblock it."},
            {"title": "Action items", "hint": "Follow-ups agreed in the standup."},
        ],
    },
    {
        "id": "weekly_sync",
        "name": "Weekly Sync",
        "builtin": True,
        "description": "Recurring team or project sync.",
        "sections": [
            {"title": "Summary", "hint": "Short overview of the week and the meeting."},
            {"title": "Updates", "hint": "Status updates per workstream or person."},
            {"title": "Risks and concerns", "hint": "Anything at risk, delayed, or worrying."},
            {"title": "Decisions", "hint": "Decisions made in the meeting."},
            {"title": "Action items", "hint": "Tasks with owners and dates."},
        ],
    },
    {
        "id": "sales_discovery",
        "name": "Sales / Discovery",
        "builtin": True,
        "description": "Prospect or customer discovery call.",
        "sections": [
            {"title": "Company and contact", "hint": "Who they are, role, company context."},
            {"title": "Pain points", "hint": "Problems they described, in their words where possible."},
            {"title": "Current setup", "hint": "Tools and process they use today."},
            {"title": "Budget and timeline", "hint": "Any signal on budget, urgency, decision process."},
            {"title": "Objections", "hint": "Concerns or pushback raised."},
            {"title": "Next steps", "hint": "Agreed follow-ups with owners."},
        ],
    },
    {
        "id": "interview",
        "name": "Interview",
        "builtin": True,
        "description": "Candidate interview debrief.",
        "sections": [
            {"title": "Background", "hint": "Candidate background and experience highlights."},
            {"title": "Questions and answers", "hint": "Key questions asked and how they answered."},
            {"title": "Strengths", "hint": "Where the candidate was strong."},
            {"title": "Concerns", "hint": "Gaps, risks, or doubts."},
            {"title": "Next steps", "hint": "Follow-up actions and process next steps."},
        ],
    },
]


def all_templates():
    return BUILTIN + db.list_custom_templates()


def get(template_id: str):
    for t in all_templates():
        if t["id"] == template_id:
            return t
    return BUILTIN[0]
