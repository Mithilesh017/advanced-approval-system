"""When a company leaves, everything that names a person goes and the anonymous decisions stay for the AI."""
import json

import pytest

from conftest import (
    SUPER_ADMIN, create_organization, create_platform_owner, create_user, execute, login, query,
)

CLOSE = '/api/platform/close_organization'
UPDATE = '/api/platform/update_organization'


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def add_request(app_db, organization_id, final_decision='APPROVED', reviewed_by='boss@acme.test'):
    rows = execute(
        app_db,
        'INSERT INTO Requests (role, department, request_type, destination, amount, currency, normalized_amount, '
        'xgb_score, ai_decision, final_decision, reviewed_by, submitted_by, employee_name, employee_id, purpose, '
        'approver_email, organization_id) '
        "VALUES ('Junior Developer', 'Engineering', 'Hotel Booking', 'Mumbai', 5000, 'INR', 5000, 0.9, 'APPROVED', ?, "
        "?, 'staff@acme.test', 'Asha Rao', 'E-100', 'Client workshop in Mumbai', 'lead@acme.test', ?) RETURNING id",
        (final_decision, reviewed_by, organization_id)
    )
    return rows[0][0]


@pytest.fixture
def owner(app_db):
    return session(app_db, create_platform_owner(app_db))


@pytest.fixture
def acme(app_db):
    organization_id = create_organization(app_db, 'Acme Pvt Ltd')
    execute(app_db, 'UPDATE Organizations SET allow_training_data = 1 WHERE id = ?', (organization_id,))
    create_user(app_db, 'boss@acme.test', role='SuperAdmin', organization_id=organization_id)
    create_user(app_db, 'staff@acme.test', organization_id=organization_id)
    decided = add_request(app_db, organization_id)
    automatic = add_request(app_db, organization_id, reviewed_by=None)
    execute(
        app_db, 'INSERT INTO Receipts (organization_id, request_id, filename, content_type, size, sha256, content, uploaded_by) '
        "VALUES (?, ?, 'hotel.pdf', 'application/pdf', 5, 'abc', ?, 'staff@acme.test')",
        (organization_id, decided, b'%PDF-')
    )
    execute(
        app_db, "INSERT INTO SpotChecks (organization_id, request_id, verdict, reviewed_by, comment) "
        "VALUES (?, ?, 'WRONG', 'boss@acme.test', 'No receipt for a hotel stay')",
        (organization_id, automatic)
    )
    execute(
        app_db, "INSERT INTO PolicyRules (organization_id, rule_type, name, config) "
        "VALUES (?, 'always_review', 'Everything', '{}')",
        (organization_id,)
    )
    execute(
        app_db, "INSERT INTO AuditEvents (organization_id, request_id, actor_email, action, comment) "
        "VALUES (?, ?, 'boss@acme.test', 'request.approved', 'Fine by me')",
        (organization_id, decided)
    )
    return {'id': organization_id, 'decided': decided, 'automatic': automatic}


def close(owner, acme, name='Acme Pvt Ltd'):
    return owner.post(CLOSE, json={'id': acme['id'], 'name': name})


def test_closing_removes_everything_that_names_a_person(app_db, owner, acme):
    response = close(owner, acme)
    assert response.status_code == 200
    assert response.get_json()['anonymous_requests_kept'] == 2

    assert query(app_db, 'SELECT id FROM Users WHERE organization_id = ?', (acme['id'],)) == []
    assert query(app_db, 'SELECT id FROM Receipts WHERE organization_id = ?', (acme['id'],)) == []
    assert query(app_db, 'SELECT id FROM PolicyRules WHERE organization_id = ?', (acme['id'],)) == []

    requests = query(
        app_db, 'SELECT submitted_by, employee_name, employee_id, purpose, approver_email, reviewed_by, '
        'role, department, normalized_amount, final_decision FROM Requests WHERE organization_id = ? ORDER BY id',
        (acme['id'],)
    )
    assert [row['employee_name'] or row['employee_id'] or row['purpose'] or row['approver_email'] for row in requests] == [None, None]
    assert [row['submitted_by'] for row in requests] == ['closed', 'closed']
    # The decision itself is untouched: this is what the AI learns from.
    assert [(row['role'], row['department'], row['normalized_amount'], row['final_decision']) for row in requests] == [
        ('Junior Developer', 'Engineering', 5000, 'APPROVED'), ('Junior Developer', 'Engineering', 5000, 'APPROVED')
    ]
    assert query(app_db, 'SELECT verdict, reviewed_by, comment FROM SpotChecks WHERE organization_id = ?', (acme['id'],)) == [
        {'verdict': 'WRONG', 'reviewed_by': 'closed', 'comment': None}
    ]


def test_the_history_goes_and_one_line_says_why(app_db, owner, acme):
    close(owner, acme)

    events = query(app_db, 'SELECT action, actor_email, details FROM AuditEvents WHERE organization_id = ?', (acme['id'],))
    assert [event['action'] for event in events] == ['organization.closed']
    assert events[0]['actor_email'] == 'founder@neuzem.test'
    assert json.loads(events[0]['details']) == {
        'accounts_removed': 2, 'receipts_removed': 1, 'rules_removed': 1,
        'history_entries_removed': 1, 'anonymous_requests_kept': 2,
    }


def test_the_history_cannot_be_touched_again_afterwards(app_db, owner, acme):
    close(owner, acme)

    with pytest.raises(Exception):
        execute(app_db, "UPDATE AuditEvents SET comment = 'changed' WHERE organization_id = ?", (acme['id'],))
    with pytest.raises(Exception):
        execute(app_db, 'DELETE FROM AuditEvents WHERE organization_id = ?', (acme['id'],))


def test_what_is_left_still_teaches_the_ai(app_db, owner, acme):
    close(owner, acme)

    conn = app_db.get_db_connection()
    try:
        decisions = {row['id']: row['final_decision'] for row in app_db.trainable_decisions(conn)}
    finally:
        conn.close()

    # The one a person decided, and the automatic one a spot check said was wrong.
    assert decisions == {acme['decided']: 'APPROVED', acme['automatic']: 'REJECTED'}


def test_a_company_that_never_shared_is_still_not_learned_from(app_db, owner):
    private_id = create_organization(app_db, 'Private Co')
    create_user(app_db, 'boss@private.test', role='SuperAdmin', organization_id=private_id)
    add_request(app_db, private_id, reviewed_by='boss@private.test')

    assert owner.post(CLOSE, json={'id': private_id, 'name': 'Private Co'}).status_code == 200
    conn = app_db.get_db_connection()
    try:
        assert app_db.trainable_decisions(conn) == []
    finally:
        conn.close()


def test_closing_needs_the_name_typed_and_cannot_be_repeated(app_db, owner, acme):
    assert close(owner, acme, name='acme pvt ltd').status_code == 400
    assert close(owner, acme, name='').status_code == 400
    assert owner.post(CLOSE, json={'id': acme['id']}).status_code == 400
    assert query(app_db, 'SELECT status FROM Organizations WHERE id = ?', (acme['id'],)) == [{'status': 'Active'}]

    assert close(owner, acme).status_code == 200
    assert query(app_db, 'SELECT status FROM Organizations WHERE id = ?', (acme['id'],)) == [{'status': 'Closed'}]
    assert close(owner, acme).status_code == 409


def test_the_default_organization_and_strangers_are_refused(app_db, owner, acme):
    assert owner.post(CLOSE, json={'id': app_db.DEFAULT_ORGANIZATION_ID, 'name': 'Default Organization'}).status_code == 409
    assert owner.post(CLOSE, json={'id': 9999, 'name': 'Nobody'}).status_code == 404
    assert session(app_db, SUPER_ADMIN['email']).post(CLOSE, json={'id': acme['id'], 'name': 'Acme Pvt Ltd'}).status_code == 403
    assert query(app_db, 'SELECT id FROM Users WHERE organization_id = ?', (acme['id'],)) != []


def test_the_old_join_link_stops_working(app_db, owner, acme):
    [before] = query(app_db, 'SELECT join_code FROM Organizations WHERE id = ?', (acme['id'],))
    close(owner, acme)
    [after] = query(app_db, 'SELECT join_code FROM Organizations WHERE id = ?', (acme['id'],))

    assert after['join_code'] != before['join_code']
    assert app_db.app.test_client().get(f"/api/auth/join_info?code={before['join_code']}").status_code == 404


@pytest.mark.parametrize('change', [
    {'status': 'Active'},
    {'status': 'Paused'},
    {'approval_mode': 'shadow'},
    {'approval_mode': 'automatic', 'force': True},
    {'allow_training_data': True},
])
def test_a_closed_organization_cannot_be_reopened_or_changed(app_db, owner, acme, change):
    execute(app_db, 'UPDATE Organizations SET allow_training_data = 0 WHERE id = ?', (acme['id'],))
    close(owner, acme)
    settings = 'SELECT status, approval_mode, allow_training_data, join_code FROM Organizations WHERE id = ?'
    before = query(app_db, settings, (acme['id'],))

    assert owner.post(UPDATE, json={'id': acme['id'], **change}).status_code == 409
    assert query(app_db, settings, (acme['id'],)) == before


def test_a_closed_organization_can_still_stop_sharing_its_data(app_db, owner, acme):
    close(owner, acme)

    assert owner.post(UPDATE, json={'id': acme['id'], 'allow_training_data': False}).status_code == 200
    assert query(app_db, 'SELECT allow_training_data FROM Organizations WHERE id = ?', (acme['id'],)) == [{'allow_training_data': 0}]
