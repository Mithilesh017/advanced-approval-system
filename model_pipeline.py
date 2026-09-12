import hashlib
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import shap
import xgboost as xgb
from sklearn.ensemble import IsolationForest
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.svm import OneClassSVM

CATEGORICAL_FEATURES = ['Role', 'Department', 'Request_Type', 'Destination']
# Amount_Ratio is the amount against what is normal where the request came from, so 50,000 rupees can mean
# "five times their usual" in one company and "an ordinary week" in another.
NUMERIC_FEATURES = ['Amount_INR', 'Amount_Ratio']
FEATURES = CATEGORICAL_FEATURES + NUMERIC_FEATURES
TRAINING_COLUMNS = ['row_key', 'source'] + FEATURES + ['label']
# The ratio is worked out again whenever it is needed, so saved models keep the shape they always had.
STORED_COLUMNS = [column for column in TRAINING_COLUMNS if column != 'Amount_Ratio']
AMOUNT_RATIO_CAP = 20.0
TYPICAL_AMOUNT_MIN_ROWS = 5
HOLDOUT_BUCKETS = 5
FEEDBACK_WEIGHT = 5.0
ONE_CLASS_SVM_MAX_ROWS = 10000
QUALITY_TOLERANCE = 0.002


class TrainingDataMissing(Exception):
    pass


def typical_amount(amounts):
    """The middle amount of a set of requests. The middle, not the average, so one huge request cannot move it."""
    values = [float(amount) for amount in amounts if amount is not None and float(amount) > 0]
    return float(np.median(values)) if values else None


def amount_ratio(amount, typical):
    """How big one request is against what is normal. 1.0 when there is nothing to compare it with yet."""
    if not typical or float(typical) <= 0:
        return 1.0
    return float(min(AMOUNT_RATIO_CAP, max(0.0, float(amount) / float(typical))))


def with_amount_ratio(frame, typical):
    frame = frame.copy()
    frame['Amount_Ratio'] = [amount_ratio(amount, typical) for amount in frame['Amount_INR']]
    return frame


def pack_training_data(frame):
    # Plain NumPy arrays keep saved models loadable without pyarrow, which pandas 3 uses for text whenever it is installed.
    packed = {col: frame[col].astype(str).to_numpy(dtype=object) for col in ['row_key', 'source'] + CATEGORICAL_FEATURES}
    packed['Amount_INR'] = frame['Amount_INR'].to_numpy(dtype='float64')
    packed['label'] = frame['label'].to_numpy(dtype='int8')
    return packed


def unpack_training_data(data):
    frame = data if isinstance(data, pd.DataFrame) else pd.DataFrame({col: data[col] for col in STORED_COLUMNS})
    if 'Amount_Ratio' not in frame.columns:
        frame = with_amount_ratio(frame, typical_amount(frame['Amount_INR']))
    return frame[TRAINING_COLUMNS]


def base_training_data_from_csv(csv_path):
    df = pd.read_csv(csv_path)
    request_ids = df['Request_ID'].astype(str)
    frame = pd.DataFrame({
        'row_key': request_ids,
        'source': np.where(request_ids.str.startswith('REAL_'), 'external', 'corporate'),
        'Amount_INR': df['Amount_INR'].astype(float),
        'label': np.where(df['Is_Anomaly'] == 0, 1, 0),
    })
    for col in CATEGORICAL_FEATURES:
        frame[col] = df[col].astype(str)
    frame = with_amount_ratio(frame, typical_amount(frame['Amount_INR']))
    return frame[TRAINING_COLUMNS]


def base_training_data(current_artifacts, csv_path=None):
    if current_artifacts is not None and current_artifacts.get('training_data') is not None:
        return unpack_training_data(current_artifacts['training_data'])
    if csv_path and os.path.exists(csv_path):
        return base_training_data_from_csv(csv_path)
    raise TrainingDataMissing(
        'The base training data is missing from the active model. '
        'Rebuild ensemble_ai_model.pkl with train_ensemble_model.py and redeploy.'
    )


def typical_amount_by_organization(decisions):
    """What a normal request costs in each organization, falling back to everybody's normal for a new one."""
    everyone = typical_amount([row['normalized_amount'] for row in decisions])
    amounts = {}
    for row in decisions:
        amounts.setdefault(row.get('organization_id'), []).append(row['normalized_amount'])
    return {
        organization_id: (typical_amount(values) if len(values) >= TYPICAL_AMOUNT_MIN_ROWS else everyone)
        for organization_id, values in amounts.items()
    }


def feedback_training_data(decisions):
    usable = [
        row for row in decisions
        if row.get('normalized_amount') is not None
        and all(row.get(key) for key in ('role', 'department', 'request_type', 'destination'))
    ]
    typical = typical_amount_by_organization(usable)
    records = [
        {
            'row_key': f"request:{row['id']}",
            'source': 'feedback',
            'Role': str(row['role']),
            'Department': str(row['department']),
            'Request_Type': str(row['request_type']),
            'Destination': str(row['destination']),
            'Amount_INR': float(row['normalized_amount']),
            'Amount_Ratio': amount_ratio(row['normalized_amount'], typical.get(row.get('organization_id'))),
            'label': 1 if row['final_decision'] == 'APPROVED' else 0,
        }
        for row in usable
    ]
    return pd.DataFrame(records, columns=TRAINING_COLUMNS)


def holdout_mask(row_keys):
    # A stable hash keeps each row on the same side of the split across retrains,
    # so a model is never compared on rows it was trained on.
    return np.array([
        int(hashlib.sha256(key.encode('utf-8')).hexdigest()[:8], 16) % HOLDOUT_BUCKETS == 0
        for key in row_keys.astype(str)
    ], dtype=bool)


def encode_features(artifacts, frame):
    encoded = pd.DataFrame(index=frame.index)
    for col in CATEGORICAL_FEATURES:
        encoder = artifacts['encoders'][col]
        values = frame[col].astype(str)
        known = values.isin(encoder.classes_).to_numpy()
        column = np.zeros(len(values), dtype=np.int64)
        if known.any():
            column[known] = encoder.transform(values[known])
        encoded[col] = column
    numeric = artifacts.get('numeric_features') or ['Amount_INR']
    encoded[numeric] = artifacts['scaler'].transform(frame[numeric].astype(float))
    return encoded[artifacts['features']]


def unknown_categories(artifacts, frame):
    """The categorical columns holding a value this model was never trained on."""
    missing = []
    for col in CATEGORICAL_FEATURES:
        values = frame[col].astype(str)
        if not values.isin(artifacts['encoders'][col].classes_).all():
            missing.append(col)
    return missing


def request_frame(artifacts, values):
    """One request in the same shape as a row of training data."""
    row = {col: str(values.get(col, '')) for col in CATEGORICAL_FEATURES}
    row['Amount_INR'] = float(values['Amount_INR'])
    # Without enough of an organization's own requests to know their normal, the base data's normal is used.
    row['Amount_Ratio'] = amount_ratio(row['Amount_INR'], values.get('typical_amount') or artifacts.get('base_typical_amount'))
    return pd.DataFrame([row])


def prepare_request(artifacts, values):
    """Turns one request into exactly the columns the model was trained on.

    Scoring and training both go through here, so the two can never drift apart. Returns the encoded
    row and the columns holding a value the model has not seen before.
    """
    frame = request_frame(artifacts, values)
    return encode_features(artifacts, frame), unknown_categories(artifacts, frame)


def evaluate(artifacts, frame):
    labels = frame['label'].to_numpy()
    if len(frame) == 0 or len(np.unique(labels)) < 2:
        return {'roc_auc': None, 'accuracy': None, 'f1': None}
    scores = artifacts['xgboost_model'].predict_proba(encode_features(artifacts, frame))[:, 1]
    predicted = (scores > 0.5).astype(int)
    return {
        'roc_auc': float(roc_auc_score(labels, scores)),
        'accuracy': float(accuracy_score(labels, predicted)),
        'f1': float(f1_score(labels, predicted)),
    }


def form_options(frame):
    # Only the base corporate vocabulary is offered to every organization; organizations' own values stay private.
    usable = frame[frame['source'].astype(str) == 'corporate']
    return {col: sorted(usable[col].astype(str).unique().tolist()) for col in CATEGORICAL_FEATURES}


def standard_form_options(artifacts):
    # Derived from the stored base data rather than the saved form options, so models trained before
    # organizations existed also stop offering one organization's values to another.
    if '_standard_form_options' not in artifacts:
        if artifacts.get('training_data') is not None:
            artifacts['_standard_form_options'] = form_options(unpack_training_data(artifacts['training_data']))
        else:
            artifacts['_standard_form_options'] = artifacts.get('form_options') or {col: [] for col in CATEGORICAL_FEATURES}
    return artifacts['_standard_form_options']


def train_ensemble(base, feedback=None):
    parts = [base] if feedback is None or feedback.empty else [base, feedback]
    text_columns = {col: str for col in ['row_key', 'source'] + CATEGORICAL_FEATURES}
    frame = pd.concat([part[TRAINING_COLUMNS].astype(text_columns) for part in parts], ignore_index=True)
    frame = frame.dropna(subset=FEATURES + ['label'])

    in_holdout = holdout_mask(frame['row_key'])
    train, holdout = frame[~in_holdout], frame[in_holdout]

    artifacts = {
        'encoders': {col: LabelEncoder().fit(train[col]) for col in CATEGORICAL_FEATURES},
        'scaler': StandardScaler().fit(train[NUMERIC_FEATURES]),
        'features': list(FEATURES),
        'numeric_features': list(NUMERIC_FEATURES),
        'base_typical_amount': typical_amount(frame[frame['source'] != 'feedback']['Amount_INR']),
    }
    X_train = encode_features(artifacts, train)
    weights = np.where(train['source'] == 'feedback', FEEDBACK_WEIGHT, 1.0)

    xgb_model = xgb.XGBClassifier(
        n_estimators=100, max_depth=5, learning_rate=0.1,
        subsample=0.8, colsample_bytree=0.8, n_jobs=-1, random_state=42
    )
    xgb_model.fit(X_train, train['label'].to_numpy(), sample_weight=weights)

    iso_forest = IsolationForest(
        n_estimators=100, contamination=0.05, max_features=1.0,
        bootstrap=True, n_jobs=-1, random_state=42
    ).fit(X_train)

    svm_sample = X_train.sample(n=min(len(X_train), ONE_CLASS_SVM_MAX_ROWS), random_state=42)
    one_class_svm = OneClassSVM(nu=0.05, kernel='rbf', gamma='scale').fit(svm_sample)

    artifacts.update(
        xgboost_model=xgb_model,
        isolation_forest=iso_forest,
        one_class_svm=one_class_svm,
        shap_explainer=shap.TreeExplainer(xgb_model),
    )

    metrics = evaluate(artifacts, holdout)
    metrics.update(
        training_rows=int(len(train)),
        holdout_rows=int(len(holdout)),
        feedback_rows=int((frame['source'] == 'feedback').sum()),
    )
    artifacts.update(
        training_data=pack_training_data(frame[frame['source'] != 'feedback'][STORED_COLUMNS]),
        form_options=form_options(train),
        metrics=metrics,
        trained_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    )
    return artifacts, holdout


def compare_with_current(candidate, holdout, current):
    if current is None:
        return {'accepted': True, 'current': None}
    try:
        current_metrics = evaluate(current, holdout)
    except Exception:
        return {'accepted': True, 'current': None}

    new_score = candidate['metrics']['roc_auc']
    old_score = current_metrics['roc_auc']
    accepted = old_score is None or (new_score is not None and new_score >= old_score - QUALITY_TOLERANCE)
    return {'accepted': accepted, 'current': current_metrics}
