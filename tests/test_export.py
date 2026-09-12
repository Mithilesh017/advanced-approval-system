"""A company can take everything it put in and read it without this system."""
import io
import json
import zipfile

import pytest

from conftest import REQUEST_DETAILS, SUPER_ADMIN, create_organization, create_user, login, query, use_model_score

PDF = b'%PDF-1.4\n%%EOF\n'
FORM = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': '5000', 'Currency': 'INR', **REQUEST_DETAILS,
}
EXPORT = '/api/auth/export'


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def download(client):
    response = client.get(EXPORT)
    assert response.status_code == 200, response.get_json()
    return response, zipfile.ZipFile(io.BytesIO(response.data))


@pytest.fixture
def company(app_db, monkeypatch):
    use_model_score(app_db, monkeypatch, 0.99)
    create_user(app_db, 'staff@example.com')
    staff = session(app_db, 'staff@example.com')
    submitted = staff.post(
        '/api/predict', data={**FORM, 'receipts': [(io.BytesIO(PDF), 'hotel.pdf')]}, content_type='multipart/form-data'
    )
    assert submitted.status_code == 200, submitted.get_json()
    return session(app_db, SUPER_ADMIN['email'])


def test_the_download_holds_the_records_and_the_receipt_files(app_db, company):
    response, bundle = download(company)

    assert response.headers['Content-Type'] == 'application/zip'
    assert response.headers['Content-Disposition'].startswith('attachment')
    assert 'default-organization-export-' in response.headers['Content-Disposition']

    names = bundle.namelist()
    assert 'README.txt' in names and 'data.json' in names
    [receipt_file] = [name for name in names if name.startswith('receipts/')]
    assert bundle.read(receipt_file) == PDF

    data = json.loads(bundle.read('data.json'))
    assert data['organization']['name'] == 'Default Organization'
    assert data['exported_by'] == SUPER_ADMIN['email']
    assert [user['email'] for user in data['users']] == [SUPER_ADMIN['email'], 'staff@example.com']
    assert [request['submitted_by'] for request in data['requests']] == ['staff@example.com']
    assert data['requests'][0]['final_decision'] == 'APPROVED'
    assert data['receipts'][0]['filename'] == 'hotel.pdf'
    assert [event['action'] for event in data['history']] == ['request.submitted', 'receipt.added']


def test_passwords_join_links_and_other_companies_stay_out(app_db, company):
    other_org = create_organization(app_db, 'Other Co')
    create_user(app_db, 'boss@other.test', role='SuperAdmin', organization_id=other_org)
    _, bundle = download(company)
    raw = bundle.read('data.json').decode('utf-8')
    data = json.loads(raw)

    assert 'other.test' not in raw
    assert 'join_code' not in raw
    assert all('password_hash' not in user and 'reset_token' not in user for user in data['users'])
    [join_code] = query(app_db, 'SELECT join_code FROM Organizations WHERE id = ?', (app_db.DEFAULT_ORGANIZATION_ID,))
    assert join_code['join_code'] not in raw


def test_each_company_downloads_only_its_own(app_db, company):
    other_org = create_organization(app_db, 'Other Co')
    create_user(app_db, 'boss@other.test', role='SuperAdmin', organization_id=other_org)

    _, theirs = download(session(app_db, 'boss@other.test'))
    data = json.loads(theirs.read('data.json'))
    assert data['organization']['name'] == 'Other Co'
    assert (data['requests'], data['receipts']) == ([], [])
    assert [user['email'] for user in data['users']] == ['boss@other.test']


def test_only_a_super_admin_can_take_the_data(app_db, company):
    create_user(app_db, 'manager@example.com', role='Admin')
    assert session(app_db, 'manager@example.com').get(EXPORT).status_code == 403
    assert session(app_db, 'staff@example.com').get(EXPORT).status_code == 403


def test_taking_the_data_is_written_in_the_history(app_db, company):
    download(company)

    [event] = query(app_db, "SELECT actor_email, organization_id, details FROM AuditEvents WHERE action = 'organization.exported'")
    assert (event['actor_email'], event['organization_id']) == (SUPER_ADMIN['email'], app_db.DEFAULT_ORGANIZATION_ID)
    counts = json.loads(event['details'])
    assert (counts['requests'], counts['receipts'], counts['users']) == (1, 1, 2)
