"""Approval and review rates by department and role, so one group is not quietly treated differently."""
from datetime import datetime, timedelta

import pytest

from conftest import create_organization, create_user, execute, login

FAIRNESS = '/api/auth/fairness'


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def add(app_db, department='Engineering', role='Junior Developer', final_decision='APPROVED', reviewed_by=None,
        days_ago=1, organization_id=None):
    created = datetime.utcnow() - timedelta(days=days_ago)
    execute(
        app_db,
        'INSERT INTO Requests (role, department, request_type, destination, amount, currency, normalized_amount, '
        'xgb_score, ai_decision, final_decision, reviewed_by, submitted_by, organization_id, created_at) '
        "VALUES (?, ?, 'Hotel Booking', 'Mumbai', 5000, 'INR', 5000, 0.9, 'APPROVED', ?, ?, 'staff@example.com', ?, ?)",
        (role, department, final_decision, reviewed_by, organization_id or app_db.DEFAULT_ORGANIZATION_ID,
         created.strftime('%Y-%m-%d %H:%M:%S'))
    )


def many(app_db, count, **changes):
    for _ in range(count):
        add(app_db, **changes)


def group(report, field, value):
    return next(row for row in report[f'by_{field}'] if row['value'] == value)


@pytest.fixture
def admin(app_db):
    create_user(app_db, 'manager@example.com', role='Admin')
    return session(app_db, 'manager@example.com')


def test_every_department_and_role_is_counted(app_db, admin):
    many(app_db, 3, department='Engineering', role='Junior Developer')
    many(app_db, 2, department='Sales', role='Manager', final_decision='REJECTED', reviewed_by='manager@example.com')
    add(app_db, department='', role='Manager')

    report = admin.get(FAIRNESS).get_json()
    assert [(row['value'], row['requests']) for row in report['by_department']] == [
        ('Engineering', 3), ('Sales', 2), ('Not given', 1)
    ]
    # Groups of the same size are listed alphabetically.
    assert [(row['value'], row['requests']) for row in report['by_role']] == [('Junior Developer', 3), ('Manager', 3)]
    assert report['overall'] == {
        'requests': 6, 'decided': 6, 'approved': 4, 'approval_rate': round(4 / 6, 4), 'needed_person': 2,
        'needed_person_rate': round(2 / 6, 4),
    }


def test_a_department_approved_far_less_often_is_pointed_out(app_db, admin):
    many(app_db, 30, department='Engineering')
    many(app_db, 6, department='Sales')
    many(app_db, 6, department='Sales', final_decision='REJECTED', reviewed_by='manager@example.com')

    report = admin.get(FAIRNESS).get_json()
    sales = group(report, 'department', 'Sales')
    assert (sales['approval_rate'], sales['approval_gap']) == (0.5, True)
    assert group(report, 'department', 'Engineering')['approval_gap'] is False
    # Sales is also sent to a person more often, but rejecting needs a person, so it is said once.
    assert (sales['review_gap'], [note['value'] for note in report['notes']]) == (True, ['Sales'])
    assert 'approved 50% of the time, against 86% across the company' in report['notes'][0]['message']


def test_a_role_sent_to_a_person_far_more_often_is_pointed_out(app_db, admin):
    many(app_db, 30, role='Junior Developer')
    many(app_db, 12, role='Intern', reviewed_by='manager@example.com')

    report = admin.get(FAIRNESS).get_json()
    intern = group(report, 'role', 'Intern')
    assert (intern['needed_person_rate'], intern['review_gap'], intern['approval_gap']) == (1.0, True, False)
    assert 'sent to a person' in report['notes'][0]['message']


def test_small_groups_are_shown_but_never_judged(app_db, admin):
    many(app_db, 30, department='Engineering')
    many(app_db, 4, department='Facilities', final_decision='REJECTED', reviewed_by='manager@example.com')

    report = admin.get(FAIRNESS).get_json()
    facilities = group(report, 'department', 'Facilities')
    assert (facilities['requests'], facilities['approval_rate']) == (4, 0.0)
    assert (facilities['compared'], facilities['approval_gap'], facilities['review_gap']) == (False, False, False)
    assert report['notes'] == []


def test_a_company_treating_everyone_the_same_gets_no_notes(app_db, admin):
    for department in ('Engineering', 'Sales', 'Finance'):
        many(app_db, 12, department=department)
        add(app_db, department=department, final_decision='REJECTED', reviewed_by='manager@example.com')

    report = admin.get(FAIRNESS).get_json()
    assert report['notes'] == []
    assert all(row['compared'] for row in report['by_department'])


def test_only_the_chosen_window_and_the_own_organization_count(app_db, admin):
    other_org = create_organization(app_db, 'Other Co')
    many(app_db, 3, department='Engineering')
    add(app_db, department='Engineering', days_ago=120)
    add(app_db, department='Engineering', organization_id=other_org)

    assert admin.get(FAIRNESS).get_json()['overall']['requests'] == 3
    assert admin.get(FAIRNESS, query_string={'days': 365}).get_json()['overall']['requests'] == 4
    assert admin.get(FAIRNESS, query_string={'days': 5}).status_code == 400


def test_employees_never_see_the_breakdown(app_db, admin):
    create_user(app_db, 'staff@example.com')
    assert session(app_db, 'staff@example.com').get(FAIRNESS).status_code == 403
