"""Every request event, access decision and platform action is recorded, and the record can never be edited."""
import json

import pytest

from conftest import REQUEST_DETAILS, SUPER_ADMIN, add_request, create_organization, create_platform_owner, create_user, login, query

REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR', **REQUEST_DETAILS,
}
HISTORY = '/api/auth/request_history'


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def events(app_db, **filters):
    where = ' AND '.join(f'{column} = ?' for column in filters) or '1 = 1'
    rows = query(app_db, f'SELECT * FROM AuditEvents WHERE {where} ORDER BY id', tuple(filters.values()))
    for row in rows:
        row['details'] = json.loads(row['details']) if row['details'] else None
    return rows


@pytest.fixture
def team(app_db):
    create_user(app_db, 'staff@example.com')
    create_user(app_db, 'manager@example.com', role='Admin')
    return {'staff': session(app_db, 'staff@example.com'), 'manager': session(app_db, 'manager@example.com')}


def test_submission_records_score_explanation_and_model_version(app_db, team):
    assert team['staff'].post('/api/predict', json=REQUEST).status_code == 200
    [saved] = query(app_db, 'SELECT id, final_decision FROM Requests')

    [event] = events(app_db)
    assert (event['action'], event['request_id'], event['actor_email'], event['to_status'], event['organization_id']) == (
        'request.submitted', saved['id'], 'staff@example.com', saved['final_decision'], app_db.DEFAULT_ORGANIZATION_ID
    )
    details = event['details']
    assert details['model_version'] is None
    assert 0 <= details['approval_score'] <= 1
    assert details['unrecognized_category'] is False
    assert set(details['anomaly_detectors']) == {'isolation_forest', 'one_class_svm'}
    assert set(details['explanation']) == set(app_db.model_pipeline.FEATURES)
    assert details['thresholds'] == {
        'auto_approve_above': app_db.AUTO_APPROVE_THRESHOLD, 'escalate_below': app_db.ESCALATE_THRESHOLD,
        'second_approval_above': None,
    }


def test_decisions_record_who_what_and_why(app_db, team):
    request_id = add_request(app_db, app_db.DEFAULT_ORGANIZATION_ID, 'staff@example.com', 'ESCALATED_ANOMALY')
    manager = team['manager']
    assert manager.post('/api/auth/approve_request', json={'id': request_id}).status_code == 200
    assert manager.post('/api/auth/reopen_request', json={'id': request_id, 'comment': '  Receipt missing  '}).status_code == 200
    assert manager.post('/api/auth/reject_request', json={'id': request_id, 'comment': 'Over the hotel limit'}).status_code == 200

    assert [(e['action'], e['actor_email'], e['from_status'], e['to_status'], e['comment']) for e in events(app_db, request_id=request_id)] == [
        ('request.approved', 'manager@example.com', 'ESCALATED_ANOMALY', 'APPROVED', None),
        ('request.reopened', 'manager@example.com', 'APPROVED', 'ESCALATED_MANUAL_REVIEW', 'Receipt missing'),
        ('request.rejected', 'manager@example.com', 'ESCALATED_MANUAL_REVIEW', 'REJECTED', 'Over the hotel limit'),
    ]


@pytest.mark.parametrize('path, starting_status', [
    ('/api/auth/reject_request', 'ESCALATED_POLICY'),
    ('/api/auth/reopen_request', 'APPROVED'),
])
@pytest.mark.parametrize('comment', [None, '', '   '])
def test_rejecting_or_reopening_needs_a_reason(app_db, team, path, starting_status, comment):
    request_id = add_request(app_db, app_db.DEFAULT_ORGANIZATION_ID, 'staff@example.com', starting_status)
    body = {'id': request_id} if comment is None else {'id': request_id, 'comment': comment}

    assert team['manager'].post(path, json=body).status_code == 400
    assert query(app_db, 'SELECT final_decision FROM Requests WHERE id = ?', (request_id,)) == [{'final_decision': starting_status}]
    assert events(app_db) == []


def test_comment_must_be_text(app_db, team):
    request_id = add_request(app_db, app_db.DEFAULT_ORGANIZATION_ID, 'staff@example.com')
    response = team['manager'].post('/api/auth/reject_request', json={'id': request_id, 'comment': ['not', 'text']})
    assert response.status_code == 400
    assert events(app_db) == []


def test_refused_decisions_record_nothing(app_db, team):
    other_org = create_organization(app_db, 'Other Co')
    create_user(app_db, 'outsider@other.test', organization_id=other_org)
    foreign = add_request(app_db, other_org, 'outsider@other.test')
    own = add_request(app_db, app_db.DEFAULT_ORGANIZATION_ID, 'manager@example.com')
    decided = add_request(app_db, app_db.DEFAULT_ORGANIZATION_ID, 'staff@example.com', 'APPROVED')
    manager = team['manager']

    assert manager.post('/api/auth/reject_request', json={'id': foreign, 'comment': 'No'}).status_code == 404
    assert manager.post('/api/auth/reject_request', json={'id': own, 'comment': 'No'}).status_code == 403
    assert manager.post('/api/auth/approve_request', json={'id': decided}).status_code == 409
    assert events(app_db) == []


def test_admins_can_read_a_requests_history(app_db, team):
    request_id = add_request(app_db, app_db.DEFAULT_ORGANIZATION_ID, 'staff@example.com')
    assert team['manager'].post('/api/auth/approve_request', json={'id': request_id, 'comment': 'Within policy'}).status_code == 200

    response = team['manager'].get(HISTORY, query_string={'id': request_id})
    assert response.status_code == 200
    [event] = response.get_json()['events']
    assert (event['action'], event['actor_email'], event['comment'], event['to_status']) == (
        'request.approved', 'manager@example.com', 'Within policy', 'APPROVED'
    )
    assert event['created_at'].endswith('Z')


def test_request_history_is_only_for_that_organizations_admins(app_db, team):
    other_org = create_organization(app_db, 'Other Co')
    create_user(app_db, 'admin@other.test', role='Admin', organization_id=other_org)
    request_id = add_request(app_db, app_db.DEFAULT_ORGANIZATION_ID, 'staff@example.com')

    assert session(app_db, 'admin@other.test').get(HISTORY, query_string={'id': request_id}).status_code == 404
    assert team['staff'].get(HISTORY, query_string={'id': request_id}).status_code == 403
    assert team['manager'].get(HISTORY).status_code == 400


@pytest.mark.parametrize('statement', ["UPDATE AuditEvents SET comment = 'Edited'", 'DELETE FROM AuditEvents'])
def test_audit_events_cannot_be_changed_or_deleted(app_db, team, statement):
    request_id = add_request(app_db, app_db.DEFAULT_ORGANIZATION_ID, 'staff@example.com')
    assert team['manager'].post('/api/auth/approve_request', json={'id': request_id, 'comment': 'Original note'}).status_code == 200
    app_db.setup_database()  # Running the startup migrations again must keep the protection in place.

    conn = app_db.get_db_connection()
    try:
        with pytest.raises(Exception, match='cannot be changed or deleted'):
            conn.execute(statement)
            conn.commit()
    finally:
        conn.close()
    assert [e['comment'] for e in events(app_db)] == ['Original note']


def test_access_decisions_are_recorded(app_db):
    create_user(app_db, 'hire@example.com', status='Pending')
    create_user(app_db, 'declined@example.com', status='Pending')
    create_user(app_db, 'leaver@example.com')
    admin = session(app_db, SUPER_ADMIN['email'])

    assert admin.post('/api/auth/approve_user', json={'email': 'hire@example.com'}).status_code == 200
    assert admin.post('/api/auth/reject_user', json={'email': 'declined@example.com'}).status_code == 200
    assert admin.post('/api/auth/delete_user', json={'email': 'leaver@example.com'}).status_code == 200

    recorded = events(app_db)
    assert [(e['action'], e['actor_email'], e['details']) for e in recorded] == [
        ('user.approved', SUPER_ADMIN['email'], {'email': 'hire@example.com', 'role': 'User'}),
        ('user.rejected', SUPER_ADMIN['email'], {'email': 'declined@example.com', 'role': 'User'}),
        ('user.deleted', SUPER_ADMIN['email'], {'email': 'leaver@example.com', 'role': 'User'}),
    ]
    assert {e['organization_id'] for e in recorded} == {app_db.DEFAULT_ORGANIZATION_ID}


def test_platform_actions_are_recorded(app_db, monkeypatch):
    monkeypatch.setattr(app_db.email_service, 'sendOrganizationCreatedEmail', lambda *args: None)
    monkeypatch.setattr(app_db, 'run_training_job', lambda job_id, started_by: None)
    owner = session(app_db, create_platform_owner(app_db))

    created = owner.post('/api/platform/create_organization', json={'name': 'Acme Pvt Ltd', 'super_admin_email': 'boss@acme.test'})
    assert created.status_code == 201
    organization_id = created.get_json()['organization_id']
    assert owner.post('/api/platform/update_organization', json={'id': organization_id, 'status': 'Paused'}).status_code == 200
    assert owner.post('/api/platform/model/activate', json={'version_id': None}).status_code == 200
    assert owner.post('/api/platform/model/retrain').status_code == 202

    recorded = events(app_db)
    assert [e['action'] for e in recorded] == ['organization.created', 'organization.updated', 'model.activated', 'model.retrain_started']
    assert {e['actor_email'] for e in recorded} == {'founder@neuzem.test'}
    assert recorded[0]['details'] == {
        'name': 'Acme Pvt Ltd', 'super_admin_email': 'boss@acme.test', 'allow_training_data': False, 'approval_mode': 'shadow',
    }
    assert recorded[1]['details'] == {'changes': {'status': 'Paused'}}
    assert (recorded[2]['organization_id'], recorded[2]['details']) == (None, {'version_id': None})
