"""The AI judges an amount against what is normal for the company that sent it, not against a fixed number."""
import json

import numpy as np
import pandas as pd
import pytest

import model_pipeline
from conftest import REQUEST_DETAILS, create_organization, create_user, execute, login, query

REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR', **REQUEST_DETAILS,
}


def decision(request_id, organization_id, amount, final_decision='APPROVED'):
    return {
        'id': request_id, 'organization_id': organization_id, 'role': 'Junior Developer', 'department': 'Engineering',
        'request_type': 'Hotel Booking', 'destination': 'Mumbai', 'normalized_amount': amount,
        'final_decision': final_decision,
    }


def add_request(app_db, organization_id, amount):
    execute(
        app_db,
        'INSERT INTO Requests (role, department, request_type, destination, amount, currency, normalized_amount, '
        'xgb_score, final_decision, submitted_by, organization_id) '
        "VALUES ('Junior Developer', 'Engineering', 'Hotel Booking', 'Mumbai', ?, 'INR', ?, 0.9, 'APPROVED', "
        "'staff@example.com', ?)",
        (amount, amount, organization_id)
    )


@pytest.mark.parametrize('amounts, expected', [
    ([1000, 2000, 3000], 2000),
    ([1000, 2000, 3000, 9000], 2500),
    ([None, 0, 4000], 4000),
    ([], None),
])
def test_the_normal_amount_is_the_middle_one(amounts, expected):
    assert model_pipeline.typical_amount(amounts) == expected


@pytest.mark.parametrize('amount, typical, expected', [
    (5000, 5000, 1.0),
    (15000, 5000, 3.0),
    (500, 5000, 0.1),
    (5_000_000, 5000, model_pipeline.AMOUNT_RATIO_CAP),  # One runaway request cannot swamp the column.
    (5000, None, 1.0),
    (5000, 0, 1.0),
])
def test_a_request_is_measured_against_that_normal(amount, typical, expected):
    assert model_pipeline.amount_ratio(amount, typical) == expected


def test_two_companies_of_different_sizes_get_the_same_ratio():
    small = [decision(index, 1, 2000) for index in range(1, 6)]
    large = [decision(index, 2, 200000) for index in range(6, 11)]
    # One request in each company, twice their own normal.
    small.append(decision(11, 1, 4000))
    large.append(decision(12, 2, 400000))

    frame = model_pipeline.feedback_training_data(small + large)
    assert frame.loc[frame['row_key'] == 'request:11', 'Amount_Ratio'].iloc[0] == 2.0
    assert frame.loc[frame['row_key'] == 'request:12', 'Amount_Ratio'].iloc[0] == 2.0
    # Without the ratio the two look nothing alike.
    assert frame.loc[frame['row_key'] == 'request:11', 'Amount_INR'].iloc[0] == 4000


def test_a_company_with_too_few_requests_is_measured_against_everybody():
    established = [decision(index, 1, 2000) for index in range(1, 6)]
    newcomer = [decision(6, 2, 4000)]

    frame = model_pipeline.feedback_training_data(established + newcomer)
    assert frame.loc[frame['row_key'] == 'request:6', 'Amount_Ratio'].iloc[0] == 2.0


def test_training_data_saved_before_the_ratio_existed_still_loads(app_db):
    stored = model_pipeline.pack_training_data(pd.DataFrame({
        'row_key': ['a', 'b', 'c'], 'source': ['corporate'] * 3, 'Role': ['Junior Developer'] * 3,
        'Department': ['Engineering'] * 3, 'Request_Type': ['Hotel Booking'] * 3, 'Destination': ['Mumbai'] * 3,
        'Amount_INR': [1000.0, 2000.0, 6000.0], 'label': [1, 1, 0],
    }))
    assert 'Amount_Ratio' not in stored

    frame = model_pipeline.unpack_training_data(stored)
    assert list(frame.columns) == model_pipeline.TRAINING_COLUMNS
    assert frame['Amount_Ratio'].tolist() == [0.5, 1.0, 3.0]


def test_a_retrained_model_uses_the_ratio_and_an_older_one_does_not(app_db):
    base = model_pipeline.base_training_data(None, 'combined_corporate_approval_data.csv').head(400)
    artifacts, _ = model_pipeline.train_ensemble(base)

    assert artifacts['features'] == model_pipeline.FEATURES
    assert 'Amount_Ratio' in artifacts['features']
    assert artifacts['base_typical_amount'] > 0

    encoded, _ = model_pipeline.prepare_request(artifacts, {
        'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
        'Destination': 'Mumbai', 'Amount_INR': 5000.0, 'typical_amount': 2500.0,
    })
    assert list(encoded.columns) == artifacts['features']
    assert np.isfinite(artifacts['xgboost_model'].predict_proba(encoded)[0][1])

    bundled = app_db.load_bundled_artifacts()
    assert 'Amount_Ratio' not in bundled['features']  # The model in production keeps the columns it was trained on.


def test_the_normal_is_read_from_that_organizations_own_recent_requests(app_db):
    other_org = create_organization(app_db, 'Other Co')
    conn = app_db.get_db_connection()
    try:
        assert app_db.organization_typical_amount(conn, app_db.DEFAULT_ORGANIZATION_ID) is None

        for amount in (1000, 2000, 3000, 4000):
            add_request(app_db, app_db.DEFAULT_ORGANIZATION_ID, amount)
        assert app_db.organization_typical_amount(conn, app_db.DEFAULT_ORGANIZATION_ID) is None  # Still too few.

        add_request(app_db, app_db.DEFAULT_ORGANIZATION_ID, 5000)
        for amount in (100000, 200000, 300000, 400000, 500000):
            add_request(app_db, other_org, amount)

        assert app_db.organization_typical_amount(conn, app_db.DEFAULT_ORGANIZATION_ID) == 3000
        assert app_db.organization_typical_amount(conn, other_org) == 300000
    finally:
        conn.close()


def test_submitting_a_request_still_works_while_a_company_is_new(app_db):
    create_user(app_db, 'staff@example.com')
    client = app_db.app.test_client()
    login(client, 'staff@example.com')

    assert client.post('/api/predict', json=REQUEST).status_code == 200
    assert len(query(app_db, 'SELECT id FROM Requests')) == 1


def start_training(app_db, monkeypatch, rows=600):
    """Retrains on a small slice of the base data so the test stays quick."""
    small_base = model_pipeline.base_training_data(None, 'combined_corporate_approval_data.csv').head(rows)
    monkeypatch.setattr(app_db.model_pipeline, 'base_training_data', lambda current, csv_path=None: small_base)
    job_id = execute(
        app_db, "INSERT INTO TrainingJobs (status, step, started_by) VALUES ('running', 'queued', 'test') RETURNING id"
    )[0][0]
    app_db.run_training_job(job_id, 'test')
    [job] = query(app_db, 'SELECT status, message FROM TrainingJobs WHERE id = ?', (job_id,))
    return job


def test_a_retrained_model_that_scores_worse_never_goes_live(app_db, monkeypatch):
    job = start_training(app_db, monkeypatch)

    assert job['status'] == 'rejected'
    assert 'the current model stays active' in job['message']
    assert query(app_db, 'SELECT id FROM ModelVersions') == []
    assert app_db.get_active_model(force=True)[1] is None


def test_a_freshly_trained_model_is_saved_loaded_and_scores_with_the_ratio(app_db, monkeypatch):
    """The whole way round: train, store, load again, and score a live request."""
    # Whether a candidate is good enough is decided by compare_with_current and checked in the test above.
    monkeypatch.setattr(app_db.model_pipeline, 'compare_with_current', lambda candidate, holdout, current: {'accepted': True, 'current': None})
    create_user(app_db, 'staff@example.com')
    client = app_db.app.test_client()
    login(client, 'staff@example.com')

    job = start_training(app_db, monkeypatch)
    assert job['status'] == 'succeeded', job['message']

    artifacts, version_id = app_db.get_active_model(force=True)
    assert version_id is not None
    assert 'Amount_Ratio' in artifacts['features']
    assert artifacts['base_typical_amount'] > 0

    assert client.post('/api/predict', json=REQUEST).status_code == 200
    [event] = query(app_db, "SELECT details FROM AuditEvents WHERE action = 'request.submitted'")
    assert 'Amount_Ratio' in json.loads(event['details'])['explanation']
