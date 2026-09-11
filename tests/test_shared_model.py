"""One model serves every organization: only Neuzem manages it, it only learns from organizations
that agreed to share data, and no organization's own vocabulary leaks into another's request form."""
import pytest
from sklearn.preprocessing import LabelEncoder

from conftest import add_request, create_organization, create_platform_owner, create_user, execute, login


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


@pytest.fixture
def owner(app_db):
    return session(app_db, create_platform_owner(app_db))


def add_decisions(app_db, organization_id):
    """Adds an admin's manual decision, an automatic approval and a pending request. Returns the manual decision's id."""
    manual = add_request(app_db, organization_id, 'staff@example.com', 'APPROVED')
    execute(app_db, "UPDATE Requests SET reviewed_by = 'manager@example.com' WHERE id = ?", (manual,))
    add_request(app_db, organization_id, 'staff@example.com', 'APPROVED')
    add_request(app_db, organization_id, 'staff@example.com')
    return manual


@pytest.fixture
def decisions(app_db):
    sharing = create_organization(app_db, 'Sharing Co')
    execute(app_db, 'UPDATE Organizations SET allow_training_data = 1 WHERE id = ?', (sharing,))
    private = create_organization(app_db, 'Private Co')
    return {
        'default': add_decisions(app_db, app_db.DEFAULT_ORGANIZATION_ID),
        'sharing': add_decisions(app_db, sharing),
        'private': add_decisions(app_db, private),
        'sharing_org': sharing,
    }


def test_platform_owner_runs_the_model_console(owner):
    for path in ('/api/platform/model/info', '/api/platform/model/versions', '/api/platform/model/jobs/latest'):
        assert owner.get(path).status_code == 200


def test_retraining_only_learns_from_manual_decisions_in_consenting_organizations(app_db, monkeypatch, decisions):
    collected = {}

    def stop_after_collecting(base, feedback=None):
        collected['row_keys'] = set(feedback['row_key'])
        raise RuntimeError('Stopping before the slow training step')

    monkeypatch.setattr(app_db.model_pipeline, 'train_ensemble', stop_after_collecting)
    job_id = execute(app_db, "INSERT INTO TrainingJobs (status, step, started_by) VALUES ('running', 'queued', 'test') RETURNING id")[0][0]
    app_db.run_training_job(job_id, 'test')

    assert collected['row_keys'] == {f"request:{decisions['default']}", f"request:{decisions['sharing']}"}


def test_model_console_only_counts_decisions_it_may_learn_from(app_db, owner, decisions):
    assert owner.get('/api/platform/model/info').get_json()['new_decisions_since_training'] == 2

    execute(app_db, 'UPDATE Organizations SET allow_training_data = 0 WHERE id = ?', (decisions['sharing_org'],))
    assert owner.get('/api/platform/model/info').get_json()['new_decisions_since_training'] == 1


def test_request_form_never_offers_another_organizations_values(app_db, monkeypatch):
    artifacts = dict(app_db.load_bundled_artifacts())
    artifacts.pop('_standard_form_options', None)
    encoders = dict(artifacts['encoders'])
    encoders['Department'] = LabelEncoder().fit([*map(str, encoders['Department'].classes_), 'Project Falcon'])
    artifacts['encoders'] = encoders
    # Models trained before organizations existed saved every learned value in their form options.
    artifacts['form_options'] = {**artifacts['form_options'], 'Department': [*artifacts['form_options']['Department'], 'Project Falcon']}
    monkeypatch.setattr(app_db, 'get_active_model', lambda force=False: (artifacts, None))

    org_a = create_organization(app_db, 'Org A')
    org_b = create_organization(app_db, 'Org B')
    create_user(app_db, 'staff@a.test', organization_id=org_a)
    create_user(app_db, 'staff@b.test', organization_id=org_b)
    add_request(app_db, org_a, 'staff@a.test', department='Project Falcon')
    add_request(app_db, org_a, 'staff@a.test', department='Skunkworks')

    options_a = session(app_db, 'staff@a.test').get('/api/model/form_options').get_json()['options']
    options_b = session(app_db, 'staff@b.test').get('/api/model/form_options').get_json()['options']

    assert 'Project Falcon' in options_a['Department']
    assert 'Skunkworks' not in options_a['Department']  # Used, but the model has not learned it yet.
    assert 'Project Falcon' not in options_b['Department']
    assert 'Engineering' in options_a['Department'] and 'Engineering' in options_b['Department']
    assert 'External Validation' not in options_b['Destination']
