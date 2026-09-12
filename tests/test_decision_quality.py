"""The quality page compares what the AI decided with what people decided on real requests."""
from datetime import datetime, timedelta

import pytest

from conftest import SUPER_ADMIN, create_organization, create_user, execute, login

QUALITY = '/api/auth/decision_quality'


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def add(app_db, ai_decision, final_decision, reviewed_by=None, days_ago=1, hours_to_decide=None, organization_id=None):
    """Stores a request the way the app would have left it, so the page can be measured on known data."""
    created = datetime.utcnow() - timedelta(days=days_ago)
    reviewed = created + timedelta(hours=hours_to_decide) if hours_to_decide is not None else None
    rows = execute(
        app_db,
        'INSERT INTO Requests (role, department, request_type, destination, amount, currency, normalized_amount, '
        'xgb_score, ai_decision, final_decision, reviewed_by, reviewed_at, submitted_by, organization_id, created_at) '
        "VALUES ('Junior Developer', 'Engineering', 'Hotel Booking', 'Mumbai', 5000, 'INR', 5000, 0.5, ?, ?, ?, ?, "
        "'staff@example.com', ?, ?) RETURNING id",
        (ai_decision, final_decision, reviewed_by, reviewed.strftime('%Y-%m-%d %H:%M:%S') if reviewed else None,
         organization_id or app_db.DEFAULT_ORGANIZATION_ID, created.strftime('%Y-%m-%d %H:%M:%S'))
    )
    return rows[0][0]


def add_spot_check(app_db, request_id, verdict=None, organization_id=None):
    execute(
        app_db, 'INSERT INTO SpotChecks (organization_id, request_id, verdict) VALUES (?, ?, ?)',
        (organization_id or app_db.DEFAULT_ORGANIZATION_ID, request_id, verdict)
    )


@pytest.fixture
def admin(app_db):
    create_user(app_db, 'manager@example.com', role='Admin')
    return session(app_db, 'manager@example.com')


def test_an_empty_organization_reports_nothing_rather_than_zero_percent(app_db, admin):
    report = admin.get(QUALITY).get_json()
    assert report['days'] == 90
    assert report['enough_decisions'] is False
    assert report['totals'] == {'requests': 0, 'automatic_approvals': 0, 'decided_by_people': 0, 'waiting': 0}
    assert report['comparison']['agreement_rate'] is None
    assert report['spot_checks'] == {'done': 0, 'waiting': 0, 'wrong': 0, 'wrong_rate': None}
    assert report['speed'] == {'decided': 0, 'average_hours': None, 'slowest_hours': None}


def test_the_ai_and_the_people_are_compared_on_decided_requests(app_db, admin):
    add(app_db, 'APPROVED', 'APPROVED', reviewed_by='manager@example.com')        # Agreed.
    add(app_db, 'APPROVED', 'APPROVED', reviewed_by='manager@example.com')        # Agreed.
    add(app_db, 'APPROVED', 'REJECTED', reviewed_by='manager@example.com')        # The AI would have let it through.
    add(app_db, 'ESCALATED_ANOMALY', 'REJECTED', reviewed_by='manager@example.com')  # Agreed.
    add(app_db, 'ESCALATED_POLICY', 'APPROVED', reviewed_by='manager@example.com')   # Asked for nothing.

    comparison = admin.get(QUALITY).get_json()['comparison']
    assert comparison == {
        'pairs': 5, 'agreed': 3, 'missed_problems': 1, 'unnecessary_reviews': 1, 'ai_approved': 3, 'ai_flagged': 2,
        'agreement_rate': 0.6, 'missed_problem_rate': round(1 / 3, 4), 'unnecessary_review_rate': 0.5,
    }


def test_requests_nobody_decided_are_counted_but_never_compared(app_db, admin):
    add(app_db, 'APPROVED', 'APPROVED')                                             # Approved automatically.
    add(app_db, 'APPROVED', 'ESCALATED_SHADOW')                                     # Still waiting.
    add(app_db, 'ESCALATED_RULE', 'ESCALATED_RULE')                                 # Still waiting.
    add(app_db, 'APPROVED', 'REJECTED', reviewed_by='manager@example.com')          # The only real pair.

    report = admin.get(QUALITY).get_json()
    assert report['totals'] == {'requests': 4, 'automatic_approvals': 1, 'decided_by_people': 1, 'waiting': 2}
    assert report['comparison']['pairs'] == 1


def test_only_requests_inside_the_window_are_measured(app_db, admin):
    add(app_db, 'APPROVED', 'APPROVED', reviewed_by='manager@example.com', days_ago=5)
    add(app_db, 'APPROVED', 'APPROVED', reviewed_by='manager@example.com', days_ago=60)
    add(app_db, 'APPROVED', 'APPROVED', reviewed_by='manager@example.com', days_ago=200)

    assert admin.get(QUALITY, query_string={'days': 30}).get_json()['comparison']['pairs'] == 1
    assert admin.get(QUALITY, query_string={'days': 90}).get_json()['comparison']['pairs'] == 2
    assert admin.get(QUALITY, query_string={'days': 365}).get_json()['comparison']['pairs'] == 3
    assert admin.get(QUALITY, query_string={'days': 7}).status_code == 400


def test_the_page_says_when_there_are_too_few_decisions_to_trust(app_db, admin):
    for _ in range(app_db.QUALITY_ENOUGH_DECISIONS - 1):
        add(app_db, 'APPROVED', 'APPROVED', reviewed_by='manager@example.com')
    assert admin.get(QUALITY).get_json()['enough_decisions'] is False

    add(app_db, 'APPROVED', 'APPROVED', reviewed_by='manager@example.com')
    report = admin.get(QUALITY).get_json()
    assert (report['enough_decisions'], report['minimum_decisions']) == (True, app_db.QUALITY_ENOUGH_DECISIONS)


def test_spot_check_answers_and_waiting_time_are_reported(app_db, admin):
    first = add(app_db, 'APPROVED', 'APPROVED', days_ago=3)
    second = add(app_db, 'APPROVED', 'APPROVED', days_ago=2)
    third = add(app_db, 'APPROVED', 'APPROVED', days_ago=1)
    add_spot_check(app_db, first, 'CORRECT')
    add_spot_check(app_db, second, 'WRONG')
    add_spot_check(app_db, third)

    add(app_db, 'APPROVED', 'APPROVED', reviewed_by='manager@example.com', hours_to_decide=2)
    add(app_db, 'APPROVED', 'REJECTED', reviewed_by='manager@example.com', hours_to_decide=8)

    report = admin.get(QUALITY).get_json()
    assert report['spot_checks'] == {'done': 2, 'waiting': 1, 'wrong': 1, 'wrong_rate': 0.5}
    assert report['speed'] == {'decided': 2, 'average_hours': 5.0, 'slowest_hours': 8.0}


def test_one_organizations_numbers_never_include_another(app_db, admin):
    other_org = create_organization(app_db, 'Other Co')
    create_user(app_db, 'admin@other.test', role='Admin', organization_id=other_org)
    add(app_db, 'APPROVED', 'APPROVED', reviewed_by='manager@example.com')
    other_request = add(app_db, 'APPROVED', 'REJECTED', reviewed_by='admin@other.test', organization_id=other_org)
    add_spot_check(app_db, other_request, 'WRONG', organization_id=other_org)

    mine = admin.get(QUALITY).get_json()
    assert (mine['totals']['requests'], mine['comparison']['missed_problems'], mine['spot_checks']['done']) == (1, 0, 0)

    theirs = session(app_db, 'admin@other.test').get(QUALITY).get_json()
    assert (theirs['totals']['requests'], theirs['comparison']['missed_problems'], theirs['spot_checks']['done']) == (1, 1, 1)


def test_only_administrators_see_the_quality_page(app_db, admin):
    create_user(app_db, 'staff@example.com')
    assert session(app_db, 'staff@example.com').get(QUALITY).status_code == 403
    assert session(app_db, SUPER_ADMIN['email']).get(QUALITY).status_code == 200
