"""
models/train.py

XGBoost W/D/L osztályozó + kalibrált valószínűségek.

Architektúra:
  - XGBoostClassifier  : W/D/L (3 osztály) softprob outputtal
  - CalibratedClassifier (isotonic) : valószínűség kalibrálás
  - TimeSeriesSplit CV  : nem random split! idő alapú, nem szivárog jövő a múltba

Kimenet:
  - models/xgb_model.joblib   : tanított modell
  - models/feature_names.json : feature sorrend (predikcihoz kell)
  - models/cv_report.txt      : cross-validation eredmények
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import (accuracy_score, brier_score_loss,
                             classification_report, log_loss)
from sklearn.model_selection import TimeSeriesSplit
import xgboost as xgb
import joblib

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection
from modell.pipeline import build_training_matrix

MODEL_DIR = Path(__file__).parent
MODEL_PATH = MODEL_DIR / "xgb_model.joblib"
FEATURE_NAMES_PATH = MODEL_DIR / "feature_names.json"
CV_REPORT_PATH = MODEL_DIR / "cv_report.txt"


XGB_PARAMS = {
    "n_estimators":      300,
    "max_depth":         4,
    "learning_rate":     0.05,
    "subsample":         0.8,
    "colsample_bytree":  0.8,
    "min_child_weight":  3,
    "gamma":             0.1,
    "reg_alpha":         0.1,
    "reg_lambda":        1.0,
    "objective":         "multi:softprob",
    "num_class":         3,
    "eval_metric":       "mlogloss",
    "random_state":      42,
    "n_jobs":            -1,
}


def train(conn, verbose: bool = True) -> dict:
    """
    Tanítja a modellt és elmenti.
    Visszaadja a CV metrikákat.
    """
    if verbose:
        print("[train] Feature mátrix építése...")
    X, y = build_training_matrix(conn)

    if verbose:
        print(f"  {X.shape[0]} meccs, {X.shape[1]} feature")
        print(f"  Label eloszlás: away={sum(y==0)}, döntetlen={sum(y==1)}, home={sum(y==2)}")

    feature_names = list(X.columns)
    X_arr = X.values.astype(np.float32)
    y_arr = y.values

    # ── Súlyozás a döntetlen osztályra (class imbalance kezelés) ────────
    # A draw a legritkább ~22%. Súlyozzuk inverz arányban.
    n_total = len(y_arr)
    n_draw = int(sum(y_arr == 1))
    n_home = int(sum(y_arr == 2))
    n_away = int(sum(y_arr == 0))
    n_classes = 3

    draw_weight = n_total / (n_classes * n_draw) if n_draw > 0 else 1.0
    home_weight = n_total / (n_classes * n_home) if n_home > 0 else 1.0
    away_weight = n_total / (n_classes * n_away) if n_away > 0 else 1.0

    sample_weight_arr = np.ones(n_total, dtype=np.float32)
    sample_weight_arr[y_arr == 1] = draw_weight
    sample_weight_arr[y_arr == 2] = home_weight
    sample_weight_arr[y_arr == 0] = away_weight

    if verbose:
        print(f"  Sample weight → away={away_weight:.2f}, draw={draw_weight:.2f}, home={home_weight:.2f}")

    # ── TimeSeriesSplit CV (nem random!) ──────────────────────────────
    # 868 meccs időrendben → 5 fold, mindig a múlton tanít, jövőn tesztel
    tscv = TimeSeriesSplit(n_splits=5)

    cv_accuracies = []
    cv_loglosses  = []
    cv_briers     = []
    fold_reports  = []

    if verbose:
        print("\n[train] TimeSeriesSplit keresztvalidáció (5 fold)...")

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X_arr), 1):
        X_tr, X_te = X_arr[train_idx], X_arr[test_idx]
        y_tr, y_te = y_arr[train_idx], y_arr[test_idx]
        sw_tr      = sample_weight_arr[train_idx]

        base = xgb.XGBClassifier(**XGB_PARAMS, verbosity=0)
        # Kalibrálás: isotonic jobb mint sigmoid kis adaton
        model = CalibratedClassifierCV(base, method="isotonic", cv=3)
        model.fit(X_tr, y_tr, sample_weight=sw_tr)

        probs = model.predict_proba(X_te)
        preds = np.argmax(probs, axis=1)

        acc = accuracy_score(y_te, preds)
        ll  = log_loss(y_te, probs)
        # Brier score: 1v1 home győzelemre
        home_win_true = (y_te == 2).astype(float)
        home_win_prob = probs[:, 2]
        bs = brier_score_loss(home_win_true, home_win_prob)

        cv_accuracies.append(acc)
        cv_loglosses.append(ll)
        cv_briers.append(bs)

        train_size = len(train_idx)
        test_size  = len(test_idx)

        report = classification_report(y_te, preds,
                                        target_names=["Away", "Draw", "Home"],
                                        output_dict=False)
        fold_reports.append(
            f"Fold {fold} (train={train_size}, test={test_size})\n"
            f"  Accuracy={acc:.3f}  LogLoss={ll:.3f}  Brier={bs:.3f}\n"
            f"{report}\n"
        )

        if verbose:
            print(f"  Fold {fold}: acc={acc:.3f}  log_loss={ll:.3f}  brier={bs:.3f}")

    # ── Teljes adaton végleges modell tanítás ─────────────────────────
    if verbose:
        print("\n[train] Végleges modell tanítása (teljes adat)...")

    base_final = xgb.XGBClassifier(**XGB_PARAMS, verbosity=0)
    final_model = CalibratedClassifierCV(base_final, method="isotonic", cv=5)
    final_model.fit(X_arr, y_arr, sample_weight=sample_weight_arr)

    # Feature importance (a belső XGB modellből)
    try:
        inner_xgb = final_model.calibrated_classifiers_[0].estimator
        importances = inner_xgb.feature_importances_
        fi = sorted(zip(feature_names, importances), key=lambda x: -x[1])
    except Exception:
        fi = []

    # ── Mentés ────────────────────────────────────────────────────────
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_model, MODEL_PATH)
    Path(FEATURE_NAMES_PATH).write_text(json.dumps(feature_names))

    summary = (
        f"=== VB Prediktor – CV Jelentés ===\n\n"
        f"Adatok: {X.shape[0]} meccs, {X.shape[1]} feature\n"
        f"Módszer: XGBoost + CalibratedClassifier (isotonic)\n"
        f"CV: TimeSeriesSplit (5 fold)\n\n"
        f"{'Metrika':<15} {'Átlag':>8} {'Std':>8}\n"
        f"{'-'*35}\n"
        f"{'Accuracy':<15} {np.mean(cv_accuracies):>8.3f} {np.std(cv_accuracies):>8.3f}\n"
        f"{'Log Loss':<15} {np.mean(cv_loglosses):>8.3f} {np.std(cv_loglosses):>8.3f}\n"
        f"{'Brier Score':<15} {np.mean(cv_briers):>8.3f} {np.std(cv_briers):>8.3f}\n\n"
        f"{'='*35}\n\n"
        + "\n".join(fold_reports)
    )

    if fi:
        summary += "\nFeature fontosság (top 10):\n"
        for name, imp in fi[:10]:
            summary += f"  {name:<25} {imp:.4f}\n"

    Path(CV_REPORT_PATH).write_text(summary)

    metrics = {
        "accuracy_mean": float(np.mean(cv_accuracies)),
        "accuracy_std":  float(np.std(cv_accuracies)),
        "logloss_mean":  float(np.mean(cv_loglosses)),
        "brier_mean":    float(np.mean(cv_briers)),
        "feature_importance": fi,
    }

    if verbose:
        print(f"\n[train] CV eredmények:")
        print(f"  Accuracy:  {metrics['accuracy_mean']:.3f} ± {metrics['accuracy_std']:.3f}")
        print(f"  Log Loss:  {metrics['logloss_mean']:.3f}")
        print(f"  Brier:     {metrics['brier_mean']:.3f}")
        print(f"\n  Modell mentve: {MODEL_PATH}")
        print(f"  CV jelentés:   {CV_REPORT_PATH}")

        if fi:
            print("\n  Top 5 feature:")
            for name, imp in fi[:5]:
                print(f"    {name:<25} {imp:.4f}")

    return metrics


def load_model():
    """Betölti a mentett modellt és feature neveket."""
    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"Modell nem található: {MODEL_PATH}\n"
            "Futtasd: python models/train.py"
        )
    model = joblib.load(MODEL_PATH)
    feature_names = json.loads(Path(FEATURE_NAMES_PATH).read_text())
    return model, feature_names


def predict_proba(model, feature_names: list[str], X: pd.DataFrame) -> np.ndarray:
    """
    Visszaadja a valószínűség mátrixot.
    shape: (n_matches, 3) ahol [:, 0]=away, [:, 1]=draw, [:, 2]=home
    """
    X_ordered = X[feature_names].values.astype(np.float32)
    return model.predict_proba(X_ordered)


def predict_proba_with_draw_boost(
    model, feature_names: list[str], X: pd.DataFrame,
    draw_boost: float = 1.4
) -> np.ndarray:
    """
    Mint predict_proba(), de a döntetlen valószínűségét megnöveli.

    draw_boost: szorzó (1.4 = 40%-kal növeli a draw esélyét)
    A másik két osztályt arányosan csökkenti, hogy összeg=1 maradjon.
    """
    probs = predict_proba(model, feature_names, X)
    adjusted = probs.copy()
    adjusted[:, 1] *= draw_boost
    row_sums = adjusted.sum(axis=1, keepdims=True)
    return adjusted / row_sums


if __name__ == "__main__":
    conn = get_connection()
    train(conn, verbose=True)
    conn.close()