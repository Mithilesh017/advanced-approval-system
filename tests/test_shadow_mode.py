"""In shadow mode the AI only recommends: every request waits for a person, and the AI's recommendation is kept."""
import json

import pytest

from conftest import create_organization, create_platform_owner, create_user, execute, login, query, use_model_score

REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR',
}


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def submit(client):
    response = client.post('/api/predict', json=REQUEST)
    assert response.status_code == 200, response.get_json()
    return response.get_json()['status']


def latest_request(app_db):
    [row] = query(app_db, 'SELECT id, final_decision, ai_decision, approval_mode FROM Requests ORDER BY id DESC LIMIT 1')
    return row


@pytest.fixture
def pilot(app_db):
    org_id = create_organization(app_db, 'Pilot Co', approval_mode='shadow')
    create_user(app_db, 'staff@pilot.test', organization_id=org_id)
    create_user(app_db, 'manager@pilot.test', role='Admin', organization_id=org_id)
    return {'org': org_id, 'staff': session(app_db, 'staff@pilot.test')}


def test_shadow_mode_sends_requests_the_ai_would_approve_to_a_person(app_db, monkeypatch, pilot):
    use_model_score(app_db, monkeypatch, 0.95)
    assert submit(pilot['staff']) == 'PENDING_REVIEW'

    saved = latest_request(app_db)
    assert (saved['final_decision'], saved['ai_decision'], saved['approval_mode']) == ('ESCALATED_SHADOW', 'APPROVED', 'shadow')
    [event] = query(app_db, "SELECT details FROM AuditEvents WHERE action = 'request.submitted'")
    details = json.loads(event['details'])
    assert (details['approval_mode'], details['ai_recommendation']) == ('shadow', 'APPROVED')

    # Employees only learn that the request is waiting, never what the AI recommended.
    [own] = pilot['staff'].get('/api/auth/my_requests').get_json()
    assert own['final_decision'] == 'ESCALATED'
    assert 'ai_decision' not in own and 'approval_mode' not in own


def test_shadow_mode_keeps_the_ai_escalation_reason(app_db, monkeypatch, pilot):
    use_model_score(app_db, monkeypatch, 0.5)
    assert submit(pilot['staff']) == 'PENDING_REVIEW'
    saved = latest_request(app_db)
    assert (saved['final_decision'], saved['ai_decision'], saved['approval_mode']) == (
        'ESCALATED_MANUAL_REVIEW', 'ESCALATED_MANUAL_REVIEW', 'shadow'
    )


def test_automatic_mode_still_approves_confident_requests(app_db, monkeypatch):
    use_model_score(app_db, monkeypatch, 0.95)
    create_user(app_db, 'staff@example.com')
    assert submit(session(app_db, 'staff@example.com')) == 'APPROVED'
    saved = latest_request(app_db)
    assert (saved['final_decision'], saved['ai_decision'], saved['approval_mode']) == ('APPROVED', 'APPROVED', 'automatic')


def test_a_person_decides_shadow_requests_and_the_ai_recommendation_is_kept(app_db, monkeypatch, pilot):
    use_model_score(app_db, monkeypatch, 0.95)
    submit(pilot['staff'])
    request_id = latest_request(app_db)['id']
    manager = session(app_db, 'manager@pilot.test')

    [listed] = manager.get('/api/auth/all_requests').get_json()
    assert (listed['final_decision'], listed['ai_decision'], listed['approval_mode']) == ('ESCALATED_SHADOW', 'APPROVED', 'shadow')

    assert manager.post('/api/auth/reject_request', json={'id': request_id, 'comment': 'Trip was cancelled'}).status_code == 200
    assert query(app_db, 'SELECT final_decision, ai_decision, reviewed_by FROM Requests WHERE id = ?', (request_id,)) == [
        {'final_decision': 'REJECTED', 'ai_decision': 'APPROVED', 'reviewed_by': 'manager@pilot.test'}
    ]


def test_an_unrecognized_approval_mode_never_auto_approves(app_db, monkeypatch):
    use_model_score(app_db, monkeypatch, 0.95)
    execute(app_db, "UPDATE Organizations SET approval_mode = 'unexpected' WHERE id = ?", (app_db.DEFAULT_ORGANIZATION_ID,))
    create_user(app_db, 'staff@example.com')

    assert submit(session(app_db, 'staff@example.com')) == 'PENDING_REVIEW'
    saved = latest_request(app_db)
    assert (saved['final_decision'], saved['approval_mode']) == ('ESCALATED_SHADOW', 'shadow')


def test_switching_to_automatic_applies_to_the_next_request(app_db, monkeypatch, pilot):
    use_model_score(app_db, monkeypatch, 0.95)
    assert submit(pilot['staff']) == 'PENDING_REVIEW'

    owner = session(app_db, create_platform_owner(app_db))
    assert owner.post('/api/platform/update_organization', json={'id': pilot['org'], 'approval_mode': 'automatic'}).status_code == 200
    assert submit(pilot['staff']) == 'APPROVED'
