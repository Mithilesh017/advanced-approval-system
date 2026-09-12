"""A spot-check verdict is a person's answer about an approval nobody saw, so the model learns from it too."""
import pytest

from conftest import create_organization, create_platform_owner, execute, login, query


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def add_request(app_db, organization_id, final_decision='APPROVED', reviewed_by=None, amount=5000):
    rows = execute(
        app_db,
        'INSERT INTO Requests (role, department, request_type, destination, amount, currency, normalized_amount, '
        'xgb_score, ai_decision, final_decision, reviewed_by, submitted_by, organization_id) '
        "VALUES ('Junior Developer', 'Engineering', 'Hotel Booking', 'Mumbai', ?, 'INR', ?, 0.9, 'APPROVED', ?, ?, "
        "'staff@example.com', ?) RETURNING id",
        (amount, amount, final_decision, reviewed_by, organization_id)
    )
    return rows[0][0]


def spot_check(app_db, organization_id, request_id, verdict=None):
    execute(
        app_db, 'INSERT INTO SpotChecks (organization_id, request_id, verdict) VALUES (?, ?, ?)',
        (organization_id, request_id, verdict)
    )


def learned(app_db):
    conn = app_db.get_db_connection()
    try:
        return {row['id']: row['final_decision'] for row in app_db.trainable_decisions(conn)}
    finally:
        conn.close()


@pytest.fixture
def sharing_org(app_db):
    return app_db.DEFAULT_ORGANIZATION_ID  # The default organization shares its data.


def test_a_checked_approval_becomes_a_training_row(app_db, sharing_org):
    right = add_request(app_db, sharing_org)
    wrong = add_request(app_db, sharing_org)
    waiting = add_request(app_db, sharing_org)
    spot_check(app_db, sharing_org, right, 'CORRECT')
    spot_check(app_db, sharing_org, wrong, 'WRONG')
    spot_check(app_db, sharing_org, waiting)  # Nobody has answered this one yet.

    assert learned(app_db) == {right: 'APPROVED', wrong: 'REJECTED'}


def test_an_approval_nobody_checked_teaches_nothing(app_db, sharing_org):
    add_request(app_db, sharing_org)
    assert learned(app_db) == {}


def test_a_persons_own_decision_wins_over_an_older_spot_check(app_db, sharing_org):
    request_id = add_request(app_db, sharing_org, final_decision='REJECTED', reviewed_by='manager@example.com')
    spot_check(app_db, sharing_org, request_id, 'CORRECT')

    # The request is counted once, as the person decided it, not twice with two different answers.
    assert learned(app_db) == {request_id: 'REJECTED'}


def test_spot_checks_from_an_organization_that_shares_nothing_are_left_out(app_db):
    private_org = create_organization(app_db, 'Private Co')
    execute(app_db, 'UPDATE Organizations SET allow_training_data = 0 WHERE id = ?', (private_org,))
    request_id = add_request(app_db, private_org)
    spot_check(app_db, private_org, request_id, 'WRONG')

    assert learned(app_db) == {}


def test_a_wrong_verdict_teaches_the_model_to_refuse_that_request(app_db, sharing_org):
    import model_pipeline

    wrong = add_request(app_db, sharing_org, amount=90000)
    spot_check(app_db, sharing_org, wrong, 'WRONG')
    conn = app_db.get_db_connection()
    try:
        frame = model_pipeline.feedback_training_data(app_db.trainable_decisions(conn))
    finally:
        conn.close()

    assert frame['row_key'].tolist() == [f'request:{wrong}']
    assert frame['label'].tolist() == [0]
    assert frame['source'].tolist() == ['feedback']


def test_the_model_console_counts_what_a_retrain_would_learn_from(app_db, sharing_org):
    owner = session(app_db, create_platform_owner(app_db))
    assert owner.get('/api/platform/model/info').get_json()['new_decisions_since_training'] == 0

    add_request(app_db, sharing_org, final_decision='APPROVED', reviewed_by='manager@example.com')
    checked = add_request(app_db, sharing_org)
    spot_check(app_db, sharing_org, checked, 'CORRECT')
    unanswered = add_request(app_db, sharing_org)
    spot_check(app_db, sharing_org, unanswered)

    assert owner.get('/api/platform/model/info').get_json()['new_decisions_since_training'] == 2
    assert len(query(app_db, 'SELECT id FROM SpotChecks')) == 2
