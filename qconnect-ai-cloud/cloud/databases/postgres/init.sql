-- =====================================================================
-- QConnect-AI Enterprise :: Postgres schema (init.sql)
-- ---------------------------------------------------------------------
-- Idempotent bootstrap for the primary relational store.
--
-- Extensions:
--   * timescaledb  -> hypertables for the high-volume qc_results table
--   * vector       -> pgvector embeddings for similarity/RCA search
--   * pgcrypto     -> gen_random_uuid() for UUID primary keys
--
-- NOTE: timescaledb and vector require a Postgres image that ships those
-- extensions (e.g. timescale/timescaledb-ha:pg16 + pgvector). On a vanilla
-- postgres:16-alpine image the two CREATE EXTENSION calls below will fail; the
-- schema itself does NOT depend on either extension being present, so you may
-- comment them out or run on the TimescaleDB image. pgcrypto ships with the
-- standard postgres contrib and is required.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;       -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS timescaledb;    -- time-series hypertables (TS image only)
CREATE EXTENSION IF NOT EXISTS vector;         -- pgvector (image with pgvector only)


-- =====================================================================
-- qc_results
-- ---------------------------------------------------------------------
-- One row per evaluated QC measurement. This is the highest-volume table
-- and the natural candidate for TimescaleDB hypertable conversion and/or
-- native range partitioning by test_date (see the partitioning note below).
-- =====================================================================
CREATE TABLE IF NOT EXISTS qc_results (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                      UUID NOT NULL UNIQUE,
    test_date                   TIMESTAMPTZ NOT NULL,
    analyzer_id                 TEXT,
    analyte_code                TEXT,
    qc_lot_id                   TEXT,
    result_value                DOUBLE PRECISION,
    target_value                DOUBLE PRECISION,
    sd_value                    DOUBLE PRECISION,
    cv_percent                  DOUBLE PRECISION,
    bias_percent                DOUBLE PRECISION,
    deviation_sd                DOUBLE PRECISION,
    westgard_rule_violated      TEXT,
    westgard_status             TEXT,
    qconnect_status             TEXT,
    sigma_status                TEXT,
    diagnostic_sigma            DOUBLE PRECISION,
    ai_anomaly_score            DOUBLE PRECISION,
    ai_failure_probability_48h  DOUBLE PRECISION,
    qc_status                   TEXT CHECK (qc_status IN ('PASS','FAIL','REVIEW_REQUIRED','HOLD_PENDING_AI')),
    severity                    TEXT,
    operator_id                 TEXT,
    -- corrective_action_id references capa_actions, which is created later in
    -- this script. The FK is added as a DEFERRABLE constraint after both
    -- tables exist (see ALTER TABLE near the end).
    corrective_action_id        UUID,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  qc_results IS 'Evaluated QC measurements (highest-volume table; partition/hypertable by test_date).';
COMMENT ON COLUMN qc_results.run_id IS 'Idempotency key from the analyzer/edge — unique per physical run.';
COMMENT ON COLUMN qc_results.deviation_sd IS 'Signed z-score (value - target) / sd at evaluation time.';
COMMENT ON COLUMN qc_results.qc_status IS 'Fused verdict across all engines.';
COMMENT ON COLUMN qc_results.corrective_action_id IS 'Optional FK to the CAPA opened for this failure.';

CREATE INDEX IF NOT EXISTS idx_qc_results_analyzer_date ON qc_results (analyzer_id, test_date);
CREATE INDEX IF NOT EXISTS idx_qc_results_analyte_date  ON qc_results (analyte_code, test_date);
CREATE INDEX IF NOT EXISTS idx_qc_results_status        ON qc_results (qc_status);

-- ---------------------------------------------------------------------
-- PARTITIONING / HYPERTABLE NOTE
-- ---------------------------------------------------------------------
-- Option A (TimescaleDB): convert to a hypertable so chunks are managed
-- automatically and old chunks can be compressed/retained:
--
--   SELECT create_hypertable('qc_results', 'test_date', if_not_exists => TRUE);
--
-- Option B (native declarative partitioning) — recreate the table as:
--
--   CREATE TABLE qc_results (...) PARTITION BY RANGE (test_date);
--   CREATE TABLE qc_results_2026q1 PARTITION OF qc_results
--       FOR VALUES FROM ('2026-01-01') TO ('2026-04-01');
--   CREATE TABLE qc_results_2026q2 PARTITION OF qc_results
--       FOR VALUES FROM ('2026-04-01') TO ('2026-07-01');
--
-- Native partitioning requires test_date in the PRIMARY KEY; left commented
-- here so the base schema stays simple and image-agnostic.
-- ---------------------------------------------------------------------


-- =====================================================================
-- control_limits
-- ---------------------------------------------------------------------
-- Current control limits per analyte/assay/lot/level. Dual limits: classic
-- Westgard (mean +/- 3SD) and QConnect (percentile/KDE based).
-- =====================================================================
CREATE TABLE IF NOT EXISTS control_limits (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analyte_code                TEXT NOT NULL,
    assay_product_code          TEXT NOT NULL,
    qc_lot_id                   TEXT NOT NULL,
    qc_level                    TEXT NOT NULL,
    westgard_mean               DOUBLE PRECISION,
    westgard_sd                 DOUBLE PRECISION,
    westgard_limit_3s_lower     DOUBLE PRECISION,
    westgard_limit_3s_upper     DOUBLE PRECISION,
    qconnect_percentile_5       DOUBLE PRECISION,
    qconnect_percentile_95      DOUBLE PRECISION,
    qconnect_lcl                DOUBLE PRECISION,
    qconnect_ucl                DOUBLE PRECISION,
    sigma_metric                DOUBLE PRECISION,
    diagnostic_sigma            DOUBLE PRECISION,
    distribution_type           TEXT,
    shapiro_wilk_p              DOUBLE PRECISION,
    mu_value                    DOUBLE PRECISION,
    valid_from_date             TIMESTAMPTZ,
    valid_to_date               TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_control_limits UNIQUE (analyte_code, assay_product_code, qc_lot_id, qc_level)
);

COMMENT ON TABLE  control_limits IS 'Active QC limits (Westgard + QConnect) per analyte/assay/lot/level.';
COMMENT ON COLUMN control_limits.distribution_type IS 'gaussian | skewed | bimodal | unknown — drives which engine is authoritative.';
COMMENT ON COLUMN control_limits.mu_value IS 'Sigma-metric measurement uncertainty (mu) for the method.';


-- =====================================================================
-- control_limits_history
-- ---------------------------------------------------------------------
-- Append-only snapshots of control_limits used to detect/track limit drift.
-- =====================================================================
CREATE TABLE IF NOT EXISTS control_limits_history (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    control_limit_id            UUID REFERENCES control_limits (id) ON DELETE CASCADE,
    snapshot_date               TIMESTAMPTZ NOT NULL DEFAULT now(),
    change_detected             BOOLEAN DEFAULT FALSE,
    change_magnitude_percent    DOUBLE PRECISION,
    payload                     JSONB
);

COMMENT ON TABLE  control_limits_history IS 'Snapshots of control_limits for drift detection / audit.';
COMMENT ON COLUMN control_limits_history.payload IS 'Full JSON snapshot of the control_limits row at snapshot_date.';

CREATE INDEX IF NOT EXISTS idx_clh_control_limit ON control_limits_history (control_limit_id, snapshot_date);


-- =====================================================================
-- qc_materials
-- ---------------------------------------------------------------------
-- QC material lots with infectious-disease negativity flags (serology),
-- commutability and storage constraints.
-- =====================================================================
CREATE TABLE IF NOT EXISTS qc_materials (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_name                 TEXT,
    product_code                TEXT,
    lot_number                  TEXT NOT NULL UNIQUE,
    analyte_code                TEXT,
    qc_level                    TEXT,
    target_value                DOUBLE PRECISION,
    target_sd                   DOUBLE PRECISION,
    manufacture_date            DATE,
    expiry_date                 DATE,
    shelf_life_days             INTEGER,
    storage_temp_min            DOUBLE PRECISION,
    storage_temp_max            DOUBLE PRECISION,
    negative_hiv                BOOLEAN,
    negative_hcv                BOOLEAN,
    negative_syphilis           BOOLEAN,
    commutability_tested        BOOLEAN,
    matrix_type                 TEXT,
    status                      TEXT CHECK (status IN ('ACTIVE','DEPLETED','EXPIRED','QUARANTINED')),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  qc_materials IS 'QC control material lots and their assigned values / safety flags.';
COMMENT ON COLUMN qc_materials.negative_hiv IS 'Donor-screen negativity attestation for the QC matrix (serology safety).';
COMMENT ON COLUMN qc_materials.commutability_tested IS 'Whether the material was verified commutable for the method.';

CREATE INDEX IF NOT EXISTS idx_qc_materials_analyte ON qc_materials (analyte_code);
CREATE INDEX IF NOT EXISTS idx_qc_materials_status  ON qc_materials (status);


-- =====================================================================
-- westgard_sigma_metrics
-- ---------------------------------------------------------------------
-- Periodic Six Sigma capability assessment per analyte/method/analyzer.
-- =====================================================================
CREATE TABLE IF NOT EXISTS westgard_sigma_metrics (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analyte_code                TEXT NOT NULL,
    method_code                 TEXT NOT NULL,
    analyzer_id                 TEXT NOT NULL,
    bias_percent                DOUBLE PRECISION,
    cv_percent                  DOUBLE PRECISION,
    allowable_error_percent     DOUBLE PRECISION,
    sigma_metric                DOUBLE PRECISION,
    sigma_category              TEXT,
    recommended_rule_set        TEXT,
    expected_frr                DOUBLE PRECISION,
    expected_edr                DOUBLE PRECISION,
    data_period_start           DATE,
    data_period_end             DATE,
    samples_analyzed            INTEGER,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_sigma_metrics UNIQUE (analyte_code, method_code, analyzer_id)
);

COMMENT ON TABLE  westgard_sigma_metrics IS 'Six Sigma capability per analyte/method/analyzer with recommended rule set.';
COMMENT ON COLUMN westgard_sigma_metrics.expected_frr IS 'Expected false rejection rate (%) for the recommended rule set.';
COMMENT ON COLUMN westgard_sigma_metrics.expected_edr IS 'Expected error detection rate (%) for the recommended rule set.';


-- =====================================================================
-- diagnostic_sigma_outcomes
-- ---------------------------------------------------------------------
-- Consequence-weighted diagnostic sigma from clinical outcome validation.
-- =====================================================================
CREATE TABLE IF NOT EXISTS diagnostic_sigma_outcomes (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analyte_code                TEXT NOT NULL,
    true_positives              INTEGER,
    true_negatives              INTEGER,
    false_positives             INTEGER,
    false_negatives             INTEGER,
    sensitivity                 DOUBLE PRECISION,
    specificity                 DOUBLE PRECISION,
    npv                         DOUBLE PRECISION,
    ppv                         DOUBLE PRECISION,
    consequence_weight_fn       DOUBLE PRECISION,
    consequence_weight_fp       DOUBLE PRECISION,
    diagnostic_sigma            DOUBLE PRECISION,
    clinical_impact_percent     DOUBLE PRECISION,
    validation_period_start     DATE,
    validation_period_end       DATE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  diagnostic_sigma_outcomes IS 'Clinically-weighted diagnostic sigma derived from confusion-matrix outcomes.';
COMMENT ON COLUMN diagnostic_sigma_outcomes.consequence_weight_fn IS 'Clinical cost weight of a false negative (e.g. missed HIV).';


-- =====================================================================
-- capa_actions
-- ---------------------------------------------------------------------
-- Corrective And Preventive Actions opened against failed QC results.
-- =====================================================================
CREATE TABLE IF NOT EXISTS capa_actions (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    capa_number                 TEXT NOT NULL UNIQUE,
    qc_result_id                UUID REFERENCES qc_results (id) ON DELETE SET NULL,
    incident_date               TIMESTAMPTZ,
    severity                    TEXT,
    root_cause_category         TEXT,
    root_cause_description      TEXT,
    ai_suggested_causes         JSONB,
    ai_rca_confidence           DOUBLE PRECISION,
    immediate_action            TEXT,
    preventive_action           TEXT,
    target_closure_date         DATE,
    actual_closure_date         DATE,
    verification_evidence       TEXT,
    effectiveness_confirmed     BOOLEAN,
    monitoring_period_days      INTEGER,
    recurrence_detected         BOOLEAN,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  capa_actions IS 'CAPA workflow records linked to the offending QC result.';
COMMENT ON COLUMN capa_actions.ai_suggested_causes IS 'Ranked candidate root causes from the RCA graph (JSON).';
COMMENT ON COLUMN capa_actions.effectiveness_confirmed IS 'Whether the preventive action was verified effective post-monitoring.';

CREATE INDEX IF NOT EXISTS idx_capa_qc_result ON capa_actions (qc_result_id);
CREATE INDEX IF NOT EXISTS idx_capa_severity  ON capa_actions (severity);


-- =====================================================================
-- distribution_predictions
-- ---------------------------------------------------------------------
-- History of distribution-shape detections that drive engine selection.
-- =====================================================================
CREATE TABLE IF NOT EXISTS distribution_predictions (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analyte_code                TEXT,
    assay_product_code          TEXT,
    qc_lot_id                   TEXT,
    analysis_date               TIMESTAMPTZ NOT NULL DEFAULT now(),
    detected_distribution       TEXT,
    detection_confidence        DOUBLE PRECISION,
    shapiro_wilk_p              DOUBLE PRECISION,
    skewness                    DOUBLE PRECISION,
    kurtosis                    DOUBLE PRECISION,
    bimodal_score               DOUBLE PRECISION,
    recommended_qc_approach     TEXT,
    sample_size                 INTEGER
);

COMMENT ON TABLE distribution_predictions IS 'Distribution-shape detections used to pick Westgard vs QConnect.';

CREATE INDEX IF NOT EXISTS idx_dist_pred_analyte ON distribution_predictions (analyte_code, analysis_date);


-- =====================================================================
-- ai_predictions
-- ---------------------------------------------------------------------
-- ML-inference outputs attached to specific QC results.
-- =====================================================================
CREATE TABLE IF NOT EXISTS ai_predictions (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    qc_result_id                UUID REFERENCES qc_results (id) ON DELETE CASCADE,
    prediction_timestamp        TIMESTAMPTZ NOT NULL DEFAULT now(),
    failure_probability_48h     DOUBLE PRECISION,
    failure_risk_level          TEXT,
    timeline_hours              DOUBLE PRECISION,
    contributing_factors        JSONB,
    anomaly_detected            BOOLEAN,
    anomaly_score               DOUBLE PRECISION,
    model_version               TEXT,
    model_confidence            DOUBLE PRECISION
);

COMMENT ON TABLE  ai_predictions IS 'AI/ML enrichment (failure probability, anomaly) per QC result.';
COMMENT ON COLUMN ai_predictions.contributing_factors IS 'Feature attributions / explanation payload (JSON).';

CREATE INDEX IF NOT EXISTS idx_ai_pred_qc_result ON ai_predictions (qc_result_id);


-- =====================================================================
-- federated_learning_updates
-- ---------------------------------------------------------------------
-- One row per global aggregation round of the federated model.
-- =====================================================================
CREATE TABLE IF NOT EXISTS federated_learning_updates (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    round_number                    INTEGER,
    participating_labs              INTEGER,
    global_model_version            TEXT,
    aggregation_timestamp           TIMESTAMPTZ NOT NULL DEFAULT now(),
    differential_privacy_epsilon    DOUBLE PRECISION,
    global_model_accuracy           DOUBLE PRECISION,
    global_model_auc                DOUBLE PRECISION
);

COMMENT ON TABLE  federated_learning_updates IS 'Federated training rounds with DP epsilon and global metrics.';
COMMENT ON COLUMN federated_learning_updates.differential_privacy_epsilon IS 'Privacy budget spent in the round (lower = stronger privacy).';

CREATE INDEX IF NOT EXISTS idx_fl_round ON federated_learning_updates (round_number);


-- =====================================================================
-- audit_trail
-- ---------------------------------------------------------------------
-- IMMUTABLE / APPEND-ONLY. Every privileged mutation is recorded here for
-- ISO 15189 / 21 CFR Part 11 / GDPR accountability. Application code MUST only
-- INSERT into this table; UPDATE/DELETE should be revoked at the DB-role level
-- in production (and ideally enforced with a BEFORE UPDATE/DELETE trigger).
-- =====================================================================
CREATE TABLE IF NOT EXISTS audit_trail (
    id                          BIGSERIAL PRIMARY KEY,
    action_timestamp            TIMESTAMPTZ NOT NULL DEFAULT now(),
    entity_type                 TEXT,
    entity_id                   UUID,
    action                      TEXT CHECK (action IN ('CREATE','UPDATE','DELETE','APPROVED')),
    user_id                     TEXT,
    system_component            TEXT,
    old_value                   JSONB,
    new_value                   JSONB,
    change_reason               TEXT
);

COMMENT ON TABLE  audit_trail IS 'Immutable append-only audit log (ISO 15189 / 21 CFR Part 11). INSERT only.';
COMMENT ON COLUMN audit_trail.action IS 'CREATE | UPDATE | DELETE | APPROVED.';

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_trail (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_time   ON audit_trail (action_timestamp);


-- =====================================================================
-- Deferred / cross FKs added after all tables exist.
-- ---------------------------------------------------------------------
-- qc_results.corrective_action_id -> capa_actions.id (DEFERRABLE so a result
-- and its CAPA can be inserted in the same transaction in either order).
-- =====================================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_qc_results_capa'
    ) THEN
        ALTER TABLE qc_results
            ADD CONSTRAINT fk_qc_results_capa
            FOREIGN KEY (corrective_action_id)
            REFERENCES capa_actions (id)
            ON DELETE SET NULL
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$$;


-- =====================================================================
-- updated_at maintenance trigger (shared).
-- =====================================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_qc_results_updated') THEN
        CREATE TRIGGER trg_qc_results_updated BEFORE UPDATE ON qc_results
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_control_limits_updated') THEN
        CREATE TRIGGER trg_control_limits_updated BEFORE UPDATE ON control_limits
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_capa_actions_updated') THEN
        CREATE TRIGGER trg_capa_actions_updated BEFORE UPDATE ON capa_actions
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END
$$;

-- End of init.sql
