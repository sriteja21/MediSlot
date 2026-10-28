
CREATE TABLE IF NOT EXISTS user_identities (
    email VARCHAR(254) NOT NULL,
    role ENUM('doctor', 'patient') NOT NULL,
    user_id CHAR(36) NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (email),
    UNIQUE KEY uq_identity_user (role, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS specialisations (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    UNIQUE KEY uq_spec_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS hospitals (
    id CHAR(36) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    address_line VARCHAR(255) NOT NULL,
    city VARCHAR(100) NOT NULL,
    state VARCHAR(100),
    postal_code VARCHAR(20),
    UNIQUE KEY uq_hospital (name, city, address_line),
    KEY idx_hospital_city_name (city, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS doctors (
    id CHAR(36) PRIMARY KEY,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(100) NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    licence_number VARCHAR(50) NOT NULL,
    specialisation_id INT UNSIGNED NOT NULL,
    hospital_id CHAR(36) NOT NULL,
    years_experience TINYINT UNSIGNED NOT NULL DEFAULT 0,
    qualifications VARCHAR(500),
    timezone VARCHAR(64) NOT NULL,
    photo_s3_key VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),

    UNIQUE KEY uq_doctor_email (email),
    UNIQUE KEY uq_doctor_licence (licence_number),
    KEY idx_doctor_name (full_name),
    KEY idx_doctor_spec_hosp (specialisation_id, hospital_id, full_name),
    KEY idx_doctor_hospital (hospital_id),

    CONSTRAINT fk_doc_spec
        FOREIGN KEY (specialisation_id) REFERENCES specialisations(id),

    CONSTRAINT fk_doc_hosp
        FOREIGN KEY (hospital_id) REFERENCES hospitals(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS doctor_languages (
    doctor_id CHAR(36) NOT NULL,
    language VARCHAR(50) NOT NULL,

    PRIMARY KEY (doctor_id, language),

    CONSTRAINT fk_lang_doc
        FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS patients (
    id CHAR(36) PRIMARY KEY,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(100) NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    date_of_birth DATE NOT NULL,
    gender ENUM('female', 'male', 'other', 'undisclosed') NOT NULL,
    phone VARCHAR(20) NOT NULL,
    city VARCHAR(100) NOT NULL,
    emergency_contact_name VARCHAR(150),
    emergency_contact_phone VARCHAR(20),
    notes TEXT,
    deleted_at DATETIME(3),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),

    UNIQUE KEY uq_patient_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS consultation_types (
    id CHAR(36) PRIMARY KEY,
    doctor_id CHAR(36) NOT NULL,
    kind ENUM('QUICK', 'STANDARD', 'DETAILED', 'HEALTH_CAMP') NOT NULL,
    name VARCHAR(100) NOT NULL,
    duration_minutes SMALLINT UNSIGNED NOT NULL,
    fee_amount DECIMAL(10,2) NOT NULL,
    capacity TINYINT UNSIGNED NOT NULL DEFAULT 1,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    UNIQUE KEY uq_ct_doctor_name (doctor_id, name),
    KEY idx_ct_doctor (doctor_id, is_active),

    CONSTRAINT fk_ct_doc
        FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        ON DELETE CASCADE,

    CONSTRAINT ck_ct_capacity
        CHECK (capacity BETWEEN 1 AND 10),

    CONSTRAINT ck_ct_fee
        CHECK (fee_amount >= 0),

    CONSTRAINT ck_ct_kind_cap
        CHECK (
            (kind <> 'HEALTH_CAMP' AND capacity = 1)
            OR
            (kind = 'HEALTH_CAMP' AND capacity BETWEEN 2 AND 10)
        )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS availability_patterns (
    id CHAR(36) PRIMARY KEY,
    doctor_id CHAR(36) NOT NULL,
    day_of_week TINYINT UNSIGNED NOT NULL,
    start_local_time TIME NOT NULL,
    end_local_time TIME NOT NULL,
    consultation_type_id CHAR(36) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),

    KEY idx_ap_doctor_dow (doctor_id, day_of_week),

    CONSTRAINT fk_ap_doc
        FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        ON DELETE CASCADE,

    CONSTRAINT fk_ap_ct
        FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(id),

    CONSTRAINT ck_ap_dow
        CHECK (day_of_week BETWEEN 1 AND 7),

    CONSTRAINT ck_ap_time
        CHECK (end_local_time > start_local_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS blackout_dates (
    id CHAR(36) PRIMARY KEY,
    doctor_id CHAR(36) NOT NULL,
    blackout_date DATE NOT NULL,
    reason VARCHAR(200),

    UNIQUE KEY uq_blackout (doctor_id, blackout_date),

    CONSTRAINT fk_bo_doc
        FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS appointments (
    id CHAR(36) PRIMARY KEY,
    patient_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    consultation_type_id CHAR(36) NOT NULL,
    slot_id CHAR(36) NOT NULL,
    slot_start_utc DATETIME(3) NOT NULL,
    slot_end_utc DATETIME(3) NOT NULL,
    doctor_timezone VARCHAR(64) NOT NULL,

    status ENUM(
        'CONFIRMED',
        'COMPLETED',
        'CANCELLED_BY_PATIENT',
        'CANCELLED_BY_DOCTOR',
        'NO_SHOW'
    ) NOT NULL DEFAULT 'CONFIRMED',

    fee_amount DECIMAL(10,2) NOT NULL,
    reschedule_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
    idempotency_key VARCHAR(128),
    source_offer_id CHAR(36),
    booked_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    closed_at DATETIME(3),

    confirmed_slot_id CHAR(36)
        GENERATED ALWAYS AS (
            IF(status = 'CONFIRMED', slot_id, NULL)
        ) STORED,

    UNIQUE KEY uq_patient_slot_confirmed (patient_id, confirmed_slot_id),
    UNIQUE KEY uq_patient_idem (patient_id, idempotency_key),

    KEY idx_appt_patient_time (patient_id, slot_start_utc),
    KEY idx_appt_patient_doctor (patient_id, doctor_id, status, slot_start_utc),
    KEY idx_appt_doctor_time (doctor_id, slot_start_utc, status),
    KEY idx_appt_slot (slot_id, status),
    KEY idx_appt_noshow (status, slot_end_utc),
    KEY idx_appt_remind (status, slot_start_utc),

    CONSTRAINT fk_ap_patient
        FOREIGN KEY (patient_id) REFERENCES patients(id),

    CONSTRAINT fk_ap_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(id),

    CONSTRAINT fk_ap_ctype
        FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(id),

    CONSTRAINT ck_appt_resched
        CHECK (reschedule_count <= 2)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS appointment_events (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    appointment_id CHAR(36) NOT NULL,

    event_type ENUM(
        'BOOKED',
        'RESCHEDULED',
        'CANCELLED_BY_PATIENT',
        'CANCELLED_BY_DOCTOR',
        'COMPLETED',
        'NO_SHOW'
    ) NOT NULL,

    from_slot_id CHAR(36),
    to_slot_id CHAR(36),

    actor_role ENUM('doctor', 'patient', 'system') NOT NULL,
    actor_id CHAR(36),
    correlation_id CHAR(36) NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    KEY idx_ae_appt (appointment_id, created_at),

    CONSTRAINT fk_ae_appt
        FOREIGN KEY (appointment_id) REFERENCES appointments(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS refund_ledger (
    id CHAR(36) PRIMARY KEY,
    appointment_id CHAR(36) NOT NULL,
    patient_id CHAR(36) NOT NULL,

    original_fee DECIMAL(10,2) NOT NULL,
    refund_percent DECIMAL(5,2) NOT NULL,
    refund_amount DECIMAL(10,2) NOT NULL,
    deduction_amount DECIMAL(10,2) NOT NULL,

    tier ENUM(
        'GT_48H',
        'H24_48',
        'H6_24',
        'LT_6H',
        'DOCTOR_CANCEL',
        'NO_SHOW'
    ) NOT NULL,

    hours_before_start DECIMAL(8,2),
    correlation_id CHAR(36) NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    UNIQUE KEY uq_refund_appt (appointment_id),
    KEY idx_refund_patient (patient_id, created_at),

    CONSTRAINT fk_rf_appt
        FOREIGN KEY (appointment_id) REFERENCES appointments(id),

    CONSTRAINT ck_rf_math
        CHECK (refund_amount + deduction_amount = original_fee)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
