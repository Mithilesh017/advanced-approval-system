from conftest import add_request, create_user, login, query

SCORE_FIELDS = {'xgb_score', 'iso_score', 'svm_score', 'risk_score'}
REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR',
}


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def test_submission_response_only_reveals_the_outcome(app_db):
    create_user(app_db, 'staff@example.com')
    body = session(app_db, 'staff@example.com').post('/api/predict', json=REQUEST).get_json()
    assert set(body) == {'status', 'message', 'normalized_inr'}
    assert body['status'] in {'APPROVED', 'PENDING_REVIEW'}


def test_employee_never_sees_scores_or_escalation_reasons(app_db):
    create_user(app_db, 'staff@example.com')
    employee = session(app_db, 'staff@example.com')

    response = employee.post('/api/predict', json=dict(REQUEST, Destination='Atlantis'))
    assert response.get_json()['status'] == 'PENDING_REVIEW'
    assert query(app_db, 'SELECT final_decision FROM Requests') == [{'final_decision': 'ESCALATED_UNKNOWN'}]

    [row] = employee.get('/api/auth/my_requests').get_json()
    assert row['final_decision'] == 'ESCALATED'
    assert not SCORE_FIELDS & set(row)


def test_admin_cannot_review_their_own_request(app_db):
    create_user(app_db, 'manager@example.com', role='Admin')
    org = app_db.DEFAULT_ORGANIZATION_ID
    pending = add_request(app_db, org, 'manager@example.com')
    decided = add_request(app_db, org, 'manager@example.com', 'APPROVED')
    admin = session(app_db, 'manager@example.com')

    assert admin.post('/api/auth/approve_request', json={'id': pending}).status_code == 403
    assert admin.post('/api/auth/reject_request', json={'id': pending}).status_code == 403
    assert admin.post('/api/auth/reopen_request', json={'id': decided}).status_code == 403


def test_decisions_only_apply_to_requests_in_the_right_state(app_db):
    create_user(app_db, 'staff@example.com')
    create_user(app_db, 'manager@example.com', role='Admin')
    org = app_db.DEFAULT_ORGANIZATION_ID
    pending = add_request(app_db, org, 'staff@example.com', 'ESCALATED_ANOMALY')
    decided = add_request(app_db, org, 'staff@example.com', 'APPROVED')
    admin = session(app_db, 'manager@example.com')

    assert admin.post('/api/auth/reopen_request', json={'id': pending}).status_code == 409
    assert admin.post('/api/auth/approve_request', json={'id': decided}).status_code == 409
    assert admin.post('/api/auth/reject_request', json={'id': decided}).status_code == 409


def test_changing_a_decision_goes_through_pending(app_db):
    create_user(app_db, 'staff@example.com')
    create_user(app_db, 'manager@example.com', role='Admin')
    decided = add_request(app_db, app_db.DEFAULT_ORGANIZATION_ID, 'staff@example.com', 'APPROVED')
    admin = session(app_db, 'manager@example.com')

    def state():
        [row] = query(app_db, 'SELECT final_decision, reviewed_by FROM Requests WHERE id = ?', (decided,))
        return row

    assert admin.post('/api/auth/reopen_request', json={'id': decided}).status_code == 200
    assert state() == {'final_decision': 'ESCALATED_MANUAL_REVIEW', 'reviewed_by': None}

    assert admin.post('/api/auth/reject_request', json={'id': decided}).status_code == 200
    assert state() == {'final_decision': 'REJECTED', 'reviewed_by': 'manager@example.com'}
