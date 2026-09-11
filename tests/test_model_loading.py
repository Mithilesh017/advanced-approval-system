from conftest import create_platform_owner, execute, login


def test_unloadable_model_version_falls_back_once_and_is_reported(app_db, monkeypatch):
    for key in ('checked_at', 'unloadable_version_id'):
        monkeypatch.setitem(app_db._model_state, key, None)
    version_id = execute(
        app_db, 'INSERT INTO ModelVersions (artifact, metrics, created_by, is_active) VALUES (?, ?, ?, 1) RETURNING id',
        (b'not a saved model', '{}', 'test')
    )[0][0]

    load_attempts = []
    real_load = app_db.joblib.load

    def counting_load(*args, **kwargs):
        load_attempts.append(1)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(app_db.joblib, 'load', counting_load)
    original = app_db.load_bundled_artifacts()

    artifacts, scoring_version = app_db.get_active_model(force=True)
    assert artifacts is original and scoring_version is None
    assert len(load_attempts) == 1

    # Routine refreshes keep the original model without retrying (and re-logging) the broken version.
    monkeypatch.setitem(app_db._model_state, 'checked_at', None)
    artifacts, scoring_version = app_db.get_active_model()
    assert artifacts is original and scoring_version is None
    assert len(load_attempts) == 1

    owner = app_db.app.test_client()
    login(owner, create_platform_owner(app_db))
    assert f'Version {version_id}' in owner.get('/api/platform/model/info').get_json()['load_problem']

    assert owner.post('/api/platform/model/activate', json={'version_id': None}).status_code == 200
    assert owner.get('/api/platform/model/info').get_json()['load_problem'] is None
