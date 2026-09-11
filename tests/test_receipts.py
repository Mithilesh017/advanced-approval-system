"""Receipts are stored in the database, checked by their content, and open only for the employee and their admins."""
import hashlib
import io
import json

import pytest

from conftest import REQUEST_DETAILS, SUPER_ADMIN, create_organization, create_user, execute, login, query, use_model_score

PDF = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 64
FORM = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': '5000', 'Currency': 'INR', **REQUEST_DETAILS,
}


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def upload(content, name):
    return (io.BytesIO(content), name)


def submit_form(client, receipts=(), **changes):
    return client.post('/api/predict', data={**FORM, **changes, 'receipts': list(receipts)}, content_type='multipart/form-data')


def add_receipts(client, request_id, receipts):
    return client.post(
        '/api/auth/add_receipts', data={'request_id': str(request_id), 'receipts': list(receipts)}, content_type='multipart/form-data'
    )


def stored_receipts(app_db):
    return query(app_db, 'SELECT request_id, filename, content_type, size, sha256, uploaded_by FROM Receipts ORDER BY id')


@pytest.fixture
def staff(app_db, monkeypatch):
    use_model_score(app_db, monkeypatch, 0.99)
    create_user(app_db, 'staff@example.com')
    return session(app_db, 'staff@example.com')


def test_receipts_are_saved_with_the_request(app_db, staff):
    response = submit_form(staff, [upload(PDF, 'Hotel invoice.pdf'), upload(JPEG, 'taxi.jpeg')])
    assert response.status_code == 200, response.get_json()

    [saved] = query(app_db, 'SELECT id FROM Requests')
    common = {'request_id': saved['id'], 'uploaded_by': 'staff@example.com'}
    assert stored_receipts(app_db) == [
        {**common, 'filename': 'Hotel_invoice.pdf', 'content_type': 'application/pdf', 'size': len(PDF), 'sha256': hashlib.sha256(PDF).hexdigest()},
        {**common, 'filename': 'taxi.jpg', 'content_type': 'image/jpeg', 'size': len(JPEG), 'sha256': hashlib.sha256(JPEG).hexdigest()},
    ]
    actions = query(app_db, 'SELECT action FROM AuditEvents WHERE request_id = ? ORDER BY id', (saved['id'],))
    assert [row['action'] for row in actions] == ['request.submitted', 'receipt.added', 'receipt.added']
    assert staff.get('/api/auth/my_requests').get_json()[0]['receipt_count'] == 2


def test_a_receipts_type_comes_from_its_content_not_its_name(app_db, staff):
    assert submit_form(staff, [upload(PNG, 'scan.pdf')]).status_code == 200
    [receipt] = stored_receipts(app_db)
    assert (receipt['filename'], receipt['content_type']) == ('scan.png', 'image/png')


@pytest.mark.parametrize('make_receipts, message', [
    (lambda: [upload(b'MZ\x90\x00 not really a receipt', 'invoice.pdf')], 'PDF, JPG or PNG'),
    (lambda: [upload(b'', 'empty.pdf')], 'empty'),
    (lambda: [upload(b'%PDF-' + b'0' * (5 * 1024 * 1024), 'huge.pdf')], '5 MB'),
    (lambda: [upload(PDF, f'receipt{number}.pdf') for number in range(6)], 'at most 5'),
])
def test_invalid_receipts_are_refused_and_nothing_is_saved(app_db, staff, make_receipts, message):
    response = submit_form(staff, make_receipts())
    assert response.status_code == 400
    assert message in response.get_json()['error']
    assert query(app_db, 'SELECT id FROM Requests') == []
    assert stored_receipts(app_db) == []


def test_uploads_over_the_total_size_limit_are_refused(app_db, staff):
    response = submit_form(staff, [upload(b'%PDF-' + b'0' * (27 * 1024 * 1024), 'enormous.pdf')])
    assert response.status_code == 413
    assert 'too large' in response.get_json()['error']
    assert query(app_db, 'SELECT id FROM Requests') == []


def test_receipts_open_only_for_the_employee_and_their_admins(app_db, staff):
    assert submit_form(staff, [upload(PDF, 'hotel.pdf'), upload(PNG, 'taxi.png')]).status_code == 200
    [saved] = query(app_db, 'SELECT id FROM Requests')
    [pdf_receipt, png_receipt] = query(app_db, 'SELECT id FROM Receipts ORDER BY id')
    create_user(app_db, 'colleague@example.com')
    create_user(app_db, 'manager@example.com', role='Admin')
    other_org = create_organization(app_db, 'Other Co')
    create_user(app_db, 'admin@other.test', role='Admin', organization_id=other_org)

    for viewer in (staff, session(app_db, 'manager@example.com')):
        document = viewer.get('/api/auth/receipt', query_string={'id': pdf_receipt['id']})
        assert document.status_code == 200
        assert (document.data, document.mimetype) == (PDF, 'application/pdf')
        assert document.headers['Content-Disposition'].startswith('attachment')
        assert document.headers['X-Content-Type-Options'] == 'nosniff'
        listed = viewer.get('/api/auth/request_receipts', query_string={'id': saved['id']}).get_json()['receipts']
        assert [receipt['filename'] for receipt in listed] == ['hotel.pdf', 'taxi.png']

    image = staff.get('/api/auth/receipt', query_string={'id': png_receipt['id']})
    assert (image.mimetype, image.data) == ('image/png', PNG)
    assert image.headers['Content-Disposition'].startswith('inline')

    for outsider in (session(app_db, 'colleague@example.com'), session(app_db, 'admin@other.test')):
        assert outsider.get('/api/auth/receipt', query_string={'id': pdf_receipt['id']}).status_code == 404
        assert outsider.get('/api/auth/request_receipts', query_string={'id': saved['id']}).status_code == 404


def test_employees_can_add_receipts_later_but_not_to_rejected_requests(app_db, staff):
    assert submit_form(staff).status_code == 200
    [saved] = query(app_db, 'SELECT id FROM Requests')
    create_user(app_db, 'colleague@example.com')

    assert add_receipts(staff, saved['id'], [upload(PDF, 'late.pdf')]).status_code == 201
    assert add_receipts(session(app_db, 'colleague@example.com'), saved['id'], [upload(PDF, 'not-mine.pdf')]).status_code == 404
    assert add_receipts(staff, saved['id'], [upload(PDF, f'more{number}.pdf') for number in range(5)]).status_code == 409
    assert add_receipts(staff, saved['id'], []).status_code == 400

    execute(app_db, "UPDATE Requests SET final_decision = 'REJECTED' WHERE id = ?", (saved['id'],))
    assert add_receipts(staff, saved['id'], [upload(PDF, 'too-late.pdf')]).status_code == 409

    assert [receipt['filename'] for receipt in stored_receipts(app_db)] == ['late.pdf']
    [event] = query(app_db, "SELECT actor_email, details FROM AuditEvents WHERE action = 'receipt.added'")
    assert event['actor_email'] == 'staff@example.com'
    assert json.loads(event['details'])['filename'] == 'late.pdf'


def add_receipt_rule(app_db, above):
    response = session(app_db, SUPER_ADMIN['email']).post('/api/auth/create_policy_rule', json={
        'rule_type': 'receipt_required', 'name': 'Receipts needed', 'config': {'above_amount_inr': above},
    })
    assert response.status_code == 201, response.get_json()


def latest_decision(app_db):
    [row] = query(app_db, 'SELECT final_decision, policy_violations FROM Requests ORDER BY id DESC LIMIT 1')
    return row['final_decision'], json.loads(row['policy_violations'] or '[]')


def test_receipt_rule_sends_requests_without_a_receipt_to_a_person(app_db, staff):
    add_receipt_rule(app_db, 2000)

    assert submit_form(staff).get_json()['status'] == 'PENDING_REVIEW'
    decision, violations = latest_decision(app_db)
    assert decision == 'ESCALATED_RULE'
    assert violations[0]['reason'] == 'No receipt was attached for an amount above ₹2,000.'

    assert submit_form(staff, [upload(PDF, 'hotel.pdf')]).get_json()['status'] == 'APPROVED'
    assert submit_form(staff, Amount='1500').get_json()['status'] == 'APPROVED'


def test_receipt_rule_without_an_amount_covers_every_request(app_db, staff):
    add_receipt_rule(app_db, 0)
    assert submit_form(staff, Amount='100').get_json()['status'] == 'PENDING_REVIEW'
    assert latest_decision(app_db)[1][0]['reason'] == 'No receipt was attached.'


@pytest.mark.parametrize('above', [-1, 'lots', True])
def test_invalid_receipt_rules_are_refused(app_db, above):
    response = session(app_db, SUPER_ADMIN['email']).post('/api/auth/create_policy_rule', json={
        'rule_type': 'receipt_required', 'name': 'Receipts needed', 'config': {'above_amount_inr': above},
    })
    assert response.status_code == 400
