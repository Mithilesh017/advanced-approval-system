"""Scoring a request and training on it build the model's columns in the same one place."""
import json

import numpy as np
import pandas as pd
import pytest

import model_pipeline
from conftest import REQUEST_DETAILS, create_user, login, query

REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR', **REQUEST_DETAILS,
}


@pytest.fixture
def artifacts(app_db):
    return app_db.load_bundled_artifacts()


@pytest.fixture
def staff(app_db):
    create_user(app_db, 'staff@example.com')
    client = app_db.app.test_client()
    login(client, 'staff@example.com')
    return client


def known_values(artifacts):
    return {col: str(artifacts['encoders'][col].classes_[0]) for col in model_pipeline.CATEGORICAL_FEATURES}


def test_a_request_is_encoded_exactly_like_a_training_row(app_db, artifacts):
    values = {**known_values(artifacts), 'Amount_INR': 7500.0}

    encoded, unknown = model_pipeline.prepare_request(artifacts, values)
    from_training = model_pipeline.encode_features(artifacts, pd.DataFrame([values]))

    assert unknown == []
    assert list(encoded.columns) == list(artifacts['features'])
    assert np.allclose(encoded.to_numpy(dtype=float), from_training.to_numpy(dtype=float))


def test_values_the_model_has_never_seen_are_named_and_fall_back_to_zero(app_db, artifacts):
    values = {**known_values(artifacts), 'Destination': 'Wakanda', 'Role': 'Time Traveller', 'Amount_INR': 5000.0}

    encoded, unknown = model_pipeline.prepare_request(artifacts, values)
    assert sorted(unknown) == ['Destination', 'Role']
    assert encoded['Destination'].iloc[0] == 0 and encoded['Role'].iloc[0] == 0
    # A value the model does know is still encoded normally.
    assert encoded['Department'].iloc[0] == artifacts['encoders']['Department'].transform([values['Department']])[0]


def test_a_model_saved_with_fewer_columns_is_still_scored(app_db, artifacts, staff):
    # Older stored models list their own columns; only those are built for them.
    trimmed = {**artifacts, 'features': ['Department', 'Amount_INR']}
    encoded, _ = model_pipeline.prepare_request(trimmed, {**known_values(artifacts), 'Amount_INR': 1000.0})
    assert list(encoded.columns) == ['Department', 'Amount_INR']


def test_an_unfamiliar_request_still_reaches_a_person(app_db, staff):
    response = staff.post('/api/predict', json={**REQUEST, 'Destination': 'Wakanda'})
    assert response.status_code == 200
    assert response.get_json()['status'] == 'PENDING_REVIEW'
    assert query(app_db, 'SELECT final_decision FROM Requests') == [{'final_decision': 'ESCALATED_UNKNOWN'}]


def test_the_explanation_names_the_columns_the_model_actually_used(app_db, staff):
    assert staff.post('/api/predict', json=REQUEST).status_code == 200
    [event] = query(app_db, "SELECT details FROM AuditEvents WHERE action = 'request.submitted'")
    explanation = json.loads(event['details'])['explanation']
    assert list(explanation) == list(app_db.load_bundled_artifacts()['features'])
