"""Week by week counts, with a warning when the newest week does not look like the weeks before it."""
from datetime import datetime, timedelta

import pytest

from conftest import create_organization, create_platform_owner, create_user, execute, login

MONITORING = '/api/auth/monitoring'


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def add(app_db, days_ago, ai_decision='APPROVED', final_decision='APPROVED', reviewed_by=None, score=0.9, organization_id=None):
    created = datetime.utcnow() - timedelta(days=days_ago, hours=1)
    execute(
        app_db,
        'INSERT INTO Requests (role, department, request_type, destination, amount, currency, normalized_amount, '
        'xgb_score, ai_decision, final_decision, reviewed_by, submitted_by, organization_id, created_at) '
        "VALUES ('Junior Developer', 'Engineering', 'Hotel Booking', 'Mumbai', 5000, 'INR', 5000, ?, ?, ?, ?, "
        "'staff@example.com', ?, ?)",
        (score, ai_decision, final_decision, reviewed_by, organization_id or app_db.DEFAULT_ORGANIZATION_ID,
         created.strftime('%Y-%m-%d %H:%M:%S'))
    )


def steady_weeks(app_db, per_week=12, weeks=4, **changes):
    """Ordinary work in the weeks before the newest one."""
    for week in range(1, weeks + 1):
        for _ in range(per_week):
            add(app_db, days_ago=week * 7, **changes)


def warnings_of(report):
    return {warning['name'] for warning in report['warnings']}


@pytest.fixture
def admin(app_db):
    create_user(app_db, 'manager@example.com', role='Admin')
    return session(app_db, 'manager@example.com')


def test_the_weeks_are_listed_oldest_first_even_when_empty(app_db, admin):
    add(app_db, days_ago=1)
    add(app_db, days_ago=9)

    report = admin.get(MONITORING).get_json()
    assert len(report['weeks']) == app_db.MONITORING_WEEKS
    assert [week['requests'] for week in report['weeks'][-3:]] == [0, 1, 1]
    assert report['weeks'][-1]['ending'] > report['weeks'][-1]['starting'] > report['weeks'][-2]['starting']
    assert report['latest']['requests'] == 1


def test_each_week_counts_what_needed_a_person_and_what_the_ai_had_not_seen(app_db, admin):
    add(app_db, days_ago=1)                                                                  # Approved on its own.
    add(app_db, days_ago=2, final_decision='APPROVED', reviewed_by='manager@example.com')    # A person approved it.
    add(app_db, days_ago=3, ai_decision='ESCALATED_UNKNOWN', final_decision='ESCALATED_UNKNOWN')
    add(app_db, days_ago=4, ai_decision='ESCALATED_ANOMALY', final_decision='ESCALATED_ANOMALY')

    latest = admin.get(MONITORING).get_json()['latest']
    assert (latest['requests'], latest['needed_person'], latest['unknown_category'], latest['unusual']) == (4, 3, 1, 1)
    assert (latest['needed_person_rate'], latest['unknown_category_rate'], latest['unusual_rate']) == (0.75, 0.25, 0.25)
    assert latest['average_score'] == 0.9


def test_a_quiet_week_is_never_judged_against_the_weeks_before(app_db, admin):
    steady_weeks(app_db)
    for _ in range(app_db.DRIFT_MIN_REQUESTS - 1):
        add(app_db, days_ago=1, ai_decision='ESCALATED_UNKNOWN', final_decision='ESCALATED_UNKNOWN')

    report = admin.get(MONITORING).get_json()
    assert report['enough_data'] is False
    assert report['warnings'] == []


def test_a_week_full_of_categories_the_ai_has_never_seen_is_flagged(app_db, admin):
    steady_weeks(app_db)
    for _ in range(12):
        add(app_db, days_ago=1, ai_decision='ESCALATED_UNKNOWN', final_decision='ESCALATED_UNKNOWN')

    report = admin.get(MONITORING).get_json()
    assert report['enough_data'] is True
    assert warnings_of(report) == {'unknown_category', 'needed_person'}
    unknown = next(w for w in report['warnings'] if w['name'] == 'unknown_category')
    assert 'never seen' in unknown['message'] and '100%' in unknown['message']


def test_more_unusual_requests_and_a_moved_score_are_flagged(app_db, admin):
    steady_weeks(app_db)
    for _ in range(12):
        add(app_db, days_ago=1, ai_decision='ESCALATED_ANOMALY', final_decision='ESCALATED_ANOMALY', score=0.2)

    assert warnings_of(admin.get(MONITORING).get_json()) == {'unusual', 'needed_person', 'average_score'}


def test_a_sudden_rush_or_a_sudden_silence_is_flagged(app_db, admin):
    steady_weeks(app_db)
    for _ in range(40):
        add(app_db, days_ago=1)
    busy = admin.get(MONITORING).get_json()
    assert 'volume' in warnings_of(busy)
    assert 'well above the usual' in next(w for w in busy['warnings'] if w['name'] == 'volume')['message']

    # A week that suddenly goes quiet is worth saying out loud, even though it is too small to judge rates on.
    execute(app_db, 'DELETE FROM Requests WHERE created_at >= ?',
            ((datetime.utcnow() - timedelta(days=6)).strftime('%Y-%m-%d %H:%M:%S'),))
    for _ in range(2):
        add(app_db, days_ago=1)
    quiet = admin.get(MONITORING).get_json()
    assert quiet['enough_data'] is False
    assert warnings_of(quiet) == {'volume'}
    assert 'well below the usual' in quiet['warnings'][0]['message']


def test_an_ordinary_week_raises_nothing(app_db, admin):
    steady_weeks(app_db)
    for _ in range(12):
        add(app_db, days_ago=1)

    report = admin.get(MONITORING).get_json()
    assert (report['enough_data'], report['warnings']) == (True, [])


def test_the_weeks_belong_to_one_organization_and_only_admins_see_them(app_db, admin):
    other_org = create_organization(app_db, 'Other Co')
    create_user(app_db, 'admin@other.test', role='Admin', organization_id=other_org)
    create_user(app_db, 'staff@example.com')
    add(app_db, days_ago=1)
    add(app_db, days_ago=1, organization_id=other_org)
    add(app_db, days_ago=1, organization_id=other_org)

    assert admin.get(MONITORING).get_json()['latest']['requests'] == 1
    assert session(app_db, 'admin@other.test').get(MONITORING).get_json()['latest']['requests'] == 2
    assert session(app_db, 'staff@example.com').get(MONITORING).status_code == 403


def test_neuzem_sees_the_same_warnings_beside_an_organizations_quality(app_db):
    org = create_organization(app_db, 'Acme Pvt Ltd', approval_mode='shadow')
    steady_weeks(app_db, organization_id=org)
    for _ in range(12):
        add(app_db, days_ago=1, ai_decision='ESCALATED_UNKNOWN', final_decision='ESCALATED_UNKNOWN', organization_id=org)

    owner = session(app_db, create_platform_owner(app_db))
    report = owner.get('/api/platform/decision_quality', query_string={'organization_id': org}).get_json()
    assert {warning['name'] for warning in report['drift_warnings']} == {'unknown_category', 'needed_person'}
