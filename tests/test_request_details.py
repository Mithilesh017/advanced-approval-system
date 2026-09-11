"""Every request carries a business purpose and its dates, which approvers and the employee can see."""
from datetime import date, timedelta

import pytest

from conftest import REQUEST_DETAILS, create_user, login, query

REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR', **REQUEST_DETAILS,
}
TODAY = date.today()


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


@pytest.fixture
def staff(app_db):
    create_user(app_db, 'staff@example.com')
    return session(app_db, 'staff@example.com')


def test_purpose_and_dates_are_saved_and_shown(app_db, staff):
    start, end = TODAY.isoformat(), (TODAY + timedelta(days=2)).isoformat()
    body = {**REQUEST, 'Purpose': '  Three-day client workshop in Mumbai  ', 'Expense_Date': start, 'End_Date': end}
    assert staff.post('/api/predict', json=body).status_code == 200

    expected = {'purpose': 'Three-day client workshop in Mumbai', 'expense_date': start, 'end_date': end}
    assert query(app_db, 'SELECT purpose, expense_date, end_date FROM Requests') == [expected]

    [own] = staff.get('/api/auth/my_requests').get_json()
    assert {key: own[key] for key in expected} == expected

    create_user(app_db, 'manager@example.com', role='Admin')
    [listed] = session(app_db, 'manager@example.com').get('/api/auth/all_requests').get_json()
    assert {key: listed[key] for key in expected} == expected


def test_end_date_is_optional(app_db, staff):
    assert staff.post('/api/predict', json=REQUEST).status_code == 200
    assert query(app_db, 'SELECT end_date FROM Requests') == [{'end_date': None}]


@pytest.mark.parametrize('changes', [
    {'Purpose': None},
    {'Purpose': ''},
    {'Purpose': 'Too short'},
    {'Purpose': 'x' * 501},
    {'Purpose': ['Client', 'workshop', 'in Mumbai']},
    {'Expense_Date': None},
    {'Expense_Date': '12/09/2026'},
    {'Expense_Date': '2026-02-30'},
    {'Expense_Date': (TODAY - timedelta(days=400)).isoformat()},
    {'Expense_Date': (TODAY + timedelta(days=400)).isoformat()},
    {'End_Date': (TODAY - timedelta(days=1)).isoformat()},
    {'End_Date': (TODAY + timedelta(days=91)).isoformat()},
    {'End_Date': 'next week'},
])
def test_missing_or_invalid_details_are_refused(app_db, staff, changes):
    response = staff.post('/api/predict', json={**REQUEST, **changes})
    assert response.status_code == 400
    assert response.get_json()['error']
    assert query(app_db, 'SELECT id FROM Requests') == []
